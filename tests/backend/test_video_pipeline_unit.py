import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import app.agent.graph as graph_module
import app.api.endpoints as endpoints_module
import app.job_queue as job_queue_module
import app.paths as paths_module
import app.storage as storage_module
from app.agent.graph import (
    FMVAgentPipeline,
    _local_media_path,
    _normalize_image_critique,
    _normalize_veo_duration_sequence,
)
from app.agent.models import AgentStage, ProductionTimelineFragment, ProjectState, StageSummary, VideoClip


class _FakeVideo:
    def __init__(self, uri: str = "gs://generated/video.mp4"):
        self.uri = uri
        self.video_bytes = None


class _FakeGeneratedVideo:
    def __init__(self):
        self.video = _FakeVideo()


class _FakePayload:
    def __init__(self):
        self.generated_videos = [_FakeGeneratedVideo()]


class _FakeEmptyPayload:
    def __init__(self, *, reasons=None, filtered_count=0):
        self.generated_videos = []
        self.rai_media_filtered_reasons = reasons or []
        self.rai_media_filtered_count = filtered_count


class _FakeRawVideoPayload:
    def __init__(self):
        self.videos = [_FakeVideo()]


class _FakeOperation:
    def __init__(self, response=None):
        self.done = True
        self.name = "operations/fake-veo"
        self.response = response or _FakePayload()


class _FakeModels:
    def __init__(self, response=None):
        self.calls = []
        self._response = response

    def generate_videos(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeOperation(response=self._response)


class _FakeFiles:
    def __init__(self, video_bytes: bytes):
        self.video_bytes = video_bytes
        self.download_calls = []

    def download(self, *, file, config=None):
        self.download_calls.append(file)
        file.video_bytes = self.video_bytes
        return self.video_bytes


class _FakeOperations:
    def get(self, operation, *, config=None):
        return operation


class _FakeClient:
    def __init__(self, video_bytes: bytes, *, response=None):
        self.models = _FakeModels(response=response)
        self.files = _FakeFiles(video_bytes)
        self.operations = _FakeOperations()


class _FakeSubprocess:
    def __init__(self, args, *, returncode=0, stdout=b"", stderr=b""):
        self.args = args
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


class _FakeAsyncMusicSession:
    def __init__(self, messages):
        self._messages = messages
        self.weighted_prompts = None
        self.music_config = None
        self.play_called = False
        self.stop_called = False

    async def set_weighted_prompts(self, prompts):
        self.weighted_prompts = prompts

    async def set_music_generation_config(self, config):
        self.music_config = config

    async def play(self):
        self.play_called = True

    async def stop(self):
        self.stop_called = True

    async def receive(self):
        for message in self._messages:
            yield message


class _FakeAsyncMusicConnect:
    def __init__(self, session):
        self._session = session
        self.model = None

    def __call__(self, *, model):
        self.model = model
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _fake_image_generation_result(image_bytes: bytes = b"fake-image-bytes"):
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            inline_data=SimpleNamespace(
                                mime_type="image/png",
                                data=image_bytes,
                            )
                        )
                    ]
                )
            )
        ]
    )


def _patch_storage_roots(monkeypatch, tmp_path):
    projects_dir = tmp_path / "projects"
    uploads_dir = projects_dir / "uploads"
    projects_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    job_queue_module.LOCAL_PIPELINE_TASKS.clear()
    storage_module.clear_storage_backend_cache()
    monkeypatch.setattr(graph_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(graph_module, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(paths_module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(paths_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths_module, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(paths_module, "UPLOADS_DIR", uploads_dir)
    monkeypatch.setattr(storage_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage_module, "PROJECTS_DIR", projects_dir)
    return projects_dir


def test_normalize_veo_duration_sequence_matches_allowed_lengths_and_target_total():
    assert _normalize_veo_duration_sequence([5, 5, 5, 5, 6], target_total=26) == [4, 6, 4, 6, 6]
    assert _normalize_veo_duration_sequence([5.2], target_total=None) == [6]


def test_pipeline_resolves_google_image_and_video_providers_from_model_selection():
    pipeline = FMVAgentPipeline(
        api_key=None,
        image_model="gemini-3-pro-image-preview",
        video_model="veo-3.1-generate-001",
    )

    assert pipeline.image_provider_id == "google-gemini-image"
    assert pipeline.image_model == "gemini-3-pro-image-preview"
    assert pipeline.video_provider_id == "google-veo"
    assert pipeline.video_model == "veo-3.1-generate-001"


def test_pipeline_defaults_to_distinct_orchestrator_and_critic_models():
    pipeline = FMVAgentPipeline(api_key=None)

    assert pipeline.orchestrator_model == "gemini-3-pro-preview"
    assert pipeline.critic_model == "gemini-3-flash-preview"
    assert pipeline.text_model == pipeline.orchestrator_model


def test_content_part_from_local_file_uses_inline_bytes_on_vertex(monkeypatch, tmp_path):
    sample_path = tmp_path / "sample.wav"
    sample_path.write_bytes(b"vertex-bytes")

    pipeline = FMVAgentPipeline(api_key=None)
    pipeline.uses_vertex_ai = True
    pipeline.client = SimpleNamespace(
        files=SimpleNamespace(
            upload=lambda **kwargs: (_ for _ in ()).throw(AssertionError("Developer upload should not be used on Vertex"))
        )
    )

    calls = []

    def fake_from_bytes(*, data, mime_type):
        calls.append((data, mime_type))
        return {"data": data, "mime_type": mime_type}

    monkeypatch.setattr(graph_module.genai.types.Part, "from_bytes", staticmethod(fake_from_bytes))

    part = pipeline._content_part_from_local_file(str(sample_path), mime_type="audio/wav")

    assert part == {"data": b"vertex-bytes", "mime_type": "audio/wav"}
    assert calls == [(b"vertex-bytes", "audio/wav")]


@pytest.mark.asyncio
async def test_generate_google_storyboard_image_requests_16_9_4k(monkeypatch):
    captured_calls = []

    def fake_generate_content(**kwargs):
        captured_calls.append(kwargs)
        return _fake_image_generation_result()

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=fake_generate_content)
    )

    async def fake_normalize_storyboard_image_bytes(*, image_bytes, image_mime_type):
        return image_bytes, image_mime_type

    pipeline._normalize_storyboard_image_bytes = fake_normalize_storyboard_image_bytes

    image_bytes, image_mime_type = await pipeline._generate_google_storyboard_image(
        contents=["A cinematic portrait"]
    )

    assert image_bytes == b"fake-image-bytes"
    assert image_mime_type == "image/png"
    assert len(captured_calls) == 1
    config = captured_calls[0]["config"]
    assert config.response_modalities == ["IMAGE", "TEXT"]
    assert config.image_config.aspect_ratio == "16:9"
    assert config.image_config.image_size == "4K"


@pytest.mark.asyncio
async def test_generate_google_storyboard_image_respects_selected_image_resolution(monkeypatch):
    captured_calls = []

    def fake_generate_content(**kwargs):
        captured_calls.append(kwargs)
        return _fake_image_generation_result()

    pipeline = FMVAgentPipeline(api_key="dummy", image_size="2K")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=fake_generate_content)
    )

    async def fake_normalize_storyboard_image_bytes(*, image_bytes, image_mime_type):
        return image_bytes, image_mime_type

    pipeline._normalize_storyboard_image_bytes = fake_normalize_storyboard_image_bytes

    await pipeline._generate_google_storyboard_image(contents=["A cinematic portrait"])

    config = captured_calls[0]["config"]
    assert pipeline.image_width == 2048
    assert pipeline.image_height == 1152
    assert config.image_config.image_size == "2K"


@pytest.mark.asyncio
async def test_normalize_storyboard_image_bytes_normalizes_non_16_9_frames(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline._probe_image_dimensions = lambda image_path: asyncio.sleep(0, result=(1024, 1024))
    tiny_png = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89"
        b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00"
        b"\xc9\xfe\x92\xef"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    async def fake_normalize_image_canvas(*, input_path, output_path):
        Path(output_path).write_bytes(b"normalized-image")

    pipeline._normalize_image_canvas = fake_normalize_image_canvas

    normalized_bytes, normalized_mime = await pipeline._normalize_storyboard_image_bytes(
        image_bytes=tiny_png,
        image_mime_type="image/png",
    )

    assert normalized_bytes == b"normalized-image"
    assert normalized_mime == "image/png"


def test_sanitize_video_motion_prompt_text_normalizes_shape_without_rewriting_content():
    pipeline = FMVAgentPipeline(api_key=None)

    sanitized = pipeline._sanitize_video_motion_prompt_text(
        '  "Macro visualization of internal clockwork gears jamming and overheating,\n'
        'smoke and sparks erupting from mechanical joints, red warning lights glowing."  '
    )

    assert sanitized == (
        "Macro visualization of internal clockwork gears jamming and overheating, "
        "smoke and sparks erupting from mechanical joints, red warning lights glowing."
    )


@pytest.mark.asyncio
async def test_build_video_motion_prompt_uses_baseline_text_when_no_rewriter_is_available():
    pipeline = FMVAgentPipeline(api_key=None)
    pipeline.client = SimpleNamespace(models=SimpleNamespace())

    clip = VideoClip(
        id="clip_filter",
        timeline_start=0,
        duration=8.0,
        storyboard_text=(
            "Intense macro visualization of internal clockwork gears jamming and overheating, "
            "smoke and sparks erupting from mechanical joints, red warning lights glowing, "
            "frantic energy, chaotic steampunk machinery, visual representation of logic fraying."
        ),
    )

    prompt = await pipeline._build_video_motion_prompt(
        clip,
        ProjectState(project_id="proj_filter", name="Filter"),
    )

    assert prompt == (
        "Intense macro visualization of internal clockwork gears jamming and overheating, "
        "smoke and sparks erupting from mechanical joints, red warning lights glowing, "
        "frantic energy, chaotic steampunk machinery, visual representation of logic fraying."
    )


@pytest.mark.asyncio
async def test_build_video_motion_prompt_uses_orchestrator_rewrite_when_available():
    captured = {}

    def fake_generate_content(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            text='"Slow macro push-in as warning lights pulse softly while thin smoke drifts through the mechanism."'
        )

    pipeline = FMVAgentPipeline(api_key=None)
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=fake_generate_content)
    )

    clip = VideoClip(
        id="clip_rewrite",
        timeline_start=0,
        duration=8.0,
        storyboard_text="Macro shot of stressed clockwork gears with warning lights and smoke.",
    )

    prompt = await pipeline._build_video_motion_prompt(
        clip,
        ProjectState(project_id="proj_rewrite", name="Rewrite"),
    )

    assert prompt == "Slow macro push-in as warning lights pulse softly while thin smoke drifts through the mechanism."
    assert captured["model"] == pipeline.orchestrator_model
    assert "Write only the motion" in captured["contents"][0]


