# FMV Studio

FMV Studio is an AI-assisted music video editor built from:

- `frontend/`: Next.js 16 / React 19 studio UI
- `backend/`: FastAPI orchestration service for planning, storyboarding, filming, and production

## Google Cloud Deployment

The repo includes automated Google Cloud deployment code for the Gemini Live Agent Challenge bonus criteria:

- Terraform: [`infra/terraform`](infra/terraform)
- Cloud Build configs: [`infra/cloudbuild`](infra/cloudbuild)
- End-to-end deployment script: [`scripts/deploy_google_cloud.sh`](scripts/deploy_google_cloud.sh)

Detailed setup and deploy instructions are in [`docs/google-cloud-deployment.md`](docs/google-cloud-deployment.md).

## Hackathon Artifacts

- Architecture diagram: [`docs/hackathon-architecture-diagram.md`](docs/hackathon-architecture-diagram.md)

## Cloud Smoke Test

After deployment, you can run a low-cost live smoke test that avoids image and video generation:

```bash
BACKEND_URL=https://your-backend-url ./scripts/smoke_test_cloud.sh
```

The script creates a project, uploads the fixture audio in [`tests/fixtures/test_audio.mp3`](tests/fixtures/test_audio.mp3), forces the `uploaded_track` path, runs only through Planning, and verifies the deployed backend responds with a saved shot list.

## Local Development

Backend:

```bash
cd backend
python -m uvicorn app.main:app --reload --port 8000
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

By default the app uses local storage under `.fmv-data/`. Vertex AI and GCS can be enabled locally with the environment variables in [`.env.example`](.env.example).
