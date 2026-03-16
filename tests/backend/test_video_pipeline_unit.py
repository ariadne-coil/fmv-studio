import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import app.agent.graph as graph_module
import app.api.endpoints as endpoints_module
import app.job_queue as job_queue_module
import app.media as media_module
import app.paths as paths_module
import app.storage as storage_module
from app.agent.graph import (
    FMVAgentPipeline,
    _is_resource_exhausted_error,
    _is_timeout_error,
    _local_media_path,
    _normalize_image_critique,
    _normalize_veo_duration_sequence,
)
from app.agent.models import AgentStage, DirectorUndoEntry, MediaAsset, ProductionTimelineFragment, ProjectState, StageSummary, VideoClip
from app.agent.models import PipelineRunState, PipelineRunStatus
from app.video.providers import VideoGenerationReferenceAsset


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
    def __init__(self, response=None, error=None):
        self.done = True
        self.name = "operations/fake-veo"
        self.response = response or _FakePayload()
        self.error = error


class _FakeModels:
    def __init__(self, response=None):
        self.calls = []
        self._response = response

    def generate_videos(self, **kwargs):
        self.calls.append(kwargs)
        response = self._response
        error = None
        if isinstance(response, list):
            response = response.pop(0) if response else None
        elif callable(response):
            response = response()
        if isinstance(response, tuple) and len(response) == 2:
            response, error = response
        return _FakeOperation(response=response, error=error)


def test_previous_review_stage_for_uploaded_track_skips_music_prompt_stage():
    state = ProjectState(
        project_id="proj_uploaded_track_prev_stage",
        name="Uploaded Track Prev Stage",
        current_stage=AgentStage.PLANNING,
        music_workflow="uploaded_track",
        music_url="/projects/song.wav",
    )

    previous_stage = endpoints_module._previous_review_stage_for_state(state, AgentStage.PLANNING)

    assert previous_stage == AgentStage.INPUT


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        (AgentStage.LYRIA_PROMPTING, AgentStage.INPUT),
        (AgentStage.PLANNING, AgentStage.LYRIA_PROMPTING),
        (AgentStage.STORYBOARDING, AgentStage.PLANNING),
        (AgentStage.FILMING, AgentStage.STORYBOARDING),
        (AgentStage.PRODUCTION, AgentStage.FILMING),
        (AgentStage.COMPLETED, AgentStage.PRODUCTION),
    ],
)
def test_previous_review_stage_for_standard_flow(stage, expected):
    state = ProjectState(
        project_id="proj_standard_prev_stage",
        name="Standard Prev Stage",
        current_stage=stage,
        music_workflow="lyria3",
    )

    previous_stage = endpoints_module._previous_review_stage_for_state(state, stage)

    assert previous_stage == expected


def test_previous_review_stage_for_halted_filming_state():
    state = ProjectState(
        project_id="proj_halted_filming_prev_stage",
        name="Halted Filming Prev Stage",
        current_stage=AgentStage.HALTED_FOR_REVIEW,
        music_workflow="lyria3",
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=6.0,
                storyboard_text="Shot 1",
                image_url="/projects/clip_0.png",
                image_approved=True,
                video_url="/projects/clip_0.mp4",
                video_approved=False,
            )
        ],
    )

    previous_stage = endpoints_module._previous_review_stage_for_state(state, AgentStage.HALTED_FOR_REVIEW)

    assert previous_stage == AgentStage.STORYBOARDING


def test_previous_review_stage_for_halted_completed_state():
    state = ProjectState(
        project_id="proj_halted_completed_prev_stage",
        name="Halted Completed Prev Stage",
        current_stage=AgentStage.HALTED_FOR_REVIEW,
        music_workflow="uploaded_track",
        music_url="/projects/song.wav",
        final_video_url="/projects/final.mp4",
    )

    previous_stage = endpoints_module._previous_review_stage_for_state(state, AgentStage.HALTED_FOR_REVIEW)

    assert previous_stage == AgentStage.PRODUCTION


def test_get_project_run_state_clears_stale_cloud_run(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    state = ProjectState(
        project_id="proj_stale_storyboard_run",
        name="Stale Storyboard Run",
        current_stage=AgentStage.STORYBOARDING,
        active_run=PipelineRunState(
            run_id="run-stale",
            stage=AgentStage.STORYBOARDING,
            status=PipelineRunStatus.RUNNING,
            driver="cloud_tasks",
            started_at="2026-03-12T00:00:00+00:00",
            updated_at="2026-03-12T00:00:00+00:00",
        ),
    )
    (projects_dir / "proj_stale_storyboard_run.fmv").write_text(state.model_dump_json())

    run_state = endpoints_module._get_project_run_state("proj_stale_storyboard_run")
    persisted = endpoints_module.get_project("proj_stale_storyboard_run")

    assert run_state is None
    assert persisted.active_run is None
    assert persisted.last_error == (
        "Background storyboarding run timed out while waiting for progress. Please retry the stage."
    )


def test_get_project_clears_stale_filming_run_from_main_project_payload(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    state = ProjectState(
        project_id="proj_stale_filming_run",
        name="Stale Filming Run",
        current_stage=AgentStage.FILMING,
        active_run=PipelineRunState(
            run_id="run-stale-film",
            stage=AgentStage.FILMING,
            status=PipelineRunStatus.RUNNING,
            driver="cloud_tasks",
            started_at="2026-03-12T00:00:00+00:00",
            updated_at="2026-03-12T00:00:00+00:00",
        ),
    )
    (projects_dir / "proj_stale_filming_run.fmv").write_text(state.model_dump_json())

    persisted = endpoints_module.get_project("proj_stale_filming_run")

    assert persisted.active_run is None
    assert persisted.last_error == (
        "Background filming run timed out while waiting for progress. Please retry the stage."
    )


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


async def _read_streaming_response_body(response) -> bytes:
    body = bytearray()
    async for chunk in response.body_iterator:
        body.extend(chunk)
    return bytes(body)


def test_normalize_veo_duration_sequence_matches_allowed_lengths_and_target_total():
    assert _normalize_veo_duration_sequence([5, 5, 5, 5, 6], target_total=26) == [4, 6, 4, 6, 6]
    assert _normalize_veo_duration_sequence([5.2], target_total=None) == [6]


def test_normalize_veo_duration_sequence_supports_ingredients_mode_eight_second_only():
    assert _normalize_veo_duration_sequence(
        [4, 6, 8],
        target_total=18,
        allowed_durations=(8,),
    ) == [8, 8, 8]


def test_update_project_enabling_ingredients_mode_preserves_existing_media_and_stage(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    current = ProjectState(
        project_id="proj_ingredients_mode_toggle",
        name="Ingredients Mode Toggle Preserve Media",
        current_stage=AgentStage.FILMING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=4.0,
                storyboard_text="Shot one",
                image_url="/projects/clip_0.png",
                image_approved=True,
                video_url="/projects/clip_0.mp4",
                video_approved=True,
            ),
            VideoClip(
                id="clip_1",
                timeline_start=4.0,
                duration=6.0,
                storyboard_text="Shot two",
                image_url="/projects/clip_1.png",
                image_approved=True,
                video_url="/projects/clip_1.mp4",
                video_approved=True,
            ),
        ],
        production_timeline=[
            ProductionTimelineFragment(
                id="video_frag_0",
                source_clip_id="clip_0",
                timeline_start=0.0,
                duration=4.0,
                audio_enabled=True,
            ),
            ProductionTimelineFragment(
                id="video_frag_1",
                source_clip_id="clip_1",
                timeline_start=4.0,
                duration=6.0,
                audio_enabled=True,
            ),
        ],
        final_video_url="/projects/proj_ingredients_mode_toggle_final.mp4",
    )
    (projects_dir / "proj_ingredients_mode_toggle.fmv").write_text(current.model_dump_json())

    updated = current.model_copy(deep=True)
    updated.ingredients_mode_enabled = True

    result = endpoints_module.update_project("proj_ingredients_mode_toggle", updated)

    assert result.ingredients_mode_enabled is True
    assert [clip.duration for clip in result.timeline] == [4.0, 6.0]
    assert [clip.timeline_start for clip in result.timeline] == [0.0, 4.0]
    assert [clip.video_url for clip in result.timeline] == [
        "/projects/clip_0.mp4",
        "/projects/clip_1.mp4",
    ]
    assert [clip.image_url for clip in result.timeline] == [
        "/projects/clip_0.png",
        "/projects/clip_1.png",
    ]
    assert len(result.production_timeline) == 2
    assert result.final_video_url == "/projects/proj_ingredients_mode_toggle_final.mp4"
    assert result.current_stage == AgentStage.FILMING


def test_build_project_asset_response_streams_full_file_without_ranges(tmp_path):
    media_path = tmp_path / "sample.mp4"
    media_path.write_bytes(b"abcdefghij")

    response = media_module.build_project_asset_response(str(media_path), method="GET")

    assert response.status_code == 200
    assert response.headers["content-length"] == "10"
    assert response.headers["accept-ranges"] == "bytes"
    assert asyncio.run(_read_streaming_response_body(response)) == b"abcdefghij"


def test_build_project_asset_response_honors_byte_ranges(tmp_path):
    media_path = tmp_path / "sample.mp4"
    media_path.write_bytes(b"abcdefghij")

    response = media_module.build_project_asset_response(
        str(media_path),
        method="GET",
        range_header="bytes=2-5",
    )

    assert response.status_code == 206
    assert response.headers["content-length"] == "4"
    assert response.headers["content-range"] == "bytes 2-5/10"


def test_is_resource_exhausted_error_matches_common_google_message_shapes():
    assert _is_resource_exhausted_error(RuntimeError("429 RESOURCE_EXHAUSTED. Try later."))
    assert _is_resource_exhausted_error(RuntimeError("The resource has been exhausted for this request"))
    assert not _is_resource_exhausted_error(RuntimeError("Invalid argument"))


def test_asset_reference_registry_includes_ai_context():
    state = ProjectState(
        project_id="proj",
        name="Test Project",
        assets=[
            MediaAsset(
                id="asset_1",
                url="/projects/uploads/proj/mira.png",
                type="image",
                name="mira.png",
                label="Mira",
                ai_context="A silver-haired lead character with mirrored eyeliner and a chrome jacket.",
            ),
            MediaAsset(
                id="asset_2",
                url="/projects/uploads/proj/lore.pdf",
                type="document",
                name="lore.pdf",
                label="Lore Bible",
                text_content="The city floats above a toxic sea.",
                ai_context="Explains that the floating city runs on stolen tidal energy and bans daylight travel.",
            ),
        ],
    )

    registry = endpoints_module._asset_reference_registry_for_state(state, max_chars=2000)
    semantic_context = endpoints_module._asset_semantic_context_for_state(state, max_chars=2000)

    assert "AI-understood context" in registry
    assert "silver-haired lead character" in registry
    assert "Mira (image):" in semantic_context
    assert "floating city runs on stolen tidal energy" in semantic_context


def test_build_uploaded_asset_response_includes_ai_context(monkeypatch):
    async def _fake_analyze_uploaded_asset(**kwargs):
        return "Uptempo synth-pop song with anxious vocals and a dramatic chorus lift."

    monkeypatch.setattr(endpoints_module, "analyze_uploaded_asset", _fake_analyze_uploaded_asset)
    monkeypatch.setattr(endpoints_module, "build_genai_client", lambda api_key=None: object())

    response_payload = asyncio.run(
        endpoints_module._build_uploaded_asset_response(
            filename="song.wav",
            mime_type="audio/wav",
            asset_type="audio",
            url="/projects/uploads/proj/song.wav",
            content=b"fake",
            extracted_text=None,
            api_key="test-key",
        )
    )

    assert response_payload["name"] == "song.wav"
    assert response_payload["asset_type"] == "audio"
    assert response_payload["ai_context"] == "Uptempo synth-pop song with anxious vocals and a dramatic chorus lift."


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


def test_project_context_block_includes_labeled_asset_registry_and_document_context():
    pipeline = FMVAgentPipeline(api_key=None)
    state = ProjectState(
        project_id="proj_asset_context",
        name="Asset Context",
        additional_lore="Rain-soaked retro future",
        assets=[
            {
                "id": "asset_img",
                "url": "/projects/mira.png",
                "type": "image",
                "name": "mira_ref.png",
                "label": "Mira",
            },
            {
                "id": "asset_doc",
                "url": "/projects/world.pdf",
                "type": "document",
                "name": "world.pdf",
                "label": "World Bible",
                "text_content": "The city floats above a toxic sea.",
            },
        ],
    )

    context = pipeline._project_context_block(state, max_document_chars=500)

    assert 'image "Mira"' in context
    assert 'document "World Bible"' in context
    assert "World Bible:\nThe city floats above a toxic sea." in context


@pytest.mark.asyncio
async def test_asset_relevance_map_prompt_uses_asset_labels(monkeypatch, tmp_path):
    image_path = tmp_path / "mira.png"
    image_path.write_bytes(b"fake-image")

    captured = {}

    def fake_generate_content(*, model, contents, config):
        captured["contents"] = contents
        return SimpleNamespace(text="{}")

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=fake_generate_content)
    )

    clip = VideoClip(
        id="clip_1",
        timeline_start=0,
        duration=6.0,
        storyboard_text="Mira walks through the neon alley under rain.",
    )
    asset = SimpleNamespace(
        id="asset_img",
        url=str(image_path),
        name="mira_ref.png",
        label="Mira",
    )

    result = await pipeline._build_asset_relevance_map(
        [asset],
        [clip],
        screenplay="Mira is the lead singer wandering through the alley.",
    )

    assert result == {}
    prompt_text = "\n".join(part for part in captured["contents"] if isinstance(part, str))
    assert "Label: Mira" in prompt_text
    assert "canonical visual reference" in prompt_text
    assert "Screenplay context:" in prompt_text


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
async def test_generate_google_storyboard_image_falls_back_to_generated_images_when_candidate_parts_missing():
    def fake_generate_content(**kwargs):
        return SimpleNamespace(
            generated_images=[
                SimpleNamespace(
                    image=SimpleNamespace(
                        image_bytes=b"generated-image-bytes",
                        mime_type="image/png",
                    ),
                    rai_filtered_reason=None,
                )
            ],
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(parts=None),
                    finish_reason=None,
                )
            ],
        )

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

    assert image_bytes == b"generated-image-bytes"
    assert image_mime_type == "image/png"


@pytest.mark.asyncio
async def test_generate_google_storyboard_image_raises_clean_error_when_no_image_parts_exist():
    def fake_generate_content(**kwargs):
        return SimpleNamespace(
            text="No image was returned",
            generated_images=None,
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(parts=None),
                    finish_reason="SAFETY",
                )
            ],
        )

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=fake_generate_content)
    )

    with pytest.raises(RuntimeError, match="returned no image data"):
        await pipeline._generate_google_storyboard_image(contents=["A cinematic portrait"])