@pytest.mark.asyncio
async def test_build_video_retry_prompt_uses_structural_fallback_when_rewriter_is_unavailable():
    pipeline = FMVAgentPipeline(api_key=None)
    pipeline.client = SimpleNamespace(models=SimpleNamespace())

    clip = VideoClip(
        id="clip_retry",
        timeline_start=0,
        duration=8.0,
        storyboard_text=(
            "Intense macro visualization of internal clockwork gears jamming and overheating, "
            "smoke and sparks erupting from mechanical joints, red warning lights glowing, "
            "frantic energy, chaotic steampunk machinery, visual representation of logic fraying."
        ),
    )

    prompt = await pipeline._build_video_retry_prompt(
        clip,
        ProjectState(project_id="proj_retry", name="Retry"),
        failed_prompt=clip.storyboard_text,
        failure_message="Google Veo returned no videos",
    )

    lowered = prompt.lower()
    assert lowered.startswith("slow continuous camera move through the scene as ")
    assert "clockwork gears" in lowered
    assert "warning lights" in lowered
    assert "steampunk machinery" in lowered
    assert prompt.endswith("Single continuous shot.")


def test_cloud_tasks_prefers_explicit_base_url_env_over_request_base_url(monkeypatch):
    captured = {}

    class _FakeTasksClient:
        def queue_path(self, project, location, queue):
            return f"{project}/{location}/{queue}"

        def create_task(self, request):
            captured["request"] = request

    fake_tasks_module = SimpleNamespace(
        CloudTasksClient=_FakeTasksClient,
        HttpMethod=SimpleNamespace(POST="POST"),
    )
    monkeypatch.setitem(sys.modules, "google.cloud", SimpleNamespace(tasks_v2=fake_tasks_module))
    monkeypatch.setitem(sys.modules, "google.cloud.tasks_v2", fake_tasks_module)
    monkeypatch.setenv("FMV_GCP_PROJECT", "cloud-project")
    monkeypatch.setenv("FMV_CLOUD_TASKS_LOCATION", "us-central1")
    monkeypatch.setenv("FMV_CLOUD_TASKS_QUEUE", "fmv-pipeline")
    monkeypatch.setenv("FMV_BASE_URL", "https://public-backend.example.com")
    monkeypatch.delenv("FMV_CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL", raising=False)
    monkeypatch.delenv("FMV_CLOUD_TASKS_AUDIENCE", raising=False)
    monkeypatch.delenv("FMV_INTERNAL_TASK_TOKEN", raising=False)

    job_queue_module._create_cloud_task(
        "proj_async",
        {"project_id": "proj_async", "run_id": "run_123"},
        "http://127.0.0.1:8000",
    )

    request = captured["request"]
    assert request["task"]["http_request"]["url"] == (
        "https://public-backend.example.com/api/internal/projects/proj_async/execute-run"
    )


def test_update_project_keeps_production_ready_when_only_deleted_scenes_are_removed(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    original = ProjectState(
        project_id="proj_rewind_on_plan_edit",
        name="Rewind On Plan Edit",
        current_stage=AgentStage.PRODUCTION,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Shot one",
                image_prompt="frame one",
                image_url="/projects/clip_0.png",
                image_approved=True,
                image_score=9,
                image_reference_ready=True,
                video_prompt="video one",
                video_url="/projects/clip_0.mp4",
                video_score=8,
                video_approved=True,
            ),
            VideoClip(
                id="clip_1",
                timeline_start=6.0,
                duration=6.0,
                storyboard_text="Shot two",
                image_prompt="frame two",
                image_url="/projects/clip_1.png",
                image_approved=True,
                image_score=8,
                image_reference_ready=True,
                video_prompt="video two",
                video_url="/projects/clip_1.mp4",
                video_score=8,
                video_approved=True,
            ),
        ],
        production_timeline=[
            ProductionTimelineFragment(
                id="clip_0_frag_0",
                source_clip_id="clip_0",
                timeline_start=0.0,
                source_start=0.0,
                duration=6.0,
                audio_enabled=True,
            )
        ],
        final_video_url="/projects/proj_rewind_on_plan_edit_final.mp4",
        stage_summaries={
            "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
            "storyboarding": StageSummary(text="Storyboarding", generated_at="2026-03-07T00:01:00+00:00"),
            "filming": StageSummary(text="Filming", generated_at="2026-03-07T00:02:00+00:00"),
            "production": StageSummary(text="Production", generated_at="2026-03-07T00:03:00+00:00"),
        },
    )
    (projects_dir / "proj_rewind_on_plan_edit.fmv").write_text(original.model_dump_json())

    updated = original.model_copy(deep=True)
    updated.timeline = [updated.timeline[0]]

    result = endpoints_module.update_project("proj_rewind_on_plan_edit", updated)

    assert result.current_stage == AgentStage.PRODUCTION
    assert result.timeline[0].image_url == "/projects/clip_0.png"
    assert result.timeline[0].video_url == "/projects/clip_0.mp4"
    assert result.production_timeline == []
    assert result.final_video_url is None
    assert result.stage_summaries == {}


def test_update_project_rewinds_to_storyboarding_when_remaining_shot_changes(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    original = ProjectState(
        project_id="proj_rewrite_on_plan_edit",
        name="Rewrite On Plan Edit",
        current_stage=AgentStage.PRODUCTION,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Shot one",
                image_prompt="frame one",
                image_url="/projects/clip_0.png",
                image_approved=True,
                image_score=9,
                image_reference_ready=True,
                video_prompt="video one",
                video_url="/projects/clip_0.mp4",
                video_score=8,
                video_approved=True,
            )
        ],
        production_timeline=[
            ProductionTimelineFragment(
                id="clip_0_frag_0",
                source_clip_id="clip_0",
                timeline_start=0.0,
                source_start=0.0,
                duration=6.0,
                audio_enabled=True,
            )
        ],
        final_video_url="/projects/proj_rewrite_on_plan_edit_final.mp4",
        stage_summaries={
            "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
            "storyboarding": StageSummary(text="Storyboarding", generated_at="2026-03-07T00:01:00+00:00"),
            "filming": StageSummary(text="Filming", generated_at="2026-03-07T00:02:00+00:00"),
            "production": StageSummary(text="Production", generated_at="2026-03-07T00:03:00+00:00"),
        },
    )
    (projects_dir / "proj_rewrite_on_plan_edit.fmv").write_text(original.model_dump_json())

    updated = original.model_copy(deep=True)
    updated.timeline[0].storyboard_text = "Rewritten shot one"

    result = endpoints_module.update_project("proj_rewrite_on_plan_edit", updated)

    assert result.current_stage == AgentStage.STORYBOARDING
    assert result.timeline[0].image_url is None
    assert result.timeline[0].video_url is None
    assert result.production_timeline == []
    assert result.final_video_url is None
    assert result.stage_summaries == {}


@pytest.mark.asyncio
async def test_live_director_planning_update_clears_only_changed_shot_outputs():
    action = {
        "reply_text": "I widened the opening shot and set it to eight seconds.",
        "change_summary": ["Updated Shot 1 and reconciled downstream outputs."],
        "global_updates": {
            "screenplay": None,
            "instructions": None,
            "additional_lore": None,
            "lyrics_prompt": None,
            "style_prompt": None,
            "music_min_duration_seconds": None,
            "music_max_duration_seconds": None,
        },
        "target_clip_id": "clip_0",
        "clip_updates": {
            "storyboard_text": "Wide opening tableau of the singer crossing the rooftop at dawn.",
            "duration": 8,
            "video_prompt": None,
        },
        "clear_target_image": False,
        "clear_target_video": False,
        "target_fragment_id": None,
        "fragment_updates": {
            "audio_enabled": None,
        },
        "rewind_to_stage": None,
    }

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **kwargs: SimpleNamespace(text=json.dumps(action)))
    )

    state = ProjectState(
        project_id="proj_live_director_plan",
        name="Live Director Planning",
        current_stage=AgentStage.PRODUCTION,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Original opening shot",
                image_prompt="opening frame",
                image_url="/projects/clip_0.png",
                image_approved=True,
                image_score=9,
                image_reference_ready=True,
                video_prompt="opening video prompt",
                video_url="/projects/clip_0.mp4",
                video_score=8,
                video_approved=True,
            ),
            VideoClip(
                id="clip_1",
                timeline_start=6.0,
                duration=6.0,
                storyboard_text="Second shot",
                image_prompt="second frame",
                image_url="/projects/clip_1.png",
                image_approved=True,
                image_score=8,
                image_reference_ready=True,
                video_prompt="second video prompt",
                video_url="/projects/clip_1.mp4",
                video_score=8,
                video_approved=True,
            ),
        ],
        production_timeline=[
            ProductionTimelineFragment(
                id="clip_0_frag_0",
                source_clip_id="clip_0",
                timeline_start=0.0,
                source_start=0.0,
                duration=6.0,
                audio_enabled=True,
            )
        ],
        final_video_url="/projects/proj_live_director_plan_final.mp4",
        stage_summaries={
            "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
            "storyboarding": StageSummary(text="Storyboarding", generated_at="2026-03-07T00:01:00+00:00"),
            "filming": StageSummary(text="Filming", generated_at="2026-03-07T00:02:00+00:00"),
            "production": StageSummary(text="Production", generated_at="2026-03-07T00:03:00+00:00"),
        },
    )

    updated_state, result = await pipeline.handle_live_director_mode(
        state,
        message="Make this shot wider and hold it for eight seconds.",
        display_stage=AgentStage.PLANNING,
        selected_clip_id="clip_0",
        source="voice",
    )

    assert result["target_clip_id"] == "clip_0"
    assert updated_state.current_stage == AgentStage.STORYBOARDING
    assert updated_state.timeline[0].duration == 8.0
    assert updated_state.timeline[0].storyboard_text == action["clip_updates"]["storyboard_text"]
    assert updated_state.timeline[0].image_url is None
    assert updated_state.timeline[0].video_url is None
    assert updated_state.timeline[1].image_url == "/projects/clip_1.png"
    assert updated_state.timeline[1].video_url == "/projects/clip_1.mp4"
    assert updated_state.timeline[1].timeline_start == 8.0
    assert updated_state.production_timeline == []
    assert updated_state.final_video_url is None
    assert updated_state.stage_summaries == {}
    assert len(updated_state.director_log) == 2
    assert updated_state.director_log[0].role == "user"
    assert updated_state.director_log[0].source == "voice"
    assert updated_state.director_log[1].role == "agent"
    assert updated_state.director_log[1].applied_changes == action["change_summary"]


