from __future__ import annotations

import mimetypes
import os
import re
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse

from app.storage import get_storage_backend


router = APIRouter()
_BYTE_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")
_STREAM_CHUNK_SIZE = 1024 * 1024


def _normalize_media_type(file_path: Path, media_type: str | None) -> str:
    suffix = file_path.suffix.lower()
    if suffix in {".wav", ".wave"}:
        return "audio/wav"
    return media_type or "application/octet-stream"


def _iter_file_bytes(local_path: str, *, start: int, end: int) -> Iterator[bytes]:
    with open(local_path, "rb") as handle:
        handle.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = handle.read(min(_STREAM_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _parse_byte_range(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    if not range_header:
        return None

    match = _BYTE_RANGE_RE.fullmatch(range_header.strip())
    if not match:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail="Invalid byte range.",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    start_raw, end_raw = match.groups()
    if not start_raw and not end_raw:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail="Invalid byte range.",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    if start_raw:
        start = int(start_raw)
        end = int(end_raw) if end_raw else file_size - 1
    else:
        suffix_length = int(end_raw)
        if suffix_length <= 0:
            raise HTTPException(
                status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                detail="Invalid byte range.",
                headers={"Content-Range": f"bytes */{file_size}"},
            )
        start = max(file_size - suffix_length, 0)
        end = file_size - 1

    if start < 0 or start >= file_size or end < start:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail="Requested range is outside the file bounds.",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    return start, min(end, file_size - 1)


def build_project_asset_response(
    local_path: str,
    *,
    method: str,
    range_header: str | None = None,
) -> Response:
    file_path = Path(local_path)
    if not file_path.exists():
        raise FileNotFoundError(local_path)

    file_size = file_path.stat().st_size
    media_type = _normalize_media_type(
        file_path,
        mimetypes.guess_type(str(file_path))[0],
    )
    normalized_method = method.upper()
    byte_range = _parse_byte_range(range_header, file_size)

    if byte_range:
        start, end = byte_range
        content_length = end - start + 1
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Range": f"bytes {start}-{end}/{file_size}",
        }
        if normalized_method == "HEAD":
            return Response(status_code=status.HTTP_206_PARTIAL_CONTENT, headers=headers, media_type=media_type)
        return StreamingResponse(
            _iter_file_bytes(str(file_path), start=start, end=end),
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            headers=headers,
            media_type=media_type,
        )

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(file_size),
    }
    if normalized_method == "HEAD":
        return Response(status_code=status.HTTP_200_OK, headers=headers, media_type=media_type)
    return StreamingResponse(
        _iter_file_bytes(str(file_path), start=0, end=max(file_size - 1, 0)),
        headers=headers,
        media_type=media_type,
    )


@router.api_route("/projects/{asset_path:path}", methods=["GET", "HEAD"])
def serve_project_asset(asset_path: str, request: Request):
    storage = get_storage_backend()
    local_path = storage.resolve_project_asset_to_local_path(f"/projects/{asset_path}")
    if not local_path:
        raise HTTPException(status_code=404, detail="Asset not found")

    try:
        return build_project_asset_response(
            local_path,
            method=request.method,
            range_header=request.headers.get("range"),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Asset not found") from exc
