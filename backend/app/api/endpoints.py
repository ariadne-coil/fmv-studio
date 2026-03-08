from datetime import datetime, timezone
import asyncio
import os
import uuid
from typing import List, Optional

from fastapi import APIRouter, File, Header, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel

from app.agent.models import ProjectState, AgentStage, PipelineRunState, PipelineRunStatus
from app.agent.graph import FMVAgentPipeline
from app.genai_runtime import build_genai_client, uses_vertex_ai
from app.job_queue import (
    cancel_local_pipeline_task,
    clear_local_pipeline_task,
    enqueue_pipeline_job,
    get_internal_task_token,
    get_job_driver,
    is_local_pipeline_task_active,
)
from app.storage import get_storage_backend
from google import genai as _genai

router = APIRouter()

STAGE_ORDER = ["input", "lyria_prompting", "planning", "storyboarding", "filming", "production", "completed"]


class ProjectSummary(BaseModel):
    project_id: str
    name: str
    current_stage: AgentStage
    updated_at: str
    final_video_url: Optional[str] = None


class ProjectRunStatus(BaseModel):
    is_running: bool
    stage: Optional[AgentStage] = None
    started_at: Optional[str] = None
    status: Optional[PipelineRunStatus] = None
    driver: Optional[str] = None


class ExecutePipelineRunRequest(BaseModel):
    project_id: str
    run_id: str
    api_key: Optional[str] = None
    orchestrator_model: Optional[str] = None
    critic_model: Optional[str] = None
    text_model: Optional[str] = None
    image_model: Optional[str] = None
    video_model: Optional[str] = None
    music_model: Optional[str] = None
    stage_voice_briefs_enabled: Optional[bool] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_optional_header(value: object) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def _coerce_optional_bool_header(value: object) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None

    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _resolve_music_provider(override: Optional[str], state: ProjectState) -> Optional[str]:
    return override or state.music_provider


def _resolve_image_provider(override: Optional[str], state: ProjectState) -> Optional[str]:
    return override or state.image_provider


def _resolve_video_provider(override: Optional[str], state: ProjectState) -> Optional[str]:
    return override or state.video_provider


def _resolve_orchestrator_model(override: Optional[str], legacy_override: Optional[str]) -> Optional[str]:
    return override or legacy_override


def _resolve_critic_model(override: Optional[str]) -> Optional[str]:
    return override


def _write_project(project_id: str, state: ProjectState) -> ProjectState:
    get_storage_backend().write_project_state(project_id, state.model_dump_json())
    return state


def _collect_project_asset_paths(state: ProjectState) -> list[str]:
    asset_paths: set[str] = set()

    if state.music_url:
        asset_paths.add(state.music_url)
    if state.final_video_url:
        asset_paths.add(state.final_video_url)

    for asset in state.assets:
        if asset.url:
            asset_paths.add(asset.url)

    for summary in state.stage_summaries.values():
        if summary.audio_url:
            asset_paths.add(summary.audio_url)

    for clip in state.timeline:
        if clip.image_url:
            asset_paths.add(clip.image_url)
        if clip.video_url:
            asset_paths.add(clip.video_url)

    return sorted(asset_paths)


def _stage_index(stage: AgentStage | str) -> int:
    value = stage.value if isinstance(stage, AgentStage) else stage
    return STAGE_ORDER.index(value)


def _planning_signature(state: ProjectState) -> list[tuple[str, float, float, str]]:
    return [
        (
            clip.id,
            round(float(clip.timeline_start), 3),
            round(float(clip.duration), 3),
            (clip.storyboard_text or "").strip(),
        )
        for clip in state.timeline
    ]


def _trim_stage_summaries_to(state: ProjectState, max_stage: AgentStage) -> None:
    max_index = _stage_index(max_stage)
    state.stage_summaries = {
        stage_name: summary
        for stage_name, summary in state.stage_summaries.items()
        if stage_name in STAGE_ORDER and _stage_index(stage_name) <= max_index
    }


def _clear_clip_storyboard_outputs(clip) -> None:
    clip.image_prompt = None
    clip.image_url = None
    clip.image_critiques = []
    clip.image_approved = False
    clip.image_score = None
    clip.image_reference_ready = False
    clip.video_prompt = None
    clip.video_url = None
    clip.video_critiques = []
    clip.video_score = None
    clip.video_approved = False


