import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from google.cloud import bigquery
from google.cloud import exceptions as gcloud_exceptions

from table_talk.hand_actions_writer import HandActionsRow, HandActionsWriteError, write_hand_actions


def _sample_state():
    return {
        "hand_start": {
            "hand_setup": {"total_seat_count": 6, "pot_size_bb": 1.5, "players": []},
            "fva": {
                "seat_position_label": "CO",
                "seat_number": 4,
                "action_type": "raise",
                "bet_amount": 2.5,
            },
        },
        "streets": [
            {
                "street_name": "preflop",
                "street_timestamp": None,
                "community_cards": [],
                "actions": [
                    {
                        "action_order": 1,
                        "seat_position_label": "CO",
                        "action_type": "raise",
                        "bet_amount": 2.5,
                    }
                ],
            },
            {
                "street_name": "flop",
                "street_timestamp": 377,
                "community_cards": ["5d", "8d", "As"],
                "actions": [],
            },
        ],
        "winning_positions": ["CO"],
    }


def _sample_row(suffix: str = "001", street_frame_gcs_paths=None) -> HandActionsRow:
    hand_setup_id = f"dQw4w9WgXcQ_001_{suffix}"
    return HandActionsRow(
        hand_start_id=f"{hand_setup_id}_001",
        hand_setup_id=hand_setup_id,
        clip_id="dQw4w9WgXcQ_001",
        video_id="dQw4w9WgXcQ",
        hand_action_state=_sample_state(),
        street_frame_gcs_paths=(
            street_frame_gcs_paths
            if street_frame_gcs_paths is not None
            else [f"gs://bucket/{hand_setup_id}/flop.jpg"]
        ),
    )


def _mock_client(job_errors=None):
    mock_job = MagicMock()
    mock_job.errors = job_errors
    mock_client = MagicMock()
    mock_client.query.return_value = mock_job
    return mock_client, mock_job


def _params(mock_client):
    _, kwargs = mock_client.query.call_args
    return {p.name: p for p in kwargs["job_config"].query_parameters}


# --- unit tests ---


def test_happy_path():
    mock_client, mock_job = _mock_client()
    row = _sample_row()

    write_hand_actions(
        [row], hand_start_id=row.hand_start_id, project_id="proj", dataset="ds", client=mock_client
    )

    mock_client.query.assert_called_once()
    args, kwargs = mock_client.query.call_args
    query_str = args[0]
    job_config = kwargs["job_config"]

    assert query_str.startswith("BEGIN TRANSACTION")
    begin_idx = query_str.index("BEGIN TRANSACTION")
    delete_idx = query_str.index(
        "DELETE FROM `proj.ds.hand_actions` WHERE hand_start_id = @replace_key"
    )
    insert_idx = query_str.index("INSERT INTO `proj.ds.hand_actions`")
    commit_idx = query_str.index("COMMIT TRANSACTION")
    assert begin_idx < delete_idx < insert_idx < commit_idx

    param_names = {p.name for p in job_config.query_parameters}
    expected = {f"{c}_0" for c in asdict(row).keys()} | {"replace_key"}
    assert param_names == expected
    # detected_at is server-defaulted and omitted from the dataclass entirely.
    assert not any("detected_at" in n for n in param_names)
    mock_job.result.assert_called_once()


def test_empty_list_issues_bare_delete():
    mock_client, _ = _mock_client()

    write_hand_actions(
        [], hand_start_id="dQw4w9WgXcQ_001_001_001", project_id="proj", dataset="ds",
        client=mock_client,
    )

    mock_client.query.assert_called_once()
    args, kwargs = mock_client.query.call_args
    query_str = args[0]
    assert query_str == "DELETE FROM `proj.ds.hand_actions` WHERE hand_start_id = @replace_key"
    assert "BEGIN TRANSACTION" not in query_str
    assert "INSERT" not in query_str

    params = kwargs["job_config"].query_parameters
    assert len(params) == 1
    assert params[0].name == "replace_key"
    assert params[0].value == "dQw4w9WgXcQ_001_001_001"


