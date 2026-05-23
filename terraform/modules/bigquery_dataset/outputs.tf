output "dataset_id" {
  value       = google_bigquery_dataset.this.dataset_id
  description = "The dataset ID (input echoed back)."
}

output "self_link" {
  value       = google_bigquery_dataset.this.self_link
  description = "Self-link of the BigQuery dataset."
}

output "location" {
  value       = google_bigquery_dataset.this.location
  description = "Location of the BigQuery dataset."
}