@pytest.mark.asyncio
async def test_live_director_production_audio_edit_reopens_cut():
    action = {
        "reply_text": "I muted the source audio on the selected edit.",
        "change_summary": ["Muted the selected edit on A1."],
        "global_updates": {
            "screenplay": None,
            "instructions": None,
            "additional_lore": None,
            "lyrics_prompt": None,
            "style_prompt": None,
            "music_min_duration_seconds": None,
            "music_max_duration_seconds": None,
        },
        "target_clip_id": None,
        "clip_updates": {
            "storyboard_text": None,
            "duration": None,
            "video_prompt": None,
        },
        "clear_target_image": False,
        "clear_target_video": False,
        "target_fragment_id": "frag_0",
        "fragment_updates": {
            "audio_enabled": False,
        },
        "rewind_to_stage": None,
    }

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **kwargs: SimpleNamespace(text=json.dumps(action)))
    )

    state = ProjectState(
        project_id="proj_live_director_prod",
        name="Live Director Production",
        current_stage=AgentStage.COMPLETED,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Opening shot",
                image_url="/projects/clip_0.png",
                image_approved=True,
                image_score=9,
                image_reference_ready=True,
                video_prompt="opening video prompt",
                video_url="/projects/clip_0.mp4",
                video_score=8,
                video_approved=True,
            )
        ],
        production_timeline=[
            ProductionTimelineFragment(
                id="frag_0",
                source_clip_id="clip_0",
                timeline_start=0.0,
                source_start=0.0,
                duration=6.0,
                audio_enabled=True,
            )
        ],
        final_video_url="/projects/proj_live_director_prod_final.mp4",
        stage_summaries={
            "filming": StageSummary(text="Filming", generated_at="2026-03-07T00:02:00+00:00"),
            "production": StageSummary(text="Production", generated_at="2026-03-07T00:03:00+00:00"),
            "completed": StageSummary(text="Completed", generated_at="2026-03-07T00:04:00+00:00"),
        },
    )

    updated_state, result = await pipeline.handle_live_director_mode(
        state,
        message="Mute the audio on this edit.",
        display_stage=AgentStage.PRODUCTION,
        selected_fragment_id="frag_0",
    )

    assert result["target_fragment_id"] == "frag_0"
    assert updated_state.current_stage == AgentStage.PRODUCTION
    assert updated_state.production_timeline[0].audio_enabled is False
    assert updated_state.final_video_url is None
    assert set(updated_state.stage_summaries.keys()) == {"filming"}
    assert len(updated_state.director_log) == 2
    assert updated_state.director_log[1].applied_changes == action["change_summary"]


@pytest.mark.asyncio
async def test_live_director_explicit_shot_number_overrides_selected_clip():
    action = {
        "reply_text": "I tightened shot 2 and refreshed its downstream media.",
        "change_summary": ["Updated shot 2 and cleared its frame/video outputs."],
        "global_updates": {
            "screenplay": None,
            "instructions": None,
            "additional_lore": None,
            "lyrics_prompt": None,
            "style_prompt": None,
            "music_min_duration_seconds": None,
            "music_max_duration_seconds": None,
        },
        "target_clip_id": None,
        "clip_updates": {
            "storyboard_text": "A tighter profile shot of the lead singer under hard side light.",
            "duration": None,
            "video_prompt": None,
        },
        "clear_target_image": False,
        "clear_target_video": False,
        "target_fragment_id": None,
        "fragment_updates": {
            "audio_enabled": None,
        },
        "rewind_to_stage": None,
    }

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **kwargs: SimpleNamespace(text=json.dumps(action)))
    )

    state = ProjectState(
        project_id="proj_live_director_numbered_shot",
        name="Live Director Numbered Shot",
        current_stage=AgentStage.FILMING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Opening shot",
                image_url="/projects/clip_0.png",
                image_approved=True,
                image_score=9,
                image_reference_ready=True,
                video_prompt="opening video prompt",
                video_url="/projects/clip_0.mp4",
                video_score=8,
                video_approved=True,
            ),
            VideoClip(
                id="clip_1",
                timeline_start=6.0,
                duration=6.0,
                storyboard_text="Second shot",
                image_url="/projects/clip_1.png",
                image_approved=True,
                image_score=8,
                image_reference_ready=True,
                video_prompt="second video prompt",
                video_url="/projects/clip_1.mp4",
                video_score=8,
                video_approved=True,
            ),
        ],
        stage_summaries={
            "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
            "storyboarding": StageSummary(text="Storyboarding", generated_at="2026-03-07T00:01:00+00:00"),
            "filming": StageSummary(text="Filming", generated_at="2026-03-07T00:02:00+00:00"),
        },
    )

    updated_state, result = await pipeline.handle_live_director_mode(
        state,
        message="Make shot 2 tighter and more dramatic.",
        display_stage=AgentStage.STORYBOARDING,
        selected_clip_id="clip_0",
    )

    assert result["target_clip_id"] == "clip_1"
    assert updated_state.timeline[0].storyboard_text == "Opening shot"
    assert updated_state.timeline[1].storyboard_text == action["clip_updates"]["storyboard_text"]
    assert updated_state.current_stage == AgentStage.STORYBOARDING


@pytest.mark.asyncio
async def test_run_pipeline_from_input_revisits_music_stage_for_generated_music_workflow():
    pipeline = FMVAgentPipeline(api_key=None)
    state = ProjectState(
        project_id="proj_input_music",
        name="Input Music",
        current_stage=AgentStage.INPUT,
        music_workflow="lyria3",
        music_url="/projects/existing_music.wav?t=1",
        lyrics_prompt="Existing lyric",
        style_prompt="Existing style",
    )

    result = await pipeline.run_pipeline(state)

    assert result.current_stage == AgentStage.LYRIA_PROMPTING


@pytest.mark.asyncio
async def test_run_pipeline_from_input_skips_music_stage_for_uploaded_track_workflow():
    pipeline = FMVAgentPipeline(api_key=None)
    state = ProjectState(
        project_id="proj_input_uploaded",
        name="Input Uploaded",
        current_stage=AgentStage.INPUT,
        music_workflow="uploaded_track",
        music_url="/projects/uploaded_track.wav?t=1",
    )

    result = await pipeline.run_pipeline(state)

    assert result.current_stage == AgentStage.PLANNING


@pytest.mark.asyncio
async def test_run_pipeline_from_music_stage_requires_explicit_song_generation_for_automatic_provider():
    pipeline = FMVAgentPipeline(api_key=None)
    state = ProjectState(
        project_id="proj_music_generate_first",
        name="Generate First",
        current_stage=AgentStage.LYRIA_PROMPTING,
        style_prompt="Synthwave, moody",
    )

    result = await pipeline.run_pipeline(state)

    assert result.current_stage == AgentStage.LYRIA_PROMPTING
    assert "Generate a song" in (result.last_error or "")


@pytest.mark.asyncio
async def test_run_pipeline_from_music_stage_requires_regeneration_when_song_is_stale():
    pipeline = FMVAgentPipeline(api_key=None)
    state = ProjectState(
        project_id="proj_music_stale",
        name="Stale Song",
        current_stage=AgentStage.LYRIA_PROMPTING,
        music_url="/projects/proj_music_stale_music.wav?t=1",
        style_prompt="New cinematic pulse",
        generated_music_provider="google-lyria-realtime",
        generated_music_style_prompt="Older style",
        generated_music_min_duration_seconds=90,
        generated_music_max_duration_seconds=240,
    )

    result = await pipeline.run_pipeline(state)

    assert result.current_stage == AgentStage.LYRIA_PROMPTING
    assert "Regenerate the song" in (result.last_error or "")


def test_estimate_music_track_duration_respects_project_bounds():
    pipeline = FMVAgentPipeline(api_key=None)

    short_song = ProjectState(
        project_id="proj_bounds_short",
        name="Bounds Short",
        lyrics_prompt="short lyric",
        music_min_duration_seconds=120,
        music_max_duration_seconds=150,
    )
    assert pipeline._estimate_music_track_duration_seconds(short_song) == 120

    long_song = ProjectState(
        project_id="proj_bounds_long",
        name="Bounds Long",
        lyrics_prompt="\n".join(["This is a much longer lyric line with enough words to drive the estimate upward."] * 80),
        music_min_duration_seconds=90,
        music_max_duration_seconds=110,
    )
    assert pipeline._estimate_music_track_duration_seconds(long_song) == 110


def test_list_projects_returns_saved_projects_sorted_by_modified_time(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    older = ProjectState(project_id="proj_old", name="Older Project", current_stage=AgentStage.INPUT)
    newer = ProjectState(project_id="proj_new", name="Newer Project", current_stage=AgentStage.PRODUCTION)

    older_path = projects_dir / "proj_old.fmv"
    newer_path = projects_dir / "proj_new.fmv"
    older_path.write_text(older.model_dump_json())
    newer_path.write_text(newer.model_dump_json())

    older_stat = older_path.stat()
    newer_stat = newer_path.stat()
    older_path.touch()
    newer_path.touch()
    import os
    os.utime(older_path, (older_stat.st_atime, older_stat.st_mtime - 10))
    os.utime(newer_path, (newer_stat.st_atime, newer_stat.st_mtime + 10))

    projects = endpoints_module.list_projects()

    assert [project.project_id for project in projects] == ["proj_new", "proj_old"]
    assert projects[0].name == "Newer Project"
    assert projects[1].name == "Older Project"


def test_delete_project_removes_saved_state_and_media(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    state = ProjectState(
        project_id="proj_delete_me",
        name="Delete Me",
        current_stage=AgentStage.PRODUCTION,
        music_url="/projects/uploads/proj_delete_me/song.mp3",
        assets=[
            {
                "id": "asset_1",
                "url": "/projects/uploads/proj_delete_me/reference.png",
                "type": "image",
                "name": "reference.png",
            }
        ],
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Opening shot",
                image_url="/projects/proj_delete_me_clip_0.png",
                image_approved=True,
                video_url="/projects/proj_delete_me_clip_0.mp4",
                video_approved=True,
            )
        ],
        final_video_url="/projects/proj_delete_me_final.mp4",
        stage_summaries={
            "planning": StageSummary(
                text="Planning ready",
                audio_url="/projects/proj_delete_me_planning_brief.wav",
                generated_at="2026-03-07T00:00:00+00:00",
            )
        },
    )

    (projects_dir / "proj_delete_me.fmv").write_text(state.model_dump_json())
    (projects_dir / "proj_delete_me_clip_0.png").write_bytes(b"image")
    (projects_dir / "proj_delete_me_clip_0.mp4").write_bytes(b"video")
    (projects_dir / "proj_delete_me_final.mp4").write_bytes(b"final")
    (projects_dir / "proj_delete_me_planning_brief.wav").write_bytes(b"brief")
    upload_dir = projects_dir / "uploads" / "proj_delete_me"
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / "song.mp3").write_bytes(b"song")
    (upload_dir / "reference.png").write_bytes(b"reference")

    response = endpoints_module.delete_project("proj_delete_me")

    assert response.status_code == 204
    assert not (projects_dir / "proj_delete_me.fmv").exists()
    assert not (projects_dir / "proj_delete_me_clip_0.png").exists()
    assert not (projects_dir / "proj_delete_me_clip_0.mp4").exists()
    assert not (projects_dir / "proj_delete_me_final.mp4").exists()
    assert not (projects_dir / "proj_delete_me_planning_brief.wav").exists()
    assert not upload_dir.exists()
    assert endpoints_module.list_projects() == []


