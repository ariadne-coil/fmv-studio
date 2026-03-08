"""
FMV Studio — Live API Integration Test
=======================================
Tests the full pipeline end-to-end using real Gemini API calls.

By providing a pre-generated 25-second audio clip we skip Lyria 3 generation
entirely, keeping the test cheap while still exercising:
  1. Gemini 3.1 Pro  → Timeline planning (node_planning)
  2. NanoBanana 2    → Storyboard frame generation (node_storyboarding)
  3. Veo 3.1         → Video clip generation (node_filming)
  4. ffmpeg          → Clip concatenation with audio mix (node_production)

Setup
-----
1.  Copy .env.example → .env and fill in your GEMINI_API_KEY:
        echo "GEMINI_API_KEY=<your_key>" > .env

2.  Install deps (if not done):
        .\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt

3.  Run from the repo root:
        .\.venv\Scripts\python.exe -m pytest tests/backend/test_pipeline_live.py -v -s

Cost-saving strategy
--------------------
- Short, 2-scene screenplay (<= 2 clips of ~5s each)
- music_url is pre-set → skips Lyria 3 generation
- Veo "fast" quality mode
"""

import asyncio
import os
import sys
import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

from app.agent.graph import FMVAgentPipeline, _local_media_path
from app.agent.models import ProjectState, AgentStage

# ── Constants ────────────────────────────────────────────────────────────────

TEST_AUDIO = str(REPO_ROOT / "tests" / "fixtures" / "test_audio.mp3")
TEST_PROJECT_ID = "test_live_integration"

# Free-tier compatible model for planning (gemini-3.1-pro requires paid tier)
# Switch these to gemini-3.1-pro-preview when a paid API key is available
TEST_PLANNING_MODEL = "gemini-2.0-flash"

SHORT_SCREENPLAY = """\
A lone lighthouse stands on a rocky cliff at dusk, its beam cutting through the fog.
Two seagulls circle overhead before landing on the railing.
"""

SHORT_INSTRUCTIONS = "Cinematic, moody, desaturated color palette, slow camera pan."


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_state(stage: AgentStage, **kwargs) -> ProjectState:
    return ProjectState(
        project_id=TEST_PROJECT_ID,
        name="Live Integration Test",
        current_stage=stage,
        screenplay=SHORT_SCREENPLAY,
        instructions=SHORT_INSTRUCTIONS,
        music_url=TEST_AUDIO,   # Pre-set to skip Lyria 3
        veo_quality="fast",
        **kwargs,
    )


# ── Tests ────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def api_key():
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        pytest.skip("GEMINI_API_KEY not set — skipping live tests")
    return key


@pytest.fixture(scope="module")
def pipeline(api_key):
    return FMVAgentPipeline(api_key=api_key)


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# ── Stage 1: Planning ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_node_planning(pipeline):
    """Gemini 3.1 Pro breaks the screenplay into a clip timeline."""
    state = make_state(AgentStage.INPUT)
    result = await pipeline.node_planning(state)

    assert result.current_stage == AgentStage.PLANNING, \
        f"Expected PLANNING, got {result.current_stage}"
    assert len(result.timeline) >= 1, "Expected at least 1 clip in the timeline"
    assert len(result.timeline) <= 10, "Too many clips for a short screenplay"

    for clip in result.timeline:
        assert clip.storyboard_text, f"Clip {clip.id} has no storyboard text"
        assert 0.0 < clip.duration <= 8.0, \
            f"Clip {clip.id} duration {clip.duration}s should be ≤ 8s"

    print(f"\n✅ Planning: {len(result.timeline)} clips generated")
    for i, clip in enumerate(result.timeline):
        print(f"   Clip {i+1} ({clip.duration:.1f}s): {clip.storyboard_text[:80]}...")

    return result  # passed to downstream fixtures via `planning_state`