def _reconcile_after_planning_edits(previous: ProjectState, state: ProjectState) -> None:
    previous_clips = {clip.id: clip for clip in previous.timeline}

    for clip in state.timeline:
        previous_clip = previous_clips.get(clip.id)
        if previous_clip is None:
            _clear_clip_storyboard_outputs(clip)
            continue

        storyboard_changed = (previous_clip.storyboard_text or "").strip() != (clip.storyboard_text or "").strip()
        duration_changed = round(float(previous_clip.duration), 3) != round(float(clip.duration), 3)
        if storyboard_changed or duration_changed:
            _clear_clip_storyboard_outputs(clip)

    state.production_timeline = []
    state.final_video_url = None
    state.last_error = None
    state.stage_summaries = {
        stage_name: summary
        for stage_name, summary in state.stage_summaries.items()
        if stage_name in STAGE_ORDER and _stage_index(stage_name) < _stage_index(AgentStage.PLANNING)
    }

    if not state.timeline:
        state.current_stage = AgentStage.PLANNING
        return

    if all(clip.video_approved and clip.video_url for clip in state.timeline):
        state.current_stage = AgentStage.PRODUCTION
        return

    if all(clip.image_approved and clip.image_url for clip in state.timeline):
        state.current_stage = AgentStage.FILMING
        return

    state.current_stage = AgentStage.STORYBOARDING


def _get_project_run_state(project_id: str) -> PipelineRunState | None:
    try:
        state = get_project(project_id)
    except HTTPException:
        return None

    active_run = state.active_run
    if not active_run:
        return None

    if active_run.driver == "local" and not is_local_pipeline_task_active(project_id, active_run.run_id):
        state.active_run = None
        _write_project(project_id, state)
        return None

    return active_run


def _is_pipeline_run_current(project_id: str, run_id: str) -> bool:
    active_run = _get_project_run_state(project_id)
    return bool(
        active_run
        and active_run.run_id == run_id
        and active_run.status in {PipelineRunStatus.QUEUED, PipelineRunStatus.RUNNING}
    )


async def _persist_pipeline_progress(project_id: str, run_id: str, state: ProjectState) -> None:
    if not _is_pipeline_run_current(project_id, run_id):
        raise asyncio.CancelledError("Pipeline run superseded")
    current = get_project(project_id)
    if not current.active_run or current.active_run.run_id != run_id:
        raise asyncio.CancelledError("Pipeline run superseded")
    current.active_run.updated_at = _now_iso()
    current.active_run.status = PipelineRunStatus.RUNNING
    state.active_run = current.active_run
    _write_project(project_id, state)


def _prepare_project_for_async_run(
    state: ProjectState,
    *,
    stage: AgentStage,
    driver: str,
) -> ProjectState:
    now = _now_iso()
    state.current_stage = stage
    state.last_error = None
    state.active_run = PipelineRunState(
        run_id=uuid.uuid4().hex,
        stage=stage,
        status=PipelineRunStatus.QUEUED,
        driver=driver,
        started_at=now,
        updated_at=now,
    )
    return state


def _clear_project_run_state(project_id: str, run_id: str | None = None) -> None:
    state = get_project(project_id)
    if not state.active_run:
        clear_local_pipeline_task(project_id, run_id)
        return
    if run_id and state.active_run.run_id != run_id:
        clear_local_pipeline_task(project_id, run_id)
        return

    state.active_run = None
    _write_project(project_id, state)
    clear_local_pipeline_task(project_id, run_id)


async def _execute_pipeline_run(
    project_id: str,
    run_id: str,
    *,
    api_key: Optional[str],
    orchestrator_model: Optional[str],
    critic_model: Optional[str],
    image_model: Optional[str],
    video_model: Optional[str],
    music_model: Optional[str],
    stage_voice_briefs_enabled: Optional[bool],
) -> None:
    try:
        state = get_project(project_id)
        if not state.active_run or state.active_run.run_id != run_id:
            return
        state.active_run.status = PipelineRunStatus.RUNNING
        state.active_run.updated_at = _now_iso()
        _write_project(project_id, state)
        pipeline = FMVAgentPipeline(
            api_key=api_key,
            orchestrator_model=orchestrator_model,
            critic_model=critic_model,
            image_model=_resolve_image_provider(image_model, state),
            video_model=_resolve_video_provider(video_model, state),
            music_model=_resolve_music_provider(music_model, state),
            stage_voice_briefs_enabled=True if stage_voice_briefs_enabled is None else stage_voice_briefs_enabled,
            persist_state_callback=lambda next_state: _persist_pipeline_progress(project_id, run_id, next_state),
            is_cancelled=lambda: not _is_pipeline_run_current(project_id, run_id),
        )
        new_state = await pipeline.run_pipeline(state)
        if _is_pipeline_run_current(project_id, run_id):
            new_state.active_run = None
            _write_project(project_id, new_state)
    except asyncio.CancelledError:
        return
    finally:
        clear_local_pipeline_task(project_id, run_id)
        if _is_pipeline_run_current(project_id, run_id):
            _clear_project_run_state(project_id, run_id)


