from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable

from google import genai

from app.core.document_context import display_asset_label, normalize_document_text

DEFAULT_ASSET_ANALYSIS_MODEL = "gemini-3-flash-preview"
ASSET_ANALYSIS_MAX_CHARS = 1600


def _find_binary(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    winget_path = os.path.expanduser(r"~\AppData\Local\Microsoft\WinGet\Packages")
    if os.path.isdir(winget_path):
        executable = f"{name}.exe" if os.name == "nt" else name
        for root, _, files in os.walk(winget_path):
            if executable in files:
                return os.path.join(root, executable)
    return name


FFMPEG = _find_binary("ffmpeg")
FFPROBE = _find_binary("ffprobe")


def normalize_asset_context_text(text: str | None, *, max_chars: int = ASSET_ANALYSIS_MAX_CHARS) -> str | None:
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    if not collapsed:
        return None
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[: max_chars - 1].rstrip() + "…"


def build_document_context(assets: Iterable[Any], *, max_chars: int = 6000) -> str:
    snippets: list[str] = []
    current_length = 0
    for asset in assets:
        if getattr(asset, "type", None) != "document" or not getattr(asset, "text_content", None):
            continue
        snippet = (
            f"{display_asset_label(getattr(asset, 'label', None), getattr(asset, 'name', None))}:\n"
            f"{normalize_document_text(getattr(asset, 'text_content', ''), max_chars=max_chars)}"
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


def build_asset_reference_registry(assets: Iterable[Any], *, max_chars: int = 2000) -> str:
    entries: list[str] = []
    current_length = 0
    for asset in assets:
        label = display_asset_label(getattr(asset, "label", None), getattr(asset, "name", None))
        asset_type = getattr(asset, "type", None)
        if asset_type == "image":
            entry = (
                f'- image "{label}" (file: {getattr(asset, "name", "")}). '
                f"If this label names a character, creature, hero prop, vehicle, or location from the screenplay, "
                f"treat this image as the canonical visual reference for that named entity."
            )
        elif asset_type == "document":
            entry = (
                f'- document "{label}" (file: {getattr(asset, "name", "")}). '
                f"Supplemental written context for the screenplay, lore, and world details."
            )
        elif asset_type == "audio":
            entry = f'- audio "{label}" (file: {getattr(asset, "name", "")}). Music or sound reference available to the project.'
        elif asset_type == "video":
            entry = f'- video "{label}" (file: {getattr(asset, "name", "")}). Motion reference available to the project.'
        else:
            entry = f'- {asset_type} "{label}" (file: {getattr(asset, "name", "")}).'

        ai_context = normalize_asset_context_text(getattr(asset, "ai_context", None), max_chars=420)
        if ai_context:
            entry += f" AI-understood context: {ai_context}"

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


def build_asset_semantic_context(assets: Iterable[Any], *, max_chars: int = 4000) -> str:
    snippets: list[str] = []
    current_length = 0
    for asset in assets:
        ai_context = normalize_asset_context_text(getattr(asset, "ai_context", None), max_chars=900)
        if not ai_context:
            continue
        label = display_asset_label(getattr(asset, "label", None), getattr(asset, "name", None))
        snippet = f'{label} ({getattr(asset, "type", "asset")}): {ai_context}'
        if current_length + len(snippet) > max_chars:
            remaining = max_chars - current_length
            if remaining <= 0:
                break
            snippet = snippet[:remaining].rstrip() + "…"
        snippets.append(snippet)
        current_length += len(snippet)
        if current_length >= max_chars:
            break
    return "\n".join(snippets)


async def _run_generate_content(
    client: genai.Client,
    *,
    prompt: str,
    parts: list[Any],
    model: str = DEFAULT_ASSET_ANALYSIS_MODEL,
) -> str | None:
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=[prompt, *parts],
        config=genai.types.GenerateContentConfig(
            temperature=0.2,
            thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
        ),
    )
    return normalize_asset_context_text(getattr(response, "text", None))


async def _probe_media_metadata(path: str) -> dict[str, Any]:
    try:
        proc = await asyncio.create_subprocess_exec(
            FFPROBE,
            "-v", "error",
            "-show_entries", "format=duration,size:stream=codec_type,width,height,codec_name",
            "-of", "json",
            path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return {}
        return json.loads(stdout.decode() or "{}")
    except Exception:
        return {}


def _describe_media_metadata(metadata: dict[str, Any]) -> str:
    format_info = metadata.get("format") or {}
    streams = metadata.get("streams") or []
    duration = format_info.get("duration")
    duration_text = ""
    if duration:
        try:
            duration_text = f"duration≈{float(duration):.1f}s"
        except Exception:
            duration_text = ""
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    parts: list[str] = []
    if duration_text:
        parts.append(duration_text)
    if video_stream:
        width = video_stream.get("width")
        height = video_stream.get("height")
        codec_name = video_stream.get("codec_name")
        if width and height:
            parts.append(f"video={width}x{height}")
        if codec_name:
            parts.append(f"video_codec={codec_name}")
    if audio_stream:
        codec_name = audio_stream.get("codec_name")
        if codec_name:
            parts.append(f"audio_codec={codec_name}")
    return ", ".join(parts)


async def _sample_audio_for_analysis(source_path: str) -> tuple[bytes | None, str | None]:
    with tempfile.TemporaryDirectory(prefix="fmv-audio-analysis-") as temp_dir:
        output_path = os.path.join(temp_dir, "sample.wav")
        proc = await asyncio.create_subprocess_exec(
            FFMPEG,
            "-y",
            "-i", source_path,
            "-t", "60",
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            output_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, _ = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(output_path):
            return None, None
        return Path(output_path).read_bytes(), "audio/wav"


async def _sample_video_frames_for_analysis(source_path: str) -> list[tuple[bytes, str]]:
    with tempfile.TemporaryDirectory(prefix="fmv-video-analysis-") as temp_dir:
        output_pattern = os.path.join(temp_dir, "frame_%02d.jpg")
        proc = await asyncio.create_subprocess_exec(
            FFMPEG,
            "-y",
            "-i", source_path,
            "-vf", "fps=1/4,scale=1280:-1",
            "-frames:v", "4",
            output_pattern,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, _ = await proc.communicate()
        if proc.returncode != 0:
            return []
        frames: list[tuple[bytes, str]] = []
        for frame_path in sorted(Path(temp_dir).glob("frame_*.jpg")):
            frames.append((frame_path.read_bytes(), "image/jpeg"))
        return frames


async def _with_temporary_source_path(
    *,
    filename: str | None,
    mime_type: str | None,
    content: bytes | None,
    local_path: str | None,
) -> tuple[str | None, Any | None]:
    if local_path and os.path.exists(local_path):
        return local_path, None
    if content is None:
        return None, None
    guessed_extension = os.path.splitext(filename or "")[1] or (mimetypes.guess_extension(mime_type or "") or "")
    temp_dir = tempfile.TemporaryDirectory(prefix="fmv-asset-analysis-")
    temp_path = os.path.join(temp_dir.name, f"asset{guessed_extension or ''}")
    Path(temp_path).write_bytes(content)
    return temp_path, temp_dir


async def analyze_uploaded_asset(
    *,
    client: genai.Client | None,
    filename: str | None,
    label: str | None,
    mime_type: str | None,
    asset_type: str,
    content: bytes | None = None,
    local_path: str | None = None,
    extracted_text: str | None = None,
) -> str | None:
    fallback_label = display_asset_label(label, filename)

    if asset_type == "document":
        if not extracted_text:
            return None
        normalized_text = normalize_document_text(extracted_text, max_chars=12000)
        if not client:
            return normalize_asset_context_text(normalized_text, max_chars=1200)
        prompt = (
            f'You are summarizing an uploaded document for a music-video production agent.\n'
            f'Label: {fallback_label}\n'
            "Extract the facts, characters, locations, world rules, visual motifs, tone, and story information that should influence planning and generation.\n"
            "Return a concise plain-text production brief, not bullet points."
        )
        return await _run_generate_content(client, prompt=prompt, parts=[normalized_text])

    if asset_type == "image":
        if not content and local_path and os.path.exists(local_path):
            content = Path(local_path).read_bytes()
        if not content:
            return None
        if not client:
            return f"Reference image labeled {fallback_label}."
        prompt = (
            f'You are summarizing an uploaded image reference for a music-video production agent.\n'
            f'Label: {fallback_label}\n'
            "Describe the primary subjects, identity-defining traits, wardrobe, props, environment, mood, style, and continuity cues that should be preserved in generation.\n"
            "Assume the label is semantic ground truth if it names a character, object, creature, vehicle, or location.\n"
            "Return a concise plain-text production brief."
        )
        mime = mime_type or mimetypes.guess_type(filename or "")[0] or "image/png"
        return await _run_generate_content(
            client,
            prompt=prompt,
            parts=[genai.types.Part.from_bytes(data=content, mime_type=mime)],
        )

    source_path, temp_dir = await _with_temporary_source_path(
        filename=filename,
        mime_type=mime_type,
        content=content,
        local_path=local_path,
    )
    try:
        metadata = await _probe_media_metadata(source_path) if source_path else {}
        metadata_text = _describe_media_metadata(metadata)

        if asset_type == "audio":
            if not source_path:
                return f"Audio reference labeled {fallback_label}."
            if not client:
                return normalize_asset_context_text(
                    f'Audio reference "{fallback_label}" ({metadata_text}).'
                )
            sample_bytes, sample_mime = await _sample_audio_for_analysis(source_path)
            if not sample_bytes:
                return normalize_asset_context_text(
                    f'Audio reference "{fallback_label}" ({metadata_text}).'
                )
            prompt = (
                f'You are summarizing an uploaded audio reference for a music-video production agent.\n'
                f'Label: {fallback_label}\n'
                f'Metadata: {metadata_text or "(none)"}\n'
                "Describe genre, energy, pacing, instrumentation or texture, emotional tone, any audible lyrics or vocal character, and the visual or narrative cues this audio suggests.\n"
                "Return a concise plain-text production brief."
            )
            return await _run_generate_content(
                client,
                prompt=prompt,
                parts=[genai.types.Part.from_bytes(data=sample_bytes, mime_type=sample_mime or "audio/wav")],
            )

        if asset_type == "video":
            if not source_path:
                return f"Video reference labeled {fallback_label}."
            if not client:
                return normalize_asset_context_text(
                    f'Video reference "{fallback_label}" ({metadata_text}).'
                )
            sampled_frames = await _sample_video_frames_for_analysis(source_path)
            if not sampled_frames:
                return normalize_asset_context_text(
                    f'Video reference "{fallback_label}" ({metadata_text}).'
                )
            prompt = (
                f'You are summarizing an uploaded video reference for a music-video production agent.\n'
                f'Label: {fallback_label}\n'
                f'Metadata: {metadata_text or "(none)"}\n'
                "Using the sampled frames, describe the main subjects, environments, actions, cinematography, editing feel, style, and continuity cues that should inform planning or generation.\n"
                "Return a concise plain-text production brief."
            )
            parts: list[Any] = []
            for frame_bytes, frame_mime in sampled_frames:
                parts.append(genai.types.Part.from_bytes(data=frame_bytes, mime_type=frame_mime))
            return await _run_generate_content(client, prompt=prompt, parts=parts)

        return None
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()
