# ffmpeg wrapper for extracting a single high-resolution JPEG frame.
# Requires ffmpeg on the system PATH.
# The -ss flag comes before -i for keyframe seek (much faster than post-input seek).
# Filter values are tuned for card/suit visibility downstream — do not change them.

import os
import subprocess


class FrameExtractionError(Exception):
    pass


def extract_frame(
    video_uri: str,
    timestamp_seconds: float | int,
    output_path: str,
) -> None:
    hours = int(timestamp_seconds // 3600)
    minutes = int((timestamp_seconds % 3600) // 60)
    secs = timestamp_seconds % 60
    timestamp_str = f"{hours:02d}:{minutes:02d}:{secs:06.3f}"

    cmd = [
        "ffmpeg", "-y",
        "-ss", timestamp_str,
        "-i", video_uri,
        "-frames:v", "1",
        "-vf", "unsharp=lx=5:ly=5:la=1.0:cx=5:cy=5:ca=0.0,eq=saturation=2.0",
        "-q:v", "2",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise FrameExtractionError(
            f"ffmpeg exited with code {result.returncode}: {result.stderr}"
        )
    if not os.path.exists(output_path):
        raise FrameExtractionError(
            f"ffmpeg returned successfully but output file not found: {output_path}"
        )