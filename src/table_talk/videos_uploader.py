# GCS uploader for ingested video files.
# Path scheme: gs://{bucket}/{video_id}.mp4 — deterministic from video_id.
# Idempotent: skips upload if the object already exists.

from dataclasses import dataclass
from pathlib import Path

from google.cloud import exceptions as gcloud_exceptions
from google.cloud import storage


@dataclass(frozen=True)
class UploadResult:
    gcs_uri: str
    size_bytes: int
    already_existed: bool


class UploadError(Exception):
    pass


def upload_video(
    local_path: Path,
    video_id: str,
    *,
    bucket: str,
    client: storage.Client | None = None,
) -> UploadResult:
    if not local_path.is_file():
        raise UploadError(f"Local path does not exist or is not a file: {local_path}")
    if len(video_id) != 11:
        raise UploadError(
            f"video_id must be exactly 11 characters, got {len(video_id)}: {video_id!r}"
        )
    if client is None:
        client = storage.Client()
    bucket_obj = client.bucket(bucket)
    blob = bucket_obj.blob(f"{video_id}.mp4")
    gcs_uri = f"gs://{bucket}/{video_id}.mp4"
    try:
        if blob.exists():
            blob.reload()
            return UploadResult(gcs_uri=gcs_uri, size_bytes=blob.size, already_existed=True)
        blob.upload_from_filename(str(local_path))
        blob.reload()
        return UploadResult(gcs_uri=gcs_uri, size_bytes=blob.size, already_existed=False)
    except gcloud_exceptions.GoogleCloudError as exc:
        raise UploadError(str(exc)) from exc
