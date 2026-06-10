# GCS downloader for ingested video files.
# Parses and validates gs://... URIs before downloading.
# GCS exceptions propagate to the caller without wrapping.

from google.cloud import storage


def download_video(
    video_gcs_uri: str,
    local_path: str,
    project_id: str,
    *,
    client: storage.Client | None = None,
) -> None:
    if not video_gcs_uri.startswith("gs://"):
        raise ValueError(f"video_gcs_uri must start with 'gs://', got: {video_gcs_uri!r}")
    remainder = video_gcs_uri[len("gs://"):]
    if "/" not in remainder:
        raise ValueError(f"video_gcs_uri must have a non-empty object path: {video_gcs_uri!r}")
    bucket_name, object_path = remainder.split("/", 1)
    if not bucket_name:
        raise ValueError(f"video_gcs_uri has empty bucket: {video_gcs_uri!r}")
    if not object_path:
        raise ValueError(f"video_gcs_uri has empty object path: {video_gcs_uri!r}")

    if client is None:
        client = storage.Client(project=project_id)
    bucket_obj = client.bucket(bucket_name)
    blob = bucket_obj.blob(object_path)
    blob.download_to_filename(local_path)
