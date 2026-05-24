# yt-dlp wrapper for downloading videos and extracting metadata.
# Produces a local .mp4 file at {download_dir}/{video_id}.mp4
# (remuxing if YouTube serves a different container).
# Requires ffmpeg on the system PATH.
# Failures are classified into FailureCode and surfaced as
# TerminalFetchError or TransientFetchError.

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yt_dlp
import yt_dlp.utils as ytdl_utils


class FailureCode(StrEnum):
    # Terminal — do not retry
    VIDEO_UNAVAILABLE = "video_unavailable"
    AGE_GATED = "age_gated"
    GEO_BLOCKED = "geo_blocked"
    UNSUPPORTED = "unsupported"
    BOT_DETECTED = "bot_detected"

    # Transient — retry-eligible
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    NETWORK_ERROR = "network_error"
    POSTPROCESSING_ERROR = "postprocessing_error"
    HTTP_403 = "http_403"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FetchResult:
    local_path: Path
    video_id: str
    title: str
    duration_seconds: int


class TerminalFetchError(Exception):
    def __init__(self, code: FailureCode, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")


class TransientFetchError(Exception):
    def __init__(self, code: FailureCode, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code.value}: {message}")


def classify_error(exc: Exception) -> tuple[FailureCode, Literal["terminal", "transient"]]:
    # Type-specific checks first (most precise, avoid message-text ambiguity)
    if isinstance(exc, ytdl_utils.GeoRestrictedError):
        return FailureCode.GEO_BLOCKED, "terminal"
    if isinstance(exc, ytdl_utils.UnsupportedError):
        return FailureCode.UNSUPPORTED, "terminal"
    if isinstance(exc, ytdl_utils.PostProcessingError):
        return FailureCode.POSTPROCESSING_ERROR, "transient"

    msg = str(exc)

    # Content-availability patterns before HTTP codes: some unavailability errors
    # surface alongside HTTP status codes, and the content signal is more actionable.
    if "Video unavailable" in msg:
        return FailureCode.VIDEO_UNAVAILABLE, "terminal"
    if "Private video" in msg:
        return FailureCode.VIDEO_UNAVAILABLE, "terminal"
    if "This video has been removed" in msg:
        return FailureCode.VIDEO_UNAVAILABLE, "terminal"
    if "Sign in to confirm your age" in msg:
        return FailureCode.AGE_GATED, "terminal"
    if "Sign in to confirm you're not a bot" in msg:
        return FailureCode.BOT_DETECTED, "terminal"
    if "unavailable in your country" in msg:
        return FailureCode.GEO_BLOCKED, "terminal"

    # HTTP / network patterns
    if "HTTP Error 429" in msg:
        return FailureCode.RATE_LIMITED, "transient"
    if re.search(r"HTTP Error 5\d\d", msg):
        return FailureCode.SERVER_ERROR, "transient"
    network_patterns = ("Read timed out", "Connection reset", "Network is unreachable", "timeout")
    if any(p in msg for p in network_patterns):
        return FailureCode.NETWORK_ERROR, "transient"
    if "HTTP Error 403" in msg:
        return FailureCode.HTTP_403, "transient"

    return FailureCode.UNKNOWN, "transient"


def fetch_video(url: str, *, download_dir: Path) -> FetchResult:
    opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "outtmpl": str(download_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
    }
    try:
        info = yt_dlp.YoutubeDL(opts).extract_info(url, download=True)
    except Exception as exc:
        code, classification = classify_error(exc)
        if classification == "terminal":
            raise TerminalFetchError(code, str(exc)) from exc
        raise TransientFetchError(code, str(exc)) from exc

    video_id = info.get("id")
    title = info.get("title")
    duration = info.get("duration")

    if not video_id or not title or duration is None:
        pairs = (("id", video_id), ("title", title), ("duration", duration))
        missing = [k for k, v in pairs if not v and v != 0]
        raise TransientFetchError(
            FailureCode.UNKNOWN,
            f"yt-dlp returned incomplete metadata: missing {missing}",
        )

    local_path = download_dir / f"{video_id}.mp4"
    if not local_path.exists():
        raise TransientFetchError(
            FailureCode.UNKNOWN,
            "yt-dlp reported success but file not found",
        )

    return FetchResult(
        local_path=local_path,
        video_id=video_id,
        title=title,
        duration_seconds=int(duration),
    )
