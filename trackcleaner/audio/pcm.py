"""Full-quality PCM decode for buffered playback.

Separate from :mod:`peaks`, which decodes a low-rate mono envelope just for the
waveform picture. Here we decode the whole track to stereo int16 at 48 kHz so
the player can hold it in memory and play it back itself -- which is what lets a
tempo change resample live (no pipeline restart, no cutout) and a cue jump move
a sample cursor (no re-decode, so no latency).

A 2.5-minute track decodes in well under a second to roughly 28 MB, and only one
track is ever held at once.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 48000        # matches the sink; Traktor runs at this rate too
CHANNELS = 2

# Windows: keep a console window from flashing up on every decode.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


@dataclass(frozen=True)
class Pcm:
    """Decoded audio, ready to play.

    ``left`` and ``right`` are int16 arrays of length ``frames``; ``rate`` is the
    sample rate in Hz. ``duration`` is in seconds, from the decoded frame count.
    """

    left: np.ndarray
    right: np.ndarray
    rate: int

    @property
    def frames(self) -> int:
        return len(self.left)

    @property
    def duration(self) -> float:
        return self.frames / float(self.rate)


def load_pcm(path: str) -> Pcm | None:
    """Decode ``path`` to stereo int16 @ 44.1 kHz, or ``None`` if it will not
    decode (missing file, unsupported codec)."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", path,
             "-f", "s16le", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "-"],
            capture_output=True, timeout=120, creationflags=_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    inter = np.frombuffer(proc.stdout, dtype="<i2")
    if inter.size < CHANNELS:
        return None
    # Drop a dangling odd sample so the interleaved view is exactly (frames, 2).
    inter = inter[: (inter.size // CHANNELS) * CHANNELS]
    # Contiguous per-channel copies -- fancy indexing in the pull callback wants
    # plain 1-D arrays, and a copy detaches from the transient ffmpeg buffer.
    left = np.ascontiguousarray(inter[0::2])
    right = np.ascontiguousarray(inter[1::2])
    return Pcm(left=left, right=right, rate=SAMPLE_RATE)