def test_mismatched_hand_start_id_raises_no_query_issued():
    mock_client, _ = _mock_client()
    row = _sample_row()

    with pytest.raises(HandActionsWriteError, match="hand_start_id"):
        write_hand_actions(
            [row], hand_start_id="some_other_id", project_id="proj", dataset="ds",
            client=mock_client,
        )

    mock_client.query.assert_not_called()


def test_multiple_rows_share_one_statement_though_phase_5_writes_at_most_one():
    # Phase 5 is 1:1 with hand_starts, so in production `rows` is only ever []
    # or a single row — the list exists so that zero-row outcomes can still
    # issue the DELETE. This pins the batch machinery inherited from the
    # hand_starts_writer template; it is not a supported production shape.
    mock_client, _ = _mock_client()
    base = _sample_row()
    rows = [base, replace(base, hand_setup_id="other_but_same_key")]

    write_hand_actions(
        rows, hand_start_id=base.hand_start_id, project_id="proj", dataset="ds", client=mock_client
    )

    mock_client.query.assert_called_once()
    _, kwargs = mock_client.query.call_args
    params = kwargs["job_config"].query_parameters
    col_count = len(asdict(base))
    assert len(params) == 2 * col_count + 1  # +1 for replace_key


def test_json_parameter_type_and_value():
    mock_client, _ = _mock_client()
    row = _sample_row()

    write_hand_actions(
        [row], hand_start_id=row.hand_start_id, project_id="proj", dataset="ds", client=mock_client
    )

    param = _params(mock_client)["hand_action_state_0"]
    assert param.type_ == "JSON"
    assert param.value == row.hand_action_state


def test_no_double_encoding_of_hand_action_state():
    # The dict is handed to BQ as-is with type JSON. A json.dumps() here would
    # arrive as a JSON *string* containing JSON — the regression this pins.
    mock_client, _ = _mock_client()
    row = _sample_row()

    write_hand_actions(
        [row], hand_start_id=row.hand_start_id, project_id="proj", dataset="ds", client=mock_client
    )

    value = _params(mock_client)["hand_action_state_0"].value
    assert isinstance(value, dict)
    assert not isinstance(value, str)
    assert value["streets"][1]["community_cards"] == ["5d", "8d", "As"]


def test_street_frame_gcs_paths_uses_array_query_parameter():
    mock_client, _ = _mock_client()
    paths = [
        "gs://bucket/v/c/hs/flop.jpg",
        "gs://bucket/v/c/hs/turn.jpg",
        "gs://bucket/v/c/hs/river.jpg",
    ]
    row = _sample_row(street_frame_gcs_paths=paths)

    write_hand_actions(
        [row], hand_start_id=row.hand_start_id, project_id="proj", dataset="ds", client=mock_client
    )

    param = _params(mock_client)["street_frame_gcs_paths_0"]
    assert isinstance(param, bigquery.ArrayQueryParameter)
    assert param.values == paths


def test_street_frame_gcs_paths_empty_list_wire_value_is_list_not_none():
    # A hand that ended preflop has no street frames. An empty REPEATED column
    # is an empty array on the wire, never a NULL.
    mock_client, _ = _mock_client()
    row = _sample_row(street_frame_gcs_paths=[])

    write_hand_actions(
        [row], hand_start_id=row.hand_start_id, project_id="proj", dataset="ds", client=mock_client
    )

    param = _params(mock_client)["street_frame_gcs_paths_0"]
    assert isinstance(param, bigquery.ArrayQueryParameter)
    api_repr = param.to_api_repr()
    assert api_repr["parameterValue"]["arrayValues"] == []
    assert api_repr["parameterValue"].get("value") is None


def test_job_errors_raises():
    mock_client, _ = _mock_client(job_errors=[{"message": "bad schema"}])
    row = _sample_row()

    with pytest.raises(HandActionsWriteError, match="bad schema"):
        write_hand_actions(
            [row], hand_start_id=row.hand_start_id, project_id="proj", dataset="ds",
            client=mock_client,
        )


