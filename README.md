# FMV Studio

FMV Studio is an AI-assisted music video editor built from:

- `frontend/`: Next.js 16 / React 19 studio UI
- `backend/`: FastAPI orchestration service for planning, storyboarding, filming, and production

## Judge Spin-Up

This project is reproducible in two ways:

- `Local`: run the full studio on your machine with local storage
- `Google Cloud`: provision and deploy the hackathon stack with Terraform + Cloud Build + Cloud Run

### Prerequisites

- `Python 3.12`
- `Node.js 20+`
- `ffmpeg`
- For cloud deployment only:
  - `gcloud`
  - `terraform`
  - a billing-enabled Google Cloud project with Vertex AI access

### Option A: Local Spin-Up

1. Create a local env file:

```bash
cp .env.example .env
```

2. Start the backend:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cd backend
python -m uvicorn app.main:app --reload --port 8000
```

On Windows PowerShell, activate with:

```powershell
.venv\Scripts\Activate.ps1
```

3. In a second terminal, start the frontend:

```bash
cd frontend
npm install
npm run dev
```

In the current Windows + WSL local setup, `npm run dev` uses webpack mode by default for compatibility.

4. Open:

```text
http://localhost:3000
```

By default, local mode uses `.fmv-data/` for project state and media. Local runtime options are documented in [`.env.example`](.env.example).

### Option B: Google Cloud Spin-Up

1. Authenticate and select a project:

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

2. Set deploy variables:

```bash
export PROJECT_ID="YOUR_PROJECT_ID"
export REGION="us-central1"
```

3. Run the automated deployment:

```bash
./scripts/deploy_google_cloud.sh
```

This script:

- provisions infrastructure with Terraform
- builds backend and frontend images with Cloud Build
- deploys both services to Cloud Run
- configures GCS and Cloud Tasks for durable project state and async jobs

4. Run the low-cost smoke test:

```bash
BACKEND_URL="https://your-backend-url" ./scripts/smoke_test_cloud.sh
```

The smoke test intentionally stops at `Planning`, so judges can verify the deployed system without paying for storyboard or video generation.

For a full cloud walkthrough, see [`docs/google-cloud-deployment.md`](docs/google-cloud-deployment.md).

## Google Cloud Deployment

The repo includes automated Google Cloud deployment code for the Gemini Live Agent Challenge bonus criteria:

- Terraform: [`infra/terraform`](infra/terraform)
- Cloud Build configs: [`infra/cloudbuild`](infra/cloudbuild)
- End-to-end deployment script: [`scripts/deploy_google_cloud.sh`](scripts/deploy_google_cloud.sh)

Detailed setup and deploy instructions are in [`docs/google-cloud-deployment.md`](docs/google-cloud-deployment.md).

## Hackathon Artifacts

- Architecture diagram: [`docs/hackathon-architecture-diagram.md`](docs/hackathon-architecture-diagram.md)
- Project summary: [`docs/hackathon-project-summary.md`](docs/hackathon-project-summary.md)
- Google Cloud deployment proof: [`docs/hackathon-google-cloud-proof.md`](docs/hackathon-google-cloud-proof.md)

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
