variable "dataset_id" {
  type        = string
  description = "The dataset to create the table in."
}

variable "table_id" {
  type        = string
  description = "The table name."
}

variable "project" {
  type        = string
  description = "GCP project ID."
}

variable "schema" {
  type        = string
  description = "JSON-encoded schema for the table."
}

variable "description" {
  type        = string
  description = "A user-friendly description of the table."
  default     = null
}

variable "labels" {
  type        = map(string)
  description = "Labels to apply to the table."
  default     = {}
}

variable "deletion_protection" {
  type        = bool
  description = "If true, prevents Terraform from destroying the table."
  default     = true
}
