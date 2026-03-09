variable "project_id" {
  type        = string
  description = "Google Cloud project id."
}

variable "region" {
  type        = string
  description = "Primary Google Cloud region."
  default     = "us-central1"
}

variable "artifact_registry_repository" {
  type        = string
  description = "Artifact Registry repository name for container images."
  default     = "fmv-studio"
}

variable "backend_service_name" {
  type        = string
  description = "Cloud Run backend service name."
  default     = "fmv-studio-backend"
}

variable "frontend_service_name" {
  type        = string
  description = "Cloud Run frontend service name."
  default     = "fmv-studio-frontend"
}

variable "live_director_service_name" {
  type        = string
  description = "Cloud Run live director gateway service name."
  default     = "fmv-studio-live-director"
}

variable "tasks_queue_name" {
  type        = string
  description = "Cloud Tasks queue name used for asynchronous storyboard/filming jobs."
  default     = "fmv-pipeline"
}

variable "storage_bucket_name" {
  type        = string
  description = "Optional GCS bucket name override."
  default     = ""
}
