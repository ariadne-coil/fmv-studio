from datetime import datetime, timezone
import asyncio
import os
import uuid
from typing import List, Optional

from fastapi import APIRouter, File, Header, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field

from app.agent.models import AgentStage, PipelineRunState, PipelineRunStatus, ProductionTimelineFragment, ProjectState
from app.agent.graph import FMVAgentPipeline, _normalize_relevant_assets
from app.core.asset_context import (
    analyze_uploaded_asset,
    build_asset_reference_registry,
    build_asset_semantic_context,
    build_document_context,
)
from app.core.document_context import (
    extract_document_text,
    infer_asset_type,
    suggest_asset_label,
)
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
DEFAULT_STALE_RUN_TIMEOUT_SECONDS = {
    AgentStage.STORYBOARDING: 15 * 60,
    AgentStage.FILMING: 60 * 60,
}


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
    image_resolution: Optional[str] = None
    video_model: Optional[str] = None
    video_resolution: Optional[str] = None
    music_model: Optional[str] = None
    stage_voice_briefs_enabled: Optional[bool] = None


class AssetUploadPlanRequest(BaseModel):
    filename: str = Field(min_length=1)
    content_type: Optional[str] = None
    size: Optional[int] = Field(default=None, ge=0)


class AssetUploadPlanResponse(BaseModel):
    mode: str
    upload_url: Optional[str] = None
    upload_method: Optional[str] = None
    upload_headers: dict[str, str] = Field(default_factory=dict)
    url: Optional[str] = None
    name: Optional[str] = None
    label: Optional[str] = None
    asset_type: Optional[str] = None
    mime_type: Optional[str] = None
    text_content: Optional[str] = None
    ai_context: Optional[str] = None


class CompleteAssetUploadRequest(BaseModel):
    url: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    content_type: Optional[str] = None


class ClipApprovalRequest(BaseModel):
    approved: bool


class StoryboardFrameUploadRequest(BaseModel):
    url: str = Field(min_length=1)
    name: str = Field(min_length=1)


class AssetLabelUpdateRequest(BaseModel):
    label: Optional[str] = None


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


def _pipeline_run_stale_timeout_seconds(stage: AgentStage) -> int:
    override = os.getenv("FMV_PIPELINE_STALE_TIMEOUT_SECONDS", "").strip()
    if override:
        try:
            return max(60, int(override))
        except ValueError:
            pass
    return DEFAULT_STALE_RUN_TIMEOUT_SECONDS.get(stage, 15 * 60)


def _is_pipeline_run_stale(active_run: PipelineRunState) -> bool:
    try:
        updated_at = datetime.fromisoformat(active_run.updated_at)
    except Exception:
        return False

    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)

    age_seconds = (datetime.now(timezone.utc) - updated_at).total_seconds()
    return age_seconds > _pipeline_run_stale_timeout_seconds(active_run.stage)


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

    for turn in state.director_log:
        if turn.audio_url:
            asset_paths.add(turn.audio_url)

    for clip in state.timeline:
        if clip.image_url:
            asset_paths.add(clip.image_url)
        if clip.video_url:
            asset_paths.add(clip.video_url)

    return sorted(asset_paths)


def _stage_index(stage: AgentStage | str) -> int:
    value = stage.value if isinstance(stage, AgentStage) else stage
    return STAGE_ORDER.index(value)


def _coerce_agent_stage(value: AgentStage | str | None) -> AgentStage | None:
    if value is None:
        return None
    if isinstance(value, AgentStage):
        return value
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    try:
        return AgentStage(normalized)
    except ValueError:
        return None


def _previous_review_stage(stage: AgentStage | str | None) -> AgentStage | None:
    resolved = _coerce_agent_stage(stage)
    if resolved is None:
        return None
    if resolved == AgentStage.HALTED_FOR_REVIEW:
        return None
    index = _stage_index(resolved)
    if index <= 0:
        return None
    return AgentStage(STAGE_ORDER[index - 1])


