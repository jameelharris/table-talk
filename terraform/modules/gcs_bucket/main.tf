resource "google_storage_bucket" "this" {
  name                        = var.name
  location                    = var.location
  project                     = var.project
  storage_class               = var.storage_class
  uniform_bucket_level_access = true
  labels                      = var.labels

  public_access_prevention = "enforced"

  versioning {
    enabled = var.versioning_enabled
  }
}
