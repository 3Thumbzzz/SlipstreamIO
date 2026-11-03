"""Serato's cue points and beatgrid, which live **inside the audio files**.

Serato keeps them in two ID3 ``GEOB`` frames, not in its database:

* ``Serato Markers2`` -- ``01 01`` then base64 text of a payload that is
  ``01 01`` plus entries, each a NUL-terminated name, a u32 length and a body.
  A ``CUE`` body is ``00, index, position (u32 ms), 00, colour (RGB), 00 00,
  name, 00``. ``COLOR``, ``BPMLOCK``, ``LOOP``, ``FLIP`` and anything else are
  Serato's and carried through untouched.
* ``Serato BeatGrid`` -- ``01 00``, a u32 marker count, the markers (a float32
  position each, then a u32 beat count to the next marker, or for the last one a
  float32 BPM), then one footer byte that is preserved.

**This is the one place the app writes into audio files.** The user chose that
with the risk stated (2026-10-31). What makes it acceptable:

* only those two frames change -- every other frame and every byte of audio is
  verified identical before the file is replaced;
* the edit is made on a copy beside the original and swapped in with
  ``os.replace``, so a failure at any point leaves the original untouched;
* the frames' previous contents are written to a backup first, from which
  :func:`restore` puts them back.

The base64 is written exactly the way Serato itself writes it -- ``=`` stripped,
one ``A`` appended unless the payload length is 1 mod 3, a newline after every
full 72-character line, NUL-padded to 470 bytes -- established by re-encoding
every Serato-written frame in a real library byte-for-byte. A second style
(``=`` kept, an extra NUL byte encoded, a trailing newline) written by a
converter is read too.

Only ID3-tagged containers are handled: MP3, AIFF and WAV. FLAC, M4A and Ogg
keep Serato's frames in other places and read as having none.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import struct
from datetime import datetime
from typing import Optional

from ..model.cues import Poi

MARKERS2 = "Serato Markers2"
BEATGRID = "Serato BeatGrid"
_OWNED = (MARKERS2, BEATGRID)
_MIME = "application/octet-stream"

#: Serato's default pad colours, A..H, for a cue it has never coloured.
DEFAULT_COLOURS = [b"\xcc\x00\x00", b"\xcc\x88\x00", b"\x00\x00\xcc", b"\xcc\xcc\x00",
                   b"\x00\xcc\x00", b"\xcc\x00\xcc", b"\x00\xcc\xcc", b"\x88\x00\xcc"]
HOT_CUE_SLOTS = range(0, 8)
ID3_EXTENSIONS = (".mp3", ".aif", ".aiff", ".wav")


# ================================ Markers2 ==================================
def decode_markers2(data: bytes) -> list:
    """``[(name, body)]`` from a ``Serato Markers2`` frame, either style."""
    if len(data) < 2:
        return []
    end = data.find(b"\x00", 2)
    b64 = (data[2:] if end < 0 else data[2:end]).replace(b"\n", b"")
    extra = b"A==" if len(b64) % 4 == 1 else b"=" * (-len(b64) % 4)
    raw = base64.b64decode(b64 + extra)
    entries, i = [], 2
    while i < len(raw) and raw[i] != 0:
        name_end = raw.index(b"\x00", i)
        (length,) = struct.unpack(">I", raw[name_end + 1:name_end + 5])
        body = raw[name_end + 5:name_end + 5 + length]
        entries.append((raw[i:name_end].decode("ascii", "replace"), body))
        i = name_end + 5 + length
    return entries


def encode_markers2(entries) -> bytes:
    """A ``Serato Markers2`` frame in Serato's own style."""
    payload = b"\x01\x01" + b"".join(
        name.encode("ascii") + b"\x00" + struct.pack(">I", len(body)) + body
        for name, body in entries)
    text = base64.b64encode(payload).rstrip(b"=")
    if len(payload) % 3 != 1:
        text += b"A"
    wrapped = b"".join(text[i:i + 72] + (b"\n" if len(text[i:i + 72]) == 72 else b"")
                       for i in range(0, len(text), 72))
    out = b"\x01\x01" + wrapped
    return out + b"\x00" * max(470 - len(out), 1)


