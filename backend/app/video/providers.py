from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agent.graph import FMVAgentPipeline


GOOGLE_VEO_PROVIDER = "google-veo"
DEFAULT_VIDEO_PROVIDER = GOOGLE_VEO_PROVIDER


@dataclass(frozen=True)
class VideoProviderDefinition:
    id: str
    label: str
    description: str
    official: bool
    default_model: str


@dataclass(frozen=True)
class VideoGenerationReferenceAsset:
    path: str
    label: str
    kind: str = "subject"
    source_asset_id: str | None = None


class BaseVideoProvider:
    definition: VideoProviderDefinition

    async def generate_clip(
        self,
        pipeline: FMVAgentPipeline,
        *,
        prompt: str,
        duration_seconds: int,
        image_path: str | None,
        reference_assets: list[VideoGenerationReferenceAsset] | None = None,
        job_started_callback=None,
        heartbeat_callback=None,
    ) -> bytes:
        raise NotImplementedError


class GoogleVeoProvider(BaseVideoProvider):
    definition = VideoProviderDefinition(
        id=GOOGLE_VEO_PROVIDER,
        label="Google Veo",
        description="Google Veo video generation provider for filming-stage clip synthesis.",
        official=True,
        default_model="veo-3.1-fast-generate-001",
    )

    async def generate_clip(
        self,
        pipeline: FMVAgentPipeline,
        *,
        prompt: str,
        duration_seconds: int,
        image_path: str | None,
        reference_assets: list[VideoGenerationReferenceAsset] | None = None,
        job_started_callback=None,
        heartbeat_callback=None,
    ) -> bytes:
        return await pipeline._generate_google_video_clip(
            prompt=prompt,
            duration_seconds=duration_seconds,
            image_path=image_path,
            reference_assets=reference_assets or [],
            job_started_callback=job_started_callback,
            heartbeat_callback=heartbeat_callback,
        )


PROVIDERS: dict[str, BaseVideoProvider] = {
    GOOGLE_VEO_PROVIDER: GoogleVeoProvider(),
}

LEGACY_VIDEO_MODEL_ALIASES = {
    "veo-3.1-fast": "veo-3.1-fast-generate-001",
    "veo-3.1-quality": "veo-3.1-generate-001",
    "veo-3.1-fast-generate-preview": "veo-3.1-fast-generate-001",
    "veo-3.1-generate-preview": "veo-3.1-generate-001",
}


def resolve_video_provider_selection(selection: str | None) -> tuple[str, str]:
    normalized = (selection or "").strip()
    if not normalized:
        provider = PROVIDERS[DEFAULT_VIDEO_PROVIDER]
        return provider.definition.id, provider.definition.default_model

    if normalized in PROVIDERS:
        provider = PROVIDERS[normalized]
        return provider.definition.id, provider.definition.default_model

    model_name = LEGACY_VIDEO_MODEL_ALIASES.get(normalized, normalized)
    provider = PROVIDERS[DEFAULT_VIDEO_PROVIDER]
    return provider.definition.id, model_name


def get_video_provider(provider_id: str | None) -> BaseVideoProvider:
    resolved_id, _ = resolve_video_provider_selection(provider_id)
    return PROVIDERS[resolved_id]


def list_video_provider_definitions() -> list[VideoProviderDefinition]:
    return [provider.definition for provider in PROVIDERS.values()]
