#!/usr/bin/env python
"""Reproduce the card-reading Gemini calls against real stored content.

CLAUDE.md's regression guard for prompts is reproduction: before changing a
prompt, reproduce the exact failing call against the real frame and confirm the
fix against both the failing case and a control. Until now that harness was a
gitignored notebook rebuilt from scratch each time. This is the tracked version,
covering the two frame-mode card reads:

  hole  -> prompts/extract_hole_cards.md        (Phase 4, step C)
  board -> prompts/extract_community_cards.md   (Phase 5, step E)

It renders prompts with the production context builders and calls the production
gemini_caller, so what it measures is what the pipeline does.

WHY FRAMES ARE RE-EXTRACTED RATHER THAN PULLED FROM GCS
-------------------------------------------------------
The manifest is keyed by (video_id, timestamp), never by a row id or a stored
frame path. Ids are positional and renumber on re-detection, and a Phase 4
re-run overwrites fva.jpg at exactly the paths a stored-frame manifest would
name -- so either anchor would silently come to point at different pixels. The
source video does not change, so re-extracting through frame_extractor with the
production ffmpeg filters is the only stable anchor.

Usage:
    uv run python scripts/repro_card_read.py --version A --reps 5
    uv run python scripts/repro_card_read.py --version A --reps 0   # frames only, no calls
    uv run python scripts/repro_card_read.py --version C --reps 20 --only-known-misreads
"""

import argparse
import contextlib
import io
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from table_talk.card_normalization import normalize_cards  # noqa: E402
from table_talk.frame_extractor import extract_frame  # noqa: E402
from table_talk.gemini_caller import (  # noqa: E402
    FRAME_MODEL,
    FRAME_RESOLUTION_ULTRA_HIGH,
    GeminiPermanentError,
    call_gemini_for_frame,
)
from table_talk.prompt_context import (  # noqa: E402
    build_hole_card_context,
    build_prior_cards_context,
)
from table_talk.videos_downloader import download_video  # noqa: E402

PROJECT = "table-talk-497020"
VIDEOS_BUCKET = "table-talk-497020-videos-dev"
REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "prompts"
REFERENCES_DIR = REPO_ROOT / "references"

# Matches hand_action_processing.FRAME_SETTLE_OFFSET: step E extracts half a
# second after the scan's timestamp to clear the dealing animation.
FRAME_SETTLE_OFFSET = 0.5

# Order is fixed here, never from a directory listing -- the same rule
# STREET_REFERENCE_ORDER encodes for the street references.
SUIT_REFERENCES = (
    ("spades", "spade_reference.png"),
    ("hearts", "heart_reference.png"),
    ("diamonds", "diamond_reference.png"),
    ("clubs", "club_reference.png"),
)

# Both card prompts carry this header; the instruction goes directly under it.
_CARD_READING_HEADER = "# CARD READING\n"

SUIT_ANCHOR_INSTRUCTION = """
For each card, find the rank in the card's top-left corner. The suit is the symbol
directly below the rank. Read the suit from that symbol only. Ignore the artwork and
any other symbols on the card. Use both the symbol's shape and its color; they should
agree. If that symbol is covered or unclear, or its shape and color point to different
suits, return the card as null rather than guessing.
"""

SUIT_REFERENCE_SECTION = """
# SUIT VISUAL REFERENCE

Four reference images are provided alongside this frame, one per suit, labelled
"Reference image — spades", "Reference image — hearts", "Reference image — diamonds"
and "Reference image — clubs". Each shows a rank with its suit symbol underneath,
cropped from this broadcast. They illustrate where the suit sits under the rank and
how each suit looks here. The rank shown in a reference is only an example — read
ranks from the frame, not from the references.
"""

VERSIONS = {
    "A": {"instruction": False, "ultra_high": False, "references": False},
    "B": {"instruction": True, "ultra_high": False, "references": False},
    "C": {"instruction": True, "ultra_high": True, "references": False},
    "D": {"instruction": True, "ultra_high": True, "references": True},
    "E": {"instruction": True, "ultra_high": False, "references": True},
    # F isolates ultra_high. B showed the instruction alone moving 4 misreads to
    # 3 (noise) and E showed instruction-plus-references going to 6, so C's win
    # is unattributed between its two levers until this arm runs.
    "F": {"instruction": False, "ultra_high": True, "references": False},
}


