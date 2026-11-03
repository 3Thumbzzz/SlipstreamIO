"""Write Traktor-sourced cue/track/playlist data into VirtualDJ.

All edits to ``database.xml`` are **text-surgical** (regex-scoped to one
``<Song>`` block at a time), mirroring the safety property from the original
``nml_to_xml_sync.py``: everything outside the touched block(s) survives
byte-for-byte. Playlist writes touch ``.vdjfolder`` files under ``Folders/``.
"""

from __future__ import annotations

import os
import re
from xml.sax.saxutils import escape

from ..model import keys, matching
from ..model.cues import POI_LINE_RE, format_poi_line, is_owned_poi, set_playcount
from ..model.library import Track


def _qattr(value: str) -> str:
    r"""Double-quoted XML attribute, escaping &, <, >, " and ' (VirtualDJ's own
    files escape apostrophes as &apos; even inside double quotes).

    Line breaks and tabs are escaped as character references for the reason
    ``saxutils.quoteattr`` does it on the Traktor side: a *literal* newline in
    an attribute is legal XML but the parser normalises it to a space, so a
    title carrying one would come back silently altered and never compare equal
    to its source again.
    """
    v = (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    v = v.replace('"', "&quot;").replace("'", "&apos;")
    v = v.replace("\r", "&#13;").replace("\n", "&#10;").replace("\t", "&#9;")
    return f'"{v}"'


def _qtext(value: str) -> str:
    r"""Escaped element text, with no bare carriage return left in it.

    ``atomic_write_text`` writes through a text-mode handle, so every ``\n``
    becomes ``\r\n`` on the way to disk. A ``\r`` that reached here raw would
    ride in front of that and land as ``\r\r\n``; the next whole-file rewrite
    reads it back as two newlines and stores ``\r\n\r\n``, so the comment grows
    a blank line every second sync. Normalising to ``\n`` is what makes the
    round trip exact -- 511 comments in the reported library had already drifted.
    """
    return escape(matching.normalize_text(value))


_SONG_BLOCK_RE = re.compile(
    r'(<Song\b[^>]*FilePath="([^"]*)"[^>]*>)(.*?)(</Song>)', re.DOTALL
)
_DB_CLOSE_RE = re.compile(r"</VirtualDJ_Database>\s*$")


def sync_track_into_xml(xml_content: str, track: Track) -> tuple[str, bool]:
    """Update the matched <Song> block's <Poi> lines + PlayCount from
    ``track`` (already in VDJ-timeline seconds — caller applies correction).
    If no Song block matches, a new one is appended before the closing tag.

    Returns (new_content, created) where created=True if a new block was added.

    The one-track form of :func:`sync_tracks_into_xml`.
    """
    new_content, created = sync_tracks_into_xml(xml_content, [track])
    return new_content, created[track.key]


def _song_with_pois(open_tag: str, body: str, close_tag: str,
                    track: Track) -> str:
    """One <Song> block rewritten with ``track``'s cue set (+ PlayCount).

    Strips ONLY the cue/beatgrid points we own; automix/remix points are
    VirtualDJ-specific and are carried through untouched.
    """
    new_body = POI_LINE_RE.sub(
        lambda pm: "" if is_owned_poi(pm.group(0)) else pm.group(0), body)
    if not new_body.endswith("\n"):
        new_body += "\n"
    lines = [format_poi_line(p) for p in track.pois]
    if lines:
        new_body += "\n".join(lines) + "\n"
    if track.playcount is not None:
        new_body = set_playcount(new_body, str(track.playcount))
    return open_tag + new_body + close_tag


#: Above this many tracks, locating each block by name costs more than one walk
#: of the whole document. Measured on a real 44 MB / 82,366-song database.xml:
#: 12.6 ms per targeted lookup against 0.61 s for the walk, so the crossover sits
#: near 48. Kept comfortably below it -- the win this exists for is the
#: single-track sync, where the walk was 0.61 s of a 5 s run.
_FAST_LOCATE_MAX = 32


def locate_songs(content: str, by_key: dict):
    """``{key: match}`` when **every** song was located, else ``None`` meaning
    "fall back to walking the document". ``by_key`` maps a key to its path.

    Rebuilds the ``FilePath=`` attribute *this module would itself write* and
    looks for it directly, which turns finding one ``<Song>`` among 82,366 from a
    0.61 s regex walk into a 12.6 ms substring search. On a real database it
    located 3,000 of 3,000 sampled tracks.

    **It only changes how a block is found, never how it is edited.** The hit is
    re-matched with :data:`_SONG_BLOCK_RE` and re-keyed with exactly the
    comparison the walk uses, so a block that gets here is one the walk would
    have chosen.

    **A miss does not prove the track is absent**, which is why it gives up
    rather than reporting one. The same path has more than one legal encoding --
    a file storing ``&#38;`` where this module writes ``&amp;`` is the obvious
    case -- and the walk decodes before comparing, so it would find what this
    search cannot. Treating a miss as "not in the database" would append a
    second ``<Song>`` for a track the collection already holds.
    """
    found: dict = {}
    for key, path in by_key.items():
        needle = 'FilePath=' + _qattr(path)
        pos = content.find(needle)
        if pos < 0:
            return None                    # cannot prove absence -- let the walk
        if content.find(needle, pos + 1) >= 0:
            return None                    # two songs claim this file
        start = content.rfind("<Song", 0, pos)
        if start < 0 or ">" in content[start:pos]:
            # Matched inside a body rather than an opening tag -- `FilePath="…"`
            # is ordinary text in a <Comment> and needs no escaping. Overlaps
            # with the key check below, which catches the same case by a
            # different route; kept because it states the intent at the point
            # the assumption is made.
            return None
        m = _SONG_BLOCK_RE.match(content, start)
        if m is None:
            return None
        if matching.path_key(matching.decode_entities(m.group(2))) != key:
            return None                    # the walk's own check, unchanged
        found[key] = m
    return found


def _splice(content: str, found: dict, rewrite) -> str:
    """Replace just the located blocks, in document order.

    ``rewrite(match, key) -> str``. Equivalent to a ``sub`` that happened to
    match only these blocks, which is what makes the output byte-identical to
    the walk's.
    """
    out, last = [], 0
    for key, m in sorted(found.items(), key=lambda kv: kv[1].start()):
        out.append(content[last:m.start()])
        out.append(rewrite(m, key))
        last = m.end()
    out.append(content[last:])
    return "".join(out)


def sync_tracks_into_xml(xml_content: str, tracks) -> tuple[str, dict]:
    """Write **many** tracks' cue sets in a single pass of the database.

    Returns ``(new_content, {key: created})``. One pass rather than one per
    track: finding a single ``<Song>`` means visiting all 82,366 of them, so N
    writes cost N x that. Each track only ever touches its own block, so the
    result is byte-identical to writing them one at a time.

    A small batch skips the walk entirely -- see :func:`locate_songs`.
    """
    by_key = {t.key: t for t in tracks}
    seen: set[str] = set()

    found = (locate_songs(xml_content, {k: t.path for k, t in by_key.items()})
             if 0 < len(by_key) <= _FAST_LOCATE_MAX else None)

    if found is None:
        def repl(m):
            open_tag, filepath, body, close_tag = m.groups()
            track = by_key.get(
                matching.path_key(matching.decode_entities(filepath)))
            if track is None:
                return m.group(0)
            seen.add(track.key)
            return _song_with_pois(open_tag, body, close_tag, track)

        new_content = _SONG_BLOCK_RE.sub(repl, xml_content)
    else:
        seen = set(found)
        new_content = _splice(
            xml_content, found,
            lambda m, k: _song_with_pois(m.group(1), m.group(3), m.group(4),
                                         by_key[k]))

    missing = [t for k, t in by_key.items() if k not in seen]
    if missing:
        blocks = "".join(_render_new_song_block(t) for t in missing)
        new_content = _DB_CLOSE_RE.sub(
            lambda _m: blocks + "</VirtualDJ_Database>", new_content)
    return new_content, {k: k not in seen for k in by_key}


def remove_song_from_xml(xml_content: str, key: str) -> tuple[str, bool]:
    """Delete the ``<Song>`` block whose ``FilePath`` resolves to ``key``. The
    database has no total-count attribute to adjust. Returns
    ``(new_content, removed)``."""
    state = {"hit": False}

    def repl(m):
        _open, filepath, _body, _close = m.groups()
        if state["hit"] or matching.path_key(matching.decode_entities(filepath)) != key:
            return m.group(0)
        state["hit"] = True
        return ""

    new_content = _SONG_BLOCK_RE.sub(repl, xml_content)
    return (new_content, True) if state["hit"] else (xml_content, False)


def repath_songs_in_xml(xml_content: str, by_key: dict) -> tuple[str, dict]:
    """Point ``<Song>`` blocks at new files, in one walk of the database.

    ``by_key`` maps a current track key to its new absolute path. Only the
    ``FilePath`` attribute moves; cues, automix points, tags and anything else
    VirtualDJ keeps in the block survive byte-for-byte.

    Playlist membership lives in the ``Folders/`` tree, not here, so a caller
    must run :func:`repath_songs_in_vdjfolder` over that too -- see
    ``VdjAdapter.repath_tracks_job``.
    """
    if not by_key:
        return xml_content, {}
    seen: set[str] = set()

    def repl(m):
        open_tag, filepath, body, close_tag = m.groups()
        key = matching.path_key(matching.decode_entities(filepath))
        new_path = by_key.get(key)
        if new_path is None:
            return m.group(0)
        seen.add(key)
        return (open_tag.replace(f'FilePath="{filepath}"',
                                 f'FilePath={_qattr(new_path)}', 1)
                + body + close_tag)

    return _SONG_BLOCK_RE.sub(repl, xml_content), {k: k in seen for k in by_key}


#: ``path=`` on a ``.vdjfolder`` row, with the value's quotes left outside the
#: captured groups so ``_qattr`` (which supplies its own) can replace it whole.
_FOLDER_SONG_PATH_RE = re.compile(r'(<song\b[^>]*?\bpath=)"([^"]*)"',
                                  re.IGNORECASE)


def repath_songs_in_vdjfolder(content: str, by_key: dict,
                              moved: set | None = None) -> tuple[str, bool]:
    """Rewrite ``path=`` on every ``<song>`` row this playlist holds for a moved
    track. Returns ``(new_content, changed)``.

    A VirtualDJ playlist names its tracks by path, so a track whose file was
    renamed drops out of every playlist unless each ``.vdjfolder`` is rewritten
    as well. Only the attribute moves -- ``idx``, ``bpm`` and the cached tags on
    the row are left exactly as they were.

    ``moved``, when given, collects the keys that were rewritten, so a caller
    can report a playlist-only reference as relinked -- see
    ``VdjAdapter.repath_tracks_job``.
    """
    if not by_key:
        return content, False
    changed = False

    def repl(m):
        nonlocal changed
        key = matching.path_key(matching.decode_entities(m.group(2)))
        new_path = by_key.get(key)
        if new_path is None:
            return m.group(0)
        changed = True
        if moved is not None:
            moved.add(key)
        return m.group(1) + _qattr(new_path)

    return _FOLDER_SONG_PATH_RE.sub(repl, content), changed


# ========================= editable text metadata ========================
#: Which ``<Tags>`` attribute each editable field lives in. ``comment`` is not
#: here: VirtualDJ keeps it as a ``<Comment>`` *element*, not an attribute.
#: BPM and Key are deliberately absent -- they are analysis output, and changing
#: the BPM without moving the beatgrid desynchronises the cues.
_TAG_ATTRS = {"title": "Title", "artist": "Author",
              "album": "Album", "genre": "Genre"}

#: Plus the musical key, which a **sync** may write but a user may not edit, so
#: it stays out of the map the metadata dialog is built from. VirtualDJ leaves
#: Key empty on 60,120 of the 81,107 songs in the reported library; carrying
#: Traktor's analysed key into those is the whole point of writing it here.
_WRITABLE_TAG_ATTRS = {**_TAG_ATTRS, "key": "Key"}


def _set_tag_attr(tag: str, name: str, value: str) -> str:
    """Set one attribute on the ``<Tags …/>`` element, adding it if absent, and
    leaving every other attribute VirtualDJ keeps there untouched."""
    pat = re.compile(rf'(\b{name}=)("[^"]*")')
    if pat.search(tag):
        return pat.sub(lambda m: m.group(1) + _qattr(value), tag, count=1)
    return re.sub(r"(\s*/?>)$", " " + name + "=" + _qattr(value) + r"\1", tag, count=1)


def set_song_metadata(xml_content: str, key: str, fields: dict) -> tuple[str, bool]:
    """Set text metadata on the ``<Song>`` matching ``key``.

    ``fields`` maps any of title/artist/album/genre/comment to its new value; a
    field **absent from the dict is left alone**, which is what lets a
    multi-track edit leave every ``<various>`` field at each track's own value.

    Returns ``(new_content, changed)``. The one-song form of
    :func:`set_songs_metadata`.
    """
    new_content, changed = set_songs_metadata(xml_content, {key: fields})
    return new_content, changed[key]


def set_songs_playcount(xml_content: str, by_key: dict) -> tuple[str, dict]:
    """Set ``PlayCount`` on **many** ``<Song>`` blocks in one pass, and nothing
    else. Returns ``(new_content, {key: changed})``.

    Deliberately not done by re-rendering the song's Pois, which is how a cue
    sync carries a play count: ``format_poi_line`` floors every position, and
    VirtualDJ's own negative grid anchors would not survive it. A song with no
    ``<Infos>`` has nowhere to keep a count and is reported unchanged.
    """
    changed = {k: False for k in by_key}

    def repl(m):
        open_tag, filepath, body, close_tag = m.groups()
        key = matching.path_key(matching.decode_entities(filepath))
        if key not in by_key:
            return m.group(0)
        new_body = set_playcount(body, str(int(by_key[key])))
        changed[key] = new_body != body
        return open_tag + new_body + close_tag

    return _SONG_BLOCK_RE.sub(repl, xml_content), changed


def set_songs_metadata(xml_content: str, by_key: dict,
                       paths: dict | None = None) -> tuple[str, dict]:
    """Set text metadata on **many** ``<Song>`` blocks in one pass.

    ``by_key`` maps a track key to its own ``fields`` dict. Returns
    ``(new_content, {key: changed})``, False for a key that matched nothing.

    ``paths`` maps a key to the file it is believed to live at. **Optional, and
    only ever an accelerator**: with it a small batch is located rather than
    walked (:func:`locate_songs`); without it, or when the locate will not vouch
    for what it found, the walk below runs exactly as before.
    """
    wanted = {}
    for song_key, fields in by_key.items():
        picked = {f: v for f, v in fields.items()
                  if f in _WRITABLE_TAG_ATTRS or f == "comment"}
        if picked:
            wanted[song_key] = picked
    if not wanted:
        return xml_content, {k: False for k in by_key}

    found = None
    if paths and len(wanted) <= _FAST_LOCATE_MAX:
        locatable = {k: paths[k] for k in wanted if paths.get(k)}
        if len(locatable) == len(wanted):
            found = locate_songs(xml_content, locatable)

    if found is None:
        seen: set[str] = set()

        def repl(m):
            open_tag, filepath, body, close_tag = m.groups()
            song_key = matching.path_key(matching.decode_entities(filepath))
            wanted_here = wanted.get(song_key)
            if wanted_here is None:
                return m.group(0)
            seen.add(song_key)
            return _song_with_metadata(open_tag, body, close_tag, wanted_here)

        new_content = _SONG_BLOCK_RE.sub(repl, xml_content)
    else:
        seen = set(found)
        new_content = _splice(
            xml_content, found,
            lambda m, k: _song_with_metadata(m.group(1), m.group(3),
                                             m.group(4), wanted[k]))

    changed = {k: k in seen for k in by_key}
    return (new_content if seen else xml_content), changed


def _song_with_metadata(open_tag: str, body: str, close_tag: str,
                        wanted: dict) -> str:
    """One <Song> block with ``wanted``'s fields set and nothing else touched."""
    tags = re.search(r"<Tags\b[^>]*?/?>", body)
    attrs = {f: v for f, v in wanted.items() if f in _WRITABLE_TAG_ATTRS}
    if attrs:
        if tags:
            tag_text = tags.group(0)
            for field, value in attrs.items():
                tag_text = _set_tag_attr(tag_text,
                                         _WRITABLE_TAG_ATTRS[field], value)
            body = body[:tags.start()] + tag_text + body[tags.end():]
        else:
            rendered = " ".join(f"{_WRITABLE_TAG_ATTRS[f]}={_qattr(v)}"
                                for f, v in attrs.items())
            body = f"  <Tags {rendered} />\n" + body

    if "comment" in wanted:
        text = _qtext(wanted["comment"])
        if re.search(r"<Comment\b[^>]*>.*?</Comment>", body, re.DOTALL):
            body = re.sub(r"(<Comment\b[^>]*>).*?(</Comment>)",
                          lambda cm: cm.group(1) + text + cm.group(2),
                          body, count=1, flags=re.DOTALL)
        elif re.search(r"<Comment\b[^>]*/>", body):
            body = re.sub(r"<Comment\b[^>]*/>",
                          f"<Comment>{text}</Comment>", body, count=1)
        else:
            body = body + f"  <Comment>{text}</Comment>\n"
    return open_tag + body + close_tag


def _vdj_key(track: Track) -> str:
    """This track's key in VirtualDJ's own notation.

    Rendered from the canonical value rather than copied from the source
    program's text: Traktor stores Open Key ("8d"), and passing that through put
    a value VirtualDJ cannot read into its Key field. Falls back to the stored
    text, which is already VirtualDJ notation for a track that came from
    VirtualDJ and is all there is for one with no analysed key.
    """
    return keys.musical_key(track.key_value) or track.key_musical


def _render_new_song_block(track: Track) -> str:
    lines = [format_poi_line(p) for p in track.pois]
    poi_block = ("\n" + "\n".join(lines) + "\n") if lines else "\n"
    playcount_attr = f' PlayCount="{track.playcount}"' if track.playcount is not None else ""
    bpm_attr = f' Bpm="{60.0/track.bpm:.6f}"' if track.bpm else ""
    return (
        f' <Song FilePath={_qattr(track.path)} FileSize="0">\n'
        # Album is written here for the same reason the rest are: it is one of
        # the fields status.py COMPARED_FIELDS checks, so leaving it out made
        # every newly created entry differ from its source the instant it was
        # written -- yellow on arrival, with nothing the user could do about it.
        f'  <Tags Author={_qattr(track.artist)} Title={_qattr(track.title)} '
        f'Album={_qattr(track.album)} '
        f'Genre={_qattr(track.genre)} Key={_qattr(_vdj_key(track))}{bpm_attr} />\n'
        f'  <Infos SongLength="{track.length or 0:.6f}" '
        f'Bitrate="{track.bitrate or 0}"{playcount_attr} />\n'
        f'  <Comment>{_qtext(track.comment)}</Comment>\n'
        f'{poi_block}'
        f' </Song>\n'
    )


# ============================ .vdjfolder writing =========================
_FOLDER_ROW_RE = re.compile(r'<song\b[^>]*?/>', re.DOTALL)
_ROW_PATH_RE = re.compile(r'\bpath="([^"]*)"')
_ROW_IDX_RE = re.compile(r'\s*\bidx="[^"]*"')


def _existing_rows(existing: str | None) -> dict:
    """``{track key: [row text, …]}`` for the ``<song …/>`` rows of a playlist
    file's current text, each with its ``idx`` removed, in file order."""
    rows: dict = {}
    for m in _FOLDER_ROW_RE.finditer(existing or ""):
        path = _ROW_PATH_RE.search(m.group(0))
        if path is None:
            continue
        key = matching.path_key(matching.decode_entities(path.group(1)))
        rows.setdefault(key, []).append(_ROW_IDX_RE.sub("", m.group(0)))
    return rows


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def render_vdjfolder(tracks: list[Track], no_duplicates: bool = False,
                     existing: str | None = None) -> str:
    """The full text of a .vdjfolder holding ``tracks`` in order. Split out from
    :func:`write_vdjfolder` so a caller can write it atomically off-thread.

    ``no_duplicates`` controls the ``<VirtualFolder noDuplicates=…>`` flag that
    tells VirtualDJ whether the same track may appear twice in this playlist.

    **A row already in the playlist is kept as VirtualDJ wrote it** -- only its
    ``idx`` is renumbered -- when ``existing``, the file's current text, is
    given. A row is VirtualDJ's cached view of the song (size, length, BPM,
    key, remix…), and rebuilding every row from the ``Track`` handed in meant a
    Traktor -> VirtualDJ sync rewrote all of them with **Traktor's** key, BPM
    and rounded length, and ``size="0"``: 2,877 of 2,882 rows in one reported
    playlist. Only rows new to the playlist are built, with the file's real
    size.
    """
    flag = "yes" if no_duplicates else "no"
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<VirtualFolder noDuplicates="{flag}">']
    kept = _existing_rows(existing)
    for idx, t in enumerate(tracks):
        reuse = kept.get(matching.path_key(t.path))
        if reuse:
            row = reuse.pop(0)
            lines.append("\t" + row[:-2].rstrip() + f' idx="{idx}" />')
            continue
        attrs = [
            f'path={_qattr(t.path)}',
            f'size="{_file_size(t.path)}"',
            f'songlength="{t.length or 0:.1f}"',
        ]
        if t.bpm:
            attrs.append(f'bpm="{t.bpm:.3f}"')
        vdj_key = _vdj_key(t)
        if vdj_key:
            attrs.append(f'key={_qattr(vdj_key)}')
        if t.artist:
            attrs.append(f'artist={_qattr(t.artist)}')
        if t.title:
            attrs.append(f'title={_qattr(t.title)}')
        attrs.append(f'idx="{idx}"')
        lines.append(f'\t<song {" ".join(attrs)} />')
    lines.append("</VirtualFolder>")
    return "\n".join(lines) + "\n"


def write_vdjfolder(path: str, tracks: list[Track], no_duplicates: bool = False) -> None:
    """(Re)write a .vdjfolder file with the given tracks, in order."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_vdjfolder(tracks, no_duplicates))


def subfolder_dir_for(folders_root: str, node_path: tuple[str, ...],
                      create: bool = True) -> str:
    """The ``.subfolders`` directory a *folder* node lives in -- the whole chain,
    including the last segment. The one definition of how a VDJ folder path is
    spelled; ``ensure_subfolder_chain`` is this minus the playlist name."""
    d = folders_root
    for part in node_path:
        d = os.path.join(d, part + ".subfolders")
        if create:
            os.makedirs(d, exist_ok=True)
    return d


def ensure_subfolder_chain(folders_root: str, node_path: tuple[str, ...]) -> str:
    """Ensure ``.subfolders`` directories exist for every folder ancestor in
    node_path (all but the last element, which is the playlist name itself).
    Returns the directory the playlist's .vdjfolder file should live in."""
    return subfolder_dir_for(folders_root, node_path[:-1])


def vdjfolder_path_for(folders_root: str, node_path: tuple[str, ...]) -> str:
    directory = ensure_subfolder_chain(folders_root, node_path)
    return os.path.join(directory, node_path[-1] + ".vdjfolder")