def test_google_cloud_error_raises():
    mock_client = MagicMock()
    mock_client.query.side_effect = gcloud_exceptions.GoogleCloudError("network error")
    row = _sample_row()

    with pytest.raises(HandActionsWriteError, match="network error"):
        write_hand_actions(
            [row], hand_start_id=row.hand_start_id, project_id="proj", dataset="ds",
            client=mock_client,
        )


def test_client_none_instantiates_with_project():
    mock_client, _ = _mock_client()
    row = _sample_row()

    with patch(
        "table_talk.hand_actions_writer.bigquery.Client", return_value=mock_client
    ) as mock_cls:
        write_hand_actions(
            [row], hand_start_id=row.hand_start_id, project_id="myproj", dataset="ds"
        )
        mock_cls.assert_called_once_with(project="myproj")


def test_client_provided_not_instantiated():
    mock_client, _ = _mock_client()
    row = _sample_row()

    with patch("table_talk.hand_actions_writer.bigquery.Client") as mock_cls:
        write_hand_actions(
            [row], hand_start_id=row.hand_start_id, project_id="proj", dataset="ds",
            client=mock_client,
        )
        mock_cls.assert_not_called()


# --- integration tests ---


def _seed_upstream(client, project, dataset, video_id, clip_id, hand_setup_id, hand_start_id):
    """Create the videos -> clip_manifest -> hand_setups -> hand_starts chain
    via each phase's production writer, per CLAUDE.md's cross-phase setup rule."""
    from table_talk._generated.hand_starts_row import HandStartsRow
    from table_talk.clip_manifest_writer import ClipManifestRow, write_clip_manifest_rows
    from table_talk.hand_setups_writer import HandSetupsRow, write_hand_setups
    from table_talk.hand_starts_writer import write_hand_starts
    from table_talk.videos_writer import VideosRow, write_video_row

    hand_setup_state = {"total_seat_count": 6, "pot_size_bb": 1.5, "players": []}

    write_video_row(
        VideosRow(
            video_id=video_id,
            source_url=f"https://www.youtube.com/watch?v={video_id}",
            title="Phase 5 writer integration test",
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
        clip_id=clip_id,
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
                hand_start_state={
                    "hand_setup": hand_setup_state,
                    "fva": {
                        "seat_position_label": "CO",
                        "seat_number": 4,
                        "action_type": "raise",
                        "bet_amount": 2.5,
                    },
                },
                fva_frame_gcs_path=f"gs://bucket/{hand_setup_id}_fva.jpg",
                verify_frame_gcs_paths=[f"gs://bucket/{hand_setup_id}_verify_000.jpg"],
            )
        ],
        hand_setup_id=hand_setup_id,
        project_id=project,
        dataset=dataset,
        client=client,
    )


def _cleanup(client, project, dataset, video_id, clip_id, hand_setup_id, hand_start_id):
    refs = [
        (f"{project}.{dataset}.hand_actions", "hand_start_id", hand_start_id),
        (f"{project}.{dataset}.hand_starts", "hand_setup_id", hand_setup_id),
        (f"{project}.{dataset}.hand_setups", "hand_setup_id", hand_setup_id),
        (f"{project}.{dataset}.clip_manifest", "clip_id", clip_id),
        (f"{project}.{dataset}.videos", "video_id", video_id),
    ]
    for table, col, val in refs:
        client.query(
            f"DELETE FROM `{table}` WHERE {col} = @val",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("val", "STRING", val)]
            ),
        ).result()