def _infer_review_stage_for_halted_state(state: ProjectState) -> AgentStage:
    if state.final_video_url:
        return AgentStage.COMPLETED

    if any((fragment.track_type or "video") != "music" for fragment in state.production_timeline):
        return AgentStage.PRODUCTION

    if state.timeline:
        has_video_state = any(
            clip.video_url or clip.video_prompt or clip.video_critiques
            for clip in state.timeline
        )
        if has_video_state:
            return AgentStage.FILMING

        has_image_state = any(
            clip.image_url or clip.image_prompt or clip.image_critiques
            for clip in state.timeline
        )
        if has_image_state:
            return AgentStage.STORYBOARDING

        return AgentStage.PLANNING

    if state.music_workflow != "uploaded_track" and not state.music_url and (state.lyrics_prompt or state.style_prompt):
        return AgentStage.LYRIA_PROMPTING

    return AgentStage.INPUT


def _previous_review_stage_for_state(
    state: ProjectState,
    stage: AgentStage | str | None,
) -> AgentStage | None:
    resolved = _coerce_agent_stage(stage)
    if resolved is None:
        return None
    if resolved == AgentStage.HALTED_FOR_REVIEW:
        resolved = _infer_review_stage_for_halted_state(state)

    if state.music_workflow == "uploaded_track":
        stage_order = [
            AgentStage.INPUT,
            AgentStage.PLANNING,
            AgentStage.STORYBOARDING,
            AgentStage.FILMING,
            AgentStage.PRODUCTION,
            AgentStage.COMPLETED,
        ]
        if resolved not in stage_order:
            return _previous_review_stage(resolved)
        index = stage_order.index(resolved)
        if index <= 0:
            return None
        return stage_order[index - 1]

    return _previous_review_stage(resolved)


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


def _document_context_for_state(state: ProjectState, *, max_chars: int = 6000) -> str:
    return build_document_context(state.assets, max_chars=max_chars)


def _asset_reference_registry_for_state(state: ProjectState, *, max_chars: int = 2000) -> str:
    return build_asset_reference_registry(state.assets, max_chars=max_chars)


def _asset_semantic_context_for_state(state: ProjectState, *, max_chars: int = 3000) -> str:
    return build_asset_semantic_context(state.assets, max_chars=max_chars) or "(none)"


async def _build_uploaded_asset_response(
    *,
    filename: str,
    mime_type: str | None,
    asset_type: str,
    url: str,
    content: bytes | None = None,
    local_path: str | None = None,
    extracted_text: str | None = None,
    api_key: str | None = None,
) -> dict[str, object | None]:
    client = build_genai_client(api_key=api_key)
    ai_context = None
    try:
        ai_context = await analyze_uploaded_asset(
            client=client,
            filename=filename,
            label=suggest_asset_label(filename),
            mime_type=mime_type,
            asset_type=asset_type,
            content=content,
            local_path=local_path,
            extracted_text=extracted_text,
        )
    except Exception:
        ai_context = None

    return {
        "url": url,
        "name": filename,
        "label": suggest_asset_label(filename),
        "asset_type": asset_type,
        "mime_type": mime_type,
        "text_content": extracted_text,
        "ai_context": ai_context,
    }


def _clear_clip_storyboard_outputs(clip) -> None:
    clip.image_prompt = None
    clip.image_url = None
    clip.image_critiques = []
    clip.image_approved = False
    clip.image_score = None
    clip.image_reference_ready = False
    clip.image_manual_override = False
    clip.video_prompt = None
    clip.video_url = None
    clip.video_critiques = []
    clip.video_score = None
    clip.video_approved = False


def _clear_clip_video_outputs(clip) -> None:
    clip.video_prompt = None
    clip.video_url = None
    clip.video_critiques = []
    clip.video_score = None
    clip.video_approved = False


def _preserve_music_production_fragments(state: ProjectState) -> None:
    state.production_timeline = [
        fragment
        for fragment in state.production_timeline
        if state.music_url and (fragment.track_type or "video") == "music"
    ]


def _normalize_music_start_seconds(value: float | None) -> float:
    if value is None:
        return 0.0
    try:
        return round(max(0.0, float(value)), 3)
    except (TypeError, ValueError):
        return 0.0