# Reported separately because a null does not cost the same in each. A hole null
# is absorbed by step C's in-attempt retry and then judged per seat by P4-6 and
# P5-16; a board null exhausts CARD_READ_ATTEMPTS and raises
# CommunityCardUnreadable, failing the hand transient and forcing a whole-hand
# Phase 5 retry on Pro. The acceptable board null rate is near zero.
GROUPS = ("known misreads", "hole controls", "board controls")


@dataclass(frozen=True)
class Frame:
    """One reproduction target.

    `timestamp` is video-absolute seconds and is the primary key together with
    `video_id`. For hole frames it is the FVA time, which is what Phase 4
    extracts at; for board frames it already carries FRAME_SETTLE_OFFSET.
    """

    video_id: str
    timestamp: float
    kind: str  # "hole" | "board"
    known_misread: bool = False
    # hole: (seat_number, seat_position_label, stack_size, (card, card))
    seats: tuple = ()
    # board:
    street: str | None = None
    prior_cards: tuple = ()
    expected_cards: tuple = ()
    note: str = ""

    @property
    def key(self) -> str:
        what = self.street if self.kind == "board" else "hole"
        return f"{self.video_id}@{self.timestamp:g}:{what}"


# --- The manifest -----------------------------------------------------------
#
# Expected values for the four known misreads are the stored reads with the
# disputed card corrected to its spade, verified against the broadcast. Hole
# controls are frames where the rebuilt run and hand_starts_pre_rebuild agree on
# every seat -- two independent reads agreeing. Board expectations come from
# hand_actions_pre_rebuild and are eye-checked against the extracted frame.

KNOWN_MISREADS = [
    Frame(
        video_id="YzKyFMQ1avU", timestamp=1501, kind="hole", known_misread=True,
        note="BB Qs read as Qh (setup t=1496)",
        seats=(
            (1, "BB", 28.7, ("Qs", "8d")),
            (2, "SB", 8.07, ("Kh", "Qd")),
            (3, "BTN", 47.3, ("Ah", "Ts")),
        ),
    ),
    Frame(
        video_id="YzKyFMQ1avU", timestamp=1716, kind="hole", known_misread=True,
        note="BTN Qs read as Qh (setup t=1712)",
        seats=(
            (1, "BB", 22.3, ("3h", "Qc")),
            (2, "SB", 2.74, ("Td", "Ah")),
            (3, "BTN", 16, ("Qs", "4s")),
            (4, "CO", 18.6, ("Th", "5c")),
            (5, "HJ", 9.14, ("Qd", "3c")),
            (6, "LJ", 44.2, ("9d", "8d")),
            (7, "UTG+2", 38.3, ("Kc", "Jd")),
        ),
    ),
    Frame(
        video_id="YzKyFMQ1avU", timestamp=2780, kind="hole", known_misread=True,
        note="SB Qs read as Qh by the pre-rebuild run (setup t=2763)",
        seats=(
            (1, "BB", 21.3, ("Kc", "7d")),
            (2, "SB", 6.41, ("Qs", "9s")),
            (3, "BTN", 38.2, ("Kd", "7h")),
        ),
    ),
    Frame(
        video_id="YzKyFMQ1avU", timestamp=4512, kind="hole", known_misread=True,
        note="BB Ks read as Kh (setup t=4501)",
        seats=(
            (1, "BB", 29.3, ("8s", "Ks")),
            (2, "SB", 54.9, ("Qs", "4d")),
            (3, "BTN", 20.9, ("9c", "Kc")),
        ),
    ),
]

