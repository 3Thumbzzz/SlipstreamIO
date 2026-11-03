"""Serato's ``database V2`` and ``.crate`` files.

Both are the same binary shape: a flat run of records, each a 4-byte ASCII tag,
a 4-byte big-endian length and that many payload bytes. A tag's first letter
says what the payload is -- ``t``/``p`` UTF-16BE text or path, ``u`` a u32,
``s`` a u16, ``b`` one byte, ``o`` a nested run of records.

* ``database V2``: ``vrsn``, then one ``otrk`` per track holding ``pfil`` (path),
  ``tsng`` (title), ``tart``, ``talb``, ``tgen``, ``tcom``, ``tbpm``, ``tkey`` and
  a few dozen fields this app does not use.
* ``Subcrates/A%%B%%C.crate``: ``vrsn``, column settings, then one ``otrk`` per
  member holding just ``ptrk`` (path). The crate's place in the tree is its
  file name, ``%%`` separating the levels; a parent with no file of its own is
  still shown by Serato as a folder.
* ``neworder.pref``: UTF-16BE lines, ``[crate]A%%B`` per crate in sidebar order.

**Paths are relative to the root of the drive the database is on** --
``Music/Funk/x.mp3`` in ``D:\\Music\\_Serato_`` is ``D:\\Music\\Funk\\x.mp3``.
A database cannot name a file on another drive (Serato keeps one ``_Serato_``
per drive), so such a path is refused here rather than written wrong.

**Records this app does not change are re-emitted byte-for-byte**: a track is
kept as its original payload until a field on it is edited, so every field
Serato stores and this app ignores survives every write.
"""

from __future__ import annotations

import os
import struct
from typing import Optional

from ..model import matching

DB_VERSION = "2.0/Serato Scratch LIVE Database"
CRATE_VERSION = "1.0/Serato ScratchLive Crate"
SEPARATOR = "%%"


# =============================== the record format ==========================
def parse(data: bytes) -> list:
    """``[(tag, payload)]``. Raises ``ValueError`` on a truncated record, which
    is what the write guard relies on to refuse a malformed file."""
    out, i = [], 0
    while i < len(data):
        if i + 8 > len(data):
            raise ValueError(f"truncated record header at byte {i}")
        tag = data[i:i + 4].decode("latin-1")
        (length,) = struct.unpack(">I", data[i + 4:i + 8])
        if i + 8 + length > len(data):
            raise ValueError(f"record {tag!r} at byte {i} runs past the end")
        out.append((tag, data[i + 8:i + 8 + length]))
        i += 8 + length
    return out


def encode(records) -> bytes:
    return b"".join(tag.encode("latin-1") + struct.pack(">I", len(payload)) + payload
                    for tag, payload in records)


def validate(data: bytes) -> None:
    """Every record and every track's fields parse. What ``atomic_write_bytes``
    runs before replacing a database or a crate."""
    for tag, payload in parse(data):
        if tag == "otrk":
            parse(payload)


def text(payload: bytes) -> str:
    return payload.decode("utf-16-be", errors="replace")


def to_text(value: str) -> bytes:
    return value.encode("utf-16-be")


def fields(otrk_payload: bytes) -> dict:
    """A track record's fields, first occurrence of each tag."""
    out = {}
    for tag, payload in parse(otrk_payload):
        out.setdefault(tag, payload)
    return out


# =================================== paths ==================================
def volume_root(db_path: str) -> str:
    """The drive root a database's paths are relative to (``D:\\``)."""
    drive = os.path.splitdrive(os.path.abspath(db_path))[0]
    return drive + "\\"


def to_path(stored: str, root: str) -> str:
    return root + stored.replace("/", "\\")


def to_stored(path: str, root: str) -> Optional[str]:
    """``D:\\Music\\x.mp3`` -> ``Music/x.mp3``, or None for a file on another
    drive, which this database has no way to name."""
    drive, rest = os.path.splitdrive(path)
    if drive.lower() != root[:2].lower():
        return None
    return rest.lstrip("\\/").replace("\\", "/")


