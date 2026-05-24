import pytest

from table_talk.manifest import (
    ManifestError,
    VideoManifestEntry,
    extract_video_id,
    load_manifest,
)

# --- load_manifest ---


def test_load_manifest_happy_path(tmp_path):
    f = tmp_path / "videos.yaml"
    f.write_text("- https://youtu.be/Y6cq2xi0xdE\n- https://youtu.be/sUYLzjgKzM8\n")
    result = load_manifest(f)
    assert result == [
        VideoManifestEntry(source_url="https://youtu.be/Y6cq2xi0xdE"),
        VideoManifestEntry(source_url="https://youtu.be/sUYLzjgKzM8"),
    ]


def test_load_manifest_missing_file(tmp_path):
    with pytest.raises(ManifestError, match="not found"):
        load_manifest(tmp_path / "missing.yaml")


def test_load_manifest_malformed_yaml(tmp_path):
    f = tmp_path / "videos.yaml"
    f.write_text("key: [unclosed\n")
    with pytest.raises(ManifestError, match="Malformed YAML"):
        load_manifest(f)


def test_load_manifest_non_list_root(tmp_path):
    f = tmp_path / "videos.yaml"
    f.write_text("url: https://youtu.be/Y6cq2xi0xdE\n")
    with pytest.raises(ManifestError, match="root must be a list"):
        load_manifest(f)


def test_load_manifest_non_string_entry(tmp_path):
    f = tmp_path / "videos.yaml"
    f.write_text("- https://youtu.be/Y6cq2xi0xdE\n- 42\n")
    with pytest.raises(ManifestError, match="must be a string"):
        load_manifest(f)


def test_load_manifest_empty_string_entry(tmp_path):
    f = tmp_path / "videos.yaml"
    f.write_text('- https://youtu.be/Y6cq2xi0xdE\n- ""\n')
    with pytest.raises(ManifestError, match="empty string"):
        load_manifest(f)


# --- extract_video_id ---


def test_extract_video_id_watch_url():
    assert extract_video_id("https://www.youtube.com/watch?v=Y6cq2xi0xdE") == "Y6cq2xi0xdE"


def test_extract_video_id_short_url():
    assert extract_video_id("https://youtu.be/Y6cq2xi0xdE") == "Y6cq2xi0xdE"


def test_extract_video_id_short_url_with_si_param():
    assert extract_video_id("https://youtu.be/Y6cq2xi0xdE?si=-24QrUubJWD39CIJ") == "Y6cq2xi0xdE"


def test_extract_video_id_watch_url_extra_params():
    assert extract_video_id("https://www.youtube.com/watch?v=sUYLzjgKzM8&t=42s") == "sUYLzjgKzM8"


def test_extract_video_id_no_www():
    assert extract_video_id("https://youtube.com/watch?v=vOyHJa7AJhc") == "vOyHJa7AJhc"


def test_extract_video_id_unparseable():
    with pytest.raises(ManifestError, match="Cannot extract video ID"):
        extract_video_id("https://example.com/not-a-youtube-url")