def _default_music_fragment_for_state(state: ProjectState) -> list[ProductionTimelineFragment]:
    if not state.music_url:
        return []

    program_duration = 0.0
    if state.timeline:
        program_duration = max(
            program_duration,
            max((float(clip.timeline_start) + float(clip.duration) for clip in state.timeline), default=0.0),
        )

    if state.production_timeline:
        video_fragments = [
            fragment
            for fragment in state.production_timeline
            if (fragment.track_type or "video") != "music"
        ]
        program_duration = max(
            program_duration,
            max(
                (float(fragment.timeline_start) + float(fragment.duration) for fragment in video_fragments),
                default=0.0,
            ),
        )

    if program_duration <= 0:
        return []

    music_start = min(
        _normalize_music_start_seconds(state.music_start_seconds),
        max(0.0, round(program_duration - 0.1, 3)),
    )
    duration = max(0.1, round(program_duration - music_start, 3))
    return [
        ProductionTimelineFragment(
            id="music_frag_0",
            track_type="music",
            source_clip_id=None,
            timeline_start=music_start,
            source_start=0.0,
            duration=duration,
            audio_enabled=True,
        )
    ]


def _reconcile_after_music_start_edit(previous: ProjectState, state: ProjectState) -> None:
    state.music_start_seconds = _normalize_music_start_seconds(state.music_start_seconds)
    video_fragments = [
        fragment
        for fragment in state.production_timeline
        if (fragment.track_type or "video") != "music"
    ]
    state.production_timeline = video_fragments + _default_music_fragment_for_state(state)
    state.final_video_url = None
    state.last_error = None
    state.stage_summaries = {
        stage_name: summary
        for stage_name, summary in state.stage_summaries.items()
        if stage_name in STAGE_ORDER and _stage_index(stage_name) < _stage_index(AgentStage.PRODUCTION)
    }


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
            if previous_clip.image_manual_override and previous_clip.image_url:
                clip.image_prompt = previous_clip.image_prompt
                clip.image_url = previous_clip.image_url
                clip.image_critiques = list(previous_clip.image_critiques)
                clip.image_approved = previous_clip.image_approved
                clip.image_score = previous_clip.image_score
                clip.image_reference_ready = previous_clip.image_reference_ready
                clip.image_manual_override = True
                _clear_clip_video_outputs(clip)
                continue
            _clear_clip_storyboard_outputs(clip)

    _preserve_music_production_fragments(state)
    state.final_video_url = None
    state.last_error = None
    state.stage_summaries = {
        stage_name: summary
        for stage_name, summary in state.stage_summaries.items()
        if stage_name in STAGE_ORDER and _stage_index(stage_name) < _stage_index(AgentStage.PLANNING)
    }

    if not state.timeline:
        next_stage = AgentStage.PLANNING
    elif all(clip.video_approved and clip.video_url for clip in state.timeline):
        next_stage = AgentStage.PRODUCTION
    elif all(clip.image_approved and clip.image_url for clip in state.timeline):
        next_stage = AgentStage.FILMING
    else:
        next_stage = AgentStage.STORYBOARDING

    current_stage = _coerce_agent_stage(state.current_stage)
    if current_stage is None or current_stage == AgentStage.HALTED_FOR_REVIEW:
        state.current_stage = next_stage
        return

    if _stage_index(current_stage) < _stage_index(AgentStage.PLANNING):
        state.current_stage = AgentStage.PLANNING
        return

    if _stage_index(current_stage) < _stage_index(next_stage):
        state.current_stage = current_stage
        return

    state.current_stage = next_stage


def _apply_revert_to_state(state: ProjectState, target_stage: AgentStage | str) -> ProjectState:
    target = _coerce_agent_stage(target_stage)
    if target is None or target.value not in {"input", "lyria_prompting", "planning", "storyboarding", "filming", "production"}:
        raise HTTPException(status_code=400, detail=f"Invalid target_stage '{target_stage}'")

    target_idx = _stage_index(target)

    if target == AgentStage.INPUT:
        state.timeline = []
        state.lyrics_prompt = ""
        state.style_prompt = ""

    if target_idx <= _stage_index(AgentStage.PLANNING) and target != AgentStage.INPUT:
        for clip in state.timeline:
            clip.image_url = None
            clip.image_prompt = None
            clip.image_critiques = []
            clip.image_approved = False

    if target_idx <= _stage_index(AgentStage.STORYBOARDING):
        for clip in state.timeline:
            clip.video_url = None
            clip.video_prompt = None
            clip.video_critiques = []
            clip.video_approved = False
        _preserve_music_production_fragments(state)

    if target_idx <= _stage_index(AgentStage.FILMING):
        _preserve_music_production_fragments(state)

    if target_idx <= _stage_index(AgentStage.PRODUCTION):
        state.final_video_url = None

    _trim_stage_summaries_to(state, target)
    state.last_error = None
    state.current_stage = target
    return state


