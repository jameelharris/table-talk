terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }

  backend "gcs" {
    # If you are forking this repo, change this to your own state bucket.
    # Terraform does not allow variables in backend configuration blocks,
    # so this value must be hardcoded and changed manually.
    bucket = "table-talk-497020-tfstate"
    prefix = "terraform/state/dev"
  }
}
