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

output "tournament_results_bucket_name" {
  value       = module.tournament_results_bucket.name
  description = "Name of the tournament-results frames GCS bucket."
}

output "tournament_results_bucket_url" {
  value       = module.tournament_results_bucket.url
  description = "GCS URL of the tournament-results frames bucket."
}

output "tournament_results_bucket_self_link" {
  value       = module.tournament_results_bucket.self_link
  description = "Self-link of the tournament-results frames GCS bucket."
}

output "tournament_results_table_id" {
  value       = module.tournament_results_table.table_id
  description = "BigQuery table ID for the tournament results stage table."
}

output "tournament_results_table_full_id" {
  value       = module.tournament_results_table.table_full_id
  description = "Fully qualified tournament results table ID (project:dataset.table)."
}

output "tournament_results_processing_attempts_table_id" {
  value       = module.tournament_results_processing_attempts_table.table_id
  description = "BigQuery table ID for the payout extraction audit log."
}

output "tournament_results_processing_attempts_table_full_id" {
  value       = module.tournament_results_processing_attempts_table.table_full_id
  description = "Fully qualified payout extraction audit log table ID."
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

output "hand_starts_bucket_name" {
  value       = module.hand_starts_bucket.name
  description = "Name of the hand-starts frames GCS bucket."
}

output "hand_starts_bucket_url" {
  value       = module.hand_starts_bucket.url
  description = "GCS URL of the hand-starts frames bucket."
}

output "hand_starts_bucket_self_link" {
  value       = module.hand_starts_bucket.self_link
  description = "Self-link of the hand-starts frames GCS bucket."
}

output "hand_starts_table_id" {
  value       = module.hand_starts_table.table_id
  description = "BigQuery table ID for the hand starts stage table."
}

output "hand_starts_table_full_id" {
  value       = module.hand_starts_table.table_full_id
  description = "Fully qualified hand starts table ID (project:dataset.table)."
}

output "hand_setup_processing_attempts_table_id" {
  value       = module.hand_setup_processing_attempts_table.table_id
  description = "BigQuery table ID for the hand setup processing audit log."
}

output "hand_setup_processing_attempts_table_full_id" {
  value       = module.hand_setup_processing_attempts_table.table_full_id
  description = "Fully qualified hand setup processing attempts table ID."
}

output "hand_actions_bucket_name" {
  value       = module.hand_actions_bucket.name
  description = "Name of the hand-actions frames GCS bucket."
}

output "hand_actions_bucket_url" {
  value       = module.hand_actions_bucket.url
  description = "GCS URL of the hand-actions frames bucket."
}

output "hand_actions_bucket_self_link" {
  value       = module.hand_actions_bucket.self_link
  description = "Self-link of the hand-actions frames GCS bucket."
}

output "hand_actions_table_id" {
  value       = module.hand_actions_table.table_id
  description = "BigQuery table ID for the hand actions stage table."
}

output "hand_actions_table_full_id" {
  value       = module.hand_actions_table.table_full_id
  description = "Fully qualified hand actions table ID (project:dataset.table)."
}

output "hand_start_processing_attempts_table_id" {
  value       = module.hand_start_processing_attempts_table.table_id
  description = "BigQuery table ID for the hand start processing audit log."
}

output "hand_start_processing_attempts_table_full_id" {
  value       = module.hand_start_processing_attempts_table.table_full_id
  description = "Fully qualified hand start processing attempts table ID."
}

output "clip_materialization_attempts_table_id" {
  value       = module.clip_materialization_attempts_table.table_id
  description = "BigQuery table ID for the clip materialization audit log."
}

output "clip_materialization_attempts_table_full_id" {
  value       = module.clip_materialization_attempts_table.table_full_id
  description = "Fully qualified clip materialization audit log table ID."
}
