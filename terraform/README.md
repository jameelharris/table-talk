# table-talk Terraform

Infrastructure-as-code for the table-talk project — a video-to-BigQuery poker analytics pipeline that downloads YouTube videos via yt-dlp, transcribes them, and loads structured data into BigQuery for analysis.

Currently provisions a single GCS bucket for video storage. Future commits will add a JSON bucket, BigQuery datasets, service accounts, and related resources.

## Prerequisites

- GCP project with billing enabled
- Terraform >= 1.5
- gcloud SDK installed and authenticated

## One-time bootstrap: state bucket

Terraform state is stored in a GCS bucket that must exist before `terraform init` can run. This creates a chicken-and-egg problem — the bucket itself cannot be managed by the Terraform configuration it backs. Create it manually once:

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project <YOUR_PROJECT_ID>

gcloud storage buckets create gs://<YOUR_PROJECT_ID>-tfstate \
  --location=us-east1 \
  --uniform-bucket-level-access \
  --public-access-prevention

gcloud storage buckets update gs://<YOUR_PROJECT_ID>-tfstate --versioning
```

This is intentional and stays outside Terraform.

## Setup

```bash
cd terraform/environments/dev

cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars and set project_id to your GCP project ID.
```

Then open `backend.tf` and change the `bucket` value to your state bucket name (`<YOUR_PROJECT_ID>-tfstate`). Terraform does not allow variables in backend configuration blocks, so this must be edited directly.

```bash
terraform init
terraform plan
terraform apply
```

## What gets created

| Resource | Name pattern | Purpose |
|---|---|---|
| GCS bucket | `<project_id>-videos-dev` | Cache of source videos downloaded from YouTube via yt-dlp, so the pipeline skips re-downloading files it has already processed |

The bucket is configured with versioning, uniform bucket-level access, and public access prevention enforced. No IAM bindings are managed here yet.