HOLE_CONTROLS = [
    Frame(
        video_id="MPBLfM4mwfE", timestamp=186, kind="hole",
        note="9 seats, all four suits, faces As Jc Jd Kd Kh Qd",
        seats=(
            (1, "BB", 9.38, ("8c", "Kd")),
            (2, "SB", 4.31, ("4c", "7c")),
            (3, "BTN", 17.2, ("Ts", "Th")),
            (4, "CO", 2.63, ("8d", "Jc")),
            (5, "HJ", 20, ("2c", "Qd")),
            (6, "LJ", 46.4, ("3h", "9c")),
            (7, "UTG+2", 14.2, ("Kh", "6s")),
            (8, "UTG+1", 9.86, ("7s", "4d")),
            (9, "UTG", 17.2, ("As", "Jd")),
        ),
    ),
    Frame(
        video_id="MPBLfM4mwfE", timestamp=509, kind="hole",
        note="two spade faces beside two heart faces",
        seats=(
            (1, "BB", 26.1, ("2s", "5c")),
            (2, "SB", 43.2, ("2h", "7h")),
            (3, "BTN", 12.1, ("7d", "3d")),
            (4, "CO", 10.2, ("Jh", "Qs")),
            (5, "HJ", 20.2, ("Ks", "7s")),
            (6, "LJ", 10.8, ("Qh", "Kc")),
        ),
    ),
    Frame(
        video_id="YzKyFMQ1avU", timestamp=405, kind="hole",
        note="8 seats, all four suits, eight face cards",
        seats=(
            (1, "BB", 26.8, ("5d", "8c")),
            (2, "SB", 1.58, ("Jh", "Th")),
            (3, "BTN", 23.1, ("6s", "9c")),
            (4, "CO", 10.1, ("4d", "Jd")),
            (5, "HJ", 4.59, ("Jc", "Ac")),
            (6, "LJ", 38.2, ("Ad", "3h")),
            (7, "UTG+2", 19.1, ("Qc", "Js")),
            (8, "UTG+1", 47.7, ("9s", "Ks")),
        ),
    ),
    Frame(
        video_id="YzKyFMQ1avU", timestamp=1636, kind="hole",
        note="three spade faces",
        seats=(
            (1, "BB", 4.36, ("Tc", "Jh")),
            (2, "SB", 16.1, ("8c", "Ad")),
            (3, "BTN", 18.7, ("3h", "Kc")),
            (4, "CO", 9.26, ("6c", "9d")),
            (5, "HJ", 44.3, ("4d", "Ks")),
            (6, "LJ", 38.4, ("3c", "Qs")),
            (7, "UTG+2", 26.6, ("Td", "Th")),
            (8, "UTG+1", 17.9, ("As", "8s")),
        ),
    ),
]

BOARD_CONTROLS = [
    Frame(
        video_id="MPBLfM4mwfE", timestamp=2559 + FRAME_SETTLE_OFFSET, kind="board",
        street="flop", prior_cards=(), expected_cards=("Jc", "As", "7h"),
    ),
    Frame(
        video_id="MPBLfM4mwfE", timestamp=2377 + FRAME_SETTLE_OFFSET, kind="board",
        street="turn", prior_cards=("8c", "4s", "9s"), expected_cards=("Qs",),
        note="exercises the 3 -> 1 count rule",
    ),
    Frame(
        video_id="YzKyFMQ1avU", timestamp=1605 + FRAME_SETTLE_OFFSET, kind="board",
        street="flop", prior_cards=(), expected_cards=("Qh", "Js", "Kc"),
    ),
    Frame(
        video_id="YzKyFMQ1avU", timestamp=1320 + FRAME_SETTLE_OFFSET, kind="board",
        street="flop", prior_cards=(), expected_cards=("8d", "Qs", "2h"),
    ),
]

MANIFEST = KNOWN_MISREADS + HOLE_CONTROLS + BOARD_CONTROLS


# --- Prompt assembly --------------------------------------------------------


def _with_instruction(prompt_text: str) -> str:
    """Idempotent: once the instruction ships in the file, this is a no-op."""
    if SUIT_ANCHOR_INSTRUCTION.strip() in prompt_text:
        return prompt_text
    return prompt_text.replace(
        _CARD_READING_HEADER, _CARD_READING_HEADER + SUIT_ANCHOR_INSTRUCTION, 1
    )


def _without_instruction(prompt_text: str) -> str:
    """Version A must be a true baseline even after the instruction has shipped."""
    return prompt_text.replace(SUIT_ANCHOR_INSTRUCTION, "")


def _with_reference_section(prompt_text: str) -> str:
    if SUIT_REFERENCE_SECTION.strip() in prompt_text:
        return prompt_text
    return prompt_text.replace(
        _CARD_READING_HEADER, SUIT_REFERENCE_SECTION + "\n" + _CARD_READING_HEADER, 1
    )