@router.get("/projects", response_model=List[ProjectSummary])
def list_projects():
    projects: list[ProjectSummary] = []
    for stored in get_storage_backend().list_project_states():
        try:
            state = ProjectState.model_validate_json(stored.data)
        except Exception:
            continue

        projects.append(
            ProjectSummary(
                project_id=state.project_id,
                name=state.name,
                current_stage=state.current_stage,
                updated_at=stored.updated_at,
                final_video_url=state.final_video_url,
            )
        )

    return projects

@router.post("/projects", response_model=ProjectState)
def create_project(project: ProjectState):
    storage = get_storage_backend()
    if storage.project_exists(project.project_id):
        raise HTTPException(status_code=400, detail="Project already exists")
    storage.write_project_state(project.project_id, project.model_dump_json())
    return project


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    state = get_project(project_id)
    active_run = _get_project_run_state(project_id)
    if active_run and active_run.status in {PipelineRunStatus.QUEUED, PipelineRunStatus.RUNNING}:
        raise HTTPException(status_code=409, detail="Cannot delete a project while a pipeline run is active")

    get_storage_backend().delete_project(
        project_id,
        asset_paths=_collect_project_asset_paths(state),
        asset_prefixes=(
            f"uploads/{project_id}/",
            f"{project_id}_",
        ),
    )
    clear_local_pipeline_task(project_id)
    return Response(status_code=204)

@router.get("/projects/{project_id}", response_model=ProjectState)
def get_project(project_id: str):
    storage = get_storage_backend()
    try:
        return ProjectState.model_validate_json(storage.read_project_state(project_id))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc

@router.put("/projects/{project_id}", response_model=ProjectState)
def update_project(project_id: str, state: ProjectState):
    if get_storage_backend().project_exists(project_id):
        current = get_project(project_id)
        if _planning_signature(current) != _planning_signature(state):
            _reconcile_after_planning_edits(current, state)
    return _write_project(project_id, state)


@router.get("/projects/{project_id}/run-status", response_model=ProjectRunStatus)
def get_project_run_status(project_id: str):
    run = _get_project_run_state(project_id)
    if not run or run.status not in {PipelineRunStatus.QUEUED, PipelineRunStatus.RUNNING}:
        return ProjectRunStatus(is_running=False)

    return ProjectRunStatus(
        is_running=True,
        stage=run.stage,
        started_at=run.started_at,
        status=run.status,
        driver=run.driver,
    )

@router.post("/projects/{project_id}/upload")
async def upload_asset(project_id: str, file: UploadFile = File(...)):
    """
    Saves an uploaded file (image or audio) to disk under projects/uploads/{project_id}/.
    Returns {"url": <server-local path>, "name": <original filename>}.
    The URL is an absolute server path that graph.py can open directly.
    """
    storage = get_storage_backend()
    ext = os.path.splitext(file.filename or "file")[1] or ""
    unique_name = f"{uuid.uuid4().hex}{ext}"
    content = await file.read()
    relative_path = f"uploads/{project_id}/{unique_name}"
    url = storage.write_project_asset_bytes(
        relative_path,
        content,
        content_type=file.content_type,
    )
    return {"url": url, "name": file.filename or unique_name}


@router.post("/projects/{project_id}/regenerate-music", response_model=ProjectState)
async def regenerate_music_preview(
    project_id: str,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_music_model: Optional[str] = Header(default=None, alias="X-Music-Model"),
):
    x_api_key = _coerce_optional_header(x_api_key)
    x_music_model = _coerce_optional_header(x_music_model)

    state = get_project(project_id)
    if state.current_stage != AgentStage.LYRIA_PROMPTING:
        raise HTTPException(status_code=400, detail="Music generation is only available during the Music stage")

    previous_music_url = state.music_url
    pipeline = FMVAgentPipeline(
        api_key=x_api_key,
        music_model=_resolve_music_provider(x_music_model, state),
    )
    state.music_provider = pipeline.music_provider_id
    if not pipeline.music_provider.can_generate_automatically():
        raise HTTPException(
            status_code=400,
            detail=pipeline.music_provider.blocking_message(state)
            or f"{pipeline.music_provider.definition.label} does not support automatic generation in this build.",
        )

    try:
        await pipeline._generate_music_track(state)
        state.last_error = None
    except Exception as exc:
        state.music_url = previous_music_url
        state.last_error = f"{pipeline.music_provider.definition.label} generation failed: {str(exc)}"
        _write_project(project_id, state)
        raise HTTPException(status_code=500, detail=state.last_error) from exc

    _write_project(project_id, state)
    return state


