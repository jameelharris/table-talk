output "name" {
  value       = google_storage_bucket.this.name
  description = "Name of the GCS bucket."
}

output "url" {
  value       = "gs://${google_storage_bucket.this.name}"
  description = "GCS URL of the bucket."
}

output "self_link" {
  value       = google_storage_bucket.this.self_link
  description = "Self-link of the GCS bucket."
}