def build_prompt(kind: str, version: dict) -> str:
    name = "extract_hole_cards" if kind == "hole" else "extract_community_cards"
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    text = _with_instruction(text) if version["instruction"] else _without_instruction(text)
    if version["references"]:
        text = _with_reference_section(text)
    return text


def load_suit_references() -> list[tuple[bytes, str, str]]:
    images = []
    for label, filename in SUIT_REFERENCES:
        path = REFERENCES_DIR / filename
        if not path.is_file():
            raise FileNotFoundError(f"reference image not found: {path}")
        images.append((path.read_bytes(), "image/png", label))
    return images


def render(frame: Frame, prompt_text: str) -> str:
    if frame.kind == "hole":
        # The same structure build_hole_card_context reads in production. Seats
        # in the manifest are already the eligible set, so the FVA seat is the
        # highest of them.
        state = {
            "hand_setup": {
                "players": [
                    {"seat_number": n, "seat_position_label": lab, "stack_size": stack}
                    for n, lab, stack, _ in frame.seats
                ]
            },
            "fva": {"seat_number": max(n for n, _, _, _ in frame.seats)},
        }
        return prompt_text.replace("{hole_card_context}", build_hole_card_context(state))
    return prompt_text.replace(
        "{prior_cards}", build_prior_cards_context(list(frame.prior_cards))
    )


USER_TEXT = {
    "hole": "Extract hole cards for all eligible players from this frame.",
    "board": "Identify the new community cards visible in this frame.",
}


# --- Frames -----------------------------------------------------------------


def ensure_frames(frames: list[Frame], cache_dir: Path) -> dict[str, Path]:
    """Download each source video once, then extract every manifest frame."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    videos = {}
    for video_id in sorted({f.video_id for f in frames}):
        local = cache_dir / f"{video_id}.mp4"
        if not local.exists():
            print(f"downloading {video_id}.mp4 ...", file=sys.stderr)
            download_video(f"gs://{VIDEOS_BUCKET}/{video_id}.mp4", str(local), PROJECT)
        videos[video_id] = local

    paths = {}
    for frame in frames:
        out = cache_dir / f"{frame.video_id}_{frame.timestamp:g}.jpg"
        if not out.exists():
            extract_frame(str(videos[frame.video_id]), frame.timestamp, str(out))
        paths[frame.key] = out
    return paths


# --- Scoring ----------------------------------------------------------------


@dataclass
class Tally:
    calls: int = 0
    cards: int = 0
    correct: int = 0
    misread: int = 0
    null: int = 0
    malformed_calls: int = 0
    api_errors: int = 0
    api_retries: int = 0
    prompt_tokens: list = field(default_factory=list)
    misreads_seen: Counter = field(default_factory=Counter)


def _score_pair(expected: str, got, tally: Tally) -> None:
    tally.cards += 1
    if got is None:
        tally.null += 1
    elif got.casefold() == expected.casefold():
        tally.correct += 1
    else:
        tally.misread += 1
        tally.misreads_seen[f"{expected}->{got}"] += 1


def score(frame: Frame, result: dict, tally: Tally) -> dict:
    """Score one response against the frame's expectation. Returns a detail row."""
    detail = {"key": frame.key, "cards": {}}
    if frame.kind == "hole":
        by_label = {
            p.get("seat_position_label"): p for p in (result.get("players") or [])
        }
        for _, label, _, expected in frame.seats:
            matched = by_label.get(label)
            got = normalize_cards(matched["hole_cards"]) if matched and matched.get(
                "hole_cards"
            ) else [None, None]
            if len(got) != 2:
                tally.malformed_calls += 1
                got = (list(got) + [None, None])[:2]
            detail["cards"][label] = got
            for exp, actual in zip(expected, got):
                _score_pair(exp, actual, tally)
        return detail

    got = normalize_cards(result.get("new_cards") or [])
    detail["cards"][frame.street] = got
    if len(got) != len(frame.expected_cards):
        tally.malformed_calls += 1
        got = (list(got) + [None] * len(frame.expected_cards))[: len(frame.expected_cards)]
    for exp, actual in zip(frame.expected_cards, got):
        _score_pair(exp, actual, tally)
    return detail


