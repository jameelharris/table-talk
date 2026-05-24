from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml


class ManifestError(Exception):
    pass


@dataclass(frozen=True)
class VideoManifestEntry:
    source_url: str


def load_manifest(path: Path) -> list[VideoManifestEntry]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise ManifestError(f"Manifest file not found: {path}")

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"Malformed YAML in {path}: {exc}") from exc

    if not isinstance(data, list):
        raise ManifestError(f"Manifest root must be a list, got {type(data).__name__}")

    entries = []
    for i, item in enumerate(data):
        if not isinstance(item, str):
            raise ManifestError(f"Entry {i} must be a string, got {type(item).__name__}")
        if not item:
            raise ManifestError(f"Entry {i} is an empty string")
        entries.append(VideoManifestEntry(source_url=item))

    return entries


def extract_video_id(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.removeprefix("www.")

    if host == "youtu.be":
        vid = parsed.path.lstrip("/").split("/")[0]
        if len(vid) == 11:
            return vid

    elif host == "youtube.com":
        qs = parse_qs(parsed.query)
        ids = qs.get("v", [])
        if ids and len(ids[0]) == 11:
            return ids[0]

    raise ManifestError(f"Cannot extract video ID from URL: {url!r}")
