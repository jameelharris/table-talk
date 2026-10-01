import subprocess
from pathlib import Path

import pytest

from table_talk.provenance import blob_hash, build_provenance, hash_files, select

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "prompts"
REFERENCES_DIR = REPO_ROOT / "references"


def _tracked_files():
    return sorted(PROMPTS_DIR.glob("*.md")) + sorted(REFERENCES_DIR.iterdir())


def _git_hash_object(path: Path) -> str:
    result = subprocess.run(
        ["git", "hash-object", str(path)],
        capture_output=True, text=True, check=True, cwd=REPO_ROOT,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# The hash is git's, and that is the whole point — a recorded value has to be
# lookupable with `git log --find-object`. Asserted against the real binary
# rather than a stored fixture: a fixture would only prove we still agree with
# ourselves.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _tracked_files(), ids=lambda p: p.name)
def test_blob_hash_matches_git_hash_object(path):
    assert blob_hash(path.read_bytes()) == _git_hash_object(path)[:12]


def test_every_prompt_and_reference_is_covered():
    """The parametrization above is only as good as what it enumerates."""
    names = {p.name for p in _tracked_files()}
    assert "extract_player_actions.md" in names
    assert "flop_reference.jpeg" in names
    # 9 prompts + 3 street references + 4 suit references; a new file must
    # arrive with a deliberate update here rather than silently unhashed.
    #
    # The four suit PNGs are measured-and-rejected candidates that no phase
    # loads (see ARCHITECTURE, "Suit misreads are a resolution problem"). They
    # are counted because they are on disk, which is what this assertion is for.
    # They are committed rather than deleted because the harness needs them to
    # reproduce the rejected arms. If they are ever removed, this goes back
    # to 12 in the same change.
    assert len(names) == 16


def test_blob_hash_is_not_a_bare_sha1_of_the_content():
    """git prefixes "blob <len>\\0". Getting this wrong yields a plausible
    12-hex string that no git command can find."""
    import hashlib
    data = b"hello"
    assert blob_hash(data) != hashlib.sha1(data).hexdigest()[:12]


def test_hash_files_keys_are_repo_relative_posix_paths():
    hashes = hash_files([PROMPTS_DIR / "extract_results.md"], REPO_ROOT)
    assert list(hashes) == ["prompts/extract_results.md"]


def test_hash_files_keys_do_not_depend_on_how_the_path_was_spelled():
    """A '..'-laden or symlinked path must key the same as a clean one, or the
    same file lands under two keys across environments."""
    awkward = PROMPTS_DIR / ".." / "prompts" / "extract_results.md"
    assert hash_files([awkward], REPO_ROOT) == hash_files(
        [PROMPTS_DIR / "extract_results.md"], REPO_ROOT
    )


def test_select_returns_only_the_named_subset():
    hashes = {"a": "1", "b": "2", "c": "3"}
    assert select(hashes, "a", "c") == {"a": "1", "c": "3"}


def test_select_raises_on_an_unknown_path():
    """A silently-absent entry would under-report what produced a row."""
    with pytest.raises(KeyError, match="nope"):
        select({"a": "1"}, "a", "nope")


def test_build_provenance_shape():
    block = build_provenance(
        {"clip": "m1"}, {"clip": "MEDIA_RESOLUTION_UNSPECIFIED"}, {"prompts/x.md": "abc"}
    )
    assert block == {
        "models": {"clip": "m1"},
        "media_resolution": {"clip": "MEDIA_RESOLUTION_UNSPECIFIED"},
        "prompts": {"prompts/x.md": "abc"},
    }


def test_media_resolution_must_cover_the_same_call_modes_as_models():
    """A row that records a model for a mode must record its resolution too.

    The two are read together — "which rows were produced at ULTRA_HIGH" is only
    answerable if every row naming a frame call also names its resolution. A
    mismatch here means a phase added a call mode to one dict and not the other,
    which would leave a silent hole in exactly the query this block exists for.
    """
    with pytest.raises(ValueError, match="same call modes"):
        build_provenance({"clip": "m1", "frame": "m2"}, {"clip": "LOW"}, {})

    with pytest.raises(ValueError, match="same call modes"):
        build_provenance({"frame": "m2"}, {"clip": "LOW", "frame": "HIGH"}, {})
