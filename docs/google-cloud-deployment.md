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

## Infrastructure-as-Code

Terraform lives in [`infra/terraform`](../infra/terraform).

It provisions:

- Artifact Registry
- GCS bucket
- Cloud Tasks queue
- Runtime service accounts
- Required IAM bindings
- A generated internal task token for backend task dispatch

## Container Builds

Cloud Build configs live in [`infra/cloudbuild`](../infra/cloudbuild):

- [`backend.yaml`](../infra/cloudbuild/backend.yaml)
- [`frontend.yaml`](../infra/cloudbuild/frontend.yaml)

The frontend build injects `NEXT_PUBLIC_API_ORIGIN` at build time so the deployed UI points at the Cloud Run backend.

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
- The current official cloud music path is instrumental-only.
- Frontend and backend are deployed as separate Cloud Run services.
