from __future__ import annotations

import mimetypes
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import urlsplit

from app.paths import DATA_DIR, PROJECTS_DIR


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_updated_at(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return _now_iso()


def _normalize_asset_relative_path(path_or_url: str | Path) -> str:
    raw = str(path_or_url).strip()
    if not raw:
        raise ValueError("Project asset path cannot be empty.")

    cleaned = raw.replace("\\", "/")
    if cleaned.startswith(("http://", "https://")):
        cleaned = urlsplit(cleaned).path
    if cleaned.startswith("/projects/"):
        cleaned = cleaned.removeprefix("/projects/")
    elif cleaned.startswith("projects/"):
        cleaned = cleaned.removeprefix("projects/")

    normalized = str(PurePosixPath(cleaned.lstrip("/")))
    if normalized in {"", "."}:
        raise ValueError(f"Invalid project asset path: {raw}")
    if normalized.startswith("../") or "/../" in f"/{normalized}" or normalized == "..":
        raise ValueError(f"Unsafe project asset path: {raw}")
    return normalized


def _guess_content_type(path: str | Path, provided: str | None = None) -> str:
    if provided:
        return provided
    guessed = mimetypes.guess_type(str(path))[0]
    return guessed or "application/octet-stream"


def _normalize_asset_prefix(prefix: str | Path) -> str:
    raw = str(prefix).strip()
    if not raw:
        raise ValueError("Project asset prefix cannot be empty.")

    had_trailing_slash = raw.endswith(("/", "\\"))
    cleaned = raw.replace("\\", "/")
    if cleaned.startswith(("http://", "https://")):
        cleaned = urlsplit(cleaned).path
    if cleaned.startswith("/projects/"):
        cleaned = cleaned.removeprefix("/projects/")
    elif cleaned.startswith("projects/"):
        cleaned = cleaned.removeprefix("projects/")

    normalized = str(PurePosixPath(cleaned.lstrip("/")))
    if normalized in {"", "."}:
        raise ValueError(f"Invalid project asset prefix: {raw}")
    if normalized.startswith("../") or "/../" in f"/{normalized}" or normalized == "..":
        raise ValueError(f"Unsafe project asset prefix: {raw}")

    if had_trailing_slash and not normalized.endswith("/"):
        normalized = f"{normalized}/"
    return normalized


@dataclass(frozen=True)
class StoredProjectDocument:
    project_id: str
    data: str
    updated_at: str


@dataclass(frozen=True)
class BrowserUploadTarget:
    upload_url: str
    method: str = "PUT"
    headers: dict[str, str] | None = None


class BaseStorageBackend:
    name = "base"

    def ensure_ready(self) -> None:
        raise NotImplementedError

    def project_exists(self, project_id: str) -> bool:
        raise NotImplementedError

    def read_project_state(self, project_id: str) -> str:
        raise NotImplementedError

    def write_project_state(self, project_id: str, data: str) -> None:
        raise NotImplementedError

    def list_project_states(self) -> Iterable[StoredProjectDocument]:
        raise NotImplementedError

    def write_project_asset_bytes(
        self,
        relative_path: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> str:
        raise NotImplementedError

    def sync_local_project_asset(
        self,
        local_path: str | Path,
        *,
        relative_path: str | None = None,
        content_type: str | None = None,
    ) -> str:
        raise NotImplementedError

    def create_browser_project_asset_upload(
        self,
        relative_path: str,
        *,
        content_type: str | None = None,
        content_length: int | None = None,
        origin: str | None = None,
    ) -> BrowserUploadTarget | None:
        return None

    def resolve_project_asset_to_local_path(self, path_or_url: str | Path | None) -> str | None:
        raise NotImplementedError

    def delete_project(
        self,
        project_id: str,
        *,
        asset_paths: Iterable[str] = (),
        asset_prefixes: Iterable[str] = (),
    ) -> None:
        raise NotImplementedError


class LocalStorageBackend(BaseStorageBackend):
    name = "local"

    def ensure_ready(self) -> None:
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

    def _project_path(self, project_id: str) -> Path:
        return PROJECTS_DIR / f"{project_id}.fmv"

    def _asset_path(self, relative_path: str | Path) -> Path:
        normalized = _normalize_asset_relative_path(relative_path)
        return PROJECTS_DIR / normalized

    def project_exists(self, project_id: str) -> bool:
        return self._project_path(project_id).exists()

    def read_project_state(self, project_id: str) -> str:
        return self._project_path(project_id).read_text()

    def write_project_state(self, project_id: str, data: str) -> None:
        self.ensure_ready()
        self._project_path(project_id).write_text(data)

    def list_project_states(self) -> Iterable[StoredProjectDocument]:
        self.ensure_ready()
        for path in sorted(PROJECTS_DIR.glob("*.fmv"), key=lambda item: item.stat().st_mtime, reverse=True):
            yield StoredProjectDocument(
                project_id=path.stem,
                data=path.read_text(),
                updated_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
            )

    def write_project_asset_bytes(
        self,
        relative_path: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> str:
        self.ensure_ready()
        dest = self._asset_path(relative_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return f"/projects/{_normalize_asset_relative_path(relative_path)}"

    def sync_local_project_asset(
        self,
        local_path: str | Path,
        *,
        relative_path: str | None = None,
        content_type: str | None = None,
    ) -> str:
        self.ensure_ready()
        source = Path(local_path)
        target_relative = relative_path or source.name
        dest = self._asset_path(target_relative)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != dest.resolve():
            shutil.copyfile(source, dest)
        return f"/projects/{_normalize_asset_relative_path(target_relative)}"

    def resolve_project_asset_to_local_path(self, path_or_url: str | Path | None) -> str | None:
        if not path_or_url:
            return None

        raw = str(path_or_url).strip()
        if not raw:
            return None

        cleaned = raw.split("?", 1)[0].split("#", 1)[0].replace("\\", "/")
        if cleaned.startswith(("http://", "https://")):
            cleaned = urlsplit(cleaned).path
        if os.path.isabs(cleaned) and not cleaned.startswith("/projects/"):
            return cleaned

        return str(self._asset_path(cleaned).resolve())

    def _delete_local_asset_path(self, relative_path: str) -> None:
        target = self._asset_path(relative_path)
        if target.exists() and target.is_file():
            target.unlink()
            self._prune_empty_asset_dirs(target.parent)

    def _delete_local_asset_prefix(self, normalized_prefix: str) -> None:
        if normalized_prefix.endswith("/"):
            target_dir = self._asset_path(normalized_prefix[:-1])
            if target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
                self._prune_empty_asset_dirs(target_dir.parent)
            return

        for path in PROJECTS_DIR.glob(f"{normalized_prefix}*"):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            self._prune_empty_asset_dirs(path.parent)

    def _prune_empty_asset_dirs(self, start_dir: Path) -> None:
        current = start_dir
        projects_root = PROJECTS_DIR.resolve()
        while current.exists() and current.is_dir():
            try:
                if current.resolve() == projects_root:
                    break
            except FileNotFoundError:
                break

            try:
                next(current.iterdir())
                break
            except StopIteration:
                parent = current.parent
                current.rmdir()
                current = parent

    def delete_project(
        self,
        project_id: str,
        *,
        asset_paths: Iterable[str] = (),
        asset_prefixes: Iterable[str] = (),
    ) -> None:
        self.ensure_ready()
        self._project_path(project_id).unlink(missing_ok=True)

        for asset_path in asset_paths:
            if not asset_path:
                continue
            try:
                self._delete_local_asset_path(_normalize_asset_relative_path(asset_path))
            except ValueError:
                continue

        for asset_prefix in asset_prefixes:
            if not asset_prefix:
                continue
            try:
                self._delete_local_asset_prefix(_normalize_asset_prefix(asset_prefix))
            except ValueError:
                continue


class GCSStorageBackend(BaseStorageBackend):
    name = "gcs"

    def __init__(self) -> None:
        self.bucket_name = os.getenv("FMV_GCS_BUCKET", "").strip()
        self.project_prefix = (os.getenv("FMV_GCS_PROJECT_PREFIX") or "state").strip().strip("/")
        self.media_prefix = (os.getenv("FMV_GCS_MEDIA_PREFIX") or "projects").strip().strip("/")
        self.local_project_cache_dir = DATA_DIR / "state-cache"

    def ensure_ready(self) -> None:
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        self.local_project_cache_dir.mkdir(parents=True, exist_ok=True)
        if not self.bucket_name:
            raise RuntimeError("FMV_STORAGE_BACKEND=gcs requires FMV_GCS_BUCKET.")

    def _storage_client(self):
        try:
            from google.cloud import storage
        except ImportError as exc:  # pragma: no cover - optional dependency at runtime
            raise RuntimeError(
                "FMV_STORAGE_BACKEND=gcs requires the 'google-cloud-storage' package."
            ) from exc

        return storage.Client()

    def _bucket(self):
        return self._storage_client().bucket(self.bucket_name)

    def _project_blob_name(self, project_id: str) -> str:
        return f"{self.project_prefix}/{project_id}.fmv"

    def _media_blob_name(self, relative_path: str | Path) -> str:
        return f"{self.media_prefix}/{_normalize_asset_relative_path(relative_path)}"

    def _project_cache_path(self, project_id: str) -> Path:
        return self.local_project_cache_dir / f"{project_id}.fmv"

    def _media_cache_path(self, relative_path: str | Path) -> Path:
        return PROJECTS_DIR / _normalize_asset_relative_path(relative_path)

    def project_exists(self, project_id: str) -> bool:
        self.ensure_ready()
        blob = self._bucket().blob(self._project_blob_name(project_id))
        return blob.exists()

    def read_project_state(self, project_id: str) -> str:
        self.ensure_ready()
        cache_path = self._project_cache_path(project_id)
        blob = self._bucket().blob(self._project_blob_name(project_id))
        if not blob.exists():
            raise FileNotFoundError(project_id)
        blob.download_to_filename(str(cache_path))
        return cache_path.read_text()

    def write_project_state(self, project_id: str, data: str) -> None:
        self.ensure_ready()
        cache_path = self._project_cache_path(project_id)
        cache_path.write_text(data)
        blob = self._bucket().blob(self._project_blob_name(project_id))
        blob.upload_from_string(data, content_type="application/json")

    def list_project_states(self) -> Iterable[StoredProjectDocument]:
        self.ensure_ready()
        prefix = f"{self.project_prefix}/"
        blobs = list(self._bucket().list_blobs(prefix=prefix))
        blobs.sort(key=lambda blob: blob.updated or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        for blob in blobs:
            if not blob.name.endswith(".fmv"):
                continue
            yield StoredProjectDocument(
                project_id=Path(blob.name).stem,
                data=blob.download_as_text(),
                updated_at=_coerce_updated_at(blob.updated),
            )

    def write_project_asset_bytes(
        self,
        relative_path: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> str:
        self.ensure_ready()
        normalized = _normalize_asset_relative_path(relative_path)
        local_path = self._media_cache_path(normalized)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(data)

        blob = self._bucket().blob(self._media_blob_name(normalized))
        blob.upload_from_filename(str(local_path), content_type=_guess_content_type(normalized, content_type))
        return f"/projects/{normalized}"

    def sync_local_project_asset(
        self,
        local_path: str | Path,
        *,
        relative_path: str | None = None,
        content_type: str | None = None,
    ) -> str:
        self.ensure_ready()
        source = Path(local_path)
        normalized = _normalize_asset_relative_path(relative_path or source.name)
        cache_path = self._media_cache_path(normalized)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != cache_path.resolve():
            shutil.copyfile(source, cache_path)

        blob = self._bucket().blob(self._media_blob_name(normalized))
        blob.upload_from_filename(str(cache_path), content_type=_guess_content_type(normalized, content_type))
        return f"/projects/{normalized}"

    def create_browser_project_asset_upload(
        self,
        relative_path: str,
        *,
        content_type: str | None = None,
        content_length: int | None = None,
        origin: str | None = None,
    ) -> BrowserUploadTarget | None:
        self.ensure_ready()
        normalized = _normalize_asset_relative_path(relative_path)
        blob = self._bucket().blob(self._media_blob_name(normalized))
        upload_url = blob.create_resumable_upload_session(
            content_type=_guess_content_type(normalized, content_type),
            size=content_length,
            origin=origin,
        )
        return BrowserUploadTarget(upload_url=upload_url, method="PUT", headers={})

    def resolve_project_asset_to_local_path(self, path_or_url: str | Path | None) -> str | None:
        if not path_or_url:
            return None

        self.ensure_ready()
        raw = str(path_or_url).strip()
        if not raw:
            return None

        cleaned = raw.split("?", 1)[0].split("#", 1)[0].replace("\\", "/")
        if cleaned.startswith(("http://", "https://")):
            cleaned = urlsplit(cleaned).path
        if os.path.isabs(cleaned) and not cleaned.startswith("/projects/"):
            return cleaned

        normalized = _normalize_asset_relative_path(cleaned)
        cache_path = self._media_cache_path(normalized)
        if cache_path.exists() and cache_path.stat().st_size > 0:
            return str(cache_path.resolve())

        blob = self._bucket().blob(self._media_blob_name(normalized))
        if not blob.exists():
            return str(cache_path.resolve())

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(cache_path))
        return str(cache_path.resolve())

    def _delete_cached_media_path(self, relative_path: str) -> None:
        cache_path = self._media_cache_path(relative_path)
        if cache_path.exists() and cache_path.is_file():
            cache_path.unlink()
            self._prune_cached_media_dirs(cache_path.parent)

    def _delete_cached_media_prefix(self, normalized_prefix: str) -> None:
        if normalized_prefix.endswith("/"):
            cache_dir = self._media_cache_path(normalized_prefix[:-1])
            if cache_dir.exists():
                shutil.rmtree(cache_dir, ignore_errors=True)
                self._prune_cached_media_dirs(cache_dir.parent)
            return

        for path in PROJECTS_DIR.glob(f"{normalized_prefix}*"):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            self._prune_cached_media_dirs(path.parent)

    def _prune_cached_media_dirs(self, start_dir: Path) -> None:
        current = start_dir
        projects_root = PROJECTS_DIR.resolve()
        while current.exists() and current.is_dir():
            try:
                if current.resolve() == projects_root:
                    break
            except FileNotFoundError:
                break

            try:
                next(current.iterdir())
                break
            except StopIteration:
                parent = current.parent
                current.rmdir()
                current = parent

    def delete_project(
        self,
        project_id: str,
        *,
        asset_paths: Iterable[str] = (),
        asset_prefixes: Iterable[str] = (),
    ) -> None:
        self.ensure_ready()
        self._project_cache_path(project_id).unlink(missing_ok=True)
        bucket = self._bucket()
        project_blob = bucket.blob(self._project_blob_name(project_id))
        if project_blob.exists():
            project_blob.delete()

        for asset_path in asset_paths:
            if not asset_path:
                continue
            try:
                normalized = _normalize_asset_relative_path(asset_path)
            except ValueError:
                continue

            self._delete_cached_media_path(normalized)
            asset_blob = bucket.blob(self._media_blob_name(normalized))
            if asset_blob.exists():
                asset_blob.delete()

        for asset_prefix in asset_prefixes:
            if not asset_prefix:
                continue
            try:
                normalized_prefix = _normalize_asset_prefix(asset_prefix)
            except ValueError:
                continue

            self._delete_cached_media_prefix(normalized_prefix)
            blob_prefix = f"{self.media_prefix}/{normalized_prefix.rstrip('/')}"
            if normalized_prefix.endswith("/"):
                blob_prefix = f"{blob_prefix}/"
            for blob in bucket.list_blobs(prefix=blob_prefix):
                blob.delete()


def _resolve_storage_backend_name() -> str:
    configured = (os.getenv("FMV_STORAGE_BACKEND") or "").strip().lower()
    if configured:
        return configured
    if os.getenv("FMV_GCS_BUCKET", "").strip():
        return "gcs"
    return "local"


@lru_cache(maxsize=1)
def get_storage_backend() -> BaseStorageBackend:
    backend_name = _resolve_storage_backend_name()
    if backend_name == "gcs":
        backend: BaseStorageBackend = GCSStorageBackend()
    else:
        backend = LocalStorageBackend()
    backend.ensure_ready()
    return backend


def clear_storage_backend_cache() -> None:
    get_storage_backend.cache_clear()
