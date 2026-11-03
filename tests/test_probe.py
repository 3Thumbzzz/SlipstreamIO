"""ffprobe-backed import metadata reader (ffprobe subprocess mocked)."""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.io import probe                               # noqa: E402
from trackcleaner.model.library import VDJ                      # noqa: E402


class _FakeRun:
    def __init__(self, payload):
        self.stdout = json.dumps(payload).encode("utf-8")


def _patch_ffprobe(monkeypatch, payload):
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: _FakeRun(payload))


def test_probe_maps_tags_and_duration(monkeypatch):
    _patch_ffprobe(monkeypatch, {
        "format": {"duration": "212.5", "bit_rate": "320000",
                   "tags": {"TITLE": "Doo Rag", "ARTIST": "Galactic",
                            "ALBUM": "Coolin' Off", "GENRE": "Funk", "BPM": "95"}},
        "streams": [{"codec_type": "audio"}],
    })
    t = probe.probe_track(r"D:\Music\x.mp3", VDJ)
    assert t is not None
    assert (t.title, t.artist, t.album, t.genre) == ("Doo Rag", "Galactic", "Coolin' Off", "Funk")
    assert t.bpm == 95.0 and t.length == 212.5 and t.bitrate == 320
    assert t.source == VDJ and t.key == probe.matching.path_key(r"D:\Music\x.mp3")


def test_probe_handles_missing_tags(monkeypatch):
    _patch_ffprobe(monkeypatch, {"format": {"duration": "100"}, "streams": [{}]})
    t = probe.probe_track(r"D:\Music\y.mp3", VDJ)
    assert t is not None and t.title == "" and t.bpm is None and t.length == 100.0


def test_probe_returns_none_when_ffprobe_missing(monkeypatch):
    def boom(*a, **k):
        raise OSError("ffprobe not found")
    monkeypatch.setattr(probe.subprocess, "run", boom)
    assert probe.probe_track(r"D:\Music\z.mp3", VDJ) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
