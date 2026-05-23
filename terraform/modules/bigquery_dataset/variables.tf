variable "dataset_id" {
  type        = string
  description = "The dataset name, e.g. \"table_talk_dev\"."
}

variable "project" {
  type        = string
  description = "GCP project ID."
}

variable "location" {
  type        = string
  description = "GCP location for the dataset."
}

variable "friendly_name" {
  type        = string
  description = "A human-readable name for the dataset."
  default     = null
}

variable "description" {
  type        = string
  description = "A user-friendly description of the dataset."
  default     = null
}

variable "labels" {
  type        = map(string)
  description = "Labels to apply to the dataset."
  default     = {}
}

variable "delete_contents_on_destroy" {
  type        = bool
  description = "If true, terraform destroy deletes all tables in the dataset. If false, destroy fails if tables exist."
  default     = false
}
