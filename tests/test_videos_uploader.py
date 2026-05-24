import uuid
from unittest.mock import MagicMock, patch

import pytest
from google.cloud import exceptions as gcloud_exceptions

from table_talk.videos_uploader import UploadError, UploadResult, upload_video

BUCKET = "test-bucket"
VIDEO_ID = "dQw4w9WgXcQ"  # 11 chars


def _mock_gcs_client(exists=False, blob_size=100):
    mock_blob = MagicMock()
    mock_blob.exists.return_value = exists
    mock_blob.size = blob_size
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bucket
    return mock_client, mock_blob


# --- unit tests ---


def test_upload_new_object(tmp_path):
    f = tmp_path / "video.mp4"
    f.write_bytes(b"fake content")
    mock_client, mock_blob = _mock_gcs_client(exists=False, blob_size=12)

    result = upload_video(f, VIDEO_ID, bucket=BUCKET, client=mock_client)

    mock_blob.upload_from_filename.assert_called_once_with(str(f))
    mock_blob.reload.assert_called_once()
    assert result == UploadResult(
        gcs_uri=f"gs://{BUCKET}/{VIDEO_ID}.mp4",
        size_bytes=12,
        already_existed=False,
    )


def test_upload_already_exists(tmp_path):
    f = tmp_path / "video.mp4"
    f.write_bytes(b"fake content")
    mock_client, mock_blob = _mock_gcs_client(exists=True, blob_size=12)

    result = upload_video(f, VIDEO_ID, bucket=BUCKET, client=mock_client)

    mock_blob.upload_from_filename.assert_not_called()
    mock_blob.reload.assert_called_once()
    assert result == UploadResult(
        gcs_uri=f"gs://{BUCKET}/{VIDEO_ID}.mp4",
        size_bytes=12,
        already_existed=True,
    )


def test_local_file_not_exist(tmp_path):
    mock_client, _ = _mock_gcs_client()
    missing = tmp_path / "missing.mp4"

    with pytest.raises(UploadError):
        upload_video(missing, VIDEO_ID, bucket=BUCKET, client=mock_client)
    mock_client.bucket.assert_not_called()


def test_local_path_is_directory(tmp_path):
    mock_client, _ = _mock_gcs_client()

    with pytest.raises(UploadError):
        upload_video(tmp_path, VIDEO_ID, bucket=BUCKET, client=mock_client)
    mock_client.bucket.assert_not_called()


def test_invalid_video_id(tmp_path):
    f = tmp_path / "video.mp4"
    f.write_bytes(b"fake content")
    mock_client, _ = _mock_gcs_client()

    with pytest.raises(UploadError, match="11 characters"):
        upload_video(f, "short", bucket=BUCKET, client=mock_client)
    mock_client.bucket.assert_not_called()


def test_google_cloud_error_from_exists(tmp_path):
    f = tmp_path / "video.mp4"
    f.write_bytes(b"fake content")
    mock_client, mock_blob = _mock_gcs_client()
    mock_blob.exists.side_effect = gcloud_exceptions.GoogleCloudError("network error")

    with pytest.raises(UploadError, match="network error"):
        upload_video(f, VIDEO_ID, bucket=BUCKET, client=mock_client)


def test_google_cloud_error_from_upload(tmp_path):
    f = tmp_path / "video.mp4"
    f.write_bytes(b"fake content")
    mock_client, mock_blob = _mock_gcs_client(exists=False)
    mock_blob.upload_from_filename.side_effect = gcloud_exceptions.GoogleCloudError("upload failed")

    with pytest.raises(UploadError, match="upload failed"):
        upload_video(f, VIDEO_ID, bucket=BUCKET, client=mock_client)


def test_client_none_instantiates(tmp_path):
    f = tmp_path / "video.mp4"
    f.write_bytes(b"fake content")
    mock_client, _ = _mock_gcs_client(exists=False, blob_size=12)

    with patch("table_talk.videos_uploader.storage.Client", return_value=mock_client) as mock_cls:
        upload_video(f, VIDEO_ID, bucket=BUCKET)
        mock_cls.assert_called_once()


def test_client_provided_not_instantiated(tmp_path):
    f = tmp_path / "video.mp4"
    f.write_bytes(b"fake content")
    mock_client, _ = _mock_gcs_client(exists=False, blob_size=12)

    with patch("table_talk.videos_uploader.storage.Client") as mock_cls:
        upload_video(f, VIDEO_ID, bucket=BUCKET, client=mock_client)
        mock_cls.assert_not_called()


# --- integration test ---


@pytest.mark.integration
def test_upload_video_integration(tmp_path):
    from google.cloud import storage as gcs

    bucket_name = "table-talk-497020-videos-dev"
    video_id = f"test{uuid.uuid4().hex[:7]}"  # 4 + 7 = 11 chars
    f = tmp_path / f"{video_id}.mp4"
    content = b"integration test content"
    f.write_bytes(content)

    client = gcs.Client()
    cleanup_blob = client.bucket(bucket_name).blob(f"{video_id}.mp4")

    try:
        result = upload_video(f, video_id, bucket=bucket_name, client=client)
        assert result.already_existed is False
        assert result.size_bytes == len(content)
        assert result.gcs_uri == f"gs://{bucket_name}/{video_id}.mp4"

        result2 = upload_video(f, video_id, bucket=bucket_name, client=client)
        assert result2.already_existed is True
        assert result2.size_bytes == len(content)
    finally:
        cleanup_blob.delete()
