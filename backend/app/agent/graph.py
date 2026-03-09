import json
import mimetypes
import os
import re
import shutil
import asyncio
import base64
import inspect
import time
import tempfile
import uuid
import wave
from difflib import SequenceMatcher
from datetime import datetime, timezone
from typing import Callable, Dict, Any, List
from urllib.parse import urlsplit
from google import genai
from .models import AgentStage, DirectorTurn, ProductionTimelineFragment, ProjectState, StageSummary, VideoClip
from app.core.document_context import display_asset_label, normalize_document_text
from app.image.providers import (
    DEFAULT_IMAGE_PROVIDER,
    get_image_provider,
    resolve_image_provider_selection,
)
from app.music.providers import (
    DEFAULT_MUSIC_PROVIDER,
    get_music_provider,
    normalize_music_provider_id,
)
from app.video.providers import (
    DEFAULT_VIDEO_PROVIDER,
    get_video_provider,
    resolve_video_provider_selection,
)
from app.genai_runtime import (
    build_genai_client,
    get_gcp_project,
    get_vertex_media_location,
    uses_vertex_ai,
)
from app.paths import PROJECTS_DIR, REPO_ROOT
from app.storage import get_storage_backend

def _find_ffmpeg() -> str:
    """Return the path to ffmpeg, trying PATH first then known WinGet install location."""
    # shutil.which checks the current process PATH
    found = shutil.which("ffmpeg")
    if found:
        return found
    # Fallback: WinGet install location on Windows
    winget_path = os.path.expanduser(
        r"~\AppData\Local\Microsoft\WinGet\Packages"
    )
    if os.path.isdir(winget_path):
        for root, _, files in os.walk(winget_path):
            if "ffmpeg.exe" in files:
                return os.path.join(root, "ffmpeg.exe")
    return "ffmpeg"  # let it fail at runtime with a clear error if not found


def _find_ffprobe(ffmpeg_path: str) -> str:
    found = shutil.which("ffprobe")
    if found:
        return found

    sibling_candidates = []
    if ffmpeg_path.endswith("ffmpeg.exe"):
        sibling_candidates.append(ffmpeg_path[:-10] + "ffprobe.exe")
    elif ffmpeg_path.endswith("ffmpeg"):
        sibling_candidates.append(ffmpeg_path[:-6] + "ffprobe")

    for candidate in sibling_candidates:
        if os.path.exists(candidate):
            return candidate

    winget_path = os.path.expanduser(r"~\AppData\Local\Microsoft\WinGet\Packages")
    if os.path.isdir(winget_path):
        for root, _, files in os.walk(winget_path):
            if "ffprobe.exe" in files:
                return os.path.join(root, "ffprobe.exe")

    return "ffprobe"

FFMPEG = _find_ffmpeg()
FFPROBE = _find_ffprobe(FFMPEG)
VALID_VEO_DURATIONS = (4, 6, 8)
TARGET_IMAGE_ASPECT_RATIO = "16:9"
TARGET_VIDEO_ASPECT_RATIO = "16:9"
DEFAULT_TARGET_IMAGE_SIZE = "4K"
DEFAULT_TARGET_VIDEO_RESOLUTION = "1080p"
IMAGE_SIZE_DIMENSIONS = {
    "1K": (1024, 576),
    "2K": (2048, 1152),
    "4K": (3840, 2160),
}
VIDEO_RESOLUTION_DIMENSIONS = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "4k": (3840, 2160),
}
STORYBOARD_PASS_SCORE = 8
STORYBOARD_MAX_ATTEMPTS = 5
STORYBOARD_MAX_REFERENCE_SHOTS = 6
LYRIA_PCM_SAMPLE_RATE = 48000
LYRIA_PCM_CHANNELS = 2
LYRIA_PCM_SAMPLE_WIDTH = 2
DEFAULT_LYRIA_SONG_SECONDS = 150.0
MIN_LYRIA_SONG_SECONDS = 90.0
MAX_LYRIA_SONG_SECONDS = 240.0
STAGE_SUMMARY_STAGES = {
    AgentStage.PLANNING,
    AgentStage.STORYBOARDING,
    AgentStage.FILMING,
    AgentStage.PRODUCTION,
    AgentStage.COMPLETED,
}
DEFAULT_ORCHESTRATOR_MODEL = "gemini-3-pro-preview"
DEFAULT_CRITIC_MODEL = "gemini-3-flash-preview"
LEGACY_TEXT_MODEL_ALIASES = {
    "gemini-3.1-pro-preview": "gemini-3-pro-preview",
}

LIVE_DIRECTOR_CREATIVE_FIELDS = {
    "screenplay",
    "instructions",
    "additional_lore",
    "lyrics_prompt",
    "style_prompt",
    "storyboard_text",
    "video_prompt",
}


def _normalize_director_similarity_text(text: str) -> str:
    lowered = re.sub(r"[^a-z0-9\s]+", " ", text.lower())
    return re.sub(r"\s+", " ", lowered).strip()


def _looks_like_literal_director_text(requested_message: str, candidate_text: str) -> bool:
    requested = _normalize_director_similarity_text(requested_message)
    candidate = _normalize_director_similarity_text(candidate_text)
    if not requested or not candidate:
        return False

    requested_words = requested.split()
    candidate_words = candidate.split()
    if not requested_words or not candidate_words:
        return False

    if candidate == requested:
        return True

    if candidate in requested or requested in candidate:
        if len(candidate_words) <= len(requested_words) + 10:
            return True

    similarity = SequenceMatcher(None, requested, candidate).ratio()
    if similarity >= 0.84 and len(candidate_words) <= len(requested_words) + 12:
        return True

    requested_vocab = set(requested_words)
    candidate_vocab = set(candidate_words)
    overlap_ratio = len(requested_vocab & candidate_vocab) / max(1, len(requested_vocab))
    if overlap_ratio >= 0.9 and len(candidate_words) <= len(requested_words) + 8:
        return True

    return False


def _looks_like_windows_abs_path(path: str) -> bool:
    return len(path) >= 3 and path[1] == ":" and path[2] in ("\\", "/")


def _normalize_text_model_name(model_name: str | None, default: str) -> str:
    normalized = (model_name or "").strip()
    if not normalized:
        return default
    return LEGACY_TEXT_MODEL_ALIASES.get(normalized, normalized)


def _normalize_image_size_name(image_size: str | None) -> str:
    normalized = (image_size or "").strip().upper()
    if normalized in IMAGE_SIZE_DIMENSIONS:
        return normalized
    return DEFAULT_TARGET_IMAGE_SIZE


def _normalize_video_resolution_name(video_resolution: str | None) -> str:
    normalized = (video_resolution or "").strip().lower()
    if normalized in VIDEO_RESOLUTION_DIMENSIONS:
        return normalized
    return DEFAULT_TARGET_VIDEO_RESOLUTION


def _local_media_path(path_or_url: str | None) -> str | None:
    """Convert a persisted media URL into a local filesystem path when possible."""
    if not path_or_url:
        return None
    resolved = get_storage_backend().resolve_project_asset_to_local_path(path_or_url)
    if resolved:
        return resolved

    cleaned = str(path_or_url).split("?", 1)[0].split("#", 1)[0].replace("\\", "/")
    if cleaned.startswith(("http://", "https://")):
        cleaned = urlsplit(cleaned).path

    if os.path.isabs(cleaned) or _looks_like_windows_abs_path(cleaned):
        return cleaned

    return str((REPO_ROOT / cleaned.lstrip("/")).resolve())


def _normalize_relevant_assets(raw_assets: Any) -> list[dict[str, str]]:
    """Best-effort normalization for Gemini's asset-routing output."""
    if isinstance(raw_assets, dict):
        raw_assets = [raw_assets]
    if not isinstance(raw_assets, list):
        return []

    normalized: list[dict[str, str]] = []
    for asset in raw_assets:
        if isinstance(asset, str):
            normalized.append({"id": asset, "type": "subject"})
            continue
        if not isinstance(asset, dict):
            continue

        asset_id = asset.get("id")
        if not isinstance(asset_id, str) or not asset_id:
            continue

        asset_type = asset.get("type")
        if asset_type not in {"subject", "background"}:
            asset_type = "subject"

        normalized.append({"id": asset_id, "type": asset_type})

    return normalized


_SPECULATIVE_CRITIQUE_SNIPPETS = (
    "maybe",
    "might",
    "possibly",
    "perhaps",
    "seems",
    "appears to",
    "unclear",
    "hard to tell",
    "looks off",
    "looks odd",
)
_SUBJECTIVE_BODY_LABELS = (
    "body dysmorphia",
    "dysmorphia",
)
_ANATOMY_HARD_FAIL_SNIPPETS = (
    "anatom",
    "limb",
    "arm",
    "hand",
    "finger",
    "leg",
    "foot",
    "feet",
    "head",
    "face",
    "torso",
    "body",
    "neck",
    "shoulder",
    "elbow",
    "wrist",
    "knee",
    "ankle",
    "fused body",
    "merged bod",
    "detached",
)
_SUBJECT_COUNT_HARD_FAIL_SNIPPETS = (
    "wrong number of people",
    "wrong number of subjects",
    "extra people",
    "extra person",
    "too many people",
    "too many subjects",
    "missing person",
    "missing people",
    "missing subject",
    "duplicate person",
    "duplicated person",
    "duplicate subject",
    "duplicated subject",
)
_MIN_CONFIDENT_VISUAL_HARD_FAIL = 0.85
CRITIC_PANEL_SIZE = 3
IMAGE_CRITIC_LENSES = (
    "Independent reviewer A. Prioritize literal prompt faithfulness and visible continuity. Reject speculative defect claims.",
    "Independent reviewer B. Prioritize anatomy, subject count, and object integrity, but only when defects are directly visible.",
    "Independent reviewer C. Prioritize technical realism, composition, and whether alleged defects are actually observable on screen.",
)
VIDEO_CRITIC_LENSES = (
    "Independent reviewer A. Prioritize scene faithfulness and visible continuity over time.",
    "Independent reviewer B. Prioritize temporal stability, artifact detection, and whether alleged defects are directly visible in the sampled frames.",
    "Independent reviewer C. Prioritize cinematic quality, motion readability, and skepticism toward speculative failure claims.",
)


def _contains_speculative_critique_language(text: str) -> bool:
    lowered = text.lower()
    return any(snippet in lowered for snippet in _SPECULATIVE_CRITIQUE_SNIPPETS)


def _contains_subjective_body_label(text: str) -> bool:
    lowered = text.lower()
    return any(label in lowered for label in _SUBJECTIVE_BODY_LABELS)


def _is_anatomy_hard_fail_reason(reason: str, category: str | None = None) -> bool:
    lowered_reason = reason.lower()
    lowered_category = (category or "").lower()
    if lowered_category == "anatomy":
        return True
    return any(snippet in lowered_reason for snippet in _ANATOMY_HARD_FAIL_SNIPPETS)


def _is_subject_count_hard_fail_reason(reason: str, category: str | None = None) -> bool:
    lowered_reason = reason.lower()
    lowered_category = (category or "").lower()
    if lowered_category == "subject_count":
        return True
    return any(snippet in lowered_reason for snippet in _SUBJECT_COUNT_HARD_FAIL_SNIPPETS)


def _infer_hard_fail_category(reason: str) -> str:
    if _is_anatomy_hard_fail_reason(reason):
        return "anatomy"
    if _is_subject_count_hard_fail_reason(reason):
        return "subject_count"

    lowered_reason = reason.lower()
    if any(snippet in lowered_reason for snippet in ("continuity", "identity", "character mismatch")):
        return "continuity"
    if any(snippet in lowered_reason for snippet in ("scene", "location", "room", "background", "environment")):
        return "scene"
    if any(snippet in lowered_reason for snippet in ("prop", "creature", "bird", "object")):
        return "object_integrity"
    return "other"


def _requires_unambiguous_visual_evidence(reason: str, category: str | None = None) -> bool:
    return _is_anatomy_hard_fail_reason(reason, category) or _is_subject_count_hard_fail_reason(reason, category)


def _median_rounded_score(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return int(ordered[mid])
    return int(round((ordered[mid - 1] + ordered[mid]) / 2))


def _canonicalize_consensus_finding_key(reason: str, category: str | None = None) -> str:
    normalized_category = (category or _infer_hard_fail_category(reason)).strip().lower() or "other"
    lowered = reason.lower()

    if normalized_category == "anatomy":
        if any(snippet in lowered for snippet in ("extra limb", "extra arm", "extra leg", "third arm", "third leg", "too many arm", "too many leg", "extra hand", "extra finger")):
            return "anatomy:extra_limbs"
        if any(snippet in lowered for snippet in ("missing limb", "missing arm", "missing leg", "missing hand", "missing finger")):
            return "anatomy:missing_limbs"
        if any(snippet in lowered for snippet in ("missing head", "missing face", "extra head", "duplicated head", "merged bod", "fused bod", "detached")):
            return "anatomy:head_body_integrity"
        return "anatomy:other"

    if normalized_category == "subject_count":
        return "subject_count:mismatch"

    if normalized_category == "continuity":
        if any(snippet in lowered for snippet in ("identity", "character", "subject mismatch", "does not match")):
            return "continuity:identity"
        return "continuity:other"

    if normalized_category == "scene":
        return "scene:continuity"

    if normalized_category == "object_integrity":
        return "object_integrity:broken_object"

    if normalized_category == "artifact":
        return "artifact:visual_artifacts"

    if normalized_category == "temporal_consistency":
        return "temporal:instability"

    if normalized_category == "scene_faithfulness":
        return "scene_faithfulness:mismatch"

    if normalized_category == "cinematic_quality":
        return "cinematic_quality:degraded"

    if normalized_category == "audio_music":
        return "audio_music:contamination"

    return f"{normalized_category}:generic"


def _extract_hard_fail_findings(raw_critique: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    raw_findings = raw_critique.get("hard_fail_findings", [])
    if isinstance(raw_findings, list):
        for finding in raw_findings:
            if isinstance(finding, str):
                finding = {"reason": finding}
            if not isinstance(finding, dict):
                continue

            reason = str(finding.get("reason") or "").strip()
            if not reason:
                continue

            category = str(finding.get("category") or "").strip() or _infer_hard_fail_category(reason)
            evidence = str(finding.get("evidence") or "").strip()
            confidence_raw = finding.get("confidence")
            try:
                confidence = float(confidence_raw) if confidence_raw is not None else None
            except (TypeError, ValueError):
                confidence = None

            findings.append(
                {
                    "reason": reason,
                    "category": category,
                    "evidence": evidence,
                    "confidence": confidence,
                    "source": "structured",
                }
            )

    raw_fail_reasons = raw_critique.get("hard_fail_reasons", [])
    if isinstance(raw_fail_reasons, str):
        raw_fail_reasons = [raw_fail_reasons]
    if not isinstance(raw_fail_reasons, list):
        raw_fail_reasons = []

    existing_reason_keys = {finding["reason"].strip().lower() for finding in findings}
    for reason in raw_fail_reasons:
        normalized_reason = str(reason).strip()
        if not normalized_reason:
            continue
        reason_key = normalized_reason.lower()
        if reason_key in existing_reason_keys:
            continue
        findings.append(
            {
                "reason": normalized_reason,
                "category": _infer_hard_fail_category(normalized_reason),
                "evidence": "",
                "confidence": None,
                "source": "legacy",
            }
        )
        existing_reason_keys.add(reason_key)

    return findings


def _normalize_structured_hard_fail_findings(raw_critique: dict[str, Any]) -> list[dict[str, Any]]:
    normalized_findings: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for finding in _extract_hard_fail_findings(raw_critique):
        reason = finding["reason"]
        category = finding["category"]
        evidence = finding["evidence"]
        confidence = finding["confidence"]
        source = finding["source"]

        if _contains_subjective_body_label(reason):
            continue
        if _contains_speculative_critique_language(reason):
            continue

        if _requires_unambiguous_visual_evidence(reason, category):
            if source == "structured":
                if confidence is not None and confidence < _MIN_CONFIDENT_VISUAL_HARD_FAIL:
                    continue
                if evidence and _contains_speculative_critique_language(evidence):
                    continue

        canonical_key = _canonicalize_consensus_finding_key(reason, category)
        if canonical_key in seen_keys:
            continue
        seen_keys.add(canonical_key)
        normalized_findings.append(
            {
                "reason": reason,
                "category": category,
                "evidence": evidence,
                "confidence": confidence,
                "canonical_key": canonical_key,
            }
        )

    return normalized_findings


def _build_panel_consensus_critique(
    panel_critiques: list[dict[str, Any]],
    *,
    medium_label: str,
    pass_score: int,
) -> dict[str, Any]:
    score_values: list[int] = []
    normalized_panel: list[dict[str, Any]] = []

    for critique in panel_critiques:
        if not isinstance(critique, dict):
            critique = {}

        try:
            score = int(float(critique.get("score", 0)))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(10, score))
        score_values.append(score)
        normalized_panel.append(
            {
                "score": score,
                "reasoning": str(critique.get("reasoning") or "").strip(),
                "suggestions": str(critique.get("suggestions") or "").strip(),
                "findings": _normalize_structured_hard_fail_findings(critique),
            }
        )

    vote_map: dict[str, list[dict[str, Any]]] = {}
    for panel_item in normalized_panel:
        for finding in panel_item["findings"]:
            vote_map.setdefault(finding["canonical_key"], []).append(finding)

    consensus_findings: list[dict[str, Any]] = []
    for canonical_key, votes in vote_map.items():
        if len(votes) < CRITIC_PANEL_SIZE:
            continue
        consensus_findings.append(
            max(
                votes,
                key=lambda finding: (
                    finding["confidence"] if finding["confidence"] is not None else 0.0,
                    len(finding["evidence"]),
                ),
            )
        )

    consensus_findings.sort(key=lambda finding: (finding["category"], finding["reason"]))
    consensus_score = _median_rounded_score(score_values)
    unanimous_pass = not consensus_findings

    if unanimous_pass:
        isolated_findings = sorted({
            finding["reason"]
            for panel_item in normalized_panel
            for finding in panel_item["findings"]
        })
        if isolated_findings:
            reasoning = (
                f"The 3 {medium_label} critics did not reach unanimous agreement on any blocking issue. "
                "Isolated complaints were treated as advisory only."
            )
            suggestions = "Review isolated notes manually, but do not treat them as automatic failures."
        else:
            reasoning = f"All 3 {medium_label} critics cleared this pass without a unanimous blocking concern."
            suggestions = "No blocking issue reached unanimous consensus."
    else:
        concern_list = ", ".join(finding["reason"] for finding in consensus_findings)
        reasoning = f"All 3 {medium_label} critics independently flagged the same blocking concern(s): {concern_list}."
        unique_suggestions = [
            panel_item["suggestions"]
            for panel_item in normalized_panel
            if panel_item["suggestions"]
        ]
        suggestions = unique_suggestions[0] if unique_suggestions else "Regenerate this shot and address the unanimous panel concern."
        consensus_score = min(consensus_score, pass_score - 1)

    return {
        "score": consensus_score,
        "passes": unanimous_pass,
        "reasoning": reasoning,
        "suggestions": suggestions,
        "hard_fail_findings": [
            {
                "reason": finding["reason"],
                "category": finding["category"],
                "confidence": finding["confidence"],
                "evidence": finding["evidence"],
            }
            for finding in consensus_findings
        ],
        "panel_scores": score_values,
    }


def _normalize_hard_fail_reasons(raw_critique: dict[str, Any]) -> list[str]:
    normalized_reasons: list[str] = []

    for finding in _normalize_structured_hard_fail_findings(raw_critique):
        if finding["reason"] not in normalized_reasons:
            normalized_reasons.append(finding["reason"])

    return normalized_reasons


def _normalize_image_critique(raw_critique: Any) -> dict[str, Any]:
    if not isinstance(raw_critique, dict):
        raw_critique = {}

    try:
        score = int(float(raw_critique.get("score", 0)))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(10, score))

    reasoning = str(raw_critique.get("reasoning") or "Critique unavailable.")
    suggestions = str(
        raw_critique.get("suggestions")
        or "Correct anatomy, subject count, object integrity, and continuity with the provided references."
    )

    hard_fail_reasons = _normalize_hard_fail_reasons(raw_critique)

    model_passes = raw_critique.get("passes")
    passes = score >= STORYBOARD_PASS_SCORE and not hard_fail_reasons
    if model_passes is True and not hard_fail_reasons:
        passes = True
    elif model_passes is False and hard_fail_reasons:
        passes = False

    return {
        "score": score,
        "passes": passes,
        "reasoning": reasoning,
        "suggestions": suggestions,
        "hard_fail_reasons": hard_fail_reasons,
    }


def _normalize_music_prompt_payload(raw_payload: Any) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        raw_payload = {}

    primary_prompt = str(raw_payload.get("primary_prompt") or "").strip()
    if not primary_prompt:
        primary_prompt = "Cinematic instrumental score with a clear melodic arc and strong stylistic identity."

    raw_accent_prompts = raw_payload.get("accent_prompts", [])
    if isinstance(raw_accent_prompts, str):
        raw_accent_prompts = [raw_accent_prompts]
    if not isinstance(raw_accent_prompts, list):
        raw_accent_prompts = []

    accent_prompts = [
        str(prompt).strip()
        for prompt in raw_accent_prompts
        if str(prompt).strip()
    ][:3]

    return {
        "primary_prompt": primary_prompt,
        "accent_prompts": accent_prompts,
    }