@router.post("/projects/{project_id}/run", response_model=ProjectState)
async def run_pipeline_step(
    project_id: str,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_orchestrator_model: Optional[str] = Header(default=None, alias="X-Orchestrator-Model"),
    x_critic_model: Optional[str] = Header(default=None, alias="X-Critic-Model"),
    x_text_model: Optional[str] = Header(default=None, alias="X-Text-Model"),
    x_image_model: Optional[str] = Header(default=None, alias="X-Image-Model"),
    x_video_model: Optional[str] = Header(default=None, alias="X-Video-Model"),
    x_music_model: Optional[str] = Header(default=None, alias="X-Music-Model"),
    x_stage_voice_briefs_enabled: Optional[str] = Header(default=None, alias="X-Stage-Voice-Briefs-Enabled"),
):
    x_api_key = _coerce_optional_header(x_api_key)
    x_orchestrator_model = _coerce_optional_header(x_orchestrator_model)
    x_critic_model = _coerce_optional_header(x_critic_model)
    x_text_model = _coerce_optional_header(x_text_model)
    x_image_model = _coerce_optional_header(x_image_model)
    x_video_model = _coerce_optional_header(x_video_model)
    x_music_model = _coerce_optional_header(x_music_model)
    stage_voice_briefs_enabled = _coerce_optional_bool_header(x_stage_voice_briefs_enabled)

    state = get_project(project_id)
    if _get_project_run_state(project_id):
        raise HTTPException(status_code=409, detail="A background pipeline run is already active for this project")
    pipeline = FMVAgentPipeline(
        api_key=x_api_key,
        orchestrator_model=_resolve_orchestrator_model(x_orchestrator_model, x_text_model),
        critic_model=_resolve_critic_model(x_critic_model),
        image_model=_resolve_image_provider(x_image_model, state),
        video_model=_resolve_video_provider(x_video_model, state),
        music_model=_resolve_music_provider(x_music_model, state),
        stage_voice_briefs_enabled=True if stage_voice_briefs_enabled is None else stage_voice_briefs_enabled,
    )
    new_state = await pipeline.run_pipeline(state)
    _write_project(project_id, new_state)
    return new_state