@pytest.mark.asyncio
async def test_process_storyboard_clip_prompt_discourages_reference_repetition_and_subtitle_overlays(monkeypatch, tmp_path):
    _patch_storage_roots(monkeypatch, tmp_path)

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace()

    captured: dict[str, object] = {}

    async def fake_generate_storyboard_frame(*, contents):
        captured["contents"] = contents
        return b"frame-bytes", "image/png"

    async def fake_critique_image(**kwargs):
        return {
            "score": 9,
            "passes": True,
            "reasoning": "Looks good.",
            "suggestions": "",
            "hard_fail_reasons": [],
        }

    monkeypatch.setattr(pipeline, "_generate_storyboard_frame", fake_generate_storyboard_frame)
    monkeypatch.setattr(pipeline, "_critique_image", fake_critique_image)
    monkeypatch.setattr(
        pipeline,
        "_sync_local_project_artifact",
        lambda path, *, relative_path, content_type=None: f"/projects/{relative_path}",
    )

    previous_frame_path = tmp_path / "projects" / "prev.png"
    previous_frame_path.write_bytes(b"previous-frame")

    state = ProjectState(
        project_id="proj_storyboard_prompt",
        name="Storyboard Prompt",
        current_stage=AgentStage.STORYBOARDING,
        instructions="Moody cinematic lighting.",
    )
    clip = VideoClip(
        id="clip_0",
        timeline_start=0.0,
        duration=6.0,
        storyboard_text="Mira turns toward the raven and starts speaking in the alley.",
    )
    previous_shot = VideoClip(
        id="clip_prev",
        timeline_start=0.0,
        duration=6.0,
        storyboard_text="Mira faces camera in the same alley.",
        image_url="/projects/prev.png",
    )
    relevant_assets = [
        {"id": "asset_mira", "type": "subject"},
        {"id": "asset_raven", "type": "subject"},
        {"id": "asset_alley", "type": "location"},
    ]
    asset_bytes = {
        "asset_mira": (b"mira", "image/png"),
        "asset_raven": (b"raven", "image/png"),
        "asset_alley": (b"alley", "image/png"),
    }
    asset_lookup = {
        "asset_mira": MediaAsset(id="asset_mira", url="/projects/mira.png", type="image", name="mira.png", label="Mira"),
        "asset_raven": MediaAsset(id="asset_raven", url="/projects/raven.png", type="image", name="raven.png", label="White Raven"),
        "asset_alley": MediaAsset(id="asset_alley", url="/projects/alley.png", type="image", name="alley.png", label="Neon Alley"),
    }

    await pipeline._process_storyboard_clip(
        state=state,
        clip=clip,
        relevant_assets=relevant_assets,
        previous_shots=[previous_shot],
        asset_bytes=asset_bytes,
        asset_lookup=asset_lookup,
    )

    prompt = str((captured["contents"] or [])[-1])
    assert "Generate a new storyboard frame for this shot's specific action" in prompt
    assert "Treat any prior storyboard frames strictly as reference images for continuity" in prompt
    assert "do not use them as fixed backgrounds or background plates" in prompt
    assert "Explicitly name every visible character, creature, or named subject present in the shot" in prompt
    assert "Explicitly name the location or setting" in prompt
    assert "Describe the exact camera angle, framing, and what the camera can see from that angle" in prompt
    assert "Describe the pose, body orientation, gaze, and action of each visible character in detail" in prompt
    assert "do not add burned-in subtitles, captions, or lyric overlays" in prompt
    assert "realistic scale, spacing, depth, and perspective" in prompt
    assert any(
        isinstance(item, str) and "not as a background plate, matte layer, or frame to copy shot-for-shot" in item
        for item in captured["contents"]
    )
    assert any(
        isinstance(item, str) and "MULTI-SUBJECT REFERENCE RULE" in item
        for item in captured["contents"]
    )


def test_build_image_critic_prompt_flags_unwanted_subtitles_and_bad_reference_scale():
    pipeline = FMVAgentPipeline(api_key="dummy")

    prompt = pipeline._build_image_critic_prompt(
        storyboard_text="Mira speaks to the raven.",
        instructions="Moody cinematic lighting.",
        image_prompt="Some prompt",
        reviewer_lens="Literal prompt faithfulness.",
    )

    assert "burned-in subtitles, captions, or lyric overlays" in prompt
    assert "believable scale, spacing, and perspective" in prompt
    assert "do not treat normal in-world text such as signage" in prompt.lower()


@pytest.mark.asyncio
async def test_build_storyboard_image_prompt_uses_orchestrator_to_require_characters_location_camera_and_pose():
    captured = {}

    def fake_generate_content(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            text=(
                '"Low-angle medium-wide shot in Neon Alley as Mira stands three-quarters to camera with '
                'her shoulders turned toward the white raven on her raised left forearm, mouth open mid-line, '
                'right hand hovering near her chest, rain-slick pavement and magenta signage in the foreground, '
                'narrow storefronts and steam in the midground, and receding neon fire escapes in the background."'
            )
        )

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=fake_generate_content)
    )

    state = ProjectState(
        project_id="proj_prompt_expand",
        name="Prompt Expand",
        instructions="Moody neon realism with cinematic contrast.",
    )
    clip = VideoClip(
        id="clip_expand",
        timeline_start=0.0,
        duration=6.0,
        storyboard_text="Mira turns toward the raven and speaks in the alley.",
    )
    previous_shot = VideoClip(
        id="clip_prev",
        timeline_start=0.0,
        duration=6.0,
        storyboard_text="Mira walks through Neon Alley before stopping under the sign.",
    )
    asset_lookup = {
        "asset_mira": MediaAsset(id="asset_mira", url="/projects/mira.png", type="image", name="mira.png", label="Mira"),
        "asset_raven": MediaAsset(id="asset_raven", url="/projects/raven.png", type="image", name="raven.png", label="White Raven"),
        "asset_alley": MediaAsset(id="asset_alley", url="/projects/alley.png", type="image", name="alley.png", label="Neon Alley"),
    }

    prompt = await pipeline._build_storyboard_image_prompt(
        state=state,
        clip=clip,
        relevant_assets=[
            {"id": "asset_mira", "type": "subject"},
            {"id": "asset_raven", "type": "subject"},
            {"id": "asset_alley", "type": "location"},
        ],
        previous_shots=[previous_shot],
        asset_lookup=asset_lookup,
    )

    assert prompt.startswith("Low-angle medium-wide shot in Neon Alley")
    assert captured["model"] == pipeline.orchestrator_model
    request_prompt = captured["contents"][0]
    assert "Explicitly name every visible character, creature, or named subject present in the shot" in request_prompt
    assert "Explicitly name the location/setting" in request_prompt
    assert "Describe the exact camera angle, shot size/framing, camera height, and point of view" in request_prompt
    assert "Describe the pose, body orientation, gaze, expression, and action of each visible character in detail" in request_prompt
    assert "Treat continuity reference images as scene references only" in request_prompt
    assert "not fixed backgrounds, matte paintings, or background plates" in request_prompt
    assert "Mira, White Raven" in request_prompt
    assert "Neon Alley" in request_prompt


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

    async def fake_normalize_image_canvas(*, input_path, output_path, target_width, target_height, pad_color):
        Path(output_path).write_bytes(b"normalized-image")

    pipeline._normalize_image_canvas = fake_normalize_image_canvas

    normalized_bytes, normalized_mime = await pipeline._normalize_storyboard_image_bytes(
        image_bytes=tiny_png,
        image_mime_type="image/png",
    )

    assert normalized_bytes == b"normalized-image"
    assert normalized_mime == "image/png"


@pytest.mark.asyncio
async def test_generate_character_reference_asset_uses_portrait_resolution_and_white_background(monkeypatch, tmp_path):
    _patch_storage_roots(monkeypatch, tmp_path)

    pipeline = FMVAgentPipeline(api_key="dummy", image_size="2K")
    pipeline.client = SimpleNamespace(models=SimpleNamespace())

    captured = {}

    async def fake_generate_google_image(
        *,
        contents,
        aspect_ratio,
        image_size,
        target_width,
        target_height,
        pad_color,
    ):
        captured["contents"] = contents
        captured["aspect_ratio"] = aspect_ratio
        captured["image_size"] = image_size
        captured["target_width"] = target_width
        captured["target_height"] = target_height
        captured["pad_color"] = pad_color
        return b"portrait-image", "image/png"

    pipeline._generate_google_image = fake_generate_google_image
    pipeline._sync_local_project_artifact = lambda path, *, relative_path, content_type=None: f"/projects/{relative_path}"

    state = ProjectState(project_id="proj_character_ref", name="Character Ref")

    asset = await pipeline._generate_character_reference_asset(
        state=state,
        label="Mira",
        generation_prompt="An androgynous synth-pop singer with silver hair and mirrored eyeliner.",
        why="Mira appears across the chorus and bridge.",
    )

    assert asset.label == "Mira"
    assert asset.source == "agent"
    assert asset.purpose == "character_reference"
    assert asset.url.endswith("proj_character_ref_character_mira.png")
    assert "canonical portrait reference for Mira" in (asset.ai_context or "")
    assert captured["aspect_ratio"] == "9:16"
    assert captured["image_size"] == "2K"
    assert captured["target_width"] == 1152
    assert captured["target_height"] == 2048
    assert captured["pad_color"] == "white"
    final_prompt = captured["contents"][0]
    assert "Neutral seamless white studio background" in final_prompt
    assert "Single centered character portrait reference" in final_prompt


@pytest.mark.asyncio
async def test_ensure_generated_character_assets_skips_user_labeled_assets_and_removes_duplicate_generated_ones(monkeypatch, tmp_path):
    _patch_storage_roots(monkeypatch, tmp_path)

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(models=SimpleNamespace())

    planned = [
        {
            "label": "Mira",
            "generation_prompt": "Portrait of Mira.",
            "why": "Lead performer.",
        },
        {
            "label": "Bird",
            "generation_prompt": "Portrait of the white raven mascot.",
            "why": "Recurring creature.",
        },
    ]

    async def fake_plan_generated_character_assets(state):
        return planned

    generated_labels = []

    async def fake_generate_character_reference_asset(*, state, label, generation_prompt, why):
        generated_labels.append(label)
        return MediaAsset(
            id=f"asset_auto_{label.lower()}",
            url=f"/projects/{label.lower()}.png",
            type="image",
            name=f"{label}.png",
            label=label,
            source="agent",
            purpose="character_reference",
        )

    persisted_snapshots = []

    async def fake_persist_state(state):
        persisted_snapshots.append([asset.label for asset in state.assets])

    pipeline._plan_generated_character_assets = fake_plan_generated_character_assets
    pipeline._generate_character_reference_asset = fake_generate_character_reference_asset
    pipeline._persist_state = fake_persist_state

    state = ProjectState(
        project_id="proj_character_skip",
        name="Character Skip",
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Mira sings with her white raven companion.",
            )
        ],
        assets=[
            MediaAsset(
                id="asset_user_mira",
                url="/projects/mira_user.png",
                type="image",
                name="mira_user.png",
                label="Mira",
            ),
            MediaAsset(
                id="asset_auto_duplicate",
                url="/projects/mira_auto.png",
                type="image",
                name="mira_auto.png",
                label="Mira",
                source="agent",
                purpose="character_reference",
            ),
        ],
    )

    await pipeline._ensure_generated_character_assets(state)

    assert generated_labels == ["Bird"]
    assert [asset.label for asset in state.assets] == ["Mira", "Bird"]
    assert all(
        not (
            asset.label == "Mira"
            and asset.source == "agent"
            and asset.purpose == "character_reference"
        )
        for asset in state.assets
    )
    assert persisted_snapshots[-1] == ["Mira", "Bird"]


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


def test_build_video_reference_assets_uses_storyboard_frame_then_relevant_named_assets(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    frame_path = projects_dir / "proj_clip_0.png"
    frame_path.write_bytes(b"frame")
    character_path = projects_dir / "mira.png"
    character_path.write_bytes(b"mira")
    background_path = projects_dir / "alley.png"
    background_path.write_bytes(b"alley")

    pipeline = FMVAgentPipeline(api_key=None)
    clip = VideoClip(
        id="clip_0",
        timeline_start=0.0,
        duration=6.0,
        storyboard_text="Mira walks through the neon alley.",
        image_url="/projects/proj_clip_0.png",
    )

    asset_lookup = {
        "asset_mira": MediaAsset(
            id="asset_mira",
            url="/projects/mira.png",
            type="image",
            name="mira.png",
            label="Mira",
        ),
        "asset_alley": MediaAsset(
            id="asset_alley",
            url="/projects/alley.png",
            type="image",
            name="alley.png",
            label="Neon Alley",
        ),
    }

    references = pipeline._build_video_reference_assets(
        clip=clip,
        relevant_assets=[
            {"id": "asset_alley", "type": "background"},
            {"id": "asset_mira", "type": "subject"},
        ],
        asset_lookup=asset_lookup,
    )

    assert [reference.label for reference in references] == [
        "Storyboard frame",
        "Mira",
        "Neon Alley",
    ]
    assert [reference.kind for reference in references] == ["subject", "subject", "background"]


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


def test_cloud_tasks_normalizes_malformed_base_url_env(monkeypatch):
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
    monkeypatch.setenv("FMV_BASE_URL", "https://public-backend.example.com)")
    monkeypatch.delenv("FMV_CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL", raising=False)
    monkeypatch.delenv("FMV_CLOUD_TASKS_AUDIENCE", raising=False)
    monkeypatch.delenv("FMV_INTERNAL_TASK_TOKEN", raising=False)

    job_queue_module._create_cloud_task(
        "proj_async",
        {"project_id": "proj_async", "run_id": "run_123"},
        "https://fmv-studio-frontend.example.com/api/projects/proj_async/run-async",
    )

    request = captured["request"]
    assert request["task"]["http_request"]["url"] == (
        "https://public-backend.example.com/api/internal/projects/proj_async/execute-run"
    )


@pytest.mark.asyncio
async def test_node_planning_generates_character_assets_after_timeline_creation():
    planning_response = json.dumps(
        [
            {
                "duration": 6,
                "storyboard_text": "Mira crosses the white soundstage toward camera.",
            },
            {
                "duration": 6,
                "storyboard_text": "Mira turns and reveals the raven perched on her shoulder.",
            },
        ]
    )

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **kwargs: SimpleNamespace(text=planning_response))
    )

    async def fake_measure_audio_duration_seconds(_music_url):
        return None

    ensure_calls = []

    async def fake_ensure_generated_character_assets(state):
        ensure_calls.append(
            {
                "stage": state.current_stage,
                "timeline_count": len(state.timeline),
            }
        )

    pipeline._measure_audio_duration_seconds = fake_measure_audio_duration_seconds
    pipeline._ensure_generated_character_assets = fake_ensure_generated_character_assets

    state = ProjectState(
        project_id="proj_plan_character_assets",
        name="Plan Character Assets",
        screenplay="Mira walks through a stark soundstage while her raven companion watches.",
        instructions="Clean and futuristic.",
    )

    result = await pipeline.node_planning(state)

    assert result.current_stage == AgentStage.PLANNING
    assert len(result.timeline) == 2
    assert ensure_calls == [{"stage": AgentStage.PLANNING, "timeline_count": 2}]


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


def test_update_project_resets_music_track_when_music_start_changes(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    original = ProjectState(
        project_id="proj_music_start_shift",
        name="Music Start Shift",
        current_stage=AgentStage.PRODUCTION,
        music_url="/projects/proj_music_start_shift_music.wav",
        music_duration_seconds=12.0,
        music_start_seconds=0.0,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Shot one",
                image_url="/projects/clip_0.png",
                image_approved=True,
                video_url="/projects/clip_0.mp4",
                video_approved=True,
            ),
            VideoClip(
                id="clip_1",
                timeline_start=6.0,
                duration=6.0,
                storyboard_text="Shot two",
                image_url="/projects/clip_1.png",
                image_approved=True,
                video_url="/projects/clip_1.mp4",
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
            ),
            ProductionTimelineFragment(
                id="clip_1_frag_0",
                source_clip_id="clip_1",
                timeline_start=6.0,
                source_start=0.0,
                duration=6.0,
                audio_enabled=True,
            ),
            ProductionTimelineFragment(
                id="music_frag_0",
                track_type="music",
                source_clip_id=None,
                timeline_start=0.0,
                source_start=0.0,
                duration=12.0,
                audio_enabled=True,
            ),
        ],
        final_video_url="/projects/proj_music_start_shift_final.mp4",
        stage_summaries={
            "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
            "storyboarding": StageSummary(text="Storyboarding", generated_at="2026-03-07T00:01:00+00:00"),
            "filming": StageSummary(text="Filming", generated_at="2026-03-07T00:02:00+00:00"),
            "production": StageSummary(text="Production", generated_at="2026-03-07T00:03:00+00:00"),
            "completed": StageSummary(text="Completed", generated_at="2026-03-07T00:04:00+00:00"),
        },
    )
    (projects_dir / "proj_music_start_shift.fmv").write_text(original.model_dump_json())

    updated = original.model_copy(deep=True)
    updated.music_start_seconds = 4.0

    result = endpoints_module.update_project("proj_music_start_shift", updated)

    assert result.music_start_seconds == 4.0
    assert [fragment.source_clip_id for fragment in result.production_timeline if (fragment.track_type or "video") != "music"] == [
        "clip_0",
        "clip_1",
    ]
    music_fragments = [fragment for fragment in result.production_timeline if (fragment.track_type or "video") == "music"]
    assert len(music_fragments) == 1
    assert music_fragments[0].timeline_start == 4.0
    assert music_fragments[0].duration == 12.0
    assert result.music_duration_seconds == 12.0
    assert result.final_video_url is None
    assert result.stage_summaries == {
        "planning": original.stage_summaries["planning"],
        "storyboarding": original.stage_summaries["storyboarding"],
        "filming": original.stage_summaries["filming"],
    }