def _get_clip_or_404(state: ProjectState, clip_id: str):
    for clip in state.timeline:
        if clip.id == clip_id:
            return clip
    raise HTTPException(status_code=404, detail=f"Clip '{clip_id}' not found")


def _get_asset_or_404(state: ProjectState, asset_id: str):
    for asset in state.assets:
        if asset.id == asset_id:
            return asset
    raise HTTPException(status_code=404, detail=f"Asset '{asset_id}' not found")


def _apply_image_approval_change(state: ProjectState, clip_id: str, approved: bool) -> ProjectState:
    clip = _get_clip_or_404(state, clip_id)
    current_value = bool(clip.image_approved)
    if current_value == approved:
        return state
    if approved and not clip.image_url:
        raise HTTPException(status_code=400, detail="Cannot approve a storyboard clip without an image")

    clip.image_approved = approved
    clip.image_reference_ready = approved and bool(clip.image_url)
    state.last_error = None
    state.final_video_url = None

    if not approved:
        clip.video_prompt = None
        clip.video_url = None
        clip.video_critiques = []
        clip.video_score = None
        clip.video_approved = False

    _trim_stage_summaries_to(state, AgentStage.PLANNING)
    state.current_stage = AgentStage.STORYBOARDING
    return state


def _apply_video_approval_change(state: ProjectState, clip_id: str, approved: bool) -> ProjectState:
    clip = _get_clip_or_404(state, clip_id)
    current_value = bool(clip.video_approved)
    if current_value == approved:
        return state
    if approved and not clip.video_url:
        raise HTTPException(status_code=400, detail="Cannot approve a filming clip without a video")

    clip.video_approved = approved
    state.last_error = None
    state.final_video_url = None
    _trim_stage_summaries_to(state, AgentStage.STORYBOARDING)
    state.current_stage = AgentStage.FILMING
    return state


def _get_project_run_state(project_id: str) -> PipelineRunState | None:
    try:
        state = get_project(project_id)
    except HTTPException:
        return None

    active_run = state.active_run
    if not active_run:
        return None

    if _is_pipeline_run_stale(active_run):
        if active_run.driver == "local":
            cancel_local_pipeline_task(project_id)
        state.active_run = None
        state.last_error = (
            f"Background {active_run.stage.value} run timed out while waiting for progress. "
            "Please retry the stage."
        )
        _write_project(project_id, state)
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
    image_resolution: Optional[str],
    video_model: Optional[str],
    video_resolution: Optional[str],
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
            image_size=image_resolution,
            video_model=_resolve_video_provider(video_model, state),
            video_resolution=video_resolution,
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
        elif round(float(current.music_start_seconds or 0.0), 3) != round(float(state.music_start_seconds or 0.0), 3):
            _reconcile_after_music_start_edit(current, state)
    return _write_project(project_id, state)


@router.post("/projects/{project_id}/assets/{asset_id}/label", response_model=ProjectState)
def update_asset_label(project_id: str, asset_id: str, payload: AssetLabelUpdateRequest):
    if _get_project_run_state(project_id):
        raise HTTPException(status_code=409, detail="Asset label edits are unavailable while a background pipeline run is active")

    state = get_project(project_id)
    asset = _get_asset_or_404(state, asset_id)
    asset.label = payload.label.strip() if isinstance(payload.label, str) and payload.label.strip() else None
    return _write_project(project_id, state)


@router.post("/projects/{project_id}/clips/{clip_id}/image-approval", response_model=ProjectState)
def update_storyboard_clip_approval(project_id: str, clip_id: str, payload: ClipApprovalRequest):
    state = get_project(project_id)
    updated_state = _apply_image_approval_change(state, clip_id, payload.approved)
    return _write_project(project_id, updated_state)