@pytest.mark.asyncio
async def test_node_filming_downloads_generated_video_from_sdk_file_handle(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)
    storyboard_path = projects_dir / "proj_test_clip_0.png"
    storyboard_path.write_bytes(b"fake-image")

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = _FakeClient(video_bytes=b"fake-video-bytes")

    async def fail_retry_prompt(*args, **kwargs):
        raise AssertionError("Retry prompt should not run when the first Veo call succeeds.")

    async def fake_critique(**kwargs):
        return {"score": 8, "reasoning": "ok", "suggestions": ""}

    pipeline._critique_video_frame = fake_critique
    pipeline._build_video_retry_prompt = fail_retry_prompt
    pipeline._probe_video_dimensions = lambda video_path: asyncio.sleep(0, result=(1920, 1080))

    state = ProjectState(
        project_id="proj_test",
        name="Unit Test",
        current_stage=AgentStage.STORYBOARDING,
        instructions="Cinematic and moody",
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=5.6,
                storyboard_text="A lighthouse in fog at dusk.",
                image_url="/projects/proj_test_clip_0.png?t=123456",
                image_approved=True,
            )
        ],
    )

    result = await pipeline.node_filming(state)

    assert result.current_stage == AgentStage.FILMING
    assert result.timeline[0].video_url is not None
    assert "w3schools" not in result.timeline[0].video_url

    saved_video_path = tmp_path / _local_media_path(result.timeline[0].video_url)
    assert saved_video_path.read_bytes() == b"fake-video-bytes"

    assert len(pipeline.client.files.download_calls) == 1
    assert len(pipeline.client.models.calls) == 1
    request = pipeline.client.models.calls[0]
    assert request["model"] == pipeline.video_model
    assert request["config"].duration_seconds == 6
    assert request["config"].aspect_ratio == "16:9"
    assert request["config"].resolution == "1080p"
    assert request["source"].image is not None
    assert request["source"].prompt.startswith("A lighthouse in fog at dusk.")
    assert "Do not generate music" in request["source"].prompt


@pytest.mark.asyncio
async def test_generate_google_video_clip_respects_selected_video_resolution(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    storyboard_path = tmp_path / "storyboard.png"
    storyboard_path.write_bytes(b"fake-image")

    pipeline = FMVAgentPipeline(api_key="dummy", video_resolution="720p")
    pipeline.client = _FakeClient(video_bytes=b"fake-video-bytes")

    video_bytes = await pipeline._generate_google_video_clip(
        prompt="Animate this scene.",
        duration_seconds=6,
        image_path=str(storyboard_path),
    )

    assert video_bytes == b"fake-video-bytes"
    request = pipeline.client.models.calls[0]
    assert pipeline.video_width == 1280
    assert pipeline.video_height == 720
    assert request["config"].resolution == "720p"


@pytest.mark.asyncio
async def test_node_filming_retries_with_adjusted_prompt_after_first_generation_failure(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)
    storyboard_path = projects_dir / "proj_retry_clip_0.png"
    storyboard_path.write_bytes(b"fake-image")

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(models=SimpleNamespace())
    pipeline._probe_video_dimensions = lambda video_path: asyncio.sleep(0, result=(1920, 1080))

    prompts = []

    async def fake_generate_video_clip(*, prompt, duration_seconds, image_path):
        prompts.append(prompt)
        if len(prompts) == 1:
            raise RuntimeError("Google Veo returned no videos")
        return b"retry-video-bytes"

    async def fake_retry_prompt(clip, state, *, failed_prompt, failure_message):
        assert failed_prompt.startswith("Intense macro visualization")
        assert "returned no videos" in failure_message
        return "Slow continuous camera move through the scene as intense macro visualization of internal clockwork gears jamming and overheating."

    async def fake_critique(**kwargs):
        return {"score": 8, "reasoning": "ok", "suggestions": ""}

    pipeline._generate_video_clip = fake_generate_video_clip
    pipeline._build_video_retry_prompt = fake_retry_prompt
    pipeline._critique_video_frame = fake_critique

    state = ProjectState(
        project_id="proj_retry_filming",
        name="Retry Filming",
        current_stage=AgentStage.STORYBOARDING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=8.0,
                storyboard_text=(
                    "Intense macro visualization of internal clockwork gears jamming and overheating, "
                    "smoke and sparks erupting from mechanical joints, red warning lights glowing."
                ),
                image_url="/projects/proj_retry_clip_0.png",
                image_approved=True,
            )
        ],
    )

    result = await pipeline.node_filming(state)

    assert result.timeline[0].video_url is not None
    assert len(prompts) == 2
    assert prompts[0].startswith("Intense macro visualization")
    assert prompts[1].startswith("Slow continuous camera move through the scene as")


@pytest.mark.asyncio
async def test_generate_google_video_clip_uses_gcs_download_on_vertex(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FMV_GCS_BUCKET", "test-bucket")
    storyboard_path = tmp_path / "storyboard.png"
    storyboard_path.write_bytes(b"fake-image")

    pipeline = FMVAgentPipeline(api_key=None)
    pipeline.uses_vertex_ai = True
    pipeline.media_client = _FakeClient(video_bytes=b"unused")

    def fail_download(*, file, config=None):
        raise AssertionError("Vertex should not use client.files.download")

    pipeline.media_client.files.download = fail_download
    pipeline._download_gcs_uri_bytes = lambda uri: b"vertex-video-bytes"

    video_bytes = await pipeline._generate_google_video_clip(
        prompt="Animate this scene.",
        duration_seconds=6,
        image_path=str(storyboard_path),
    )

    assert video_bytes == b"vertex-video-bytes"
    request = pipeline.media_client.models.calls[0]
    assert request["config"].output_gcs_uri.startswith("gs://")


@pytest.mark.asyncio
async def test_generate_google_video_clip_falls_back_to_gcs_prefix_when_vertex_response_has_no_videos(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FMV_GCS_BUCKET", "test-bucket")
    storyboard_path = tmp_path / "storyboard.png"
    storyboard_path.write_bytes(b"fake-image")

    pipeline = FMVAgentPipeline(api_key=None)
    pipeline.uses_vertex_ai = True
    pipeline.media_client = _FakeClient(
        video_bytes=b"unused",
        response=_FakeEmptyPayload(),
    )
    pipeline._download_first_gcs_prefix_bytes = lambda prefix_uri: b"fallback-video-bytes"

    video_bytes = await pipeline._generate_google_video_clip(
        prompt="Animate this scene.",
        duration_seconds=6,
        image_path=str(storyboard_path),
    )

    assert video_bytes == b"fallback-video-bytes"
    request = pipeline.media_client.models.calls[0]
    assert request["config"].output_gcs_uri.startswith("gs://")


def test_extract_generated_videos_accepts_vertex_style_videos_field():
    pipeline = FMVAgentPipeline(api_key=None)

    videos = pipeline._extract_generated_videos(_FakeRawVideoPayload())

    assert len(videos) == 1
    assert getattr(videos[0], "uri", None) == "gs://generated/video.mp4"


@pytest.mark.asyncio
async def test_node_filming_persists_intermediate_progress_for_live_ui_updates(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)
    storyboard_path = projects_dir / "proj_live_clip_0.png"
    storyboard_path.write_bytes(b"fake-image")

    persisted_states = []

    async def persist_state(state):
        persisted_states.append(state.model_copy(deep=True))

    pipeline = FMVAgentPipeline(
        api_key="dummy",
        persist_state_callback=persist_state,
    )
    pipeline.client = _FakeClient(video_bytes=b"fake-video-bytes")

    async def fake_critique(**kwargs):
        return {"score": 8, "reasoning": "ok", "suggestions": ""}

    pipeline._critique_video_frame = fake_critique
    pipeline._probe_video_dimensions = lambda video_path: asyncio.sleep(0, result=(1920, 1080))

    state = ProjectState(
        project_id="proj_live",
        name="Unit Test",
        current_stage=AgentStage.STORYBOARDING,
        instructions="Cinematic and moody",
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=5.6,
                storyboard_text="A lighthouse in fog at dusk.",
                image_url="/projects/proj_live_clip_0.png?t=123456",
                image_approved=True,
            )
        ],
    )

    result = await pipeline.node_filming(state)

    assert result.current_stage == AgentStage.FILMING
    assert len(persisted_states) >= 2
    assert persisted_states[0].current_stage == AgentStage.FILMING
    assert persisted_states[0].timeline[0].video_url is None
    assert persisted_states[-1].timeline[0].video_url == result.timeline[0].video_url


@pytest.mark.asyncio
async def test_node_storyboarding_persists_intermediate_progress_for_live_ui_updates(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_storage_roots(monkeypatch, tmp_path)

    persisted_states = []

    async def persist_state(state):
        persisted_states.append(state.model_copy(deep=True))

    async def fake_sleep(_seconds):
        return None

    pipeline = FMVAgentPipeline(
        api_key="dummy",
        persist_state_callback=persist_state,
    )
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kwargs: _fake_image_generation_result(b"storyboard-bytes")
        )
    )

    async def fake_select_previous_shots(current_clip, previous_clips, limit=6):
        return []

    async def fake_critique_image(**kwargs):
        return {
            "score": 9,
            "passes": True,
            "reasoning": "Looks good.",
            "suggestions": "",
            "hard_fail_reasons": [],
        }

    monkeypatch.setattr(graph_module.asyncio, "sleep", fake_sleep)
    pipeline._select_relevant_previous_shots = fake_select_previous_shots
    pipeline._critique_image = fake_critique_image

    state = ProjectState(
        project_id="proj_storyboard_live",
        name="Storyboard Live",
        current_stage=AgentStage.PLANNING,
        instructions="Moody and cinematic",
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=5.0,
                storyboard_text="A lone figure under a flickering streetlight.",
            )
        ],
    )

    result = await pipeline.node_storyboarding(state)

    assert result.current_stage == AgentStage.STORYBOARDING
    assert len(persisted_states) >= 2
    assert persisted_states[0].current_stage == AgentStage.STORYBOARDING
    assert persisted_states[0].timeline[0].image_url is None
    assert persisted_states[-1].timeline[0].image_url == result.timeline[0].image_url


@pytest.mark.asyncio
async def test_node_music_prompting_for_automatic_provider_only_drafts_prompts(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_storage_roots(monkeypatch, tmp_path)

    prompt_response = SimpleNamespace(
        text=json.dumps({
            "lyrics_prompt": "Neon hearts under midnight rain",
            "style_prompt": "Synthwave, cinematic, moody",
        })
    )
    calls = []

    pipeline = FMVAgentPipeline(api_key="dummy", music_model="lyria-realtime-exp")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kwargs: calls.append(kwargs) or prompt_response
        )
    )
    pipeline.music_client = SimpleNamespace(
        aio=SimpleNamespace(
            live=SimpleNamespace(
                music=SimpleNamespace(
                    connect=lambda **kwargs: pytest.fail("Drafting prompts should not call the automatic music API")
                )
            )
        )
    )

    state = ProjectState(
        project_id="proj_music_prompt",
        name="Music Prompt Test",
        current_stage=AgentStage.INPUT,
        screenplay="A neon city sings back to the night.",
        instructions="Dreamy and cinematic.",
        additional_lore="The lead is wandering alone.",
    )

    result = await pipeline.node_music_prompting(state)

    assert result.current_stage == AgentStage.LYRIA_PROMPTING
    assert result.lyrics_prompt == "Neon hearts under midnight rain"
    assert result.style_prompt == "Synthwave, cinematic, moody"
    assert result.music_url is None
    assert result.last_error is None
    assert calls[0]["model"] == pipeline.text_model