def test_update_filming_clip_approval_preserves_other_rerendered_clips(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    original = ProjectState(
        project_id="proj_preserve_rerenders",
        name="Preserve Rerenders",
        current_stage=AgentStage.FILMING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Shot one",
                image_url="/projects/clip_0.png",
                image_approved=True,
                video_url="/projects/clip_0_rerender.mp4?t=111",
                video_approved=True,
            ),
            VideoClip(
                id="clip_1",
                timeline_start=6.0,
                duration=6.0,
                storyboard_text="Shot two",
                image_url="/projects/clip_1.png",
                image_approved=True,
                video_url="/projects/clip_1_rerender.mp4?t=222",
                video_approved=True,
            ),
            VideoClip(
                id="clip_2",
                timeline_start=12.0,
                duration=6.0,
                storyboard_text="Shot three",
                image_url="/projects/clip_2.png",
                image_approved=True,
                video_url="/projects/clip_2_rerender.mp4?t=333",
                video_approved=False,
            ),
        ],
        stage_summaries={
            "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
            "storyboarding": StageSummary(text="Storyboarding", generated_at="2026-03-07T00:01:00+00:00"),
            "filming": StageSummary(text="Filming", generated_at="2026-03-07T00:02:00+00:00"),
        },
    )
    (projects_dir / "proj_preserve_rerenders.fmv").write_text(original.model_dump_json())

    result = endpoints_module.update_filming_clip_approval(
        "proj_preserve_rerenders",
        "clip_2",
        endpoints_module.ClipApprovalRequest(approved=True),
    )

    assert result.current_stage == AgentStage.FILMING
    assert [clip.video_approved for clip in result.timeline] == [True, True, True]
    assert [clip.video_url for clip in result.timeline] == [
        "/projects/clip_0_rerender.mp4?t=111",
        "/projects/clip_1_rerender.mp4?t=222",
        "/projects/clip_2_rerender.mp4?t=333",
    ]
    assert result.final_video_url is None
    assert set(result.stage_summaries.keys()) == {"planning", "storyboarding"}

    persisted = endpoints_module.get_project("proj_preserve_rerenders")
    assert [clip.video_approved for clip in persisted.timeline] == [True, True, True]


def test_update_storyboard_clip_approval_preserves_other_approved_rerenders(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    original = ProjectState(
        project_id="proj_preserve_storyboard_rerenders",
        name="Preserve Storyboard Rerenders",
        current_stage=AgentStage.STORYBOARDING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Shot one",
                image_url="/projects/clip_0_rerender.png?t=111",
                image_approved=True,
                image_reference_ready=True,
            ),
            VideoClip(
                id="clip_1",
                timeline_start=6.0,
                duration=6.0,
                storyboard_text="Shot two",
                image_url="/projects/clip_1_rerender.png?t=222",
                image_approved=True,
                image_reference_ready=True,
            ),
            VideoClip(
                id="clip_2",
                timeline_start=12.0,
                duration=6.0,
                storyboard_text="Shot three",
                image_url="/projects/clip_2_rerender.png?t=333",
                image_approved=False,
                image_reference_ready=False,
            ),
        ],
        stage_summaries={
            "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
            "storyboarding": StageSummary(text="Storyboarding", generated_at="2026-03-07T00:01:00+00:00"),
        },
    )
    (projects_dir / "proj_preserve_storyboard_rerenders.fmv").write_text(original.model_dump_json())

    result = endpoints_module.update_storyboard_clip_approval(
        "proj_preserve_storyboard_rerenders",
        "clip_2",
        endpoints_module.ClipApprovalRequest(approved=True),
    )

    assert result.current_stage == AgentStage.STORYBOARDING
    assert [clip.image_approved for clip in result.timeline] == [True, True, True]
    assert [clip.image_url for clip in result.timeline] == [
        "/projects/clip_0_rerender.png?t=111",
        "/projects/clip_1_rerender.png?t=222",
        "/projects/clip_2_rerender.png?t=333",
    ]
    assert [clip.image_reference_ready for clip in result.timeline] == [True, True, True]
    assert set(result.stage_summaries.keys()) == {"planning"}


def test_update_project_keeps_rewound_planning_stage_when_outputs_could_otherwise_auto_advance(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    original = ProjectState(
        project_id="proj_rewound_planning_sticks",
        name="Rewound Planning Sticks",
        current_stage=AgentStage.PLANNING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Opening shot",
                image_url="/projects/clip_0_manual.png",
                image_prompt="manual frame",
                image_critiques=["Manual override"],
                image_approved=True,
                image_score=9,
                image_reference_ready=True,
                image_manual_override=True,
                video_url="/projects/clip_0.mp4",
                video_approved=True,
            )
        ],
        stage_summaries={
            "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
            "storyboarding": StageSummary(text="Storyboarding", generated_at="2026-03-07T00:01:00+00:00"),
            "filming": StageSummary(text="Filming", generated_at="2026-03-07T00:02:00+00:00"),
            "production": StageSummary(text="Production", generated_at="2026-03-07T00:03:00+00:00"),
        },
    )
    (projects_dir / "proj_rewound_planning_sticks.fmv").write_text(original.model_dump_json())

    edited = original.model_copy(deep=True)
    edited.timeline[0].duration = 8.0

    result = endpoints_module.update_project("proj_rewound_planning_sticks", edited)

    assert result.current_stage == AgentStage.PLANNING
    assert result.timeline[0].image_url == "/projects/clip_0_manual.png"
    assert result.timeline[0].image_manual_override is True
    assert result.timeline[0].video_url is None
    assert result.timeline[0].video_approved is False
    assert result.stage_summaries == {}


@pytest.mark.asyncio
async def test_endpoint_review_flow_requires_explicit_forward_actions_and_preserves_rerenders(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    class _FakePipeline:
        def __init__(self, *args, **kwargs):
            pass

        async def run_pipeline(self, state):
            next_state = state.model_copy(deep=True)

            if next_state.current_stage == AgentStage.PLANNING:
                for index, clip in enumerate(next_state.timeline):
                    clip.image_url = f"/projects/{next_state.project_id}_{clip.id}.png"
                    clip.image_prompt = f"Storyboard prompt {index}"
                    clip.image_approved = index == 0
                    clip.image_reference_ready = index == 0
                next_state.current_stage = AgentStage.STORYBOARDING
                return next_state

            if next_state.current_stage == AgentStage.STORYBOARDING:
                assert all(clip.image_approved and clip.image_url for clip in next_state.timeline)
                for index, clip in enumerate(next_state.timeline):
                    clip.video_url = f"/projects/{next_state.project_id}_{clip.id}_v1.mp4"
                    clip.video_prompt = f"Video prompt {index}"
                    clip.video_approved = index == 0
                next_state.current_stage = AgentStage.FILMING
                return next_state

            if next_state.current_stage == AgentStage.FILMING:
                if all(clip.video_approved and clip.video_url for clip in next_state.timeline):
                    next_state.production_timeline = [
                        ProductionTimelineFragment(
                            id=f"{clip.id}_frag_0",
                            source_clip_id=clip.id,
                            timeline_start=clip.timeline_start,
                            source_start=0.0,
                            duration=clip.duration,
                            audio_enabled=True,
                        )
                        for clip in next_state.timeline
                    ]
                    next_state.current_stage = AgentStage.PRODUCTION
                    return next_state

                for clip in next_state.timeline:
                    if clip.video_approved:
                        continue
                    clip.video_url = f"/projects/{next_state.project_id}_{clip.id}_rerender.mp4"
                    clip.video_prompt = f"Rerendered {clip.id}"
                next_state.current_stage = AgentStage.FILMING
                return next_state

            if next_state.current_stage == AgentStage.PRODUCTION:
                next_state.final_video_url = f"/projects/{next_state.project_id}_final_v2.mp4"
                next_state.current_stage = AgentStage.COMPLETED
                return next_state

            return next_state

    monkeypatch.setattr(endpoints_module, "FMVAgentPipeline", _FakePipeline)

    initial_state = ProjectState(
        project_id="proj_comprehensive_review_flow",
        name="Comprehensive Review Flow",
        current_stage=AgentStage.PLANNING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Opening shot",
            ),
            VideoClip(
                id="clip_1",
                timeline_start=6.0,
                duration=6.0,
                storyboard_text="Second shot",
            ),
        ],
        stage_summaries={
            "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
        },
    )
    (projects_dir / "proj_comprehensive_review_flow.fmv").write_text(initial_state.model_dump_json())

    storyboard_state = await endpoints_module.run_pipeline_step("proj_comprehensive_review_flow")
    assert storyboard_state.current_stage == AgentStage.STORYBOARDING
    assert [clip.image_approved for clip in storyboard_state.timeline] == [True, False]

    storyboard_review_state = endpoints_module.update_storyboard_clip_approval(
        "proj_comprehensive_review_flow",
        "clip_1",
        endpoints_module.ClipApprovalRequest(approved=True),
    )
    assert storyboard_review_state.current_stage == AgentStage.STORYBOARDING
    assert all(clip.image_approved for clip in storyboard_review_state.timeline)

    filming_state = await endpoints_module.run_pipeline_step("proj_comprehensive_review_flow")
    assert filming_state.current_stage == AgentStage.FILMING
    assert [clip.video_approved for clip in filming_state.timeline] == [True, False]

    filming_review_state = endpoints_module.update_filming_clip_approval(
        "proj_comprehensive_review_flow",
        "clip_1",
        endpoints_module.ClipApprovalRequest(approved=True),
    )
    assert filming_review_state.current_stage == AgentStage.FILMING
    original_clip_video_urls = [clip.video_url for clip in filming_review_state.timeline]

    production_state = await endpoints_module.run_pipeline_step("proj_comprehensive_review_flow")
    assert production_state.current_stage == AgentStage.PRODUCTION
    assert [fragment.source_clip_id for fragment in production_state.production_timeline] == ["clip_0", "clip_1"]

    completed_state = await endpoints_module.run_pipeline_step("proj_comprehensive_review_flow")
    assert completed_state.current_stage == AgentStage.COMPLETED
    assert completed_state.final_video_url == "/projects/proj_comprehensive_review_flow_final_v2.mp4"

    rewound_to_production = endpoints_module.revert_pipeline(
        "proj_comprehensive_review_flow",
        endpoints_module.RevertRequest(target_stage="production"),
    )
    assert rewound_to_production.current_stage == AgentStage.PRODUCTION
    assert rewound_to_production.final_video_url is None

    rewound_to_filming = endpoints_module.revert_pipeline(
        "proj_comprehensive_review_flow",
        endpoints_module.RevertRequest(target_stage="filming"),
    )
    assert rewound_to_filming.current_stage == AgentStage.FILMING
    assert [clip.video_url for clip in rewound_to_filming.timeline] == original_clip_video_urls

    rejected_for_refilm = endpoints_module.update_filming_clip_approval(
        "proj_comprehensive_review_flow",
        "clip_0",
        endpoints_module.ClipApprovalRequest(approved=False),
    )
    assert rejected_for_refilm.current_stage == AgentStage.FILMING
    assert rejected_for_refilm.timeline[0].video_approved is False
    assert rejected_for_refilm.timeline[1].video_url == original_clip_video_urls[1]

    rerendered_filming_state = await endpoints_module.run_pipeline_step("proj_comprehensive_review_flow")
    assert rerendered_filming_state.current_stage == AgentStage.FILMING
    assert rerendered_filming_state.timeline[0].video_url == "/projects/proj_comprehensive_review_flow_clip_0_rerender.mp4"
    assert rerendered_filming_state.timeline[1].video_url == original_clip_video_urls[1]

    reapproved_filming_state = endpoints_module.update_filming_clip_approval(
        "proj_comprehensive_review_flow",
        "clip_0",
        endpoints_module.ClipApprovalRequest(approved=True),
    )
    assert reapproved_filming_state.current_stage == AgentStage.FILMING
    assert [clip.video_approved for clip in reapproved_filming_state.timeline] == [True, True]
    assert reapproved_filming_state.timeline[1].video_url == original_clip_video_urls[1]

    back_to_production = await endpoints_module.run_pipeline_step("proj_comprehensive_review_flow")
    assert back_to_production.current_stage == AgentStage.PRODUCTION
    assert [fragment.source_clip_id for fragment in back_to_production.production_timeline] == ["clip_0", "clip_1"]


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
    assert updated_state.current_stage == AgentStage.PLANNING
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
    assert len(updated_state.director_undo_stack) == 1
    assert updated_state.director_undo_stack[0].snapshot["timeline"][0]["storyboard_text"] == "Original opening shot"


@pytest.mark.asyncio
async def test_live_director_can_update_multiple_numbered_shots_in_one_turn():
    action = {
        "reply_text": "I refreshed both shots with moodier framing.",
        "change_summary": ["Updated shots 1 and 2 for a darker, tighter storyboard pass."],
        "global_updates": {
            "screenplay": None,
            "instructions": None,
            "additional_lore": None,
            "lyrics_prompt": None,
            "style_prompt": None,
            "music_min_duration_seconds": None,
            "music_max_duration_seconds": None,
        },
        "clip_operations": [
            {
                "target_clip_id": "clip_0",
                "storyboard_text": "A tighter, moodier opening tableau with longer shadows and colder dawn light.",
                "duration": None,
                "video_prompt": None,
                "clear_target_image": False,
                "clear_target_video": False,
            },
            {
                "target_clip_id": "clip_1",
                "storyboard_text": "A matching close, dramatic second frame with stronger contrast and denser haze.",
                "duration": None,
                "video_prompt": None,
                "clear_target_image": False,
                "clear_target_video": False,
            },
        ],
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
        project_id="proj_live_director_multi_shot",
        name="Live Director Multi Shot",
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
        message="Make shots 1 and 2 moodier and more dramatic.",
        display_stage=AgentStage.STORYBOARDING,
    )

    assert result["target_clip_id"] == "clip_0"
    assert updated_state.timeline[0].storyboard_text == action["clip_operations"][0]["storyboard_text"]
    assert updated_state.timeline[1].storyboard_text == action["clip_operations"][1]["storyboard_text"]
    assert updated_state.current_stage == AgentStage.STORYBOARDING
    assert updated_state.final_video_url is None
    assert updated_state.production_timeline == []
    assert set(updated_state.stage_summaries.keys()) == {"planning"}
    assert updated_state.director_log[-1].applied_changes == action["change_summary"]