@pytest.mark.integration
def test_write_hand_actions_integration():
    project = "table-talk-497020"
    dataset = "table_talk_dev"
    uid = uuid.uuid4().hex[:8]
    video_id = f"test_p5_{uid}"
    clip_id = f"{video_id}_001"
    hand_setup_id = f"{clip_id}_001"
    hand_start_id = f"{hand_setup_id}_001"

    client = bigquery.Client(project=project)
    hand_actions_ref = f"{project}.{dataset}.hand_actions"
    paths = [f"gs://bucket/{video_id}/{clip_id}/{hand_setup_id}/flop.jpg"]

    try:
        _seed_upstream(client, project, dataset, video_id, clip_id, hand_setup_id, hand_start_id)

        first_row = HandActionsRow(
            hand_start_id=hand_start_id,
            hand_setup_id=hand_setup_id,
            clip_id=clip_id,
            video_id=video_id,
            hand_action_state=_sample_state(),
            street_frame_gcs_paths=paths,
        )
        write_hand_actions(
            [first_row], hand_start_id=hand_start_id, project_id=project, dataset=dataset,
            client=client,
        )

        query = f"SELECT * FROM `{hand_actions_ref}` WHERE hand_start_id = @hand_start_id"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("hand_start_id", "STRING", hand_start_id)
            ]
        )
        results = list(client.query(query, job_config=job_config).result())
        assert len(results) == 1

        r = results[0]
        assert r.hand_setup_id == hand_setup_id
        assert r.clip_id == clip_id
        assert r.video_id == video_id
        assert list(r.street_frame_gcs_paths) == paths
        assert r.detected_at is not None
        skew = abs(r.detected_at.replace(tzinfo=UTC) - datetime.now(UTC))
        assert skew < timedelta(seconds=30)

        # The JSON column round-trips as parsed JSON, not as a quoted string.
        state = r.hand_action_state
        assert state["winning_positions"] == ["CO"]
        assert state["streets"][1]["community_cards"] == ["5d", "8d", "As"]
        assert state["streets"][0]["street_timestamp"] is None

        # Reprocess with the same key: REPLACE, not append. This is the direct
        # regression test for the duplicate-row class of bug.
        second_state = _sample_state()
        second_state["winning_positions"] = ["BB"]
        second_row = replace(first_row, hand_action_state=second_state)
        write_hand_actions(
            [second_row], hand_start_id=hand_start_id, project_id=project, dataset=dataset,
            client=client,
        )

        results = list(client.query(query, job_config=job_config).result())
        assert len(results) == 1
        assert results[0].hand_action_state["winning_positions"] == ["BB"]
    finally:
        _cleanup(client, project, dataset, video_id, clip_id, hand_setup_id, hand_start_id)


@pytest.mark.integration
def test_write_hand_actions_integration_zero_row_deletes_existing():
    project = "table-talk-497020"
    dataset = "table_talk_dev"
    uid = uuid.uuid4().hex[:8]
    video_id = f"test_p5_{uid}"
    clip_id = f"{video_id}_001"
    hand_setup_id = f"{clip_id}_001"
    hand_start_id = f"{hand_setup_id}_001"

    client = bigquery.Client(project=project)
    hand_actions_ref = f"{project}.{dataset}.hand_actions"

    try:
        _seed_upstream(client, project, dataset, video_id, clip_id, hand_setup_id, hand_start_id)

        write_hand_actions(
            [
                HandActionsRow(
                    hand_start_id=hand_start_id,
                    hand_setup_id=hand_setup_id,
                    clip_id=clip_id,
                    video_id=video_id,
                    hand_action_state=_sample_state(),
                    street_frame_gcs_paths=[],
                )
            ],
            hand_start_id=hand_start_id,
            project_id=project,
            dataset=dataset,
            client=client,
        )

        query = f"SELECT * FROM `{hand_actions_ref}` WHERE hand_start_id = @hand_start_id"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("hand_start_id", "STRING", hand_start_id)
            ]
        )
        assert len(list(client.query(query, job_config=job_config).result())) == 1

        # Every Phase 5 failure and skip concludes with zero rows, and must
        # clear a row left by a prior successful run rather than skip writing.
        write_hand_actions(
            [], hand_start_id=hand_start_id, project_id=project, dataset=dataset, client=client
        )

        assert list(client.query(query, job_config=job_config).result()) == []
    finally:
        _cleanup(client, project, dataset, video_id, clip_id, hand_setup_id, hand_start_id)