@pytest.mark.asyncio
async def test_node_music_prompting_manual_import_provider_only_drafts_prompts(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_storage_roots(monkeypatch, tmp_path)

    prompt_response = SimpleNamespace(
        text=json.dumps({
            "lyrics_prompt": "City lights call out your name",
            "style_prompt": "Alt-pop, intimate, soaring hook",
        })
    )

    pipeline = FMVAgentPipeline(api_key="dummy", music_model="external-import")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **kwargs: prompt_response)
    )
    pipeline.music_client = SimpleNamespace(
        aio=SimpleNamespace(
            live=SimpleNamespace(
                music=SimpleNamespace(
                    connect=lambda **kwargs: pytest.fail("Manual import mode should not call the automatic music API")
                )
            )
        )
    )

    state = ProjectState(
        project_id="proj_music_prompt_manual",
        name="Music Prompt Manual Test",
        current_stage=AgentStage.INPUT,
        screenplay="A singer walks through a city of mirrors.",
        instructions="Big chorus, emotional release.",
        additional_lore="The voice should feel confessional.",
    )

    result = await pipeline.node_music_prompting(state)

    assert result.current_stage == AgentStage.LYRIA_PROMPTING
    assert result.lyrics_prompt == "City lights call out your name"
    assert result.style_prompt == "Alt-pop, intimate, soaring hook"
    assert result.music_url is None
    assert result.last_error is None


@pytest.mark.asyncio
async def test_update_stage_summary_persists_filming_brief_text(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_storage_roots(monkeypatch, tmp_path)

    pipeline = FMVAgentPipeline(api_key=None)
    state = ProjectState(
        project_id="proj_summary",
        name="Summary Test",
        current_stage=AgentStage.FILMING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=4.0,
                storyboard_text="Shot one",
                video_url="/projects/clip_0.mp4",
                video_score=9,
                video_critiques=["Score: 9/10 — Stable and cinematic."],
            ),
            VideoClip(
                id="clip_1",
                timeline_start=4.0,
                duration=4.0,
                storyboard_text="Shot two",
                video_url="/projects/clip_1.mp4",
                video_score=5,
                video_critiques=["Score: 5/10 — Music hard fail: the clip contains a generated score."],
            ),
        ],
    )

    await pipeline._update_stage_summary(state, AgentStage.FILMING)

    summary = state.stage_summaries["filming"]
    assert "Filming is ready." in summary.text
    assert "shot 2" in summary.text.lower()
    assert summary.audio_url is None


@pytest.mark.asyncio
async def test_update_stage_summary_writes_tts_audio_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_storage_roots(monkeypatch, tmp_path)

    fake_tts_response = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            inline_data=SimpleNamespace(
                                data=b"\x00\x00" * 400,
                            )
                        )
                    ]
                )
            )
        ]
    )

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kwargs: fake_tts_response
        )
    )

    state = ProjectState(
        project_id="proj_tts_summary",
        name="TTS Summary Test",
        current_stage=AgentStage.PLANNING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=6.0,
                storyboard_text="Shot one",
            )
        ],
    )

    await pipeline._update_stage_summary(state, AgentStage.PLANNING)

    summary = state.stage_summaries["planning"]
    assert summary.audio_url is not None
    audio_path = Path(_local_media_path(summary.audio_url))
    assert audio_path.exists()
    assert audio_path.read_bytes()[:4] == b"RIFF"


@pytest.mark.asyncio
async def test_update_stage_summary_skips_tts_when_voice_briefs_are_disabled(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_storage_roots(monkeypatch, tmp_path)

    pipeline = FMVAgentPipeline(api_key="dummy", stage_voice_briefs_enabled=False)
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kwargs: pytest.fail("TTS should not run when voice briefs are disabled")
        )
    )

    state = ProjectState(
        project_id="proj_summary_disabled",
        name="Disabled Voice Summary Test",
        current_stage=AgentStage.PLANNING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=6.0,
                storyboard_text="Shot one",
            )
        ],
    )

    await pipeline._update_stage_summary(state, AgentStage.PLANNING)

    summary = state.stage_summaries["planning"]
    assert "Planning is ready." in summary.text
    assert summary.audio_url is None


@pytest.mark.asyncio
async def test_run_pipeline_does_not_refresh_existing_stage_summary_when_stage_does_not_advance():
    pipeline = FMVAgentPipeline(api_key=None)
    refreshed = {"called": False}

    async def fake_update_stage_summary(state, stage):
        refreshed["called"] = True

    async def fake_node_production(state):
        state.current_stage = AgentStage.PRODUCTION
        state.last_error = "No new final render was produced."
        return state

    pipeline._update_stage_summary = fake_update_stage_summary
    pipeline.node_production = fake_node_production

    state = ProjectState(
        project_id="proj_same_stage_summary",
        name="Same Stage Summary",
        current_stage=AgentStage.PRODUCTION,
        stage_summaries={
            "production": StageSummary(
                text="Production is ready.",
                generated_at="2026-03-07T00:00:00+00:00",
            )
        },
    )

    result = await pipeline.run_pipeline(state)

    assert result.current_stage == AgentStage.PRODUCTION
    assert refreshed["called"] is False


@pytest.mark.asyncio
async def test_regenerate_music_preview_endpoint_updates_project_track(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    class _FakePipeline:
        def __init__(self, *args, **kwargs):
            self.music_provider_id = "google-lyria-realtime"
            self.music_provider = SimpleNamespace(
                can_generate_automatically=lambda: True,
                blocking_message=lambda state: None,
                definition=SimpleNamespace(label="Google Lyria RealTime API"),
            )

        async def _generate_music_track(self, state, *, target_duration_seconds=None):
            music_path = projects_dir / "proj_music_endpoint_music.wav"
            music_path.write_bytes(b"new-preview")
            state.music_url = "/projects/proj_music_endpoint_music.wav?t=321"
            return state.music_url

    monkeypatch.setattr(endpoints_module, "FMVAgentPipeline", _FakePipeline)

    state = ProjectState(
        project_id="proj_music_endpoint",
        name="Endpoint Test",
        current_stage=AgentStage.LYRIA_PROMPTING,
        lyrics_prompt="A midnight chorus rises.",
        style_prompt="Pulse, synth, echo",
        music_url="/projects/old_music.wav?t=1",
    )
    (projects_dir / "proj_music_endpoint.fmv").write_text(state.model_dump_json())

    result = await endpoints_module.regenerate_music_preview(
        "proj_music_endpoint",
        x_music_model="lyria-realtime-exp",
    )
    persisted = endpoints_module.get_project("proj_music_endpoint")

    assert result.current_stage == AgentStage.LYRIA_PROMPTING
    assert result.music_url == "/projects/proj_music_endpoint_music.wav?t=321"
    assert persisted.music_url == result.music_url


@pytest.mark.asyncio
async def test_regenerate_music_preview_endpoint_rejects_manual_import_provider(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    state = ProjectState(
        project_id="proj_music_external",
        name="External Lyria Test",
        current_stage=AgentStage.LYRIA_PROMPTING,
        music_provider="external-import",
        lyrics_prompt="A midnight chorus rises.",
        style_prompt="Pulse, synth, echo",
    )
    (projects_dir / "proj_music_external.fmv").write_text(state.model_dump_json())

    with pytest.raises(endpoints_module.HTTPException) as exc_info:
        await endpoints_module.regenerate_music_preview("proj_music_external")

    assert exc_info.value.status_code == 400
    assert "Import a rendered song" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_critique_video_frame_hard_fails_when_music_is_detected(monkeypatch, tmp_path):
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"fake-video")

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        files=SimpleNamespace(
            upload=lambda file, config=None: str(file),
        ),
        models=SimpleNamespace(
            generate_content=lambda **kwargs: SimpleNamespace(
                text=json.dumps({
                    "score": 8,
                    "reasoning": "The visuals are stable and cinematic.",
                    "suggestions": "Looks good.",
                })
            )
        ),
    )

    async def fake_audio_analysis(video_path):
        return {
            "has_audio_stream": True,
            "audible_audio_detected": True,
            "mean_volume_db": -18.0,
            "max_volume_db": -3.0,
            "reasoning": "Audible audio is present.",
        }

    async def fake_music_classification(*, video_path, audio_analysis):
        return {
            "contains_music": True,
            "reasoning": "The clip contains a musical backing track with rhythmic instrumentation.",
        }

    async def fake_create_subprocess_exec(*args, **kwargs):
        if "fps=2" in args:
            Path(args[-1].replace("%04d", "0001")).write_bytes(b"frame")
        return _FakeSubprocess(args)

    pipeline._analyze_generated_video_audio = fake_audio_analysis
    pipeline._classify_generated_video_audio_content = fake_music_classification
    monkeypatch.setattr("app.agent.graph.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    critique = await pipeline._critique_video_frame(
        video_path=str(video_path),
        video_prompt="A lighthouse in fog at dusk.",
        duration=4.0,
    )

    assert critique["score"] == 3
    assert "music hard fail" in critique["reasoning"].lower()
    assert "without any music" in critique["suggestions"].lower()


@pytest.mark.asyncio
async def test_critique_video_frame_allows_diegetic_sfx_without_music_penalty(monkeypatch, tmp_path):
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"fake-video")

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        files=SimpleNamespace(
            upload=lambda file, config=None: str(file),
        ),
        models=SimpleNamespace(
            generate_content=lambda **kwargs: SimpleNamespace(
                text=json.dumps({
                    "score": 8,
                    "reasoning": "The visuals are stable and cinematic.",
                    "suggestions": "Looks good.",
                })
            )
        ),
    )

    async def fake_audio_analysis(video_path):
        return {
            "has_audio_stream": True,
            "audible_audio_detected": True,
            "mean_volume_db": -24.0,
            "max_volume_db": -8.0,
            "reasoning": "Audible non-musical sound is present.",
        }

    async def fake_music_classification(*, video_path, audio_analysis):
        return {
            "contains_music": False,
            "reasoning": "The audio is diegetic ambience and sound effects only.",
        }

    async def fake_create_subprocess_exec(*args, **kwargs):
        if "fps=2" in args:
            Path(args[-1].replace("%04d", "0001")).write_bytes(b"frame")
        return _FakeSubprocess(args)

    pipeline._analyze_generated_video_audio = fake_audio_analysis
    pipeline._classify_generated_video_audio_content = fake_music_classification
    monkeypatch.setattr("app.agent.graph.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    critique = await pipeline._critique_video_frame(
        video_path=str(video_path),
        video_prompt="A lighthouse in fog at dusk.",
        duration=4.0,
    )

    assert critique["score"] == 8
    assert "music hard fail" not in critique["reasoning"].lower()