def _closest_valid_veo_duration(duration: float | int | None) -> int:
    value = float(duration or 6)
    # Prefer the longer duration on ties so 5 -> 6 and 7 -> 8.
    return min(VALID_VEO_DURATIONS, key=lambda allowed: (abs(allowed - value), -allowed))


def _normalize_veo_duration_sequence(
    durations: list[float],
    target_total: float | None = None,
) -> list[int]:
    if not durations:
        return []

    if target_total is None:
        return [_closest_valid_veo_duration(duration) for duration in durations]

    original_cumulative: list[float] = []
    running_original = 0.0
    for duration in durations:
        running_original += float(duration)
        original_cumulative.append(running_original)

    states: dict[int, tuple[float, list[int]]] = {0: (0.0, [])}

    for idx, duration in enumerate(durations):
        next_states: dict[int, tuple[float, list[int]]] = {}
        for running_total, (cost, chosen) in states.items():
            for allowed in VALID_VEO_DURATIONS:
                new_total = running_total + allowed
                new_cost = (
                    cost
                    + abs(allowed - float(duration)) * 10
                    + abs(new_total - original_cumulative[idx])
                )
                existing = next_states.get(new_total)
                candidate = (new_cost, chosen + [allowed])
                if existing is None or candidate[0] < existing[0]:
                    next_states[new_total] = candidate
        states = next_states

    best_total, (_, best_choice) = min(
        states.items(),
        key=lambda item: (
            abs(item[0] - target_total),
            item[1][0],
            item[0],
        ),
    )
    return best_choice