def _cue(body: bytes) -> Optional[tuple]:
    """``(index, seconds)`` of a ``CUE`` body, or None if it is not one."""
    if len(body) < 6:
        return None
    return body[1], struct.unpack(">I", body[2:6])[0] / 1000.0


def _cue_body(index: int, seconds: float, old: Optional[bytes]) -> bytes:
    """A cue at ``seconds``, keeping the colour and name a previous cue on the
    same pad had -- they are the user's, and the shared model has no slot for
    either."""
    ms = max(int(round(seconds * 1000)), 0)
    if old is not None and len(old) >= 12:
        return old[:1] + bytes([index]) + struct.pack(">I", ms) + old[6:]
    return (b"\x00" + bytes([index]) + struct.pack(">I", ms) + b"\x00"
            + DEFAULT_COLOURS[index] + b"\x00\x00" + b"\x00")


def entries_with_cues(entries, cues) -> list:
    """``entries`` with every ``CUE`` replaced by ``cues`` (``[(index, s)]``),
    placed where the first old cue was, in pad order. Everything else stays."""
    old = {}
    for name, body in entries:
        parsed = _cue(body) if name == "CUE" else None
        if parsed is not None:
            old.setdefault(parsed[0], body)
    new = [("CUE", _cue_body(i, s, old.get(i))) for i, s in sorted(cues)]
    out, placed = [], False
    for name, body in entries:
        if name == "CUE":
            if not placed:
                out += new
                placed = True
            continue
        out.append((name, body))
    if not placed:
        # No cues before: after COLOR if there is one, so the order is Serato's.
        at = next((i + 1 for i, (n, _b) in enumerate(out) if n == "COLOR"), 0)
        out[at:at] = new
    return out


# ================================ BeatGrid ==================================
def decode_beatgrid(data: bytes) -> tuple[list, bytes]:
    """``([(position, value, terminal)], footer)``: ``value`` is the beat count
    to the next marker, or the BPM on the last one."""
    if len(data) < 6:
        return [], b""
    (count,) = struct.unpack(">I", data[2:6])
    markers, off = [], 6
    for n in range(count):
        if off + 8 > len(data):
            break
        (pos,) = struct.unpack(">f", data[off:off + 4])
        terminal = n == count - 1
        value = struct.unpack(">f" if terminal else ">I", data[off + 4:off + 8])[0]
        markers.append((pos, value, terminal))
        off += 8
    return markers, data[off:]


def encode_beatgrid(anchor: float, bpm: float, footer: bytes = b"\x00") -> bytes:
    """A one-marker grid: Serato's shape for a constant tempo."""
    return (b"\x01\x00" + struct.pack(">I", 1) + struct.pack(">f", anchor)
            + struct.pack(">f", bpm) + (footer or b"\x00"))


# =============================== the files ==================================
def _load(path: str):
    """``(audio file, its ID3 tags or None)`` through mutagen, or ``(None,
    None)`` for a container this module does not handle."""
    if not path.lower().endswith(ID3_EXTENSIONS):
        return None, None
    import mutagen
    if path.lower().endswith(".mp3"):
        from mutagen.id3 import ID3, ID3NoHeaderError
        try:
            return None, ID3(path, translate=False)
        except ID3NoHeaderError:
            from mutagen.id3 import ID3 as _ID3
            return None, _ID3()
    audio = mutagen.File(path)
    if audio is None:
        return None, None
    if audio.tags is None:
        audio.add_tags()
    return audio, audio.tags


def _frames(tags) -> dict:
    """``{description: data}`` for the Serato frames we own."""
    out = {}
    for key, frame in tags.items():
        if key.startswith("GEOB") and frame.desc in _OWNED:
            out[frame.desc] = frame.data
    return out


def points_from_frames(frames: dict) -> list:
    pois = []
    markers, _footer = decode_beatgrid(frames.get(BEATGRID, b""))
    if markers:
        pois.append(Poi(round(float(markers[0][0]), 6), None, "beatgrid"))
    for name, body in decode_markers2(frames.get(MARKERS2, b"")):
        parsed = _cue(body) if name == "CUE" else None
        if parsed is not None and parsed[0] in HOT_CUE_SLOTS:
            pois.append(Poi(parsed[1], parsed[0] + 1, "cue"))
    return pois


