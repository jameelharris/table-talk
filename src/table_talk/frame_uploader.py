# GCS uploader for extracted frame images.
# Parses and validates gs://... URIs before uploading.
# GCS exceptions propagate to the caller without wrapping.

from google.cloud import storage


def upload_frame(
    local_path: str,
    gcs_uri: str,
    project_id: str,
    *,
    client: storage.Client | None = None,
) -> None:
    if not gcs_uri.startswith("gs://"):
        raise ValueError(f"gcs_uri must start with 'gs://', got: {gcs_uri!r}")
    remainder = gcs_uri[len("gs://"):]
    if "/" not in remainder:
        raise ValueError(f"gcs_uri must have a non-empty object path: {gcs_uri!r}")
    bucket_name, object_path = remainder.split("/", 1)
    if not bucket_name:
        raise ValueError(f"gcs_uri has empty bucket: {gcs_uri!r}")
    if not object_path:
        raise ValueError(f"gcs_uri has empty object path: {gcs_uri!r}")

    if client is None:
        client = storage.Client(project=project_id)
    bucket_obj = client.bucket(bucket_name)
    blob = bucket_obj.blob(object_path)
    blob.upload_from_filename(local_path)