@pytest.mark.asyncio
async def test_live_director_can_rename_selected_asset():
    action = {
        "reply_text": "I renamed that reference to Mira Solis.",
        "change_summary": ["Renamed the selected reference asset."],
        "global_updates": {
            "screenplay": None,
            "instructions": None,
            "additional_lore": None,
            "lyrics_prompt": None,
            "style_prompt": None,
            "music_min_duration_seconds": None,
            "music_max_duration_seconds": None,
        },
        "clip_operations": [],
        "asset_operations": [
            {
                "operation_type": "update_label",
                "target_asset_id": None,
                "label": "Mira Solis",
            }
        ],
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
        project_id="proj_live_director_asset_rename",
        name="Live Director Asset Rename",
        current_stage=AgentStage.INPUT,
        assets=[
            MediaAsset(
                id="asset_hero",
                url="/projects/mira.png",
                type="image",
                name="mira.png",
                label="Lead Singer",
                ai_context="Portrait reference for the lead singer.",
            )
        ],
    )

    updated_state, result = await pipeline.handle_live_director_mode(
        state,
        message="Rename this asset to Mira Solis.",
        display_stage=AgentStage.INPUT,
        selected_asset_id="asset_hero",
    )

    assert updated_state.assets[0].label == "Mira Solis"
    assert result["target_asset_id"] == "asset_hero"
    assert updated_state.current_stage == AgentStage.INPUT
    assert updated_state.director_log[-1].applied_changes == action["change_summary"]
    assert len(updated_state.director_undo_stack) == 1


@pytest.mark.asyncio
async def test_live_director_can_delete_audio_asset_and_clear_music_outputs():
    action = {
        "reply_text": "I removed the uploaded song reference.",
        "change_summary": ["Deleted the selected audio asset."],
        "global_updates": {
            "screenplay": None,
            "instructions": None,
            "additional_lore": None,
            "lyrics_prompt": None,
            "style_prompt": None,
            "music_min_duration_seconds": None,
            "music_max_duration_seconds": None,
        },
        "clip_operations": [],
        "asset_operations": [
            {
                "operation_type": "delete",
                "target_asset_id": "asset_song",
                "label": None,
            }
        ],
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
        project_id="proj_live_director_asset_delete",
        name="Live Director Asset Delete",
        current_stage=AgentStage.COMPLETED,
        music_url="/projects/song.wav",
        music_workflow="uploaded_track",
        final_video_url="/projects/final.mp4",
        assets=[
            MediaAsset(
                id="asset_song",
                url="/projects/song.wav",
                type="audio",
                name="song.wav",
                label="Demo Song",
            ),
            MediaAsset(
                id="asset_ref",
                url="/projects/mira.png",
                type="image",
                name="mira.png",
                label="Mira",
            ),
        ],
        production_timeline=[
            ProductionTimelineFragment(
                id="video_frag_0",
                track_type="video",
                source_clip_id="clip_0",
                timeline_start=0.0,
                source_start=0.0,
                duration=6.0,
                audio_enabled=True,
            ),
            ProductionTimelineFragment(
                id="music_frag_0",
                track_type="music",
                source_clip_id=None,
                timeline_start=0.0,
                source_start=0.0,
                duration=6.0,
                audio_enabled=True,
            ),
        ],
    )

    updated_state, result = await pipeline.handle_live_director_mode(
        state,
        message="Delete the uploaded song asset.",
        display_stage=AgentStage.INPUT,
        selected_asset_id="asset_song",
    )

    assert [asset.id for asset in updated_state.assets] == ["asset_ref"]
    assert updated_state.music_url is None
    assert updated_state.music_workflow == "lyria3"
    assert updated_state.final_video_url is None
    assert all((fragment.track_type or "video") != "music" for fragment in updated_state.production_timeline)
    assert result["target_asset_id"] is None
    assert updated_state.director_log[-1].applied_changes == action["change_summary"]


@pytest.mark.asyncio
async def test_live_director_can_regenerate_selected_image_asset_and_clear_affected_shots():
    action = {
        "director_operation": "none",
        "reply_text": "I updated the character reference to reflect the new look.",
        "change_summary": ["Updated the selected character reference."],
        "global_updates": {
            "screenplay": None,
            "instructions": None,
            "additional_lore": None,
            "lyrics_prompt": None,
            "style_prompt": None,
            "music_min_duration_seconds": None,
            "music_max_duration_seconds": None,
        },
        "clip_operations": [],
        "asset_operations": [
            {
                "operation_type": "regenerate_image",
                "target_asset_id": None,
                "label": None,
                "generation_instruction": "Update Mira to have a short silver bob and a crimson sequined jacket while preserving her identity.",
            }
        ],
        "target_fragment_id": None,
        "fragment_updates": {
            "audio_enabled": None,
        },
        "navigation_action": "stay",
        "target_stage": None,
        "rewind_to_stage": None,
    }

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **kwargs: SimpleNamespace(text=json.dumps(action)))
    )

    async def fake_regenerate_asset(state, *, asset_id, generation_instruction):
        assert asset_id == "asset_hero"
        assert "silver bob" in generation_instruction
        asset = next(asset for asset in state.assets if asset.id == asset_id)
        asset.url = "/projects/mira_refined.png"
        asset.ai_context = "Updated character reference for Mira with silver hair and crimson wardrobe."
        return asset

    async def fake_affected_clips(state, *, asset):
        assert asset.id == "asset_hero"
        return {"clip_0"}

    pipeline._regenerate_director_image_asset = fake_regenerate_asset
    pipeline._resolve_director_asset_affected_clip_ids = fake_affected_clips

    state = ProjectState(
        project_id="proj_live_director_asset_regenerate",
        name="Live Director Asset Regenerate",
        current_stage=AgentStage.COMPLETED,
        assets=[
            MediaAsset(
                id="asset_hero",
                url="/projects/mira.png",
                type="image",
                name="mira.png",
                label="Mira",
                ai_context="Portrait reference for Mira.",
                purpose="character_reference",
            )
        ],
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Mira sings under neon lights in a rain-slick alley.",
                image_url="/projects/clip_0.png",
                image_approved=True,
                image_score=9,
                image_reference_ready=True,
                video_url="/projects/clip_0.mp4",
                video_approved=True,
            )
        ],
        final_video_url="/projects/final.mp4",
        stage_summaries={
            "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
            "storyboarding": StageSummary(text="Storyboarding", generated_at="2026-03-07T00:01:00+00:00"),
            "filming": StageSummary(text="Filming", generated_at="2026-03-07T00:02:00+00:00"),
            "completed": StageSummary(text="Completed", generated_at="2026-03-07T00:03:00+00:00"),
        },
    )

    updated_state, result = await pipeline.handle_live_director_mode(
        state,
        message="Change this character so she has a short silver bob and a crimson sequined jacket.",
        display_stage=AgentStage.STORYBOARDING,
        selected_asset_id="asset_hero",
        speech_mode="realtime",
    )

    assert updated_state.assets[0].url == "/projects/mira_refined.png"
    assert updated_state.timeline[0].storyboard_text == state.timeline[0].storyboard_text
    assert updated_state.timeline[0].image_url is None
    assert updated_state.timeline[0].video_url is None
    assert updated_state.final_video_url is None
    assert updated_state.current_stage == AgentStage.STORYBOARDING
    assert set(updated_state.stage_summaries.keys()) == {"planning"}


@pytest.mark.asyncio
async def test_live_director_asset_regeneration_keeps_planning_stage_when_reviewing_planning():
    action = {
        "director_operation": "none",
        "reply_text": "I updated the character reference and kept the planning stage in focus.",
        "change_summary": ["Updated the selected character reference."],
        "global_updates": {
            "screenplay": None,
            "instructions": None,
            "additional_lore": None,
            "lyrics_prompt": None,
            "style_prompt": None,
            "music_min_duration_seconds": None,
            "music_max_duration_seconds": None,
        },
        "clip_operations": [],
        "asset_operations": [
            {
                "operation_type": "regenerate_image",
                "target_asset_id": "asset_hero",
                "label": None,
                "generation_instruction": "Update Mira to wear a structured white coat while preserving her identity.",
            }
        ],
        "target_fragment_id": None,
        "fragment_updates": {
            "audio_enabled": None,
        },
        "navigation_action": "stay",
        "target_stage": None,
        "rewind_to_stage": None,
    }

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **kwargs: SimpleNamespace(text=json.dumps(action)))
    )

    async def fake_regenerate_asset(state, *, asset_id, generation_instruction):
        assert asset_id == "asset_hero"
        assert "white coat" in generation_instruction
        asset = next(asset for asset in state.assets if asset.id == asset_id)
        asset.url = "/projects/mira_white_coat.png"
        asset.ai_context = "Updated character reference for Mira in a structured white coat."
        return asset

    async def fake_affected_clips(state, *, asset):
        assert asset.id == "asset_hero"
        return {"clip_0"}

    pipeline._regenerate_director_image_asset = fake_regenerate_asset
    pipeline._resolve_director_asset_affected_clip_ids = fake_affected_clips

    state = ProjectState(
        project_id="proj_live_director_asset_regenerate_planning",
        name="Live Director Asset Regenerate Planning",
        current_stage=AgentStage.COMPLETED,
        assets=[
            MediaAsset(
                id="asset_hero",
                url="/projects/mira.png",
                type="image",
                name="mira.png",
                label="Mira",
                ai_context="Portrait reference for Mira.",
                purpose="character_reference",
            )
        ],
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Mira walks across a polished white soundstage.",
                image_url="/projects/clip_0.png",
                image_approved=True,
                image_score=9,
                image_reference_ready=True,
                video_url="/projects/clip_0.mp4",
                video_approved=True,
            )
        ],
        final_video_url="/projects/final.mp4",
        stage_summaries={
            "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
            "storyboarding": StageSummary(text="Storyboarding", generated_at="2026-03-07T00:01:00+00:00"),
            "filming": StageSummary(text="Filming", generated_at="2026-03-07T00:02:00+00:00"),
            "completed": StageSummary(text="Completed", generated_at="2026-03-07T00:03:00+00:00"),
        },
    )

    updated_state, result = await pipeline.handle_live_director_mode(
        state,
        message="Change this character so she wears a structured white coat.",
        display_stage=AgentStage.PLANNING,
        selected_asset_id="asset_hero",
        speech_mode="realtime",
    )

    assert updated_state.assets[0].url == "/projects/mira_white_coat.png"
    assert updated_state.timeline[0].image_url is None
    assert updated_state.timeline[0].video_url is None
    assert updated_state.final_video_url is None
    assert updated_state.current_stage == AgentStage.PLANNING
    assert result["stage"] == AgentStage.PLANNING.value
    assert set(updated_state.stage_summaries.keys()) == {"planning"}
    assert result["target_asset_id"] == "asset_hero"
    assert updated_state.director_log[-1].applied_changes == action["change_summary"]


@pytest.mark.asyncio
async def test_create_director_image_asset_uses_character_reference_settings(monkeypatch):
    pipeline = FMVAgentPipeline(api_key="dummy", image_size="2K")
    pipeline.client = SimpleNamespace()

    captured = {}

    async def fake_generate_google_image(
        *,
        contents,
        aspect_ratio,
        image_size,
        target_width,
        target_height,
        pad_color,
    ):
        captured["contents"] = contents
        captured["aspect_ratio"] = aspect_ratio
        captured["image_size"] = image_size
        captured["target_width"] = target_width
        captured["target_height"] = target_height
        captured["pad_color"] = pad_color
        return b"director-asset", "image/png"

    monkeypatch.setattr(pipeline, "_generate_google_image", fake_generate_google_image)
    monkeypatch.setattr(pipeline, "_run_with_resource_exhausted_retry", lambda operation: operation())
    monkeypatch.setattr(
        pipeline,
        "_write_project_asset_bytes",
        lambda relative_path, data, *, content_type=None: f"/projects/{relative_path}",
    )
    monkeypatch.setattr(
        graph_module,
        "analyze_uploaded_asset",
        lambda **kwargs: asyncio.sleep(0, result="Canonical portrait reference for Mira Solis."),
    )

    state = ProjectState(project_id="proj_director_create_asset", name="Director Create Asset")

    asset = await pipeline._create_director_image_asset(
        state,
        label="Mira Solis",
        generation_instruction="Create a canonical portrait reference for Mira with silver hair and a structured white coat.",
        purpose="character_reference",
    )

    assert asset is not None
    assert asset in state.assets
    assert asset.label == "Mira Solis"
    assert asset.purpose == "character_reference"
    assert asset.source == "agent"
    assert asset.ai_context == "Canonical portrait reference for Mira Solis."
    assert captured["aspect_ratio"] == "9:16"
    assert captured["image_size"] == "2K"
    assert captured["target_width"] == 1152
    assert captured["target_height"] == 2048
    assert captured["pad_color"] == "white"
    assert "Single centered character portrait reference." in captured["contents"][0]
    assert "Neutral seamless white studio background." in captured["contents"][0]


@pytest.mark.asyncio
async def test_live_director_can_create_new_image_asset_and_target_it():
    action = {
        "director_operation": "none",
        "reply_text": "I created a new rooftop location reference.",
        "change_summary": ["Created a new location reference asset."],
        "global_updates": {
            "screenplay": None,
            "instructions": None,
            "additional_lore": None,
            "lyrics_prompt": None,
            "style_prompt": None,
            "music_min_duration_seconds": None,
            "music_max_duration_seconds": None,
        },
        "clip_operations": [],
        "asset_operations": [
            {
                "operation_type": "create_image",
                "target_asset_id": None,
                "label": "Neon Alley Rooftop",
                "generation_instruction": "Create a moody rooftop location reference with wet concrete, magenta neon spill, and distant city haze.",
                "purpose": "reference_image",
            }
        ],
        "target_fragment_id": None,
        "fragment_updates": {
            "audio_enabled": None,
        },
        "navigation_action": "stay",
        "target_stage": None,
        "rewind_to_stage": None,
    }

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **kwargs: SimpleNamespace(text=json.dumps(action)))
    )

    async def fake_create_asset(state, *, label, generation_instruction, purpose=None):
        assert label == "Neon Alley Rooftop"
        assert "rooftop location reference" in generation_instruction
        assert purpose == "reference_image"
        asset = MediaAsset(
            id="asset_neon_rooftop",
            url="/projects/neon_rooftop.png",
            type="image",
            name="Neon Alley Rooftop.png",
            label=label,
            ai_context="Rooftop location reference with wet concrete, magenta glow, and distant skyline haze.",
            source="agent",
            purpose="reference_image",
        )
        state.assets.append(asset)
        return asset

    pipeline._create_director_image_asset = fake_create_asset

    state = ProjectState(
        project_id="proj_live_director_asset_create",
        name="Live Director Asset Create",
        current_stage=AgentStage.PLANNING,
    )

    updated_state, result = await pipeline.handle_live_director_mode(
        state,
        message="Create a new rooftop location reference asset for Neon Alley.",
        display_stage=AgentStage.PLANNING,
        speech_mode="realtime",
    )

    assert len(updated_state.assets) == 1
    assert updated_state.assets[0].id == "asset_neon_rooftop"
    assert updated_state.current_stage == AgentStage.PLANNING
    assert result["target_asset_id"] == "asset_neon_rooftop"
    assert updated_state.director_log[-1].applied_changes == action["change_summary"]
    assert len(updated_state.director_undo_stack) == 1


