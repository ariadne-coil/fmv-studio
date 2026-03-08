#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="${ROOT_DIR}/infra/terraform"

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID to your Google Cloud project id.}"
REGION="${REGION:-us-central1}"
VERTEX_LOCATION="${VERTEX_LOCATION:-global}"
VERTEX_MEDIA_LOCATION="${VERTEX_MEDIA_LOCATION:-${REGION}}"
ARTIFACT_REPOSITORY="${ARTIFACT_REPOSITORY:-fmv-studio}"
BACKEND_SERVICE_NAME="${BACKEND_SERVICE_NAME:-fmv-studio-backend}"
FRONTEND_SERVICE_NAME="${FRONTEND_SERVICE_NAME:-fmv-studio-frontend}"
TASKS_QUEUE_NAME="${TASKS_QUEUE_NAME:-fmv-pipeline}"
STORAGE_BUCKET_NAME="${STORAGE_BUCKET_NAME:-${PROJECT_ID}-fmv-studio-assets}"
IMAGE_TAG="${IMAGE_TAG:-$(date +%Y%m%d-%H%M%S)}"

terraform -chdir="${TF_DIR}" init
terraform -chdir="${TF_DIR}" apply -auto-approve \
  -var="project_id=${PROJECT_ID}" \
  -var="region=${REGION}" \
  -var="artifact_registry_repository=${ARTIFACT_REPOSITORY}" \
  -var="backend_service_name=${BACKEND_SERVICE_NAME}" \
  -var="frontend_service_name=${FRONTEND_SERVICE_NAME}" \
  -var="tasks_queue_name=${TASKS_QUEUE_NAME}" \
  -var="storage_bucket_name=${STORAGE_BUCKET_NAME}"

REPOSITORY_URL="$(terraform -chdir="${TF_DIR}" output -raw artifact_registry_repository_url)"
BACKEND_SERVICE_ACCOUNT="$(terraform -chdir="${TF_DIR}" output -raw backend_service_account_email)"
FRONTEND_SERVICE_ACCOUNT="$(terraform -chdir="${TF_DIR}" output -raw frontend_service_account_email)"
INTERNAL_TASK_TOKEN="$(terraform -chdir="${TF_DIR}" output -raw internal_task_token)"

BACKEND_IMAGE="${REPOSITORY_URL}/backend:${IMAGE_TAG}"
FRONTEND_IMAGE="${REPOSITORY_URL}/frontend:${IMAGE_TAG}"

gcloud config set project "${PROJECT_ID}" >/dev/null
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

gcloud builds submit "${ROOT_DIR}/backend" \
  --config "${ROOT_DIR}/infra/cloudbuild/backend.yaml" \
  --substitutions "_IMAGE=${BACKEND_IMAGE}"

gcloud run deploy "${BACKEND_SERVICE_NAME}" \
  --image "${BACKEND_IMAGE}" \
  --region "${REGION}" \
  --allow-unauthenticated \
  --service-account "${BACKEND_SERVICE_ACCOUNT}" \
  --concurrency 1 \
  --cpu 2 \
  --memory 4Gi \
  --timeout 3600 \
  --set-env-vars "FMV_GENAI_BACKEND=vertex,FMV_STORAGE_BACKEND=gcs,FMV_GCS_BUCKET=${STORAGE_BUCKET_NAME},FMV_GCP_PROJECT=${PROJECT_ID},FMV_VERTEX_LOCATION=${VERTEX_LOCATION},FMV_VERTEX_MEDIA_LOCATION=${VERTEX_MEDIA_LOCATION},FMV_JOB_DRIVER=cloud_tasks,FMV_CLOUD_TASKS_LOCATION=${REGION},FMV_CLOUD_TASKS_QUEUE=${TASKS_QUEUE_NAME},FMV_INTERNAL_TASK_TOKEN=${INTERNAL_TASK_TOKEN}"

BACKEND_URL="$(gcloud run services describe "${BACKEND_SERVICE_NAME}" --region "${REGION}" --format='value(status.url)')"

gcloud run services update "${BACKEND_SERVICE_NAME}" \
  --region "${REGION}" \
  --update-env-vars "FMV_BASE_URL=${BACKEND_URL}" >/dev/null

gcloud builds submit "${ROOT_DIR}/frontend" \
  --config "${ROOT_DIR}/infra/cloudbuild/frontend.yaml" \
  --substitutions "_IMAGE=${FRONTEND_IMAGE},_NEXT_PUBLIC_API_ORIGIN=${BACKEND_URL}"

gcloud run deploy "${FRONTEND_SERVICE_NAME}" \
  --image "${FRONTEND_IMAGE}" \
  --region "${REGION}" \
  --allow-unauthenticated \
  --service-account "${FRONTEND_SERVICE_ACCOUNT}" \
  --cpu 1 \
  --memory 1Gi \
  --set-env-vars "NEXT_PUBLIC_API_ORIGIN=${BACKEND_URL}"

FRONTEND_URL="$(gcloud run services describe "${FRONTEND_SERVICE_NAME}" --region "${REGION}" --format='value(status.url)')"

cat <<EOF
Deployment complete.
Frontend: ${FRONTEND_URL}
Backend:  ${BACKEND_URL}
Images:
  ${BACKEND_IMAGE}
  ${FRONTEND_IMAGE}
EOF
