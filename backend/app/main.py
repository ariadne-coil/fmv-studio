import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.endpoints import router as project_router
from app.media import router as media_router
from app.paths import ENV_FILE, LEGACY_ENV_FILE
from app.storage import get_storage_backend

for env_path in (ENV_FILE, LEGACY_ENV_FILE):
    if env_path.exists():
        load_dotenv(env_path)
        break

app = FastAPI(title="FMV Studio Backend")
get_storage_backend().ensure_ready()

configured_cors_origins = [
    origin.strip()
    for origin in (os.getenv("FMV_CORS_ORIGINS") or "http://localhost:3000,http://127.0.0.1:3000").split(",")
    if origin.strip()
]

if configured_cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=configured_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(project_router, prefix="/api")
app.include_router(media_router)

@app.get("/api/health")
def health_check():
    return {"status": "ok"}