@pytest.mark.asyncio
async def test_live_director_can_insert_and_delete_shots_in_storyboarding_review():
    action = {
        "reply_text": "I added a new opening shot and removed the redundant second shot.",
        "change_summary": [],
        "global_updates": {
            "screenplay": None,
            "instructions": None,
            "additional_lore": None,
            "lyrics_prompt": None,
            "style_prompt": None,
            "music_min_duration_seconds": None,
            "music_max_duration_seconds": None,
        },
        "clip_operations": [
            {
                "operation_type": "insert_before",
                "target_clip_id": "clip_0",
                "anchor_clip_id": None,
                "storyboard_text": "A dawn establishing frame over the empty stadium before the singer enters.",
                "duration": 4,
                "video_prompt": None,
                "clear_target_image": False,
                "clear_target_video": False,
            },
            {
                "operation_type": "delete",
                "target_clip_id": "clip_1",
                "anchor_clip_id": None,
                "storyboard_text": None,
                "duration": None,
                "video_prompt": None,
                "clear_target_image": False,
                "clear_target_video": False,
            },
        ],
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
        project_id="proj_live_director_insert_delete",
        name="Live Director Insert Delete",
        current_stage=AgentStage.FILMING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Original opening shot",
                image_url="/projects/clip_0.png",
                image_approved=True,
                image_score=9,
                image_reference_ready=True,
                video_url="/projects/clip_0.mp4",
                video_approved=True,
            ),
            VideoClip(
                id="clip_1",
                timeline_start=6.0,
                duration=6.0,
                storyboard_text="Original second shot",
                image_url="/projects/clip_1.png",
                image_approved=True,
                image_score=8,
                image_reference_ready=True,
                video_url="/projects/clip_1.mp4",
                video_approved=True,
            ),
        ],
        final_video_url="/projects/proj_live_director_insert_delete_final.mp4",
        stage_summaries={
            "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
            "storyboarding": StageSummary(text="Storyboarding", generated_at="2026-03-07T00:01:00+00:00"),
            "filming": StageSummary(text="Filming", generated_at="2026-03-07T00:02:00+00:00"),
        },
    )

    updated_state, result = await pipeline.handle_live_director_mode(
        state,
        message="Add a new opening shot before shot 1 and delete shot 2.",
        display_stage=AgentStage.STORYBOARDING,
    )

    assert updated_state.current_stage == AgentStage.STORYBOARDING
    assert len(updated_state.timeline) == 2
    assert updated_state.timeline[0].id != "clip_0"
    assert updated_state.timeline[0].storyboard_text == action["clip_operations"][0]["storyboard_text"]
    assert updated_state.timeline[0].image_url is None
    assert updated_state.timeline[0].video_url is None
    assert updated_state.timeline[1].id == "clip_0"
    assert updated_state.timeline[1].image_url == "/projects/clip_0.png"
    assert updated_state.timeline[1].video_url == "/projects/clip_0.mp4"
    assert updated_state.timeline[1].timeline_start == 4.0
    assert updated_state.final_video_url is None
    assert set(updated_state.stage_summaries.keys()) <= {"planning", "lyria_prompting", "input"}
    assert result["target_clip_id"] == updated_state.timeline[0].id
    assert updated_state.director_log[-1].applied_changes == [
        f"Added shot 1 before shot 1.",
        "Deleted shot 2.",
    ]


@pytest.mark.asyncio
async def test_live_director_can_reorder_shots():
    action = {
        "reply_text": "I moved shot 3 after shot 1.",
        "change_summary": [],
        "global_updates": {
            "screenplay": None,
            "instructions": None,
            "additional_lore": None,
            "lyrics_prompt": None,
            "style_prompt": None,
            "music_min_duration_seconds": None,
            "music_max_duration_seconds": None,
        },
        "clip_operations": [
            {
                "operation_type": "move_after",
                "target_clip_id": "clip_2",
                "anchor_clip_id": "clip_0",
                "storyboard_text": None,
                "duration": None,
                "video_prompt": None,
                "clear_target_image": False,
                "clear_target_video": False,
            }
        ],
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
        project_id="proj_live_director_reorder",
        name="Live Director Reorder",
        current_stage=AgentStage.PRODUCTION,
        timeline=[
            VideoClip(id="clip_0", timeline_start=0.0, duration=6.0, storyboard_text="Shot one", image_url="/projects/clip_0.png", image_approved=True, image_score=9, image_reference_ready=True, video_url="/projects/clip_0.mp4", video_approved=True),
            VideoClip(id="clip_1", timeline_start=6.0, duration=6.0, storyboard_text="Shot two", image_url="/projects/clip_1.png", image_approved=True, image_score=8, image_reference_ready=True, video_url="/projects/clip_1.mp4", video_approved=True),
            VideoClip(id="clip_2", timeline_start=12.0, duration=6.0, storyboard_text="Shot three", image_url="/projects/clip_2.png", image_approved=True, image_score=8, image_reference_ready=True, video_url="/projects/clip_2.mp4", video_approved=True),
        ],
    )

    updated_state, result = await pipeline.handle_live_director_mode(
        state,
        message="Move shot 3 after shot 1.",
        display_stage=AgentStage.STORYBOARDING,
    )

    assert [clip.id for clip in updated_state.timeline] == ["clip_0", "clip_2", "clip_1"]
    assert [clip.timeline_start for clip in updated_state.timeline] == [0.0, 6.0, 12.0]
    assert updated_state.current_stage == AgentStage.STORYBOARDING
    assert result["target_clip_id"] == "clip_2"
    assert updated_state.director_log[-1].applied_changes == ["Moved shot 3 after shot 1."]


@pytest.mark.asyncio
async def test_live_director_returns_advance_navigation_intent_without_mutating_stage():
    action = {
        "reply_text": "Moving ahead to the next stage.",
        "change_summary": ["Proceeding to the next stage."],
        "global_updates": {
            "screenplay": None,
            "instructions": None,
            "additional_lore": None,
            "lyrics_prompt": None,
            "style_prompt": None,
            "music_min_duration_seconds": None,
            "music_max_duration_seconds": None,
        },
        "clip_operations": [],
        "target_fragment_id": None,
        "fragment_updates": {
            "audio_enabled": None,
        },
        "navigation_action": "advance",
        "target_stage": None,
        "rewind_to_stage": None,
    }

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **kwargs: SimpleNamespace(text=json.dumps(action)))
    )

    state = ProjectState(
        project_id="proj_live_director_advance",
        name="Live Director Advance",
        current_stage=AgentStage.PLANNING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Opening shot",
            )
        ],
    )

    updated_state, result = await pipeline.handle_live_director_mode(
        state,
        message="Go to the next stage.",
        display_stage=AgentStage.PLANNING,
    )

    assert updated_state.current_stage == AgentStage.PLANNING
    assert result["navigation_action"] == "advance"
    assert result["target_stage"] is None
    assert updated_state.director_log[-1].text == action["reply_text"]


@pytest.mark.asyncio
async def test_live_director_does_not_infer_navigation_when_model_omits_it():
    action = {
        "director_operation": "none",
        "reply_text": "Proceeding to the next stage.",
        "change_summary": ["Proceeding to the next stage."],
        "global_updates": {
            "screenplay": None,
            "instructions": None,
            "additional_lore": None,
            "lyrics_prompt": None,
            "style_prompt": None,
            "music_min_duration_seconds": None,
            "music_max_duration_seconds": None,
        },
        "clip_operations": [],
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
        project_id="proj_live_director_infer_advance",
        name="Live Director Infer Advance",
        current_stage=AgentStage.PLANNING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Opening shot",
            )
        ],
    )

    updated_state, result = await pipeline.handle_live_director_mode(
        state,
        message="Move to the next stage.",
        display_stage=AgentStage.PLANNING,
    )

    assert updated_state.current_stage == AgentStage.PLANNING
    assert result["navigation_action"] == "stay"
    assert result["target_stage"] is None


@pytest.mark.asyncio
async def test_live_director_undo_restores_previous_state_from_dissatisfaction_feedback():
    action = {
        "director_operation": "undo_last_change",
        "reply_text": "Reverting to the previous version.",
        "change_summary": ["Reverting the last Live Director change."],
        "global_updates": {
            "screenplay": None,
            "instructions": None,
            "additional_lore": None,
            "lyrics_prompt": None,
            "style_prompt": None,
            "music_min_duration_seconds": None,
            "music_max_duration_seconds": None,
        },
        "clip_operations": [],
        "asset_operations": [],
        "target_fragment_id": None,
        "fragment_updates": {
            "audio_enabled": None,
        },
        "navigation_action": "stay",
        "target_stage": None,
        "rewind_to_stage": None,
    }

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kwargs: SimpleNamespace(text=json.dumps(action))
        )
    )

    previous_state = ProjectState(
        project_id="proj_live_director_undo",
        name="Live Director Undo",
        current_stage=AgentStage.STORYBOARDING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Original opening shot with the singer silhouetted against a neon marquee.",
                image_url="/projects/clip_0.png",
                image_approved=True,
                image_score=9,
                image_reference_ready=True,
            )
        ],
        stage_summaries={
            "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
            "storyboarding": StageSummary(text="Storyboarding", generated_at="2026-03-07T00:01:00+00:00"),
        },
    )

    state = previous_state.model_copy(deep=True)
    state.timeline[0].storyboard_text = "A flat close shot that lost the original mood."
    state.timeline[0].image_url = None
    state.timeline[0].image_approved = False
    state.stage_summaries = {
        "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
    }
    state.director_undo_stack = [
        DirectorUndoEntry(
            id="director_undo_1",
            message="Make shot 1 flatter.",
            stage=AgentStage.STORYBOARDING.value,
            created_at="2026-03-07T00:05:00+00:00",
            change_summary=["Updated shot 1 and cleared its frame/video outputs."],
            snapshot=previous_state.model_dump(exclude={"director_log", "director_undo_stack"}),
        )
    ]

    updated_state, result = await pipeline.handle_live_director_mode(
        state,
        message="Can we go back to the version from before that change? It missed the intent.",
        display_stage=AgentStage.STORYBOARDING,
        source="voice",
        speech_mode="realtime",
    )

    assert updated_state.current_stage == AgentStage.STORYBOARDING
    assert updated_state.timeline[0].storyboard_text == previous_state.timeline[0].storyboard_text
    assert updated_state.timeline[0].image_url == previous_state.timeline[0].image_url
    assert updated_state.stage_summaries == previous_state.stage_summaries
    assert updated_state.director_undo_stack == []
    assert result["navigation_action"] == "stay"
    assert result["applied_changes"][0] == "Reverted the last Live Director change."
    assert updated_state.director_log[-2].role == "user"
    assert updated_state.director_log[-2].text == "Can we go back to the version from before that change? It missed the intent."
    assert updated_state.director_log[-1].role == "agent"
    assert "Reverted the last Live Director change" in updated_state.director_log[-1].text


@pytest.mark.asyncio
async def test_live_director_undo_reports_when_nothing_is_available_to_revert():
    action = {
        "director_operation": "undo_last_change",
        "reply_text": "I will revert the last change.",
        "change_summary": [],
        "global_updates": {
            "screenplay": None,
            "instructions": None,
            "additional_lore": None,
            "lyrics_prompt": None,
            "style_prompt": None,
            "music_min_duration_seconds": None,
            "music_max_duration_seconds": None,
        },
        "clip_operations": [],
        "asset_operations": [],
        "target_fragment_id": None,
        "fragment_updates": {
            "audio_enabled": None,
        },
        "navigation_action": "stay",
        "target_stage": None,
        "rewind_to_stage": None,
    }

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kwargs: SimpleNamespace(text=json.dumps(action))
        )
    )

    state = ProjectState(
        project_id="proj_live_director_empty_undo",
        name="Live Director Empty Undo",
        current_stage=AgentStage.PLANNING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Opening shot",
            )
        ],
    )

    updated_state, result = await pipeline.handle_live_director_mode(
        state,
        message="Can you revert the last Live Director change?",
        display_stage=AgentStage.PLANNING,
        speech_mode="realtime",
    )

    assert updated_state.current_stage == AgentStage.PLANNING
    assert updated_state.timeline[0].storyboard_text == "Opening shot"
    assert updated_state.director_undo_stack == []
    assert result["applied_changes"] == []
    assert updated_state.director_log[-1].text == "There isn't a previous Live Director change to undo."


@pytest.mark.asyncio
async def test_live_director_realtime_mode_skips_reply_audio():
    action = {
        "reply_text": "I tightened the direction and updated the target shot.",
        "change_summary": ["Updated shot 1."],
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
            "storyboard_text": "A tighter, moodier opening frame with more dramatic dawn haze.",
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

    async def _unexpected_tts(*args, **kwargs):
        raise AssertionError("Realtime live director path should not synthesize fallback TTS audio.")

    pipeline._synthesize_director_reply_audio = _unexpected_tts

    state = ProjectState(
        project_id="proj_live_director_realtime",
        name="Live Director Realtime",
        current_stage=AgentStage.STORYBOARDING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Original opening shot",
                image_url="/projects/clip_0.png",
                image_approved=True,
            ),
        ],
    )

    updated_state, result = await pipeline.handle_live_director_mode(
        state,
        message="Make shot 1 tighter and moodier.",
        display_stage=AgentStage.STORYBOARDING,
        selected_clip_id="clip_0",
        source="voice",
        speech_mode="realtime",
    )

    assert result["target_clip_id"] == "clip_0"
    assert updated_state.director_log[-1].role == "agent"
    assert updated_state.director_log[-1].audio_url is None
    assert updated_state.director_log[-1].text == action["reply_text"]


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
async def test_live_director_enriches_literal_storyboard_update_before_applying():
    user_message = "Add more description to this shot."
    action = {
        "reply_text": "I enriched the shot description.",
        "change_summary": ["Expanded the selected storyboard frame."],
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
            "storyboard_text": user_message,
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
    enriched_storyboard = (
        "A medium-wide rooftop frame at blue hour, with the singer silhouetted against a glowing skyline, "
        "wind tugging at the coat while distant neon reflections shimmer across the wet concrete."
    )

    class _SequencedResponder:
        def __init__(self):
            self.calls = []

        def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return SimpleNamespace(text=json.dumps(action))
            return SimpleNamespace(text=enriched_storyboard)

    responder = _SequencedResponder()
    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=responder.generate_content)
    )
    async def _skip_director_audio(*args, **kwargs):
        return None
    pipeline._synthesize_director_reply_audio = _skip_director_audio

    state = ProjectState(
        project_id="proj_live_director_enrich",
        name="Live Director Enrich",
        current_stage=AgentStage.STORYBOARDING,
        screenplay="A rooftop performance at dusk.",
        instructions="Cinematic realism with moody city lighting.",
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Singer on a rooftop at dusk.",
            )
        ],
    )

    updated_state, result = await pipeline.handle_live_director_mode(
        state,
        message=user_message,
        display_stage=AgentStage.STORYBOARDING,
        selected_clip_id="clip_0",
    )

    assert result["target_clip_id"] == "clip_0"
    assert updated_state.timeline[0].storyboard_text == enriched_storyboard
    assert len(responder.calls) == 2
    assert "finished, richer project copy" in responder.calls[0]["contents"][0]
    assert "Return ONLY the final field text" in responder.calls[1]["contents"][0]


@pytest.mark.asyncio
async def test_live_director_skips_enrichment_for_already_detailed_storyboard_update():
    action = {
        "reply_text": "I sharpened the shot direction.",
        "change_summary": ["Refined the selected storyboard frame."],
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
            "storyboard_text": (
                "A low-angle close shot of the singer under hard amber sidelighting, with drifting smoke, "
                "rain flecks on the lens, and the skyline reduced to soft bokeh behind them."
            ),
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

    class _SingleResponder:
        def __init__(self):
            self.calls = []

        def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(text=json.dumps(action))

    responder = _SingleResponder()
    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=responder.generate_content)
    )
    async def _skip_director_audio(*args, **kwargs):
        return None
    pipeline._synthesize_director_reply_audio = _skip_director_audio

    state = ProjectState(
        project_id="proj_live_director_no_extra_rewrite",
        name="Live Director No Extra Rewrite",
        current_stage=AgentStage.STORYBOARDING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Singer on a rooftop at dusk.",
            )
        ],
    )

    updated_state, result = await pipeline.handle_live_director_mode(
        state,
        message="Make this shot moodier and more cinematic.",
        display_stage=AgentStage.STORYBOARDING,
        selected_clip_id="clip_0",
    )

    assert result["target_clip_id"] == "clip_0"
    assert updated_state.timeline[0].storyboard_text == action["clip_updates"]["storyboard_text"]
    assert len(responder.calls) == 1


