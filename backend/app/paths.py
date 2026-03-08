import os
from pathlib import Path


def _resolve_repo_path(value: str | None, default: Path) -> Path:
    if not value:
        return default.resolve()

    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    return candidate.resolve()


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
DATA_DIR = _resolve_repo_path(os.getenv("FMV_DATA_DIR"), REPO_ROOT / ".fmv-data")
PROJECTS_DIR = _resolve_repo_path(
    os.getenv("FMV_PROJECTS_DIR"),
    DATA_DIR / "projects",
)
UPLOADS_DIR = PROJECTS_DIR / "uploads"
ENV_FILE = _resolve_repo_path(os.getenv("FMV_ENV_FILE"), REPO_ROOT / ".env")
LEGACY_ENV_FILE = BACKEND_DIR / ".env"


def path_to_project_url(path: str | Path) -> str:
    path_obj = Path(path)
    try:
        relative = path_obj.resolve().relative_to(PROJECTS_DIR.resolve())
    except ValueError:
        return str(path_obj).replace("\\", "/")
    return f"/projects/{str(relative).replace('\\', '/')}"
