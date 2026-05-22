variable "name" {
  type        = string
  description = "Name of the GCS bucket."
}

variable "location" {
  type        = string
  description = "GCP location for the bucket."
}

variable "project" {
  type        = string
  description = "GCP project ID."
}

variable "versioning_enabled" {
  type        = bool
  description = "Enable object versioning."
  default     = true
}

variable "storage_class" {
  type        = string
  description = "Storage class for the bucket."
  default     = "STANDARD"
}

variable "labels" {
  type        = map(string)
  description = "Labels to apply to the bucket."
  default     = {}
}
