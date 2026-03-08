from __future__ import annotations

import os
from typing import Any

from google import genai


def get_genai_backend() -> str:
    configured = (os.getenv("FMV_GENAI_BACKEND") or "").strip().lower()
    if configured:
        return configured
    if os.getenv("FMV_GCP_PROJECT", "").strip() or os.getenv("GOOGLE_CLOUD_PROJECT", "").strip():
        return "vertex"
    return "developer"


def uses_vertex_ai() -> bool:
    return get_genai_backend() == "vertex"


def get_gcp_project() -> str | None:
    project = (os.getenv("FMV_GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
    return project or None


def get_vertex_location() -> str:
    return (os.getenv("FMV_VERTEX_LOCATION") or "global").strip()


def get_vertex_media_location() -> str:
    return (os.getenv("FMV_VERTEX_MEDIA_LOCATION") or os.getenv("FMV_VERTEX_LOCATION") or "us-central1").strip()


def build_genai_client(
    *,
    api_key: str | None,
    api_version: str | None = None,
    media: bool = False,
) -> genai.Client | None:
    client_kwargs: dict[str, Any] = {}
    if api_version:
        client_kwargs["http_options"] = {"api_version": api_version}

    if uses_vertex_ai():
        project = get_gcp_project()
        if not project:
            raise RuntimeError("Vertex AI mode requires FMV_GCP_PROJECT or GOOGLE_CLOUD_PROJECT.")
        client_kwargs.update(
            {
                "vertexai": True,
                "project": project,
                "location": get_vertex_media_location() if media else get_vertex_location(),
            }
        )
        return genai.Client(**client_kwargs)

    if not api_key:
        return None

    client_kwargs["api_key"] = api_key
    return genai.Client(**client_kwargs)
