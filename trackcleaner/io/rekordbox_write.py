"""Text-surgical writes to a ``rekordbox.xml``.

Same rules as the NML writer: every edit is a regex or a splice scoped to one
``<TRACK>`` block or one ``<NODE>`` span, so the rest of the file -- attributes
this app does not understand, Rekordbox's own formatting, every other track --
survives byte-for-byte. Every function is a pure ``str -> (str, answer)``, which
is what lets the adapter batch them inside a collection session.

**What is ours on a track** is what the reader turns into Pois: hot cues
(``<POSITION_MARK Type="0">`` with ``Num`` 0..7) and the grid anchor (the single
``<TEMPO>``). Memory cues, loops, fades and load points are carried through
untouched. A track whose grid has several ``<TEMPO>`` elements -- a tempo-changing
grid -- keeps it: one anchor cannot describe it, and replacing it would destroy
analysis Rekordbox cannot recreate.

**Playlists** are a nested ``<NODE>`` tree with no stable identifier, so they are
addressed by path (case-insensitively, as everywhere else) and edited by
splicing the spans :func:`scan_nodes` finds. A playlist names its members by
``TrackID`` (``KeyType="0"``) or by ``Location`` (``KeyType="1"``); writes keep
whichever the playlist already uses, and new playlists use ``TrackID``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional
from urllib.parse import quote
from xml.sax.saxutils import escape

from ..model import keys, matching
from ..model.library import Track, path_ci
from .rekordbox_read import HOT_CUE_SLOTS, location_to_path

_COLLECTION_OPEN_RE = re.compile(r'(<COLLECTION\b[^>]*?\bEntries=")(\d*)(")')
_TRACK_RE = re.compile(r"<TRACK\b[^>]*?(?:/>|>.*?</TRACK>)", re.DOTALL)
_OPEN_TAG_RE = re.compile(r"<TRACK\b[^>]*?/?>")
_CHILD_RE = re.compile(r"<([A-Za-z_][\w.-]*)\b[^>]*?(?:/>|>.*?</\1>)", re.DOTALL)
_ATTR_RE = r'\b{name}="([^"]*)"'

#: Characters Rekordbox leaves literal in a Location; everything else is
#: percent-encoded (``&`` as ``%26``, ``[`` as ``%5B``, UTF-8 bytes as ``%E3``…).
#: Read off a real 21,855-track file so a written path looks like Rekordbox's own.
_LOCATION_SAFE = "/:()'!,+~@$;="

def tonality(key_value: Optional[int]) -> str:
    """Canonical key 0..23 -> Rekordbox's own notation, which spells every key
    with flats -- "Ebm", "Bb", never "D#m" -- or ``""``."""
    return keys.flat_key(key_value)


def path_to_location(path: str) -> str:
    """``D:\\Music\\A & B.mp3`` -> ``file://localhost/D:/Music/A%20%26%20B.mp3``.
    The inverse of :func:`rekordbox_read.location_to_path`."""
    return "file://localhost/" + quote(path.replace("\\", "/"), safe=_LOCATION_SAFE)


def _attr(value) -> str:
    """A double-quoted attribute value. Line breaks and tabs become character
    references, as on the other writers: a literal one would come back from the
    parser normalised to a space, and a bare CR would double on the next write."""
    text = escape(str(value), {'"': "&quot;", "\n": "&#10;", "\r": "&#13;",
                               "\t": "&#9;"})
    return f'"{text}"'


def _get(tag: str, name: str) -> Optional[str]:
    m = re.search(_ATTR_RE.format(name=re.escape(name)), tag)
    return matching.decode_entities(m.group(1)) if m else None


def _set(tag: str, name: str, value) -> str:
    """``tag`` with attribute ``name`` set to ``value``, replaced in place or
    appended before the closing bracket."""
    pattern = re.compile(_ATTR_RE.format(name=re.escape(name)))
    if pattern.search(tag):
        return pattern.sub(lambda _m: f"{name}={_attr(value)}", tag, count=1)
    return re.sub(r"\s*(/?>)$", lambda m: f" {name}={_attr(value)}{m.group(1)}",
                  tag, count=1)


# ============================== the collection ==============================
def _collection_span(content: str) -> tuple[int, int]:
    """``(start, end)`` of the text between ``<COLLECTION …>`` and its close."""
    m = re.search(r"<COLLECTION\b[^>]*?(/?)>", content)
    if m is None:
        return -1, -1
    if m.group(1):
        return m.end(), m.end()
    return m.end(), content.find("</COLLECTION>", m.end())


def _block_key(block: str) -> str:
    """The track key a ``<TRACK>`` block describes. The Location is raw XML text
    here, so its entities are decoded once, then its percent-escapes once."""
    open_tag = _OPEN_TAG_RE.match(block).group(0)
    return matching.path_key(location_to_path(_get(open_tag, "Location") or ""))


def _tracks(content: str):
    """``(start, end, key, track_id)`` for every collection track."""
    start, end = _collection_span(content)
    if start < 0:
        return
    for m in _TRACK_RE.finditer(content, start, end):
        open_tag = _OPEN_TAG_RE.match(m.group(0)).group(0)
        yield (m.start(), m.end(), _block_key(m.group(0)),
               _get(open_tag, "TrackID") or "")


def track_ids(content: str) -> dict:
    """``{track key: TrackID}``. Last one wins, as in the reader."""
    return {key: tid for _s, _e, key, tid in _tracks(content)}


def _splice(content: str, edits) -> str:
    """Apply ``[(start, end, replacement)]`` edits, which must not overlap."""
    out, pos = [], 0
    for start, end, text in sorted(edits, key=lambda e: e[0]):
        out.append(content[pos:start])
        out.append(text)
        pos = end
    out.append(content[pos:])
    return "".join(out)


def _split(block: str) -> tuple[str, list[str]]:
    """A track block -> (opening tag ending in ``>``, its child elements)."""
    open_tag = _OPEN_TAG_RE.match(block).group(0)
    if open_tag.endswith("/>"):
        return open_tag[:-2].rstrip() + ">", []
    inner = block[len(open_tag):-len("</TRACK>")]
    return open_tag, [m.group(0) for m in _CHILD_RE.finditer(inner)]


def _join(open_tag: str, children: list[str], indent: str = "\t\t") -> str:
    if not children:
        return open_tag[:-1].rstrip() + "/>"
    inner = "".join(f"\n{indent}\t{c}" for c in children)
    return f"{open_tag}{inner}\n{indent}</TRACK>"


def _is_owned_mark(element: str) -> bool:
    if not element.startswith("<POSITION_MARK"):
        return False
    if (_get(element, "Type") or "0") != "0":
        return False
    try:
        return int(_get(element, "Num") or "") in HOT_CUE_SLOTS
    except ValueError:
        return False


def _mark(poi) -> str:
    return (f'<POSITION_MARK Name="" Type="0" Start="{poi.pos:.3f}" '
            f'Num="{poi.num - 1}"/>')


def _tempo(anchor: float, bpm: float, old: str = "") -> str:
    """The grid as one ``<TEMPO>``. A replaced one keeps its ``Metro``."""
    metro = (_get(old, "Metro") if old else None) or "4/4"
    return (f'<TEMPO Inizio="{anchor:.3f}" Bpm="{bpm:.2f}" '
            f'Metro={_attr(metro)} Battito="1"/>')


def _hot_cues(track: Track) -> list:
    """The track's cues Rekordbox can hold: pads A..H. A higher-numbered cue
    (VirtualDJ has more pads) has nowhere to go and is left out."""
    return sorted((p for p in track.pois
                   if p.kind == "cue" and p.num and p.num - 1 in HOT_CUE_SLOTS),
                  key=lambda p: (p.num, p.pos))


def track_with_cues(block: str, track: Track) -> str:
    """One ``<TRACK>`` rewritten with ``track``'s hot cues, grid anchor and play
    count. The whole owned set is replaced, so a cue deleted on the source side
    goes here too; everything else is carried through as it was."""
    open_tag, children = _split(block)
    tempos = [c for c in children if c.startswith("<TEMPO")]
    grid = sorted(p.pos for p in track.pois if p.kind == "beatgrid")
    kept = [c for c in children if not _is_owned_mark(c)]
    if grid and len(tempos) <= 1:
        bpm = track.bpm or (float(_get(tempos[0], "Bpm") or 0) if tempos else 0)
        if bpm:
            kept = ([_tempo(grid[0], bpm, tempos[0] if tempos else "")]
                    + [c for c in kept if not c.startswith("<TEMPO")])
    kept += [_mark(p) for p in _hot_cues(track)]
    if track.playcount is not None:
        open_tag = _set(open_tag, "PlayCount", track.playcount)
    return _join(open_tag, kept)


def render_new_track(track: Track, track_id: str) -> str:
    """A new collection track, in Rekordbox's attribute order. Rekordbox fills
    in the rest (size, sample rate, analysis) when it imports and analyses it."""
    parts = [f"TrackID={_attr(track_id)}", f"Name={_attr(track.title)}",
             f"Artist={_attr(track.artist)}", f"Album={_attr(track.album)}",
             f"Genre={_attr(track.genre)}"]
    if track.length:
        parts.append(f'TotalTime="{track.length:.0f}"')
    if track.bpm:
        parts.append(f'AverageBpm="{track.bpm:.2f}"')
    parts.append(f'DateAdded="{date.today().isoformat()}"')
    if track.bitrate:
        parts.append(f'BitRate="{int(track.bitrate)}"')
    parts.append(f"Comments={_attr(track.comment)}")
    if track.playcount is not None:
        parts.append(f'PlayCount="{track.playcount}"')
    parts.append(f"Location={_attr(path_to_location(track.path))}")
    # The canonical value when the source supplied one; parsing the stored text
    # is the fallback for a track that has never been analysed.
    key_value = (track.key_value if track.key_value is not None
                 else keys.parse_vdj_key(track.key_musical))
    if tonality(key_value):
        parts.append(f"Tonality={_attr(tonality(key_value))}")
    open_tag = "<TRACK " + " ".join(parts) + ">"
    return track_with_cues(open_tag + "</TRACK>", track)


def _append_tracks(content: str, blocks: list[str]) -> str:
    """Add new track blocks at the end of the collection and bump ``Entries``
    once by how many landed."""
    if not blocks:
        return content
    start, end = _collection_span(content)
    if start < 0:
        raise ValueError("rekordbox.xml has no <COLLECTION>")
    if start == end and content[start - 2:start] == "/>":
        # <COLLECTION Entries="0"/>: open it up first.
        tag_start = content.rfind("<COLLECTION", 0, start)
        tag = content[tag_start:start][:-2].rstrip() + ">"
        content = content[:tag_start] + tag + "\n\t</COLLECTION>" + content[start:]
        start, end = _collection_span(content)
    head = content[:end].rstrip(" \t")
    text = "".join(f"\t\t{b}\n" for b in blocks)
    if not head.endswith("\n"):
        text = "\n" + text
    content = head + text + "\t" + content[end:]

    def bump(m):
        return m.group(1) + str(int(m.group(2) or 0) + len(blocks)) + m.group(3)
    return _COLLECTION_OPEN_RE.sub(bump, content, count=1)


def _next_id(content: str) -> int:
    ids = [int(tid) for *_rest, tid in _tracks(content) if tid.isdigit()]
    return max(ids, default=0) + 1


def sync_tracks(content: str, tracks) -> tuple[str, dict]:
    """Write many tracks' cue sets in one walk. Returns
    ``(new_content, {key: created})``; tracks the collection lacks are appended
    with fresh TrackIDs."""
    by_key = {t.key: t for t in tracks}
    edits, seen = [], set()
    for start, end, key, _tid in _tracks(content):
        track = by_key.get(key)
        if track is not None:
            seen.add(key)
            edits.append((start, end, track_with_cues(content[start:end], track)))
    content = _splice(content, edits)
    missing = [t for k, t in by_key.items() if k not in seen]
    first = _next_id(content)
    content = _append_tracks(content, [render_new_track(t, str(first + i))
                                       for i, t in enumerate(missing)])
    return content, {k: k not in seen for k in by_key}


def ensure_tracks(content: str, tracks) -> tuple[str, dict]:
    """Make sure every track has a collection entry, without touching the ones
    that do. Returns ``(new_content, {key: TrackID})`` -- what a playlist names
    its members by."""
    ids = track_ids(content)
    missing = list({t.key: t for t in tracks if t.key not in ids}.values())
    first = _next_id(content)
    for i, t in enumerate(missing):
        ids[t.key] = str(first + i)
    content = _append_tracks(content, [render_new_track(t, ids[t.key])
                                       for t in missing])
    return content, ids


#: Shared field name -> Rekordbox attribute. ``key`` is the spelled Tonality
#: the adapter's ``key_fields`` supplies.
_FIELD_ATTRS = {"title": "Name", "artist": "Artist", "album": "Album",
                "genre": "Genre", "comment": "Comments", "key": "Tonality"}


def set_tracks_metadata(content: str, by_key: dict) -> tuple[str, dict]:
    """Set text attributes on many tracks in one walk. ``{key: changed}``,
    False for a key no track matched."""
    wanted = {k: {_FIELD_ATTRS[f]: v for f, v in fields.items() if f in _FIELD_ATTRS}
              for k, fields in by_key.items()}
    edits, done = [], {k: False for k in by_key}
    for start, end, key, _tid in _tracks(content):
        attrs = wanted.get(key)
        if not attrs:
            continue
        block = content[start:end]
        open_tag = _OPEN_TAG_RE.match(block).group(0)
        new_tag = open_tag
        for name, value in attrs.items():
            new_tag = _set(new_tag, name, matching.normalize_text(value))
        edits.append((start, end, new_tag + block[len(open_tag):]))
        done[key] = True
    return _splice(content, edits), done


def remove_track(content: str, key: str) -> tuple[str, bool]:
    """Delete one collection track (and its line) and decrement ``Entries``.
    Playlist references are left to the caller, as on Traktor."""
    for start, end, k, _tid in _tracks(content):
        if k != key:
            continue
        line_start = content.rfind("\n", 0, start) + 1
        if not content[line_start:start].strip():
            start = line_start
        if content[end:end + 1] == "\n":
            end += 1
        content = content[:start] + content[end:]

        def dec(m):
            return m.group(1) + str(max(0, int(m.group(2) or 0) - 1)) + m.group(3)
        return _COLLECTION_OPEN_RE.sub(dec, content, count=1), True
    return content, False


def find_tracks(content: str, keys) -> dict:
    """``{key: block text}`` for these keys, for reading back after a write."""
    wanted = set(keys)
    return {key: content[s:e] for s, e, key, _tid in _tracks(content)
            if key in wanted}


def repath_tracks(content: str, by_key: dict) -> tuple[str, dict]:
    """Point tracks at new files: the collection ``Location`` and every
    ``KeyType="1"`` playlist reference naming one. ``TrackID`` references need
    nothing -- they follow the entry. ``{old key: done}``."""
    edits, done = [], {k: False for k in by_key}
    for start, end, key, _tid in _tracks(content):
        if key in by_key:
            block = content[start:end]
            open_tag = _OPEN_TAG_RE.match(block).group(0)
            new_tag = _set(open_tag, "Location", path_to_location(by_key[key]))
            edits.append((start, end, new_tag + block[len(open_tag):]))
            done[key] = True
    content = _splice(content, edits)
    edits = []
    for node in _all_nodes(scan_nodes(content)):
        if node.kind != "1" or node.key_type != "1":
            continue
        for start, end, ref_key in _refs(content, node):
            path = location_to_path(ref_key)
            target = by_key.get(matching.path_key(path)) if path else None
            if target:
                edits.append((start, end,
                              f"<TRACK Key={_attr(path_to_location(target))}/>"))
                done[matching.path_key(path)] = True
    return _splice(content, edits), done


# ============================== the playlist tree ===========================
_NODE_TAG_RE = re.compile(r"<NODE\b[^>]*?/?>|</NODE>")
_REF_RE = re.compile(r"<TRACK\b[^>]*?/>")


@dataclass
class Span:
    """One ``<NODE>`` and where it sits in the text."""

    name: str
    kind: str                 # "0" folder, "1" playlist
    key_type: str
    start: int                # the "<" of the opening tag
    open_end: int             # just past the opening tag
    close_start: int          # the "<" of "</NODE>"; == open_end when self-closing
    end: int                  # just past the element
    depth: int
    children: list = field(default_factory=list)

    @property
    def self_closing(self) -> bool:
        return self.close_start == self.open_end == self.end


def scan_nodes(content: str) -> Optional[Span]:
    """The ``ROOT`` node span with every ``<NODE>`` below it, or None."""
    start = content.find("<PLAYLISTS")
    if start < 0:
        return None
    end = content.find("</PLAYLISTS>", start)
    stack: list[Span] = []
    root = None
    for m in _NODE_TAG_RE.finditer(content, start, end if end >= 0 else len(content)):
        tag = m.group(0)
        if tag.startswith("</"):
            node = stack.pop()
            node.close_start, node.end = m.start(), m.end()
            continue
        node = Span(name=_get(tag, "Name") or "", kind=_get(tag, "Type") or "",
                    key_type=_get(tag, "KeyType") or "0", start=m.start(),
                    open_end=m.end(), close_start=m.end(), end=m.end(),
                    depth=len(stack))
        if stack:
            stack[-1].children.append(node)
        else:
            root = node
        if not tag.endswith("/>"):
            stack.append(node)
    return root


def _all_nodes(root: Optional[Span]):
    if root is None:
        return
    yield root
    for child in root.children:
        yield from _all_nodes(child)


def find_node(root: Optional[Span], node_path) -> Optional[Span]:
    """The span at ``node_path`` below ROOT, matched like ``path_ci``."""
    node = root
    for name in path_ci(node_path):
        if node is None:
            return None
        node = next((c for c in node.children if c.name.lower() == name), None)
    return node


def _refs(content: str, node: Span):
    """``(start, end, Key)`` for each member reference of a playlist span."""
    for m in _REF_RE.finditer(content, node.open_end, node.close_start):
        yield m.start(), m.end(), _get(m.group(0), "Key") or ""


def _indent(depth: int) -> str:
    # PLAYLISTS sits at one tab and ROOT at two, so a node at depth d is at d+2.
    return "\t" * (depth + 2)


def _render_node(name: str, kind: str, depth: int, refs=()) -> str:
    pad = _indent(depth)
    if kind == "0":
        return f'{pad}<NODE Name={_attr(name)} Type="0" Count="0"></NODE>'
    lines = [f'{pad}<NODE Name={_attr(name)} Type="1" KeyType="0" '
             f'Entries="{len(refs)}">']
    lines += [f"{pad}\t<TRACK Key={_attr(r)}/>" for r in refs]
    lines.append(f"{pad}</NODE>")
    return "\n".join(lines)


def _with_count(content: str, node: Span, delta: int) -> str:
    """``content`` with ``node``'s ``Count`` moved by ``delta``. Callers apply
    it to the earliest edit last, so positions before it stay valid."""
    tag = content[node.start:node.open_end]
    count = int(_get(tag, "Count") or 0) + delta
    return content[:node.start] + _set(tag, "Count", max(count, 0)) + content[node.open_end:]


def _insert_child(content: str, parent: Span, text: str) -> str:
    """Add ``text`` (already indented) as ``parent``'s last child and count it."""
    if parent.self_closing:
        tag = content[parent.start:parent.open_end][:-2].rstrip() + ">"
        body = f"{tag}\n{text}\n{_indent(parent.depth)}</NODE>"
        content = content[:parent.start] + body + content[parent.end:]
    else:
        at = parent.close_start
        before = content[:at].rstrip(" \t")
        if not before.endswith("\n"):
            before += "\n"
        content = before + text + "\n" + _indent(parent.depth) + content[at:]
    root = scan_nodes(content)
    return _with_count(content, _find_by_start(root, parent.start), +1)


