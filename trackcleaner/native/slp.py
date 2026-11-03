"""The ``.slp`` file — SlipStream's own library format.

Deliberately **neutral**. It is NML-inspired in shape (an XML tree you can read
and diff by eye) but carries none of Traktor's conventions, because baking one
vendor's quirks into the shared layer would defeat the point of having one:

* paths are plain and absolute -- no ``VOLUME``/``DIR`` with ``/:`` separators;
* cues and beatgrids are separate elements -- no ``TYPE`` codes where 4 means
  "a beatgrid, and also a cue if HOTCUE says so";
* one key notation, not Open-Key on one side and musical on the other;
* **positions are canonical** -- seconds from the first decoded audio sample.
  Every adapter converts to and from that (see ``LibraryAdapter.to_native_pos``),
  so a position in here means the same physical instant regardless of which
  program it came from.

Unlike the vendor writers this parses and re-renders the whole document with
ElementTree rather than doing text surgery. That is safe *because we own every
byte of it*: there is no foreign data to preserve. The surgical rule exists to
protect VirtualDJ's automix/``<Scan>`` points and Traktor's loop/fade markers,
and none of those live here.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET

from ..io.write_queue import atomic_write_text
from ..model.cues import Poi
from ..model.library import Library, Playlist, TreeNode
from ..model.matching import path_key

SLIPSTREAM = "slipstream"
FORMAT_VERSION = "1"


# ============================== reading =================================
def empty_doc() -> ET.Element:
    root = ET.Element("SlipStream", {"VERSION": FORMAT_VERSION})
    ET.SubElement(root, "COLLECTION")
    ET.SubElement(root, "PLAYLISTS")
    return root


def load_doc(path: str) -> ET.Element:
    """Parse the file, or hand back an empty document. A missing or corrupt
    ``.slp`` is not an error: it is our own file and we can always rebuild it,
    so the app opens with an empty SlipStream library rather than refusing."""
    if not path or not os.path.exists(path):
        return empty_doc()
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return empty_doc()
    if root.find("COLLECTION") is None:
        ET.SubElement(root, "COLLECTION")
    if root.find("PLAYLISTS") is None:
        ET.SubElement(root, "PLAYLISTS")
    return root


def save_doc(path: str, root: ET.Element) -> None:
    ET.indent(root, space="  ")
    xml = ET.tostring(root, encoding="unicode")
    atomic_write_text(path, '<?xml version="1.0" encoding="UTF-8"?>\n' + xml + "\n")


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _track_from_el(el: ET.Element):
    from ..model.library import Track
    p = el.get("PATH", "")
    if not p:
        return None
    pois = []
    for grid in el.findall("BEATGRID"):
        pois.append(Poi(_to_float(grid.get("POS")) or 0.0, None, "beatgrid"))
    for cue in el.findall("CUE"):
        num = cue.get("NUM")
        pois.append(Poi(_to_float(cue.get("POS")) or 0.0,
                        int(num) if num and num.isdigit() else None, "cue"))
    playcount = el.get("PLAYCOUNT")
    return Track(
        key=path_key(p), path=p, source=SLIPSTREAM,
        title=el.get("TITLE", "") or "",
        artist=el.get("ARTIST", "") or "",
        album=el.get("ALBUM", "") or "",
        genre=el.get("GENRE", "") or "",
        comment=el.get("COMMENT", "") or "",
        key_musical=el.get("KEY", "") or "",
        bpm=_to_float(el.get("BPM")),
        length=_to_float(el.get("LENGTH")),
        playcount=int(playcount) if playcount and playcount.isdigit() else None,
        pois=pois,
    )


def _build_node(el: ET.Element, parent_path: tuple, yield_fn=None) -> TreeNode:
    name = el.get("NAME", "") or ""
    node_path = parent_path + (name,)
    if el.tag == "PLAYLIST":
        keys = [path_key(e.get("PATH", "")) for e in el.findall("ENTRY")
                if e.get("PATH")]
        return TreeNode(
            name=name, kind="playlist", source=SLIPSTREAM, node_path=node_path,
            # Duplicates are allowed: a set can legitimately play the same track
            # twice, and nothing in our own format needs to forbid it.
            playlist=Playlist(name=name, source=SLIPSTREAM, track_keys=keys,
                              no_duplicates=False))
    node = TreeNode(name=name, kind="folder", source=SLIPSTREAM, node_path=node_path)
    for child in el:
        if child.tag in ("FOLDER", "PLAYLIST"):
            node.children.append(_build_node(child, node_path, yield_fn))
    if yield_fn is not None:
        yield_fn()
    return node


def read(path: str, yield_fn=None) -> Library:
    root = load_doc(path)
    lib = Library(source=SLIPSTREAM)
    collection = root.find("COLLECTION")
    for i, el in enumerate(collection.findall("TRACK") if collection is not None else []):
        track = _track_from_el(el)
        if track is not None:
            lib.tracks[track.key] = track
        if yield_fn is not None and (i & 0x7F) == 0:
            yield_fn()
    tree_root = TreeNode(name="SlipStream Playlists", kind="folder", source=SLIPSTREAM)
    playlists = root.find("PLAYLISTS")
    for child in (playlists if playlists is not None else []):
        if child.tag in ("FOLDER", "PLAYLIST"):
            tree_root.children.append(_build_node(child, (), yield_fn))
    lib.root = tree_root
    _adopt_orphan_entries(root, lib)
    return lib


def _adopt_orphan_entries(root: ET.Element, lib: Library) -> None:
    """Give every playlist entry a track, even one the collection never got.

    A row is only drawn for a key that resolves in ``lib.tracks``, so an entry
    with no collection record disappears from the playlist while still sitting in
    the file -- and re-adding it then produces a visible duplicate beside the
    invisible original. ``set_entries`` writes the collection record now, but
    files written before it did still exist, so the reader repairs them rather
    than showing an empty playlist.

    Mirrors :mod:`trackcleaner.io.vdj_read`, where a track known only from a
    ``.vdjfolder`` is real but ``in_database=False``.
    """
    from ..model.library import Track

    playlists = root.find("PLAYLISTS")
    if playlists is None:
        return
    for entry in playlists.iter("ENTRY"):
        path = entry.get("PATH", "")
        if not path:
            continue
        key = path_key(path)
        if key not in lib.tracks:
            lib.tracks[key] = Track(key=key, path=path, source=SLIPSTREAM,
                                    in_database=False)


# ============================== navigation ==============================
def _children(el: ET.Element):
    return [c for c in el if c.tag in ("FOLDER", "PLAYLIST")]


def find_el(root: ET.Element, node_path) -> ET.Element | None:
    """The FOLDER/PLAYLIST element at ``node_path``. Matching is
    case-insensitive, the same rule the rest of the app uses for node paths."""
    current = root.find("PLAYLISTS")
    if current is None:
        return None
    for name in node_path:
        wanted = str(name).lower()
        nxt = next((c for c in _children(current)
                    if (c.get("NAME", "") or "").lower() == wanted), None)
        if nxt is None:
            return None
        current = nxt
    return current


def _parent_el(root: ET.Element, node_path):
    return find_el(root, tuple(node_path)[:-1]) if len(node_path) > 1 \
        else root.find("PLAYLISTS")


# ============================== mutation ================================
def ensure_folder(root: ET.Element, node_path) -> None:
    current = root.find("PLAYLISTS")
    for depth in range(1, len(node_path) + 1):
        existing = find_el(root, tuple(node_path[:depth]))
        if existing is None:
            existing = ET.SubElement(current, "FOLDER", {"NAME": node_path[depth - 1]})
        current = existing


def set_entries(root: ET.Element, el: ET.Element, tracks) -> None:
    """Rewrite ``el``'s membership, and make sure the collection knows every
    track it now names.

    Both halves matter. An ``ENTRY`` carries only a path, so a playlist whose
    members are absent from ``<COLLECTION>`` reads back as a list of keys that
    resolve to nothing -- the playlist renders **empty** while the file still
    holds it, and adding one of those tracks again appends a second entry
    beside the invisible first. Writing the collection entry alongside is what
    keeps the file self-describing.
    """
    for entry in el.findall("ENTRY"):
        el.remove(entry)
    for t in tracks:
        ET.SubElement(el, "ENTRY", {"PATH": t.path})
        ensure_track(root, t)


def create_playlist(root: ET.Element, node_path, tracks) -> None:
    if len(node_path) > 1:
        ensure_folder(root, tuple(node_path)[:-1])
    parent = _parent_el(root, node_path)
    if parent is None:
        return
    el = ET.SubElement(parent, "PLAYLIST", {"NAME": node_path[-1]})
    set_entries(root, el, tracks)


def set_playlist_entries(root: ET.Element, node_path, tracks) -> bool:
    el = find_el(root, node_path)
    if el is None or el.tag != "PLAYLIST":
        return False
    set_entries(root, el, tracks)
    return True


def relocate(root: ET.Element, old_path, new_path) -> bool:
    """Rename and/or move in one step -- for a tree we own outright these are
    the same edit: detach the element, rename it, re-attach under the new
    parent."""
    el = find_el(root, old_path)
    if el is None:
        return False
    old_parent = _parent_el(root, old_path)
    if old_parent is None:
        return False
    old_parent.remove(el)
    el.set("NAME", new_path[-1])
    if len(new_path) > 1:
        ensure_folder(root, tuple(new_path)[:-1])
    new_parent = _parent_el(root, new_path)
    if new_parent is None:                 # destination vanished: put it back
        old_parent.append(el)
        return False
    new_parent.append(el)
    return True


def remove_node(root: ET.Element, node_path) -> bool:
    el = find_el(root, node_path)
    parent = _parent_el(root, node_path)
    if el is None or parent is None:
        return False
    parent.remove(el)
    return True


def upsert_track(root: ET.Element, track) -> None:
    """Add or replace one collection entry, matched on path identity rather than
    the raw string so a differently-cased path never yields a duplicate."""
    collection = root.find("COLLECTION")
    if collection is None:
        collection = ET.SubElement(root, "COLLECTION")
    key = path_key(track.path)
    for el in collection.findall("TRACK"):
        if path_key(el.get("PATH", "")) == key:
            collection.remove(el)
            break
    attrs = {"PATH": track.path}
    for name, value in (("TITLE", track.title), ("ARTIST", track.artist),
                        ("ALBUM", track.album), ("GENRE", track.genre),
                        ("COMMENT", track.comment), ("KEY", track.key_musical)):
        if value:
            attrs[name] = value
    if track.bpm:
        attrs["BPM"] = f"{track.bpm:.6f}"
    if track.length:
        attrs["LENGTH"] = f"{track.length:.6f}"
    if track.playcount is not None:
        attrs["PLAYCOUNT"] = str(track.playcount)
    el = ET.SubElement(collection, "TRACK", attrs)
    for poi in sorted(track.pois, key=lambda p: (p.pos, p.kind)):
        if poi.kind == "beatgrid":
            ET.SubElement(el, "BEATGRID", {"POS": f"{max(poi.pos, 0.0):.6f}"})
        elif poi.kind == "cue":
            cue = {"POS": f"{max(poi.pos, 0.0):.6f}"}
            if poi.num is not None:
                cue["NUM"] = str(poi.num)
            ET.SubElement(el, "CUE", cue)


def find_track(root: ET.Element, path: str):
    """The collection's ``TRACK`` for ``path``, or None. Matched on path
    identity rather than the raw string, so case never splits one track in two."""
    collection = root.find("COLLECTION")
    if collection is None:
        return None
    key = path_key(path)
    for el in collection.findall("TRACK"):
        if path_key(el.get("PATH", "")) == key:
            return el
    return None


def ensure_track(root: ET.Element, track) -> None:
    """Add ``track`` to the collection only if that path is not already there.

    Deliberately not an upsert: adding a track to a playlist must not overwrite
    what SlipStream already knows about it. The incoming Track is whatever the
    drag came from, which may carry less than our own record.
    """
    if find_track(root, track.path) is None:
        upsert_track(root, track)


#: Editable text fields -> the attribute holding each. BPM and KEY are absent on
#: purpose: they are analysis output, and moving the BPM without the beatgrid
#: desynchronises the cues.
_METADATA_ATTRS = {"title": "TITLE", "artist": "ARTIST", "album": "ALBUM",
                   "genre": "GENRE", "comment": "COMMENT"}

#: Plus the key, which a sync may carry even though a user may not edit it.
_WRITABLE_ATTRS = {**_METADATA_ATTRS, "key": "KEY"}


def set_track_metadata(root: ET.Element, key: str, fields: dict) -> bool:
    """Set text metadata on the collection entry matching ``key``.

    A field **absent from ``fields`` is left alone**, which is what lets a
    multi-track edit leave every ``<various>`` field at each track's own value.
    An empty value removes the attribute rather than storing ``""``, keeping the
    document the same shape a freshly written one would have.

    The one-entry form of :func:`set_tracks_metadata`.
    """
    return set_tracks_metadata(root, {key: fields})[key]


def set_tracks_metadata(root: ET.Element, by_key: dict) -> dict:
    """Set text metadata on **many** entries in one scan of the collection.

    Returns ``{key: changed}``. The text formats have to walk their document to
    find one entry, so writing N tracks one at a time is N walks; this one keeps
    the interface the same across the adapters even though a parsed tree could
    be indexed either way.
    """
    changed = {k: False for k in by_key}
    collection = root.find("COLLECTION")
    if collection is None:
        return changed
    for el in collection.findall("TRACK"):
        fields = by_key.get(path_key(el.get("PATH", "")))
        if fields is None:
            continue
        for field, value in fields.items():
            attr = _WRITABLE_ATTRS.get(field)
            if attr is None:
                continue
            if value:
                el.set(attr, value)
            else:
                el.attrib.pop(attr, None)
        changed[path_key(el.get("PATH", ""))] = True
    return changed


def tracks_by_key(root: ET.Element, keys) -> dict:
    """``{key: Track}`` for just these keys, out of an already-parsed document.

    No locating to do -- this format is held as a tree rather than as text, and
    it is small because it carries only what SlipStream owns.
    """
    wanted = set(keys)
    if not wanted:
        return {}
    collection = root.find("COLLECTION")
    out: dict = {}
    for el in (collection.findall("TRACK") if collection is not None else []):
        track = _track_from_el(el)
        if track is not None and track.key in wanted:
            out[track.key] = track
    return out


def repath_tracks(root: ET.Element, by_key: dict) -> dict:
    """Point tracks at new files. ``by_key`` maps a current key to a new path.

    Both places a path appears move together: the ``TRACK`` in the COLLECTION,
    and every playlist ``ENTRY`` naming it -- membership here is by path, like
    Traktor's primary keys, so rewriting only the collection would drop the
    track from its playlists.
    """
    done = {k: False for k in by_key}
    collection = root.find("COLLECTION")
    if collection is not None:
        for el in collection.findall("TRACK"):
            new_path = by_key.get(path_key(el.get("PATH", "")))
            if new_path is None:
                continue
            done[path_key(el.get("PATH", ""))] = True
            el.set("PATH", new_path)
    for el in root.iter("ENTRY"):
        new_path = by_key.get(path_key(el.get("PATH", "")))
        if new_path is not None:
            el.set("PATH", new_path)
    return done


def remove_track(root: ET.Element, key: str) -> bool:
    collection = root.find("COLLECTION")
    if collection is None:
        return False
    for el in collection.findall("TRACK"):
        if path_key(el.get("PATH", "")) == key:
            collection.remove(el)
            return True
    return False
