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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allow nextjs frontend
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(project_router, prefix="/api")
app.include_router(media_router)

@app.get("/api/health")
def health_check():
    return {"status": "ok"}