def _find_by_start(root: Span, start: int) -> Span:
    return next(n for n in _all_nodes(root) if n.start == start)


def ensure_folders(content: str, folder_path) -> tuple[str, bool]:
    """Create each missing folder along ``folder_path``."""
    changed = False
    for depth in range(1, len(folder_path) + 1):
        root = scan_nodes(content)
        if root is None:
            raise ValueError("rekordbox.xml has no <PLAYLISTS> ROOT node")
        if find_node(root, folder_path[:depth]) is not None:
            continue
        parent = find_node(root, folder_path[:depth - 1])
        content = _insert_child(content, parent,
                                _render_node(folder_path[depth - 1], "0", depth))
        changed = True
    return content, changed


def create_playlist(content: str, node_path, track_ids_in_order) -> str:
    content, _ = ensure_folders(content, tuple(node_path)[:-1])
    root = scan_nodes(content)
    parent = find_node(root, tuple(node_path)[:-1])
    return _insert_child(content, parent, _render_node(
        node_path[-1], "1", len(node_path), list(track_ids_in_order)))


def set_playlist(content: str, node_path, refs_by_track) -> tuple[str, bool]:
    """Rewrite a playlist's members. ``refs_by_track`` is ``[(TrackID, path)]``
    in order; the playlist's own ``KeyType`` decides which one is written."""
    node = find_node(scan_nodes(content), node_path)
    if node is None or node.kind != "1":
        return content, False
    by_location = node.key_type == "1"
    pad = _indent(node.depth)
    refs = [path_to_location(path) if by_location else tid
            for tid, path in refs_by_track]
    tag = _set(content[node.start:node.open_end], "Entries", len(refs))
    if tag.endswith("/>"):
        tag = tag[:-2].rstrip() + ">"
    inner = "".join(f"\n{pad}\t<TRACK Key={_attr(r)}/>" for r in refs)
    body = f"{tag}{inner}\n{pad}</NODE>"
    return content[:node.start] + body + content[node.end:], True