def stored_key(stored: str, root: str) -> str:
    return matching.path_key(to_path(stored, root))


# ================================ the database ==============================
#: Shared field name -> database tag.
FIELD_TAGS = {"title": "tsng", "artist": "tart", "album": "talb",
              "genre": "tgen", "comment": "tcom", "key": "tkey"}


def _track_keys(records, root):
    """``{index of otrk record: track key}``."""
    out = {}
    for i, (tag, payload) in enumerate(records):
        if tag == "otrk":
            pfil = fields(payload).get("pfil")
            if pfil is not None:
                out[i] = stored_key(text(pfil), root)
    return out


def _set_fields(otrk_payload: bytes, values: dict) -> bytes:
    """A track record with ``{tag: text}`` set, each replaced where it already
    is or appended at the end. Every other field keeps its bytes."""
    records = parse(otrk_payload)
    pending = dict(values)
    out = []
    for tag, payload in records:
        if tag in pending:
            out.append((tag, to_text(pending.pop(tag))))
        else:
            out.append((tag, payload))
    out += [(tag, to_text(v)) for tag, v in pending.items()]
    return encode(out)


def new_track_record(track, stored: str) -> bytes:
    """A minimal track record. Serato fills in the analysis fields (length,
    size, bitrate, the flags) itself when it next scans the file."""
    from time import time
    ext = os.path.splitext(track.path)[1].lstrip(".").lower() or "mp3"
    now = int(time())
    values = [("ttyp", ext), ("pfil", stored), ("tsng", track.title),
              ("tart", track.artist), ("talb", track.album),
              ("tgen", track.genre), ("tcom", track.comment)]
    if track.bpm:
        values.append(("tbpm", f"{track.bpm:.2f}"))
    from ..model import keys
    key = keys.flat_key(track.key_value if track.key_value is not None
                        else keys.parse_vdj_key(track.key_musical))
    if key:
        values.append(("tkey", key))
    values.append(("tadd", str(now)))
    records = [(tag, to_text(v)) for tag, v in values if v or tag in ("ttyp", "pfil")]
    records.append(("uadd", struct.pack(">I", now)))
    return encode(records)


def ensure_tracks(data: bytes, root: str, tracks) -> tuple[bytes, dict]:
    """Append a record for every track the database lacks. ``{key: created}``;
    a track on another drive is neither present nor creatable and answers
    False."""
    records = parse(data) if data else [("vrsn", to_text(DB_VERSION))]
    have = set(_track_keys(records, root).values())
    created = {}
    for track in tracks:
        if track.key in have or track.key in created:
            created.setdefault(track.key, False)
            continue
        stored = to_stored(track.path, root)
        if stored is None:
            created[track.key] = False
            continue
        records.append(("otrk", new_track_record(track, stored)))
        created[track.key] = True
    return encode(records), created


def set_tracks_metadata(data: bytes, root: str, by_key: dict) -> tuple[bytes, dict]:
    records = parse(data)
    done = {k: False for k in by_key}
    for i, key in _track_keys(records, root).items():
        wanted = by_key.get(key)
        if wanted is None:
            continue
        values = {FIELD_TAGS[f]: matching.normalize_text(v)
                  for f, v in wanted.items() if f in FIELD_TAGS}
        if values:
            records[i] = ("otrk", _set_fields(records[i][1], values))
            done[key] = True
    return encode(records), done


def remove_track(data: bytes, root: str, key: str) -> tuple[bytes, bool]:
    records = parse(data)
    for i, k in _track_keys(records, root).items():
        if k == key:
            del records[i]
            return encode(records), True
    return data, False


