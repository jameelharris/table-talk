import uuid
from unittest.mock import MagicMock, patch

import pytest

from table_talk.videos_downloader import download_video


def _mock_gcs_client():
    mock_blob = MagicMock()
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bucket
    return mock_client, mock_blob


# --- unit tests ---


def test_missing_gs_prefix_raises():
    with pytest.raises(ValueError, match="gs://"):
        download_video("s3://bucket/video.mp4", "/tmp/out.mp4", "proj")


def test_empty_bucket_raises():
    with pytest.raises(ValueError, match="empty bucket"):
        download_video("gs:///video.mp4", "/tmp/out.mp4", "proj")


def test_empty_object_path_raises():
    with pytest.raises(ValueError, match="empty object path"):
        download_video("gs://bucket/", "/tmp/out.mp4", "proj")


def test_no_slash_after_bucket_raises():
    with pytest.raises(ValueError, match="non-empty object path"):
        download_video("gs://bucket-only", "/tmp/out.mp4", "proj")


def test_uri_parsed_correctly(tmp_path):
    mock_client, mock_blob = _mock_gcs_client()
    output = str(tmp_path / "video.mp4")

    download_video("gs://my-bucket/path/to/video.mp4", output, "proj", client=mock_client)

    mock_client.bucket.assert_called_once_with("my-bucket")
    mock_client.bucket.return_value.blob.assert_called_once_with("path/to/video.mp4")
    mock_blob.download_to_filename.assert_called_once_with(output)


def test_client_none_instantiates_with_project(tmp_path):
    mock_client, _ = _mock_gcs_client()
    output = str(tmp_path / "video.mp4")

    with patch("table_talk.videos_downloader.storage.Client", return_value=mock_client) as mock_cls:
        download_video("gs://bucket/video.mp4", output, "myproj")
        mock_cls.assert_called_once_with(project="myproj")


def test_client_provided_not_instantiated(tmp_path):
    mock_client, _ = _mock_gcs_client()
    output = str(tmp_path / "video.mp4")

    with patch("table_talk.videos_downloader.storage.Client") as mock_cls:
        download_video("gs://bucket/video.mp4", output, "proj", client=mock_client)
        mock_cls.assert_not_called()


# --- integration test ---


@pytest.mark.integration
def test_download_video_integration(tmp_path):
    from google.cloud import storage as gcs

    bucket_name = "table-talk-497020-videos-dev"
    object_path = f"test/{uuid.uuid4().hex}/video.mp4"
    content = b"integration test video content for download"

    client = gcs.Client()
    cleanup_blob = client.bucket(bucket_name).blob(object_path)
    cleanup_blob.upload_from_string(content)

    output_path = str(tmp_path / "downloaded.mp4")

    try:
        download_video(
            f"gs://{bucket_name}/{object_path}",
            output_path,
            project_id="table-talk-497020",
            client=client,
        )
        with open(output_path, "rb") as f:
            assert f.read() == content
    finally:
        cleanup_blob.delete()
