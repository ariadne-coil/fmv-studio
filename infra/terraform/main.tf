locals {
  bucket_name = trimspace(var.storage_bucket_name) != "" ? var.storage_bucket_name : "${var.project_id}-fmv-studio-assets"
  required_services = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudtasks.googleapis.com",
    "iamcredentials.googleapis.com",
    "run.googleapis.com",
    "storage.googleapis.com",
  ])
}

resource "google_project_service" "required" {
  for_each                   = local.required_services
  project                    = var.project_id
  service                    = each.value
  disable_dependent_services = false
  disable_on_destroy         = false
}

resource "google_artifact_registry_repository" "containers" {
  location      = var.region
  repository_id = var.artifact_registry_repository
  description   = "FMV Studio container images"
  format        = "DOCKER"

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket" "fmv_assets" {
  name                        = local.bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  cors {
    origin          = ["*"]
    method          = ["GET", "HEAD", "POST", "PUT", "OPTIONS"]
    response_header = ["Content-Type", "Content-Length", "Content-Range", "x-goog-resumable", "x-guploader-uploadid"]
    max_age_seconds = 3600
  }

  versioning {
    enabled = true
  }

  depends_on = [google_project_service.required]
}

resource "google_cloud_tasks_queue" "pipeline" {
  name     = var.tasks_queue_name
  location = var.region

  rate_limits {
    max_concurrent_dispatches = 4
    max_dispatches_per_second = 1
  }

  retry_config {
    max_attempts = 3
  }

  depends_on = [google_project_service.required]
}

resource "google_service_account" "backend_runtime" {
  account_id   = "fmv-backend-run"
  display_name = "FMV Studio Backend Runtime"
}

resource "google_service_account" "frontend_runtime" {
  account_id   = "fmv-frontend-run"
  display_name = "FMV Studio Frontend Runtime"
}

resource "google_service_account" "live_director_runtime" {
  account_id   = "fmv-live-director-run"
  display_name = "FMV Studio Live Director Runtime"
}

resource "google_service_account" "tasks_invoker" {
  account_id   = "fmv-tasks-invoker"
  display_name = "FMV Studio Cloud Tasks Invoker"
}

resource "google_project_iam_member" "backend_vertex_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.backend_runtime.email}"
}

resource "google_project_iam_member" "backend_tasks_enqueuer" {
  project = var.project_id
  role    = "roles/cloudtasks.enqueuer"
  member  = "serviceAccount:${google_service_account.backend_runtime.email}"
}

resource "google_project_iam_member" "backend_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.backend_runtime.email}"
}

resource "google_project_iam_member" "frontend_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.frontend_runtime.email}"
}

resource "google_project_iam_member" "live_director_vertex_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.live_director_runtime.email}"
}

resource "google_project_iam_member" "live_director_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.live_director_runtime.email}"
}

resource "google_service_account_iam_member" "backend_can_act_as_tasks_invoker" {
  service_account_id = google_service_account.tasks_invoker.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.backend_runtime.email}"
}

resource "google_storage_bucket_iam_member" "backend_bucket_admin" {
  bucket = google_storage_bucket.fmv_assets.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.backend_runtime.email}"
}

resource "random_password" "internal_task_token" {
  length  = 40
  special = false
}
