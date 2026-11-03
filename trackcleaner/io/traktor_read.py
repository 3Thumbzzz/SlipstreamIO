"""Read a Traktor ``collection.nml`` into a :class:`Library`.

The COLLECTION can hold tens of thousands of ``<ENTRY>`` blocks in a ~130 MB
file, so entries are sliced out by regex and parsed one at a time (as the
original ``nml_to_xml_sync.py`` did) rather than DOM-parsing the whole file.
The much smaller ``<PLAYLISTS>`` tail is parsed with ElementTree since its
nested node structure is far easier to walk as a DOM.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Optional

from ..model import keys, matching
from ..model.cues import pois_from_cue_v2
from ..model.library import Library, Playlist, Track, TreeNode, TRAKTOR

# Bytes variants: match on the raw file so a huge NML (up to ~130 MB) is never
# decoded + joined into one string (a single un-yieldable block that would
# stutter audio mid-reload). ElementTree parses the small matched blocks directly
# from bytes.
_ENTRY_RE = re.compile(rb"<ENTRY\b[^>]*>.*?</ENTRY>", re.DOTALL | re.IGNORECASE)
_PLAYLISTS_RE = re.compile(rb"<PLAYLISTS>.*?</PLAYLISTS>", re.DOTALL | re.IGNORECASE)


def _parse_modified(entry: ET.Element) -> Optional[datetime]:
    date_s = entry.get("MODIFIED_DATE")
    if not date_s:
        return None
    try:
        y, m, d = (int(x) for x in date_s.split("/"))
        base = datetime(y, m, d)
    except (ValueError, TypeError):
        return None
    secs = entry.get("MODIFIED_TIME")
    if secs:
        try:
            base += timedelta(seconds=int(secs))
        except (ValueError, TypeError):
            pass
    return base


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


def _track_from_entry(entry: ET.Element) -> Optional[Track]:
    loc = entry.find("LOCATION")
    if loc is None or not loc.get("FILE"):
        return None
    win_path = matching.build_windows_path(
        loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", "")
    )
    info = entry.find("INFO")
    album = entry.find("ALBUM")
    tempo = entry.find("TEMPO")

    bitrate = None
    genre = key = comment = comment2 = ""
    length = None
    playcount = None
    if info is not None:
        br = _to_int(info.get("BITRATE"))
        bitrate = br // 1000 if br else None
        genre = matching.normalize_text(info.get("GENRE"))
        key = info.get("KEY", "") or ""
        # Traktor writes a line break as &#xD;&#xA;, which parses back as CRLF;
        # VirtualDJ's element text parses back as LF. Fold to LF on the way in
        # so the two can ever compare equal.
        comment = matching.normalize_text(info.get("COMMENT"))
        comment2 = matching.normalize_text(info.get("COMMENT2"))
        length = _to_float(info.get("PLAYTIME"))
        playcount = _to_int(info.get("PLAYCOUNT"))

    # MUSICAL_KEY is unambiguous; INFO/@KEY is free text and holds Open Key
    # on some entries and musical notation on others, so it is a display
    # value only. Parsing it is the fallback for entries Traktor has not
    # analysed.
    musical = entry.find("MUSICAL_KEY")
    key_value = _to_int(musical.get("VALUE")) if musical is not None else None
    if key_value is None:
        key_value = keys.parse_vdj_key(key)

    cue_dicts = [c.attrib for c in entry.findall("CUE_V2")]

    return Track(
        key=matching.path_key(win_path),
        path=win_path,
        source=TRAKTOR,
        title=matching.normalize_text(entry.get("TITLE")),
        artist=matching.normalize_text(entry.get("ARTIST")),
        album=matching.normalize_text(
            album.get("TITLE") if album is not None else ""),
        bpm=_to_float(tempo.get("BPM")) if tempo is not None else None,
        length=length,
        comment=comment,
        comment2=comment2,
        key_musical=key,
        key_value=key_value,
        genre=genre,
        bitrate=bitrate,
        playcount=playcount,
        pois=pois_from_cue_v2(cue_dicts),
        modified=_parse_modified(entry),
    )


def _build_tree(node: ET.Element, source: str, parent_path: tuple[str, ...]) -> Optional[TreeNode]:
    ntype = (node.get("TYPE") or "").upper()
    name = node.get("NAME", "") or ""
    node_path = parent_path + (name,)

    if ntype == "FOLDER":
        tn = TreeNode(name=name, kind="folder", source=source, node_path=node_path)
        subnodes = node.find("SUBNODES")
        if subnodes is not None:
            for child in subnodes.findall("NODE"):
                built = _build_tree(child, source, node_path)
                if built is not None:
                    tn.children.append(built)
        return tn

    if ntype == "PLAYLIST":
        pl_el = node.find("PLAYLIST")
        track_keys: list[str] = []
        uuid = ""
        if pl_el is not None:
            uuid = pl_el.get("UUID", "") or ""
            for entry in pl_el.findall("ENTRY"):
                pk = entry.find("PRIMARYKEY")
                if pk is None:
                    continue
                key = pk.get("KEY", "")
                if key:
                    # KEY is like "D:/:Music/:x/:y.mp3" -> canonical key. Shared
                    # with the playlist writer so both agree on identity.
                    track_keys.append(matching.traktor_key_to_path_key(key))
        playlist = Playlist(name=name, source=source, track_keys=track_keys,
                            traktor_uuid=uuid, no_duplicates=False)
        return TreeNode(name=name, kind="playlist", source=source,
                        playlist=playlist, node_path=node_path)
    return None


def _split_primarykey(key: str):
    """A PRIMARYKEY KEY ``D:/:Music/:x/:y.mp3`` -> (VOLUME, DIR, FILE)."""
    key = matching.decode_entities(key)
    m = re.match(r"^([A-Za-z]:)/:(.*)$", key)
    if m:
        volume, rest = m.group(1), m.group(2)
    else:
        volume, rest = "", key.lstrip("/:")
    parts = [p for p in rest.split("/:") if p]
    file_name = parts[-1] if parts else ""
    dir_attr = ("/:" + "/:".join(parts[:-1]) + "/:") if len(parts) > 1 else "/:"
    return volume, dir_attr, file_name


def read(nml_path: str, yield_fn=None) -> Library:
    """Parse the whole NML into a Library. ``yield_fn`` (optional) is called every
    ~256 entries so a caller can keep an event loop -- and audio -- alive during a
    big parse (see main_window)."""
    with open(nml_path, "rb") as f:
        content = f.read()

    lib = Library(source=TRAKTOR)

    for i, m in enumerate(_ENTRY_RE.finditer(content)):
        try:
            entry = ET.fromstring(m.group(0))   # ElementTree parses bytes directly
        except ET.ParseError:
            continue
        track = _track_from_entry(entry)
        if track is not None:
            lib.tracks[track.key] = track    # last one wins (as documented)
        if yield_fn is not None and (i & 0x7F) == 0:
            yield_fn()

    root = TreeNode(name="Traktor Playlists", kind="folder", source=TRAKTOR)
    pm = _PLAYLISTS_RE.search(content)
    if pm:
        try:
            playlists_el = ET.fromstring(pm.group(0))
            root_node = playlists_el.find("NODE")   # $ROOT
            subnodes = root_node.find("SUBNODES") if root_node is not None else None
            if subnodes is not None:
                for child in subnodes.findall("NODE"):
                    built = _build_tree(child, TRAKTOR, ())
                    if built is not None:
                        root.children.append(built)
        except ET.ParseError:
            pass
    lib.root = root
    return lib
