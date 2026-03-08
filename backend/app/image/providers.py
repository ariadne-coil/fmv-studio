from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.agent.graph import FMVAgentPipeline


GOOGLE_GEMINI_IMAGE_PROVIDER = "google-gemini-image"
DEFAULT_IMAGE_PROVIDER = GOOGLE_GEMINI_IMAGE_PROVIDER


@dataclass(frozen=True)
class ImageProviderDefinition:
    id: str
    label: str
    description: str
    official: bool
    default_model: str


class BaseImageProvider:
    definition: ImageProviderDefinition

    async def generate_frame(
        self,
        pipeline: FMVAgentPipeline,
        *,
        contents: list[Any],
    ) -> tuple[bytes, str]:
        raise NotImplementedError


class GoogleGeminiImageProvider(BaseImageProvider):
    definition = ImageProviderDefinition(
        id=GOOGLE_GEMINI_IMAGE_PROVIDER,
        label="Google Gemini Image",
        description="Google Gemini image generation provider for storyboard frame creation.",
        official=True,
        default_model="gemini-2.5-flash-image",
    )

    async def generate_frame(
        self,
        pipeline: FMVAgentPipeline,
        *,
        contents: list[Any],
    ) -> tuple[bytes, str]:
        return await pipeline._generate_google_storyboard_image(contents=contents)


PROVIDERS: dict[str, BaseImageProvider] = {
    GOOGLE_GEMINI_IMAGE_PROVIDER: GoogleGeminiImageProvider(),
}

LEGACY_IMAGE_MODEL_ALIASES = {
    "nanobanana-2": "gemini-2.5-flash-image",
    "nanobanana-pro": "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image-preview": "gemini-2.5-flash-image",
}


def resolve_image_provider_selection(selection: str | None) -> tuple[str, str]:
    normalized = (selection or "").strip()
    if not normalized:
        provider = PROVIDERS[DEFAULT_IMAGE_PROVIDER]
        return provider.definition.id, provider.definition.default_model

    if normalized in PROVIDERS:
        provider = PROVIDERS[normalized]
        return provider.definition.id, provider.definition.default_model

    model_name = LEGACY_IMAGE_MODEL_ALIASES.get(normalized, normalized)
    provider = PROVIDERS[DEFAULT_IMAGE_PROVIDER]
    return provider.definition.id, model_name


def get_image_provider(provider_id: str | None) -> BaseImageProvider:
    resolved_id, _ = resolve_image_provider_selection(provider_id)
    return PROVIDERS[resolved_id]


def list_image_provider_definitions() -> list[ImageProviderDefinition]:
    return [provider.definition for provider in PROVIDERS.values()]
