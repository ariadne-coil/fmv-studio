from __future__ import annotations

import io
import os
import re
from typing import Optional


DOCUMENT_EXTENSIONS = {".pdf", ".docx"}


def suggest_asset_label(filename: str | None) -> str:
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    cleaned = re.sub(r"[_\-]+", " ", stem).strip()
    return cleaned or "Untitled Asset"


def display_asset_label(label: str | None, fallback_name: str | None) -> str:
    normalized_label = (label or "").strip()
    if normalized_label:
        return normalized_label
    return suggest_asset_label(fallback_name)


def infer_asset_type(filename: str | None, mime_type: str | None) -> str:
    extension = os.path.splitext((filename or "").lower())[1]
    normalized_mime = (mime_type or "").lower()

    if extension in DOCUMENT_EXTENSIONS or normalized_mime in {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }:
        return "document"
    if normalized_mime.startswith("audio/"):
        return "audio"
    if normalized_mime.startswith("video/"):
        return "video"
    return "image"


def normalize_document_text(text: str, *, max_chars: int = 12000) -> str:
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[: max_chars - 1].rstrip() + "…"


def extract_document_text(
    *,
    filename: str | None,
    content: bytes,
    mime_type: str | None = None,
    max_chars: int = 12000,
) -> Optional[str]:
    asset_type = infer_asset_type(filename, mime_type)
    if asset_type != "document":
        return None

    extension = os.path.splitext((filename or "").lower())[1]

    if extension == ".pdf" or (mime_type or "").lower() == "application/pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        normalized = normalize_document_text(text, max_chars=max_chars)
        return normalized or None

    if extension == ".docx" or (mime_type or "").lower() == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        from docx import Document

        document = Document(io.BytesIO(content))
        text = "\n".join(paragraph.text for paragraph in document.paragraphs if paragraph.text.strip())
        normalized = normalize_document_text(text, max_chars=max_chars)
        return normalized or None

    return None