@pytest.mark.asyncio
async def test_live_director_writes_agent_reply_audio_with_stage_brief_voice_path(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_storage_roots(monkeypatch, tmp_path)

    action = {
        "reply_text": "I tightened the frame and deepened the lighting contrast.",
        "change_summary": ["Refined the selected storyboard frame."],
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
            "storyboard_text": (
                "A close profile frame with harder side light carving the singer out from the darkened skyline."
            ),
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
    fake_tts_response = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            inline_data=SimpleNamespace(
                                data=b"\x00\x00" * 300,
                            )
                        )
                    ]
                )
            )
        ]
    )

    class _SequencedResponder:
        def __init__(self):
            self.calls = []

        def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return SimpleNamespace(text=json.dumps(action))
            return fake_tts_response

    responder = _SequencedResponder()
    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(generate_content=responder.generate_content)
    )

    state = ProjectState(
        project_id="proj_live_director_audio",
        name="Live Director Audio",
        current_stage=AgentStage.STORYBOARDING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Singer on a rooftop at dusk.",
            )
        ],
    )

    updated_state, _ = await pipeline.handle_live_director_mode(
        state,
        message="Make this frame tighter.",
        display_stage=AgentStage.STORYBOARDING,
        selected_clip_id="clip_0",
    )

    assert len(updated_state.director_log) == 2
    assert updated_state.director_log[0].audio_url is None
    assert updated_state.director_log[1].audio_url is not None
    audio_path = Path(_local_media_path(updated_state.director_log[1].audio_url))
    assert audio_path.exists()
    assert audio_path.read_bytes()[:4] == b"RIFF"
    assert responder.calls[1]["config"].speech_config.voice_config.prebuilt_voice_config.voice_name == pipeline.stage_brief_voice


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
    assert result.timeline[0].video_approved is True
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
    assert request["config"].generate_audio is True
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
async def test_generate_google_video_clip_supports_4k_resolution(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    storyboard_path = tmp_path / "storyboard.png"
    storyboard_path.write_bytes(b"fake-image")

    pipeline = FMVAgentPipeline(api_key="dummy", video_resolution="4k")
    pipeline.client = _FakeClient(video_bytes=b"fake-video-bytes")

    video_bytes = await pipeline._generate_google_video_clip(
        prompt="Animate this scene in maximum detail.",
        duration_seconds=6,
        image_path=str(storyboard_path),
    )

    assert video_bytes == b"fake-video-bytes"
    request = pipeline.client.models.calls[0]
    assert pipeline.video_width == 3840
    assert pipeline.video_height == 2160
    assert request["config"].resolution == "4k"
    assert request["config"].generate_audio is True


@pytest.mark.asyncio
async def test_generate_google_video_clip_uses_ingredients_mode_with_selected_stable_model(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    storyboard_path = tmp_path / "storyboard.png"
    storyboard_path.write_bytes(b"storyboard-image")
    character_path = tmp_path / "mira.png"
    character_path.write_bytes(b"mira-image")

    pipeline = FMVAgentPipeline(api_key="dummy", video_model="veo-3.1-fast-generate-001")
    pipeline.client = _FakeClient(video_bytes=b"untrimmed-video-bytes")

    video_bytes = await pipeline._generate_google_video_clip(
        prompt="Animate the singer stepping forward as the city lights shimmer.",
        duration_seconds=8,
        image_path=str(storyboard_path),
        reference_assets=[
            VideoGenerationReferenceAsset(path=str(storyboard_path), label="Storyboard frame"),
            VideoGenerationReferenceAsset(path=str(character_path), label="Mira"),
        ],
    )

    assert video_bytes == b"untrimmed-video-bytes"
    assert len(pipeline.client.models.calls) == 1
    request = pipeline.client.models.calls[0]
    assert request["model"] == "veo-3.1-fast-generate-001"
    assert request["config"].duration_seconds == 8
    assert request["config"].generate_audio is True
    assert len(request["config"].reference_images) == 2
    assert request["source"].image is None
    assert len(pipeline.client.files.download_calls) == 1


@pytest.mark.asyncio
async def test_generate_google_video_clip_does_not_fall_back_to_second_attempt_when_ingredients_return_no_videos(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    storyboard_path = tmp_path / "storyboard.png"
    storyboard_path.write_bytes(b"storyboard-image")
    character_path = tmp_path / "mira.png"
    character_path.write_bytes(b"mira-image")

    pipeline = FMVAgentPipeline(api_key="dummy", video_model="veo-3.1-fast-generate-001")
    pipeline.client = _FakeClient(
        video_bytes=b"",
        response=[_FakeEmptyPayload()],
    )

    with pytest.raises(RuntimeError, match="returned no videos"):
        await pipeline._generate_google_video_clip(
            prompt="Animate the singer stepping forward as the city lights shimmer.",
            duration_seconds=6,
            image_path=str(storyboard_path),
            reference_assets=[
                VideoGenerationReferenceAsset(path=str(storyboard_path), label="Storyboard frame"),
                VideoGenerationReferenceAsset(path=str(character_path), label="Mira"),
            ],
        )

    assert len(pipeline.client.models.calls) == 1
    request = pipeline.client.models.calls[0]
    assert request["config"].duration_seconds == 8
    assert len(request["config"].reference_images) == 2
    assert request["source"].image is None
    assert len(pipeline.client.files.download_calls) == 0


@pytest.mark.asyncio
async def test_generate_google_video_clip_trims_ingredients_result_back_to_requested_duration(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    storyboard_path = tmp_path / "storyboard.png"
    storyboard_path.write_bytes(b"storyboard-image")
    character_path = tmp_path / "mira.png"
    character_path.write_bytes(b"mira-image")

    pipeline = FMVAgentPipeline(api_key="dummy", video_model="veo-3.1-fast-generate-001")
    pipeline.client = _FakeClient(video_bytes=b"untrimmed-video-bytes")

    async def fake_normalize_video_canvas(*, input_path, output_path, include_audio, duration, **kwargs):
        assert include_audio is True
        assert duration == 6.0
        Path(output_path).write_bytes(b"trimmed-video-bytes")

    pipeline._normalize_video_canvas = fake_normalize_video_canvas

    video_bytes = await pipeline._generate_google_video_clip(
        prompt="Animate the singer stepping forward as the city lights shimmer.",
        duration_seconds=6,
        image_path=str(storyboard_path),
        reference_assets=[
            VideoGenerationReferenceAsset(path=str(storyboard_path), label="Storyboard frame"),
            VideoGenerationReferenceAsset(path=str(character_path), label="Mira"),
        ],
    )

    assert video_bytes == b"trimmed-video-bytes"
    request = pipeline.client.models.calls[0]
    assert request["config"].duration_seconds == 8


@pytest.mark.asyncio
async def test_generate_google_video_clip_surfaces_operation_error_message(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    storyboard_path = tmp_path / "storyboard.png"
    storyboard_path.write_bytes(b"storyboard-image")
    character_path = tmp_path / "mira.png"
    character_path.write_bytes(b"mira-image")

    pipeline = FMVAgentPipeline(api_key="dummy", video_model="veo-3.1-fast-generate-001")
    pipeline.client = _FakeClient(
        video_bytes=b"",
        response=[(None, {"code": 3, "message": "Unsupported output video duration 6 seconds, supported durations are [8] for feature reference_to_video."})],
    )

    with pytest.raises(RuntimeError, match="Unsupported output video duration 6 seconds"):
        await pipeline._generate_google_video_clip(
            prompt="Animate the singer stepping forward as the city lights shimmer.",
            duration_seconds=6,
            image_path=str(storyboard_path),
            reference_assets=[
                VideoGenerationReferenceAsset(path=str(storyboard_path), label="Storyboard frame"),
                VideoGenerationReferenceAsset(path=str(character_path), label="Mira"),
            ],
        )


def test_google_video_operation_timeout_seconds_scales_for_heavier_jobs():
    pipeline = FMVAgentPipeline(api_key="dummy", video_resolution="720p")

    fast_timeout = pipeline._google_video_operation_timeout_seconds(
        model_name="veo-3.1-fast-generate-001",
        duration_seconds=4,
        uses_ingredients_mode=False,
        reference_asset_count=0,
    )
    quality_timeout = pipeline._google_video_operation_timeout_seconds(
        model_name="veo-3.1-generate-001",
        duration_seconds=8,
        uses_ingredients_mode=True,
        reference_asset_count=3,
    )

    pipeline.video_resolution = "4k"
    four_k_timeout = pipeline._google_video_operation_timeout_seconds(
        model_name="veo-3.1-fast-generate-001",
        duration_seconds=6,
        uses_ingredients_mode=False,
        reference_asset_count=0,
    )

    assert fast_timeout == 600
    assert quality_timeout > fast_timeout
    assert four_k_timeout > fast_timeout


@pytest.mark.asyncio
async def test_generate_google_video_clip_timeout_message_includes_generation_context(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    storyboard_path = tmp_path / "storyboard.png"
    storyboard_path.write_bytes(b"storyboard-image")
    character_path = tmp_path / "mira.png"
    character_path.write_bytes(b"mira-image")

    pending_operation = SimpleNamespace(
        done=False,
        name="operations/slow-veo",
        response=None,
        error=None,
    )
    captured_calls = []

    class _PendingModels:
        def generate_videos(self, **kwargs):
            captured_calls.append(kwargs)
            return pending_operation

    class _PendingOperations:
        def get(self, operation, *, config=None):
            return operation

    pipeline = FMVAgentPipeline(api_key="dummy", video_model="veo-3.1-fast-generate-001")
    pipeline.client = SimpleNamespace(
        models=_PendingModels(),
        files=_FakeFiles(b""),
        operations=_PendingOperations(),
    )
    monkeypatch.setattr(
        pipeline,
        "_google_video_operation_timeout_seconds",
        lambda **kwargs: 10,
    )
    fake_time = [0.0]
    monkeypatch.setattr(graph_module.time, "monotonic", lambda: fake_time[0])

    async def fake_sleep(_seconds):
        fake_time[0] += _seconds
        return None

    monkeypatch.setattr(graph_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(
        TimeoutError,
        match=r"timed out after 10s \(model=veo-3\.1-fast-generate-001, mode=ingredients, refs=2, duration=6s, resolution=1080p\)",
    ):
        await pipeline._generate_google_video_clip(
            prompt="Animate the singer stepping forward as the city lights shimmer.",
            duration_seconds=6,
            image_path=str(storyboard_path),
            reference_assets=[
                VideoGenerationReferenceAsset(path=str(storyboard_path), label="Storyboard frame"),
                VideoGenerationReferenceAsset(path=str(character_path), label="Mira"),
            ],
        )


@pytest.mark.asyncio
async def test_generate_google_video_clip_invokes_heartbeat_while_polling(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    storyboard_path = tmp_path / "storyboard.png"
    storyboard_path.write_bytes(b"storyboard-image")

    pending_operation = SimpleNamespace(
        done=False,
        name="operations/slow-veo",
        response=None,
        error=None,
    )

    class _PendingModels:
        def generate_videos(self, **kwargs):
            return pending_operation

    class _PendingOperations:
        def __init__(self):
            self.calls = 0

        def get(self, operation, *, config=None):
            self.calls += 1
            if self.calls >= 3:
                operation.done = True
                operation.response = _FakePayload()
            return operation

    fake_time = [0.0]
    heartbeat_calls: list[float] = []

    async def fake_sleep(seconds):
        fake_time[0] += seconds
        return None

    pipeline = FMVAgentPipeline(api_key="dummy", video_model="veo-3.1-fast-generate-001")
    pipeline.client = SimpleNamespace(
        models=_PendingModels(),
        files=_FakeFiles(b"fake-video-bytes"),
        operations=_PendingOperations(),
    )
    monkeypatch.setattr(graph_module.time, "monotonic", lambda: fake_time[0])
    monkeypatch.setattr(graph_module.asyncio, "sleep", fake_sleep)

    video_bytes = await pipeline._generate_google_video_clip(
        prompt="Animate this scene.",
        duration_seconds=6,
        image_path=str(storyboard_path),
        heartbeat_callback=lambda: heartbeat_calls.append(fake_time[0]),
    )

    assert video_bytes == b"fake-video-bytes"
    assert heartbeat_calls == [0.0, 5.0, 10.0]


def test_reconcile_production_timeline_syncs_whole_clip_fragment_durations_to_latest_storyboard_timing():
    pipeline = FMVAgentPipeline(api_key=None)

    state = ProjectState(
        project_id="proj_production_sync",
        name="Production Sync",
        current_stage=AgentStage.PRODUCTION,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=8.0,
                storyboard_text="Opening shot",
                video_url="/projects/clip_0.mp4",
                video_approved=True,
            ),
            VideoClip(
                id="clip_1",
                timeline_start=8.0,
                duration=4.0,
                storyboard_text="Follow shot",
                video_url="/projects/clip_1.mp4",
                video_approved=True,
            ),
        ],
        production_timeline=[
            ProductionTimelineFragment(
                id="clip_1_frag_0",
                source_clip_id="clip_1",
                timeline_start=0.0,
                source_start=0.0,
                duration=6.0,
                audio_enabled=True,
            ),
            ProductionTimelineFragment(
                id="clip_0_frag_0",
                source_clip_id="clip_0",
                timeline_start=6.0,
                source_start=0.0,
                duration=4.0,
                audio_enabled=True,
            ),
        ],
    )

    pipeline._reconcile_production_timeline(state)

    assert [fragment.source_clip_id for fragment in state.production_timeline] == ["clip_1", "clip_0"]
    assert [fragment.duration for fragment in state.production_timeline] == [4.0, 8.0]
    assert [fragment.timeline_start for fragment in state.production_timeline] == [0.0, 4.0]


def test_initialize_production_timeline_uses_music_start_offset():
    pipeline = FMVAgentPipeline(api_key=None)

    state = ProjectState(
        project_id="proj_music_offset",
        name="Music Offset",
        current_stage=AgentStage.PRODUCTION,
        music_url="/projects/proj_music_offset_music.wav",
        music_duration_seconds=12.0,
        music_start_seconds=4.0,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Opening shot",
            ),
            VideoClip(
                id="clip_1",
                timeline_start=6.0,
                duration=6.0,
                storyboard_text="Second shot",
            ),
        ],
    )

    pipeline._initialize_production_timeline(state)

    music_fragments = [fragment for fragment in state.production_timeline if (fragment.track_type or "video") == "music"]
    assert len(music_fragments) == 1
    assert music_fragments[0].timeline_start == 4.0
    assert music_fragments[0].duration == 12.0


def test_get_project_backfills_music_duration_seconds(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    state = ProjectState(
        project_id="proj_music_duration_backfill",
        name="Music Duration Backfill",
        current_stage=AgentStage.PLANNING,
        music_url="/projects/proj_music_duration_backfill_music.wav",
        music_duration_seconds=None,
    )
    (projects_dir / "proj_music_duration_backfill.fmv").write_text(state.model_dump_json())

    monkeypatch.setattr(endpoints_module, "_measure_audio_duration_seconds_sync", lambda music_url: 42.5)

    loaded = endpoints_module.get_project("proj_music_duration_backfill")
    persisted = endpoints_module.get_project("proj_music_duration_backfill")

    assert loaded.music_duration_seconds == 42.5
    assert persisted.music_duration_seconds == 42.5


def test_reconcile_production_timeline_preserves_music_duration_beyond_picture():
    pipeline = FMVAgentPipeline(api_key=None)

    state = ProjectState(
        project_id="proj_music_reconcile",
        name="Music Reconcile",
        current_stage=AgentStage.PRODUCTION,
        music_url="/projects/proj_music_reconcile_music.wav",
        music_start_seconds=4.0,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Opening shot",
            ),
            VideoClip(
                id="clip_1",
                timeline_start=6.0,
                duration=6.0,
                storyboard_text="Second shot",
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
            ),
            ProductionTimelineFragment(
                id="clip_1_frag_0",
                source_clip_id="clip_1",
                timeline_start=6.0,
                source_start=0.0,
                duration=6.0,
                audio_enabled=True,
            ),
            ProductionTimelineFragment(
                id="music_frag_0",
                track_type="music",
                source_clip_id=None,
                timeline_start=4.0,
                source_start=0.0,
                duration=12.0,
                audio_enabled=True,
            ),
        ],
    )

    pipeline._reconcile_production_timeline(state)

    music_fragments = [fragment for fragment in state.production_timeline if (fragment.track_type or "video") == "music"]
    assert len(music_fragments) == 1
    assert music_fragments[0].timeline_start == 4.0
    assert music_fragments[0].duration == 12.0


@pytest.mark.asyncio
async def test_node_filming_does_not_retry_with_adjusted_prompt_after_generation_failure(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)
    storyboard_path = projects_dir / "proj_retry_clip_0.png"
    storyboard_path.write_bytes(b"fake-image")

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(models=SimpleNamespace())
    pipeline._probe_video_dimensions = lambda video_path: asyncio.sleep(0, result=(1920, 1080))

    prompts = []

    async def fake_generate_video_clip(
        *,
        prompt,
        duration_seconds,
        image_path,
        reference_assets=None,
        job_started_callback=None,
        heartbeat_callback=None,
    ):
        prompts.append(prompt)
        raise RuntimeError("Google Veo returned no videos")

    async def fake_critique(**kwargs):
        return {"score": 8, "reasoning": "ok", "suggestions": ""}

    pipeline._generate_video_clip = fake_generate_video_clip
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

    assert result.timeline[0].video_url is None
    assert result.timeline[0].video_approved is False
    assert len(prompts) == 1
    assert prompts[0].startswith("Intense macro visualization")
    assert "returned no videos" in result.timeline[0].video_critiques[-1]


def test_is_timeout_error_detects_wrapped_timeout():
    wrapped = RuntimeError("Retried with adjusted phrasing, but Veo still failed")
    wrapped.__cause__ = TimeoutError("Google Veo job timed out after 600s")

    assert _is_timeout_error(wrapped) is True


@pytest.mark.asyncio
async def test_node_filming_persists_video_job_tracking_for_timed_out_clip(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)
    storyboard_path = projects_dir / "proj_timeout_clip_0.png"
    storyboard_path.write_bytes(b"fake-image")

    persisted_states = []

    async def persist_state(state):
        persisted_states.append(state.model_copy(deep=True))

    pipeline = FMVAgentPipeline(api_key="dummy", persist_state_callback=persist_state)
    pipeline.client = SimpleNamespace(models=SimpleNamespace())

    async def fake_generate_video_clip(
        *,
        prompt,
        duration_seconds,
        image_path,
        reference_assets=None,
        job_started_callback=None,
        heartbeat_callback=None,
    ):
        if job_started_callback is not None:
            await job_started_callback(
                "operations/slow-veo",
                "gs://test-bucket/generated-video/slow-job/",
                "veo-3.1-fast-generate-001",
            )
        raise TimeoutError("Google Veo job timed out after 600s")

    pipeline._generate_video_clip = fake_generate_video_clip

    state = ProjectState(
        project_id="proj_timeout",
        name="Timed Out Filming",
        current_stage=AgentStage.STORYBOARDING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=6.0,
                storyboard_text="A lighthouse in fog at dusk.",
                image_url="/projects/proj_timeout_clip_0.png",
                image_approved=True,
            )
        ],
    )

    result = await pipeline.node_filming(state)

    assert result.timeline[0].video_url is None
    assert result.timeline[0].video_approved is False
    assert result.timeline[0].video_generation_operation_name == "operations/slow-veo"
    assert result.timeline[0].video_generation_output_gcs_uri == "gs://test-bucket/generated-video/slow-job/"
    assert result.timeline[0].video_generation_model == "veo-3.1-fast-generate-001"
    assert any(saved.timeline[0].video_generation_operation_name == "operations/slow-veo" for saved in persisted_states)


@pytest.mark.asyncio
async def test_node_filming_harvests_completed_video_from_timed_out_job(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)
    storyboard_path = projects_dir / "proj_harvest_clip_0.png"
    storyboard_path.write_bytes(b"fake-image")

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(models=SimpleNamespace())
    pipeline._download_first_gcs_prefix_bytes = lambda prefix_uri: b"harvested-video-bytes"
    pipeline._probe_video_dimensions = lambda video_path: asyncio.sleep(0, result=(1920, 1080))

    async def fake_generate_video_clip(*, prompt, duration_seconds, image_path, reference_assets=None, job_started_callback=None):
        raise AssertionError("Harvested clips should not start a new Veo generation")

    async def fake_critique_video_frame(*, video_path, video_prompt, duration):
        return {"score": 9, "reasoning": "Recovered clip looks clean."}

    pipeline._generate_video_clip = fake_generate_video_clip
    pipeline._critique_video_frame = fake_critique_video_frame

    state = ProjectState(
        project_id="proj_harvest",
        name="Harvest Timed Out Video",
        current_stage=AgentStage.FILMING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0,
                duration=6.0,
                storyboard_text="A lighthouse in fog at dusk.",
                image_url="/projects/proj_harvest_clip_0.png",
                image_approved=True,
                video_prompt="A gentle push-in through the fog toward the lighthouse.",
                video_generation_operation_name="operations/slow-veo",
                video_generation_output_gcs_uri="gs://test-bucket/generated-video/slow-job/",
                video_generation_model="veo-3.1-fast-generate-001",
                video_generation_started_at="2026-03-15T00:00:00+00:00",
            )
        ],
    )

    result = await pipeline.node_filming(state)

    assert result.timeline[0].video_url is not None
    assert result.timeline[0].video_approved is True
    assert result.timeline[0].video_generation_operation_name is None
    assert result.timeline[0].video_generation_output_gcs_uri is None
    assert result.timeline[0].video_generation_model is None
    assert result.timeline[0].video_generation_started_at is None
    assert any("Recovered completed Veo output" in critique for critique in result.timeline[0].video_critiques)


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
    assert "instrumental-only" in calls[0]["contents"][0]
    assert "Do not include vocal style instructions" in calls[0]["contents"][0]


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
async def test_generate_google_lyria_realtime_track_adapts_prompt_for_instrumental_provider(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_storage_roots(monkeypatch, tmp_path)

    calls = []

    pipeline = FMVAgentPipeline(api_key="dummy", music_model="lyria-realtime-exp")
    pipeline.uses_vertex_ai = True
    pipeline.client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda **kwargs: calls.append(kwargs) or SimpleNamespace(
                text="Dark synthwave instrumental, pulsing analog bass, glassy arpeggios, nocturnal tension, cinematic rise"
            )
        )
    )

    captured_prompt = {}

    def _fake_generate_vertex_audio(prompt: str):
        captured_prompt["value"] = prompt
        return (b"RIFFfake", "audio/wav")

    pipeline._generate_vertex_lyria_track_bytes = _fake_generate_vertex_audio

    state = ProjectState(
        project_id="proj_music_adapt",
        name="Music Adapt Test",
        current_stage=AgentStage.LYRIA_PROMPTING,
        screenplay="A lonely singer crosses a neon city.",
        instructions="Cinematic and moody.",
        lyrics_prompt="City lights call out your name.",
        style_prompt="Synthwave ballad with soaring female vocals and a huge chorus.",
    )

    result = await pipeline._generate_google_lyria_realtime_track(state)

    assert result is not None
    assert captured_prompt["value"] == (
        "Dark synthwave instrumental, pulsing analog bass, glassy arpeggios, nocturnal tension, cinematic rise"
    )
    assert "instrumental-only music generation model" in calls[0]["contents"][0]
    assert "Use lyrics only as narrative context" in calls[0]["contents"][0]