# ── Stage 2: Storyboarding ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_node_storyboarding(pipeline):
    """NanoBanana 2 generates storyboard images for each clip."""
    # First get the planning output
    plan_state = make_state(AgentStage.INPUT)
    plan_state = await pipeline.node_planning(plan_state)

    result = await pipeline.node_storyboarding(plan_state)

    assert result.current_stage == AgentStage.STORYBOARDING

    for clip in result.timeline:
        assert clip.image_url, f"Clip {clip.id} has no image_url after storyboarding"
        # If saved locally, file should exist
        local_path = _local_media_path(clip.image_url)
        if local_path and not local_path.startswith("http"):
            assert os.path.exists(local_path), \
                f"Image file not found on disk: {local_path}"

    print(f"\n✅ Storyboarding: images generated for {len(result.timeline)} clips")
    for clip in result.timeline:
        print(f"   {clip.id}: {clip.image_url}")

    return result


# ── Stage 3: Filming ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_node_filming(pipeline):
    """Veo 3.1 animates each storyboard image into a short video clip."""
    # Get storyboard output first
    plan_state = make_state(AgentStage.INPUT)
    plan_state = await pipeline.node_planning(plan_state)
    story_state = await pipeline.node_storyboarding(plan_state)

    print(f"\n⏳ Submitting {len(story_state.timeline)} clips to Veo 3.1 (async)...")
    result = await pipeline.node_filming(story_state)

    assert result.current_stage == AgentStage.FILMING

    for clip in result.timeline:
        assert clip.video_url, f"Clip {clip.id} has no video_url after filming"
        print(f"   {clip.id}: {clip.video_url}")
        local_video_path = _local_media_path(clip.video_url)
        if local_video_path and not local_video_path.startswith("http"):
            assert os.path.exists(local_video_path), \
                f"Video file not found on disk: {local_video_path}"
        if clip.video_critiques:
            print(f"   ⚠️  Critiques: {clip.video_critiques}")

    print(f"✅ Filming: videos generated for {len(result.timeline)} clips")
    return result


# ── Stage 4: Full end-to-end ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_pipeline_end_to_end(pipeline):
    """
    Full run: INPUT → PLANNING → STORYBOARDING → FILMING → PRODUCTION.
    Checks that a final_video_url is set and the file exists on disk.
    """
    state = make_state(AgentStage.INPUT)

    print("\n=== Phase 1: Planning ===")
    state = await pipeline.node_planning(state)
    assert state.current_stage == AgentStage.PLANNING
    print(f"   {len(state.timeline)} clips planned")

    print("=== Phase 2: Storyboarding ===")
    state = await pipeline.node_storyboarding(state)
    assert state.current_stage == AgentStage.STORYBOARDING
    imgs = sum(1 for c in state.timeline if c.image_url)
    print(f"   {imgs}/{len(state.timeline)} images generated")

    # Auto-approve all storyboard frames
    for clip in state.timeline:
        clip.image_approved = True

    print("=== Phase 3: Filming ===")
    state = await pipeline.node_filming(state)
    assert state.current_stage == AgentStage.FILMING
    vids = sum(1 for c in state.timeline if c.video_url)
    print(f"   {vids}/{len(state.timeline)} videos generated")

    # Auto-approve all video clips
    for clip in state.timeline:
        clip.video_approved = True

    print("=== Phase 4: Production (ffmpeg stitch + audio mix) ===")
    state = await pipeline.node_production(state)
    assert state.current_stage == AgentStage.COMPLETED, \
        f"Expected COMPLETED, got {state.current_stage}. Error: {state.last_error}"

    assert state.final_video_url, "No final_video_url set after production"
    print(f"\n✅ PIPELINE COMPLETE: {state.final_video_url}")

    local_final_path = _local_media_path(state.final_video_url)
    if local_final_path and not local_final_path.startswith("http"):
        assert os.path.exists(local_final_path), \
            f"Final video file not found on disk: {local_final_path}"
        size_kb = os.path.getsize(local_final_path) // 1024
        print(f"   File size: {size_kb} KB")

    if state.last_error:
        print(f"   ⚠️  Non-fatal error recorded: {state.last_error}")
