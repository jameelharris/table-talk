# The reference images accompany extract_community_cards.md's video scan, and
# gemini_caller wraps each label as "Reference image — {label}:". These tests use
# synthetic files under tmp_path rather than the real references/ directory, so
# the suite does not depend on operator-supplied assets.

from pathlib import Path

import pytest

from table_talk.reference_images import (
    STREET_REFERENCE_ORDER,
    load_reference_images,
    reference_image_filename,
)


def _write_references(directory: Path, suffix: str = ".jpeg") -> dict[str, bytes]:
    directory.mkdir(parents=True, exist_ok=True)
    written = {}
    for street in STREET_REFERENCE_ORDER:
        data = f"{street}-bytes".encode()
        (directory / f"{street}_reference{suffix}").write_bytes(data)
        written[street] = data
    return written


def test_loads_all_three_with_bytes_from_disk(tmp_path):
    written = _write_references(tmp_path)

    images = load_reference_images(tmp_path)

    assert len(images) == 3
    for (data, _mime, label), street in zip(images, STREET_REFERENCE_ORDER, strict=True):
        assert label == street
        assert data == written[street]


def test_order_is_flop_turn_river_not_alphabetical(tmp_path):
    # sorted() over the directory would give flop, river, turn — plausible and
    # wrong, since the scan prompt describes them in play order and each street's
    # description builds on the previous one.
    _write_references(tmp_path)

    labels = [label for _data, _mime, label in load_reference_images(tmp_path)]

    assert labels == ["flop", "turn", "river"]
    assert labels != sorted(labels)


def test_labels_are_bare_street_names(tmp_path):
    # gemini_caller renders "Reference image — {label}:", so a wrapped or
    # filename-derived label here would produce a doubled prefix.
    _write_references(tmp_path)

    for _data, _mime, label in load_reference_images(tmp_path):
        assert label in STREET_REFERENCE_ORDER
        assert "Reference image" not in label
        assert "_reference" not in label


def test_jpeg_files_get_jpeg_mime_type(tmp_path):
    _write_references(tmp_path, suffix=".jpeg")

    assert {mime for _data, mime, _label in load_reference_images(tmp_path)} == {"image/jpeg"}


def test_png_files_get_png_mime_type(tmp_path, monkeypatch):
    # The mime type is derived from the extension rather than hardcoded, so a
    # reference swapped for a PNG later is still sent correctly.
    monkeypatch.setattr(
        "table_talk.reference_images.reference_image_filename",
        lambda street: f"{street}_reference.png",
    )
    _write_references(tmp_path, suffix=".png")

    assert {mime for _data, mime, _label in load_reference_images(tmp_path)} == {"image/png"}


def test_missing_file_raises_naming_the_path(tmp_path):
    _write_references(tmp_path)
    (tmp_path / reference_image_filename("turn")).unlink()

    with pytest.raises(FileNotFoundError, match="turn_reference.jpeg"):
        load_reference_images(tmp_path)


def test_missing_directory_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="flop_reference.jpeg"):
        load_reference_images(tmp_path / "does-not-exist")