@router.post("/projects/{project_id}/run-async", response_model=ProjectState)
async def run_pipeline_step_async(
    project_id: str,
    request: Request = None,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_orchestrator_model: Optional[str] = Header(default=None, alias="X-Orchestrator-Model"),
    x_critic_model: Optional[str] = Header(default=None, alias="X-Critic-Model"),
    x_text_model: Optional[str] = Header(default=None, alias="X-Text-Model"),
    x_image_model: Optional[str] = Header(default=None, alias="X-Image-Model"),
    x_video_model: Optional[str] = Header(default=None, alias="X-Video-Model"),
    x_music_model: Optional[str] = Header(default=None, alias="X-Music-Model"),
    x_stage_voice_briefs_enabled: Optional[str] = Header(default=None, alias="X-Stage-Voice-Briefs-Enabled"),
):
    x_api_key = _coerce_optional_header(x_api_key)
    x_orchestrator_model = _coerce_optional_header(x_orchestrator_model)
    x_critic_model = _coerce_optional_header(x_critic_model)
    x_text_model = _coerce_optional_header(x_text_model)
    x_image_model = _coerce_optional_header(x_image_model)
    x_video_model = _coerce_optional_header(x_video_model)
    x_music_model = _coerce_optional_header(x_music_model)
    stage_voice_briefs_enabled = _coerce_optional_bool_header(x_stage_voice_briefs_enabled)

    existing_run = _get_project_run_state(project_id)
    if existing_run and existing_run.status in {PipelineRunStatus.QUEUED, PipelineRunStatus.RUNNING}:
        return get_project(project_id)

    state = get_project(project_id)
    driver = get_job_driver()
    pipeline = FMVAgentPipeline(
        api_key=x_api_key,
        orchestrator_model=_resolve_orchestrator_model(x_orchestrator_model, x_text_model),
        critic_model=_resolve_critic_model(x_critic_model),
        image_model=_resolve_image_provider(x_image_model, state),
        video_model=_resolve_video_provider(x_video_model, state),
        music_model=_resolve_music_provider(x_music_model, state),
        stage_voice_briefs_enabled=True if stage_voice_briefs_enabled is None else stage_voice_briefs_enabled,
    )

    if state.current_stage == AgentStage.PLANNING:
        if not state.timeline:
            raise HTTPException(status_code=400, detail="Cannot start storyboarding until planning has produced a shot list")
        _prepare_project_for_async_run(state, stage=AgentStage.STORYBOARDING, driver=driver)
    elif state.current_stage == AgentStage.STORYBOARDING:
        if all(clip.image_approved for clip in state.timeline):
            await pipeline._normalize_timeline_for_veo(state)
            _prepare_project_for_async_run(state, stage=AgentStage.FILMING, driver=driver)
        else:
            _prepare_project_for_async_run(state, stage=AgentStage.STORYBOARDING, driver=driver)
    elif state.current_stage == AgentStage.FILMING:
        if all(clip.video_approved and clip.video_url for clip in state.timeline):
            raise HTTPException(status_code=400, detail="Filming is already complete for this project")
        _prepare_project_for_async_run(state, stage=AgentStage.FILMING, driver=driver)
    else:
        raise HTTPException(status_code=400, detail="Asynchronous pipeline runs are only supported during storyboarding and filming")

    _write_project(project_id, state)
    payload = ExecutePipelineRunRequest(
        project_id=project_id,
        run_id=state.active_run.run_id,
        api_key=x_api_key,
        orchestrator_model=_resolve_orchestrator_model(x_orchestrator_model, x_text_model),
        critic_model=_resolve_critic_model(x_critic_model),
        text_model=x_text_model,
        image_model=x_image_model,
        video_model=x_video_model,
        music_model=x_music_model,
        stage_voice_briefs_enabled=stage_voice_briefs_enabled,
    )
    try:
        await enqueue_pipeline_job(
            project_id=project_id,
            run_id=state.active_run.run_id,
            payload=payload.model_dump(),
            base_url=str(request.base_url).rstrip("/") if request else None,
            execute_local=lambda: _execute_pipeline_run(
                project_id,
                state.active_run.run_id,
                api_key=x_api_key,
                orchestrator_model=_resolve_orchestrator_model(x_orchestrator_model, x_text_model),
                critic_model=_resolve_critic_model(x_critic_model),
                image_model=x_image_model,
                video_model=x_video_model,
                music_model=x_music_model,
                stage_voice_briefs_enabled=stage_voice_briefs_enabled,
            ),
        )
    except Exception as exc:
        _clear_project_run_state(project_id, state.active_run.run_id)
        state = get_project(project_id)
        state.last_error = f"Failed to enqueue pipeline run: {str(exc)}"
        _write_project(project_id, state)
        raise HTTPException(status_code=500, detail=state.last_error) from exc

    return get_project(project_id)


@router.post("/internal/projects/{project_id}/execute-run", response_model=ProjectState)
async def execute_pipeline_run_internal(
    project_id: str,
    body: ExecutePipelineRunRequest,
    x_internal_task_token: Optional[str] = Header(default=None, alias="X-Internal-Task-Token"),
):
    expected_token = get_internal_task_token()
    if expected_token and x_internal_task_token != expected_token:
        raise HTTPException(status_code=401, detail="Invalid internal task token")
    if body.project_id != project_id:
        raise HTTPException(status_code=400, detail="Project id mismatch")

    state = get_project(project_id)
    if not state.active_run or state.active_run.run_id != body.run_id:
        return state
    if state.active_run.status == PipelineRunStatus.RUNNING:
        return state

    await _execute_pipeline_run(
        project_id,
        body.run_id,
        api_key=body.api_key,
        orchestrator_model=_resolve_orchestrator_model(body.orchestrator_model, body.text_model),
        critic_model=_resolve_critic_model(body.critic_model),
        image_model=body.image_model,
        video_model=body.video_model,
        music_model=body.music_model,
        stage_voice_briefs_enabled=body.stage_voice_briefs_enabled,
    )
    return get_project(project_id)


class RevertRequest(BaseModel):
    target_stage: str