@pytest.mark.asyncio
async def test_critique_video_frame_ignores_nonunanimous_visual_failure(monkeypatch, tmp_path):
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"fake-video")

    captured_calls = []
    responses = iter([
        SimpleNamespace(
            text=json.dumps(
                {
                    "score": 5,
                    "passes": False,
                    "reasoning": "A possible morphing artifact appears around 1.0s.",
                    "suggestions": "Stabilize the subject silhouette.",
                    "hard_fail_findings": [
                        {
                            "reason": "morphing artifact",
                            "category": "artifact",
                            "confidence": 0.92,
                            "evidence": "Around 1.0s the subject outline appears to morph for a single sampled frame.",
                        }
                    ],
                }
            )
        ),
        SimpleNamespace(
            text=json.dumps(
                {
                    "score": 8,
                    "passes": True,
                    "reasoning": "The clip is stable and readable.",
                    "suggestions": "Looks good.",
                    "hard_fail_findings": [],
                }
            )
        ),
        SimpleNamespace(
            text=json.dumps(
                {
                    "score": 9,
                    "passes": True,
                    "reasoning": "The sampled frames look clean and coherent.",
                    "suggestions": "Looks good.",
                    "hard_fail_findings": [],
                }
            )
        ),
    ])

    def fake_generate_content(**kwargs):
        captured_calls.append(kwargs)
        return next(responses)

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        files=SimpleNamespace(
            upload=lambda file, config=None: str(file),
        ),
        models=SimpleNamespace(generate_content=fake_generate_content),
    )

    async def fake_audio_analysis(video_path):
        return {
            "has_audio_stream": False,
            "audible_audio_detected": False,
            "mean_volume_db": None,
            "max_volume_db": None,
            "reasoning": "No audio stream detected.",
        }

    async def fake_music_classification(*, video_path, audio_analysis):
        return {
            "contains_music": False,
            "reasoning": "No music detected.",
        }

    async def fake_create_subprocess_exec(*args, **kwargs):
        if "fps=2" in args:
            Path(args[-1].replace("%04d", "0001")).write_bytes(b"frame")
        return _FakeSubprocess(args)

    pipeline._analyze_generated_video_audio = fake_audio_analysis
    pipeline._classify_generated_video_audio_content = fake_music_classification
    monkeypatch.setattr("app.agent.graph.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    critique = await pipeline._critique_video_frame(
        video_path=str(video_path),
        video_prompt="A lighthouse in fog at dusk.",
        duration=4.0,
    )

    assert len(captured_calls) == 3
    assert critique["passes"] is True
    assert critique["score"] == 8
    assert "did not reach unanimous agreement" in critique["reasoning"].lower()


@pytest.mark.asyncio
async def test_node_filming_formats_unavailable_video_review_without_fake_score(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)
    storyboard_path = projects_dir / "proj_review_clip_0.png"
    storyboard_path.write_bytes(b"fake-image")

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = _FakeClient(video_bytes=b"fake-video-bytes")
    pipeline._probe_video_dimensions = lambda video_path: asyncio.sleep(0, result=(1920, 1080))

    async def fake_critique(**kwargs):
        return {
            "score": None,
            "reasoning": "Automated video review unavailable: upstream timeout",
            "suggestions": "",
        }

    pipeline._critique_video_frame = fake_critique

    state = ProjectState(
        project_id="proj_review",
        name="Review Warning",
        current_stage=AgentStage.STORYBOARDING,
        instructions="Cinematic and moody",
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=5.6,
                storyboard_text="A lighthouse in fog at dusk.",
                image_url="/projects/proj_review_clip_0.png?t=123456",
                image_approved=True,
            )
        ],
    )

    result = await pipeline.node_filming(state)

    assert result.timeline[0].video_score is None
    assert result.timeline[0].video_critiques[-1] == "Automated video review unavailable: upstream timeout"


@pytest.mark.asyncio
async def test_node_filming_normalizes_non_landscape_clips_to_1080p(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)
    storyboard_path = projects_dir / "proj_canvas_clip_0.png"
    storyboard_path.write_bytes(b"fake-image")

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = _FakeClient(video_bytes=b"fake-video-bytes")
    pipeline._probe_video_dimensions = lambda video_path: asyncio.sleep(0, result=(1024, 1024))

    normalized_inputs = []

    async def fake_normalize_video_canvas(*, input_path, output_path, include_audio, source_start=0.0, duration=None):
        normalized_inputs.append((input_path, output_path, include_audio))
        Path(output_path).write_bytes(b"normalized-video-bytes")

    async def fake_critique(**kwargs):
        return {"score": 8, "reasoning": "ok", "suggestions": ""}

    pipeline._normalize_video_canvas = fake_normalize_video_canvas
    pipeline._critique_video_frame = fake_critique

    state = ProjectState(
        project_id="proj_canvas",
        name="Canvas Test",
        current_stage=AgentStage.STORYBOARDING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=6.0,
                storyboard_text="A lighthouse in fog at dusk.",
                image_url="/projects/proj_canvas_clip_0.png",
                image_approved=True,
            )
        ],
    )

    result = await pipeline.node_filming(state)

    assert result.timeline[0].video_url is not None
    assert normalized_inputs
    assert normalized_inputs[0][2] is True
    saved_video_path = tmp_path / _local_media_path(result.timeline[0].video_url)
    assert saved_video_path.read_bytes() == b"normalized-video-bytes"


@pytest.mark.asyncio
async def test_node_production_accepts_cache_busted_local_paths(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    clip_path = projects_dir / "proj_test_clip_0.mp4"
    clip_path.write_bytes(b"clip-bytes")
    music_path = projects_dir / "music.mp3"
    music_path.write_bytes(b"music-bytes")

    pipeline = FMVAgentPipeline(api_key=None)

    subprocess_calls = []

    class _FakeProcess:
        def __init__(self, args):
            self.args = args
            self.returncode = 0

        async def communicate(self):
            Path(self.args[-1]).write_bytes(b"final-video")
            return b"", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        subprocess_calls.append(args)
        return _FakeProcess(args)

    monkeypatch.setattr("app.agent.graph.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    state = ProjectState(
        project_id="proj_test",
        name="Unit Test",
        current_stage=AgentStage.FILMING,
        music_url="/projects/music.mp3?t=99",
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=5.0,
                storyboard_text="A lighthouse in fog at dusk.",
                video_url="/projects/proj_test_clip_0.mp4?t=42",
                video_approved=True,
            )
        ],
    )

    result = await pipeline.node_production(state)

    assert result.current_stage == AgentStage.COMPLETED
    assert result.final_video_url == "/projects/proj_test_final.mp4"
    assert len(subprocess_calls) == 5
    assert str(clip_path) in subprocess_calls[0]
    assert str(projects_dir / "proj_test_clip_0_frag_0_video.mp4") in subprocess_calls[0]
    assert str(projects_dir / "proj_test_clip_0_frag_0_audio.m4a") in subprocess_calls[1]
    assert str(projects_dir / "proj_test_clip_0_frag_0_segment.mp4") in subprocess_calls[2]
    assert str(projects_dir / "proj_test_sequence.mp4") in subprocess_calls[3]
    assert _local_media_path(state.music_url) in subprocess_calls[4]


@pytest.mark.asyncio
async def test_node_production_rebuilds_stale_timeline_after_removed_scenes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    clip_path = projects_dir / "proj_trim_clip_keep.mp4"
    clip_path.write_bytes(b"clip-bytes")

    pipeline = FMVAgentPipeline(api_key=None)
    subprocess_calls = []

    class _FakeProcess:
        def __init__(self, args):
            self.args = args
            self.returncode = 0

        async def communicate(self):
            Path(self.args[-1]).write_bytes(b"render-output")
            return b"", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        subprocess_calls.append(args)
        return _FakeProcess(args)

    monkeypatch.setattr("app.agent.graph.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    state = ProjectState(
        project_id="proj_trim",
        name="Trimmed Edit",
        current_stage=AgentStage.PRODUCTION,
        timeline=[
            VideoClip(
                id="clip_keep",
                timeline_start=0,
                duration=6.0,
                storyboard_text="Keep this shot",
                video_url="/projects/proj_trim_clip_keep.mp4",
                video_approved=True,
            )
        ],
        production_timeline=[
            ProductionTimelineFragment(
                id="clip_removed_frag_0",
                source_clip_id="clip_removed",
                timeline_start=0.0,
                source_start=0.0,
                duration=4.0,
                audio_enabled=True,
            )
        ],
    )

    result = await pipeline.node_production(state)

    assert result.current_stage == AgentStage.COMPLETED
    assert len(result.production_timeline) == 1
    assert result.production_timeline[0].source_clip_id == "clip_keep"
    assert str(clip_path) in subprocess_calls[0]


@pytest.mark.asyncio
async def test_run_pipeline_step_async_transitions_project_to_filming_immediately(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)
    job_queue_module.LOCAL_PIPELINE_TASKS.clear()
    monkeypatch.setenv("FMV_JOB_DRIVER", "local")

    started = asyncio.Event()
    finish = asyncio.Event()

    class _FakePipeline:
        def __init__(self, *args, **kwargs):
            self.persist_state_callback = kwargs.get("persist_state_callback")

        async def _normalize_timeline_for_veo(self, state):
            state.timeline[0].duration = 4.0

        async def run_pipeline(self, state):
            started.set()
            await finish.wait()
            state.timeline[0].video_url = "/projects/proj_async_clip_0.mp4"
            state.current_stage = AgentStage.FILMING
            if self.persist_state_callback:
                await self.persist_state_callback(state)
            return state

    monkeypatch.setattr(endpoints_module, "FMVAgentPipeline", _FakePipeline)

    state = ProjectState(
        project_id="proj_async",
        name="Async Filming",
        current_stage=AgentStage.STORYBOARDING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=5.0,
                storyboard_text="A lighthouse in fog at dusk.",
                image_url="/projects/proj_async_clip_0.png",
                image_approved=True,
            )
        ],
    )
    (projects_dir / "proj_async.fmv").write_text(state.model_dump_json())

    started_state = await endpoints_module.run_pipeline_step_async("proj_async")
    await asyncio.wait_for(started.wait(), timeout=1)

    assert started_state.current_stage == AgentStage.FILMING
    assert started_state.timeline[0].duration == 4.0

    status = endpoints_module.get_project_run_status("proj_async")
    persisted = endpoints_module.get_project("proj_async")

    assert status.is_running is True
    assert status.stage == AgentStage.FILMING
    assert status.status == endpoints_module.PipelineRunStatus.RUNNING
    assert status.driver == "local"
    assert persisted.current_stage == AgentStage.FILMING
    assert persisted.timeline[0].duration == 4.0
    assert persisted.active_run is not None
    assert persisted.active_run.driver == "local"

    finish.set()
    await asyncio.sleep(0.05)

    final_status = endpoints_module.get_project_run_status("proj_async")
    final_state = endpoints_module.get_project("proj_async")

    assert final_status.is_running is False
    assert final_state.timeline[0].video_url == "/projects/proj_async_clip_0.mp4"
    assert final_state.active_run is None