class FMVAgentPipeline:
    def __init__(
        self,
        api_key: str = None,
        orchestrator_model: str = None,
        critic_model: str = None,
        text_model: str = None,
        image_model: str = None,
        image_size: str = None,
        video_model: str = None,
        video_resolution: str = None,
        music_model: str = None,
        stage_voice_briefs_enabled: bool = True,
        persist_state_callback: Callable[[ProjectState], Any] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ):
        # Initialize Google GenAI client
        # In a real app, this would use the provided API key or fallback to env vars
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.orchestrator_model = _normalize_text_model_name(
            orchestrator_model or text_model,
            DEFAULT_ORCHESTRATOR_MODEL,
        )
        self.critic_model = _normalize_text_model_name(
            critic_model,
            DEFAULT_CRITIC_MODEL,
        )
        self.text_model = self.orchestrator_model
        self.image_provider_id, self.image_model = resolve_image_provider_selection(image_model or DEFAULT_IMAGE_PROVIDER)
        self.image_provider = get_image_provider(self.image_provider_id)
        self.image_aspect_ratio = TARGET_IMAGE_ASPECT_RATIO
        self.image_size = _normalize_image_size_name(image_size)
        self.image_width, self.image_height = IMAGE_SIZE_DIMENSIONS[self.image_size]
        self.video_provider_id, self.video_model = resolve_video_provider_selection(video_model or DEFAULT_VIDEO_PROVIDER)
        self.video_provider = get_video_provider(self.video_provider_id)
        self.video_aspect_ratio = TARGET_VIDEO_ASPECT_RATIO
        self.video_resolution = _normalize_video_resolution_name(video_resolution)
        self.video_width, self.video_height = VIDEO_RESOLUTION_DIMENSIONS[self.video_resolution]
        self.music_provider_id = normalize_music_provider_id(music_model or DEFAULT_MUSIC_PROVIDER)
        self.music_provider = get_music_provider(self.music_provider_id)
        self.music_model = self.music_provider.definition.default_model
        self.speech_model = os.getenv("FMV_TTS_MODEL", "gemini-2.5-flash-preview-tts")
        self.stage_brief_voice = os.getenv("FMV_STAGE_BRIEF_VOICE", "Kore")
        self.stage_voice_briefs_enabled = stage_voice_briefs_enabled
        self._persist_state_callback = persist_state_callback
        self._is_cancelled = is_cancelled or (lambda: False)
        self.uses_vertex_ai = uses_vertex_ai()
        
        self.client = build_genai_client(api_key=self.api_key)
        self.media_client = build_genai_client(api_key=self.api_key, media=True)
        self.music_client = (
            build_genai_client(
                api_key=self.api_key,
                api_version="v1alpha",
                media=True,
            )
            if not self.uses_vertex_ai
            else self.media_client
        )

    def _check_cancelled(self) -> None:
        if self._is_cancelled():
            raise asyncio.CancelledError("Pipeline run cancelled")

    async def _persist_state(self, state: ProjectState) -> None:
        self._check_cancelled()
        if not self._persist_state_callback:
            return

        persisted_state = state.model_copy(deep=True)
        result = self._persist_state_callback(persisted_state)
        if inspect.isawaitable(result):
            await result
        self._check_cancelled()

    def _stage_summary_generated_at(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _storage(self):
        return get_storage_backend()

    def _cache_busted_project_url(self, url: str) -> str:
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}t={int(time.time())}"

    def _write_project_asset_bytes(
        self,
        relative_path: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> str:
        url = self._storage().write_project_asset_bytes(
            relative_path,
            data,
            content_type=content_type,
        )
        return self._cache_busted_project_url(url)

    def _sync_local_project_artifact(
        self,
        local_path: str | os.PathLike[str],
        *,
        relative_path: str | None = None,
        content_type: str | None = None,
    ) -> str:
        url = self._storage().sync_local_project_asset(
            local_path,
            relative_path=relative_path,
            content_type=content_type,
        )
        return self._cache_busted_project_url(url)

    def _orchestrator_config(
        self,
        *,
        response_mime_type: str | None = None,
        thinking_budget: int | None = None,
        temperature: float | None = None,
    ) -> genai.types.GenerateContentConfig:
        config_kwargs: dict[str, Any] = {}
        if response_mime_type:
            config_kwargs["response_mime_type"] = response_mime_type
        if thinking_budget is not None:
            config_kwargs["thinking_config"] = genai.types.ThinkingConfig(thinking_budget=thinking_budget)
        if temperature is not None:
            config_kwargs["temperature"] = temperature
        return genai.types.GenerateContentConfig(**config_kwargs)

    def _critic_config(
        self,
        *,
        response_mime_type: str | None = None,
    ) -> genai.types.GenerateContentConfig:
        config_kwargs: dict[str, Any] = {
            "thinking_config": genai.types.ThinkingConfig(thinking_budget=0),
            "temperature": 0.1,
        }
        if response_mime_type:
            config_kwargs["response_mime_type"] = response_mime_type
        return genai.types.GenerateContentConfig(**config_kwargs)

    def _shot_lookup(self, state: ProjectState) -> dict[str, int]:
        return {
            clip.id: index + 1
            for index, clip in enumerate(sorted(state.timeline, key=lambda item: item.timeline_start))
        }

    def _shot_label(self, clip: VideoClip | None, shot_lookup: dict[str, int]) -> str:
        if not clip:
            return "this shot"
        shot_number = shot_lookup.get(clip.id)
        return f"shot {shot_number}" if shot_number is not None else clip.id

    def _document_context_text(self, state: ProjectState, *, max_chars: int = 6000) -> str:
        snippets: list[str] = []
        current_length = 0
        for asset in state.assets:
            if asset.type != "document" or not asset.text_content:
                continue
            snippet = (
                f"{display_asset_label(asset.label, asset.name)}:\n"
                f"{normalize_document_text(asset.text_content, max_chars=max_chars)}"
            )
            if current_length + len(snippet) > max_chars:
                remaining = max_chars - current_length
                if remaining <= 0:
                    break
                snippet = snippet[:remaining].rstrip() + "…"
            snippets.append(snippet)
            current_length += len(snippet)
            if current_length >= max_chars:
                break
        return "\n\n".join(snippets)

    def _asset_reference_registry_text(self, state: ProjectState, *, max_chars: int = 2000) -> str:
        entries: list[str] = []
        current_length = 0
        for asset in state.assets:
            label = display_asset_label(asset.label, asset.name)
            if asset.type == "image":
                entry = (
                    f'- image "{label}" (file: {asset.name}). '
                    f"If this label names a character, creature, hero prop, vehicle, or location from the screenplay, "
                    f"treat this image as the canonical visual reference for that named entity anywhere it appears."
                )
            elif asset.type == "document":
                entry = f'- document "{label}" (file: {asset.name}). Supplemental written context for the screenplay, lore, and world details.'
            elif asset.type == "audio":
                entry = f'- audio "{label}" (file: {asset.name}). Music or sound reference available to the project.'
            else:
                entry = f'- {asset.type} "{label}" (file: {asset.name}).'

            if current_length + len(entry) > max_chars:
                remaining = max_chars - current_length
                if remaining <= 0:
                    break
                entry = entry[:remaining].rstrip() + "…"
            entries.append(entry)
            current_length += len(entry)
            if current_length >= max_chars:
                break
        return "\n".join(entries) or "(none)"

    def _project_context_block(self, state: ProjectState, *, max_document_chars: int = 6000) -> str:
        document_context = self._document_context_text(state, max_chars=max_document_chars) or "(none)"
        asset_registry = self._asset_reference_registry_text(state, max_chars=2000)
        lore_text = state.additional_lore.strip() or "(none)"
        return (
            f"Additional Lore:\n{lore_text}\n\n"
            f"Uploaded Asset Registry:\n{asset_registry}\n\n"
            f"Uploaded Document Context:\n{document_context}"
        )

    def _music_production_fragments(self, state: ProjectState) -> list[ProductionTimelineFragment]:
        return [
            fragment
            for fragment in state.production_timeline
            if (fragment.track_type or "video") == "music"
        ]

    def _preserve_music_production_fragments(self, state: ProjectState) -> None:
        state.production_timeline = self._music_production_fragments(state) if state.music_url else []

    def _build_live_director_field_context(
        self,
        *,
        field_name: str,
        state: ProjectState,
        review_stage: AgentStage,
        target_clip: VideoClip | None,
        shot_lookup: dict[str, int],
    ) -> str:
        field_label_map = {
            "screenplay": "project screenplay",
            "instructions": "global visual instructions",
            "additional_lore": "world and character lore",
            "lyrics_prompt": "lyrics prompt",
            "style_prompt": "music style prompt",
            "storyboard_text": "single-shot storyboard frame description",
            "video_prompt": "single-shot motion and camera prompt",
        }
        context_lines = [
            f"Field being written: {field_label_map.get(field_name, field_name)}",
            f"Review stage: {review_stage.value}",
            f"Project screenplay: {state.screenplay}",
            f"Project instructions: {state.instructions}",
            f"Additional lore: {state.additional_lore}",
            f"Uploaded asset registry: {self._asset_reference_registry_text(state, max_chars=1200)}",
            f"Uploaded document context: {self._document_context_text(state, max_chars=2000) or '(none)'}",
            f"Lyrics prompt: {state.lyrics_prompt}",
            f"Style prompt: {state.style_prompt}",
        ]
        if target_clip:
            context_lines.extend(
                [
                    f"Target shot: {self._shot_label(target_clip, shot_lookup)}",
                    f"Target shot storyboard text: {target_clip.storyboard_text}",
                    f"Target shot video prompt: {target_clip.video_prompt}",
                    f"Target shot duration: {target_clip.duration}",
                ]
            )
        return "\n".join(context_lines)

    def _enrich_live_director_creative_text(
        self,
        *,
        field_name: str,
        requested_message: str,
        candidate_text: str,
        current_value: str,
        state: ProjectState,
        review_stage: AgentStage,
        target_clip: VideoClip | None,
        shot_lookup: dict[str, int],
    ) -> str:
        if field_name not in LIVE_DIRECTOR_CREATIVE_FIELDS:
            return candidate_text.strip()
        if not _looks_like_literal_director_text(requested_message, candidate_text):
            return candidate_text.strip()

        prompt = f"""
You are polishing a Live Director edit for FMV Studio.

The user instruction expresses creative intent, not the final text that should be stored in the project.
Rewrite the candidate update into finished, production-ready copy that preserves the requested meaning while adding concrete detail, atmosphere, specificity, and creative enhancement grounded in the project context.

Rules:
- Do not echo or lightly paraphrase the user instruction.
- Do not mention the user, editing actions, shot numbers, or revision language.
- Keep the meaning aligned with the request and the existing project context.
- Return ONLY the final field text. No JSON. No quotes. No markdown.

{self._build_live_director_field_context(
    field_name=field_name,
    state=state,
    review_stage=review_stage,
    target_clip=target_clip,
    shot_lookup=shot_lookup,
)}

Current field value:
{current_value}

Literal candidate update:
{candidate_text}

User instruction:
{requested_message}
"""

        response = self.client.models.generate_content(
            model=self.orchestrator_model,
            contents=[prompt],
            config=self._orchestrator_config(
                thinking_budget=1024,
                temperature=0.5,
            ),
        )
        enriched_text = str(response.text or "").strip()
        if not enriched_text:
            return candidate_text.strip()
        if _looks_like_literal_director_text(requested_message, enriched_text):
            return candidate_text.strip()
        return enriched_text

    def _resolve_director_shot_reference(self, state: ProjectState, message: str) -> tuple[int | None, str | None]:
        match = re.search(r"\b(?:shot|scene|clip|frame)\s+(\d{1,3})\b", message, flags=re.IGNORECASE)
        if not match:
            return None, None

        shot_number = int(match.group(1))
        if shot_number < 1:
            return None, None

        shot_lookup = self._shot_lookup(state)
        clip_id_by_shot_number = {
            clip_number: clip_id
            for clip_id, clip_number in shot_lookup.items()
        }
        return shot_number, clip_id_by_shot_number.get(shot_number)

    def _expand_director_shot_number_phrase(self, phrase: str) -> list[int]:
        normalized = phrase.lower()
        results: list[int] = []

        for start_text, end_text in re.findall(r"(\d{1,3})\s*(?:-|to|through)\s*(\d{1,3})", normalized):
            start = int(start_text)
            end = int(end_text)
            if start < 1 or end < 1:
                continue
            step = 1 if end >= start else -1
            for value in range(start, end + step, step):
                if value not in results:
                    results.append(value)

        normalized = re.sub(r"(\d{1,3})\s*(?:-|to|through)\s*(\d{1,3})", " ", normalized)
        for value_text in re.findall(r"\d{1,3}", normalized):
            value = int(value_text)
            if value >= 1 and value not in results:
                results.append(value)

        return results

    def _resolve_director_shot_references(self, state: ProjectState, message: str) -> list[tuple[int, str | None]]:
        shot_lookup = self._shot_lookup(state)
        clip_id_by_shot_number = {
            clip_number: clip_id
            for clip_id, clip_number in shot_lookup.items()
        }

        resolved_numbers: list[int] = []
        for match in re.finditer(r"\b(?:shot|scene|clip|frame)\s+(\d{1,3})\b", message, flags=re.IGNORECASE):
            shot_number = int(match.group(1))
            if shot_number >= 1 and shot_number not in resolved_numbers:
                resolved_numbers.append(shot_number)

        for match in re.finditer(
            r"\b(?:shots|scenes|clips|frames)\s+([\d,\sandthroughto-]+)\b",
            message,
            flags=re.IGNORECASE,
        ):
            for shot_number in self._expand_director_shot_number_phrase(match.group(1)):
                if shot_number not in resolved_numbers:
                    resolved_numbers.append(shot_number)

        return [
            (shot_number, clip_id_by_shot_number.get(shot_number))
            for shot_number in resolved_numbers
        ]

    def _resolve_director_fragment_reference_for_clip(self, state: ProjectState, clip_id: str | None) -> str | None:
        if not clip_id:
            return None

        matching_fragments = sorted(
            [
                fragment
                for fragment in state.production_timeline
                if (fragment.track_type or "video") != "music" and fragment.source_clip_id == clip_id
            ],
            key=lambda fragment: fragment.timeline_start,
        )
        if len(matching_fragments) == 1:
            return matching_fragments[0].id
        return None

    def _latest_review_note(self, critiques: list[str]) -> str:
        if not critiques:
            return ""

        note = critiques[-1]
        note = re.sub(r"^\[Attempt \d+\]\s*", "", note).strip()
        if "—" in note:
            note = note.split("—", 1)[1].strip()
        elif ":" in note and (
            note.lower().startswith("veo 3.1 generation failed")
            or note.lower().startswith("generation failed")
        ):
            note = note.split(":", 1)[1].strip()
        if " | " in note:
            note = note.split(" | ", 1)[0].strip()
        return note.rstrip(". ")

    def _append_director_turn(
        self,
        state: ProjectState,
        *,
        turn_id: str | None = None,
        role: str,
        text: str,
        audio_url: str | None = None,
        stage: AgentStage | str,
        source: str | None = None,
        applied_changes: list[str] | None = None,
    ) -> None:
        if not text.strip():
            return

        state.director_log.append(
            DirectorTurn(
                id=turn_id or f"director_{uuid.uuid4().hex}",
                role=role,
                text=text.strip(),
                audio_url=audio_url,
                stage=stage.value if isinstance(stage, AgentStage) else str(stage),
                created_at=datetime.now(timezone.utc).isoformat(),
                source=source,
                applied_changes=applied_changes or [],
            )
        )
        state.director_log = state.director_log[-24:]

    def _trim_stage_summaries_to_stage(self, state: ProjectState, max_stage: AgentStage | str) -> None:
        stage_value = max_stage.value if isinstance(max_stage, AgentStage) else str(max_stage)
        stage_order = [stage.value for stage in AgentStage if stage != AgentStage.HALTED_FOR_REVIEW]
        if stage_value not in stage_order:
            return
        max_index = stage_order.index(stage_value)
        state.stage_summaries = {
            stage_name: summary
            for stage_name, summary in state.stage_summaries.items()
            if stage_name in stage_order and stage_order.index(stage_name) <= max_index
        }

    def _clear_clip_storyboard_outputs(self, clip: VideoClip) -> None:
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

    def _clear_clip_video_outputs(self, clip: VideoClip, *, preserve_prompt: bool = True) -> None:
        if not preserve_prompt:
            clip.video_prompt = None
        clip.video_url = None
        clip.video_critiques = []
        clip.video_score = None
        clip.video_approved = False

    def _recalculate_timeline_starts(self, state: ProjectState) -> None:
        current_time = 0.0
        for clip in state.timeline:
            clip.duration = float(_closest_valid_veo_duration(clip.duration))
            clip.timeline_start = current_time
            current_time += clip.duration

    def _next_director_clip_id(self, state: ProjectState) -> str:
        max_index = -1
        for clip in state.timeline:
            match = re.search(r"(\d+)$", clip.id)
            if match:
                max_index = max(max_index, int(match.group(1)))

        candidate_index = max_index + 1
        while True:
            candidate_id = f"clip_{candidate_index}"
            if all(existing.id != candidate_id for existing in state.timeline):
                return candidate_id
            candidate_index += 1

    def _insert_director_clip(
        self,
        state: ProjectState,
        *,
        anchor_clip_id: str | None,
        position: str,
        storyboard_text: str,
        duration: float | None,
    ) -> VideoClip | None:
        if not storyboard_text.strip():
            return None

        new_clip = VideoClip(
            id=self._next_director_clip_id(state),
            timeline_start=0.0,
            duration=float(_closest_valid_veo_duration(duration or 4.0)),
            storyboard_text=storyboard_text.strip(),
        )

        insert_index = len(state.timeline) if position == "after" else 0
        if anchor_clip_id:
            anchor_index = next(
                (index for index, clip in enumerate(state.timeline) if clip.id == anchor_clip_id),
                None,
            )
            if anchor_index is not None:
                insert_index = anchor_index + 1 if position == "after" else anchor_index

        state.timeline.insert(insert_index, new_clip)
        return new_clip

    def _delete_director_clip(self, state: ProjectState, *, clip_id: str) -> VideoClip | None:
        for index, clip in enumerate(state.timeline):
            if clip.id == clip_id:
                return state.timeline.pop(index)
        return None

    def _move_director_clip(
        self,
        state: ProjectState,
        *,
        clip_id: str,
        anchor_clip_id: str,
        position: str,
    ) -> VideoClip | None:
        if clip_id == anchor_clip_id:
            return None

        source_index = next(
            (index for index, clip in enumerate(state.timeline) if clip.id == clip_id),
            None,
        )
        anchor_index = next(
            (index for index, clip in enumerate(state.timeline) if clip.id == anchor_clip_id),
            None,
        )
        if source_index is None or anchor_index is None:
            return None

        moved_clip = state.timeline.pop(source_index)
        if source_index < anchor_index:
            anchor_index -= 1
        insert_index = anchor_index + 1 if position == "after" else anchor_index
        state.timeline.insert(insert_index, moved_clip)
        return moved_clip

    def _normalize_music_production_fragments(
        self,
        fragments: list[ProductionTimelineFragment],
        *,
        program_duration: float,
    ) -> list[ProductionTimelineFragment]:
        if program_duration <= 0:
            return []
        normalized_fragments: list[ProductionTimelineFragment] = []
        working_fragments = [fragment for fragment in fragments if fragment.duration > 0]
        if not working_fragments:
            return [
                ProductionTimelineFragment(
                    id="music_frag_0",
                    track_type="music",
                    source_clip_id=None,
                    timeline_start=0.0,
                    source_start=0.0,
                    duration=round(program_duration, 3),
                    audio_enabled=True,
                )
            ]

        current_end = 0.0
        for index, fragment in enumerate(
            sorted(working_fragments, key=lambda item: (item.timeline_start, item.id))
        ):
            timeline_start = max(0.0, round(float(fragment.timeline_start), 3))
            timeline_start = max(timeline_start, round(current_end, 3))
            max_start = max(0.0, program_duration - 0.1)
            timeline_start = min(timeline_start, max_start)

            duration = max(0.1, round(float(fragment.duration), 3))
            duration = min(duration, max(0.1, program_duration - timeline_start))
            source_start = max(0.0, round(float(fragment.source_start), 3))

            normalized_fragments.append(
                ProductionTimelineFragment(
                    id=fragment.id or f"music_frag_{index}",
                    track_type="music",
                    source_clip_id=None,
                    timeline_start=round(timeline_start, 3),
                    source_start=source_start,
                    duration=round(duration, 3),
                    audio_enabled=True,
                )
            )
            current_end = timeline_start + duration

        return normalized_fragments

    def _reconcile_after_planning_edits(self, previous: ProjectState, state: ProjectState) -> None:
        previous_clips = {clip.id: clip for clip in previous.timeline}
        self._recalculate_timeline_starts(state)

        for clip in state.timeline:
            previous_clip = previous_clips.get(clip.id)
            if previous_clip is None:
                self._clear_clip_storyboard_outputs(clip)
                continue

            storyboard_changed = (previous_clip.storyboard_text or "").strip() != (clip.storyboard_text or "").strip()
            duration_changed = round(float(previous_clip.duration), 3) != round(float(clip.duration), 3)
            if storyboard_changed or duration_changed:
                self._clear_clip_storyboard_outputs(clip)

        self._preserve_music_production_fragments(state)
        state.final_video_url = None
        state.last_error = None
        self._trim_stage_summaries_to_stage(state, AgentStage.LYRIA_PROMPTING)

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

    def _rewind_state_to_stage(self, state: ProjectState, target_stage: AgentStage) -> None:
        stage_order = [
            AgentStage.INPUT,
            AgentStage.LYRIA_PROMPTING,
            AgentStage.PLANNING,
            AgentStage.STORYBOARDING,
            AgentStage.FILMING,
            AgentStage.PRODUCTION,
            AgentStage.COMPLETED,
        ]
        target_idx = stage_order.index(target_stage)

        if target_stage == AgentStage.INPUT:
            state.timeline = []
            state.lyrics_prompt = ""
            state.style_prompt = ""

        if target_idx <= stage_order.index(AgentStage.PLANNING) and target_stage != AgentStage.INPUT:
            for clip in state.timeline:
                clip.image_url = None
                clip.image_prompt = None
                clip.image_critiques = []
                clip.image_approved = False
                clip.image_score = None
                clip.image_reference_ready = False

        if target_idx <= stage_order.index(AgentStage.STORYBOARDING):
            for clip in state.timeline:
                clip.video_url = None
                clip.video_critiques = []
                clip.video_score = None
                clip.video_approved = False
                clip.video_prompt = None
            self._preserve_music_production_fragments(state)

        if target_idx <= stage_order.index(AgentStage.FILMING):
            self._preserve_music_production_fragments(state)

        if target_idx <= stage_order.index(AgentStage.PRODUCTION):
            state.final_video_url = None

        self._trim_stage_summaries_to_stage(state, target_stage)
        state.last_error = None
        state.current_stage = target_stage

    async def handle_live_director_mode(
        self,
        state: ProjectState,
        *,
        message: str,
        display_stage: AgentStage | str | None = None,
        selected_clip_id: str | None = None,
        selected_fragment_id: str | None = None,
        source: str = "text",
        speech_mode: str = "standard",
    ) -> tuple[ProjectState, dict[str, Any]]:
        if not self.client:
            raise RuntimeError("Live Director Mode requires Google model access.")

        requested_message = message.strip()
        if not requested_message:
            raise ValueError("Director instruction cannot be empty.")

        stage_value = display_stage.value if isinstance(display_stage, AgentStage) else str(display_stage or state.current_stage.value)
        try:
            review_stage = AgentStage(stage_value)
        except ValueError:
            review_stage = state.current_stage

        updated_state = state.model_copy(deep=True)
        previous_state = state.model_copy(deep=True)

        selected_clip = next((clip for clip in updated_state.timeline if clip.id == selected_clip_id), None)
        selected_fragment = next((fragment for fragment in updated_state.production_timeline if fragment.id == selected_fragment_id), None)
        shot_lookup = self._shot_lookup(updated_state)
        explicit_shot_references = self._resolve_director_shot_references(updated_state, requested_message)
        explicit_shot_number = explicit_shot_references[0][0] if explicit_shot_references else None
        explicit_target_clip_id = explicit_shot_references[0][1] if explicit_shot_references else None
        explicit_target_clip_ids = [clip_id for _, clip_id in explicit_shot_references if clip_id]
        explicit_target_fragment_id = self._resolve_director_fragment_reference_for_clip(updated_state, explicit_target_clip_id)

        clip_context = [
            {
                "shot_number": shot_lookup.get(clip.id),
                "clip_id": clip.id,
                "duration": clip.duration,
                "storyboard_text": clip.storyboard_text,
                "video_prompt": clip.video_prompt,
                "image_ready": bool(clip.image_url),
                "video_ready": bool(clip.video_url),
                "image_approved": clip.image_approved,
                "video_approved": clip.video_approved,
            }
            for clip in updated_state.timeline
        ]
        fragment_context = [
            {
                "track_type": fragment.track_type or "video",
                "fragment_id": fragment.id,
                "source_clip_id": fragment.source_clip_id,
                "source_shot_number": shot_lookup.get(fragment.source_clip_id),
                "timeline_start": fragment.timeline_start,
                "source_start": fragment.source_start,
                "duration": fragment.duration,
                "audio_enabled": fragment.audio_enabled,
            }
            for fragment in updated_state.production_timeline
        ]

        prompt = f"""
You are FMV Studio's Live Director agent.

Your job is to help the user adjust the CURRENTLY REVIEWED stage of an AI music video project in real time.
You must stay within the supported operations and never invent ids.

Current reviewed stage: {review_stage.value}
Actual saved pipeline stage: {updated_state.current_stage.value}
Selected clip id: {selected_clip.id if selected_clip else ""}
Selected fragment id: {selected_fragment.id if selected_fragment else ""}
Explicitly referenced shot number: {explicit_shot_number if explicit_shot_number is not None else ""}
Explicitly referenced shot numbers: {json.dumps([shot_number for shot_number, _ in explicit_shot_references], ensure_ascii=True)}
Resolved explicit clip id: {explicit_target_clip_id or ""}
Resolved explicit clip ids: {json.dumps(explicit_target_clip_ids, ensure_ascii=True)}
Resolved explicit fragment id: {explicit_target_fragment_id or ""}

Project summary:
- Name: {updated_state.name}
- Screenplay: {updated_state.screenplay}
- Instructions: {updated_state.instructions}
- Additional lore: {updated_state.additional_lore}
- Uploaded document context: {self._document_context_text(updated_state, max_chars=2000) or "(none)"}
- Lyrics prompt: {updated_state.lyrics_prompt}
- Style prompt: {updated_state.style_prompt}

Timeline clips:
{json.dumps(clip_context, ensure_ascii=True)}

Production fragments:
{json.dumps(fragment_context, ensure_ascii=True)}

Supported operations:
- global_updates: screenplay, instructions, additional_lore, lyrics_prompt, style_prompt, music_min_duration_seconds, music_max_duration_seconds
- one or more clip operations: each clip operation may update, insert, delete, or move a shot
- one production audio toggle: target_fragment_id + fragment_updates.audio_enabled
- optional stage navigation when the user asks to move forward or backward in the pipeline

Rules:
- If the user explicitly names a shot number, that exact shot is the primary target. Do not switch to a different shot.
- If the user explicitly names multiple shot numbers, you may update multiple shots in one reply. Return one clip_operations entry per affected shot.
- If the user asks to proceed, continue, move on, or go to the next stage, set navigation_action to "advance".
- If the user asks to go back, return, rewind, or revisit an earlier stage, set navigation_action to "rewind".
- If the user explicitly names a stage like planning, storyboarding, filming, production, or music, set target_stage to that stage value.
- Use clip operation types:
  - "update": modify an existing shot's storyboard_text and/or duration and/or video_prompt
  - "insert_before" / "insert_after": create a new shot relative to target_clip_id
  - "delete": remove target_clip_id
  - "move_before" / "move_after": move target_clip_id relative to anchor_clip_id
- In production, use fragment source_shot_number to match numbered shot requests when you need target_fragment_id.
- If the user says "this shot", "this clip", "this frame", or "this edit", prefer the selected ids.
- duration must be one of 4, 6, or 8 seconds.
- storyboard_text is required for insert operations and is only for planning/storyboarding-level changes.
- video_prompt is only for filming-level changes.
- fragment_updates.audio_enabled is only for production.
- For screenplay, instructions, additional_lore, lyrics_prompt, style_prompt, storyboard_text, and video_prompt, treat brief user phrasing as intent. Return finished, richer project copy, not a paraphrase of the instruction.
- When updating creative text, add grounded detail and make the output more vivid or production-ready than the user's wording.
- Multi-shot clip edits are supported, but keep each clip operation explicit and targeted to a real clip id from the timeline.
- Do not invent unsupported multi-fragment production edits or arbitrary timeline restructuring outside of the supported move/insert/delete shot operations.
- If the request is unsupported, explain that briefly in reply_text and leave the update fields empty.
- Keep reply_text concise and director-facing.

Return STRICTLY as JSON matching this schema:
{{
  "reply_text": string,
  "change_summary": [string],
  "global_updates": {{
    "screenplay": string | null,
    "instructions": string | null,
    "additional_lore": string | null,
    "lyrics_prompt": string | null,
    "style_prompt": string | null,
    "music_min_duration_seconds": number | null,
    "music_max_duration_seconds": number | null
  }},
  "clip_operations": [
    {{
      "operation_type": "update" | "insert_before" | "insert_after" | "delete" | "move_before" | "move_after",
      "target_clip_id": string | null,
      "anchor_clip_id": string | null,
      "storyboard_text": string | null,
      "duration": number | null,
      "video_prompt": string | null,
      "clear_target_image": boolean,
      "clear_target_video": boolean
    }}
  ],
  "target_fragment_id": string | null,
  "fragment_updates": {{
    "audio_enabled": boolean | null
  }},
  "navigation_action": "stay" | "advance" | "rewind",
  "target_stage": string | null,
  "rewind_to_stage": string | null
}}

User instruction:
{requested_message}
"""

        response = self.client.models.generate_content(
            model=self.orchestrator_model,
            contents=[prompt],
            config=self._orchestrator_config(
                response_mime_type="application/json",
                thinking_budget=2048,
                temperature=0.4,
            ),
        )
        action = json.loads(response.text)

        reply_text = str(action.get("reply_text") or "I reviewed the request, but I did not apply any concrete changes.").strip()
        change_summary = [
            str(item).strip()
            for item in (action.get("change_summary") or [])
            if str(item).strip()
        ]

        self._append_director_turn(
            updated_state,
            role="user",
            text=requested_message,
            stage=review_stage,
            source=source,
        )

        target_clip = None

        global_updates = action.get("global_updates") or {}
        for field_name in ("screenplay", "instructions", "additional_lore", "lyrics_prompt", "style_prompt"):
            value = global_updates.get(field_name)
            if isinstance(value, str):
                setattr(
                    updated_state,
                    field_name,
                    self._enrich_live_director_creative_text(
                        field_name=field_name,
                        requested_message=requested_message,
                        candidate_text=value.strip(),
                        current_value=str(getattr(updated_state, field_name) or "").strip(),
                        state=updated_state,
                        review_stage=review_stage,
                        target_clip=target_clip,
                        shot_lookup=shot_lookup,
                    ),
                )

        min_duration = global_updates.get("music_min_duration_seconds")
        max_duration = global_updates.get("music_max_duration_seconds")
        if isinstance(min_duration, (int, float)):
            updated_state.music_min_duration_seconds = max(8.0, float(min_duration))
        if isinstance(max_duration, (int, float)):
            updated_state.music_max_duration_seconds = max(8.0, float(max_duration))
        if (
            updated_state.music_min_duration_seconds is not None
            and updated_state.music_max_duration_seconds is not None
            and updated_state.music_min_duration_seconds > updated_state.music_max_duration_seconds
        ):
            updated_state.music_min_duration_seconds, updated_state.music_max_duration_seconds = (
                updated_state.music_max_duration_seconds,
                updated_state.music_min_duration_seconds,
            )

        raw_clip_operations = action.get("clip_operations")
        clip_operations: list[dict[str, Any]] = []
        if isinstance(raw_clip_operations, list):
            clip_operations = [item for item in raw_clip_operations if isinstance(item, dict)]

        legacy_target_clip_id = str(
            explicit_target_clip_id
            or action.get("target_clip_id")
            or selected_clip_id
            or ""
        ).strip() or None
        legacy_clip_updates = action.get("clip_updates") or {}
        if not clip_operations and (
            legacy_target_clip_id
            or isinstance(legacy_clip_updates, dict)
            or bool(action.get("clear_target_image"))
            or bool(action.get("clear_target_video"))
        ):
            clip_operations = [
                {
                    "target_clip_id": legacy_target_clip_id,
                    "storyboard_text": legacy_clip_updates.get("storyboard_text"),
                    "duration": legacy_clip_updates.get("duration"),
                    "video_prompt": legacy_clip_updates.get("video_prompt"),
                    "clear_target_image": bool(action.get("clear_target_image")),
                    "clear_target_video": bool(action.get("clear_target_video")),
                }
            ]

        planning_changed_clip_ids: set[str] = set()
        storyboarding_changed_clip_ids: set[str] = set()
        filming_changed_clip_ids: set[str] = set()
        structural_change_summary: list[str] = []

        def resolve_clip_by_id(clip_id: str | None) -> VideoClip | None:
            if not clip_id:
                return None
            return next((clip for clip in updated_state.timeline if clip.id == clip_id), None)

        for operation_index, clip_operation in enumerate(clip_operations):
            operation_type = str(clip_operation.get("operation_type") or "update").strip().lower() or "update"
            operation_target_clip_id = str(
                clip_operation.get("target_clip_id")
                or (
                    legacy_target_clip_id
                    if len(clip_operations) == 1 and operation_type == "update"
                    else ""
                )
                or ""
            ).strip() or None
            if operation_target_clip_id is None and len(explicit_target_clip_ids) > operation_index:
                operation_target_clip_id = explicit_target_clip_ids[operation_index]
            if operation_target_clip_id is None and len(clip_operations) == 1:
                operation_target_clip_id = explicit_target_clip_id or selected_clip_id

            operation_anchor_clip_id = str(clip_operation.get("anchor_clip_id") or "").strip() or None
            if (
                operation_anchor_clip_id is None
                and len(clip_operations) == 1
                and len(explicit_target_clip_ids) >= 2
                and operation_type in {"move_before", "move_after"}
            ):
                operation_anchor_clip_id = explicit_target_clip_ids[1]

            if operation_type in {"insert_before", "insert_after"}:
                inserted_storyboard_text = str(clip_operation.get("storyboard_text") or "").strip()
                inserted_clip = self._insert_director_clip(
                    updated_state,
                    anchor_clip_id=operation_target_clip_id,
                    position="after" if operation_type.endswith("after") else "before",
                    storyboard_text=inserted_storyboard_text,
                    duration=clip_operation.get("duration") if isinstance(clip_operation.get("duration"), (int, float)) else None,
                )
                if inserted_clip is None:
                    continue
                planning_changed_clip_ids.add(inserted_clip.id)
                if target_clip is None:
                    target_clip = inserted_clip
                if not change_summary:
                    anchor_label = self._shot_label(resolve_clip_by_id(operation_target_clip_id), shot_lookup)
                    relation = "after" if operation_type.endswith("after") else "before"
                    structural_change_summary.append(f"Added {self._shot_label(inserted_clip, self._shot_lookup(updated_state))} {relation} {anchor_label}.")
                continue

            operation_target_clip = resolve_clip_by_id(operation_target_clip_id)

            if operation_type == "delete":
                if operation_target_clip is None:
                    continue
                deleted_label = self._shot_label(operation_target_clip, shot_lookup)
                deleted_clip = self._delete_director_clip(updated_state, clip_id=operation_target_clip.id)
                if deleted_clip is None:
                    continue
                planning_changed_clip_ids.add(operation_target_clip.id)
                if target_clip is None:
                    target_clip = operation_target_clip
                if not change_summary:
                    structural_change_summary.append(f"Deleted {deleted_label}.")
                continue

            if operation_type in {"move_before", "move_after"}:
                if operation_target_clip is None or not operation_anchor_clip_id:
                    continue
                moved_label = self._shot_label(operation_target_clip, shot_lookup)
                anchor_clip = resolve_clip_by_id(operation_anchor_clip_id)
                if anchor_clip is None:
                    continue
                moved_clip = self._move_director_clip(
                    updated_state,
                    clip_id=operation_target_clip.id,
                    anchor_clip_id=anchor_clip.id,
                    position="after" if operation_type.endswith("after") else "before",
                )
                if moved_clip is None:
                    continue
                planning_changed_clip_ids.add(moved_clip.id)
                if target_clip is None:
                    target_clip = moved_clip
                if not change_summary:
                    relation = "after" if operation_type.endswith("after") else "before"
                    structural_change_summary.append(
                        f"Moved {moved_label} {relation} {self._shot_label(anchor_clip, shot_lookup)}."
                    )
                continue

            if operation_target_clip is None:
                continue
            if target_clip is None:
                target_clip = operation_target_clip

            storyboard_text = clip_operation.get("storyboard_text")
            if isinstance(storyboard_text, str) and storyboard_text.strip():
                operation_target_clip.storyboard_text = self._enrich_live_director_creative_text(
                    field_name="storyboard_text",
                    requested_message=requested_message,
                    candidate_text=storyboard_text.strip(),
                    current_value=str(operation_target_clip.storyboard_text or "").strip(),
                    state=updated_state,
                    review_stage=review_stage,
                    target_clip=operation_target_clip,
                    shot_lookup=shot_lookup,
                )
                if review_stage == AgentStage.PLANNING:
                    planning_changed_clip_ids.add(operation_target_clip.id)
                else:
                    storyboarding_changed_clip_ids.add(operation_target_clip.id)

            duration = clip_operation.get("duration")
            if isinstance(duration, (int, float)):
                operation_target_clip.duration = float(_closest_valid_veo_duration(duration))
                planning_changed_clip_ids.add(operation_target_clip.id)

            video_prompt = clip_operation.get("video_prompt")
            if isinstance(video_prompt, str) and video_prompt.strip():
                operation_target_clip.video_prompt = self._enrich_live_director_creative_text(
                    field_name="video_prompt",
                    requested_message=requested_message,
                    candidate_text=video_prompt.strip(),
                    current_value=str(operation_target_clip.video_prompt or "").strip(),
                    state=updated_state,
                    review_stage=review_stage,
                    target_clip=operation_target_clip,
                    shot_lookup=shot_lookup,
                )
                filming_changed_clip_ids.add(operation_target_clip.id)

            if bool(clip_operation.get("clear_target_image")):
                self._clear_clip_storyboard_outputs(operation_target_clip)
                storyboarding_changed_clip_ids.add(operation_target_clip.id)

            if bool(clip_operation.get("clear_target_video")):
                self._clear_clip_video_outputs(operation_target_clip, preserve_prompt=True)
                filming_changed_clip_ids.add(operation_target_clip.id)

        target_fragment_id = str(
            explicit_target_fragment_id
            or action.get("target_fragment_id")
            or selected_fragment_id
            or ""
        ).strip() or None
        fragment_updates = action.get("fragment_updates") or {}
        target_fragment = next((fragment for fragment in updated_state.production_timeline if fragment.id == target_fragment_id), None)
        fragment_changed = False
        if target_fragment and isinstance(fragment_updates.get("audio_enabled"), bool):
            target_fragment.audio_enabled = bool(fragment_updates["audio_enabled"])
            fragment_changed = True

        rewind_to_stage_raw = str(action.get("rewind_to_stage") or "").strip().lower()
        rewind_to_stage: AgentStage | None = None
        if rewind_to_stage_raw:
            try:
                rewind_to_stage = AgentStage(rewind_to_stage_raw)
            except ValueError:
                rewind_to_stage = None

        navigation_action = str(action.get("navigation_action") or "stay").strip().lower() or "stay"
        if navigation_action not in {"stay", "advance", "rewind"}:
            navigation_action = "stay"
        target_stage_raw = str(action.get("target_stage") or "").strip().lower()
        target_stage: AgentStage | None = None
        if target_stage_raw:
            try:
                target_stage = AgentStage(target_stage_raw)
            except ValueError:
                target_stage = None
        if rewind_to_stage and navigation_action == "stay":
            navigation_action = "rewind"
            target_stage = rewind_to_stage

        if planning_changed_clip_ids:
            self._reconcile_after_planning_edits(previous_state, updated_state)
            if review_stage in {AgentStage.PLANNING, AgentStage.STORYBOARDING} and not rewind_to_stage:
                updated_state.current_stage = review_stage
            if not change_summary:
                if structural_change_summary:
                    change_summary.extend(structural_change_summary)
                elif len(planning_changed_clip_ids) == 1 and target_clip is not None:
                    change_summary.append(f"Updated {self._shot_label(target_clip, shot_lookup)} and reconciled downstream outputs.")
                else:
                    change_summary.append(f"Updated {len(planning_changed_clip_ids)} shots and reconciled downstream outputs.")
        else:
            if storyboarding_changed_clip_ids:
                self._preserve_music_production_fragments(updated_state)
                updated_state.final_video_url = None
                updated_state.last_error = None
                updated_state.current_stage = AgentStage.STORYBOARDING
                self._trim_stage_summaries_to_stage(updated_state, AgentStage.PLANNING)
                if not change_summary:
                    if len(storyboarding_changed_clip_ids) == 1 and target_clip is not None:
                        change_summary.append(f"Updated {self._shot_label(target_clip, shot_lookup)} and cleared its frame/video outputs.")
                    else:
                        change_summary.append(f"Updated {len(storyboarding_changed_clip_ids)} shots and cleared their frame/video outputs.")

            if filming_changed_clip_ids and not storyboarding_changed_clip_ids:
                self._preserve_music_production_fragments(updated_state)
                updated_state.final_video_url = None
                updated_state.last_error = None
                updated_state.current_stage = AgentStage.FILMING
                self._trim_stage_summaries_to_stage(updated_state, AgentStage.STORYBOARDING)
                if not change_summary:
                    if len(filming_changed_clip_ids) == 1 and target_clip is not None:
                        change_summary.append(f"Updated {self._shot_label(target_clip, shot_lookup)} and cleared its rendered clip.")
                    else:
                        change_summary.append(f"Updated {len(filming_changed_clip_ids)} shots and cleared their rendered clips.")

            if fragment_changed:
                updated_state.final_video_url = None
                updated_state.last_error = None
                updated_state.current_stage = AgentStage.PRODUCTION
                self._trim_stage_summaries_to_stage(updated_state, AgentStage.FILMING)
                if not change_summary:
                    change_summary.append("Updated the selected production fragment.")

        if rewind_to_stage:
            self._rewind_state_to_stage(updated_state, rewind_to_stage)
            if not change_summary:
                change_summary.append(f"Rewound the project to {rewind_to_stage.value}.")

        agent_turn_id = f"director_{uuid.uuid4().hex}"
        agent_audio_url = None
        if speech_mode != "realtime":
            agent_audio_url = await self._synthesize_director_reply_audio(
                updated_state,
                turn_id=agent_turn_id,
                reply_text=reply_text,
            )
        self._append_director_turn(
            updated_state,
            turn_id=agent_turn_id,
            role="agent",
            text=reply_text,
            audio_url=agent_audio_url,
            stage=updated_state.current_stage,
            source="agent",
            applied_changes=change_summary,
        )

        return updated_state, {
            "reply_text": reply_text,
            "applied_changes": change_summary,
            "target_clip_id": target_clip.id if target_clip else None,
            "target_fragment_id": target_fragment.id if target_fragment else None,
            "stage": updated_state.current_stage.value,
            "navigation_action": navigation_action,
            "target_stage": target_stage.value if target_stage else None,
        }

    def _build_stage_summary_text(self, state: ProjectState, stage: AgentStage) -> str:
        shot_lookup = self._shot_lookup(state)
        ordered_clips = sorted(state.timeline, key=lambda item: item.timeline_start)

        if stage == AgentStage.PLANNING:
            if not ordered_clips:
                return "Planning is ready, but no shots were generated. Please revisit the screenplay and inputs before moving forward."

            total_duration = sum(clip.duration for clip in ordered_clips)
            longest_clip = max(ordered_clips, key=lambda clip: clip.duration)
            return (
                f"Planning is ready. I mapped {len(ordered_clips)} shots across roughly {total_duration:.1f} seconds, "
                "and the overall structure now has a clear visual arc. "
                f"Please give {self._shot_label(longest_clip, shot_lookup)} extra attention because at {longest_clip.duration:.1f} seconds, "
                "it will have the biggest impact on pacing."
            )

        if stage == AgentStage.STORYBOARDING:
            if not ordered_clips:
                return "Storyboarding is ready, but there are no shots to review."

            approved_clips = [clip for clip in ordered_clips if clip.image_approved is True and clip.image_url]
            scored_clips = [clip for clip in approved_clips if clip.image_score is not None]
            best_clip = max(scored_clips, key=lambda clip: clip.image_score or 0, default=None)
            attention_clip = next((clip for clip in ordered_clips if clip.image_approved is not True), None)
            if attention_clip is None:
                low_scored = [clip for clip in approved_clips if (clip.image_score or 10) < STORYBOARD_PASS_SCORE + 1]
                attention_clip = min(low_scored, key=lambda clip: clip.image_score or 10, default=None)

            positive = (
                f"{len(approved_clips)} of {len(ordered_clips)} frames are in place"
                if len(approved_clips) != len(ordered_clips)
                else f"All {len(ordered_clips)} frames are in place"
            )
            if best_clip and best_clip.image_score is not None:
                positive += f", and {self._shot_label(best_clip, shot_lookup)} is the strongest image so far with a score of {best_clip.image_score}."
            else:
                positive += "."

            if attention_clip:
                note = self._latest_review_note(attention_clip.image_critiques) or "it still needs a closer continuity pass."
                return (
                    f"Storyboarding is ready. {positive} "
                    f"Please inspect {self._shot_label(attention_clip, shot_lookup)} next; the latest review noted that {note}."
                )

            return (
                f"Storyboarding is ready. {positive} "
                "No single frame is standing out as a major risk right now, so this pass mostly needs your taste and continuity review."
            )

        if stage == AgentStage.FILMING:
            if not ordered_clips:
                return "Filming is ready, but there are no shots to review."

            rendered_clips = [clip for clip in ordered_clips if clip.video_url]
            scored_clips = [clip for clip in rendered_clips if clip.video_score is not None]
            best_clip = max(scored_clips, key=lambda clip: clip.video_score or 0, default=None)

            attention_clip = next((clip for clip in ordered_clips if not clip.video_url), None)
            if attention_clip is None:
                attention_clip = next(
                    (
                        clip for clip in rendered_clips
                        if any("music hard fail" in critique.lower() for critique in clip.video_critiques)
                    ),
                    None,
                )
            if attention_clip is None:
                low_scored = [clip for clip in rendered_clips if (clip.video_score or 10) <= 7]
                attention_clip = min(low_scored, key=lambda clip: clip.video_score or 10, default=None)

            positive = (
                f"{len(rendered_clips)} of {len(ordered_clips)} clips rendered successfully"
                if len(rendered_clips) != len(ordered_clips)
                else f"All {len(ordered_clips)} clips rendered successfully"
            )
            if best_clip and best_clip.video_score is not None:
                positive += f", and {self._shot_label(best_clip, shot_lookup)} is the cleanest take so far with a score of {best_clip.video_score}."
            else:
                positive += "."

            if attention_clip:
                note = self._latest_review_note(attention_clip.video_critiques)
                if not note and not attention_clip.video_url:
                    note = "it did not finish with a usable render."
                elif not note:
                    note = "it should get a closer review before you approve the stage."
                return (
                    f"Filming is ready. {positive} "
                    f"Please inspect {self._shot_label(attention_clip, shot_lookup)} first; the latest review noted that {note}."
                )

            return (
                f"Filming is ready. {positive} "
                "Nothing is standing out as a major problem right now, so the next pass is mainly about your editorial taste."
            )

        if stage == AgentStage.PRODUCTION:
            fragments = sorted(state.production_timeline, key=lambda item: item.timeline_start)
            total_duration = sum(fragment.duration for fragment in fragments)
            muted_fragments = [fragment for fragment in fragments if not fragment.audio_enabled]
            low_scored = [clip for clip in ordered_clips if (clip.video_score or 10) <= 7]
            best_clip = max(
                [clip for clip in ordered_clips if clip.video_score is not None],
                key=lambda clip: clip.video_score or 0,
                default=None,
            )

            positive = (
                f"Production is ready. The cut is assembled into {len(fragments)} fragments across roughly {total_duration:.1f} seconds."
                if fragments
                else "Production is ready, but the edit timeline is still empty."
            )
            if best_clip and best_clip.video_score is not None:
                positive += f" {self._shot_label(best_clip, shot_lookup)} remains your strongest source clip."

            if muted_fragments:
                return (
                    f"{positive} "
                    f"Please review the audio flow carefully because {len(muted_fragments)} fragment"
                    f"{'s have' if len(muted_fragments) != 1 else ' has'} muted source audio."
                )
            if low_scored:
                focus_clip = min(low_scored, key=lambda clip: clip.video_score or 10)
                return (
                    f"{positive} "
                    f"Please keep an eye on {self._shot_label(focus_clip, shot_lookup)}, because it is still one of the weaker rendered sources in the cut."
                )
            return (
                f"{positive} "
                "The flow looks stable overall, so this stage mainly needs your editorial judgment on rhythm and transitions."
            )

        if stage == AgentStage.COMPLETED:
            fragments = sorted(state.production_timeline, key=lambda item: item.timeline_start)
            total_duration = sum(fragment.duration for fragment in fragments) or sum(clip.duration for clip in ordered_clips)
            low_scored = [clip for clip in ordered_clips if (clip.video_score or 10) <= 7]
            muted_fragments = [fragment for fragment in fragments if not fragment.audio_enabled]

            positive = (
                f"The final render is ready at roughly {total_duration:.1f} seconds."
                if state.final_video_url
                else "The final render step finished, but no export is currently attached."
            )
            if low_scored:
                focus_clip = min(low_scored, key=lambda clip: clip.video_score or 10)
                return (
                    f"{positive} "
                    f"The overall structure held together well, but you may want to revisit {self._shot_label(focus_clip, shot_lookup)} if you want to polish the master further."
                )
            if muted_fragments:
                return (
                    f"{positive} "
                    f"Please remember that {len(muted_fragments)} fragment"
                    f"{'s were' if len(muted_fragments) != 1 else ' was'} exported without source audio by design."
                )
            return (
                f"{positive} "
                "Nothing obvious is demanding attention now, so this master is ready for your final taste check and export."
            )

        return ""

    def _write_stage_summary_wave_file(self, output_path: str, pcm_bytes: bytes) -> None:
        with wave.open(output_path, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes(pcm_bytes)

    async def _synthesize_project_voice_audio(
        self,
        *,
        state: ProjectState,
        text: str,
        relative_filename: str,
    ) -> str | None:
        if not self.client:
            return None

        try:
            response = await asyncio.to_thread(
                self.client.models.generate_content,
                model=self.speech_model,
                contents=text,
                config=genai.types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=genai.types.SpeechConfig(
                        voice_config=genai.types.VoiceConfig(
                            prebuilt_voice_config=genai.types.PrebuiltVoiceConfig(
                                voice_name=self.stage_brief_voice,
                            )
                        )
                    ),
                ),
            )
            audio_bytes = response.candidates[0].content.parts[0].inline_data.data
            if not audio_bytes:
                return None

            output_path = PROJECTS_DIR / relative_filename
            self._write_stage_summary_wave_file(str(output_path), audio_bytes)
            return self._sync_local_project_artifact(
                output_path,
                relative_path=output_path.name,
                content_type="audio/wav",
            )
        except Exception as e:
            print(f"[tts] Audio generation failed for {relative_filename}: {e}")
            return None

    async def _synthesize_stage_summary_audio(self, state: ProjectState, stage: AgentStage, summary_text: str) -> str | None:
        if not self.stage_voice_briefs_enabled:
            return None

        try:
            return await self._synthesize_project_voice_audio(
                state=state,
                text=summary_text,
                relative_filename=f"{state.project_id}_{stage.value}_brief.wav",
            )
        except Exception as e:
            print(f"[stage_summary] TTS generation failed for {stage.value}: {e}")
            return None

    async def _synthesize_director_reply_audio(
        self,
        state: ProjectState,
        *,
        turn_id: str,
        reply_text: str,
    ) -> str | None:
        return await self._synthesize_project_voice_audio(
            state=state,
            text=reply_text,
            relative_filename=f"{state.project_id}_{turn_id}.wav",
        )

    async def _update_stage_summary(self, state: ProjectState, stage: AgentStage) -> None:
        if stage not in STAGE_SUMMARY_STAGES:
            return

        summary_text = self._build_stage_summary_text(state, stage).strip()
        if not summary_text:
            return

        audio_url = await self._synthesize_stage_summary_audio(state, stage, summary_text)
        state.stage_summaries[stage.value] = StageSummary(
            text=summary_text,
            audio_url=audio_url,
            generated_at=self._stage_summary_generated_at(),
        )

    def _infer_resume_stage(self, state: ProjectState) -> AgentStage:
        """Infer the most likely recoverable stage after a halted run."""
        if state.final_video_url:
            return AgentStage.COMPLETED

        if state.production_timeline:
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

        if not state.music_url and (state.lyrics_prompt or state.style_prompt):
            return AgentStage.LYRIA_PROMPTING

        return AgentStage.INPUT

    async def _measure_audio_duration_seconds(self, music_url: str | None) -> float | None:
        music_path = _local_media_path(music_url)
        if not music_path or not os.path.exists(music_path):
            return None

        try:
            ffprobe_path = FFMPEG.replace("ffmpeg", "ffprobe").replace("ffmpeg.exe", "ffprobe.exe")
            proc = await asyncio.create_subprocess_exec(
                ffprobe_path,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                music_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return float(stdout.decode().strip())
        except Exception:
            return None

    async def _probe_video_dimensions(self, video_path: str) -> tuple[int | None, int | None]:
        try:
            proc = await asyncio.create_subprocess_exec(
                FFPROBE,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0:s=x",
                video_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return (None, None)
            output = stdout.decode().strip()
            if "x" not in output:
                return (None, None)
            width_text, height_text = output.split("x", 1)
            return (int(width_text), int(height_text))
        except Exception:
            return (None, None)

    async def _probe_image_dimensions(self, image_path: str) -> tuple[int | None, int | None]:
        try:
            proc = await asyncio.create_subprocess_exec(
                FFPROBE,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0:s=x",
                image_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return (None, None)
            output = stdout.decode().strip()
            if "x" not in output:
                return (None, None)
            width_text, height_text = output.split("x", 1)
            return (int(width_text), int(height_text))
        except Exception:
            return (None, None)

    async def _normalize_image_canvas(
        self,
        *,
        input_path: str,
        output_path: str,
    ) -> None:
        proc = await asyncio.create_subprocess_exec(
            FFMPEG,
            "-y",
            "-i", input_path,
            "-vf",
            (
                f"scale={self.image_width}:{self.image_height}:force_original_aspect_ratio=decrease,"
                f"pad={self.image_width}:{self.image_height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                "setsar=1"
            ),
            "-frames:v", "1",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg failed while normalizing image '{input_path}': {stderr.decode()}"
            )
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError(
                f"ffmpeg reported success but produced an empty normalized image for '{input_path}'"
            )

    async def _normalize_storyboard_image_bytes(
        self,
        *,
        image_bytes: bytes,
        image_mime_type: str,
    ) -> tuple[bytes, str]:
        is_png = image_mime_type == "image/png" and image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        is_jpeg = image_mime_type in {"image/jpeg", "image/jpg"} and image_bytes.startswith(b"\xff\xd8")
        is_webp = image_mime_type == "image/webp" and image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP"

        if not (is_png or is_jpeg or is_webp):
            return image_bytes, image_mime_type

        if is_png and len(image_bytes) >= 24:
            width = int.from_bytes(image_bytes[16:20], "big")
            height = int.from_bytes(image_bytes[20:24], "big")
            if (width, height) == (self.image_width, self.image_height):
                return image_bytes, image_mime_type

        guessed_extension = mimetypes.guess_extension(image_mime_type) or ".png"
        if guessed_extension == ".jpe":
            guessed_extension = ".jpg"

        with tempfile.TemporaryDirectory(prefix="fmv-storyboard-") as temp_dir:
            input_path = os.path.join(temp_dir, f"input{guessed_extension}")
            output_path = os.path.join(temp_dir, "output.png")
            with open(input_path, "wb") as file_handle:
                file_handle.write(image_bytes)

            width, height = await self._probe_image_dimensions(input_path)
            if width is None or height is None:
                return image_bytes, image_mime_type
            if (width, height) != (self.image_width, self.image_height):
                await self._normalize_image_canvas(
                    input_path=input_path,
                    output_path=output_path,
                )
                with open(output_path, "rb") as file_handle:
                    return file_handle.read(), "image/png"

        return image_bytes, image_mime_type

    async def _normalize_video_canvas(
        self,
        *,
        input_path: str,
        output_path: str,
        include_audio: bool,
        source_start: float = 0.0,
        duration: float | None = None,
    ) -> None:
        command = [FFMPEG, "-y"]
        if source_start > 0:
            command.extend(["-ss", f"{source_start:.3f}"])
        command.extend(["-i", input_path])
        if duration is not None:
            command.extend(["-t", f"{duration:.3f}"])
        command.extend(["-map", "0:v:0"])
        if include_audio:
            command.extend(["-map", "0:a?"])
        else:
            command.append("-an")
        command.extend([
            "-vf", (
                "fps=30,"
                f"scale={self.video_width}:{self.video_height}:force_original_aspect_ratio=decrease,"
                f"pad={self.video_width}:{self.video_height}:(ow-iw)/2:(oh-ih)/2,"
                "setsar=1"
            ),
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
        ])
        if include_audio:
            command.extend([
                "-c:a", "aac",
                "-ac", "2",
                "-ar", "48000",
            ])
        command.extend([
            "-movflags", "+faststart",
            str(output_path),
        ])
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg failed while normalizing clip '{input_path}': {stderr.decode()}"
            )
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError(
                f"ffmpeg reported success but produced an empty normalized clip for '{input_path}'"
            )

    async def _generate_storyboard_frame(self, *, contents: list[Any]) -> tuple[bytes, str]:
        return await self.image_provider.generate_frame(self, contents=contents)

    async def _generate_google_storyboard_image(self, *, contents: list[Any]) -> tuple[bytes, str]:
        result = await asyncio.to_thread(
            self.client.models.generate_content,
            model=self.image_model,
            contents=contents,
            config=genai.types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
                image_config=genai.types.ImageConfig(
                    aspect_ratio=self.image_aspect_ratio,
                    image_size=self.image_size,
                ),
            )
        )

        image_bytes_out = None
        image_mime_type = "image/png"
        for part in result.candidates[0].content.parts:
            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                image_bytes_out = part.inline_data.data
                image_mime_type = part.inline_data.mime_type
                break

        if not image_bytes_out:
            raise RuntimeError(f"{self.image_provider.definition.label} returned no image data")

        return await self._normalize_storyboard_image_bytes(
            image_bytes=image_bytes_out,
            image_mime_type=image_mime_type,
        )

    def _sanitize_video_motion_prompt_text(self, text: str) -> str:
        cleaned = str(text or "").strip()
        if not cleaned:
            return ""

        cleaned = cleaned.replace("\n", " ").replace("\r", " ")
        cleaned = re.sub(r"[\"'`]+", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned)
        cleaned = re.sub(r"\s+([,.;:])", r"\1", cleaned)
        cleaned = re.sub(r"([,.;:]){2,}", r"\1", cleaned)
        cleaned = re.sub(r"\.\.+", ".", cleaned)
        cleaned = cleaned.strip(" ,.;:-")

        if not cleaned:
            return ""

        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+", cleaned)
            if sentence.strip()
        ]
        if sentences:
            cleaned = " ".join(sentences[:2])

        words = cleaned.split()
        if len(words) > 40:
            cleaned = " ".join(words[:40]).rstrip(",;:")

        if cleaned and cleaned[-1] not in ".!?":
            cleaned += "."

        return cleaned

    def _fallback_video_motion_prompt(self, clip: VideoClip, state: ProjectState) -> str:
        storyboard_text = self._sanitize_video_motion_prompt_text(clip.storyboard_text or "")
        if not storyboard_text:
            return "Slow continuous camera move through the scene with natural motion. Single continuous shot."

        content_clause = storyboard_text.rstrip(".")
        return f"Slow continuous camera move through the scene as {content_clause}. Single continuous shot."

    def _compose_video_generation_prompt(self, motion_prompt: str) -> str:
        cleaned_prompt = self._sanitize_video_motion_prompt_text(motion_prompt)
        if not cleaned_prompt:
            cleaned_prompt = "Natural continuous motion within the scene."

        return (
            f"{cleaned_prompt} "
            "Preserve the existing subject, scene, and style from the source frame. "
            "Single continuous shot. "
            "Do not generate music, vocals, singing, soundtrack, beat, or score. "
            "Diegetic ambient sound effects are acceptable, but there must be no musical content."
        )

    async def _build_video_motion_prompt(self, clip: VideoClip, state: ProjectState) -> str:
        storyboard_text = self._sanitize_video_motion_prompt_text(clip.storyboard_text or "")
        if not storyboard_text:
            return "Natural continuous motion within the scene."

        model_generate_content = getattr(getattr(self.client, "models", None), "generate_content", None)
        if not callable(model_generate_content):
            return storyboard_text

        prompt = f"""
You are rewriting a storyboard description into a Veo 3.1 image-to-video motion prompt.

The source image already defines the subject, scene, lighting, composition, wardrobe, and style.
Write only the motion to animate from that image.

Rules:
- Return one short plain-English sentence, about 12 to 35 words.
- Focus on camera motion, subtle subject motion, and environmental motion.
- Use general terms like "the subject", "the figure", "the mechanism", or "the scene".
- Preserve the original subjects, props, setting, and themes from the storyboard text.
- Rephrase only enough to make the shot easier for Veo to animate from the source frame and less likely to trigger unnecessary filtering.
- Do not restate appearance, textures, lighting, or static scene details already visible in the frame.
- Keep the same core imagery and tone; do not censor or remove the subject matter.
- Avoid piling on loaded, overly literal, or redundant phrasing.
- Do not mention audio, music, duration, aspect ratio, resolution, or editing.
- Keep it as one continuous shot.

Storyboard description:
{storyboard_text}

Return only the final motion prompt.
""".strip()

        try:
            response = await asyncio.to_thread(
                model_generate_content,
                model=self.orchestrator_model,
                contents=[prompt],
                config=self._orchestrator_config(
                    thinking_budget=512,
                    temperature=0.2,
                ),
            )
            candidate = self._sanitize_video_motion_prompt_text(getattr(response, "text", "") or "")
        except Exception:
            return storyboard_text

        word_count = len(candidate.split())
        if not candidate or word_count < 6:
            return storyboard_text

        return candidate

    async def _build_video_retry_prompt(
        self,
        clip: VideoClip,
        state: ProjectState,
        *,
        failed_prompt: str,
        failure_message: str,
    ) -> str:
        fallback_prompt = self._fallback_video_motion_prompt(clip, state)
        model_generate_content = getattr(getattr(self.client, "models", None), "generate_content", None)
        if not callable(model_generate_content):
            return fallback_prompt

        storyboard_text = self._sanitize_video_motion_prompt_text(clip.storyboard_text or "")
        previous_prompt = self._sanitize_video_motion_prompt_text(failed_prompt)
        failure_text = self._sanitize_video_motion_prompt_text(failure_message)

        prompt = f"""
A Veo 3.1 image-to-video generation failed. Rewrite the prompt so it keeps the same subjects, props, setting, and themes but is phrased more simply and naturally for animation from the source frame.

Rules:
- Return one short plain-English sentence, about 12 to 35 words.
- Keep the same core content and tone.
- Focus on visible motion and a single continuous shot.
- Preserve the original meaning instead of substituting different imagery.
- Simplify phrasing and remove redundant detail if needed.
- Do not mention audio, music, duration, aspect ratio, resolution, or editing.

Storyboard description:
{storyboard_text}

Previous prompt:
{previous_prompt}

Failure message:
{failure_text}

Return only the rewritten motion prompt.
""".strip()

        try:
            response = await asyncio.to_thread(
                model_generate_content,
                model=self.orchestrator_model,
                contents=[prompt],
                config=self._orchestrator_config(
                    thinking_budget=512,
                    temperature=0.2,
                ),
            )
            candidate = self._sanitize_video_motion_prompt_text(getattr(response, "text", "") or "")
        except Exception:
            return fallback_prompt

        word_count = len(candidate.split())
        if not candidate or word_count < 6:
            return fallback_prompt

        return candidate

    async def _generate_video_clip(
        self,
        *,
        prompt: str,
        duration_seconds: int,
        image_path: str | None,
    ) -> bytes:
        return await self.video_provider.generate_clip(
            self,
            prompt=prompt,
            duration_seconds=duration_seconds,
            image_path=image_path,
        )

    async def _generate_google_video_clip(
        self,
        *,
        prompt: str,
        duration_seconds: int,
        image_path: str | None,
    ) -> bytes:
        client = self.media_client if self.uses_vertex_ai else self.client
        if client is None:
            client = self.media_client or self.client
        if not client:
            raise RuntimeError("Google video generation is not configured.")

        source_kwargs: dict[str, Any] = {"prompt": prompt}
        if image_path and os.path.exists(image_path):
            with open(image_path, "rb") as f:
                img_bytes = f.read()
            mime = mimetypes.guess_type(image_path)[0] or "image/png"
            source_kwargs["image"] = genai.types.Image(image_bytes=img_bytes, mime_type=mime)

        config_kwargs: dict[str, Any] = {
            "number_of_videos": 1,
            "duration_seconds": duration_seconds,
            "aspect_ratio": self.video_aspect_ratio,
            "resolution": self.video_resolution,
        }
        output_gcs_uri: str | None = None
        if self.uses_vertex_ai:
            output_gcs_uri = self._vertex_output_gcs_uri(
                f"generated-video/{uuid.uuid4().hex}"
            )
            config_kwargs["output_gcs_uri"] = output_gcs_uri

        generate_kwargs: dict[str, Any] = dict(
            model=self.video_model,
            source=genai.types.GenerateVideosSource(**source_kwargs),
            config=genai.types.GenerateVideosConfig(**config_kwargs),
        )

        operation = await asyncio.to_thread(
            client.models.generate_videos,
            **generate_kwargs
        )

        timeout_seconds = 300
        poll_interval = 5
        elapsed = 0

        while not operation.done:
            if elapsed >= timeout_seconds:
                raise TimeoutError(
                    f"{self.video_provider.definition.label} job timed out after {timeout_seconds}s"
                )
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            operation = await asyncio.to_thread(client.operations.get, operation)

        operation_payload = getattr(operation, "response", None) or getattr(operation, "result", None)
        generated_videos = self._extract_generated_videos(operation_payload)
        if not generated_videos and output_gcs_uri:
            video_bytes = await asyncio.to_thread(
                self._download_first_gcs_prefix_bytes,
                output_gcs_uri,
            )
            if video_bytes:
                return video_bytes

        if not generated_videos:
            filter_reasons = self._extract_video_filter_reasons(operation_payload)
            if filter_reasons:
                raise RuntimeError(
                    f"{self.video_provider.definition.label} returned no videos. "
                    f"Possible filter reasons: {', '.join(filter_reasons)}"
                )
            raise RuntimeError(f"{self.video_provider.definition.label} returned no videos")

        generated_video = generated_videos[0]
        video_bytes = getattr(generated_video, "video_bytes", None)
        if not video_bytes and not self.uses_vertex_ai and getattr(client, "files", None):
            video_bytes = await asyncio.to_thread(
                client.files.download,
                file=generated_video,
            )
        if not video_bytes:
            video_uri = getattr(generated_video, "uri", None)
            if video_uri and str(video_uri).startswith("gs://"):
                video_bytes = await asyncio.to_thread(self._download_gcs_uri_bytes, str(video_uri))
        if not video_bytes and output_gcs_uri:
            video_bytes = await asyncio.to_thread(
                self._download_first_gcs_prefix_bytes,
                output_gcs_uri,
            )
        if not video_bytes:
            raise RuntimeError(f"{self.video_provider.definition.label} returned no downloadable video bytes")

        return video_bytes

    def _music_extension_for_mime_type(self, mime_type: str | None) -> str:
        normalized = (mime_type or "").lower()
        if normalized in {"audio/mpeg", "audio/mp3", "audio/mpga"}:
            return ".mp3"
        if normalized in {"audio/wav", "audio/x-wav", "audio/wave"}:
            return ".wav"
        if normalized in {"audio/ogg", "audio/vorbis"}:
            return ".ogg"

        guessed = mimetypes.guess_extension(normalized) if normalized else None
        if guessed == ".mpga":
            return ".mp3"
        return guessed or ".mp3"

    def _content_part_from_local_file(
        self,
        path: str,
        *,
        mime_type: str | None = None,
    ) -> Any:
        detected_mime = mime_type or mimetypes.guess_type(path)[0] or "application/octet-stream"

        if self.uses_vertex_ai:
            with open(path, "rb") as file_handle:
                return genai.types.Part.from_bytes(
                    data=file_handle.read(),
                    mime_type=detected_mime,
                )

        if not self.client or not getattr(self.client, "files", None):
            raise RuntimeError("Google file upload is not configured.")

        upload_kwargs: dict[str, Any] = {"file": path}
        if mime_type:
            upload_kwargs["config"] = {"mime_type": detected_mime}
        return self.client.files.upload(**upload_kwargs)

    def _write_silent_music_preview(self, output_path: str, duration_seconds: float) -> None:
        sample_rate = LYRIA_PCM_SAMPLE_RATE
        clamped_duration = max(1.0, float(duration_seconds))
        frame_count = max(sample_rate, int(sample_rate * clamped_duration))
        silence = b"\x00\x00" * frame_count * LYRIA_PCM_CHANNELS
        with wave.open(output_path, "wb") as wav_file:
            wav_file.setnchannels(LYRIA_PCM_CHANNELS)
            wav_file.setsampwidth(LYRIA_PCM_SAMPLE_WIDTH)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(silence)

    def _write_music_preview_wave_file(self, output_path: str, pcm_bytes: bytes) -> None:
        # Google's Live Music stream currently returns raw 16-bit PCM at 48kHz stereo.
        with wave.open(output_path, "wb") as wav_file:
            wav_file.setnchannels(LYRIA_PCM_CHANNELS)
            wav_file.setsampwidth(LYRIA_PCM_SAMPLE_WIDTH)
            wav_file.setframerate(LYRIA_PCM_SAMPLE_RATE)
            wav_file.writeframes(pcm_bytes)

    def _pcm_duration_seconds(self, pcm_bytes: bytes) -> float:
        bytes_per_second = LYRIA_PCM_SAMPLE_RATE * LYRIA_PCM_CHANNELS * LYRIA_PCM_SAMPLE_WIDTH
        if bytes_per_second <= 0:
            return 0.0
        return len(pcm_bytes) / bytes_per_second

    def _normalized_music_model_name(self) -> str:
        if not self.music_model:
            raise RuntimeError(f"{self.music_provider.definition.label} does not expose a direct model name.")
        if self.music_model.startswith("models/"):
            return self.music_model
        return f"models/{self.music_model}"

    def _vertex_output_gcs_uri(self, subdir: str) -> str:
        bucket_name = os.getenv("FMV_GCS_BUCKET", "").strip()
        if not bucket_name:
            raise RuntimeError("Vertex AI media generation requires FMV_GCS_BUCKET for output staging.")
        cleaned_subdir = subdir.strip("/").replace("\\", "/")
        if cleaned_subdir:
            return f"gs://{bucket_name}/{cleaned_subdir}/"
        return f"gs://{bucket_name}/"

    def _extract_generated_videos(self, operation_payload: Any) -> list[Any]:
        if not operation_payload:
            return []

        raw_videos = None
        if isinstance(operation_payload, dict):
            raw_videos = (
                operation_payload.get("generated_videos")
                or operation_payload.get("generatedVideos")
                or operation_payload.get("videos")
            )
        else:
            raw_videos = (
                getattr(operation_payload, "generated_videos", None)
                or getattr(operation_payload, "generatedVideos", None)
                or getattr(operation_payload, "videos", None)
            )

        if not raw_videos:
            return []

        normalized_videos: list[Any] = []
        for item in raw_videos:
            if isinstance(item, dict):
                normalized_videos.append(item.get("video") or item)
            else:
                normalized_videos.append(getattr(item, "video", None) or item)
        return [video for video in normalized_videos if video is not None]

    def _extract_video_filter_reasons(self, operation_payload: Any) -> list[str]:
        if not operation_payload:
            return []

        if isinstance(operation_payload, dict):
            reasons = (
                operation_payload.get("rai_media_filtered_reasons")
                or operation_payload.get("raiMediaFilteredReasons")
                or []
            )
            filtered_count = (
                operation_payload.get("rai_media_filtered_count")
                or operation_payload.get("raiMediaFilteredCount")
                or 0
            )
        else:
            reasons = (
                getattr(operation_payload, "rai_media_filtered_reasons", None)
                or getattr(operation_payload, "raiMediaFilteredReasons", None)
                or []
            )
            filtered_count = (
                getattr(operation_payload, "rai_media_filtered_count", None)
                or getattr(operation_payload, "raiMediaFilteredCount", None)
                or 0
            )

        normalized_reasons = [str(reason) for reason in reasons if str(reason).strip()]
        if normalized_reasons:
            return normalized_reasons
        if filtered_count:
            return [f"{filtered_count} video(s) filtered by safety checks"]
        return []

    def _download_gcs_uri_bytes(self, uri: str) -> bytes:
        try:
            from google.cloud import storage
        except ImportError as exc:  # pragma: no cover - runtime dependency
            raise RuntimeError(
                "Downloading Vertex AI media from GCS requires the 'google-cloud-storage' package."
            ) from exc

        if not uri.startswith("gs://"):
            raise RuntimeError(f"Unsupported GCS URI: {uri}")

        bucket_name, _, blob_name = uri.removeprefix("gs://").partition("/")
        if not bucket_name or not blob_name:
            raise RuntimeError(f"Malformed GCS URI: {uri}")

        client = storage.Client(project=get_gcp_project() or None)
        blob = client.bucket(bucket_name).blob(blob_name)
        return blob.download_as_bytes()

    def _download_first_gcs_prefix_bytes(self, prefix_uri: str) -> bytes | None:
        try:
            from google.cloud import storage
        except ImportError as exc:  # pragma: no cover - runtime dependency
            raise RuntimeError(
                "Downloading Vertex AI media from GCS requires the 'google-cloud-storage' package."
            ) from exc

        if not prefix_uri.startswith("gs://"):
            raise RuntimeError(f"Unsupported GCS URI: {prefix_uri}")

        bucket_name, _, blob_prefix = prefix_uri.removeprefix("gs://").partition("/")
        if not bucket_name:
            raise RuntimeError(f"Malformed GCS URI: {prefix_uri}")

        client = storage.Client(project=get_gcp_project() or None)
        bucket = client.bucket(bucket_name)
        blobs = sorted(
            (
                blob
                for blob in bucket.list_blobs(prefix=blob_prefix)
                if blob.name.lower().endswith(".mp4")
            ),
            key=lambda blob: blob.name,
        )
        if not blobs:
            return None
        return blobs[0].download_as_bytes()

    def _music_duration_bounds_seconds(self, state: ProjectState) -> tuple[float, float]:
        min_duration = float(state.music_min_duration_seconds or MIN_LYRIA_SONG_SECONDS)
        max_duration = float(state.music_max_duration_seconds or MAX_LYRIA_SONG_SECONDS)

        normalized_min = max(8.0, min_duration)
        normalized_max = max(8.0, max_duration)
        if normalized_min > normalized_max:
            normalized_min, normalized_max = normalized_max, normalized_min

        return normalized_min, normalized_max

    def _estimate_music_track_duration_seconds(self, state: ProjectState) -> float:
        min_duration, max_duration = self._music_duration_bounds_seconds(state)
        lyric_text = (state.lyrics_prompt or "").strip()
        if not lyric_text:
            return max(min_duration, min(max_duration, DEFAULT_LYRIA_SONG_SECONDS))

        word_count = len(re.findall(r"\b[\w']+\b", lyric_text))
        nonempty_lines = [
            line.strip()
            for line in lyric_text.splitlines()
            if line.strip()
        ]
        section_headers = re.findall(
            r"\((verse|chorus|bridge|hook|pre-chorus|outro|intro)[^)]*\)",
            lyric_text,
            flags=re.IGNORECASE,
        )

        # Songs are usually longer than the raw lyric readout because sections repeat
        # and there is instrumental space around the vocal lines.
        spoken_seconds = word_count / 2.2 if word_count else 0.0
        arrangement_padding = 18.0
        line_padding = min(len(nonempty_lines), 24) * 1.5
        section_padding = len(section_headers) * 10.0

        estimated_seconds = (spoken_seconds * 1.35) + arrangement_padding + line_padding + section_padding
        return max(min_duration, min(max_duration, estimated_seconds))

    def _current_generated_music_signature(self, state: ProjectState) -> dict[str, float | str]:
        min_duration, max_duration = self._music_duration_bounds_seconds(state)
        return {
            "provider": self.music_provider_id,
            "lyrics_prompt": (state.lyrics_prompt or "").strip() if self.music_provider.definition.uses_lyrics else "",
            "style_prompt": (state.style_prompt or "").strip(),
            "min_duration_seconds": float(min_duration),
            "max_duration_seconds": float(max_duration),
        }

    def _apply_generated_music_signature(self, state: ProjectState) -> None:
        signature = self._current_generated_music_signature(state)
        state.generated_music_provider = str(signature["provider"])
        state.generated_music_lyrics_prompt = str(signature["lyrics_prompt"])
        state.generated_music_style_prompt = str(signature["style_prompt"])
        state.generated_music_min_duration_seconds = float(signature["min_duration_seconds"])
        state.generated_music_max_duration_seconds = float(signature["max_duration_seconds"])

    def _has_current_generated_music_track(self, state: ProjectState) -> bool:
        if not state.music_url:
            return False

        signature = self._current_generated_music_signature(state)
        return (
            (state.generated_music_provider or "") == signature["provider"]
            and (state.generated_music_lyrics_prompt or "") == signature["lyrics_prompt"]
            and (state.generated_music_style_prompt or "") == signature["style_prompt"]
            and float(state.generated_music_min_duration_seconds or 0.0) == signature["min_duration_seconds"]
            and float(state.generated_music_max_duration_seconds or 0.0) == signature["max_duration_seconds"]
        )

    def _music_prompting_blocking_message(self, state: ProjectState) -> str | None:
        if not self.music_provider.can_generate_automatically():
            return self.music_provider.blocking_message(state)

        if not state.music_url:
            return f"Generate a song with {self.music_provider.definition.label} before continuing to Planning."

        if not self._has_current_generated_music_track(state):
            return "Regenerate the song after changing the active music generation settings before continuing to Planning."

        return None

    async def _generate_music_track(
        self,
        state: ProjectState,
        *,
        target_duration_seconds: float | None = None,
    ) -> str | None:
        music_url = await self.music_provider.generate_track(
            self,
            state,
            target_duration_seconds=target_duration_seconds,
        )
        if music_url:
            state.music_url = music_url
            self._apply_generated_music_signature(state)
        return music_url

    async def _build_music_generation_prompt(self, state: ProjectState) -> str:
        style_prompt = (state.style_prompt or "").strip()
        lyrics_prompt = (state.lyrics_prompt or "").strip()

        if self.music_provider.definition.uses_lyrics:
            prompt_parts = []
            if lyrics_prompt:
                prompt_parts.append(lyrics_prompt)
            if style_prompt:
                prompt_parts.append(f"Style: {style_prompt}")
            prompt = "\n\n".join(prompt_parts).strip()
            if not prompt:
                raise RuntimeError("Cannot generate music without lyrics or style prompts.")
            return prompt

        if not style_prompt and not lyrics_prompt:
            raise RuntimeError(
                f"{self.music_provider.definition.label} requires a style prompt because lyrics are ignored by this provider."
            )

        if not self.client:
            return style_prompt

        adaptation_prompt = f"""
You are preparing a prompt for an instrumental-only music generation model.

Preserve the intended genre, mood, tempo, energy, instrumentation, era, and production texture.
Use lyrics only as narrative context and emotional guidance. Do not write a sung performance prompt.
Return one concise instrumental production prompt only. No JSON. No bullets. No quotes.

Project screenplay:
{state.screenplay}

Project instructions:
{state.instructions}

Narrative lyric context:
{lyrics_prompt}

Existing style prompt:
{style_prompt}
"""

        response = self.client.models.generate_content(
            model=self.orchestrator_model,
            contents=[adaptation_prompt],
            config=self._orchestrator_config(
                thinking_budget=1024,
                temperature=0.3,
            ),
        )
        adapted_prompt = str(response.text or "").strip()
        return adapted_prompt or style_prompt

    def _generate_vertex_lyria_track_bytes(self, prompt: str) -> tuple[bytes, str]:
        try:
            import google.auth
            from google.auth.transport.requests import Request as GoogleAuthRequest
        except ImportError as exc:  # pragma: no cover - runtime dependency
            raise RuntimeError("Vertex AI music generation requires the 'google-auth' package.") from exc

        from urllib import request as urllib_request

        project = get_gcp_project()
        location = get_vertex_media_location()
        if not project:
            raise RuntimeError("Vertex AI music generation requires FMV_GCP_PROJECT or GOOGLE_CLOUD_PROJECT.")

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(GoogleAuthRequest())

        endpoint = (
            f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}"
            f"/locations/{location}/publishers/google/models/lyria-002:predict"
        )
        payload = {
            "instances": [
                {
                    "prompt": prompt,
                    "negative_prompt": "vocals, singing, lyrics, speech, spoken word, dialogue",
                }
            ],
            "parameters": {},
        }
        request = urllib_request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {credentials.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib_request.urlopen(request, timeout=300) as response:  # nosec B310
            body = json.loads(response.read().decode("utf-8"))

        prediction = (body.get("predictions") or [None])[0]
        audio_content = None
        if isinstance(prediction, dict):
            audio_content = prediction.get("audioContent") or prediction.get("bytesBase64Encoded")
        if not isinstance(prediction, dict) or not audio_content:
            body_preview = json.dumps(body, ensure_ascii=True)[:600]
            raise RuntimeError(f"Vertex AI Lyria returned no audio content. Response preview: {body_preview}")
        return (
            base64.b64decode(audio_content),
            str(prediction.get("mimeType") or "audio/wav"),
        )

    async def _generate_google_lyria_realtime_track(
        self,
        state: ProjectState,
        *,
        target_duration_seconds: float | None = None,
    ) -> str | None:
        project_dir = PROJECTS_DIR
        os.makedirs(project_dir, exist_ok=True)

        if not self.music_client:
            preview_duration = max(8.0, float(target_duration_seconds or self._estimate_music_track_duration_seconds(state)))
            music_path = project_dir / f"{state.project_id}_music.wav"
            self._write_silent_music_preview(str(music_path), preview_duration)
            state.music_url = self._sync_local_project_artifact(
                music_path,
                relative_path=music_path.name,
                content_type="audio/wav",
            )
            return state.music_url

        lyria_prompt = await self._build_music_generation_prompt(state)

        if self.uses_vertex_ai:
            audio_bytes, mime_type = await asyncio.to_thread(
                self._generate_vertex_lyria_track_bytes,
                lyria_prompt,
            )
            music_extension = self._music_extension_for_mime_type(mime_type)
            music_path = project_dir / f"{state.project_id}_music{music_extension}"
            music_path.write_bytes(audio_bytes)
            state.music_url = self._sync_local_project_artifact(
                music_path,
                relative_path=music_path.name,
                content_type=mime_type,
            )
            return state.music_url

        weighted_prompts = [genai.types.WeightedPrompt(text=lyria_prompt, weight=1.0)]
        if state.style_prompt.strip() and state.style_prompt.strip() not in lyria_prompt:
            weighted_prompts.append(
                genai.types.WeightedPrompt(text=state.style_prompt.strip(), weight=0.75)
            )

        target_seconds = max(
            8.0,
            float(target_duration_seconds or self._estimate_music_track_duration_seconds(state)),
        )
        timeout_seconds = max(30.0, target_seconds * 4.0)
        target_pcm_duration = target_seconds
        collected_chunks: list[bytes] = []

        async with self.music_client.aio.live.music.connect(model=self._normalized_music_model_name()) as session:
            await session.set_weighted_prompts(prompts=weighted_prompts)
            await session.set_music_generation_config(
                config=genai.types.LiveMusicGenerationConfig(
                    music_generation_mode=genai.types.MusicGenerationMode.QUALITY,
                )
            )
            await session.play()

            try:
                async with asyncio.timeout(timeout_seconds):
                    async for message in session.receive():
                        if message.filtered_prompt:
                            filtered_text = message.filtered_prompt.text or "Prompt was filtered."
                            filtered_reason = message.filtered_prompt.filtered_reason or "No reason given."
                            raise RuntimeError(f"Lyria prompt filtered: {filtered_reason}. Prompt: {filtered_text}")

                        audio_chunks = getattr(message.server_content, "audio_chunks", None) or []
                        for chunk in audio_chunks:
                            if chunk.data:
                                collected_chunks.append(chunk.data)

                        total_pcm = b"".join(collected_chunks)
                        if self._pcm_duration_seconds(total_pcm) >= target_pcm_duration:
                            await session.stop()
                            break
            except TimeoutError as exc:
                if not collected_chunks:
                    raise RuntimeError("Lyria music generation timed out before any audio arrived.") from exc

        pcm_bytes = b"".join(collected_chunks)
        if not pcm_bytes:
            raise RuntimeError("Lyria music generation returned no audio chunks.")

        music_path = project_dir / f"{state.project_id}_music.wav"
        self._write_music_preview_wave_file(str(music_path), pcm_bytes)
        state.music_url = self._sync_local_project_artifact(
            music_path,
            relative_path=music_path.name,
            content_type="audio/wav",
        )
        return state.music_url

    def _apply_timeline_durations(
        self,
        state: ProjectState,
        durations: list[int],
    ) -> None:
        current_time = 0.0
        for clip, duration in zip(state.timeline, durations):
            clip.duration = float(duration)
            clip.timeline_start = current_time
            current_time += duration

    def _initialize_production_timeline(self, state: ProjectState) -> None:
        current_time = 0.0
        fragments: list[ProductionTimelineFragment] = []
        for clip in sorted(state.timeline, key=lambda item: item.timeline_start):
            duration = max(0.1, float(clip.duration))
            fragments.append(
                ProductionTimelineFragment(
                    id=f"{clip.id}_frag_0",
                    track_type="video",
                    source_clip_id=clip.id,
                    timeline_start=current_time,
                    source_start=0.0,
                    duration=duration,
                    audio_enabled=True,
                )
            )
            current_time += duration
        music_fragments = self._normalize_music_production_fragments(
            self._music_production_fragments(state) if state.music_url else [],
            program_duration=current_time,
        ) if state.music_url else []
        state.production_timeline = fragments + music_fragments

    def _reconcile_production_timeline(self, state: ProjectState) -> None:
        if not state.timeline:
            self._preserve_music_production_fragments(state)
            return

        if not state.production_timeline:
            self._initialize_production_timeline(state)
            return

        ordered_clips = sorted(state.timeline, key=lambda item: item.timeline_start)
        clip_lookup = {clip.id: clip for clip in ordered_clips}
        clip_ids = {clip.id for clip in ordered_clips}
        video_fragments = [
            fragment
            for fragment in state.production_timeline
            if (fragment.track_type or "video") != "music"
        ]
        fragment_clip_ids = {
            fragment.source_clip_id
            for fragment in video_fragments
            if fragment.duration > 0
        }

        # If the cut references removed shots or omits newly current shots, fall back
        # to a clean default edit instead of trying to render stale fragments.
        if fragment_clip_ids != clip_ids:
            self._initialize_production_timeline(state)
            return

        normalized_fragments: list[ProductionTimelineFragment] = []
        current_time = 0.0
        for fragment in sorted(
            video_fragments,
            key=lambda item: (item.timeline_start, item.id),
        ):
            clip = clip_lookup.get(fragment.source_clip_id)
            if clip is None:
                self._initialize_production_timeline(state)
                return

            clip_duration = max(0.1, float(clip.duration))
            source_start = max(0.0, float(fragment.source_start))
            if source_start >= clip_duration:
                self._initialize_production_timeline(state)
                return

            remaining_duration = max(0.1, clip_duration - source_start)
            duration = max(0.1, float(fragment.duration))
            if duration > remaining_duration + 1e-3:
                self._initialize_production_timeline(state)
                return

            normalized_fragments.append(
                ProductionTimelineFragment(
                    id=fragment.id,
                    track_type="video",
                    source_clip_id=fragment.source_clip_id,
                    timeline_start=current_time,
                    source_start=round(source_start, 3),
                    duration=round(duration, 3),
                    audio_enabled=fragment.audio_enabled,
                )
            )
            current_time += duration

        music_fragments = self._normalize_music_production_fragments(
            self._music_production_fragments(state) if state.music_url else [],
            program_duration=current_time,
        ) if state.music_url else []
        state.production_timeline = normalized_fragments + music_fragments

    async def _normalize_timeline_for_veo(self, state: ProjectState) -> None:
        if not state.timeline:
            return
        target_total = await self._measure_audio_duration_seconds(state.music_url)
        normalized_durations = _normalize_veo_duration_sequence(
            [clip.duration for clip in state.timeline],
            target_total=target_total,
        )
        self._apply_timeline_durations(state, normalized_durations)

    async def run_pipeline(self, state: ProjectState) -> ProjectState:
        """
        Main routing function for the ADK pipeline.
        Executes the appropriate node based on the current stage and advances the state
        if no HITL breakpoints are hit.
        """
        starting_stage = state.current_stage
        state.image_provider = self.image_provider_id
        state.video_provider = self.video_provider_id
        state.music_provider = self.music_provider_id
        try:
            if state.current_stage == AgentStage.INPUT:
                if state.music_workflow == "uploaded_track" and state.music_url:
                    state = await self.node_planning(state)
                else:
                    # Generated or manually imported song workflows should always revisit
                    # the Music stage from Input, even if a prior render still exists.
                    state = await self.node_music_prompting(state)
            
            elif state.current_stage == AgentStage.LYRIA_PROMPTING:
                blocking_message = self._music_prompting_blocking_message(state)
                if blocking_message:
                    state.last_error = blocking_message
                    state.current_stage = AgentStage.LYRIA_PROMPTING
                    return state
                state.last_error = None
                state = await self.node_planning(state)
            
            elif state.current_stage == AgentStage.PLANNING:
                # Assuming HITL approved the planning phase
                state = await self.node_storyboarding(state)
            
            elif state.current_stage == AgentStage.STORYBOARDING:
                if all(clip.image_approved for clip in state.timeline):
                    state = await self.node_filming(state)
                else:
                    state = await self.node_storyboarding(state)
            
            elif state.current_stage == AgentStage.FILMING:
                if all(clip.video_approved and clip.video_url for clip in state.timeline):
                    state = self.node_prepare_production(state)
                else:
                    state = await self.node_filming(state)

            elif state.current_stage == AgentStage.PRODUCTION:
                state = await self.node_production(state)

            elif state.current_stage == AgentStage.HALTED_FOR_REVIEW:
                state.current_stage = self._infer_resume_stage(state)
                state.last_error = None
                return await self.run_pipeline(state)

            stage_summary_key = state.current_stage.value
            if (
                state.current_stage != starting_stage
                or stage_summary_key not in state.stage_summaries
            ):
                await self._update_stage_summary(state, state.current_stage)
            return state

        except Exception as e:
            state.last_error = str(e)
            state.current_stage = AgentStage.HALTED_FOR_REVIEW
            return state

    async def node_music_prompting(self, state: ProjectState) -> ProjectState:
        """
        Node 1.5: Gemini 3.1 Pro (Music Drafts)
        If the user didn't upload audio, we need a song workflow.
        This step uses Gemini to read the screenplay and draft the inputs
        (lyrics and style) for the selected music provider, which the user can then review.
        """
        provider_generates_lyrics = self.music_provider.definition.uses_lyrics
        if not self.client:
            state.lyrics_prompt = "(Mock) A silent breeze blows through the neon trees\nDigital hearts beat to an analog freeze..."
            state.style_prompt = "(Mock) Synthwave, cyberpunk, 80s retrowave, driving beat, shimmering analog synth textures"
        else:
            music_prompting_task = (
                "1. 'lyrics_prompt': A few stanzas of poetic lyrics that capture the narrative.\n"
                "            2. 'style_prompt': A comma-separated list of musical genres, moods, instrumentation, tempo, and production qualities."
                if provider_generates_lyrics
                else "1. 'lyrics_prompt': A short narrative lyric sketch for user reference only.\n"
                "            2. 'style_prompt': A comma-separated list of instrumental genres, moods, instrumentation, tempo, and production qualities. "
                "Do not include vocal style instructions because the active provider is instrumental-only."
            )
            prompt = f"""
            You are an expert music producer and lyricist. 
            The user wants to generate a song for a music video.
            Analyze the following screenplay, instructions, lore, and uploaded document context to draft two things:
            {music_prompting_task}

            ### Inputs:
            Screenplay:
            {state.screenplay}
            
            Instructions:
            {state.instructions}

            {self._project_context_block(state, max_document_chars=3000)}
            
            ### Task:
            Provide the output STRICTLY as a JSON object matching this schema: 
            {{ "lyrics_prompt": string, "style_prompt": string }}
            """
            
            response = self.client.models.generate_content(
                model=self.orchestrator_model,
                contents=[prompt],
                config=self._orchestrator_config(
                    response_mime_type="application/json",
                    thinking_budget=1024,
                ),
            )
            
            try:
                data = json.loads(response.text)
                state.lyrics_prompt = data.get("lyrics_prompt", "")
                state.style_prompt = data.get("style_prompt", "")
            except json.JSONDecodeError as e:
                raise ValueError(f"Failed to parse Gemini Lyria prompting JSON: {str(e)}\nRaw Response: {response.text}")
                
        state.last_error = None

        state.current_stage = AgentStage.LYRIA_PROMPTING
        return state

    async def node_planning(self, state: ProjectState) -> ProjectState:
        """
        Node 1: Gemini 3.1
        Takes the screenplay and lore, and breaks it down into a timeline of chunks (<=8s each).
        Halt for HITL review after generation.
        """
        if not self.client:
            # Mock behavior if no key (for dev purposes)
            state.timeline = [
                VideoClip(
                    id="clip_1", timeline_start=0, duration=6.0, 
                    storyboard_text="A sprawling neon city at night, rain falling."
                ),
                VideoClip(
                    id="clip_2", timeline_start=6.0, duration=4.0, 
                    storyboard_text="Close up on the protagonist looking up at the sky."
                )
            ]
        else:
            # ── Measure audio duration so Gemini knows how long to fill ────────
            audio_duration_seconds = await self._measure_audio_duration_seconds(state.music_url)
            audio_duration_hint = ""
            if audio_duration_seconds is not None:
                audio_duration_hint = (
                    f"\n            DURATION CONSTRAINT (HARD RULE):\n"
                    f"            The audio track is exactly {audio_duration_seconds:.1f} seconds long.\n"
                    f"            Your clips MUST sum to approximately {audio_duration_seconds:.1f} seconds total.\n"
                    f"            Generate the minimum number of clips needed to fill this duration naturally.\n"
                    f"            Every clip duration MUST be exactly one of: 4, 6, or 8 seconds.\n"
                    f"            Aim for {max(1, int(audio_duration_seconds // 6))}–{max(2, int(audio_duration_seconds // 4))} clips."
                )

            # ── Upload audio for multimodal beat alignment ───────────────────
            media_files = []
            music_path = _local_media_path(state.music_url)
            if music_path and os.path.exists(music_path):
                media_files.append(self._content_part_from_local_file(music_path))

            prompt = f"""
            You are an expert music video director and visual architect. 
            Break down the following screenplay into a timeline of individual visual clips for an AI pipeline. 
            Each clip duration must be EXACTLY one of 4, 6, or 8 seconds. Do not use any other values.
{audio_duration_hint}
            CRITICAL: 
            - The storyboard image provider will generate the starting frames, and the video provider will animate them.
            - The storyboard image provider requires highly descriptive, specific visual prompts (lighting, camera angle, subject, style).
            - Consider the provided "Instructions", "Additional Lore", and uploaded document context carefully to ensure stylistic consistency across all clips.
            - If uploaded reference images are labeled with a character, creature, prop, vehicle, or location name, preserve that name verbatim in any storyboard descriptions where that entity appears so downstream reference routing can attach the correct asset.
            - If an audio track was provided (which you are currently listening to), align the clip transitions with major musical shifts or beats.

            ### Inputs:
            Screenplay:
            {state.screenplay}
            
            Instructions:
            {state.instructions}

            {self._project_context_block(state, max_document_chars=4000)}
            
            ### Task:
            Provide the output STRICTLY as a JSON list of objects matching this schema: 
            [{{ "duration": float, "storyboard_text": string }}]
            """
            
            response = self.client.models.generate_content(
                model=self.orchestrator_model,
                contents=media_files + [prompt],
                config=self._orchestrator_config(
                    response_mime_type="application/json",
                    thinking_budget=16384,
                ),
            )
            
            try:
                clip_data = json.loads(response.text)
                state.timeline = []
                proposed_storyboard_texts: list[str] = []
                proposed_durations: list[float] = []
                for clip in clip_data:
                    proposed_storyboard_texts.append(clip.get("storyboard_text", ""))
                    proposed_durations.append(float(clip.get("duration", 6.0)))

                normalized_durations = _normalize_veo_duration_sequence(
                    proposed_durations,
                    target_total=audio_duration_seconds,
                )
                current_time = 0.0
                for idx, (storyboard_text, duration) in enumerate(zip(proposed_storyboard_texts, normalized_durations)):
                    state.timeline.append(VideoClip(
                        id=f"clip_{idx}",
                        timeline_start=current_time,
                        duration=float(duration),
                        storyboard_text=storyboard_text
                    ))
                    current_time += duration
            except Exception as e:
                raise RuntimeError(f"Failed to parse Gemini output: {e}\nResponse: {response.text}")

        # Breakpoint for HITL review
        state.current_stage = AgentStage.PLANNING
        return state

    async def _build_asset_relevance_map(
        self,
        image_assets: list,
        clips: list,
        *,
        screenplay: str,
    ) -> dict:
        """
        Orchestrator call: shows Gemini all reference images + all storyboard texts in one
        multimodal request.  Returns a dict mapping clip.id → [asset.id, ...] listing only
        the assets that are visually relevant to that specific shot.

        Example: a shot of a hallway gets the hallway reference but NOT the woman/dog references.
        """
        import mimetypes

        if not image_assets or not clips:
            return {}

        # ── Build the multimodal prompt ──────────────────────────────────────
        # Label each asset image so Gemini can refer to it by ID in the JSON output.
        parts: list = [
            "You are a film production asset coordinator.\n\n"
            "The following labeled reference images have been provided by the director. "
            "Labels are semantic ground truth. If a label names a character, creature, prop, vehicle, or location from the screenplay, "
            "treat that image as the canonical visual reference for that named entity.\n"
        ]

        for asset in image_assets:
            asset_label = display_asset_label(getattr(asset, "label", None), asset.name)
            parts.append(f"[ASSET_ID: {asset.id}  |  Label: {asset_label}  |  File: {asset.name}]")
            asset_path = _local_media_path(asset.url)
            if not asset_path or not os.path.exists(asset_path):
                continue
            with open(asset_path, "rb") as f:
                img_bytes = f.read()
            mime = mimetypes.guess_type(asset_path)[0] or "image/jpeg"
            parts.append(genai.types.Part.from_bytes(data=img_bytes, mime_type=mime))

        clips_json = "\n".join(
            f'  "{clip.id}": "{clip.storyboard_text}"'
            for clip in clips
        )
        parts.append(
            f"\nScreenplay context:\n{screenplay}\n\n"
            f"\nHere are the storyboard descriptions for each shot (clip_id → description):\n"
            f"{{\n{clips_json}\n}}\n\n"
            "For EACH clip, decide which of the reference assets are visually relevant to that shot.\n"
            "An asset is relevant ONLY if the shot features the same labeled person, creature, prop, vehicle, or location shown in the asset image.\n"
            "It is NOT relevant if the shot does not feature that subject.\n\n"
            "If a label matches a named entity in the screenplay or storyboard text, route that asset to every shot where that named entity appears.\n\n"
            "CRITICAL: For every relevant asset you select, you must explicitly classify it as either a 'subject' (e.g., a character, vehicle, or specific object) or a 'background' (e.g., a room, landscape, or environment).\n\n"
            "Return a JSON object mapping each clip_id to a list of relevant asset objects:\n"
            '{"clip_id": [{"id": "asset_id1", "type": "subject"}, {"id": "asset_id2", "type": "background"}], ...}\n'
            "Use [] for clips where no assets are relevant. Return ALL clip_ids."
        )

        try:
            response = self.client.models.generate_content(
                model=self.orchestrator_model,
                contents=parts,
                config=self._orchestrator_config(
                    response_mime_type="application/json",
                    thinking_budget=1024,
                ),
            )
            data = json.loads(response.text)
            if not isinstance(data, dict):
                raise ValueError(
                    f"Expected a JSON object from asset routing, got {type(data).__name__}"
                )
            return data
        except Exception as e:
            print(f"[asset_relevance_map] Failed, falling back to no references: {e}")
            return {}

    async def _select_relevant_previous_shots(
        self,
        current_clip: VideoClip,
        previous_clips: list[VideoClip],
        *,
        limit: int = STORYBOARD_MAX_REFERENCE_SHOTS,
    ) -> list[VideoClip]:
        """
        Uses Gemini to determine which previously high-scoring shots are the most
        relevant continuity references for the current clip.
        """
        if not previous_clips:
            return []

        prev_clips_json = "\n".join(
            f'  ["{idx}"] - "{clip.storyboard_text}"'
            for idx, clip in enumerate(previous_clips)
        )

        prompt = f"""
You are an expert continuity director.

Current Shot to Generate:
"{current_clip.storyboard_text}"

Previously Confirmed High-Quality Shots in the Timeline:
{prev_clips_json}

Select up to {limit} prior shots that are the most relevant continuity references for the current shot.
Prioritize:
- the same character identity / design
- the same creature or hero prop
- the same room, location, or environment
- immediate action continuity

If a prior shot is unrelated, do not include it.
Order the returned indices from most useful to least useful.

Return your answer STRICTLY as a JSON object:
{{"relevant_indices": [<int>, ...]}}
"""
        try:
            response = self.client.models.generate_content(
                model=self.orchestrator_model,
                contents=[prompt],
                config=self._orchestrator_config(
                    response_mime_type="application/json",
                    thinking_budget=512,
                )
            )
            data = json.loads(response.text)
            raw_indices = data.get("relevant_indices", [])
            if not isinstance(raw_indices, list):
                return []

            normalized_indices: list[int] = []
            for index in raw_indices:
                if not isinstance(index, int):
                    continue
                if 0 <= index < len(previous_clips) and index not in normalized_indices:
                    normalized_indices.append(index)
                if len(normalized_indices) >= limit:
                    break

            return [previous_clips[index] for index in normalized_indices]
        except Exception as e:
            print(f"[select_previous_shots] Failed: {e}")
            return []

    async def node_storyboarding(self, state: ProjectState) -> ProjectState:
        """
        Node 2: Storyboard Image Provider + Auto-Critique
        1. Gemini orchestrator decides which reference assets apply to each shot (smart routing).
        2. The active image provider generates each frame using only the relevant references.
        3. Gemini critic scores each frame and triggers retries with refined prompts.
        """
        state.current_stage = AgentStage.STORYBOARDING
        state.last_error = None

        if not self.client:
            await self._persist_state(state)
            for clip in state.timeline:
                self._check_cancelled()
                if not clip.image_approved:
                    clip.image_url = f"https://picsum.photos/seed/{clip.id}/800/450"
                    await self._persist_state(state)
            return state

        await self._persist_state(state)

        # ── Phase 1: Smart asset routing ─────────────────────────────────────
        import mimetypes
        image_assets = []
        for asset in state.assets:
            if asset.type != "image":
                continue
            asset_path = _local_media_path(asset.url)
            if asset_path and os.path.exists(asset_path):
                image_assets.append((asset, asset_path))

        # Build a reusable bytes cache keyed by asset id
        asset_bytes: dict[str, tuple[bytes, str]] = {}
        asset_lookup = {asset.id: asset for asset, _ in image_assets}
        for asset, asset_path in image_assets:
            with open(asset_path, "rb") as f:
                img_bytes = f.read()
            mime = mimetypes.guess_type(asset_path)[0] or "image/jpeg"
            asset_bytes[asset.id] = (img_bytes, mime)

        unapproved_clips = [c for c in state.timeline if not c.image_approved]

        relevance_map: dict[str, Any] = {}
        if image_assets:
            raw_relevance_map = await self._build_asset_relevance_map(
                [asset for asset, _ in image_assets],
                unapproved_clips,
                screenplay=state.screenplay,
            )
            if isinstance(raw_relevance_map, dict):
                relevance_map = raw_relevance_map
            else:
                print(
                    f"[asset_relevance_map] Ignoring unexpected relevance map type: "
                    f"{type(raw_relevance_map).__name__}"
                )

        # ── Phase 2: Generate each shot with iterative auto-critique ──────────
        async def _process_clip(
            clip: VideoClip,
            relevant_assets: list[dict],
            previous_shots: list[VideoClip],
        ) -> None:
            base_prompt = (
                f"{clip.storyboard_text}. {state.instructions}. "
                f"High quality, cinematic still frame, {self.image_aspect_ratio} aspect ratio, {self.image_size} detail."
            )
            working_prompt = base_prompt
            clip.image_prompt = base_prompt
            clip.image_url = None
            clip.image_score = None
            clip.image_reference_ready = False
            clip.image_approved = False
            clip.image_critiques = []

            clip_ref_parts: list = []
            primary_previous_shot = previous_shots[0] if previous_shots else None

            primary_shot_path = _local_media_path(primary_previous_shot.image_url) if primary_previous_shot else None
            if primary_previous_shot and primary_shot_path and os.path.exists(primary_shot_path):
                clip_ref_parts.append(
                    "PRIMARY CONTINUITY REFERENCE (CRITICAL): Match the same character identity, environment, and visual continuity unless the shot description explicitly calls for a change."
                )
                with open(primary_shot_path, "rb") as f:
                    clip_ref_parts.append(
                        genai.types.Part.from_bytes(data=f.read(), mime_type="image/png")
                    )

            if relevant_assets:
                for asset in relevant_assets:
                    asset_id = asset.get("id")
                    asset_type = asset.get("type")
                    if asset_id in asset_bytes:
                        img_bytes, mime = asset_bytes[asset_id]
                        asset_label = display_asset_label(
                            getattr(asset_lookup.get(asset_id), "label", None),
                            getattr(asset_lookup.get(asset_id), "name", asset_id),
                        )
                        if asset_type == "subject":
                            clip_ref_parts.append(
                                f"REFERENCE: Subject '{asset_label}' [{asset_id}]. "
                                f"Treat this image as the canonical appearance reference for that named subject. "
                                f"Use this image ONLY for the subject's likeness/appearance. DO NOT copy or use the background from this image. "
                                f"Ensure the subject is placed entirely in the environment described in the main prompt or continuity references."
                            )
                        else:
                            clip_ref_parts.append(
                                f"REFERENCE: Background/Location '{asset_label}' [{asset_id}]. "
                                f"Use this image as the canonical environmental reference for that named setting."
                            )
                        clip_ref_parts.append(
                            genai.types.Part.from_bytes(data=img_bytes, mime_type=mime)
                        )

            best_attempt: dict[str, Any] | None = None

            for attempt in range(1, STORYBOARD_MAX_ATTEMPTS + 1):
                try:
                    contents = clip_ref_parts + [working_prompt]
                    image_bytes_out, image_mime_type = await self._generate_storyboard_frame(
                        contents=contents,
                    )

                    critique = _normalize_image_critique(
                        await self._critique_image(
                            image_bytes=image_bytes_out,
                            image_mime_type=image_mime_type,
                            storyboard_text=clip.storyboard_text,
                            instructions=state.instructions,
                            image_prompt=working_prompt,
                            primary_reference_shot=primary_previous_shot,
                            continuity_reference_shots=previous_shots,
                            relevant_assets=relevant_assets,
                            asset_bytes=asset_bytes,
                            asset_lookup=asset_lookup,
                        )
                    )

                    critique_line = (
                        f"[Attempt {attempt}] Score: {critique['score']}/10 — {critique['reasoning']}"
                    )
                    if critique["hard_fail_reasons"]:
                        critique_line += f" | Hard fails: {', '.join(critique['hard_fail_reasons'])}"
                    if relevant_assets:
                        critique_line += f" | Refs used: {[a.get('id') for a in relevant_assets]}"
                    if primary_previous_shot:
                        critique_line += f" | Primary continuity: {primary_previous_shot.id}"
                    if len(previous_shots) > 1:
                        critique_line += (
                            " | Additional continuity: "
                            + str([shot.id for shot in previous_shots[1:STORYBOARD_MAX_REFERENCE_SHOTS]])
                        )
                    clip.image_critiques.append(critique_line)

                    candidate = {
                        "bytes": image_bytes_out,
                        "mime_type": image_mime_type,
                        "critique": critique,
                        "prompt": working_prompt,
                    }
                    candidate_rank = (
                        1 if critique["passes"] else 0,
                        critique["score"],
                        -len(critique["hard_fail_reasons"]),
                    )
                    if best_attempt is None:
                        best_attempt = candidate
                    else:
                        best_rank = (
                            1 if best_attempt["critique"]["passes"] else 0,
                            best_attempt["critique"]["score"],
                            -len(best_attempt["critique"]["hard_fail_reasons"]),
                        )
                        if candidate_rank > best_rank:
                            best_attempt = candidate

                    if critique["passes"]:
                        break

                    if attempt < STORYBOARD_MAX_ATTEMPTS:
                        working_prompt = (
                            f"{clip.storyboard_text}. {state.instructions}. "
                            f"Mandatory corrections before regeneration: {critique['suggestions']}. "
                            f"High quality, cinematic still frame, {self.image_aspect_ratio} aspect ratio, {self.image_size} detail. "
                            "Do not introduce extra limbs, extra people, missing heads, fused bodies, broken animals, or inconsistent character design."
                        )
                except Exception as e:
                    clip.image_critiques.append(
                        f"[Attempt {attempt}] {self.image_provider.definition.label} generation failed: {str(e)}"
                    )
                    break

            if best_attempt:
                image_path = PROJECTS_DIR / f"{state.project_id}_{clip.id}.png"
                os.makedirs(PROJECTS_DIR, exist_ok=True)
                with open(image_path, "wb") as f:
                    f.write(best_attempt["bytes"])
                clip.image_url = self._sync_local_project_artifact(
                    image_path,
                    relative_path=image_path.name,
                    content_type=best_attempt.get("mime_type"),
                )
                clip.image_prompt = best_attempt["prompt"]
                clip.image_score = best_attempt["critique"]["score"]
                clip.image_reference_ready = best_attempt["critique"]["passes"]
                clip.image_approved = best_attempt["critique"]["passes"]

        # Process all clips sequentially to avoid Google API rate limits (429 errors)
        for idx, clip in enumerate(state.timeline):
            self._check_cancelled()
            if not clip.image_approved:
                previous_reference_ready = [
                    c for c in state.timeline[:idx]
                    if c.image_url is not None and c.image_reference_ready
                ]
                relevant_prev_shots = await self._select_relevant_previous_shots(
                    clip,
                    previous_reference_ready,
                )

                relevant_assets = _normalize_relevant_assets(relevance_map.get(clip.id, []))

                await _process_clip(clip, relevant_assets, relevant_prev_shots)
                await self._persist_state(state)
                await asyncio.sleep(3) # Throttle to stay within free-tier RPM limits
        return state

    async def node_filming(self, state: ProjectState) -> ProjectState:
        """
        Node 3: Video Provider — Async Long-Running Operation
        1. Uploads the storyboard frame as an image initializer when the provider supports it.
        2. Starts the provider job and waits for completion.
        3. Saves the video bytes to disk so ffmpeg can stitch them later.
        """
        state.current_stage = AgentStage.FILMING
        state.last_error = None

        if not self.client:
             await self._persist_state(state)
             for clip in state.timeline:
                 self._check_cancelled()
                 if not clip.video_approved:
                     clip.video_url = "https://www.w3schools.com/html/mov_bbb.mp4"
                     await self._persist_state(state)
             state.current_stage = AgentStage.FILMING
             return state
            
        await self._normalize_timeline_for_veo(state)
        os.makedirs(PROJECTS_DIR, exist_ok=True)
        await self._persist_state(state)
        
        async def _process_filming(clip: VideoClip) -> None:
            clip.video_score = None
            motion_prompt = await self._build_video_motion_prompt(clip, state)
            clip.video_prompt = self._compose_video_generation_prompt(motion_prompt)
            
            try:
                image_path = _local_media_path(clip.image_url)
                try:
                    video_bytes = await self._generate_video_clip(
                        prompt=clip.video_prompt,
                        duration_seconds=int(clip.duration),
                        image_path=image_path,
                    )
                except Exception as first_error:
                    retry_motion_prompt = await self._build_video_retry_prompt(
                        clip,
                        state,
                        failed_prompt=motion_prompt,
                        failure_message=str(first_error),
                    )
                    retry_prompt = self._compose_video_generation_prompt(retry_motion_prompt)
                    if retry_prompt.strip() == clip.video_prompt.strip():
                        raise

                    clip.video_prompt = retry_prompt
                    try:
                        video_bytes = await self._generate_video_clip(
                            prompt=clip.video_prompt,
                            duration_seconds=int(clip.duration),
                            image_path=image_path,
                        )
                    except Exception as second_error:
                        raise RuntimeError(
                            f"{first_error} Retried with adjusted phrasing, but Veo still failed: {second_error}"
                        ) from second_error

                raw_video_path = PROJECTS_DIR / f"{state.project_id}_{clip.id}_raw.mp4"
                video_path = PROJECTS_DIR / f"{state.project_id}_{clip.id}.mp4"
                with open(raw_video_path, "wb") as f:
                    f.write(video_bytes)

                probed_width, probed_height = await self._probe_video_dimensions(str(raw_video_path))
                if (probed_width, probed_height) != (self.video_width, self.video_height):
                    await self._normalize_video_canvas(
                        input_path=str(raw_video_path),
                        output_path=str(video_path),
                        include_audio=True,
                    )
                    if os.path.exists(raw_video_path):
                        os.remove(raw_video_path)
                else:
                    os.replace(raw_video_path, video_path)
                clip.video_url = self._sync_local_project_artifact(
                    video_path,
                    relative_path=video_path.name,
                    content_type="video/mp4",
                )

                critique = await self._critique_video_frame(
                    video_path=video_path,
                    video_prompt=clip.video_prompt,
                    duration=clip.duration,
                )
                critique_reasoning = str(critique.get("reasoning") or "").strip() or "Automated video review unavailable."
                try:
                    raw_video_score = critique.get("score")
                    if raw_video_score is None or raw_video_score == "":
                        raise TypeError("score unavailable")
                    clip.video_score = int(float(raw_video_score))
                except (TypeError, ValueError):
                    clip.video_score = None
                if clip.video_score is None:
                    clip.video_critiques.append(critique_reasoning)
                else:
                    clip.video_critiques.append(
                        f"Score: {clip.video_score}/10 — {critique_reasoning}"
                    )

            except Exception as e:
                raw_video_path = PROJECTS_DIR / f"{state.project_id}_{clip.id}_raw.mp4"
                if os.path.exists(raw_video_path):
                    os.remove(raw_video_path)
                clip.video_critiques.append(
                    f"{self.video_provider.definition.label} generation failed: {str(e)}"
                )
                clip.video_url = None

        # Submit and poll all active clips sequentially to prevent rate limits
        for clip in state.timeline:
            self._check_cancelled()
            if not clip.video_approved:
                await _process_filming(clip)
                await self._persist_state(state)
                await asyncio.sleep(3)

        state.current_stage = AgentStage.FILMING
        return state

    def node_prepare_production(self, state: ProjectState) -> ProjectState:
        missing_outputs = [
            clip.id
            for clip in state.timeline
            if not clip.video_approved or not clip.video_url
        ]
        if missing_outputs and self.client:
            raise RuntimeError(
                "Cannot enter production until every clip has a generated video and approval. "
                f"Missing outputs: {', '.join(missing_outputs)}"
            )

        self._initialize_production_timeline(state)
        state.final_video_url = None
        state.last_error = None
        state.current_stage = AgentStage.PRODUCTION
        return state

    def _build_image_critic_contents(
        self,
        *,
        image_bytes: bytes,
        image_mime_type: str,
        primary_reference_shot: VideoClip | None,
        continuity_reference_shots: list[VideoClip],
        relevant_assets: list[dict[str, str]],
        asset_bytes: dict[str, tuple[bytes, str]],
        asset_lookup: dict[str, Any] | None = None,
    ) -> list[Any]:
        asset_lookup = asset_lookup or {}
        contents: list[Any] = [
            "GENERATED FRAME UNDER REVIEW:",
            genai.types.Part.from_bytes(data=image_bytes, mime_type=image_mime_type),
        ]

        included_reference_ids: list[str] = []
        for reference_shot in continuity_reference_shots[:STORYBOARD_MAX_REFERENCE_SHOTS]:
            reference_path = _local_media_path(reference_shot.image_url)
            if not reference_path or not os.path.exists(reference_path):
                continue
            label = (
                "PRIMARY GENERATION CONTINUITY REFERENCE"
                if primary_reference_shot and reference_shot.id == primary_reference_shot.id
                else f"ADDITIONAL CONTINUITY REFERENCE {len(included_reference_ids)}"
            )
            contents.append(f"{label}: {reference_shot.storyboard_text}")
            with open(reference_path, "rb") as f:
                contents.append(
                    genai.types.Part.from_bytes(data=f.read(), mime_type="image/png")
                )
            included_reference_ids.append(reference_shot.id)

        for asset in relevant_assets:
            asset_id = asset.get("id")
            asset_type = asset.get("type", "subject")
            if asset_id not in asset_bytes:
                continue
            asset_img_bytes, asset_mime = asset_bytes[asset_id]
            asset_label = display_asset_label(
                getattr(asset_lookup.get(asset_id), "label", None),
                getattr(asset_lookup.get(asset_id), "name", asset_id),
            )
            contents.append(
                f"GENERATION REFERENCE ASSET ({asset_type.upper()}): {asset_label} [{asset_id}]"
            )
            contents.append(
                genai.types.Part.from_bytes(data=asset_img_bytes, mime_type=asset_mime)
            )

        return contents

    def _build_image_critic_prompt(
        self,
        *,
        storyboard_text: str,
        instructions: str,
        image_prompt: str,
        reviewer_lens: str,
    ) -> str:
        return f"""\
You are one of 3 independent storyboard critics. Review this frame independently.

Reviewer lens:
{reviewer_lens}

Current shot brief:
"{storyboard_text}"

The exact generation prompt used for this attempt was:
"{image_prompt}"

Visual style guide:
"{instructions}"

Evaluate the generated frame on all of the following:
1. Faithfulness to the shot brief
2. Cinematic composition and framing
3. Style consistency with the guide
4. Technical quality / realism
5. Anatomy and artifact integrity
6. Subject identity consistency with the reference shots/assets
7. Scene and environment continuity with the reference shots

Hard-fail findings must be reserved for blocking issues that clearly justify rejection.

Rules:
- Only call severe defects when they are directly and clearly visible in the generated frame.
- If a limb, hand, head, face, or body part is occluded, cropped, tiny, blurred, stylized, or otherwise hard to inspect, do not guess.
- Uncertain concerns belong in reasoning/suggestions only, not hard-fail findings.
- Every hard-fail finding must cite direct visual evidence from the frame itself.

Return a single JSON object:
{{
  "score": <integer 0-10>,
  "passes": <true if you personally believe the frame is strong enough to keep>,
  "reasoning": "<2-4 concise sentences>",
  "suggestions": "<1-2 sentences describing exactly what to fix next>",
  "hard_fail_findings": [
    {{
      "reason": "<short defect label>",
      "category": "<anatomy|continuity|subject_count|object_integrity|scene|other>",
      "confidence": <number 0.0-1.0>,
      "evidence": "<brief direct visual evidence>"
    }}
  ]
}}"""

    async def _single_image_critic_pass(
        self,
        *,
        contents: list[Any],
        storyboard_text: str,
        instructions: str,
        image_prompt: str,
        reviewer_lens: str,
    ) -> dict[str, Any]:
        response = self.client.models.generate_content(
            model=self.critic_model,
            contents=contents + [
                self._build_image_critic_prompt(
                    storyboard_text=storyboard_text,
                    instructions=instructions,
                    image_prompt=image_prompt,
                    reviewer_lens=reviewer_lens,
                )
            ],
            config=self._critic_config(response_mime_type="application/json"),
        )
        return json.loads(response.text)

    async def _critique_image(
        self,
        *,
        image_bytes: bytes,
        image_mime_type: str,
        storyboard_text: str,
        instructions: str,
        image_prompt: str,
        primary_reference_shot: VideoClip | None,
        continuity_reference_shots: list[VideoClip],
        relevant_assets: list[dict[str, str]],
        asset_bytes: dict[str, tuple[bytes, str]],
        asset_lookup: dict[str, Any] | None = None,
    ) -> dict:
        """
        Uses a 3-critic panel to evaluate a provider-generated storyboard image.
        A frame only fails automatically when all 3 critics independently raise the same blocking concern.
        """
        try:
            contents = self._build_image_critic_contents(
                image_bytes=image_bytes,
                image_mime_type=image_mime_type,
                primary_reference_shot=primary_reference_shot,
                continuity_reference_shots=continuity_reference_shots,
                relevant_assets=relevant_assets,
                asset_bytes=asset_bytes,
                asset_lookup=asset_lookup,
            )
            panel_critiques = [
                await self._single_image_critic_pass(
                    contents=contents,
                    storyboard_text=storyboard_text,
                    instructions=instructions,
                    image_prompt=image_prompt,
                    reviewer_lens=reviewer_lens,
                )
                for reviewer_lens in IMAGE_CRITIC_LENSES
            ]
            return _build_panel_consensus_critique(
                panel_critiques,
                medium_label="image",
                pass_score=STORYBOARD_PASS_SCORE,
            )
        except Exception as e:
            return {
                "score": 0,
                "passes": False,
                "reasoning": f"Critique unavailable: {str(e)}",
                "suggestions": "Regenerate with stricter anatomy, correct subject count, intact hero objects, and stronger continuity to the supplied references.",
                "hard_fail_reasons": ["critique_unavailable"],
            }

    async def _critique_video_frame(
        self,
        video_path: str,
        video_prompt: str,
        duration: float,
    ) -> dict:
        """
        Extracts frames at 2 FPS from the generated clip using ffmpeg's fps filter,
        then sends all frames to the configured critic model in a single request to evaluate
        temporal consistency, artifacts, and overall quality.
        For an 8s clip this yields ~16 frames; each 500ms interval is covered.
        Returns {"score": int (1-10), "reasoning": str, "suggestions": str}.
        """
        import tempfile
        frame_paths: list[str] = []
        tmp_dir = tempfile.mkdtemp(prefix="fmv_critique_")

        try:
            audio_analysis = await self._analyze_generated_video_audio(video_path)
            audio_music_classification = await self._classify_generated_video_audio_content(
                video_path=video_path,
                audio_analysis=audio_analysis,
            )

            # ── Extract at 2 FPS into a temp directory ────────────────────────
            frame_pattern = os.path.join(tmp_dir, "frame_%04d.png")
            proc = await asyncio.create_subprocess_exec(
                FFMPEG, "-y",
                "-i", video_path,
                "-vf", "fps=2",
                "-q:v", "3",
                frame_pattern,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            # Collect frames in order
            frame_paths = sorted([
                os.path.join(tmp_dir, f)
                for f in os.listdir(tmp_dir)
                if f.endswith(".png")
            ])

            if not frame_paths:
                raise RuntimeError("ffmpeg extracted no frames at 2 FPS")

            # ── Upload all frames ────────────────────────────────────────────
            uploaded_files = []
            for fp in frame_paths:
                uploaded_files.append(self._content_part_from_local_file(fp, mime_type="image/png"))

            # Build contents: interleave timestamp label + image for each frame
            contents = []
            interval = 0.5  # seconds between frames at 2 FPS
            for idx, uf in enumerate(uploaded_files):
                t = idx * interval
                contents.append(f"Frame {idx + 1} at t={t:.1f}s:")
                contents.append(uf)

            contents.append(f"""\
Audio inspection from ffmpeg/ffprobe:
- has_audio_stream: {audio_analysis["has_audio_stream"]}
- audible_audio_detected: {audio_analysis["audible_audio_detected"]}
- mean_volume_db: {audio_analysis["mean_volume_db"] if audio_analysis["mean_volume_db"] is not None else "unknown"}
- max_volume_db: {audio_analysis["max_volume_db"] if audio_analysis["max_volume_db"] is not None else "unknown"}
- audio_reasoning: {audio_analysis["reasoning"]}
- contains_music: {audio_music_classification["contains_music"]}
- music_reasoning: {audio_music_classification["reasoning"]}

This clip must not contain generated music, vocals, singing, beat, soundtrack, or score.
Diegetic sound effects or ambience alone are acceptable.""")

            panel_critiques: list[dict[str, Any]] = []
            for reviewer_lens in VIDEO_CRITIC_LENSES:
                prompt = f"""\
You are one of 3 independent video critics reviewing sampled frames from an AI-generated clip.

Reviewer lens:
{reviewer_lens}

The clip was supposed to show:
"{video_prompt}"

Study every frame carefully. Evaluate holistically on:
1. Scene faithfulness throughout the clip
2. Temporal consistency of subjects, backgrounds, and lighting
3. Artifact detection such as flicker, morphing, ghosting, blur, or deformation
4. Cinematic quality of motion, composition, and readability
5. Audio contamination — generated music, vocals, soundtrack, beat, or score is unacceptable; diegetic sound effects alone are acceptable

Rules:
- Hard-fail findings are only for blocking issues that clearly justify rejecting the clip.
- If a claimed defect is ambiguous, intermittent, or hard to verify from the sampled frames, keep it out of hard-fail findings.
- Cite direct frame or timestamp evidence for every hard-fail finding.

Return a single JSON object:
{{
  "score": <integer 0-10>,
  "passes": <true if you personally believe the clip is strong enough to keep>,
  "reasoning": "<2-4 concise sentences referencing visible evidence or timestamps>",
  "suggestions": "<1-2 concise sentences>",
  "hard_fail_findings": [
    {{
      "reason": "<short defect label>",
      "category": "<scene_faithfulness|temporal_consistency|artifact|cinematic_quality|audio_music|other>",
      "confidence": <number 0.0-1.0>,
      "evidence": "<brief visual or temporal evidence>"
    }}
  ]
}}"""
                response = self.client.models.generate_content(
                    model=self.critic_model,
                    contents=contents + [prompt],
                    config=self._critic_config(response_mime_type="application/json"),
                )
                panel_critiques.append(json.loads(response.text))

            critique = _build_panel_consensus_critique(
                panel_critiques,
                medium_label="video",
                pass_score=STORYBOARD_PASS_SCORE,
            )
            if audio_music_classification["contains_music"]:
                base_reasoning = str(critique.get("reasoning") or "").strip()
                audio_reasoning = str(audio_music_classification["reasoning"]).strip()
                critique["score"] = min(int(critique.get("score", 0) or 0), 3)
                critique["passes"] = False
                critique["reasoning"] = " ".join(
                    part for part in [
                        base_reasoning,
                        f"Music hard fail: {audio_reasoning}",
                    ] if part
                )
                critique["suggestions"] = (
                    "Regenerate without any music, vocals, singing, soundtrack, beat, or score. Ambient diegetic sound effects are acceptable."
                )
            return critique

        except Exception as e:
            detail = str(e).strip()
            return {
                "score": None,
                "reasoning": (
                    f"Automated video review unavailable: {detail}"
                    if detail
                    else "Automated video review unavailable."
                ),
                "suggestions": "",
            }
        finally:
            # Clean up temp directory and all extracted frames
            import shutil as _shutil
            _shutil.rmtree(tmp_dir, ignore_errors=True)

    async def _analyze_generated_video_audio(self, video_path: str) -> dict[str, Any]:
        result = {
            "has_audio_stream": False,
            "audible_audio_detected": False,
            "mean_volume_db": None,
            "max_volume_db": None,
            "reasoning": "No audio stream detected.",
        }

        try:
            probe_proc = await asyncio.create_subprocess_exec(
                FFPROBE,
                "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=index",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await probe_proc.communicate()
            if probe_proc.returncode != 0 or not stdout.decode().strip():
                return result

            result["has_audio_stream"] = True

            vol_proc = await asyncio.create_subprocess_exec(
                FFMPEG, "-i", video_path,
                "-vn",
                "-af", "volumedetect",
                "-f", "null",
                os.devnull,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await vol_proc.communicate()
            analysis_text = stderr.decode()

            mean_match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", analysis_text)
            max_match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", analysis_text)
            mean_volume = float(mean_match.group(1)) if mean_match else None
            max_volume = float(max_match.group(1)) if max_match else None
            result["mean_volume_db"] = mean_volume
            result["max_volume_db"] = max_volume

            audible_audio = False
            if max_volume is not None and max_volume > -45:
                audible_audio = True
            if mean_volume is not None and mean_volume > -55:
                audible_audio = True

            result["audible_audio_detected"] = audible_audio
            if audible_audio:
                result["reasoning"] = (
                    "The generated clip contains an audible audio track. "
                    f"Measured mean volume: {mean_volume if mean_volume is not None else 'unknown'} dB, "
                    f"max volume: {max_volume if max_volume is not None else 'unknown'} dB."
                )
            else:
                result["reasoning"] = "An audio stream exists, but it appears effectively silent."

            return result
        except Exception as e:
            return {
                **result,
                "reasoning": f"Audio analysis unavailable: {str(e)}",
            }

    async def _classify_generated_video_audio_content(
        self,
        *,
        video_path: str,
        audio_analysis: dict[str, Any],
    ) -> dict[str, Any]:
        result = {
            "contains_music": False,
            "reasoning": "No audible audio detected.",
        }
        if not audio_analysis.get("audible_audio_detected"):
            return result
        if not self.client:
            return {
                "contains_music": False,
                "reasoning": "Music classification unavailable because no Gemini client is configured.",
            }

        audio_extract_path = f"{video_path}.critique.wav"
        try:
            extract_proc = await asyncio.create_subprocess_exec(
                FFMPEG, "-y",
                "-i", video_path,
                "-vn",
                "-ac", "1",
                "-ar", "16000",
                "-c:a", "pcm_s16le",
                audio_extract_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await extract_proc.communicate()
            if extract_proc.returncode != 0 or not os.path.exists(audio_extract_path):
                return {
                    "contains_music": False,
                    "reasoning": f"Music classification unavailable because audio extraction failed: {stderr.decode()}",
                }

            uploaded_audio = self._content_part_from_local_file(
                audio_extract_path,
                mime_type="audio/wav",
            )
            prompt = """\
You are reviewing the audio track from an AI-generated video clip.

Determine whether the audio contains MUSICAL content. Musical content includes:
- instrumental music
- rhythmic beat-driven backing
- melodic or harmonic score
- singing, chanting, humming, or vocals intended as music

Acceptable audio that should NOT count as music:
- diegetic sound effects
- ambience
- foley
- environmental noise
- non-musical character or object sounds

Return a single JSON object:
{"contains_music": <true_or_false>, "reasoning": "<1-2 concise sentences>"}"""

            response = self.client.models.generate_content(
                model=self.critic_model,
                contents=[
                    "AUDIO TRACK UNDER REVIEW:",
                    uploaded_audio,
                    prompt,
                ],
                config=self._critic_config(response_mime_type="application/json"),
            )
            data = json.loads(response.text)
            return {
                "contains_music": bool(data.get("contains_music")),
                "reasoning": str(data.get("reasoning") or "Music classification returned no reasoning."),
            }
        except Exception as e:
            return {
                "contains_music": False,
                "reasoning": f"Music classification unavailable: {str(e)}",
            }
        finally:
            if os.path.exists(audio_extract_path):
                os.remove(audio_extract_path)

    async def _normalize_clip_for_production(
        self,
        *,
        input_path: str,
        output_path: str,
        source_start: float = 0.0,
        duration: float | None = None,
    ) -> None:
        """
        Re-encode a clip into a uniform MP4 format so user-supplied footage can be
        stitched alongside generated clips without codec/container mismatches.
        """
        await self._normalize_video_canvas(
            input_path=input_path,
            output_path=output_path,
            include_audio=False,
            source_start=source_start,
            duration=duration,
        )

    async def _extract_clip_audio_for_production(
        self,
        *,
        input_path: str,
        output_path: str,
        source_start: float,
        duration: float,
    ) -> None:
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-y",
            "-ss", f"{source_start:.3f}",
            "-i", input_path,
            "-t", f"{duration:.3f}",
            "-vn",
            "-ac", "2",
            "-ar", "48000",
            "-c:a", "aac",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError(
                f"ffmpeg failed while extracting audio from '{input_path}': {stderr.decode()}"
            )

    async def _concatenate_audio_segments_for_production(
        self,
        *,
        segment_paths: list[str],
        output_path: str,
    ) -> None:
        concat_list_path = f"{output_path}.concat.txt"
        with open(concat_list_path, "w", encoding="utf-8") as handle:
            for path in segment_paths:
                handle.write(f"file '{os.path.abspath(path)}'\n")

        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-y",
            "-f", "concat", "-safe", "0", "-i", concat_list_path,
            "-c", "copy",
            output_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError(
                f"ffmpeg failed while concatenating audio into '{output_path}': {stderr.decode()}"
            )

    async def _create_silent_audio_for_production(
        self,
        *,
        output_path: str,
        duration: float,
    ) -> None:
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-y",
            "-f", "lavfi",
            "-i", "anullsrc=r=48000:cl=stereo",
            "-t", f"{duration:.3f}",
            "-c:a", "aac",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError(
                f"ffmpeg failed while creating silent audio '{output_path}': {stderr.decode()}"
            )

    async def _mux_production_segment(
        self,
        *,
        video_path: str,
        audio_path: str,
        output_path: str,
    ) -> None:
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-y",
            "-i", video_path,
            "-i", audio_path,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError(
                f"ffmpeg failed while muxing production segment '{output_path}': {stderr.decode()}"
            )


    async def node_production(self, state: ProjectState) -> ProjectState:
        """
        Node 4: Music + ffmpeg Compilation
        1. If no music_url and a compatible API music model is enabled, generate a song from lyrics_prompt and style_prompt.
        2. Uses the persisted production edit timeline to cut and reorder source clips.
        3. Mixes the music track into the final video.
        """
        project_dir = PROJECTS_DIR
        os.makedirs(project_dir, exist_ok=True)
        previous_final_url = None
        previous_final_path = None
        if state.final_video_url and "w3schools" not in state.final_video_url:
            candidate_path = _local_media_path(state.final_video_url)
            if candidate_path and os.path.exists(candidate_path) and os.path.getsize(candidate_path) > 0:
                previous_final_url = state.final_video_url
                previous_final_path = candidate_path

        self._reconcile_production_timeline(state)

        video_fragments = [
            fragment
            for fragment in sorted(
                state.production_timeline,
                key=lambda item: (item.timeline_start, item.id),
            )
            if fragment.duration > 0 and (fragment.track_type or "video") != "music"
        ]
        music_fragments = [
            fragment
            for fragment in sorted(
                state.production_timeline,
                key=lambda item: (item.timeline_start, item.id),
            )
            if fragment.duration > 0 and (fragment.track_type or "video") == "music"
        ]
        edited_total_duration = sum(fragment.duration for fragment in video_fragments)
        clip_lookup = {clip.id: clip for clip in state.timeline}

        missing_outputs = sorted({
            fragment.source_clip_id
            for fragment in video_fragments
            if (
                fragment.source_clip_id not in clip_lookup
                or not clip_lookup[fragment.source_clip_id].video_approved
                or not clip_lookup[fragment.source_clip_id].video_url
            )
        })
        if missing_outputs and self.client:
            raise RuntimeError(
                "Cannot produce final video until every clip has a generated video and approval. "
                f"Missing outputs: {', '.join(missing_outputs)}"
            )

        # ── Step 1: Use the song already attached in the Music stage ─────────
        if not state.music_url:
            # Music generation is user-driven in the Music stage. Production can
            # still render a silent master for older projects that have no song.
            pass

        # ── Step 2: Build the edited source sequence from production fragments ─
        video_sources: list[tuple[ProductionTimelineFragment, VideoClip, str]] = []
        missing_local_videos = []
        for fragment in video_fragments:
            clip = clip_lookup.get(fragment.source_clip_id)
            if not clip or not clip.video_url:
                missing_local_videos.append(fragment.id)
                continue
            if clip.video_url.startswith("http"):
                continue
            abs_path = _local_media_path(clip.video_url)
            if not abs_path:
                missing_local_videos.append(fragment.id)
                continue
            if os.path.exists(abs_path) and os.path.getsize(abs_path) > 0:
                video_sources.append((fragment, clip, abs_path))
            else:
                missing_local_videos.append(fragment.id)

        final_video_path = project_dir / f"{state.project_id}_final.mp4"
        sequence_path = project_dir / f"{state.project_id}_sequence.mp4"

        if missing_local_videos and self.client:
            raise RuntimeError(
                "Cannot produce final video because some edited fragments are missing or empty on disk: "
                f"{', '.join(missing_local_videos)}"
            )

        if not video_sources:
            state.last_error = "Cannot produce final video because no edited clips are available on disk."
            state.final_video_url = previous_final_url
            state.current_stage = AgentStage.PRODUCTION
            return state

        try:
            segment_paths = []
            for fragment, clip, source_path in video_sources:
                max_source_start = max(0.0, float(clip.duration) - 0.1)
                source_start = min(max(0.0, float(fragment.source_start)), max_source_start)
                remaining_duration = max(0.1, float(clip.duration) - source_start)
                segment_duration = min(max(0.1, float(fragment.duration)), remaining_duration)

                normalized_video_path = project_dir / f"{state.project_id}_{fragment.id}_video.mp4"
                audio_segment_path = project_dir / f"{state.project_id}_{fragment.id}_audio.m4a"
                segment_path = project_dir / f"{state.project_id}_{fragment.id}_segment.mp4"

                await self._normalize_clip_for_production(
                    input_path=source_path,
                    output_path=str(normalized_video_path),
                    source_start=source_start,
                    duration=segment_duration,
                )
                if fragment.audio_enabled:
                    try:
                        await self._extract_clip_audio_for_production(
                            input_path=source_path,
                            output_path=str(audio_segment_path),
                            source_start=source_start,
                            duration=segment_duration,
                        )
                    except RuntimeError:
                        await self._create_silent_audio_for_production(
                            output_path=str(audio_segment_path),
                            duration=segment_duration,
                        )
                else:
                    await self._create_silent_audio_for_production(
                        output_path=str(audio_segment_path),
                        duration=segment_duration,
                    )
                await self._mux_production_segment(
                    video_path=str(normalized_video_path),
                    audio_path=str(audio_segment_path),
                    output_path=str(segment_path),
                )
                segment_paths.append(segment_path)

            # Write a temporary concat list file
            concat_list_path = project_dir / f"{state.project_id}_concat.txt"
            with open(concat_list_path, "w") as f:
                for path in segment_paths:
                    f.write(f"file '{os.path.abspath(path)}'\n")

            concat_proc = await asyncio.create_subprocess_exec(
                FFMPEG, "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_list_path),
                "-c", "copy",
                str(sequence_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, concat_stderr = await concat_proc.communicate()
            if concat_proc.returncode != 0:
                raise RuntimeError(f"ffmpeg failed while building the production sequence: {concat_stderr.decode()}")

            music_path = _local_media_path(state.music_url)
            if music_path and os.path.exists(music_path):
                effective_music_fragments = music_fragments or [
                    ProductionTimelineFragment(
                        id="music_frag_0",
                        track_type="music",
                        source_clip_id=None,
                        timeline_start=0.0,
                        source_start=0.0,
                        duration=round(edited_total_duration, 3),
                        audio_enabled=True,
                    )
                ]

                music_segment_paths: list[str] = []
                current_music_time = 0.0
                for index, fragment in enumerate(effective_music_fragments):
                    fragment_start = max(0.0, float(fragment.timeline_start))
                    if fragment_start > current_music_time + 1e-3:
                        gap_duration = fragment_start - current_music_time
                        gap_path = project_dir / f"{state.project_id}_music_gap_{index}.m4a"
                        await self._create_silent_audio_for_production(
                            output_path=str(gap_path),
                            duration=gap_duration,
                        )
                        music_segment_paths.append(str(gap_path))
                        current_music_time = fragment_start

                    segment_duration = min(
                        max(0.1, float(fragment.duration)),
                        max(0.1, edited_total_duration - current_music_time),
                    )
                    music_segment_path = project_dir / f"{state.project_id}_{fragment.id}_music.m4a"
                    try:
                        await self._extract_clip_audio_for_production(
                            input_path=music_path,
                            output_path=str(music_segment_path),
                            source_start=max(0.0, float(fragment.source_start)),
                            duration=segment_duration,
                        )
                    except RuntimeError:
                        await self._create_silent_audio_for_production(
                            output_path=str(music_segment_path),
                            duration=segment_duration,
                        )
                    music_segment_paths.append(str(music_segment_path))
                    current_music_time = fragment_start + segment_duration

                if current_music_time < edited_total_duration - 1e-3:
                    tail_silence_path = project_dir / f"{state.project_id}_music_tail.m4a"
                    await self._create_silent_audio_for_production(
                        output_path=str(tail_silence_path),
                        duration=edited_total_duration - current_music_time,
                    )
                    music_segment_paths.append(str(tail_silence_path))

                if music_segment_paths:
                    music_bed_path = project_dir / f"{state.project_id}_music_bed.m4a"
                    await self._concatenate_audio_segments_for_production(
                        segment_paths=music_segment_paths,
                        output_path=str(music_bed_path),
                    )

                    # Mix the edited clip audio with the arranged music bed.
                    proc = await asyncio.create_subprocess_exec(
                        FFMPEG, "-y",
                        "-i", str(sequence_path),
                        "-i", str(music_bed_path),
                        "-filter_complex", "[0:a:0][1:a:0]amix=inputs=2:duration=first:dropout_transition=2[aout]",
                        "-map", "0:v:0",
                        "-map", "[aout]",
                        "-c:v", "copy",
                        "-c:a", "aac",
                        "-shortest",
                        str(final_video_path),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                else:
                    shutil.copyfile(sequence_path, final_video_path)
                    proc = None
            else:
                shutil.copyfile(sequence_path, final_video_path)
                proc = None

            if proc is not None:
                _, stderr = await proc.communicate()

                if proc.returncode != 0:
                    raise RuntimeError(f"ffmpeg failed: {stderr.decode()}")
            if not os.path.exists(final_video_path) or os.path.getsize(final_video_path) == 0:
                raise RuntimeError("ffmpeg reported success but produced an empty final video file")

            state.final_video_url = self._sync_local_project_artifact(
                final_video_path,
                relative_path=final_video_path.name,
                content_type="video/mp4",
            ).split("?", 1)[0]
            state.last_error = None

        except (FileNotFoundError, OSError) as e:
            state.last_error = f"ffmpeg unavailable: {str(e)}"
            state.final_video_url = previous_final_url
            state.current_stage = AgentStage.PRODUCTION
            return state
        except RuntimeError as e:
            state.last_error = str(e)
            state.final_video_url = previous_final_url
            state.current_stage = AgentStage.PRODUCTION
            return state

        state.current_stage = AgentStage.COMPLETED
        return state