def read_points(path: str) -> Optional[list]:
    """The hot cues and grid anchor stored in ``path``, as Pois numbered pad A
    = 1. ``None`` when the file cannot be read or is not a container this
    module handles -- distinct from ``[]``, a file that simply has none."""
    try:
        _audio, tags = _load(path)
    except Exception:                                    # noqa: BLE001
        return None
    if tags is None:
        return None
    return points_from_frames(_frames(tags))


def _owned_points(track) -> tuple[list, Optional[float]]:
    cues = sorted((p.num - 1, p.pos) for p in track.pois
                  if p.kind == "cue" and p.num and p.num - 1 in HOT_CUE_SLOTS)
    grid = sorted(p.pos for p in track.pois if p.kind == "beatgrid")
    return cues, (grid[0] if grid else None)


def frames_for(frames: dict, track) -> dict:
    """The Serato frames ``track``'s points call for, starting from the file's
    current ones. A grid with several markers -- a tempo-changing grid -- is
    never replaced by one anchor."""
    cues, anchor = _owned_points(track)
    out = dict(frames)
    if cues or MARKERS2 in frames:
        out[MARKERS2] = encode_markers2(
            entries_with_cues(decode_markers2(frames.get(MARKERS2, b"")), cues))
    markers, footer = decode_beatgrid(frames.get(BEATGRID, b""))
    if anchor is not None and len(markers) <= 1:
        bpm = track.bpm or (markers[0][1] if markers else None)
        if bpm:
            out[BEATGRID] = encode_beatgrid(anchor, float(bpm), footer)
    return out


def _same_points(a: list, b: list) -> bool:
    key = lambda p: (p.kind, p.num, round(p.pos, 3))         # noqa: E731
    return sorted(map(key, a)) == sorted(map(key, b))


def _apply(tags, frames: dict) -> None:
    from mutagen.id3 import GEOB
    for desc in _OWNED:
        if desc not in frames:
            continue
        tags.delall(f"GEOB:{desc}")
        tags.add(GEOB(encoding=0, mime=_MIME, filename="", desc=desc,
                      data=frames[desc]))


def _save(audio, tags, path: str, version) -> None:
    if audio is not None:
        audio.save(path)
    else:
        tags.save(path, v2_version=version[1] if version[1] in (3, 4) else 4)


def _other_frames(tags) -> list:
    """Every frame but ours, for the before/after check. A text frame holding
    nothing is left out: it carries no information, and mutagen does not write
    one back."""
    return sorted((k, repr(f)) for k, f in tags.items()
                  if not (k.startswith("GEOB") and f.desc in _OWNED)
                  and getattr(f, "text", None) not in ([], [""]))


def _tag_length(data: bytes) -> int:
    """Bytes taken by a leading ID3v2 tag (header, frames, padding, footer)."""
    if data[:3] != b"ID3" or len(data) < 10:
        return 0
    size = 0
    for b in data[6:10]:
        size = (size << 7) | (b & 0x7F)
    return 10 + size + (10 if data[5] & 0x10 else 0)


def _audio_bytes(path: str) -> bytes:
    """The file minus its leading ID3v2 tag -- what must never change."""
    with open(path, "rb") as f:
        data = f.read()
    return data[_tag_length(data):]