def repath_tracks(data: bytes, root: str, by_key: dict) -> tuple[bytes, dict]:
    records = parse(data)
    done = {k: False for k in by_key}
    for i, key in _track_keys(records, root).items():
        if key in by_key:
            stored = to_stored(by_key[key], root)
            if stored is not None:
                records[i] = ("otrk", _set_fields(records[i][1], {"pfil": stored}))
                done[key] = True
    return encode(records), done


def find_tracks(data: bytes, root: str, keys) -> dict:
    """``{key: fields}`` for these keys."""
    wanted = set(keys)
    records = parse(data)
    return {key: fields(records[i][1])
            for i, key in _track_keys(records, root).items() if key in wanted}


# ================================== crates ==================================
def crate_file(subcrates: str, node_path) -> str:
    return os.path.join(subcrates, SEPARATOR.join(node_path) + ".crate")


def crate_node_path(file_name: str) -> tuple:
    return tuple(file_name[:-len(".crate")].split(SEPARATOR))


def crate_files(subcrates: str) -> dict:
    """``{node_path: file}`` for every crate."""
    try:
        names = os.listdir(subcrates)
    except OSError:
        return {}
    return {crate_node_path(n): os.path.join(subcrates, n)
            for n in names if n.lower().endswith(".crate")
            and os.path.isfile(os.path.join(subcrates, n))}


def crate_members(data: bytes) -> list:
    """The stored paths a crate lists, in order."""
    out = []
    for tag, payload in parse(data):
        if tag == "otrk":
            ptrk = fields(payload).get("ptrk")
            if ptrk is not None:
                out.append(text(ptrk))
    return out


def crate_with_members(data: Optional[bytes], stored_paths) -> bytes:
    """A crate listing exactly ``stored_paths``. Everything else in an existing
    crate -- version, column and sort settings -- is kept as it was."""
    records = parse(data) if data else [("vrsn", to_text(CRATE_VERSION))]
    kept = [(t, p) for t, p in records if t != "otrk"]
    members = [("otrk", encode([("ptrk", to_text(s))])) for s in stored_paths]
    return encode(kept + members)


def repath_crate(data: bytes, root: str, by_key: dict) -> tuple[bytes, set]:
    """Rewrite member paths for moved tracks. Returns the keys it moved."""
    moved = set()
    out = []
    for tag, payload in parse(data):
        if tag == "otrk":
            ptrk = fields(payload).get("ptrk")
            key = stored_key(text(ptrk), root) if ptrk is not None else None
            stored = to_stored(by_key[key], root) if key in by_key else None
            if stored is not None:
                moved.add(key)
                payload = _set_fields(payload, {"ptrk": stored})
        out.append((tag, payload))
    return encode(out), moved


# ============================== sidebar order ===============================
def read_order(pref_path: str) -> list:
    """Crate names (``A%%B``) in Serato's sidebar order."""
    try:
        with open(pref_path, "rb") as f:
            raw = f.read()
    except OSError:
        return []
    return [line[len("[crate]"):] for line in raw.decode("utf-16-be", "replace").splitlines()
            if line.startswith("[crate]")]


def order_with(raw: bytes, renames: dict = None, added=(), removed=()) -> bytes:
    """``neworder.pref`` with crates renamed (``{old name: new name}``, applied
    to every descendant too), added before ``[end record]`` and removed."""
    lines = raw.decode("utf-16-be", "replace").split("\n") if raw else \
        ["[begin record]", "[end record]", ""]
    renames = renames or {}
    gone = set(removed)
    out = []
    for line in lines:
        if line.startswith("[crate]"):
            name = line[len("[crate]"):]
            if any(name == g or name.startswith(g + SEPARATOR) for g in gone):
                continue
            for old, new in renames.items():
                if name == old or name.startswith(old + SEPARATOR):
                    name = new + name[len(old):]
                    break
            line = "[crate]" + name
        if line == "[end record]":
            present = {l[len("[crate]"):] for l in out if l.startswith("[crate]")}
            out += ["[crate]" + a for a in added if a not in present]
        out.append(line)
    return "\n".join(out).encode("utf-16-be")
