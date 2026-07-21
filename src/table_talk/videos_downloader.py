# GCS downloader for ingested video files.
# Parses and validates gs://... URIs before downloading.
# NotFound (404) is re-raised as DownloadPermanentError; other GCS exceptions propagate unwrapped.

import google.api_core.exceptions as api_exc
from google.cloud import storage


class DownloadPermanentError(Exception):
    """Raised when a GCS video object does not exist (404). Retrying will not help."""


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
    try:
        blob.download_to_filename(local_path)
    except api_exc.NotFound as exc:
        raise DownloadPermanentError(
            f"Video object not found: {video_gcs_uri}"
        ) from exc