def _commit(path: str, change, verify) -> bool:
    """Apply ``change(tags)`` to a copy of ``path`` and swap it in only if
    ``verify(original tags, new tags)`` holds.

    For an MP3 only the leading ID3v2 tag is taken from mutagen's output; every
    byte after it comes from the original file. mutagen also regenerates a
    trailing ID3v1 tag when it saves, which would otherwise change the end of
    the file -- 36 of 60 sampled real files carry one.
    """
    stem, ext = os.path.splitext(path)
    tmp = stem + ".slipstream-tmp" + ext       # the extension picks the container
    try:
        _audio0, before = _load(path)
        version = getattr(before, "version", (2, 4, 0))
        shutil.copy2(path, tmp)
        audio, tags = _load(tmp)
        change(tags)
        _save(audio, tags, tmp, version)
        if audio is None:
            with open(tmp, "rb") as f:
                new = f.read()
            with open(path, "rb") as f:
                old = f.read()
            with open(tmp, "wb") as f:
                f.write(new[:_tag_length(new)] + old[_tag_length(old):])
        _audio1, after = _load(tmp)
        if not verify(before, after):
            return False
        if audio is None and _audio_bytes(tmp) != _audio_bytes(path):
            return False
        os.replace(tmp, path)
        return True
    except Exception:                                    # noqa: BLE001
        return False
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_points(path: str, track, backup_dir: str) -> bool:
    """Store ``track``'s hot cues and grid anchor in ``path``'s Serato frames.

    Returns True when the file now holds them (including when it already did,
    which writes nothing). False -- with the original untouched -- for a
    missing or unsupported file, or when the result does not verify.
    """
    if not os.path.isfile(path):
        return False
    try:
        audio, tags = _load(path)
    except Exception:                                    # noqa: BLE001
        return False
    if tags is None:
        return False
    current = _frames(tags)
    wanted = frames_for(current, track)
    want_points = points_from_frames(wanted)
    if _same_points(points_from_frames(current), want_points):
        return True
    _backup(backup_dir, path, current)
    # Verified before the swap: our points landed, and nothing else moved.
    return _commit(path, lambda t: _apply(t, wanted), lambda before, after: (
        _same_points(points_from_frames(_frames(after)), want_points)
        and _other_frames(after) == _other_frames(before)))


# ================================ backups ===================================
def _backup(backup_dir: str, path: str, frames: dict) -> None:
    """Append the frames ``path`` held before this write to today's manifest.
    One JSON line per file write, so a restore can replay them newest-last."""
    os.makedirs(backup_dir, exist_ok=True)
    manifest = os.path.join(backup_dir,
                            datetime.now().strftime("serato_tags_%Y_%m_%d.jsonl"))
    line = {"path": path, "at": datetime.now().isoformat(timespec="seconds"),
            "frames": {d: base64.b64encode(frames[d]).decode("ascii")
                       if d in frames else None for d in _OWNED}}
    with open(manifest, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def restore(manifest_path: str) -> int:
    """Put back the frames a manifest recorded, oldest entry per file winning
    (the state before the first write that day). Returns files restored."""
    first = {}
    with open(manifest_path, encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            first.setdefault(entry["path"], entry["frames"])
    restored = 0
    for path, saved in first.items():
        frames = {d: base64.b64decode(b) for d, b in saved.items() if b is not None}

        def change(tags, saved=saved, frames=frames):
            for desc in saved:
                tags.delall(f"GEOB:{desc}")
            _apply(tags, frames)

        def verify(before, after, frames=frames):
            return (_frames(after) == frames
                    and _other_frames(after) == _other_frames(before))

        if os.path.isfile(path) and _commit(path, change, verify):
            restored += 1
    return restored


# ================================ the cache =================================
class CueCache:
    """Each file's points, valid while its size and modification time hold.

    Reading 20,000 files' tags costs about 24 s; checking whether they changed
    costs 0.4 s. So a load reads only the files that changed since the last
    one, and the answer is identical either way -- the cache can only be
    stale if a file changed without its size or mtime moving.
    """

    def __init__(self, path: str):
        self.path = path
        self._entries: dict = {}
        self._dirty = False
        try:
            with open(path, encoding="utf-8") as f:
                self._entries = json.load(f)
        except (OSError, ValueError):
            self._entries = {}

    def points(self, file_path: str) -> list:
        """The file's points, read only if it changed. ``[]`` for a file that
        is missing or cannot be read."""
        try:
            st = os.stat(file_path)
        except OSError:
            return []
        key = file_path.lower()
        stamp = [st.st_size, st.st_mtime_ns]
        hit = self._entries.get(key)
        if hit is not None and hit[0] == stamp:
            return [Poi(pos, num, kind) for kind, num, pos in hit[1]]
        pois = read_points(file_path) or []
        self._entries[key] = [stamp, [[p.kind, p.num, p.pos] for p in pois]]
        self._dirty = True
        return pois

    def forget(self, file_path: str) -> None:
        if self._entries.pop(file_path.lower(), None) is not None:
            self._dirty = True

    def save(self) -> None:
        if not self._dirty or not self.path:
            return
        from .write_queue import atomic_write_text
        atomic_write_text(self.path, json.dumps(self._entries))
        self._dirty = False