def rename_node(content: str, node_path, name: str) -> tuple[str, bool]:
    node = find_node(scan_nodes(content), node_path)
    if node is None:
        return content, False
    tag = _set(content[node.start:node.open_end], "Name", name)
    return content[:node.start] + tag + content[node.open_end:], True


def _cut(content: str, node: Span) -> tuple[str, str]:
    """Remove ``node``'s lines; return (content, the removed element text)."""
    line_start = content.rfind("\n", 0, node.start) + 1
    start = line_start if not content[line_start:node.start].strip() else node.start
    end = node.end + (1 if content[node.end:node.end + 1] == "\n" else 0)
    return content[:start] + content[end:], content[node.start:node.end]


def remove_node(content: str, node_path) -> tuple[str, bool]:
    root = scan_nodes(content)
    node = find_node(root, node_path)
    parent = find_node(root, tuple(node_path)[:-1])
    if node is None or parent is None or node is root:
        return content, False
    content, _ = _cut(content, node)
    # The parent opens before the cut, so its position is unchanged.
    return _with_count(content, _find_by_start(scan_nodes(content), parent.start), -1), True


def move_node(content: str, node_path, new_parent_path) -> tuple[str, bool]:
    """Move a node (and everything in it) under ``new_parent_path``, which
    must exist. Re-indented to its new depth."""
    if path_ci(new_parent_path)[:len(node_path)] == path_ci(node_path):
        return content, False                 # into itself
    root = scan_nodes(content)
    node = find_node(root, node_path)
    old_parent = find_node(root, tuple(node_path)[:-1])
    if node is None or old_parent is None or find_node(root, new_parent_path) is None:
        return content, False
    content, element = _cut(content, node)
    content = _with_count(content, _find_by_start(scan_nodes(content),
                                                  old_parent.start), -1)
    new_parent = find_node(scan_nodes(content), new_parent_path)
    shift = new_parent.depth + 1 - node.depth
    lines = element.split("\n")
    if shift >= 0:
        lines = ["\t" * shift + line if i else line for i, line in enumerate(lines)]
    else:
        lines = [line[-shift:] if i and line.startswith("\t" * -shift) else line
                 for i, line in enumerate(lines)]
    text = _indent(new_parent.depth + 1) + "\n".join(lines)
    return _insert_child(content, new_parent, text), True
