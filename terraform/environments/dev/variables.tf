variable "project_id" {
  type        = string
  description = "GCP project ID. Set this in terraform.tfvars — do not commit that file."
}

variable "region" {
  type        = string
  description = "GCP region."
  default     = "us-east1"
}

variable "environment" {
  type        = string
  description = "Environment name."
  default     = "dev"
}