@pytest.mark.asyncio
async def test_run_pipeline_step_async_transitions_project_to_storyboarding_immediately(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)
    job_queue_module.LOCAL_PIPELINE_TASKS.clear()
    monkeypatch.setenv("FMV_JOB_DRIVER", "local")

    started = asyncio.Event()
    finish = asyncio.Event()

    class _FakePipeline:
        def __init__(self, *args, **kwargs):
            self.persist_state_callback = kwargs.get("persist_state_callback")

        async def run_pipeline(self, state):
            started.set()
            await finish.wait()
            state.timeline[0].image_url = "/projects/proj_async_storyboard_clip_0.png"
            state.timeline[0].image_approved = True
            state.current_stage = AgentStage.STORYBOARDING
            if self.persist_state_callback:
                await self.persist_state_callback(state)
            return state

    monkeypatch.setattr(endpoints_module, "FMVAgentPipeline", _FakePipeline)

    state = ProjectState(
        project_id="proj_async_storyboard",
        name="Async Storyboarding",
        current_stage=AgentStage.PLANNING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=5.0,
                storyboard_text="A lighthouse in fog at dusk.",
            )
        ],
    )
    (projects_dir / "proj_async_storyboard.fmv").write_text(state.model_dump_json())

    started_state = await endpoints_module.run_pipeline_step_async("proj_async_storyboard")
    await asyncio.wait_for(started.wait(), timeout=1)

    assert started_state.current_stage == AgentStage.STORYBOARDING

    status = endpoints_module.get_project_run_status("proj_async_storyboard")
    persisted = endpoints_module.get_project("proj_async_storyboard")

    assert status.is_running is True
    assert status.stage == AgentStage.STORYBOARDING
    assert status.status == endpoints_module.PipelineRunStatus.RUNNING
    assert status.driver == "local"
    assert persisted.current_stage == AgentStage.STORYBOARDING
    assert persisted.active_run is not None
    assert persisted.active_run.driver == "local"

    finish.set()
    await asyncio.sleep(0.05)

    final_status = endpoints_module.get_project_run_status("proj_async_storyboard")
    final_state = endpoints_module.get_project("proj_async_storyboard")

    assert final_status.is_running is False
    assert final_state.timeline[0].image_url == "/projects/proj_async_storyboard_clip_0.png"
    assert final_state.active_run is None


@pytest.mark.asyncio
async def test_run_pipeline_step_async_queues_cloud_task_and_persists_run_state(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)
    job_queue_module.LOCAL_PIPELINE_TASKS.clear()
    monkeypatch.setenv("FMV_JOB_DRIVER", "cloud_tasks")

    captured = {}

    class _FakePipeline:
        def __init__(self, *args, **kwargs):
            pass

        async def _normalize_timeline_for_veo(self, state):
            state.timeline[0].duration = 6.0

    async def fake_enqueue_pipeline_job(*, project_id, run_id, payload, base_url, execute_local):
        captured["project_id"] = project_id
        captured["run_id"] = run_id
        captured["payload"] = payload
        captured["base_url"] = base_url
        return "cloud_tasks"

    monkeypatch.setattr(endpoints_module, "FMVAgentPipeline", _FakePipeline)
    monkeypatch.setattr(endpoints_module, "enqueue_pipeline_job", fake_enqueue_pipeline_job)

    state = ProjectState(
        project_id="proj_cloud_queue",
        name="Cloud Queue",
        current_stage=AgentStage.STORYBOARDING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=5.0,
                storyboard_text="A lighthouse in fog at dusk.",
                image_url="/projects/proj_cloud_queue_clip_0.png",
                image_approved=True,
            )
        ],
    )
    (projects_dir / "proj_cloud_queue.fmv").write_text(state.model_dump_json())

    queued_state = await endpoints_module.run_pipeline_step_async("proj_cloud_queue")
    persisted = endpoints_module.get_project("proj_cloud_queue")
    status = endpoints_module.get_project_run_status("proj_cloud_queue")

    assert queued_state.current_stage == AgentStage.FILMING
    assert queued_state.timeline[0].duration == 6.0
    assert queued_state.active_run is not None
    assert queued_state.active_run.status == endpoints_module.PipelineRunStatus.QUEUED
    assert queued_state.active_run.driver == "cloud_tasks"
    assert persisted.active_run is not None
    assert persisted.active_run.run_id == queued_state.active_run.run_id
    assert status.is_running is True
    assert status.status == endpoints_module.PipelineRunStatus.QUEUED
    assert status.driver == "cloud_tasks"
    assert captured["project_id"] == "proj_cloud_queue"
    assert captured["run_id"] == queued_state.active_run.run_id
    assert captured["payload"]["project_id"] == "proj_cloud_queue"
    assert captured["payload"]["run_id"] == queued_state.active_run.run_id


@pytest.mark.asyncio
async def test_node_production_uses_silent_audio_for_muted_fragments(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    clip_path = projects_dir / "proj_test_clip_0.mp4"
    clip_path.write_bytes(b"clip-bytes")
    music_path = projects_dir / "music.mp3"
    music_path.write_bytes(b"music-bytes")

    pipeline = FMVAgentPipeline(api_key=None)

    subprocess_calls = []

    class _FakeProcess:
        def __init__(self, args):
            self.args = args
            self.returncode = 0

        async def communicate(self):
            Path(self.args[-1]).write_bytes(b"render-output")
            return b"", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        subprocess_calls.append(args)
        return _FakeProcess(args)

    monkeypatch.setattr("app.agent.graph.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    state = ProjectState(
        project_id="proj_test",
        name="Unit Test",
        current_stage=AgentStage.PRODUCTION,
        music_url="/projects/music.mp3",
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=5.0,
                storyboard_text="A lighthouse in fog at dusk.",
                video_url="/projects/proj_test_clip_0.mp4",
                video_approved=True,
            )
        ],
        production_timeline=[
            ProductionTimelineFragment(
                id="clip_0_frag_0",
                source_clip_id="clip_0",
                timeline_start=0.0,
                source_start=1.0,
                duration=2.0,
                audio_enabled=False,
            )
        ],
    )

    result = await pipeline.node_production(state)

    assert result.current_stage == AgentStage.COMPLETED
    assert len(subprocess_calls) == 5
    assert "anullsrc=r=48000:cl=stereo" in subprocess_calls[1]
    assert str(clip_path) not in subprocess_calls[1]
    assert str(projects_dir / "proj_test_clip_0_frag_0_audio.m4a") in subprocess_calls[1]


@pytest.mark.asyncio
async def test_node_production_keeps_previous_final_and_stays_in_production_on_render_failure(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    clip_path = projects_dir / "proj_test_clip_0.mp4"
    clip_path.write_bytes(b"clip-bytes")
    previous_final_path = projects_dir / "proj_test_previous_final.mp4"
    previous_final_path.write_bytes(b"previous-final")

    pipeline = FMVAgentPipeline(api_key=None)

    class _FailingProcess:
        returncode = 1

        async def communicate(self):
            return b"", b"forced ffmpeg failure"

    async def fake_create_subprocess_exec(*args, **kwargs):
        return _FailingProcess()

    monkeypatch.setattr("app.agent.graph.asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    state = ProjectState(
        project_id="proj_test",
        name="Unit Test",
        current_stage=AgentStage.PRODUCTION,
        final_video_url="/projects/proj_test_previous_final.mp4",
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=5.0,
                storyboard_text="A lighthouse in fog at dusk.",
                video_url="/projects/proj_test_clip_0.mp4?t=42",
                video_approved=True,
            )
        ],
    )

    result = await pipeline.node_production(state)

    assert result.current_stage == AgentStage.PRODUCTION
    assert result.final_video_url == "/projects/proj_test_previous_final.mp4"
    assert "ffmpeg failed while normalizing clip" in (result.last_error or "")


@pytest.mark.asyncio
async def test_node_storyboarding_ignores_list_shaped_relevance_map(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    asset_path = projects_dir / "ref.png"
    asset_path.write_bytes(b"asset-bytes")

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kwargs: _fake_image_generation_result()
        )
    )

    async def fake_build_asset_relevance_map(image_assets, clips):
        return []

    async def fake_select_previous_shots(current_clip, previous_clips, limit=6):
        return []

    async def fake_critique_image(**kwargs):
        return {"score": 8, "reasoning": "ok", "suggestions": ""}

    pipeline._build_asset_relevance_map = fake_build_asset_relevance_map
    pipeline._select_relevant_previous_shots = fake_select_previous_shots
    pipeline._critique_image = fake_critique_image

    state = ProjectState(
        project_id="proj_storyboard",
        name="Storyboard Test",
        current_stage=AgentStage.PLANNING,
        instructions="Moody and cinematic",
        assets=[
            {
                "id": "asset_1",
                "url": str(asset_path),
                "type": "image",
                "name": "ref.png",
            }
        ],
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=5.0,
                storyboard_text="A figure stands under a flickering streetlight in rain.",
            )
        ],
    )

    result = await pipeline.node_storyboarding(state)

    assert result.current_stage == AgentStage.STORYBOARDING
    assert result.timeline[0].image_url is not None
    assert result.timeline[0].image_approved is True
    assert result.timeline[0].image_reference_ready is True


@pytest.mark.asyncio
async def test_node_storyboarding_retries_until_high_score_and_only_persists_best_attempt(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_storage_roots(monkeypatch, tmp_path)

    generation_bytes = iter([b"attempt-one", b"attempt-two"])

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kwargs: _fake_image_generation_result(next(generation_bytes))
        )
    )

    async def fake_select_previous_shots(current_clip, previous_clips, limit=6):
        return []

    critique_results = iter([
        {
            "score": 4,
            "passes": False,
            "reasoning": "Extra arm visible.",
            "suggestions": "Remove the extra arm and keep only one subject.",
            "hard_fail_reasons": ["extra limbs"],
        },
        {
            "score": 9,
            "passes": True,
            "reasoning": "Clean anatomy and continuity.",
            "suggestions": "",
            "hard_fail_reasons": [],
        },
    ])

    async def fake_critique_image(**kwargs):
        return next(critique_results)

    pipeline._select_relevant_previous_shots = fake_select_previous_shots
    pipeline._critique_image = fake_critique_image

    state = ProjectState(
        project_id="proj_storyboard_retry",
        name="Storyboard Retry Test",
        current_stage=AgentStage.PLANNING,
        instructions="Moody and cinematic",
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=5.0,
                storyboard_text="A figure stands under a flickering streetlight in rain.",
            )
        ],
    )

    result = await pipeline.node_storyboarding(state)

    saved_path = Path(_local_media_path(result.timeline[0].image_url))
    assert saved_path.read_bytes() == b"attempt-two"
    assert result.timeline[0].image_approved is True
    assert result.timeline[0].image_reference_ready is True
    assert result.timeline[0].image_score == 9
    assert len(result.timeline[0].image_critiques) == 2


