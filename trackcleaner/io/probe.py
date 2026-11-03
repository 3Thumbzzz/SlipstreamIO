"""Read tags/duration from an audio file for import, via ffprobe.

Reuses the ffprobe dependency the app already relies on (see
``model/cues.py``). Returns a :class:`Track` with whatever metadata is
available; a file ffprobe can't read yields ``None`` so the caller can skip it.
"""

from __future__ import annotations

import json
import subprocess
from typing import Optional

from ..model import matching
from ..model.library import Track

# Audio extensions we accept for import (drop filtering + a sanity gate here).
AUDIO_EXTS = (".mp3", ".wav", ".flac", ".aiff", ".aif", ".m4a", ".aac",
              ".ogg", ".wma", ".mp4")


def _to_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def probe_track(path: str, source: str) -> Optional[Track]:
    """Build a :class:`Track` for ``path`` in ``source``'s library, reading
    title/artist/album/genre/bpm/length via ffprobe. ``None`` if ffprobe is
    missing or the file isn't decodable."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", "-select_streams", "a:0", path],
            capture_output=True, timeout=30,
        ).stdout
        data = json.loads(out)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None

    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    if not streams and not fmt:
        return None

    # Tags live under format (and sometimes the stream); merge, lower-cased keys.
    tags = {}
    for src in (fmt.get("tags", {}), (streams[0].get("tags", {}) if streams else {})):
        for k, v in (src or {}).items():
            tags.setdefault(k.lower(), v)

    length = _to_float(fmt.get("duration"))
    bitrate = None
    br = _to_float(fmt.get("bit_rate") or (streams[0].get("bit_rate") if streams else None))
    if br:
        bitrate = int(br // 1000)

    return Track(
        key=matching.path_key(path),
        path=path,
        source=source,
        title=tags.get("title", "") or "",
        artist=tags.get("artist", "") or "",
        album=tags.get("album", "") or "",
        genre=tags.get("genre", "") or "",
        bpm=_to_float(tags.get("bpm") or tags.get("tbpm")),
        length=length,
        key_musical=tags.get("key", "") or tags.get("initialkey", "") or "",
        bitrate=bitrate,
        pois=[],
    )
