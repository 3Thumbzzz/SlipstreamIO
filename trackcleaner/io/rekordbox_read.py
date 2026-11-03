"""Read a ``rekordbox.xml`` into a :class:`Library`.

Rekordbox's documented exchange format, ``<DJ_PLAYLISTS>``: a ``<COLLECTION>`` of
``<TRACK>`` elements, then a ``<PLAYLISTS>`` tree of ``<NODE>`` elements
(``Type="0"`` folder, ``Type="1"`` playlist) whose members are
``<TRACK Key="…"/>`` references -- a ``TrackID`` when the playlist says
``KeyType="0"``, the track's ``Location`` when it says ``KeyType="1"``.

Its live library is an encrypted database; this file is what Rekordbox exports
(File → Export Collection in xml format) and imports (the "rekordbox xml" view).

Mapping onto the shared model:

* **Location** is a ``file://localhost/`` URL, percent-encoded. It is decoded
  exactly once, after ElementTree has decoded the XML entities -- two separate
  layers, each undone once (see ``matching.path_key``).
* **Cues**: a ``<POSITION_MARK Type="0">`` with ``Num`` 0..7 is a hot cue, read as
  ``Poi(Start, Num + 1, "cue")`` -- the same ``HOTCUE + 1`` numbering Traktor's
  reader uses, so pad A is cue 1 everywhere. Memory cues (``Num="-1"``), loops,
  fades and load points are not ours: ignored here, preserved by the writer.
* **Beatgrid**: the first ``<TEMPO>``'s ``Inizio`` is the grid anchor.
* **Key**: ``Tonality`` is musical notation ("Am", "Ebm"), the notation
  ``keys.parse_vdj_key`` reads.

Like the NML reader, tracks are sliced out by regex and parsed one block at a
time, so a large file never becomes one un-yieldable parse.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import unquote

from ..model import keys, matching
from ..model.cues import Poi
from ..model.library import Library, Playlist, Track, TreeNode

REKORDBOX = "rekordbox"

_LOCATION_PREFIX = "file://localhost/"

_COLLECTION_RE = re.compile(rb"<COLLECTION\b[^>]*>(.*?)</COLLECTION>",
                            re.DOTALL)
#: One collection track: self-closing, or with TEMPO / POSITION_MARK children.
_TRACK_RE = re.compile(rb"<TRACK\b[^>]*?(?:/>|>.*?</TRACK>)", re.DOTALL)
_PLAYLISTS_RE = re.compile(rb"<PLAYLISTS>.*?</PLAYLISTS>", re.DOTALL)

#: Hot cue slots A..H. A ``Num`` outside this range is a memory cue (-1).
HOT_CUE_SLOTS = range(0, 8)


def location_to_path(location: str) -> str:
    """``file://localhost/D:/Music/A%20B.mp3`` -> ``D:\\Music\\A B.mp3``.

    ``location`` must already be entity-decoded (an ElementTree attribute is).
    Percent-decoding happens here and nowhere else, once.
    """
    if not location:
        return ""
    rest = location
    if rest.lower().startswith(_LOCATION_PREFIX):
        rest = rest[len(_LOCATION_PREFIX):]
    elif rest.lower().startswith("file:///"):
        rest = rest[len("file:///"):]
    return unquote(rest).replace("/", "\\")


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


def pois_from_track(el: ET.Element) -> list[Poi]:
    """The hot cues and grid anchor of one ``<TRACK>``, as shared Pois."""
    pois: list[Poi] = []
    tempo = el.find("TEMPO")
    if tempo is not None:
        start = _to_float(tempo.get("Inizio"))
        if start is not None:
            pois.append(Poi(start, None, "beatgrid"))
    for mark in el.findall("POSITION_MARK"):
        if mark.get("Type", "0") != "0":
            continue                      # loop, fade or load point: not ours
        num = _to_int(mark.get("Num"))
        start = _to_float(mark.get("Start"))
        if num in HOT_CUE_SLOTS and start is not None:
            pois.append(Poi(start, num + 1, "cue"))
    return pois


def track_from_element(el: ET.Element) -> Optional[Track]:
    path = location_to_path(el.get("Location", ""))
    if not path:
        return None
    tonality = el.get("Tonality", "") or ""
    return Track(
        key=matching.path_key(path),
        path=path,
        source=REKORDBOX,
        title=matching.normalize_text(el.get("Name")),
        artist=matching.normalize_text(el.get("Artist")),
        album=matching.normalize_text(el.get("Album")),
        genre=matching.normalize_text(el.get("Genre")),
        comment=matching.normalize_text(el.get("Comments")),
        bpm=_to_float(el.get("AverageBpm")),
        length=_to_float(el.get("TotalTime")),
        bitrate=_to_int(el.get("BitRate")),
        playcount=_to_int(el.get("PlayCount")),
        key_musical=tonality,
        key_value=keys.parse_vdj_key(tonality),
        pois=pois_from_track(el),
    )


def _build_tree(node: ET.Element, by_id: dict, parent_path: tuple) -> Optional[TreeNode]:
    name = node.get("Name", "") or ""
    node_path = parent_path + (name,)
    if node.get("Type") == "0":
        tn = TreeNode(name=name, kind="folder", source=REKORDBOX,
                      node_path=node_path)
        for child in node.findall("NODE"):
            built = _build_tree(child, by_id, node_path)
            if built is not None:
                tn.children.append(built)
        return tn
    if node.get("Type") == "1":
        by_location = node.get("KeyType") == "1"
        track_keys = []
        for ref in node.findall("TRACK"):
            key = ref.get("Key", "")
            if by_location:
                path = location_to_path(key)
                if path:
                    track_keys.append(matching.path_key(path))
            elif key in by_id:
                track_keys.append(by_id[key])
        playlist = Playlist(name=name, source=REKORDBOX, track_keys=track_keys,
                            no_duplicates=False)
        return TreeNode(name=name, kind="playlist", source=REKORDBOX,
                        playlist=playlist, node_path=node_path)
    return None


def read(xml_path: str, yield_fn=None) -> Library:
    with open(xml_path, "rb") as f:
        content = f.read()

    lib = Library(source=REKORDBOX)
    by_id: dict[str, str] = {}           # TrackID -> track key
    coll = _COLLECTION_RE.search(content)
    body = coll.group(1) if coll else b""
    for i, m in enumerate(_TRACK_RE.finditer(body)):
        try:
            el = ET.fromstring(m.group(0))
        except ET.ParseError:
            continue
        track = track_from_element(el)
        if track is not None:
            lib.tracks[track.key] = track          # last one wins, as elsewhere
            if el.get("TrackID"):
                by_id[el.get("TrackID")] = track.key
        if yield_fn is not None and (i & 0x7F) == 0:
            yield_fn()

    root = TreeNode(name="Rekordbox Playlists", kind="folder", source=REKORDBOX)
    pm = _PLAYLISTS_RE.search(content)
    if pm:
        try:
            top = ET.fromstring(pm.group(0)).find("NODE")      # ROOT
        except ET.ParseError:
            top = None
        for child in (top.findall("NODE") if top is not None else ()):
            built = _build_tree(child, by_id, ())
            if built is not None:
                root.children.append(built)
    lib.root = root
    return lib
