# Records which models and which prompt file versions produced a stage row.
#
# The corpus is a Flash/Pro mixture and nothing in the tables says which row came
# from which: the model that served a call is emitted only on gemini_caller's
# `gemini_usage` stderr line, which is not persisted. This module closes that,
# and extends it to the prompts — a prompt edit changes extraction behaviour
# corpus-wide, and without a recorded version there is no way to tell which side
# of an edit a given row landed on.
#
# The block is a sibling of each phase's own contribution inside its existing
# JSON state column. No schema change: the column is JSON and BigQuery parses it
# server-side, so bq_param_type (which has no float or None branch) is never
# reached by anything in here.
#
# WHY THE GIT BLOB HASH RATHER THAN A PLAIN SHA-256
#
# It is what `git hash-object <file>` prints, which makes the stored value
# directly lookupable: `git log --find-object=<hash>` names the commits carrying
# that exact file version. A bare content digest would identify the bytes without
# connecting them to history. Computing it needs no git binary and no .git
# directory at runtime, so this works in Cloud Run.
#
# Hash the STORED file, never the rendered prompt. A rendered prompt carries
# per-hand context (seat lines, stacks, the FVA block) and would differ on every
# row, recording nothing about the version.

import hashlib
from collections.abc import Iterable
from pathlib import Path

# Enough to be collision-free across a repo's file versions, short enough to read
# in a status message or a query result. `git log --find-object` accepts a
# prefix, so a truncated value stays lookupable.
_HASH_LENGTH = 12


def blob_hash(data: bytes) -> str:
    """The git blob hash of `data`, truncated to 12 hex characters.

    git prefixes the content with "blob <len>\\0" before hashing, which is why
    this does not equal a bare sha1 of the file. SHA-1 is git's format here, not
    a security choice — this identifies a file version, it does not authenticate
    one.
    """
    header = b"blob " + str(len(data)).encode() + b"\0"
    return hashlib.sha1(header + data).hexdigest()[:_HASH_LENGTH]


def hash_files(paths: Iterable[Path], repo_root: Path) -> dict[str, str]:
    """{repo-relative path: blob hash} for each file.

    Keyed by repo-relative POSIX path so the value is stable across machines and
    means the same thing in a query as it does in the repo. Call once per run
    when the phase loads its files, not per row: the files do not change mid-run,
    and re-reading them per hand would add disk I/O to every iteration.
    """
    hashes: dict[str, str] = {}
    for path in paths:
        key = path.resolve().relative_to(repo_root.resolve()).as_posix()
        hashes[key] = blob_hash(path.read_bytes())
    return hashes


def build_provenance(models: dict[str, str], prompts: dict[str, str]) -> dict:
    """The provenance block written as a sibling of a phase's own contribution.

    `models` is keyed by call mode ("clip", "frame") rather than holding one
    value, because every phase but payout extraction makes both kinds of call and
    they can be served by different models — that split is the whole point of
    TT_CLIP_MODEL and TT_FRAME_MODEL being separate.

    `prompts` must contain only the files that actually contributed to THIS row.
    Phase 3 lists the bounty addendum on progressive videos only; Phase 5 lists
    step E's prompts and reference images only when step E ran. Listing a file a
    row never saw would make the record say something false about how it was
    produced.
    """
    return {"models": models, "prompts": prompts}


def select(hashes: dict[str, str], *paths: str) -> dict[str, str]:
    """The subset of `hashes` for the named repo-relative paths.

    Raises on an unknown path rather than skipping it. A silently absent entry
    would under-report what produced a row, and the caller naming a path the CLI
    never hashed is a wiring bug that should fail loudly at the first write.
    """
    missing = [p for p in paths if p not in hashes]
    if missing:
        raise KeyError(f"no hash recorded for: {', '.join(sorted(missing))}")
    return {p: hashes[p] for p in paths}
