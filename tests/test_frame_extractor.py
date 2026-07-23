import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from table_talk.frame_extractor import FrameExtractionError, extract_frame


def _mock_run(returncode=0, stderr=""):
    result = MagicMock()
    result.returncode = returncode
    result.stderr = stderr
    return result


# --- unit tests ---


def test_correct_argv(tmp_path):
    output = str(tmp_path / "frame.jpg")
    mock_result = _mock_run(returncode=0)

    with patch("table_talk.frame_extractor.subprocess.run", return_value=mock_result) as mock_run, \
         patch("table_talk.frame_extractor.os.path.exists", return_value=True):
        extract_frame("https://example.com/video.mp4", 332, output)

    mock_run.assert_called_once_with(
        [
            "ffmpeg", "-y",
            "-ss", "00:05:32.000",
            "-i", "https://example.com/video.mp4",
            "-frames:v", "1",
            "-vf", "unsharp=lx=5:ly=5:la=1.0:cx=5:cy=5:ca=0.0,eq=saturation=2.0",
            "-q:v", "2",
            output,
        ],
        capture_output=True,
        text=True,
    )


def test_timestamp_zero():
    with patch("table_talk.frame_extractor.subprocess.run", return_value=_mock_run()) as mock_run, \
         patch("table_talk.frame_extractor.os.path.exists", return_value=True):
        extract_frame("/path/to/video.mp4", 0, "/tmp/out.jpg")

    argv = mock_run.call_args[0][0]
    assert argv[3] == "00:00:00.000"


def test_timestamp_332():
    with patch("table_talk.frame_extractor.subprocess.run", return_value=_mock_run()) as mock_run, \
         patch("table_talk.frame_extractor.os.path.exists", return_value=True):
        extract_frame("/path/to/video.mp4", 332, "/tmp/out.jpg")

    argv = mock_run.call_args[0][0]
    assert argv[3] == "00:05:32.000"


def test_timestamp_3665():
    with patch("table_talk.frame_extractor.subprocess.run", return_value=_mock_run()) as mock_run, \
         patch("table_talk.frame_extractor.os.path.exists", return_value=True):
        extract_frame("/path/to/video.mp4", 3665, "/tmp/out.jpg")

    argv = mock_run.call_args[0][0]
    assert argv[3] == "01:01:05.000"


def test_timestamp_float():
    with patch("table_talk.frame_extractor.subprocess.run", return_value=_mock_run()) as mock_run, \
         patch("table_talk.frame_extractor.os.path.exists", return_value=True):
        extract_frame("/path/to/video.mp4", 332.5, "/tmp/out.jpg")

    argv = mock_run.call_args[0][0]
    assert argv[3] == "00:05:32.500"


def test_timestamp_float_crossing_hour_boundary():
    with patch("table_talk.frame_extractor.subprocess.run", return_value=_mock_run()) as mock_run, \
         patch("table_talk.frame_extractor.os.path.exists", return_value=True):
        extract_frame("/path/to/video.mp4", 3665.05, "/tmp/out.jpg")

    argv = mock_run.call_args[0][0]
    assert argv[3] == "01:01:05.050"


def test_nonzero_returncode_raises_with_stderr():
    with patch("table_talk.frame_extractor.subprocess.run", return_value=_mock_run(returncode=1, stderr="No such file")):
        with pytest.raises(FrameExtractionError, match="No such file"):
            extract_frame("/missing.mp4", 0, "/tmp/out.jpg")


def test_missing_output_file_raises():
    with patch("table_talk.frame_extractor.subprocess.run", return_value=_mock_run(returncode=0)), \
         patch("table_talk.frame_extractor.os.path.exists", return_value=False):
        with pytest.raises(FrameExtractionError, match="output file not found"):
            extract_frame("/path/to/video.mp4", 0, "/tmp/out.jpg")


# --- integration test ---


@pytest.mark.integration
def test_extract_frame_integration():
    with tempfile.TemporaryDirectory() as tmpdir:
        fixture_path = os.path.join(tmpdir, "test.mp4")
        output_path = os.path.join(tmpdir, "frame.jpg")

        result = subprocess.run(
            [
                "ffmpeg", "-f", "lavfi",
                "-i", "testsrc=duration=10:size=320x240",
                "-y", fixture_path,
            ],
            capture_output=True,
        )
        assert result.returncode == 0, f"Failed to generate fixture: {result.stderr}"

        extract_frame(fixture_path, 5, output_path)

        assert os.path.exists(output_path)
        assert os.path.getsize(output_path) > 0
