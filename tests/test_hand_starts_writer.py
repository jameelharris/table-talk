import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from table_talk.hand_starts_writer import HandStartsRow, HandStartsWriteError, write_hand_starts


def _sample_row(suffix: str = "001", verify_frame_gcs_paths=None) -> HandStartsRow:
    hand_setup_id = f"dQw4w9WgXcQ_001_{suffix}"
    return HandStartsRow(
        hand_start_id=f"{hand_setup_id}_001",
        hand_setup_id=hand_setup_id,
        clip_id="dQw4w9WgXcQ_001",
        video_id="dQw4w9WgXcQ",
        fva_time_seconds=32,
        second_action_time_seconds=35,
        hand_start_state={
            "hand_setup": {"total_seat_count": 6, "pot_size_bb": None, "players": []},
            "fva": {"seat_position_label": "BB", "seat_number": 1, "action_type": "raise", "bet_amount": 3.0},
        },
        fva_frame_gcs_path=f"gs://bucket/{hand_setup_id}_fva.jpg",
        verify_frame_gcs_paths=(
            verify_frame_gcs_paths
            if verify_frame_gcs_paths is not None
            else [f"gs://bucket/{hand_setup_id}_verify_001.jpg"]
        ),
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

    write_hand_starts([row], project_id="proj", dataset="ds", client=mock_client)

    mock_client.query.assert_called_once()
    args, kwargs = mock_client.query.call_args
    query_str = args[0]
    job_config = kwargs["job_config"]
    assert query_str.startswith("INSERT INTO")
    assert "proj.ds.hand_starts" in query_str
    param_names = {p.name for p in job_config.query_parameters}
    expected = {f"{c}_0" for c in asdict(row).keys()}
    assert param_names == expected
    assert not any("detected_at" in n for n in param_names)
    mock_job.result.assert_called_once()


def test_empty_list_is_noop():
    mock_client, _ = _mock_client()

    write_hand_starts([], project_id="proj", dataset="ds", client=mock_client)

    mock_client.query.assert_not_called()


def test_n_rows_single_query():
    mock_client, _ = _mock_client()
    rows = [_sample_row(suffix=f"{i:03d}") for i in range(3)]

    write_hand_starts(rows, project_id="proj", dataset="ds", client=mock_client)

    mock_client.query.assert_called_once()
    args, kwargs = mock_client.query.call_args
    query_str = args[0]
    assert "hand_starts" in query_str
    params = kwargs["job_config"].query_parameters
    col_count = len(asdict(rows[0]))
    assert len(params) == 3 * col_count


def test_json_parameter_type_and_value():
    mock_client, _ = _mock_client()
    row = _sample_row()

    write_hand_starts([row], project_id="proj", dataset="ds", client=mock_client)

    _, kwargs = mock_client.query.call_args
    params = {p.name: p for p in kwargs["job_config"].query_parameters}
    json_param = params["hand_start_state_0"]
    assert json_param.type_ == "JSON"
    assert json_param.to_api_repr()["parameterValue"]["value"] == json.dumps(row.hand_start_state)


def test_no_double_encoding_of_hand_start_state():
    mock_client, _ = _mock_client()
    row = _sample_row()

    write_hand_starts([row], project_id="proj", dataset="ds", client=mock_client)

    _, kwargs = mock_client.query.call_args
    params = {p.name: p for p in kwargs["job_config"].query_parameters}
    p = params["hand_start_state_0"]

    assert p.type_ == "JSON"
    api_value = p.to_api_repr()["parameterValue"]["value"]
    assert api_value == json.dumps(row.hand_start_state)
    assert api_value != json.dumps(json.dumps(row.hand_start_state))


def test_verify_frame_gcs_paths_uses_array_query_parameter():
    mock_client, _ = _mock_client()
    paths = ["gs://bucket/verify_001.jpg", "gs://bucket/verify_002.jpg"]
    row = _sample_row(verify_frame_gcs_paths=paths)

    write_hand_starts([row], project_id="proj", dataset="ds", client=mock_client)

    _, kwargs = mock_client.query.call_args
    params = {p.name: p for p in kwargs["job_config"].query_parameters}
    p = params["verify_frame_gcs_paths_0"]

    assert isinstance(p, bigquery.ArrayQueryParameter)
    assert not isinstance(p, bigquery.ScalarQueryParameter)
    api_repr = p.to_api_repr()
    assert api_repr["parameterType"]["type"] == "ARRAY"
    assert api_repr["parameterType"]["arrayType"]["type"] == "STRING"
    wire_values = [v["value"] for v in api_repr["parameterValue"]["arrayValues"]]
    assert wire_values == paths


def test_verify_frame_gcs_paths_empty_list_wire_value_is_list_not_none():
    mock_client, _ = _mock_client()
    row = _sample_row(verify_frame_gcs_paths=[])

    write_hand_starts([row], project_id="proj", dataset="ds", client=mock_client)

    _, kwargs = mock_client.query.call_args
    params = {p.name: p for p in kwargs["job_config"].query_parameters}
    p = params["verify_frame_gcs_paths_0"]

    assert isinstance(p, bigquery.ArrayQueryParameter)
    api_repr = p.to_api_repr()
    # An empty REPEATED column is an empty array on the wire, never a NULL/None.
    assert api_repr["parameterValue"]["arrayValues"] == []
    assert api_repr["parameterValue"].get("value") is None


def test_job_errors_raises():
    mock_client, _ = _mock_client(job_errors=[{"message": "bad schema"}])

    with pytest.raises(HandStartsWriteError, match="bad schema"):
        write_hand_starts([_sample_row()], project_id="proj", dataset="ds", client=mock_client)


def test_google_cloud_error_raises():
    mock_client = MagicMock()
    err = gcloud_exceptions.GoogleCloudError("network error")
    mock_client.query.side_effect = err

    with pytest.raises(HandStartsWriteError, match="network error"):
        write_hand_starts([_sample_row()], project_id="proj", dataset="ds", client=mock_client)


def test_client_none_instantiates_with_project():
    mock_client, _ = _mock_client()

    with patch("table_talk.hand_starts_writer.bigquery.Client", return_value=mock_client) as mock_cls:
        write_hand_starts([_sample_row()], project_id="myproj", dataset="ds")
        mock_cls.assert_called_once_with(project="myproj")


def test_client_provided_not_instantiated():
    mock_client, _ = _mock_client()

    with patch("table_talk.hand_starts_writer.bigquery.Client") as mock_cls:
        write_hand_starts([_sample_row()], project_id="proj", dataset="ds", client=mock_client)
        mock_cls.assert_not_called()


# --- integration test ---


@pytest.mark.integration
def test_write_hand_starts_integration():
    from table_talk.clip_manifest_writer import ClipManifestRow, write_clip_manifest_rows
    from table_talk.hand_setups_writer import HandSetupsRow, write_hand_setups
    from table_talk.videos_writer import VideosRow, write_video_row

    project = "table-talk-497020"
    dataset = "table_talk_dev"
    uid = uuid.uuid4().hex[:8]
    video_id = f"test_{uid}"
    clip_id = f"{video_id}_001"
    hand_setup_id = f"{clip_id}_001"
    hand_start_id = f"{hand_setup_id}_001"

    client = bigquery.Client(project=project)
    videos_ref = f"{project}.{dataset}.videos"
    clip_ref = f"{project}.{dataset}.clip_manifest"
    hand_setups_ref = f"{project}.{dataset}.hand_setups"
    hand_starts_ref = f"{project}.{dataset}.hand_starts"

    hand_setup_state = {"hand_setup_time_seconds": 30, "total_seat_count": 6, "pot_size_bb": None, "players": []}
    hand_start_state = {
        "hand_setup": hand_setup_state,
        "fva": {"seat_position_label": "BB", "seat_number": 1, "action_type": "raise", "bet_amount": 3.0},
    }
    verify_paths = [
        f"gs://bucket/{hand_start_id}_verify_001.jpg",
        f"gs://bucket/{hand_start_id}_verify_002.jpg",
    ]

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
            [ClipManifestRow(clip_id=clip_id, video_id=video_id, clip_start_time=0, clip_end_time=300)],
            project=project,
            dataset=dataset,
            client=client,
        )
        write_hand_setups(
            [
                HandSetupsRow(
                    hand_setup_id=hand_setup_id,
                    clip_id=clip_id,
                    video_id=video_id,
                    hand_setup_time_seconds=30,
                    frame_gcs_path=f"gs://bucket/{hand_setup_id}.jpg",
                    hand_setup_state=hand_setup_state,
                )
            ],
            project_id=project,
            dataset=dataset,
            client=client,
        )

        write_hand_starts(
            [
                HandStartsRow(
                    hand_start_id=hand_start_id,
                    hand_setup_id=hand_setup_id,
                    clip_id=clip_id,
                    video_id=video_id,
                    fva_time_seconds=32,
                    second_action_time_seconds=35,
                    hand_start_state=hand_start_state,
                    fva_frame_gcs_path=f"gs://bucket/{hand_start_id}_fva.jpg",
                    verify_frame_gcs_paths=verify_paths,
                )
            ],
            project_id=project,
            dataset=dataset,
            client=client,
        )

        query = f"SELECT * FROM `{hand_starts_ref}` WHERE hand_setup_id = @hand_setup_id"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("hand_setup_id", "STRING", hand_setup_id)]
        )
        results = list(client.query(query, job_config=job_config).result())
        assert len(results) == 1

        r = results[0]
        assert r.hand_start_id == hand_start_id
        assert r.fva_time_seconds == 32
        assert r.second_action_time_seconds == 35
        assert list(r.verify_frame_gcs_paths) == verify_paths
        assert r.detected_at is not None
        skew = abs(r.detected_at.replace(tzinfo=UTC) - datetime.now(UTC))
        assert skew < timedelta(seconds=30)
    finally:
        for table, col, val in [
            (hand_starts_ref, "hand_setup_id", hand_setup_id),
            (hand_setups_ref, "hand_setup_id", hand_setup_id),
            (clip_ref, "clip_id", clip_id),
            (videos_ref, "video_id", video_id),
        ]:
            client.query(
                f"DELETE FROM `{table}` WHERE {col} = @val",
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("val", "STRING", val)]
                ),
            ).result()
