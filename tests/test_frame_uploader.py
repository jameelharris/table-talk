import uuid
from unittest.mock import MagicMock, patch

import pytest

from table_talk.frame_uploader import upload_frame


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
        upload_frame("/tmp/frame.jpg", "s3://bucket/obj.jpg", "proj")


def test_empty_bucket_raises():
    with pytest.raises(ValueError, match="empty bucket"):
        upload_frame("/tmp/frame.jpg", "gs:///object.jpg", "proj")


def test_empty_object_path_raises():
    with pytest.raises(ValueError, match="empty object path"):
        upload_frame("/tmp/frame.jpg", "gs://bucket/", "proj")


def test_no_slash_after_bucket_raises():
    with pytest.raises(ValueError, match="non-empty object path"):
        upload_frame("/tmp/frame.jpg", "gs://bucket-only", "proj")


def test_uri_parsed_correctly(tmp_path):
    f = tmp_path / "frame.jpg"
    f.write_bytes(b"fake jpeg")
    mock_client, mock_blob = _mock_gcs_client()

    upload_frame(str(f), "gs://my-bucket/path/to/frame.jpg", "proj", client=mock_client)

    mock_client.bucket.assert_called_once_with("my-bucket")
    mock_client.bucket.return_value.blob.assert_called_once_with("path/to/frame.jpg")
    mock_blob.upload_from_filename.assert_called_once_with(str(f))


def test_upload_called_with_local_path(tmp_path):
    f = tmp_path / "frame.jpg"
    f.write_bytes(b"fake jpeg")
    mock_client, mock_blob = _mock_gcs_client()

    upload_frame(str(f), "gs://bucket/frame.jpg", "proj", client=mock_client)

    mock_blob.upload_from_filename.assert_called_once_with(str(f))


def test_client_none_instantiates_with_project(tmp_path):
    f = tmp_path / "frame.jpg"
    f.write_bytes(b"fake jpeg")
    mock_client, _ = _mock_gcs_client()

    with patch("table_talk.frame_uploader.storage.Client", return_value=mock_client) as mock_cls:
        upload_frame(str(f), "gs://bucket/frame.jpg", "myproj")
        mock_cls.assert_called_once_with(project="myproj")


def test_client_provided_not_instantiated(tmp_path):
    f = tmp_path / "frame.jpg"
    f.write_bytes(b"fake jpeg")
    mock_client, _ = _mock_gcs_client()

    with patch("table_talk.frame_uploader.storage.Client") as mock_cls:
        upload_frame(str(f), "gs://bucket/frame.jpg", "proj", client=mock_client)
        mock_cls.assert_not_called()


# --- integration test ---


@pytest.mark.integration
def test_upload_frame_integration(tmp_path):
    from google.cloud import storage as gcs

    bucket_name = "table-talk-497020-hand-setups-dev"
    object_path = f"test/{uuid.uuid4().hex}/frame.jpg"
    gcs_uri = f"gs://{bucket_name}/{object_path}"

    f = tmp_path / "frame.jpg"
    content = b"integration test frame content"
    f.write_bytes(content)

    client = gcs.Client()
    cleanup_blob = client.bucket(bucket_name).blob(object_path)

    try:
        upload_frame(str(f), gcs_uri, project_id="table-talk-497020", client=client)
        assert cleanup_blob.exists()
    finally:
        if cleanup_blob.exists():
            cleanup_blob.delete()