_PROMPT_TOKENS = re.compile(r"prompt_tokens=(\d+)")

# gemini_caller already backs off on 429 inside the call. This is the outer
# guard for everything it classifies transient and re-raises -- 500s, connection
# resets, an exhausted 429 backoff. An infrastructure failure must never be
# scored: it is neither a misread nor a null, and counting it as either would
# move a version's rate for a reason that has nothing to do with the prompt.
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY_SECONDS = 20.0


def call_once(frame: Frame, prompt_text: str, frame_bytes: bytes, version: dict, tally):
    """One production call, returning (result, prompt_tokens).

    Usage is read off gemini_caller's stderr line rather than re-derived, so what
    is reported is what the pipeline's own cost instrumentation reports.

    GeminiPermanentError is deliberately not retried: malformed JSON, a safety
    block or MAX_TOKENS are properties of the response, and a retry would hide a
    version that produces them. They surface as api_errors so they are visible
    without being scored.
    """
    last_exc = None
    for attempt in range(API_RETRY_ATTEMPTS):
        captured = io.StringIO()
        try:
            with contextlib.redirect_stderr(captured):
                result = call_gemini_for_frame(
                    prompt_text,
                    frame_bytes,
                    PROJECT,
                    user_text=USER_TEXT[frame.kind],
                    reference_images=(
                        load_suit_references() if version["references"] else None
                    ),
                    frame_media_resolution=(
                        FRAME_RESOLUTION_ULTRA_HIGH if version["ultra_high"] else None
                    ),
                    label=f"repro_{frame.kind}",
                )
        except GeminiPermanentError:
            raise
        except Exception as exc:  # transient: 5xx, reset, exhausted 429 backoff
            last_exc = exc
            if attempt + 1 < API_RETRY_ATTEMPTS:
                tally.api_retries += 1
                print(
                    f"  ~ retrying after {type(exc).__name__}: {str(exc)[:120]}",
                    file=sys.stderr,
                )
                time.sleep(API_RETRY_DELAY_SECONDS * (attempt + 1))
            continue
        match = _PROMPT_TOKENS.search(captured.getvalue())
        return result, int(match.group(1)) if match else None
    raise last_exc


# --- Reporting --------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> str:
    if not denominator:
        return "-"
    return f"{numerator}/{denominator} ({100 * numerator / denominator:.1f}%)"


def report(version_name: str, tallies: dict[str, Tally]) -> Tally:
    overall = Tally()
    for tally in tallies.values():
        overall.calls += tally.calls
        overall.cards += tally.cards
        overall.correct += tally.correct
        overall.misread += tally.misread
        overall.null += tally.null
        overall.malformed_calls += tally.malformed_calls
        overall.api_errors += tally.api_errors
        overall.api_retries += tally.api_retries
        overall.prompt_tokens += tally.prompt_tokens
        overall.misreads_seen += tally.misreads_seen

    print(f"\n=== version {version_name} | model {FRAME_MODEL} ===")
    for name in GROUPS:
        if name in tallies:
            print(_line(name, tallies[name]))
    print(_line("ALL", overall))
    if overall.misreads_seen:
        top = ", ".join(f"{k}x{v}" for k, v in overall.misreads_seen.most_common(12))
        print(f"  misreads: {top}")
    return overall


def _line(name: str, tally: Tally) -> str:
    avg = (
        f"{sum(tally.prompt_tokens) / len(tally.prompt_tokens):.0f}"
        if tally.prompt_tokens
        else "-"
    )
    return (
        f"  {name:<16} scored={tally.calls - tally.api_errors:<4} "
        f"cards={tally.cards:<5} "
        f"misread={_rate(tally.misread, tally.cards):<16} "
        f"null={_rate(tally.null, tally.cards):<16} "
        f"avg_input_tokens={avg:<7} malformed={tally.malformed_calls:<3} "
        f"api_errors={tally.api_errors:<3} api_retries={tally.api_retries}"
    )