@router.post("/projects/{project_id}/revert", response_model=ProjectState)
def revert_pipeline(project_id: str, body: RevertRequest):
    state = get_project(project_id)
    target = body.target_stage
    if state.active_run:
        cancel_local_pipeline_task(project_id)
        state.active_run = None

    VALID_TARGETS = {"input", "lyria_prompting", "planning", "storyboarding", "filming", "production"}
    if target not in VALID_TARGETS:
        raise HTTPException(status_code=400, detail=f"Invalid target_stage '{target}'")

    STAGE_ORDER = ["input", "lyria_prompting", "planning", "storyboarding", "filming", "production", "completed"]
    target_idx = STAGE_ORDER.index(target)

    # Reverting all the way to 'input' is a full reset — clear everything
    if target == "input":
        state.timeline = []
        state.lyrics_prompt = ""
        state.style_prompt = ""

    # Reverting to lyria_prompting: just change stage so the user can re-edit
    # the music prompts. Keep lyrics_prompt and style_prompt intact so they
    # can tweak them rather than losing their work.
    # (No data cleared here — only the stage changes.)

    # Clear storyboard images when going back to planning or earlier (but not input)
    if target_idx <= STAGE_ORDER.index("planning") and target != "input":
        for clip in state.timeline:
            clip.image_url = None
            clip.image_prompt = None
            clip.image_critiques = []
            clip.image_approved = False

    # Clear videos when going back to storyboarding or earlier
    if target_idx <= STAGE_ORDER.index("storyboarding"):
        for clip in state.timeline:
            clip.video_url = None
            clip.video_prompt = None
            clip.video_critiques = []
            clip.video_approved = False
        state.production_timeline = []

    # Rebuild production edits after leaving filming.
    if target_idx <= STAGE_ORDER.index("filming"):
        state.production_timeline = []

    # Clear final video when reverting from completed/production/filming
    if target_idx <= STAGE_ORDER.index("production"):
        state.final_video_url = None

    state.stage_summaries = {
        stage_name: summary
        for stage_name, summary in state.stage_summaries.items()
        if stage_name in STAGE_ORDER and STAGE_ORDER.index(stage_name) <= target_idx
    }

    state.last_error = None
    state.current_stage = AgentStage(target)
    _write_project(project_id, state)
    return state


class FillClipRequest(BaseModel):
    clip_id: str
    clip_index: int
    total_clips: int
    surrounding_context: str   # adjacent clips' storyboard_text joined with " | "
    duration: float


@router.post("/projects/{project_id}/fill-clip")
async def fill_clip(
    project_id: str,
    body: FillClipRequest,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_orchestrator_model: Optional[str] = Header(default=None, alias="X-Orchestrator-Model"),
    x_text_model: Optional[str] = Header(default=None, alias="X-Text-Model"),
):
    """
    Uses Gemini to generate a storyboard description for a user-added shot.
    Returns {"storyboard_text": str}.
    """
    state = get_project(project_id)
    api_key = x_api_key or os.getenv("GEMINI_API_KEY")
    if not api_key and not uses_vertex_ai():
        raise HTTPException(status_code=400, detail="No API key available")

    client = build_genai_client(api_key=api_key)
    if client is None:
        raise HTTPException(status_code=500, detail="Google GenAI client is not configured")
    
    # Use selected model or default to 3.1 Pro
    orchestrator_model = _resolve_orchestrator_model(
        _coerce_optional_header(x_orchestrator_model),
        _coerce_optional_header(x_text_model),
    ) or "gemini-3-pro-preview"

    prompt = f"""You are an expert music video director.

The user is creating a music video with {body.total_clips} shots total.
This is shot {body.clip_index + 1} of {body.total_clips}, and it should last {body.duration:.1f} seconds.

Project context:
- Screenplay: {state.screenplay[:600]}
- Style instructions: {state.instructions[:300]}

Nearby shots for context:
{body.surrounding_context or "(no adjacent shots yet)"}

Write a single, vivid storyboard description for this shot. Be specific about:
- Subject / characters in frame
- Camera angle and movement
- Lighting and color palette
- Environment / background
- Overall mood

Return ONLY the storyboard description text, no bullet points, no JSON, no extra commentary. 2-4 sentences max."""

    response = client.models.generate_content(
        model=orchestrator_model,
        contents=[prompt],
        config=_genai.types.GenerateContentConfig(
            thinking_config=_genai.types.ThinkingConfig(thinking_budget=1024),
        ),
    )
    return {"storyboard_text": response.text.strip()}