@router.post("/projects/{project_id}/clips/{clip_id}/video-approval", response_model=ProjectState)
def update_filming_clip_approval(project_id: str, clip_id: str, payload: ClipApprovalRequest):
    state = get_project(project_id)
    updated_state = _apply_video_approval_change(state, clip_id, payload.approved)
    return _write_project(project_id, updated_state)


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
async def upload_asset(
    project_id: str,
    file: UploadFile = File(...),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
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
    asset_type = infer_asset_type(file.filename, file.content_type)
    text_content = None
    if asset_type == "document":
        try:
            text_content = extract_document_text(
                filename=file.filename,
                content=content,
                mime_type=file.content_type,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to read uploaded document: {str(exc)}") from exc
    url = storage.write_project_asset_bytes(
        relative_path,
        content,
        content_type=file.content_type,
    )
    return await _build_uploaded_asset_response(
        filename=file.filename or unique_name,
        mime_type=file.content_type,
        asset_type=asset_type,
        url=url,
        content=content,
        extracted_text=text_content,
        api_key=_coerce_optional_header(x_api_key),
    )


@router.post("/projects/{project_id}/upload-plan", response_model=AssetUploadPlanResponse)
async def create_asset_upload_plan(
    project_id: str,
    payload: AssetUploadPlanRequest,
    request: Request,
):
    storage = get_storage_backend()
    asset_type = infer_asset_type(payload.filename, payload.content_type)
    if asset_type == "document":
        return AssetUploadPlanResponse(mode="proxy")

    ext = os.path.splitext(payload.filename or "file")[1] or ""
    unique_name = f"{uuid.uuid4().hex}{ext}"
    relative_path = f"uploads/{project_id}/{unique_name}"
    upload_target = storage.create_browser_project_asset_upload(
        relative_path,
        content_type=payload.content_type,
        content_length=payload.size,
        origin=request.headers.get("origin"),
    )
    if not upload_target:
        return AssetUploadPlanResponse(mode="proxy")

    normalized_relative_path = relative_path.replace("\\", "/").lstrip("/")
    return AssetUploadPlanResponse(
        mode="direct",
        upload_url=upload_target.upload_url,
        upload_method=upload_target.method,
        upload_headers=upload_target.headers or {},
        url=f"/projects/{normalized_relative_path}",
        name=payload.filename or unique_name,
        label=suggest_asset_label(payload.filename or unique_name),
        asset_type=asset_type,
        mime_type=payload.content_type,
        text_content=None,
        ai_context=None,
    )


@router.post("/projects/{project_id}/upload-complete", response_model=AssetUploadPlanResponse)
async def complete_asset_upload(
    project_id: str,
    payload: CompleteAssetUploadRequest,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    storage = get_storage_backend()
    asset_type = infer_asset_type(payload.filename, payload.content_type)
    local_path = storage.resolve_project_asset_to_local_path(payload.url)
    if not local_path or not os.path.exists(local_path):
        raise HTTPException(status_code=404, detail="Uploaded asset could not be found for analysis")

    response_payload = await _build_uploaded_asset_response(
        filename=payload.filename,
        mime_type=payload.content_type,
        asset_type=asset_type,
        url=payload.url,
        local_path=local_path,
        api_key=_coerce_optional_header(x_api_key),
    )
    return AssetUploadPlanResponse(mode="direct", **response_payload)


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
    x_image_resolution: Optional[str] = Header(default=None, alias="X-Image-Resolution"),
    x_video_model: Optional[str] = Header(default=None, alias="X-Video-Model"),
    x_video_resolution: Optional[str] = Header(default=None, alias="X-Video-Resolution"),
    x_music_model: Optional[str] = Header(default=None, alias="X-Music-Model"),
    x_stage_voice_briefs_enabled: Optional[str] = Header(default=None, alias="X-Stage-Voice-Briefs-Enabled"),
):
    x_api_key = _coerce_optional_header(x_api_key)
    x_orchestrator_model = _coerce_optional_header(x_orchestrator_model)
    x_critic_model = _coerce_optional_header(x_critic_model)
    x_text_model = _coerce_optional_header(x_text_model)
    x_image_model = _coerce_optional_header(x_image_model)
    x_image_resolution = _coerce_optional_header(x_image_resolution)
    x_video_model = _coerce_optional_header(x_video_model)
    x_video_resolution = _coerce_optional_header(x_video_resolution)
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
        image_size=x_image_resolution,
        video_model=_resolve_video_provider(x_video_model, state),
        video_resolution=x_video_resolution,
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
    x_image_resolution: Optional[str] = Header(default=None, alias="X-Image-Resolution"),
    x_video_model: Optional[str] = Header(default=None, alias="X-Video-Model"),
    x_video_resolution: Optional[str] = Header(default=None, alias="X-Video-Resolution"),
    x_music_model: Optional[str] = Header(default=None, alias="X-Music-Model"),
    x_stage_voice_briefs_enabled: Optional[str] = Header(default=None, alias="X-Stage-Voice-Briefs-Enabled"),
):
    x_api_key = _coerce_optional_header(x_api_key)
    x_orchestrator_model = _coerce_optional_header(x_orchestrator_model)
    x_critic_model = _coerce_optional_header(x_critic_model)
    x_text_model = _coerce_optional_header(x_text_model)
    x_image_model = _coerce_optional_header(x_image_model)
    x_image_resolution = _coerce_optional_header(x_image_resolution)
    x_video_model = _coerce_optional_header(x_video_model)
    x_video_resolution = _coerce_optional_header(x_video_resolution)
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
        image_size=x_image_resolution,
        video_model=_resolve_video_provider(x_video_model, state),
        video_resolution=x_video_resolution,
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
        image_resolution=x_image_resolution,
        video_model=x_video_model,
        video_resolution=x_video_resolution,
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
                image_resolution=x_image_resolution,
                video_model=x_video_model,
                video_resolution=x_video_resolution,
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
        image_resolution=body.image_resolution,
        video_model=body.video_model,
        video_resolution=body.video_resolution,
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
    _apply_revert_to_state(state, target)
    _write_project(project_id, state)
    return state


class FillClipRequest(BaseModel):
    clip_id: str
    clip_index: int
    total_clips: int
    surrounding_context: str   # adjacent clips' storyboard_text joined with " | "
    duration: float


class LiveDirectorRequest(BaseModel):
    message: str
    display_stage: Optional[AgentStage] = None
    selected_clip_id: Optional[str] = None
    selected_fragment_id: Optional[str] = None
    selected_asset_id: Optional[str] = None
    source: str = "text"
    speech_mode: str = "standard"


class LiveDirectorResponse(BaseModel):
    project: ProjectState
    reply_text: str
    applied_changes: List[str] = Field(default_factory=list)
    target_clip_id: Optional[str] = None
    target_fragment_id: Optional[str] = None
    target_asset_id: Optional[str] = None
    stage: AgentStage


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
- Additional lore: {state.additional_lore[:400]}
- Uploaded asset registry: {_asset_reference_registry_for_state(state, max_chars=1600)}
- Uploaded asset understanding: {_asset_semantic_context_for_state(state, max_chars=2000)}
- Uploaded document context: {_document_context_for_state(state, max_chars=2000) or "(none)"}

Nearby shots for context:
{body.surrounding_context or "(no adjacent shots yet)"}

Write a single, vivid storyboard description for this shot. Be specific about:
- Subject / characters in frame
- Camera angle and movement
- Lighting and color palette
- Environment / background
- Overall mood
- If the project has labeled reference images for named characters, props, creatures, vehicles, or locations, preserve those names verbatim in the description when they are present in this shot.

Return ONLY the storyboard description text, no bullet points, no JSON, no extra commentary. 2-4 sentences max."""

    response = client.models.generate_content(
        model=orchestrator_model,
        contents=[prompt],
        config=_genai.types.GenerateContentConfig(
            thinking_config=_genai.types.ThinkingConfig(thinking_budget=1024),
        ),
    )
    return {"storyboard_text": response.text.strip()}


@router.post("/projects/{project_id}/storyboard-clips/{clip_id}/regenerate", response_model=ProjectState)
async def regenerate_storyboard_clip(
    project_id: str,
    clip_id: str,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_orchestrator_model: Optional[str] = Header(default=None, alias="X-Orchestrator-Model"),
    x_critic_model: Optional[str] = Header(default=None, alias="X-Critic-Model"),
    x_text_model: Optional[str] = Header(default=None, alias="X-Text-Model"),
    x_image_model: Optional[str] = Header(default=None, alias="X-Image-Model"),
    x_image_resolution: Optional[str] = Header(default=None, alias="X-Image-Resolution"),
):
    if _get_project_run_state(project_id):
        raise HTTPException(status_code=409, detail="Storyboard regeneration is unavailable while a background pipeline run is active")

    state = get_project(project_id)
    clip_index = next((index for index, clip in enumerate(state.timeline) if clip.id == clip_id), None)
    if clip_index is None:
        raise HTTPException(status_code=404, detail=f"Clip '{clip_id}' was not found")

    target_clip = state.timeline[clip_index]
    _clear_clip_storyboard_outputs(target_clip)
    _preserve_music_production_fragments(state)
    state.final_video_url = None
    state.last_error = None
    state.current_stage = AgentStage.STORYBOARDING
    _trim_stage_summaries_to(state, AgentStage.PLANNING)

    pipeline = FMVAgentPipeline(
        api_key=_coerce_optional_header(x_api_key),
        orchestrator_model=_resolve_orchestrator_model(
            _coerce_optional_header(x_orchestrator_model),
            _coerce_optional_header(x_text_model),
        ),
        critic_model=_resolve_critic_model(_coerce_optional_header(x_critic_model)),
        image_model=_coerce_optional_header(x_image_model),
        image_size=_coerce_optional_header(x_image_resolution),
        persist_state_callback=lambda updated_state: _write_project(project_id, updated_state),
    )

    await pipeline._persist_state(state)
    await pipeline._ensure_generated_character_assets(state)

    image_assets, asset_bytes, asset_lookup = pipeline._build_storyboard_asset_context(state)
    relevance_map = await pipeline._build_storyboard_relevance_map(
        [target_clip],
        image_assets=image_assets,
        screenplay=state.screenplay,
    )
    relevant_assets = _normalize_relevant_assets(relevance_map.get(target_clip.id, []))
    previous_reference_ready = [
        clip
        for clip in state.timeline[:clip_index]
        if clip.image_url is not None and clip.image_reference_ready
    ]
    relevant_prev_shots = await pipeline._select_relevant_previous_shots(
        target_clip,
        previous_reference_ready,
    )

    try:
        await pipeline._process_storyboard_clip(
            state=state,
            clip=target_clip,
            relevant_assets=relevant_assets,
            previous_shots=relevant_prev_shots,
            asset_bytes=asset_bytes,
            asset_lookup=asset_lookup,
        )
        await pipeline._persist_state(state)
    except Exception as exc:
        state.last_error = f"Storyboard regeneration failed for {clip_id}: {exc}"
        _write_project(project_id, state)
        raise HTTPException(status_code=500, detail=state.last_error) from exc

    return state


@router.post("/projects/{project_id}/storyboard-clips/{clip_id}/upload-frame", response_model=ProjectState)
def upload_storyboard_frame(
    project_id: str,
    clip_id: str,
    payload: StoryboardFrameUploadRequest,
):
    if _get_project_run_state(project_id):
        raise HTTPException(status_code=409, detail="Storyboard frame upload is unavailable while a background pipeline run is active")

    state = get_project(project_id)
    clip = _get_clip_or_404(state, clip_id)

    clip.image_url = payload.url
    clip.image_prompt = None
    clip.image_score = None
    clip.image_reference_ready = True
    clip.image_approved = True
    clip.image_manual_override = True
    clip.image_critiques = [
        *clip.image_critiques,
        f"Manual storyboard frame uploaded: {payload.name}",
    ]
    _clear_clip_video_outputs(clip)
    clip.video_approved = None

    _preserve_music_production_fragments(state)
    state.final_video_url = None
    state.last_error = None
    state.current_stage = AgentStage.STORYBOARDING
    _trim_stage_summaries_to(state, AgentStage.PLANNING)

    return _write_project(project_id, state)


@router.post("/projects/{project_id}/live-director", response_model=LiveDirectorResponse)
async def live_director_mode(
    project_id: str,
    body: LiveDirectorRequest,
    request: Request = None,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_orchestrator_model: Optional[str] = Header(default=None, alias="X-Orchestrator-Model"),
    x_critic_model: Optional[str] = Header(default=None, alias="X-Critic-Model"),
    x_text_model: Optional[str] = Header(default=None, alias="X-Text-Model"),
    x_image_model: Optional[str] = Header(default=None, alias="X-Image-Model"),
    x_image_resolution: Optional[str] = Header(default=None, alias="X-Image-Resolution"),
    x_video_model: Optional[str] = Header(default=None, alias="X-Video-Model"),
    x_video_resolution: Optional[str] = Header(default=None, alias="X-Video-Resolution"),
    x_music_model: Optional[str] = Header(default=None, alias="X-Music-Model"),
    x_stage_voice_briefs_enabled: Optional[str] = Header(default=None, alias="X-Stage-Voice-Briefs-Enabled"),
):
    if _get_project_run_state(project_id):
        raise HTTPException(status_code=409, detail="Live Director Mode is unavailable while a background pipeline run is active")

    state = get_project(project_id)
    x_api_key = _coerce_optional_header(x_api_key)
    x_orchestrator_model = _coerce_optional_header(x_orchestrator_model)
    x_critic_model = _coerce_optional_header(x_critic_model)
    x_text_model = _coerce_optional_header(x_text_model)
    x_image_model = _coerce_optional_header(x_image_model)
    x_image_resolution = _coerce_optional_header(x_image_resolution)
    x_video_model = _coerce_optional_header(x_video_model)
    x_video_resolution = _coerce_optional_header(x_video_resolution)
    x_music_model = _coerce_optional_header(x_music_model)
    stage_voice_briefs_enabled = _coerce_optional_bool_header(x_stage_voice_briefs_enabled)
    pipeline = FMVAgentPipeline(
        api_key=x_api_key,
        orchestrator_model=_resolve_orchestrator_model(
            x_orchestrator_model,
            x_text_model,
        ),
    )

    try:
        updated_state, result = await pipeline.handle_live_director_mode(
            state,
            message=body.message,
            display_stage=body.display_stage,
            selected_clip_id=body.selected_clip_id,
            selected_fragment_id=body.selected_fragment_id,
            selected_asset_id=body.selected_asset_id,
            source=body.source,
            speech_mode=body.speech_mode,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Live Director Mode failed: {str(exc)}") from exc

    navigation_action = str(result.get("navigation_action") or "").strip().lower()
    requested_target_stage = _coerce_agent_stage(result.get("target_stage"))
    display_stage = _coerce_agent_stage(body.display_stage) or updated_state.current_stage
    response_state = updated_state

    if navigation_action == "advance" and requested_target_stage and _stage_index(requested_target_stage) < _stage_index(display_stage):
        navigation_action = "rewind"

    if navigation_action == "rewind":
        rewind_target = requested_target_stage or _previous_review_stage_for_state(updated_state, display_stage)
        if rewind_target is not None:
            response_state = _apply_revert_to_state(updated_state, rewind_target)
        _write_project(project_id, response_state)
    elif navigation_action == "advance":
        if display_stage != updated_state.current_stage:
            updated_state = _apply_revert_to_state(updated_state, display_stage)
        _write_project(project_id, updated_state)
        if updated_state.current_stage == AgentStage.COMPLETED:
            response_state = updated_state
        elif (
            updated_state.current_stage in {AgentStage.PLANNING, AgentStage.STORYBOARDING}
            or (
                updated_state.current_stage == AgentStage.FILMING
                and not all(clip.video_approved and clip.video_url for clip in updated_state.timeline)
            )
        ):
            response_state = await run_pipeline_step_async(
                project_id,
                request=request,
                x_api_key=x_api_key,
                x_orchestrator_model=x_orchestrator_model,
                x_critic_model=x_critic_model,
                x_text_model=x_text_model,
                x_image_model=x_image_model,
                x_image_resolution=x_image_resolution,
                x_video_model=x_video_model,
                x_video_resolution=x_video_resolution,
                x_music_model=x_music_model,
                x_stage_voice_briefs_enabled=x_stage_voice_briefs_enabled,
            )
        else:
            response_state = await run_pipeline_step(
                project_id,
                x_api_key=x_api_key,
                x_orchestrator_model=x_orchestrator_model,
                x_critic_model=x_critic_model,
                x_text_model=x_text_model,
                x_image_model=x_image_model,
                x_image_resolution=x_image_resolution,
                x_video_model=x_video_model,
                x_video_resolution=x_video_resolution,
                x_music_model=x_music_model,
                x_stage_voice_briefs_enabled=x_stage_voice_briefs_enabled,
            )
    else:
        _write_project(project_id, updated_state)

    return LiveDirectorResponse(
        project=response_state,
        reply_text=result["reply_text"],
        applied_changes=result.get("applied_changes", []),
        target_clip_id=result.get("target_clip_id"),
        target_fragment_id=result.get("target_fragment_id"),
        stage=response_state.current_stage,
    )