def test_generate_vertex_lyria_track_bytes_accepts_bytes_base64_encoded(monkeypatch):
    import urllib.request

    pipeline = FMVAgentPipeline(api_key="dummy", music_model="lyria-realtime-exp")

    class _FakeCredentials:
        token = "test-token"

        def refresh(self, _request):
            return None

    google_module = ModuleType("google")
    google_auth_module = ModuleType("google.auth")
    google_auth_module.default = lambda scopes=None: (_FakeCredentials(), None)
    google_module.auth = google_auth_module

    google_auth_transport_module = ModuleType("google.auth.transport")
    google_auth_transport_requests_module = ModuleType("google.auth.transport.requests")
    google_auth_transport_requests_module.Request = lambda: object()
    google_auth_transport_module.requests = google_auth_transport_requests_module

    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.auth", google_auth_module)
    monkeypatch.setitem(sys.modules, "google.auth.transport", google_auth_transport_module)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", google_auth_transport_requests_module)
    monkeypatch.setattr(graph_module, "get_gcp_project", lambda: "proj-test")
    monkeypatch.setattr(graph_module, "get_vertex_media_location", lambda: "us-central1")

    response_body = {
        "predictions": [
            {
                "bytesBase64Encoded": "UklGRgAAAAA=",
            }
        ]
    }

    class _FakeHTTPResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(response_body).encode("utf-8")

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=300: _FakeHTTPResponse())

    music_bytes, mime_type = pipeline._generate_vertex_lyria_track_bytes("test prompt")

    assert music_bytes == b"RIFF\x00\x00\x00\x00"
    assert mime_type == "audio/wav"


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


def test_build_stage_summary_text_uses_whole_seconds_for_speech():
    pipeline = FMVAgentPipeline(api_key=None)
    state = ProjectState(
        project_id="proj_summary_rounding",
        name="Summary Rounding Test",
        current_stage=AgentStage.PLANNING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=4.0,
                storyboard_text="Opening shot",
            ),
            VideoClip(
                id="clip_1",
                timeline_start=4.0,
                duration=4.0,
                storyboard_text="Middle shot",
            ),
        ],
    )

    summary_text = pipeline._build_stage_summary_text(state, AgentStage.PLANNING)

    assert "8 seconds" in summary_text
    assert "8.0 seconds" not in summary_text


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
async def test_run_pipeline_retries_once_after_resource_exhausted_storyboarding_failure():
    pipeline = FMVAgentPipeline(api_key=None)

    attempts = {"count": 0}
    refreshed_stages: list[AgentStage] = []

    async def fake_node_storyboarding(state):
        attempts["count"] += 1
        state.current_stage = AgentStage.STORYBOARDING
        if attempts["count"] == 1:
            raise RuntimeError("429 RESOURCE_EXHAUSTED. Try later.")
        return state

    async def fake_update_stage_summary(state, stage):
        refreshed_stages.append(stage)

    async def fake_sleep(_seconds):
        return None

    pipeline.node_storyboarding = fake_node_storyboarding
    pipeline._update_stage_summary = fake_update_stage_summary
    pipeline._sleep_with_cancellation_checks = fake_sleep

    state = ProjectState(
        project_id="proj_retry_storyboard",
        name="Retry Storyboard",
        current_stage=AgentStage.PLANNING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=4.0,
                storyboard_text="Retry shot",
            )
        ],
    )

    result = await pipeline.run_pipeline(state)

    assert attempts["count"] == 2
    assert result.current_stage == AgentStage.STORYBOARDING
    assert result.last_error is None
    assert refreshed_stages == [AgentStage.STORYBOARDING]


@pytest.mark.asyncio
async def test_run_pipeline_clears_completed_summary_when_production_fails():
    pipeline = FMVAgentPipeline(api_key=None)
    refreshed = {"called": False}

    async def fake_update_stage_summary(state, stage):
        refreshed["called"] = True

    async def fake_node_production(state):
        state.current_stage = AgentStage.PRODUCTION
        state.last_error = "ffmpeg failed while building the production sequence"
        state.final_video_url = None
        return state

    pipeline._update_stage_summary = fake_update_stage_summary
    pipeline.node_production = fake_node_production

    state = ProjectState(
        project_id="proj_failed_render_summary",
        name="Failed Render Summary",
        current_stage=AgentStage.PRODUCTION,
        final_video_url="/projects/previous_final.mp4",
        stage_summaries={
            "production": StageSummary(
                text="Production is ready.",
                generated_at="2026-03-07T00:00:00+00:00",
            ),
            "completed": StageSummary(
                text="The final render is ready.",
                generated_at="2026-03-07T00:10:00+00:00",
            ),
        },
    )

    result = await pipeline.run_pipeline(state)

    assert result.current_stage == AgentStage.PRODUCTION
    assert "completed" not in result.stage_summaries
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

    assert result.timeline[0].video_approved is True
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
    assert len(subprocess_calls) == 7
    assert str(clip_path) in subprocess_calls[0]
    assert str(projects_dir / "proj_test_clip_0_frag_0_video.mp4") in subprocess_calls[0]
    assert str(projects_dir / "proj_test_clip_0_frag_0_audio.m4a") in subprocess_calls[1]
    assert str(projects_dir / "proj_test_clip_0_frag_0_segment.mp4") in subprocess_calls[2]
    assert str(projects_dir / "proj_test_sequence.mp4") in subprocess_calls[3]
    assert _local_media_path(state.music_url) in subprocess_calls[4]
    assert str(projects_dir / "proj_test_music_bed.m4a") in subprocess_calls[5]
    assert str(projects_dir / "proj_test_final.mp4") in subprocess_calls[6]


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
async def test_regenerate_storyboard_clip_endpoint_rerenders_target_clip_immediately(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    class _FakePipeline:
        def __init__(self, *args, **kwargs):
            self.persist_state_callback = kwargs.get("persist_state_callback")

        async def _persist_state(self, state):
            if self.persist_state_callback:
                result = self.persist_state_callback(state)
                if asyncio.iscoroutine(result):
                    await result

        async def _ensure_generated_character_assets(self, state):
            return None

        def _build_storyboard_asset_context(self, state):
            return [], {}, {}

        async def _build_storyboard_relevance_map(self, clips, *, image_assets, screenplay=None):
            return {}

        async def _select_relevant_previous_shots(self, clip, previous_clips):
            return []

        async def _process_storyboard_clip(self, *, state, clip, relevant_assets, previous_shots, asset_bytes, asset_lookup):
            clip.image_prompt = "Regenerated storyboard prompt"
            clip.image_url = f"/projects/{state.project_id}_{clip.id}.png"
            clip.image_score = 9
            clip.image_reference_ready = True
            clip.image_approved = True
            clip.image_critiques = ["[Attempt 1] Score: 9/10 — Strong regenerated frame."]

    monkeypatch.setattr(endpoints_module, "FMVAgentPipeline", _FakePipeline)

    state = ProjectState(
        project_id="proj_storyboard_regen",
        name="Storyboard Regen",
        current_stage=AgentStage.FILMING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Opening shot",
                image_url="/projects/proj_storyboard_regen_clip_0_old.png",
                image_prompt="Old prompt",
                image_approved=True,
                image_score=7,
                image_reference_ready=True,
                video_url="/projects/proj_storyboard_regen_clip_0.mp4",
                video_prompt="Old video prompt",
                video_approved=True,
            ),
            VideoClip(
                id="clip_1",
                timeline_start=6.0,
                duration=6.0,
                storyboard_text="Second shot",
                image_url="/projects/proj_storyboard_regen_clip_1.png",
                image_prompt="Second prompt",
                image_approved=True,
                image_score=8,
                image_reference_ready=True,
                video_url="/projects/proj_storyboard_regen_clip_1.mp4",
                video_prompt="Second video prompt",
                video_approved=True,
            ),
        ],
        final_video_url="/projects/proj_storyboard_regen_final.mp4",
        stage_summaries={
            "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
            "storyboarding": StageSummary(text="Storyboarding", generated_at="2026-03-07T00:01:00+00:00"),
            "filming": StageSummary(text="Filming", generated_at="2026-03-07T00:02:00+00:00"),
        },
    )
    (projects_dir / "proj_storyboard_regen.fmv").write_text(state.model_dump_json())

    response = await endpoints_module.regenerate_storyboard_clip(
        "proj_storyboard_regen",
        "clip_0",
    )

    assert response.current_stage == AgentStage.STORYBOARDING
    assert response.timeline[0].image_url == "/projects/proj_storyboard_regen_clip_0.png"
    assert response.timeline[0].image_prompt == "Regenerated storyboard prompt"
    assert response.timeline[0].image_approved is True
    assert response.timeline[0].video_url is None
    assert response.timeline[0].video_prompt is None
    assert response.timeline[1].image_url == "/projects/proj_storyboard_regen_clip_1.png"
    assert response.timeline[1].video_url == "/projects/proj_storyboard_regen_clip_1.mp4"
    assert response.final_video_url is None
    assert set(response.stage_summaries.keys()) == {"planning"}

    persisted = endpoints_module.get_project("proj_storyboard_regen")
    assert persisted.timeline[0].image_url == "/projects/proj_storyboard_regen_clip_0.png"
    assert persisted.timeline[0].video_url is None
    assert persisted.current_stage == AgentStage.STORYBOARDING


