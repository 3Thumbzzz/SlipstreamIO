"""Read a VirtualDJ library: ``database.xml`` (tracks) + ``Folders/`` (playlist
tree of ``.vdjfolder`` files, ``.subfolders`` dirs and ``order`` files).

``database.xml`` is sliced into ``<Song>`` blocks by regex then parsed per block
(it can be tens of MB). The ``Folders/`` tree is walked from the filesystem; an
``order`` file, when present, dictates child order and carries VDJ directives
(``Exists = 0``, ``relocate``, ``Name||recurse``).
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional

from ..model import keys, matching
from ..model.cues import parse_poi_lines
from ..model.library import Library, Playlist, Track, TreeNode, VDJ

_SONG_RE = re.compile(r"<Song\b[^>]*?(?:/>|>.*?</Song>)", re.DOTALL)
# Bytes variant: matching on the raw file avoids decoding + joining the whole
# (tens of MB) file into one string -- a single un-yieldable block that stutters
# audio mid-reload. Each matched <Song> is small and decoded on its own.
_SONG_RE_B = re.compile(rb"<Song\b[^>]*?(?:/>|>.*?</Song>)", re.DOTALL)


def _to_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _track_from_song(song_xml: str) -> Optional[Track]:
    try:
        el = ET.fromstring(song_xml)
    except ET.ParseError:
        return None
    fp = el.get("FilePath")
    if not fp:
        return None
    # NOT decoded again: ElementTree already did it. A second decode turns a
    # name that really contains ``&amp;`` into one containing ``&`` -- a file
    # that is not on disk, so the track reads as missing and cannot be played.
    tags = el.find("Tags")
    infos = el.find("Infos")
    comment_el = el.find("Comment")

    title = artist = album = genre = key = ""
    bpm = None
    if tags is not None:
        title = matching.normalize_text(tags.get("Title"))
        artist = matching.normalize_text(tags.get("Author"))
        album = matching.normalize_text(tags.get("Album"))
        genre = matching.normalize_text(tags.get("Genre"))
        key = tags.get("Key", "") or ""
        # VDJ Bpm attr is in beats-per-*second* fraction? It's stored as a
        # period-like value; convert 60/x when plausible. Keep raw fallback.
        raw_bpm = _to_float(tags.get("Bpm"))
        bpm = _vdj_bpm(raw_bpm)

    length = None
    bitrate = None
    playcount = None
    modified = None
    if infos is not None:
        length = _to_float(infos.get("SongLength"))
        bitrate = _to_int(infos.get("Bitrate"))
        playcount = _to_int(infos.get("PlayCount"))
        lm = _to_int(infos.get("LastModified"))
        if lm:
            try:
                modified = datetime.fromtimestamp(lm)
            except (OverflowError, OSError, ValueError):
                modified = None

    # Element text, so XML has already folded CRLF to LF -- but a file written
    # with a stray CR in it would not be, and the whole point is that the
    # in-memory form is LF whatever the file holds.
    comment = (matching.normalize_text(comment_el.text).strip()
               if comment_el is not None else "")

    return Track(
        key=matching.path_key(fp),
        path=fp,
        source=VDJ,
        title=title,
        artist=artist,
        album=album,
        bpm=bpm,
        length=length,
        comment=comment,
        key_musical=key,
        key_value=keys.parse_vdj_key(key),
        genre=genre,
        bitrate=bitrate,
        playcount=playcount,
        pois=parse_poi_lines(song_xml),
        modified=modified,
    )


def _vdj_bpm(raw: Optional[float]) -> Optional[float]:
    """VirtualDJ stores Bpm as seconds-per-beat (e.g. 0.705882 ~= 85 BPM).
    Convert to BPM when the value looks like a period; pass through otherwise."""
    if raw is None or raw <= 0:
        return raw
    if raw < 10:            # a period, not an actual BPM
        return round(60.0 / raw, 3)
    return raw


def _read_database(xml_path: str, yield_fn=None) -> dict[str, Track]:
    tracks: dict[str, Track] = {}
    if not xml_path or not os.path.exists(xml_path):
        return tracks
    with open(xml_path, "rb") as f:
        raw = f.read()
    for i, m in enumerate(_SONG_RE_B.finditer(raw)):
        t = _track_from_song(m.group(0).decode("utf-8", "ignore"))
        if t is not None:
            tracks[t.key] = t
        # Hand control back to the caller periodically (the UI uses this to keep
        # the audio pull serviced during a big parse -- see main_window).
        if yield_fn is not None and (i & 0x7F) == 0:
            yield_fn()
    return tracks


# ========================= Folders/ tree walk ===========================
def _parse_order(order_path: str):
    """Return (ordered_names, flags) from an ``order`` file.

    ``ordered_names`` is the child order. ``flags[name]`` is a dict with
    ``exists`` (bool) and ``recurse`` (bool).
    """
    ordered: list[str] = []
    flags: dict[str, dict] = {}
    try:
        with open(order_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [ln.rstrip("\n") for ln in f]
    except OSError:
        return ordered, flags

    last = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        # directive lines apply to the previously named entry
        if low.startswith("exists") and "=" in line:
            if last is not None:
                val = line.split("=", 1)[1].strip()
                flags.setdefault(last, {})["exists"] = (val != "0")
            continue
        if low in ("relocate",):
            continue
        name = line
        recurse = False
        if "||" in line:
            name, _, tail = line.partition("||")
            name = name.strip()
            recurse = "recurse" in tail.lower()
        ordered.append(name)
        flags.setdefault(name, {})["recurse"] = recurse
        last = name
    return ordered, flags


def _read_vdjfolder(path: str, tracks: dict[str, Track],
                    yield_fn=None) -> tuple[list[str], bool]:
    """Parse a .vdjfolder; return ``(ordered track keys, no_duplicates)`` and
    register any missing tracks (minimal metadata from the folder entry) into
    ``tracks``. ``no_duplicates`` comes from the ``<VirtualFolder noDuplicates=…>``
    open tag; absent -> True (disallow), matching VirtualDJ's default. A single
    playlist can hold thousands of songs, so ``yield_fn`` is called every ~256
    entries too."""
    keys: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except OSError:
        return keys, True
    fm = re.search(r'<VirtualFolder\b[^>]*\bnoDuplicates="([^"]*)"', content, re.IGNORECASE)
    no_duplicates = (fm.group(1).strip().lower() != "no") if fm else True
    for i, m in enumerate(re.finditer(r"<song\b([^>]*)/?>", content)):
        if yield_fn is not None and (i & 0x7F) == 0:
            yield_fn()
        attrs = m.group(1)
        pm = re.search(r'path="([^"]*)"', attrs)
        if not pm:
            continue
        p = matching.decode_entities(pm.group(1))
        key = matching.path_key(p)
        keys.append(key)
        if key not in tracks:
            tracks[key] = Track(
                key=key, path=p, source=VDJ,
                title=matching.decode_entities(_attr(attrs, "title")),
                artist=matching.decode_entities(_attr(attrs, "artist")),
                bpm=_vdj_bpm(_to_float(_attr(attrs, "bpm"))),
                length=_to_float(_attr(attrs, "songlength")),
                key_musical=_attr(attrs, "key"),
                # only known from the .vdjfolder -- there is no <Song> block
                in_database=False,
            )
    return keys, no_duplicates


def _attr(attrs: str, name: str) -> str:
    m = re.search(name + r'="([^"]*)"', attrs)
    return m.group(1) if m else ""


def _build_folder_node(dir_path: str, name: str, node_path: tuple[str, ...],
                       tracks: dict[str, Track], yield_fn=None) -> TreeNode:
    """Build a folder TreeNode from a ``*.subfolders`` directory. Each
    ``.vdjfolder`` is a separate file read, so ``yield_fn`` is called per
    playlist to keep a big library walk from blocking the caller."""
    node = TreeNode(name=name, kind="folder", source=VDJ, node_path=node_path)
    try:
        entries = os.listdir(dir_path)
    except OSError:
        return node

    subfolder_dirs = {}   # base name -> full path
    vdjfolder_files = {}  # base name -> full path
    for e in entries:
        full = os.path.join(dir_path, e)
        if e.lower().endswith(".subfolders") and os.path.isdir(full):
            subfolder_dirs[e[: -len(".subfolders")]] = full
        elif e.lower().endswith(".vdjfolder") and os.path.isfile(full):
            vdjfolder_files[e[: -len(".vdjfolder")]] = full

    ordered, flags = _parse_order(os.path.join(dir_path, "order"))
    seen: set[str] = set()

    def make_child(base: str) -> Optional[TreeNode]:
        if base in seen:
            return None
        seen.add(base)
        child_path = node_path + (base,)
        has_dir = base in subfolder_dirs
        has_pl = base in vdjfolder_files
        if yield_fn is not None:
            yield_fn()
        if has_dir:
            child = _build_folder_node(subfolder_dirs[base], base, child_path, tracks, yield_fn)
            if has_pl:  # folder that is also its own list
                pl_keys, pl_nodup = _read_vdjfolder(vdjfolder_files[base], tracks, yield_fn)
                child.playlist = Playlist(
                    name=base, source=VDJ,
                    track_keys=pl_keys,
                    vdj_file=vdjfolder_files[base],
                    no_duplicates=pl_nodup,
                )
            return child
        if has_pl:
            # A ".vdjfolder" file on disk always wins, even if the order file
            # marks this name "Exists = 0" / "relocate": that combination is
            # observed in real VDJ libraries pointing at a file VDJ still
            # keeps here (an undocumented VDJ-internal tombstone/relocation
            # marker). Trusting the file's presence over the flag avoids
            # silently hiding a playlist the user can actually open.
            pl_keys, pl_nodup = _read_vdjfolder(vdjfolder_files[base], tracks, yield_fn)
            pl = Playlist(name=base, source=VDJ,
                        track_keys=pl_keys,
                        vdj_file=vdjfolder_files[base],
                        no_duplicates=pl_nodup)
            return TreeNode(name=base, kind="playlist", source=VDJ,
                            playlist=pl, node_path=child_path)
        # named in order but no backing file at all: a true ghost entry.
        if not flags.get(base, {}).get("exists", True):
            pl = Playlist(name=base, source=VDJ, exists=False)
            return TreeNode(name=base, kind="playlist", source=VDJ,
                            playlist=pl, node_path=child_path)
        return None

    for base in ordered:
        child = make_child(base)
        if child is not None:
            node.children.append(child)
    # anything on disk not named in order -> append alphabetically
    for base in sorted(set(subfolder_dirs) | set(vdjfolder_files)):
        child = make_child(base)
        if child is not None:
            node.children.append(child)
    return node


def read(xml_path: str, folders_dir: str, yield_fn=None) -> Library:
    tracks = _read_database(xml_path, yield_fn)
    lib = Library(source=VDJ, tracks=tracks)
    if folders_dir and os.path.isdir(folders_dir):
        lib.root = _build_folder_node(folders_dir, "VDJ Playlists", (), tracks, yield_fn)
    else:
        lib.root = TreeNode(name="VDJ Playlists", kind="folder", source=VDJ)
    return lib
