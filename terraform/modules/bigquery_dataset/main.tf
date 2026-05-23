resource "google_bigquery_dataset" "this" {
  dataset_id                 = var.dataset_id
  project                    = var.project
  location                   = var.location
  friendly_name              = var.friendly_name
  description                = var.description
  labels                     = var.labels
  delete_contents_on_destroy = var.delete_contents_on_destroy
}
