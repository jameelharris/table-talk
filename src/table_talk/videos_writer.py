# Schema source of truth: terraform/environments/dev/schemas/videos.json
# The VideoRow dataclass below must be kept in sync with that file.
# Drift is caught at integration-test time (see tests/test_videos_writer.py).

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions


@dataclass(frozen=True)
class VideoRow:
    video_id: str
    source_url: str
    title: str
    duration_seconds: int
    gcs_path: str
    file_size_bytes: int


class VideosWriteError(Exception):
    pass


def write_video_row(
    row: VideoRow,
    *,
    project: str,
    dataset: str,
    client: bigquery.Client | None = None,
) -> None:
    if client is None:
        client = bigquery.Client(project=project)
    table_ref = f"{project}.{dataset}.videos"
    row_dict = {**asdict(row), "ingested_at": datetime.now(UTC).isoformat()}
    try:
        job = client.load_table_from_json([row_dict], table_ref)
        job.result()
    except gcloud_exceptions.GoogleCloudError as exc:
        raise VideosWriteError(str(exc)) from exc
    if job.errors:
        raise VideosWriteError(f"BQ load job errors: {job.errors}")
