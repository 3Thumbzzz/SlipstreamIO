"""Full-quality PCM decode used by the buffered player."""

import os
import subprocess
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.audio import pcm as pcm_mod                     # noqa: E402


def _have_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=15)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_ffmpeg = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg not on PATH")


@needs_ffmpeg
def test_decode_shape_rate_and_duration(tmp_path):
    path = str(tmp_path / "tone.mp3")
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=3", "-af", "volume=8", path],
        capture_output=True, timeout=60,
    )
    p = pcm_mod.load_pcm(path)
    assert p is not None
    assert p.rate == pcm_mod.SAMPLE_RATE
    assert p.left.dtype == np.int16 and p.right.dtype == np.int16
    assert p.left.size == p.right.size == p.frames
    assert p.duration == pytest.approx(3.0, abs=0.15)
    assert np.abs(p.left).max() > 1000, "a loud tone must carry real amplitude"


@needs_ffmpeg
def test_mono_source_is_upmixed_to_stereo(tmp_path):
    path = str(tmp_path / "mono.mp3")
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi",
         "-i", "sine=frequency=330:duration=1", "-ac", "1", path],
        capture_output=True, timeout=60,
    )
    p = pcm_mod.load_pcm(path)
    assert p is not None
    assert p.left.size == p.right.size and p.frames > 0


def test_missing_file_returns_none(tmp_path):
    assert pcm_mod.load_pcm(str(tmp_path / "nope.mp3")) is None


def test_undecodable_file_returns_none(tmp_path):
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"not audio at all" * 100)
    assert pcm_mod.load_pcm(str(junk)) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
