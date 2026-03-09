output "artifact_registry_repository" {
  value = google_artifact_registry_repository.containers.repository_id
}

output "artifact_registry_repository_url" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.containers.repository_id}"
}

output "storage_bucket_name" {
  value = google_storage_bucket.fmv_assets.name
}

output "tasks_queue_name" {
  value = google_cloud_tasks_queue.pipeline.name
}

output "backend_service_account_email" {
  value = google_service_account.backend_runtime.email
}

output "frontend_service_account_email" {
  value = google_service_account.frontend_runtime.email
}

output "live_director_service_account_email" {
  value = google_service_account.live_director_runtime.email
}

output "tasks_service_account_email" {
  value = google_service_account.tasks_invoker.email
}

output "backend_service_name" {
  value = var.backend_service_name
}

output "frontend_service_name" {
  value = var.frontend_service_name
}

output "live_director_service_name" {
  value = var.live_director_service_name
}

output "internal_task_token" {
  value     = random_password.internal_task_token.result
  sensitive = true
}
