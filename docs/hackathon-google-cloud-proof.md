# Proof of Google Cloud Deployment

This document is the repo artifact for the hackathon's "Proof of Google Cloud Deployment" requirement.

The code below proves the FMV Studio backend is designed to run on Google Cloud and uses Google Cloud services directly.

## Primary Proof Files

### 1. Cloud Run deployment script

[`scripts/deploy_google_cloud.sh`](../scripts/deploy_google_cloud.sh)

This is the end-to-end deployment entrypoint. It:

- applies Terraform infrastructure
- builds backend and frontend images with Cloud Build
- deploys the backend to Cloud Run with Vertex AI, GCS, and Cloud Tasks enabled
- deploys a separate `Live Director` Cloud Run gateway for realtime voice sessions

The backend Cloud Run deploy command explicitly sets:

- `FMV_GENAI_BACKEND=vertex`
- `FMV_STORAGE_BACKEND=gcs`
- `FMV_JOB_DRIVER=cloud_tasks`

Those environment variables are the runtime switch that moves the backend off local disk / local jobs and onto Google Cloud services. The deploy script also hardens the backend by removing anonymous access and granting `roles/run.invoker` only to the frontend service account and the Cloud Tasks service account.

The same script also deploys the public websocket gateway used by `Live Director`, which proxies browser sessions to the Vertex AI Live API with Google Cloud credentials.

### 2. Terraform infrastructure

[`infra/terraform/main.tf`](../infra/terraform/main.tf)

This file provisions the Google Cloud resources the backend depends on:

- Google Cloud APIs
- Artifact Registry
- Google Cloud Storage
- Cloud Tasks
- backend and frontend runtime service accounts
- IAM bindings for Vertex AI, Cloud Tasks, Cloud Logging, and Cloud Storage

This is the infrastructure-as-code proof that the deployment is automated and reproducible.

### 3. Vertex AI client configuration in the backend

[`backend/app/genai_runtime.py`](../backend/app/genai_runtime.py)

This is the backend code that switches the app into Vertex AI mode. When `FMV_GENAI_BACKEND=vertex`, the backend constructs the Google GenAI client with:

- `vertexai=True`
- the configured Google Cloud project
- the configured Vertex AI location

### 4. Google Cloud Storage persistence

[`backend/app/storage.py`](../backend/app/storage.py)

This file contains `GCSStorageBackend`, which stores:

- project state
- uploaded assets
- generated frames
- generated clips
- generated music
- final renders

in a Google Cloud Storage bucket.

This is important because the backend is not just deployed on Cloud Run; it is also using durable Google Cloud storage instead of local instance disk.

### 5. Cloud Tasks async job dispatch

[`backend/app/job_queue.py`](../backend/app/job_queue.py)

This file contains the `cloud_tasks` job driver. When the backend runs in cloud mode, long-running storyboard and filming jobs are enqueued via `google.cloud.tasks_v2.CloudTasksClient()` and sent back to the backend's internal execution endpoint.

This is the proof that background work is running through Google Cloud Tasks rather than only in local in-memory asyncio tasks.

### 6. Vertex AI media generation path

[`backend/app/agent/graph.py`](../backend/app/agent/graph.py)

The pipeline graph contains the cloud media path for Vertex-backed generation. In particular, it:

- stages media output to GCS for Vertex AI jobs
- downloads generated media back from GCS
- handles Vertex AI media output URIs

This is the strongest backend-only code proof that the generation pipeline is wired to Google Cloud services in production.

### 7. Vertex Live gateway for Live Director

[`backend/app/live_gateway.py`](../backend/app/live_gateway.py)

This file is the clearest code proof that FMV Studio's realtime director feature is also running on Google Cloud. It:

- runs as a public Cloud Run websocket service
- uses Google Cloud ADC to mint bearer tokens
- opens a Vertex AI Live session against Google's bidirectional live endpoint
- relays browser audio and model audio between the user and Vertex AI

This shows that the live multimodal part of the product is also using Google Cloud services directly, not only the staged backend pipeline.

## Why This Satisfies the Deliverable

The hackathon asks for proof that the backend is running on Google Cloud. This repo provides that proof in code form:

- deployment automation to Cloud Run
- deployment automation for the Live Director Cloud Run gateway
- Terraform-managed Google Cloud infrastructure
- backend runtime configuration for Vertex AI
- Google Cloud Storage persistence
- Cloud Tasks background execution
- Vertex AI Live websocket proxying for realtime director voice

Taken together, these files show both:

1. the backend is meant to be deployed on Google Cloud
2. the backend actively uses Google Cloud services and APIs at runtime

## Supporting Link

- [`scripts/deploy_google_cloud.sh`](../scripts/deploy_google_cloud.sh)
- [`infra/terraform/main.tf`](../infra/terraform/main.tf)
- [`backend/app/genai_runtime.py`](../backend/app/genai_runtime.py)
- [`backend/app/storage.py`](../backend/app/storage.py)
- [`backend/app/job_queue.py`](../backend/app/job_queue.py)
- [`backend/app/live_gateway.py`](../backend/app/live_gateway.py)