def test_upload_storyboard_frame_endpoint_replaces_target_clip_immediately(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    state = ProjectState(
        project_id="proj_storyboard_upload",
        name="Storyboard Upload",
        current_stage=AgentStage.FILMING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Opening shot",
                image_url="/projects/proj_storyboard_upload_clip_0_old.png",
                image_prompt="Old prompt",
                image_approved=True,
                image_score=7,
                image_reference_ready=True,
                image_critiques=["Old critique"],
                video_url="/projects/proj_storyboard_upload_clip_0.mp4",
                video_prompt="Old video prompt",
                video_critiques=["Old video critique"],
                video_score=8,
                video_approved=True,
            ),
            VideoClip(
                id="clip_1",
                timeline_start=6.0,
                duration=6.0,
                storyboard_text="Second shot",
                image_url="/projects/proj_storyboard_upload_clip_1.png",
                image_prompt="Second prompt",
                image_approved=True,
                image_score=8,
                image_reference_ready=True,
                video_url="/projects/proj_storyboard_upload_clip_1.mp4",
                video_prompt="Second video prompt",
                video_approved=True,
            ),
        ],
        final_video_url="/projects/proj_storyboard_upload_final.mp4",
        stage_summaries={
            "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
            "storyboarding": StageSummary(text="Storyboarding", generated_at="2026-03-07T00:01:00+00:00"),
            "filming": StageSummary(text="Filming", generated_at="2026-03-07T00:02:00+00:00"),
        },
    )
    (projects_dir / "proj_storyboard_upload.fmv").write_text(state.model_dump_json())

    response = endpoints_module.upload_storyboard_frame(
        "proj_storyboard_upload",
        "clip_0",
        endpoints_module.StoryboardFrameUploadRequest(
            url="/projects/uploads/custom_frame.png",
            name="custom_frame.png",
        ),
    )

    assert response.current_stage == AgentStage.STORYBOARDING
    assert response.timeline[0].image_url == "/projects/uploads/custom_frame.png"
    assert response.timeline[0].image_prompt is None
    assert response.timeline[0].image_score is None
    assert response.timeline[0].image_reference_ready is True
    assert response.timeline[0].image_approved is True
    assert response.timeline[0].image_manual_override is True
    assert response.timeline[0].image_critiques[-1] == "Manual storyboard frame uploaded: custom_frame.png"
    assert response.timeline[0].video_url is None
    assert response.timeline[0].video_prompt is None
    assert response.timeline[0].video_critiques == []
    assert response.timeline[0].video_score is None
    assert response.timeline[0].video_approved is None
    assert response.timeline[1].image_url == "/projects/proj_storyboard_upload_clip_1.png"
    assert response.timeline[1].video_url == "/projects/proj_storyboard_upload_clip_1.mp4"
    assert response.final_video_url is None
    assert set(response.stage_summaries.keys()) == {"planning"}

    persisted = endpoints_module.get_project("proj_storyboard_upload")
    assert persisted.timeline[0].image_url == "/projects/uploads/custom_frame.png"
    assert persisted.timeline[0].video_url is None
    assert persisted.current_stage == AgentStage.STORYBOARDING


def test_update_project_preserves_manual_storyboard_frame_on_description_edit(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    previous = ProjectState(
        project_id="proj_manual_storyboard_preserve",
        name="Manual Storyboard Preserve",
        current_stage=AgentStage.STORYBOARDING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Original shot description",
                image_url="/projects/uploads/manual_frame.png",
                image_prompt=None,
                image_critiques=["Manual storyboard frame uploaded: manual_frame.png"],
                image_approved=True,
                image_score=None,
                image_reference_ready=True,
                image_manual_override=True,
                video_url="/projects/proj_manual_storyboard_preserve_clip_0.mp4",
                video_prompt="Old video prompt",
                video_critiques=["Old video critique"],
                video_score=8,
                video_approved=True,
            )
        ],
        final_video_url="/projects/proj_manual_storyboard_preserve_final.mp4",
        stage_summaries={
            "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
            "storyboarding": StageSummary(text="Storyboarding", generated_at="2026-03-07T00:01:00+00:00"),
            "filming": StageSummary(text="Filming", generated_at="2026-03-07T00:02:00+00:00"),
        },
    )
    (projects_dir / "proj_manual_storyboard_preserve.fmv").write_text(previous.model_dump_json())

    edited = previous.model_copy(deep=True)
    edited.timeline[0].storyboard_text = "Updated shot description with more detail"

    response = endpoints_module.update_project("proj_manual_storyboard_preserve", edited)

    assert response.timeline[0].image_url == "/projects/uploads/manual_frame.png"
    assert response.timeline[0].image_approved is True
    assert response.timeline[0].image_reference_ready is True
    assert response.timeline[0].image_manual_override is True
    assert response.timeline[0].video_url is None
    assert response.timeline[0].video_prompt is None
    assert response.timeline[0].video_approved is False
    assert response.final_video_url is None


def test_update_storyboard_clip_text_endpoint_reconciles_outputs(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    state = ProjectState(
        project_id="proj_storyboard_text_update",
        name="Storyboard Text Update",
        current_stage=AgentStage.FILMING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Opening shot",
                image_url="/projects/proj_storyboard_text_update_clip_0.png",
                image_prompt="Old prompt",
                image_approved=True,
                image_score=8,
                image_reference_ready=True,
                video_url="/projects/proj_storyboard_text_update_clip_0.mp4",
                video_prompt="Old video prompt",
                video_approved=True,
            ),
            VideoClip(
                id="clip_1",
                timeline_start=6.0,
                duration=6.0,
                storyboard_text="Second shot",
                image_url="/projects/proj_storyboard_text_update_clip_1.png",
                image_prompt="Second prompt",
                image_approved=True,
                image_score=8,
                image_reference_ready=True,
                video_url="/projects/proj_storyboard_text_update_clip_1.mp4",
                video_prompt="Second video prompt",
                video_approved=True,
            ),
        ],
        final_video_url="/projects/proj_storyboard_text_update_final.mp4",
        stage_summaries={
            "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
            "storyboarding": StageSummary(text="Storyboarding", generated_at="2026-03-07T00:01:00+00:00"),
            "filming": StageSummary(text="Filming", generated_at="2026-03-07T00:02:00+00:00"),
        },
    )
    (projects_dir / "proj_storyboard_text_update.fmv").write_text(state.model_dump_json())

    response = endpoints_module.update_storyboard_clip_text(
        "proj_storyboard_text_update",
        "clip_0",
        endpoints_module.StoryboardTextUpdateRequest(
            storyboard_text="Updated opening shot with a stronger silhouette.",
        ),
    )

    assert response.current_stage == AgentStage.STORYBOARDING
    assert response.timeline[0].storyboard_text == "Updated opening shot with a stronger silhouette."
    assert response.timeline[0].image_url is None
    assert response.timeline[0].video_url is None
    assert response.timeline[1].image_url == "/projects/proj_storyboard_text_update_clip_1.png"
    assert response.timeline[1].video_url == "/projects/proj_storyboard_text_update_clip_1.mp4"
    assert response.final_video_url is None
    assert response.stage_summaries == {}


def test_update_asset_label_endpoint_does_not_touch_storyboard_frame(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    state = ProjectState(
        project_id="proj_asset_label_update",
        name="Asset Label Update",
        current_stage=AgentStage.STORYBOARDING,
        assets=[
            MediaAsset(
                id="asset_0",
                url="/projects/uploads/manual_frame.png",
                type="image",
                name="manual_frame.png",
                label="Old Label",
            )
        ],
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Shot",
                image_url="/projects/uploads/manual_frame.png",
                image_critiques=["Manual storyboard frame uploaded: manual_frame.png"],
                image_approved=True,
                image_reference_ready=True,
                image_manual_override=True,
            )
        ],
    )
    (projects_dir / "proj_asset_label_update.fmv").write_text(state.model_dump_json())

    response = endpoints_module.update_asset_label(
        "proj_asset_label_update",
        "asset_0",
        endpoints_module.AssetLabelUpdateRequest(label="New Label"),
    )

    assert response.assets[0].label == "New Label"
    assert response.timeline[0].image_url == "/projects/uploads/manual_frame.png"
    assert response.timeline[0].image_manual_override is True


@pytest.mark.asyncio
async def test_live_director_endpoint_can_advance_into_async_storyboarding(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)
    job_queue_module.LOCAL_PIPELINE_TASKS.clear()
    monkeypatch.setenv("FMV_JOB_DRIVER", "local")

    started = asyncio.Event()
    finish = asyncio.Event()

    class _FakePipeline:
        def __init__(self, *args, **kwargs):
            self.persist_state_callback = kwargs.get("persist_state_callback")

        async def handle_live_director_mode(self, state, **kwargs):
            return state.model_copy(deep=True), {
                "reply_text": "Moving into storyboarding now.",
                "applied_changes": ["Proceeding to storyboarding."],
                "target_clip_id": None,
                "target_fragment_id": None,
                "stage": state.current_stage.value,
                "navigation_action": "advance",
                "target_stage": None,
            }

        async def run_pipeline(self, state):
            started.set()
            await finish.wait()
            state.timeline[0].image_url = "/projects/proj_live_director_stage_nav_clip_0.png"
            state.timeline[0].image_approved = True
            state.current_stage = AgentStage.STORYBOARDING
            if self.persist_state_callback:
                await self.persist_state_callback(state)
            return state

    monkeypatch.setattr(endpoints_module, "FMVAgentPipeline", _FakePipeline)

    state = ProjectState(
        project_id="proj_live_director_stage_nav",
        name="Live Director Stage Nav",
        current_stage=AgentStage.PLANNING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Opening shot",
            )
        ],
    )
    (projects_dir / "proj_live_director_stage_nav.fmv").write_text(state.model_dump_json())

    response = await endpoints_module.live_director_mode(
        "proj_live_director_stage_nav",
        endpoints_module.LiveDirectorRequest(
            message="Go to the next stage.",
            display_stage=AgentStage.PLANNING,
        ),
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    assert response.project.current_stage == AgentStage.STORYBOARDING
    assert response.project.active_run is not None
    assert response.project.active_run.stage == AgentStage.STORYBOARDING

    status = endpoints_module.get_project_run_status("proj_live_director_stage_nav")
    assert status.is_running is True
    assert status.stage == AgentStage.STORYBOARDING

    finish.set()
    await asyncio.sleep(0.05)

    final_state = endpoints_module.get_project("proj_live_director_stage_nav")
    assert final_state.timeline[0].image_url == "/projects/proj_live_director_stage_nav_clip_0.png"
    assert final_state.active_run is None


@pytest.mark.asyncio
async def test_live_director_endpoint_can_rewind_to_previous_display_stage(monkeypatch, tmp_path):
    projects_dir = _patch_storage_roots(monkeypatch, tmp_path)

    class _FakePipeline:
        def __init__(self, *args, **kwargs):
            pass

        async def handle_live_director_mode(self, state, **kwargs):
            return state.model_copy(deep=True), {
                "reply_text": "Going back one stage for another pass.",
                "applied_changes": ["Returned to the previous stage."],
                "target_clip_id": None,
                "target_fragment_id": None,
                "stage": state.current_stage.value,
                "navigation_action": "rewind",
                "target_stage": None,
            }

    monkeypatch.setattr(endpoints_module, "FMVAgentPipeline", _FakePipeline)

    state = ProjectState(
        project_id="proj_live_director_rewind",
        name="Live Director Rewind",
        current_stage=AgentStage.FILMING,
        timeline=[
            VideoClip(
                id="clip_0",
                timeline_start=0.0,
                duration=6.0,
                storyboard_text="Opening shot",
                image_url="/projects/proj_live_director_rewind_clip_0.png",
                image_prompt="opening frame",
                image_critiques=["Solid continuity."],
                image_approved=True,
                video_url="/projects/proj_live_director_rewind_clip_0.mp4",
                video_prompt="opening render",
                video_critiques=["Rendered cleanly."],
                video_approved=True,
            )
        ],
        final_video_url="/projects/proj_live_director_rewind_final.mp4",
        stage_summaries={
            "planning": StageSummary(text="Planning", generated_at="2026-03-07T00:00:00+00:00"),
            "storyboarding": StageSummary(text="Storyboarding", generated_at="2026-03-07T00:01:00+00:00"),
            "filming": StageSummary(text="Filming", generated_at="2026-03-07T00:02:00+00:00"),
        },
    )
    (projects_dir / "proj_live_director_rewind.fmv").write_text(state.model_dump_json())

    response = await endpoints_module.live_director_mode(
        "proj_live_director_rewind",
        endpoints_module.LiveDirectorRequest(
            message="Go back one stage.",
            display_stage=AgentStage.STORYBOARDING,
        ),
    )

    assert response.project.current_stage == AgentStage.PLANNING
    assert response.project.timeline[0].image_url is None
    assert response.project.timeline[0].video_url is None
    assert response.project.final_video_url is None
    assert set(response.project.stage_summaries.keys()) == {"planning"}


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
    assert len(subprocess_calls) == 7
    assert "anullsrc=r=48000:cl=stereo" in subprocess_calls[1]
    assert str(clip_path) not in subprocess_calls[1]
    assert str(projects_dir / "proj_test_clip_0_frag_0_audio.m4a") in subprocess_calls[1]
    assert _local_media_path(state.music_url) in subprocess_calls[4]


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

    async def fake_build_asset_relevance_map(image_assets, clips, *, screenplay):
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
    pipeline._build_storyboard_image_prompt = (
        lambda **kwargs: asyncio.sleep(0, result="A figure stands under a flickering streetlight in rain.")
    )

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


def test_cache_busted_project_url_uses_unique_high_resolution_timestamp(monkeypatch):
    pipeline = FMVAgentPipeline(api_key=None)
    timestamps = iter([111_111_111, 111_111_112])
    monkeypatch.setattr(graph_module.time, "time_ns", lambda: next(timestamps))

    first_url = pipeline._cache_busted_project_url("/projects/frame.png")
    second_url = pipeline._cache_busted_project_url("/projects/frame.png")

    assert first_url == "/projects/frame.png?t=111111111"
    assert second_url == "/projects/frame.png?t=111111112"
    assert first_url != second_url


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


@pytest.mark.asyncio
async def test_critique_image_runs_three_critic_lenses_concurrently(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _patch_storage_roots(monkeypatch, tmp_path)

    pipeline = FMVAgentPipeline(api_key="dummy")
    pipeline.client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kwargs: None))
    monkeypatch.setattr(pipeline, "_build_image_critic_contents", lambda **kwargs: ["frame"])

    started = []

    async def fake_single_pass(**kwargs):
        started.append(kwargs["reviewer_lens"])
        await asyncio.sleep(0.05)
        return {
            "score": 8,
            "passes": True,
            "reasoning": "Looks good.",
            "suggestions": "None.",
            "hard_fail_findings": [],
        }

    monkeypatch.setattr(pipeline, "_single_image_critic_pass", fake_single_pass)

    started_at = asyncio.get_running_loop().time()
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
    elapsed = asyncio.get_running_loop().time() - started_at

    assert critique["passes"] is True
    assert len(started) == 3
    assert elapsed < 0.12


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
