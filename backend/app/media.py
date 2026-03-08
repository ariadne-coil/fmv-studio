from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.storage import get_storage_backend


router = APIRouter()


@router.get("/projects/{asset_path:path}")
def serve_project_asset(asset_path: str):
    storage = get_storage_backend()
    local_path = storage.resolve_project_asset_to_local_path(f"/projects/{asset_path}")
    if not local_path:
        raise HTTPException(status_code=404, detail="Asset not found")

    try:
        return FileResponse(local_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Asset not found") from exc
