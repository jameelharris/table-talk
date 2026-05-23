output "table_id" {
  value       = google_bigquery_table.this.table_id
  description = "The table ID (echoed back)."
}

output "self_link" {
  value       = google_bigquery_table.this.self_link
  description = "Self-link of the BigQuery table."
}

output "table_full_id" {
  value       = "${var.project}:${var.dataset_id}.${var.table_id}"
  description = "Fully qualified table ID (project:dataset.table)."
}
