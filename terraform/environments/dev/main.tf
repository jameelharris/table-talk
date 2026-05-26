provider "google" {
  project = var.project_id
  region  = var.region
}

module "videos_bucket" {
  source = "../../modules/gcs_bucket"

  name     = "${var.project_id}-videos-${var.environment}"
  location = var.region
  project  = var.project_id
  labels = {
    environment = var.environment
    managed_by  = "terraform"
    purpose     = "videos"
  }
}

module "bigquery_dataset" {
  source = "../../modules/bigquery_dataset"

  dataset_id    = "table_talk_${var.environment}"
  project       = var.project_id
  location      = var.region
  friendly_name = "table-talk ${var.environment}"
  description   = "table-talk analytics dataset (${var.environment} environment)"
  labels = {
    environment = var.environment
    managed_by  = "terraform"
    purpose     = "analytics"
  }
  delete_contents_on_destroy = false
}

module "videos_table" {
  source = "../../modules/bigquery_table"

  dataset_id  = module.bigquery_dataset.dataset_id
  table_id    = "videos"
  project     = var.project_id
  schema      = file("${path.module}/../../../schemas/videos.json")
  description = "Entity table for successfully ingested YouTube poker broadcast videos. One row per video. Immutable once written. Foreign key target for downstream pipeline tables (clips, hands, actions)."
  labels = {
    environment = var.environment
    managed_by  = "terraform"
    purpose     = "video_entity"
  }
  deletion_protection = true
}

module "video_ingestion_attempts_table" {
  source = "../../modules/bigquery_table"

  dataset_id  = module.bigquery_dataset.dataset_id
  table_id    = "video_ingestion_attempts"
  project     = var.project_id
  schema      = file("${path.module}/../../../schemas/video_ingestion_attempts.json")
  description = "Append-only audit log of every video ingestion attempt (success or failure). One row per attempt. Joins to videos via source_url (always present) or video_id (present on successful attempts)."
  labels = {
    environment = var.environment
    managed_by  = "terraform"
    purpose     = "video_ingestion_audit"
  }
  deletion_protection = true
}

module "clip_manifest_table" {
  source = "../../modules/bigquery_table"

  dataset_id  = module.bigquery_dataset.dataset_id
  table_id    = "clip_manifest"
  project     = var.project_id
  schema      = file("${path.module}/../../../schemas/clip_manifest.json")
  description = "Inventory of all clips derived from ingested videos. One row per clip, immutable after write. Foreign key target for clip_processing_attempts and downstream extraction tables."
  labels = {
    environment = var.environment
    managed_by  = "terraform"
    purpose     = "clip_inventory"
  }
  deletion_protection = true
}

module "clip_processing_attempts_table" {
  source = "../../modules/bigquery_table"

  dataset_id  = module.bigquery_dataset.dataset_id
  table_id    = "clip_processing_attempts"
  project     = var.project_id
  schema      = file("${path.module}/../../../schemas/clip_processing_attempts.json")
  description = "Append-only audit log of every clip processing attempt. One row per attempt. Absence of rows for a clip_id means the clip is unprocessed. Joins to clip_manifest via clip_id."
  labels = {
    environment = var.environment
    managed_by  = "terraform"
    purpose     = "clip_processing_audit"
  }
  deletion_protection = true
}
