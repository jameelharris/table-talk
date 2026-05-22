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
