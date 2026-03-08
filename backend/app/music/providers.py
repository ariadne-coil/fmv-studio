from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agent.graph import FMVAgentPipeline
    from app.agent.models import ProjectState


GOOGLE_LYRIA_REALTIME_PROVIDER = "google-lyria-realtime"
EXTERNAL_IMPORT_PROVIDER = "external-import"
DEFAULT_MUSIC_PROVIDER = GOOGLE_LYRIA_REALTIME_PROVIDER


@dataclass(frozen=True)
class MusicProviderDefinition:
    id: str
    label: str
    description: str
    official: bool
    mode: str
    uses_lyrics: bool = True
    available: bool = True
    default_model: str | None = None
    availability_note: str | None = None


class BaseMusicProvider:
    definition: MusicProviderDefinition

    def is_available(self) -> bool:
        return self.definition.available

    def can_generate_automatically(self) -> bool:
        return self.definition.mode == "automatic" and self.definition.available

    def requires_manual_import(self) -> bool:
        return self.definition.mode == "manual_import"

    def blocking_message(self, state: ProjectState) -> str | None:
        if not self.is_available():
            note = self.definition.availability_note or "Choose a different provider for now."
            return f"{self.definition.label} is not available yet. {note}"
        if self.requires_manual_import() and not state.music_url:
            return "Import a song file before continuing to Planning."
        return None

    async def generate_track(
        self,
        pipeline: FMVAgentPipeline,
        state: ProjectState,
        *,
        target_duration_seconds: float | None = None,
    ) -> str | None:
        raise RuntimeError(self.blocking_message(state) or f"{self.definition.label} cannot generate a track in this build.")


class GoogleLyriaRealtimeProvider(BaseMusicProvider):
    definition = MusicProviderDefinition(
        id=GOOGLE_LYRIA_REALTIME_PROVIDER,
        label="Google Lyria",
        description="Official Google music route. Uses Vertex AI in cloud deployments and the Live Music API locally. Automatic and instrumental-only.",
        official=True,
        mode="automatic",
        uses_lyrics=False,
        available=True,
        default_model="lyria-realtime-exp",
    )

    async def generate_track(
        self,
        pipeline: FMVAgentPipeline,
        state: ProjectState,
        *,
        target_duration_seconds: float | None = None,
    ) -> str | None:
        return await pipeline._generate_google_lyria_realtime_track(
            state,
            target_duration_seconds=target_duration_seconds,
        )


class ExternalImportProvider(BaseMusicProvider):
    definition = MusicProviderDefinition(
        id=EXTERNAL_IMPORT_PROVIDER,
        label="Manual Song Import",
        description="Use Gemini to draft prompts, then import a rendered song from another tool.",
        official=False,
        mode="manual_import",
        available=True,
    )

    def blocking_message(self, state: ProjectState) -> str | None:
        if not state.music_url:
            return "Import a rendered song before continuing to Planning."
        return None


PROVIDERS: dict[str, BaseMusicProvider] = {
    GOOGLE_LYRIA_REALTIME_PROVIDER: GoogleLyriaRealtimeProvider(),
    EXTERNAL_IMPORT_PROVIDER: ExternalImportProvider(),
}

LEGACY_PROVIDER_ALIASES = {
    "lyria-realtime-exp": GOOGLE_LYRIA_REALTIME_PROVIDER,
    "external-lyria-3": EXTERNAL_IMPORT_PROVIDER,
    "lyria-3": EXTERNAL_IMPORT_PROVIDER,
}


def normalize_music_provider_id(provider_id: str | None) -> str:
    normalized = (provider_id or "").strip()
    if not normalized:
        return DEFAULT_MUSIC_PROVIDER
    if normalized in PROVIDERS:
        return normalized
    return LEGACY_PROVIDER_ALIASES.get(normalized, DEFAULT_MUSIC_PROVIDER)


def get_music_provider(provider_id: str | None) -> BaseMusicProvider:
    return PROVIDERS[normalize_music_provider_id(provider_id)]


def list_music_provider_definitions() -> list[MusicProviderDefinition]:
    return [provider.definition for provider in PROVIDERS.values()]
