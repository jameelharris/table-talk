from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yt_dlp.utils as ytdl_utils

from table_talk.videos_fetcher import (
    FailureCode,
    FetchResult,
    TerminalFetchError,
    TransientFetchError,
    classify_error,
    fetch_video,
)

VIDEO_ID = "dQw4w9WgXcQ"


# --- classify_error unit tests ---


def test_classify_video_unavailable():
    exc = ytdl_utils.DownloadError("Video unavailable")
    code, kind = classify_error(exc)
    assert code == FailureCode.VIDEO_UNAVAILABLE
    assert kind == "terminal"


def test_classify_private_video():
    exc = ytdl_utils.DownloadError("Private video")
    code, kind = classify_error(exc)
    assert code == FailureCode.VIDEO_UNAVAILABLE
    assert kind == "terminal"


def test_classify_video_removed():
    exc = ytdl_utils.DownloadError("This video has been removed by the user")
    code, kind = classify_error(exc)
    assert code == FailureCode.VIDEO_UNAVAILABLE
    assert kind == "terminal"


def test_classify_age_gated():
    exc = ytdl_utils.DownloadError("Sign in to confirm your age")
    code, kind = classify_error(exc)
    assert code == FailureCode.AGE_GATED
    assert kind == "terminal"


def test_classify_geo_blocked_by_type():
    exc = ytdl_utils.GeoRestrictedError("Not available in your region")
    code, kind = classify_error(exc)
    assert code == FailureCode.GEO_BLOCKED
    assert kind == "terminal"


def test_classify_geo_blocked_by_message():
    exc = ytdl_utils.DownloadError("This video is unavailable in your country")
    code, kind = classify_error(exc)
    assert code == FailureCode.GEO_BLOCKED
    assert kind == "terminal"


def test_classify_unsupported():
    exc = ytdl_utils.UnsupportedError("https://example.com/not-a-video")
    code, kind = classify_error(exc)
    assert code == FailureCode.UNSUPPORTED
    assert kind == "terminal"


def test_classify_bot_detected():
    exc = ytdl_utils.DownloadError("Sign in to confirm you're not a bot")
    code, kind = classify_error(exc)
    assert code == FailureCode.BOT_DETECTED
    assert kind == "terminal"


def test_classify_rate_limited():
    exc = ytdl_utils.DownloadError("HTTP Error 429: Too Many Requests")
    code, kind = classify_error(exc)
    assert code == FailureCode.RATE_LIMITED
    assert kind == "transient"


def test_classify_server_error_500():
    exc = ytdl_utils.DownloadError("HTTP Error 500: Internal Server Error")
    code, kind = classify_error(exc)
    assert code == FailureCode.SERVER_ERROR
    assert kind == "transient"


def test_classify_server_error_503():
    exc = ytdl_utils.DownloadError("HTTP Error 503: Service Unavailable")
    code, kind = classify_error(exc)
    assert code == FailureCode.SERVER_ERROR
    assert kind == "transient"


def test_classify_network_error_timeout():
    exc = ytdl_utils.DownloadError("Read timed out.")
    code, kind = classify_error(exc)
    assert code == FailureCode.NETWORK_ERROR
    assert kind == "transient"


def test_classify_network_error_connection_reset():
    exc = ytdl_utils.DownloadError("Connection reset by peer")
    code, kind = classify_error(exc)
    assert code == FailureCode.NETWORK_ERROR
    assert kind == "transient"


def test_classify_network_error_unreachable():
    exc = ytdl_utils.DownloadError("Network is unreachable")
    code, kind = classify_error(exc)
    assert code == FailureCode.NETWORK_ERROR
    assert kind == "transient"


def test_classify_postprocessing_error():
    exc = ytdl_utils.PostProcessingError("ffmpeg not found")
    code, kind = classify_error(exc)
    assert code == FailureCode.POSTPROCESSING_ERROR
    assert kind == "transient"


def test_classify_http_403():
    exc = ytdl_utils.DownloadError("HTTP Error 403: Forbidden")
    code, kind = classify_error(exc)
    assert code == FailureCode.HTTP_403
    assert kind == "transient"


def test_classify_unknown_fallback():
    exc = Exception("something completely unexpected happened")
    code, kind = classify_error(exc)
    assert code == FailureCode.UNKNOWN
    assert kind == "transient"


