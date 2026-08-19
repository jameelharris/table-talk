# Loads the street reference images that accompany Phase 5's community-card
# scan call.
#
# This is more than file I/O, which is why it is here rather than inlined in
# cli.py (which has no automated tests). It encodes three contracts:
#
#   1. The flop-turn-river ordering the scan prompt's narrative assumes.
#   2. The mime-type derivation.
#   3. That the label is the *bare* street name — gemini_caller supplies the
#      surrounding "Reference image — {label}:" wording, so passing the wrapped
#      form would produce a doubled prefix.

from pathlib import Path

# Never derive this from a directory listing: sorted() yields flop, river, turn,
# which looks plausible and is wrong.
STREET_REFERENCE_ORDER = ("flop", "turn", "river")

_MIME_TYPES = {".png": "image/png"}
_DEFAULT_MIME_TYPE = "image/jpeg"


def reference_image_filename(street: str) -> str:
    return f"{street}_reference.jpeg"


def _mime_type_for(path: Path) -> str:
    # Derived rather than hardcoded: the files are .jpeg today, but a reference
    # swapped for a PNG later would otherwise be sent with a wrong mime type and
    # no error anywhere.
    return _MIME_TYPES.get(path.suffix.lower(), _DEFAULT_MIME_TYPE)


def load_reference_images(references_dir: Path) -> list[tuple[bytes, str, str]]:
    """Load the street reference images as (bytes, mime_type, label) tuples.

    Ordered flop, turn, river — the order extract_community_cards.md describes
    them in. A missing file raises FileNotFoundError naming the path; this never
    returns a short list or a None entry, because a silently absent reference
    would degrade the scan without failing it.
    """
    images: list[tuple[bytes, str, str]] = []
    for street in STREET_REFERENCE_ORDER:
        path = references_dir / reference_image_filename(street)
        if not path.is_file():
            raise FileNotFoundError(f"reference image not found: {path}")
        images.append((path.read_bytes(), _mime_type_for(path), street))
    return images
