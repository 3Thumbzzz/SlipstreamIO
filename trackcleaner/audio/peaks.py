"""Waveform peaks: decode audio to a compact min/max envelope via ffmpeg.

ffmpeg is already a dependency of this app (``model/cues.py`` shells out to
ffprobe for start_time), and it decodes these MP3s to raw PCM in ~0.2-0.4 s for
a typical track, so there is no need for an in-process decoder.

The envelope is stored at a fixed, generous column count and re-binned by the
widget to whatever width it happens to be, so resizing never re-decodes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 8000        # plenty for a visual envelope; keeps decoding quick
COLUMNS = 6000            # stored resolution, re-binned to widget width
_CACHE_SIZE = 8

# Windows: stop a console window flashing up in front of the GUI on every load.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


@dataclass(frozen=True)
class Peaks:
    """Min/max envelope of one track.

    ``lo`` and ``hi`` are float arrays in -1.0..1.0, both of length
    :data:`COLUMNS`. ``duration`` is in seconds, measured from the decoded
    sample count (the audio itself, not the container metadata).
    """

    lo: np.ndarray
    hi: np.ndarray
    duration: float

    def binned(self, columns: int) -> tuple[np.ndarray, np.ndarray]:
        """Re-bin the envelope to ``columns``, for painting at a given width."""
        columns = max(1, int(columns))
        if columns >= len(self.lo):
            return self.lo, self.hi
        edges = np.linspace(0, len(self.lo), columns + 1).astype(int)
        lo = np.minimum.reduceat(self.lo, edges[:-1])
        hi = np.maximum.reduceat(self.hi, edges[:-1])
        return lo, hi


_cache: "OrderedDict[tuple, Peaks]" = OrderedDict()


def _cache_key(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (os.path.normcase(path), st.st_mtime_ns, st.st_size)


def _decode(path: str) -> np.ndarray | None:
    """Whole file as mono float32 in -1.0..1.0, or None if it will not decode."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", path,
             "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
            capture_output=True, timeout=120, creationflags=_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    raw = np.frombuffer(proc.stdout, dtype="<i2")
    if raw.size == 0:
        return None
    return raw.astype(np.float32) / 32768.0


def _envelope(samples: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Min/max per column over exactly :data:`COLUMNS` bins."""
    if samples.size < COLUMNS:
        # Very short file: pad so the envelope always has a fixed length.
        samples = np.pad(samples, (0, COLUMNS - samples.size))
    edges = np.linspace(0, samples.size, COLUMNS + 1).astype(int)
    edges[-1] = samples.size
    # reduceat needs strictly increasing starts; equal edges would silently
    # reverse the slice and produce garbage columns.
    starts = np.maximum.accumulate(edges[:-1])
    starts = np.minimum(starts, samples.size - 1)
    return np.minimum.reduceat(samples, starts), np.maximum.reduceat(samples, starts)


def load_peaks(path: str) -> Peaks | None:
    """Envelope for ``path``, or ``None`` if it is missing or undecodable.

    Cached by (path, mtime, size), so re-clicking a track is instant and an
    edited file is re-read.
    """
    key = _cache_key(path)
    if key is None:
        return None
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]

    samples = _decode(path)
    if samples is None:
        return None
    lo, hi = _envelope(samples)
    peaks = Peaks(lo=lo, hi=hi, duration=samples.size / float(SAMPLE_RATE))

    _cache[key] = peaks
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return peaks


def cache_clear() -> None:
    _cache.clear()