def test_classify_order_video_unavailable_beats_http():
    # A message that mentions both "Video unavailable" and an HTTP code —
    # the content signal must win over the HTTP pattern.
    exc = ytdl_utils.DownloadError("Video unavailable; returned HTTP Error 403")
    code, kind = classify_error(exc)
    assert code == FailureCode.VIDEO_UNAVAILABLE
    assert kind == "terminal"


# --- fetch_video unit tests ---


def _make_mock_ydl(info: dict):
    mock_ydl = MagicMock()
    mock_ydl.extract_info.return_value = info
    return mock_ydl


def test_fetch_video_happy_path(tmp_path):
    info = {"id": VIDEO_ID, "title": "Never Gonna Give You Up", "duration": 212}
    (tmp_path / f"{VIDEO_ID}.mp4").write_bytes(b"fake video data")

    with patch("table_talk.videos_fetcher.yt_dlp.YoutubeDL") as mock_cls:
        mock_cls.return_value = _make_mock_ydl(info)
        result = fetch_video(f"https://youtu.be/{VIDEO_ID}", download_dir=tmp_path)

    assert result == FetchResult(
        local_path=tmp_path / f"{VIDEO_ID}.mp4",
        video_id=VIDEO_ID,
        title="Never Gonna Give You Up",
        duration_seconds=212,
    )


def test_fetch_video_terminal_exception(tmp_path):
    with patch("table_talk.videos_fetcher.yt_dlp.YoutubeDL") as mock_cls:
        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = ytdl_utils.DownloadError(
            "Sign in to confirm you're not a bot"
        )
        mock_cls.return_value = mock_ydl

        with pytest.raises(TerminalFetchError) as exc_info:
            fetch_video(f"https://youtu.be/{VIDEO_ID}", download_dir=tmp_path)

    assert exc_info.value.code == FailureCode.BOT_DETECTED


def test_fetch_video_transient_exception(tmp_path):
    with patch("table_talk.videos_fetcher.yt_dlp.YoutubeDL") as mock_cls:
        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = ytdl_utils.DownloadError(
            "HTTP Error 429: Too Many Requests"
        )
        mock_cls.return_value = mock_ydl

        with pytest.raises(TransientFetchError) as exc_info:
            fetch_video(f"https://youtu.be/{VIDEO_ID}", download_dir=tmp_path)

    assert exc_info.value.code == FailureCode.RATE_LIMITED


def test_fetch_video_missing_duration(tmp_path):
    info = {"id": VIDEO_ID, "title": "Test", "duration": None}
    (tmp_path / f"{VIDEO_ID}.mp4").write_bytes(b"fake")

    with patch("table_talk.videos_fetcher.yt_dlp.YoutubeDL") as mock_cls:
        mock_cls.return_value = _make_mock_ydl(info)

        with pytest.raises(TransientFetchError) as exc_info:
            fetch_video(f"https://youtu.be/{VIDEO_ID}", download_dir=tmp_path)

    assert exc_info.value.code == FailureCode.UNKNOWN
    assert "incomplete metadata" in exc_info.value.message


def test_fetch_video_file_not_found_after_success(tmp_path):
    info = {"id": VIDEO_ID, "title": "Test", "duration": 100}
    # Do NOT create the file — simulates yt-dlp reporting success without writing it

    with patch("table_talk.videos_fetcher.yt_dlp.YoutubeDL") as mock_cls:
        mock_cls.return_value = _make_mock_ydl(info)

        with pytest.raises(TransientFetchError) as exc_info:
            fetch_video(f"https://youtu.be/{VIDEO_ID}", download_dir=tmp_path)

    assert exc_info.value.code == FailureCode.UNKNOWN
    assert "file not found" in exc_info.value.message


# --- integration test ---


@pytest.mark.integration
def test_fetch_video_smoke(tmp_path):
    from table_talk.manifest import load_manifest

    manifest = load_manifest(Path("corpus/videos.yaml"))
    url = manifest[0].source_url

    result = fetch_video(url, download_dir=tmp_path)

    assert result.local_path.exists()
    assert result.local_path.stat().st_size > 0
    assert len(result.video_id) == 11
    assert result.title
    assert result.duration_seconds > 0