def test_normalize_image_critique_filters_speculative_body_dysmorphia_finding():
    normalized = _normalize_image_critique(
        {
            "score": 9,
            "passes": False,
            "reasoning": "The image is clean overall, but there may be slight body dysmorphia.",
            "suggestions": "No major changes needed.",
            "hard_fail_findings": [
                {
                    "reason": "body dysmorphia",
                    "category": "anatomy",
                    "confidence": 0.41,
                    "evidence": "The torso maybe seems a little unusual.",
                }
            ],
        }
    )

    assert normalized["hard_fail_reasons"] == []
    assert normalized["passes"] is True


def test_normalize_image_critique_keeps_clear_high_confidence_anatomy_failure():
    normalized = _normalize_image_critique(
        {
            "score": 3,
            "passes": False,
            "reasoning": "A third arm is visible on the subject.",
            "suggestions": "Remove the extra arm.",
            "hard_fail_findings": [
                {
                    "reason": "extra limbs",
                    "category": "anatomy",
                    "confidence": 0.98,
                    "evidence": "A third arm is clearly visible extending from the subject's left side.",
                }
            ],
        }
    )

    assert normalized["hard_fail_reasons"] == ["extra limbs"]
    assert normalized["passes"] is False


@pytest.mark.asyncio
async def test_critique_image_passes_when_panel_disagrees_on_extra_leg_claim(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_storage_roots(monkeypatch, tmp_path)

    captured_calls = []
    responses = iter([
        SimpleNamespace(
            text=json.dumps(
                {
                    "score": 5,
                    "passes": False,
                    "reasoning": "A third leg appears visible behind the subject.",
                    "suggestions": "Remove the extra leg.",
                    "hard_fail_findings": [
                        {
                            "reason": "extra limbs",
                            "category": "anatomy",
                            "confidence": 0.97,
                            "evidence": "A third leg appears behind the subject on the right side.",
                        }
                    ],
                }
            )
        ),
        SimpleNamespace(
            text=json.dumps(
                {
                    "score": 8,
                    "passes": True,
                    "reasoning": "The frame reads coherently and no blocking defect is clearly visible.",
                    "suggestions": "Looks usable.",
                    "hard_fail_findings": [],
                }
            )
        ),
        SimpleNamespace(
            text=json.dumps(
                {
                    "score": 9,
                    "passes": True,
                    "reasoning": "The shot is stable and visually coherent.",
                    "suggestions": "Looks usable.",
                    "hard_fail_findings": [],
                }
            )
        ),
    ])

    def fake_generate_content(**kwargs):
        captured_calls.append(kwargs)
        return next(responses)

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=fake_generate_content)
    )

    critique = await pipeline._critique_image(
        image_bytes=b"fake-image",
        image_mime_type="image/png",
        storyboard_text="A single dancer standing in an empty studio.",
        instructions="Natural proportions and realistic lighting.",
        image_prompt="A single dancer standing in an empty studio.",
        primary_reference_shot=None,
        continuity_reference_shots=[],
        relevant_assets=[],
        asset_bytes={},
    )
    normalized = _normalize_image_critique(critique)

    assert len(captured_calls) == 3
    assert all(call["model"] == pipeline.critic_model for call in captured_calls)
    assert normalized["hard_fail_reasons"] == []
    assert normalized["passes"] is True
    assert normalized["score"] == 8


@pytest.mark.asyncio
async def test_critique_image_fails_when_all_three_critics_raise_same_issue(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_storage_roots(monkeypatch, tmp_path)

    responses = iter([
        SimpleNamespace(
            text=json.dumps(
                {
                    "score": 4,
                    "passes": False,
                    "reasoning": "A third arm is clearly visible.",
                    "suggestions": "Remove the extra arm.",
                    "hard_fail_findings": [
                        {
                            "reason": "extra limbs",
                            "category": "anatomy",
                            "confidence": 0.96,
                            "evidence": "A third arm is clearly visible extending from the subject's left side.",
                        }
                    ],
                }
            )
        ),
        SimpleNamespace(
            text=json.dumps(
                {
                    "score": 5,
                    "passes": False,
                    "reasoning": "The subject appears to have an extra arm.",
                    "suggestions": "Correct the arm count.",
                    "hard_fail_findings": [
                        {
                            "reason": "third arm visible",
                            "category": "anatomy",
                            "confidence": 0.94,
                            "evidence": "A third arm is visible on the subject's left side in the middle of frame.",
                        }
                    ],
                }
            )
        ),
        SimpleNamespace(
            text=json.dumps(
                {
                    "score": 3,
                    "passes": False,
                    "reasoning": "An extra limb is visible.",
                    "suggestions": "Remove the extra limb.",
                    "hard_fail_findings": [
                        {
                            "reason": "extra arm",
                            "category": "anatomy",
                            "confidence": 0.99,
                            "evidence": "The subject has a visible extra arm protruding from the left torso.",
                        }
                    ],
                }
            )
        ),
    ])

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **kwargs: next(responses))
    )

    critique = await pipeline._critique_image(
        image_bytes=b"fake-image",
        image_mime_type="image/png",
        storyboard_text="A single dancer standing in an empty studio.",
        instructions="Natural proportions and realistic lighting.",
        image_prompt="A single dancer standing in an empty studio.",
        primary_reference_shot=None,
        continuity_reference_shots=[],
        relevant_assets=[],
        asset_bytes={},
    )
    normalized = _normalize_image_critique(critique)

    assert normalized["passes"] is False
    assert len(normalized["hard_fail_reasons"]) == 1
    assert normalized["hard_fail_reasons"][0] in {"extra limbs", "extra arm", "third arm visible"}


def test_critic_config_uses_low_temperature():
    pipeline = FMVAgentPipeline(api_key="dummy")
    config = pipeline._critic_config(response_mime_type="application/json")

    assert getattr(config, "temperature", None) == 0.1


@pytest.mark.asyncio
async def test_node_storyboarding_only_uses_high_scoring_previous_shots_as_references(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_storage_roots(monkeypatch, tmp_path)

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kwargs: _fake_image_generation_result()
        )
    )

    observed_previous_ids = []

    async def fake_select_previous_shots(current_clip, previous_clips, limit=6):
        observed_previous_ids.append([clip.id for clip in previous_clips])
        return []

    critique_results = iter([
        {
            "score": 5,
            "passes": False,
            "reasoning": "Broken anatomy.",
            "suggestions": "Fix the anatomy.",
            "hard_fail_reasons": ["extra limbs"],
        },
        {
            "score": 5,
            "passes": False,
            "reasoning": "Broken anatomy.",
            "suggestions": "Fix the anatomy.",
            "hard_fail_reasons": ["extra limbs"],
        },
        {
            "score": 5,
            "passes": False,
            "reasoning": "Broken anatomy.",
            "suggestions": "Fix the anatomy.",
            "hard_fail_reasons": ["extra limbs"],
        },
        {
            "score": 5,
            "passes": False,
            "reasoning": "Broken anatomy.",
            "suggestions": "Fix the anatomy.",
            "hard_fail_reasons": ["extra limbs"],
        },
        {
            "score": 5,
            "passes": False,
            "reasoning": "Broken anatomy.",
            "suggestions": "Fix the anatomy.",
            "hard_fail_reasons": ["extra limbs"],
        },
        {
            "score": 9,
            "passes": True,
            "reasoning": "Clean anatomy and continuity.",
            "suggestions": "",
            "hard_fail_reasons": [],
        },
    ])

    async def fake_critique_image(**kwargs):
        return next(critique_results)

    pipeline._select_relevant_previous_shots = fake_select_previous_shots
    pipeline._critique_image = fake_critique_image

    state = ProjectState(
        project_id="proj_storyboard_reference_gate",
        name="Storyboard Reference Gate Test",
        current_stage=AgentStage.PLANNING,
        instructions="Moody and cinematic",
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=5.0,
                storyboard_text="A figure stands under a flickering streetlight in rain.",
            ),
            VideoClip(
                id="clip_1",
                timeline_start=5.0,
                duration=5.0,
                storyboard_text="The same figure turns toward the alley.",
            ),
        ],
    )

    result = await pipeline.node_storyboarding(state)

    assert observed_previous_ids == [[], []]
    assert result.timeline[0].image_reference_ready is False
    assert result.timeline[0].image_approved is False
    assert result.timeline[1].image_reference_ready is True


@pytest.mark.asyncio
async def test_run_pipeline_resumes_from_halted_storyboarding_state():
    pipeline = FMVAgentPipeline(api_key=None)

    called = {"storyboarding": False}

    async def fake_node_storyboarding(state):
        called["storyboarding"] = True
        state.current_stage = AgentStage.STORYBOARDING
        return state

    pipeline.node_storyboarding = fake_node_storyboarding

    state = ProjectState(
        project_id="proj_halted",
        name="Halted Test",
        current_stage=AgentStage.HALTED_FOR_REVIEW,
        last_error="'list' object has no attribute 'get'",
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=5.0,
                storyboard_text="Shot",
                image_url="/projects/clip_0.png",
                image_approved=False,
            )
        ],
    )

    result = await pipeline.run_pipeline(state)

    assert called["storyboarding"] is True
    assert result.current_stage == AgentStage.STORYBOARDING
    assert result.last_error is None


@pytest.mark.asyncio
async def test_run_pipeline_enters_production_edit_stage_before_render():
    pipeline = FMVAgentPipeline(api_key=None)

    called = {"production": False}

    async def fake_node_production(state):
        called["production"] = True
        return state

    pipeline.node_production = fake_node_production

    state = ProjectState(
        project_id="proj_cut",
        name="Production Prep Test",
        current_stage=AgentStage.FILMING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=6.0,
                storyboard_text="Shot",
                video_approved=True,
                video_url="/projects/clip_0.mp4",
            )
        ],
    )

    result = await pipeline.run_pipeline(state)

    assert called["production"] is False
    assert result.current_stage == AgentStage.PRODUCTION
    assert len(result.production_timeline) == 1
    assert result.production_timeline[0].source_clip_id == "clip_0"


@pytest.mark.asyncio
async def test_run_pipeline_does_not_enter_production_without_real_video_outputs():
    pipeline = FMVAgentPipeline(api_key=None)

    called = {"production": False, "filming": False}

    async def fake_node_production(state):
        called["production"] = True
        return state

    async def fake_node_filming(state):
        called["filming"] = True
        state.current_stage = AgentStage.FILMING
        return state

    pipeline.node_production = fake_node_production
    pipeline.node_filming = fake_node_filming

    state = ProjectState(
        project_id="proj_gate",
        name="Gate Test",
        current_stage=AgentStage.FILMING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=6.0,
                storyboard_text="Shot",
                video_approved=True,
                video_url=None,
            )
        ],
    )

    result = await pipeline.run_pipeline(state)

    assert called["production"] is False
    assert called["filming"] is True
    assert result.current_stage == AgentStage.FILMING
