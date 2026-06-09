output "bucket_name" {
  value       = module.videos_bucket.name
  description = "Name of the videos GCS bucket."
}

output "bucket_url" {
  value       = module.videos_bucket.url
  description = "GCS URL of the videos bucket."
}

output "bucket_self_link" {
  value       = module.videos_bucket.self_link
  description = "Self-link of the GCS bucket."
}

output "bigquery_dataset_id" {
  value       = module.bigquery_dataset.dataset_id
  description = "BigQuery dataset ID."
}

output "videos_table_id" {
  value       = module.videos_table.table_id
  description = "BigQuery table ID for the videos entity table."
}

output "videos_table_full_id" {
  value       = module.videos_table.table_full_id
  description = "Fully qualified videos table ID (project:dataset.table)."
}

output "video_ingestion_attempts_table_id" {
  value       = module.video_ingestion_attempts_table.table_id
  description = "BigQuery table ID for the video ingestion audit log."
}

output "video_ingestion_attempts_table_full_id" {
  value       = module.video_ingestion_attempts_table.table_full_id
  description = "Fully qualified audit log table ID."
}

output "hand_setups_bucket_name" {
  value       = module.hand_setups_bucket.name
  description = "Name of the hand-setups frames GCS bucket."
}

output "hand_setups_bucket_url" {
  value       = module.hand_setups_bucket.url
  description = "GCS URL of the hand-setups frames bucket."
}

output "hand_setups_bucket_self_link" {
  value       = module.hand_setups_bucket.self_link
  description = "Self-link of the hand-setups frames GCS bucket."
}

output "hand_setups_table_id" {
  value       = module.hand_setups_table.table_id
  description = "BigQuery table ID for the hand setups inventory table."
}

output "hand_setups_table_full_id" {
  value       = module.hand_setups_table.table_full_id
  description = "Fully qualified hand setups table ID (project:dataset.table)."
}
