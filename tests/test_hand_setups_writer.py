import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from table_talk.hand_setups_writer import HandSetupsRow, HandSetupsWriteError, write_hand_setups


def _sample_row(suffix: str = "0") -> HandSetupsRow:
    return HandSetupsRow(
        hand_setup_id=f"dQw4w9WgXcQ_001_{suffix:>03}",
        clip_id="dQw4w9WgXcQ_001",
        video_id="dQw4w9WgXcQ",
        hand_setup_time_seconds=30,
        frame_gcs_path="gs://table-talk-hand-setups/dQw4w9WgXcQ_001_001.jpg",
        hand_setup_state={"hand_setup_time_seconds": 30, "total_seat_count": 6, "pot_size_bb": None, "players": []},
    )


def _mock_client(job_errors=None):
    mock_job = MagicMock()
    mock_job.errors = job_errors
    mock_client = MagicMock()
    mock_client.query.return_value = mock_job
    return mock_client, mock_job


# --- unit tests ---


def test_happy_path():
    mock_client, mock_job = _mock_client()
    row = _sample_row()

    write_hand_setups([row], project_id="proj", dataset="ds", client=mock_client)

    mock_client.query.assert_called_once()
    args, kwargs = mock_client.query.call_args
    query_str = args[0]
    job_config = kwargs["job_config"]
    assert query_str.startswith("INSERT INTO")
    assert "proj.ds.hand_setups" in query_str
    param_names = {p.name for p in job_config.query_parameters}
    expected = {f"{c}_0" for c in asdict(row).keys()}
    assert param_names == expected
    assert not any("detected_at" in n for n in param_names)
    mock_job.result.assert_called_once()


def test_empty_list_is_noop():
    mock_client, _ = _mock_client()

    write_hand_setups([], project_id="proj", dataset="ds", client=mock_client)

    mock_client.query.assert_not_called()


def test_n_rows_single_query():
    mock_client, _ = _mock_client()
    rows = [
        HandSetupsRow(
            hand_setup_id=f"dQw4w9WgXcQ_001_{i:03d}",
            clip_id="dQw4w9WgXcQ_001",
            video_id="dQw4w9WgXcQ",
            hand_setup_time_seconds=i * 30,
            frame_gcs_path=f"gs://bucket/frame_{i}.jpg",
            hand_setup_state={"hand_setup_time_seconds": i * 30, "total_seat_count": 6, "pot_size_bb": None, "players": []},
        )
        for i in range(3)
    ]

    write_hand_setups(rows, project_id="proj", dataset="ds", client=mock_client)

    mock_client.query.assert_called_once()
    args, kwargs = mock_client.query.call_args
    query_str = args[0]
    assert "hand_setups" in query_str
    params = kwargs["job_config"].query_parameters
    col_count = len(asdict(rows[0]))
    assert len(params) == 3 * col_count


def test_json_parameter_type_and_value():
    mock_client, _ = _mock_client()
    state = {"hand_setup_time_seconds": 30, "total_seat_count": 6, "pot_size_bb": None, "players": []}
    row = HandSetupsRow(
        hand_setup_id="vid_001_001",
        clip_id="vid_001",
        video_id="vid",
        hand_setup_time_seconds=30,
        frame_gcs_path="gs://bucket/frame.jpg",
        hand_setup_state=state,
    )

    write_hand_setups([row], project_id="proj", dataset="ds", client=mock_client)

    _, kwargs = mock_client.query.call_args
    params = {p.name: p for p in kwargs["job_config"].query_parameters}
    json_param = params["hand_setup_state_0"]
    assert json_param.type_ == "JSON"
    assert json_param.value == json.dumps(state)


def test_job_errors_raises():
    mock_client, _ = _mock_client(job_errors=[{"message": "bad schema"}])

    with pytest.raises(HandSetupsWriteError, match="bad schema"):
        write_hand_setups([_sample_row()], project_id="proj", dataset="ds", client=mock_client)


def test_google_cloud_error_raises():
    mock_client = MagicMock()
    err = gcloud_exceptions.GoogleCloudError("network error")
    mock_client.query.side_effect = err

    with pytest.raises(HandSetupsWriteError, match="network error"):
        write_hand_setups([_sample_row()], project_id="proj", dataset="ds", client=mock_client)