def summary(by_version: dict[str, Tally], groups: dict[str, dict[str, Tally]]) -> None:
    """The table the choice is made from: one row per version.

    Hole and board nulls are separate columns, never pooled -- see GROUPS.
    """
    print("\n=== summary ===")
    print(
        f"  {'ver':<4} {'known-misread frames':<22} {'hole null':<15} "
        f"{'board null':<15} {'all misread':<15} {'avg in':<8} api_err"
    )
    for name in sorted(by_version):
        overall = by_version[name]
        per_group = groups[name]
        known = per_group.get(GROUPS[0], Tally())
        hole = [per_group.get(g, Tally()) for g in (GROUPS[0], GROUPS[1])]
        board = per_group.get(GROUPS[2], Tally())
        avg = (
            f"{sum(overall.prompt_tokens) / len(overall.prompt_tokens):.0f}"
            if overall.prompt_tokens
            else "-"
        )
        print(
            f"  {name:<4} "
            f"{f'{known.misread} misread, {known.null} null':<22} "
            f"{_rate(sum(t.null for t in hole), sum(t.cards for t in hole)):<15} "
            f"{_rate(board.null, board.cards):<15} "
            f"{_rate(overall.misread, overall.cards):<15} "
            f"{avg:<8} {overall.api_errors}"
        )


# --- Entry point ------------------------------------------------------------


def group_of(frame: Frame) -> str:
    if frame.known_misread:
        return GROUPS[0]
    return GROUPS[2] if frame.kind == "board" else GROUPS[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--versions",
        default=",".join(sorted(VERSIONS)),
        help="comma-separated, e.g. C,E. Run together so they interleave.",
    )
    parser.add_argument("--reps", type=int, default=5, help="0 extracts frames only")
    parser.add_argument("--only-known-misreads", action="store_true")
    parser.add_argument(
        "--cache-dir", default=os.environ.get("TT_REPRO_CACHE", "scratch/repro-cache")
    )
    parser.add_argument("--out", help="write per-call results as JSON")
    args = parser.parse_args()

    names = [v.strip() for v in args.versions.split(",") if v.strip()]
    unknown = [v for v in names if v not in VERSIONS]
    if unknown:
        parser.error(f"unknown version(s): {', '.join(unknown)}")

    frames = KNOWN_MISREADS if args.only_known_misreads else MANIFEST
    paths = ensure_frames(frames, Path(args.cache_dir))

    if args.reps == 0:
        print(f"{len(frames)} frames extracted under {args.cache_dir}:")
        for frame in frames:
            print(f"  {frame.key:<34} {paths[frame.key]}  {frame.note}")
        return 0

    # Prompts and frame bytes are built once: identical across reps by
    # construction, which is the property the whole comparison rests on.
    prompts = {
        (name, frame.key): render(frame, build_prompt(frame.kind, VERSIONS[name]))
        for name in names
        for frame in frames
    }
    frame_bytes = {frame.key: paths[frame.key].read_bytes() for frame in frames}

    tallies = {name: {} for name in names}
    rows = []

    # Interleaved: every version sees each frame within the same repetition,
    # back to back. Running a version to completion before starting the next
    # would confound any drift in the service with the difference between arms.
    for rep in range(args.reps):
        for frame in frames:
            for name in names:
                tally = tallies[name].setdefault(group_of(frame), Tally())
                tally.calls += 1
                try:
                    result, tokens = call_once(
                        frame, prompts[(name, frame.key)], frame_bytes[frame.key],
                        VERSIONS[name], tally,
                    )
                except Exception as exc:
                    # Infrastructure, not extraction. Counted apart and never
                    # scored -- see call_once.
                    tally.api_errors += 1
                    rows.append({
                        "version": name, "key": frame.key, "rep": rep,
                        "api_error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    })
                    print(
                        f"  ! {name} {frame.key} rep{rep}: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    continue
                if tokens is not None:
                    tally.prompt_tokens.append(tokens)
                detail = score(frame, result, tally)
                detail.update({"version": name, "rep": rep, "prompt_tokens": tokens,
                               "raw": result})
                rows.append(detail)
        print(f"  rep {rep + 1}/{args.reps} done", file=sys.stderr)

    by_version = {name: report(name, tallies[name]) for name in names}
    summary(by_version, tallies)

    if args.out:
        Path(args.out).write_text(
            json.dumps({"versions": names, "model": FRAME_MODEL, "rows": rows}, indent=1)
        )
        print(f"\n  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
