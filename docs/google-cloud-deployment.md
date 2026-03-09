# Google Cloud Deployment

This repo contains automated deployment code for FMV Studio on Google Cloud.

## What Gets Automated

- Google Cloud APIs enablement
- Artifact Registry repository creation
- Google Cloud Storage bucket creation for persisted project state and media
- Cloud Tasks queue creation for asynchronous storyboard/filming runs
- Service accounts and IAM bindings
- Backend container build and Cloud Run deployment
- Frontend container build and Cloud Run deployment
- Live Director gateway build and Cloud Run deployment

## Infrastructure-as-Code

Terraform lives in [`infra/terraform`](../infra/terraform).

It provisions:

- Artifact Registry
- GCS bucket
- Cloud Tasks queue
- Runtime service accounts
- Live Director runtime service account
- Required IAM bindings
- A generated internal task token for backend task dispatch

## Container Builds

Cloud Build configs live in [`infra/cloudbuild`](../infra/cloudbuild):

- [`backend.yaml`](../infra/cloudbuild/backend.yaml)
- [`frontend.yaml`](../infra/cloudbuild/frontend.yaml)

The frontend container is runtime-configured with `FMV_BACKEND_ORIGIN` so its server-side proxy routes know how to reach the private Cloud Run backend.

The deployment also builds and deploys the public `Live Director` websocket gateway, which proxies realtime browser audio sessions to Vertex AI Live.

## One-Command Deploy

The deployment entrypoint is [`scripts/deploy_google_cloud.sh`](../scripts/deploy_google_cloud.sh).

Example:

```bash
export PROJECT_ID="your-gcp-project"
export REGION="us-central1"
./scripts/deploy_google_cloud.sh
```

Optional overrides:

```bash
export ARTIFACT_REPOSITORY="fmv-studio"
export BACKEND_SERVICE_NAME="fmv-studio-backend"
export FRONTEND_SERVICE_NAME="fmv-studio-frontend"
export LIVE_DIRECTOR_SERVICE_NAME="fmv-studio-live-director"
export TASKS_QUEUE_NAME="fmv-pipeline"
export STORAGE_BUCKET_NAME="${PROJECT_ID}-fmv-studio-assets"
export VERTEX_LOCATION="global"
export VERTEX_MEDIA_LOCATION="${REGION}"
export IMAGE_TAG="$(date +%Y%m%d-%H%M%S)"
```

## Runtime Architecture

Cloud deployment uses:

- `FMV_GENAI_BACKEND=vertex`
- `FMV_STORAGE_BACKEND=gcs`
- `FMV_JOB_DRIVER=cloud_tasks`

That means:

- Gemini/Veo/Lyria requests use Vertex AI credentials instead of API keys
- project JSON and generated media persist to GCS instead of local disk
- long-running storyboard/filming runs are dispatched through Cloud Tasks instead of in-process asyncio jobs
- realtime `Live Director` voice sessions run through a dedicated Cloud Run websocket gateway that proxies to Vertex AI Live

## Backend Hardening

The cloud deployment keeps the frontend public but makes the backend private.

- the backend Cloud Run service is deployed without anonymous access
- the frontend talks to the backend through same-origin Next.js proxy routes
- the frontend runtime service account receives `roles/run.invoker` on the backend service
- Cloud Tasks uses a dedicated service account with `roles/run.invoker` on the backend service
- the internal task endpoint still requires `X-Internal-Task-Token` as a second check

This keeps public users on the frontend URL while removing direct anonymous access to the backend API.

## Live Director Realtime Gateway

The cloud deployment also exposes a separate public Cloud Run service for realtime `Live Director` voice:

- the browser opens a websocket to the `Live Director` gateway
- the gateway authenticates to Google Cloud with ADC
- the gateway opens the upstream Vertex AI Live session
- the frontend still sends actual project mutations through the main backend, so project state remains centralized

This split keeps the private backend hardened while still allowing realtime browser audio for the directing experience.

## Local Testing

The same codebase still supports local mode:

- `FMV_STORAGE_BACKEND=local`
- `FMV_JOB_DRIVER=local`
- `FMV_GENAI_BACKEND=developer` or `vertex`

For local Vertex AI testing, authenticate with Application Default Credentials:

```bash
gcloud auth application-default login
```

Then set the relevant environment variables from [`.env.example`](../.env.example).

## Notes

- The backend serves media through `/projects/...` regardless of whether the underlying storage is local disk or GCS.
- In cloud mode, media and API traffic flow through the frontend's same-origin proxy routes so the private backend URL does not need to be exposed to browsers.
- Realtime Live Director voice is the one exception: it uses a dedicated public websocket gateway because browsers need direct low-latency audio streaming.
- The current official cloud music path is instrumental-only.
- Frontend, backend, and the Live Director gateway are deployed as separate Cloud Run services.