def test_client_none_instantiates_with_project():
    mock_client, _ = _mock_client()

    with patch("table_talk.hand_setups_writer.bigquery.Client", return_value=mock_client) as mock_cls:
        write_hand_setups([_sample_row()], project_id="myproj", dataset="ds")
        mock_cls.assert_called_once_with(project="myproj")


def test_client_provided_not_instantiated():
    mock_client, _ = _mock_client()

    with patch("table_talk.hand_setups_writer.bigquery.Client") as mock_cls:
        write_hand_setups([_sample_row()], project_id="proj", dataset="ds", client=mock_client)
        mock_cls.assert_not_called()


# --- integration test ---


@pytest.mark.integration
def test_write_hand_setups_integration():
    from table_talk.clip_manifest_writer import ClipManifestRow, write_clip_manifest_rows
    from table_talk.videos_writer import VideosRow, write_video_row

    project = "table-talk-497020"
    dataset = "table_talk_dev"
    uid = uuid.uuid4().hex[:8]
    video_id = f"test_{uid}"
    clip_id = f"{video_id}_001"
    hand_setup_id_1 = f"{clip_id}_001"
    hand_setup_id_2 = f"{clip_id}_002"

    client = bigquery.Client(project=project)
    videos_ref = f"{project}.{dataset}.videos"
    clip_ref = f"{project}.{dataset}.clip_manifest"
    hand_setups_ref = f"{project}.{dataset}.hand_setups"

    state_1 = {"hand_setup_time_seconds": 30, "total_seat_count": 6, "pot_size_bb": None, "players": []}
    state_2 = {"hand_setup_time_seconds": 90, "total_seat_count": 6, "pot_size_bb": 12.5, "players": []}

    try:
        write_video_row(
            VideosRow(
                video_id=video_id,
                source_url=f"https://www.youtube.com/watch?v={video_id}",
                title="Integration Test Video",
                duration_seconds=300,
                gcs_path=f"gs://table-talk-videos/{video_id}.mp4",
                file_size_bytes=12345,
            ),
            project=project,
            dataset=dataset,
            client=client,
        )
        write_clip_manifest_rows(
            [
                ClipManifestRow(
                    clip_id=clip_id,
                    video_id=video_id,
                    clip_start_time=0,
                    clip_end_time=300,
                )
            ],
            project=project,
            dataset=dataset,
            client=client,
        )
        write_hand_setups(
            [
                HandSetupsRow(
                    hand_setup_id=hand_setup_id_1,
                    clip_id=clip_id,
                    video_id=video_id,
                    hand_setup_time_seconds=30,
                    frame_gcs_path=f"gs://bucket/{hand_setup_id_1}.jpg",
                    hand_setup_state=state_1,
                ),
                HandSetupsRow(
                    hand_setup_id=hand_setup_id_2,
                    clip_id=clip_id,
                    video_id=video_id,
                    hand_setup_time_seconds=90,
                    frame_gcs_path=f"gs://bucket/{hand_setup_id_2}.jpg",
                    hand_setup_state=state_2,
                ),
            ],
            project_id=project,
            dataset=dataset,
            client=client,
        )

        query = f"SELECT * FROM `{hand_setups_ref}` WHERE clip_id = @clip_id ORDER BY hand_setup_time_seconds"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("clip_id", "STRING", clip_id)]
        )
        results = list(client.query(query, job_config=job_config).result())
        assert len(results) == 2

        r1, r2 = results
        assert r1.hand_setup_id == hand_setup_id_1
        assert r1.video_id == video_id
        assert r1.hand_setup_time_seconds == 30
        assert r2.hand_setup_id == hand_setup_id_2
        assert r2.hand_setup_time_seconds == 90
        assert r1.detected_at is not None
        skew = abs(r1.detected_at.replace(tzinfo=UTC) - datetime.now(UTC))
        assert skew < timedelta(seconds=30)
    finally:
        client.query(
            f"DELETE FROM `{hand_setups_ref}` WHERE clip_id = @clip_id",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("clip_id", "STRING", clip_id)]
            ),
        ).result()
        client.query(
            f"DELETE FROM `{clip_ref}` WHERE clip_id = @clip_id",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("clip_id", "STRING", clip_id)]
            ),
        ).result()
        client.query(
            f"DELETE FROM `{videos_ref}` WHERE video_id = @video_id",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("video_id", "STRING", video_id)]
            ),
        ).result()
