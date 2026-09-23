def parse_timestamp(s: str) -> int:
    """Convert 'MM:SS' or 'HH:MM:SS' to integer seconds. Raise ValueError on bad input."""
    parts = s.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"Invalid timestamp format: {s!r}")
    if any("." in p for p in parts):
        raise ValueError(f"Sub-second precision not supported: {s!r}")
    try:
        values = [int(p) for p in parts]
    except ValueError:
        raise ValueError(f"Non-numeric components in timestamp: {s!r}")
    if any(v < 0 for v in values):
        raise ValueError(f"Negative values in timestamp: {s!r}")
    if len(values) == 2:
        return values[0] * 60 + values[1]
    return values[0] * 3600 + values[1] * 60 + values[2]


def format_timestamp(seconds: int) -> str:
    """Convert integer seconds to 'MM:SS', or 'HH:MM:SS' at or beyond an hour.

    The inverse of parse_timestamp over the range it produces, and the format
    the clip prompts already specify for the timestamps they return.
    """
    if seconds < 0:
        raise ValueError(f"Negative seconds: {seconds!r}")
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
