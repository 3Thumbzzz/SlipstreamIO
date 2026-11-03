"""Write VirtualDJ-sourced cue/track/playlist data into Traktor's NML.

Like ``vdj_write.py``, edits are **text-surgical**: an ``<ENTRY>`` block is
located by regex and only its ``<CUE_V2>`` elements + ``INFO PLAYCOUNT`` are
rewritten; unrelated content is untouched. New entries are appended inside
``<COLLECTION>`` (bumping ``ENTRIES``); new playlist nodes are appended inside
the target ``<SUBNODES>`` (bumping its ``COUNT``).
"""

from __future__ import annotations

import re
from datetime import datetime
from xml.sax.saxutils import escape, quoteattr

from ..model import keys, matching
from ..model.cues import CUE_V2_RE, format_cue_v2, is_owned_cue_v2
from ..model.library import Track, path_ci

_ENTRY_RE = re.compile(r"(<ENTRY\b[^>]*>)(.*?)(</ENTRY>)", re.DOTALL | re.IGNORECASE)
_COLLECTION_OPEN_RE = re.compile(r'(<COLLECTION\b[^>]*ENTRIES=")(\d+)("[^>]*>)')
_TEMPO_BPM_RE = re.compile(r'<TEMPO\b[^>]*\bBPM="([^"]*)"')
_COLLECTION_CLOSE_RE = re.compile(r"</COLLECTION>")


_LOCATION_RE = re.compile(r"<LOCATION\b([^>]*)/?>")
_LOC_ATTRS = {name: re.compile(name + r'="([^"]*)"')
              for name in ("VOLUME", "DIR", "FILE")}


def _entry_key(entry_text: str) -> str:
    """This ENTRY's canonical track key, or ``""`` for one with no ``<LOCATION>``
    (the ``<ENTRY><PRIMARYKEY…></ENTRY>`` blocks inside playlists).

    Asks *what* the entry is rather than *whether* it is the one we want, which
    is what lets a write for many tracks be one dict lookup per entry instead of
    a full document walk per track. Answering the old question re-decided the
    same thing 5,050,750 times in one 1005-track run.

    ``entry_text`` must include the <LOCATION> element (i.e. the ENTRY's body,
    not just its opening tag). The patterns are compiled once: going through
    ``re.search``'s cache lookup for every attribute of every entry cost 20.6 s
    of that run on its own.
    """
    loc_m = _LOCATION_RE.search(entry_text)
    if not loc_m:
        return ""
    attrs = loc_m.group(1)

    def a(name):
        m = _LOC_ATTRS[name].search(attrs)
        return m.group(1) if m else ""

    # Raw attribute text straight off the file, so this is where the one entity
    # decode belongs -- ``path_key`` does not do it (see its docstring). Without
    # it a name holding a literal ``&amp;`` keys differently here than it does in
    # the reader, and no write can ever find its entry.
    return matching.path_key(matching.decode_entities(
        matching.build_windows_path(a("VOLUME"), a("DIR"), a("FILE"))))


def _entry_matches(entry_text: str, target_key: str) -> bool:
    """``entry_text`` must include the <LOCATION> element."""
    return bool(target_key) and _entry_key(entry_text) == target_key


#: Above this many tracks, locating each entry by filename costs more than one
#: walk of the whole document -- same measured crossover as the VirtualDJ side.
_FAST_LOCATE_MAX = 32

#: A filename may legitimately repeat across directories, so a needle can hit
#: several entries and each one has to be keyed to tell them apart. Past a
#: handful that is no longer cheaper than walking, so it falls back instead.
_MAX_CANDIDATES = 8


def locate_entries(content: str, by_key: dict):
    """``{key: match}`` when **every** entry was located, else ``None`` meaning
    "fall back to walking the document". ``by_key`` maps a key to its path.

    Looks for the ``FILE=`` attribute this module would itself write, which finds
    one ``<ENTRY>`` among tens of thousands with a substring search instead of a
    regex walk of a 132 MB file -- 2.38 s down to 0.33 s.

    A filename is **not** unique: the same track name lives in many folders, so
    the needle hits several entries and only the full path tells them apart.
    Every hit is therefore re-matched with :data:`_ENTRY_RE` and re-keyed with
    :func:`_entry_key`, the walk's own comparison. Without that the cues of one
    track land on a different track that happens to share a filename.

    Exactly one survivor is a hit. **Anything else gives up**: two survivors mean
    two entries claim the path and the walk would rewrite both; none means the
    search missed, which does *not* prove the entry is absent -- the same path
    has more than one legal encoding and the walk decodes before comparing, so
    reporting "absent" here would append a duplicate ``<ENTRY>``.
    """
    found: dict = {}
    for key, path in by_key.items():
        _volume, _dir_attr, file_name = matching.traktor_location_parts(path)
        if not file_name:
            return None
        needle = "FILE=" + quoteattr(file_name)
        hits, pos = [], content.find(needle)
        while pos >= 0:
            hits.append(pos)
            if len(hits) > _MAX_CANDIDATES:
                return None            # too common a name to be worth chasing
            pos = content.find(needle, pos + 1)

        matched = []
        for pos in hits:
            start = content.rfind("<ENTRY", 0, pos)
            if start < 0:
                return None
            m = _ENTRY_RE.match(content, start)
            if m is None:
                return None
            if _entry_key(m.group(2)) == key:
                matched.append(m)
        if len(matched) != 1:
            return None
        found[key] = matched[0]
    return found


def _splice(content: str, found: dict, rewrite) -> str:
    """Replace just the located blocks, in document order.

    Equivalent to a ``sub`` that happened to match only these blocks, which is
    what makes the output byte-identical to the walk's. (The VirtualDJ writer
    holds its own copy of this: the two format modules are deliberately
    independent of one another.)
    """
    out, last = [], 0
    for key, m in sorted(found.items(), key=lambda kv: kv[1].start()):
        out.append(content[last:m.start()])
        out.append(rewrite(m, key))
        last = m.end()
    out.append(content[last:])
    return "".join(out)


def sync_track_into_nml(nml_content: str, track: Track) -> tuple[str, bool]:
    """Update the matched <ENTRY>'s CUE_V2 set (+ PLAYCOUNT) from ``track``
    (positions already in the NML timeline — caller applies the inverse
    correction). Appends a new ENTRY to COLLECTION if no match is found.
    Returns (new_content, created).

    The one-track form of :func:`sync_tracks_into_nml`, kept so every caller and
    test that has only one track reads exactly as it always did.
    """
    new_content, created = sync_tracks_into_nml(nml_content, [track])
    return new_content, created[track.key]


def _entry_with_cues(open_tag: str, body: str, close_tag: str,
                     track: Track) -> str:
    """One ENTRY rewritten with ``track``'s cue set (+ PLAYCOUNT).

    Replaces the whole cue/grid set, so cues deleted on the source side
    disappear here too. Traktor-only markers (fade-in/out, load, loop) are NOT
    ours and are carried through untouched. One CUE_V2 per line.
    """
    new_body = CUE_V2_RE.sub(
        lambda cm: "" if is_owned_cue_v2(cm.group(0)) else cm.group(0), body)
    new_body = re.sub(r"\n[ \t]*\n+", "\n", new_body).rstrip()
    # The grid's tempo is the entry's own, so the marker agrees with TEMPO.
    tempo = _TEMPO_BPM_RE.search(new_body)
    bpm = tempo.group(1) if tempo else track.bpm
    cues_xml = "\n".join(format_cue_v2(p, bpm) for p in _ordered_pois(track))
    if cues_xml:
        new_body = new_body + "\n" + cues_xml
    if track.playcount is not None:
        new_body = _set_playcount_nml(new_body, track.playcount)
    return _set_modified(open_tag) + new_body + "\n" + close_tag


def sync_tracks_into_nml(nml_content: str, tracks) -> tuple[str, dict]:
    """Write **many** tracks' cue sets in a single walk of the document.

    Returns ``(new_content, {key: created})`` -- the writer's own answer per
    track, which is what the caller reports rather than guessing from memory.

    One walk, not one per track. Each write used to run ``_ENTRY_RE.sub`` over
    the whole file to find its single entry, which is O(tracks x entries): a
    1005-track run spent 46.6 of its 110.9 seconds inside ``re.sub``. Doing them
    together applies the same edits in the same order -- each track only ever
    touches its own entry -- so the file that lands is byte-identical.

    Two tracks with the same key collapse to the last one, exactly as before,
    where the second write simply overwrote the first.

    A small batch skips the walk entirely -- see :func:`locate_entries`.
    """
    by_key = {t.key: t for t in tracks}
    seen: set[str] = set()

    found = (locate_entries(nml_content, {k: t.path for k, t in by_key.items()})
             if 0 < len(by_key) <= _FAST_LOCATE_MAX else None)

    if found is None:
        def repl(m):
            open_tag, body, close_tag = m.groups()
            track = by_key.get(_entry_key(body))
            if track is None:
                return m.group(0)
            seen.add(track.key)
            return _entry_with_cues(open_tag, body, close_tag, track)

        new_content = _ENTRY_RE.sub(repl, nml_content)
    else:
        seen = set(found)
        new_content = _splice(
            nml_content, found,
            lambda m, k: _entry_with_cues(m.group(1), m.group(2), m.group(3),
                                          by_key[k]))

    missing = [t for k, t in by_key.items() if k not in seen]
    if missing:
        # Appended in the order given (playlist order), with ENTRIES bumped once
        # by however many really landed.
        entries_xml = "".join(_render_new_entry(t) for t in missing)
        new_content = _COLLECTION_CLOSE_RE.sub(
            lambda _m: entries_xml + "</COLLECTION>", new_content, count=1)

        def bump(m):
            return m.group(1) + str(int(m.group(2)) + len(missing)) + m.group(3)
        new_content = _COLLECTION_OPEN_RE.sub(bump, new_content, count=1)

    return new_content, {k: k not in seen for k in by_key}


def repath_entries_in_nml(nml_content: str, by_key: dict) -> tuple[str, dict]:
    """Point entries at new files. ``by_key`` maps a current key to a new path.

    Two things move, and missing either one loses the track:

    * the ``<LOCATION>`` on its COLLECTION ``<ENTRY>``, which is where the file
      lives;
    * every ``<PRIMARYKEY KEY="D:/:…">`` naming it, because Traktor records
      playlist membership **by path**, not by a reference to the entry. Rewrite
      only the LOCATION and the track drops out of every playlist holding it.

    One walk of the document for the entries and one substitution pass for the
    primary keys, whatever the batch size. Returns ``(new_content, {key: done})``
    where done is True if **either** was rewritten: a playlist may name a track
    the collection has no entry for, and moving that reference is still work.
    """
    if not by_key:
        return nml_content, {}
    seen: set[str] = set()

    def repl(m):
        open_tag, body, close_tag = m.groups()
        key = _entry_key(body)
        new_path = by_key.get(key)
        if new_path is None:
            return m.group(0)
        seen.add(key)
        volume, dir_attr, file_name = matching.traktor_location_parts(new_path)
        body = _LOCATION_RE.sub(
            lambda lm: (f'<LOCATION DIR="{escape(dir_attr)}" '
                        f'FILE={quoteattr(file_name)} '
                        f'VOLUME="{escape(volume)}"'
                        + _kept_location_attrs(lm.group(1)) + ">"),
            body, count=1)
        return _set_modified(open_tag) + body + close_tag

    new_content = _ENTRY_RE.sub(repl, nml_content)

    # Membership, by path, wherever it appears. Not gated on the entry having
    # been found: an orphan playlist reference has to move too, or Clean up
    # leaves it pointing at the old filename for ever.
    stored_for = {k: matching.windows_path_to_traktor_key(p)
                  for k, p in by_key.items()}
    moved: set[str] = set()

    def pk_repl(m):
        current = matching.traktor_key_to_path_key(
            matching.decode_entities(m.group(2)))
        stored = stored_for.get(current)
        if stored is None:
            return m.group(0)
        moved.add(current)
        return m.group(1) + escape(stored) + m.group(3)

    new_content = re.sub(r'(<PRIMARYKEY\b[^>]*\bKEY=")([^"]*)(")',
                         pk_repl, new_content)
    return new_content, {k: (k in seen or k in moved) for k in by_key}


#: Attributes on a ``<LOCATION>`` that are not part of the path and must survive
#: a repath -- ``VOLUMEID`` above all, which Traktor uses to identify the drive.
_LOC_KEEP_RE = re.compile(r'\b(?!DIR=|FILE=|VOLUME=)(\w+="[^"]*")')


def _kept_location_attrs(attrs: str) -> str:
    """Everything on the old LOCATION tag except the three path attributes."""
    kept = _LOC_KEEP_RE.findall(attrs.rstrip("/").strip())
    return (" " + " ".join(kept)) if kept else ""


def remove_entry_from_nml(nml_content: str, key: str) -> tuple[str, bool]:
    """Delete the COLLECTION ``<ENTRY>`` for ``key`` and decrement the COLLECTION
    ``ENTRIES`` count. Matched by ``<LOCATION>`` via :func:`_entry_matches`, so the
    ``<ENTRY><PRIMARYKEY…></ENTRY>`` blocks inside playlists (which carry no
    LOCATION) are never touched. Returns ``(new_content, removed)``."""
    state = {"hit": False}

    def repl(m):
        _open, body, _close = m.groups()
        if state["hit"] or not _entry_matches(body, key):
            return m.group(0)
        state["hit"] = True
        return ""

    new_content = _ENTRY_RE.sub(repl, nml_content)
    if not state["hit"]:
        return nml_content, False

    def dec(m):
        return m.group(1) + str(max(0, int(m.group(2)) - 1)) + m.group(3)
    return _COLLECTION_OPEN_RE.sub(dec, new_content, count=1), True


def _set_playcount_nml(body: str, playcount: int) -> str:
    """Set ``PLAYCOUNT`` on the entry's ``<INFO>``.

    Traktor writes ``<INFO …></INFO>``, not a self-closing tag, so an INFO
    without the attribute used to fall through to the last branch and gain a
    **second** ``<INFO>`` element. The attribute now goes onto the one there.
    """
    if re.search(r'<INFO\b[^>]*\bPLAYCOUNT="[^"]*"', body):
        return re.sub(r'(<INFO\b[^>]*\bPLAYCOUNT=")[^"]*(")',
                      rf'\g<1>{playcount}\g<2>', body, count=1)
    if re.search(r"<INFO\b", body):
        return re.sub(r"(<INFO\b[^>]*?)(\s*/?>)", rf'\1 PLAYCOUNT="{playcount}"\2',
                      body, count=1)
    return body + f'<INFO PLAYCOUNT="{playcount}"></INFO>'


def set_entries_playcount(nml_content: str, by_key: dict) -> tuple[str, dict]:
    """Set the play count on **many** ``<ENTRY>`` elements in one pass, and
    nothing else -- not the cues, not the modification stamp. Returns
    ``(new_content, {key: changed})``."""
    changed = {k: False for k in by_key}

    def repl(m):
        open_tag, body, close_tag = m.groups()
        key = _entry_key(body)
        if key not in by_key:
            return m.group(0)
        new_body = _set_playcount_nml(body, int(by_key[key]))
        changed[key] = new_body != body
        return open_tag + new_body + close_tag

    return _ENTRY_RE.sub(repl, nml_content), changed


# ========================= editable text metadata ========================
#: The fields a user may edit. BPM and musical key are excluded on purpose: they
#: are each program's own analysis output, and moving the BPM without moving the
#: beatgrid desynchronises the cues that model/cues.py exists to keep aligned.
#: Each value here is (element, attribute) inside the ENTRY -- ``None`` for the
#: element means the ENTRY's own opening tag.
_METADATA_TARGETS = {
    "title": (None, "TITLE"),
    "artist": (None, "ARTIST"),
    "album": ("ALBUM", "TITLE"),
    "genre": ("INFO", "GENRE"),
    "comment": ("INFO", "COMMENT"),
}

#: The musical key, which a **sync** may write but a user may not edit -- so it
#: is here rather than in the map above, which is what the metadata dialog is
#: built from. Both are written: Traktor displays from ``MUSICAL_KEY`` and
#: ``INFO/@KEY`` is the text other software reads, and leaving them disagreeing
#: is the state that put musical notation in a pane that should show Open Key.
_KEY_TARGETS = {
    "key": ("INFO", "KEY"),
    "key_value": ("MUSICAL_KEY", "VALUE"),
}

_WRITABLE_TARGETS = {**_METADATA_TARGETS, **_KEY_TARGETS}


def _set_tag_attr(tag: str, name: str, value: str) -> str:
    """Set one attribute on a single element's opening tag, adding it if absent.

    Only that attribute is touched, so everything else Traktor keeps on the tag
    survives -- the same rule the cue writer follows for the elements it does
    not own.
    """
    pat = re.compile(rf"(\b{name}=)(\"[^\"]*\"|'[^']*')", re.IGNORECASE)
    if pat.search(tag):
        return pat.sub(lambda m: m.group(1) + quoteattr(value), tag, count=1)
    return re.sub(r"(\s*/?>)$", " " + name + "=" + quoteattr(value) + r"\1",
                  tag, count=1)


def _set_child_attr(body: str, element: str, name: str, value: str) -> str:
    """Set an attribute on a child element of an ENTRY, creating the element if
    the track never had one (a track with no ALBUM, say)."""
    m = re.search(rf"<{element}\b[^>]*>", body, re.IGNORECASE)
    if m:
        return body[:m.start()] + _set_tag_attr(m.group(0), name, value) + body[m.end():]
    # Appended rather than slotted into Traktor's element order: it re-writes the
    # file in its own order when it next saves, and guessing a position risks
    # landing inside something we do not own.
    return body + f"<{element} {name}={quoteattr(value)}></{element}>"


def set_entry_metadata(nml_content: str, key: str,
                       fields: dict) -> tuple[str, bool]:
    """Set text metadata on the ``<ENTRY>`` matching ``key``.

    ``fields`` maps any of :data:`_WRITABLE_TARGETS` to its new value. A field
    that is **absent from the dict is left alone** -- that is what lets a
    multi-track edit change only the fields the user actually typed into, and
    leave every ``<various>`` one at each track's own value.

    Returns ``(new_content, changed)``; ``changed`` is False when no entry
    matched, so the caller can tell "nothing to do" from "done".

    The one-entry form of :func:`set_entries_metadata`.
    """
    new_content, changed = set_entries_metadata(nml_content, {key: fields})
    return new_content, changed[key]


def _entry_with_metadata(open_tag: str, body: str, close_tag: str,
                         fields: dict) -> str:
    """One ENTRY with ``fields`` set and nothing else touched.

    The single edit, so the located route and the walked one cannot drift apart.
    """
    for field, value in fields.items():
        element, attr = _WRITABLE_TARGETS[field]
        if element is None:
            open_tag = _set_tag_attr(open_tag, attr, value)
        else:
            body = _set_child_attr(body, element, attr, value)
    return _set_modified(open_tag) + body + close_tag


def set_entries_metadata(nml_content: str, by_key: dict,
                         paths: dict | None = None) -> tuple[str, dict]:
    """Set text metadata on **many** ``<ENTRY>`` elements in one pass.

    ``by_key`` maps a track key to its own ``fields`` dict, so a multi-track edit
    that changes different fields on different tracks is still one pass. Returns
    ``(new_content, {key: changed})``, ``changed`` being False for a key no entry
    matched -- the distinction that keeps an unwritten tag from reading as synced.

    ``paths`` maps a key to the file it is believed to live at. **Optional, and
    only ever an accelerator**: with it a small batch is located rather than
    walked (:func:`locate_entries`), which is the difference between 2.29 s and
    milliseconds on a 133.5 MB collection. Without it -- or when the locate will
    not vouch for what it found -- the walk below runs exactly as before. A
    caller that has no path is not doing anything wrong.
    """
    wanted = {k: {f: v for f, v in fields.items() if f in _WRITABLE_TARGETS}
              for k, fields in by_key.items()}
    wanted = {k: f for k, f in wanted.items() if f}
    if not wanted:
        return nml_content, {k: False for k in by_key}

    found = None
    if paths and len(wanted) <= _FAST_LOCATE_MAX:
        locatable = {k: paths[k] for k in wanted if paths.get(k)}
        if len(locatable) == len(wanted):
            found = locate_entries(nml_content, locatable)

    if found is None:
        seen: set[str] = set()

        def repl(m):
            open_tag, body, close_tag = m.groups()
            entry_key = _entry_key(body)
            fields = wanted.get(entry_key)
            if fields is None:
                return m.group(0)
            seen.add(entry_key)
            return _entry_with_metadata(open_tag, body, close_tag, fields)

        new_content = _ENTRY_RE.sub(repl, nml_content)
    else:
        seen = set(found)
        new_content = _splice(
            nml_content, found,
            lambda m, k: _entry_with_metadata(m.group(1), m.group(2),
                                              m.group(3), wanted[k]))

    changed = {k: k in seen for k in by_key}
    return (new_content if seen else nml_content), changed


def _ordered_pois(track: Track):
    """CUE_V2 order: by position, grid before a co-located cue (as Traktor
    writes them)."""
    return sorted(track.pois, key=lambda p: (p.pos, 0 if p.kind == "beatgrid" else 1))


def _modified_attrs(now: datetime | None = None) -> str:
    """Traktor's per-entry modification stamp. MODIFIED_TIME is seconds since
    midnight. Writing this is what makes UPDATE-mode recency decidable on the
    next diff (entries without it always read back as 'unknown')."""
    now = now or datetime.now()
    secs = now.hour * 3600 + now.minute * 60 + now.second
    return f'MODIFIED_DATE="{now.year}/{now.month}/{now.day}" MODIFIED_TIME="{secs}"'


def _set_modified(open_tag: str) -> str:
    """Refresh (or insert) MODIFIED_DATE/MODIFIED_TIME on an <ENTRY ...> tag."""
    now = datetime.now()
    date = f"{now.year}/{now.month}/{now.day}"
    secs = str(now.hour * 3600 + now.minute * 60 + now.second)
    for attr, val in (("MODIFIED_DATE", date), ("MODIFIED_TIME", secs)):
        if re.search(rf'\b{attr}="[^"]*"', open_tag):
            open_tag = re.sub(rf'(\b{attr}=")[^"]*(")', rf'\g<1>{val}\g<2>', open_tag, count=1)
        else:
            open_tag = re.sub(r"^<ENTRY\b", f'<ENTRY {attr}="{val}"', open_tag, count=1)
    return open_tag


def _render_info(track: Track, key_value) -> str:
    parts = [f'KEY={quoteattr(keys.open_key(key_value))}',
             f'GENRE={quoteattr(track.genre)}']
    if track.comment:
        parts.append(f'COMMENT={quoteattr(track.comment)}')
    if track.bitrate:
        parts.append(f'BITRATE="{int(track.bitrate) * 1000}"')
    parts.append(f'PLAYTIME="{track.length or 0:.0f}"')
    if track.playcount is not None:
        parts.append(f'PLAYCOUNT="{track.playcount}"')
    return "<INFO " + " ".join(parts) + "></INFO>"


def _render_new_entry(track: Track) -> str:
    """A new COLLECTION entry, one element per line, in Traktor's element order.

    LOUDNESS is never emitted (VirtualDJ supplies no equivalent); ALBUM and
    MUSICAL_KEY appear only when we actually have the data. Traktor fills the
    rest in when it analyses the track.
    """
    volume, dir_attr, file_name = matching.traktor_location_parts(track.path)
    # The canonical value when the source program supplied one; parsing the
    # display text is the fallback for a track that has never been analysed.
    key_value = (track.key_value if track.key_value is not None
                 else keys.parse_vdj_key(track.key_musical))

    lines = [
        f'<ENTRY {_modified_attrs()} TITLE={quoteattr(track.title)} '
        f'ARTIST={quoteattr(track.artist)}>'
        f'<LOCATION DIR="{escape(dir_attr)}" FILE={quoteattr(file_name)} '
        f'VOLUME="{escape(volume)}" VOLUMEID=""></LOCATION>'
    ]
    if track.album:
        lines.append(f'<ALBUM TITLE={quoteattr(track.album)}></ALBUM>')
    lines.append('<MODIFICATION_INFO AUTHOR_TYPE="user"></MODIFICATION_INFO>')
    lines.append(_render_info(track, key_value))
    if track.bpm:
        lines.append(f'<TEMPO BPM="{track.bpm:.6f}" BPM_QUALITY="100.000000"></TEMPO>')
    if key_value is not None:
        lines.append(f'<MUSICAL_KEY VALUE="{key_value}"></MUSICAL_KEY>')
    lines.extend(format_cue_v2(p, track.bpm) for p in _ordered_pois(track))
    lines.append("</ENTRY>")
    return "\n".join(lines) + "\n"


# ============================ playlist writing ===========================
_PLAYLIST_BY_UUID_RE_TMPL = r'(<PLAYLIST\b[^>]*UUID="{uuid}"[^>]*>)(.*?)(</PLAYLIST>)'


def add_tracks_to_playlist(nml_content: str, uuid: str, tracks: list[Track]) -> str:
    """Append ENTRY/PRIMARYKEY rows for ``tracks`` to the playlist with this
    UUID, bumping its ENTRIES count."""
    pattern = re.compile(_PLAYLIST_BY_UUID_RE_TMPL.format(uuid=re.escape(uuid)), re.DOTALL)

    def repl(m):
        open_tag, body, close_tag = m.groups()
        new_entries = "".join(
            f'<ENTRY><PRIMARYKEY TYPE="TRACK" KEY="{escape(matching.windows_path_to_traktor_key(t.path))}"></PRIMARYKEY></ENTRY>'
            for t in tracks
        )
        new_open = re.sub(r'(ENTRIES=")(\d+)(")',
                          lambda mm: mm.group(1) + str(int(mm.group(2)) + len(tracks)) + mm.group(3),
                          open_tag, count=1)
        return new_open + body + new_entries + close_tag

    return pattern.sub(repl, nml_content, count=1)


def set_playlist_entries(nml_content: str, uuid: str, tracks: list[Track]) -> str:
    """Replace a playlist's entire membership with ``tracks``, in order.

    Used by UPDATE/OVERWRITE, which mirror the source exactly: rewriting the
    body is both simpler than remove-then-append and the only way to mirror
    *order* too. A Traktor <PLAYLIST> contains nothing but <ENTRY> children, so
    replacing the body is safe.
    """
    pattern = re.compile(_PLAYLIST_BY_UUID_RE_TMPL.format(uuid=re.escape(uuid)), re.DOTALL)

    def repl(m):
        open_tag, _body, close_tag = m.groups()
        entries = "".join(
            f'<ENTRY><PRIMARYKEY TYPE="TRACK" KEY="{escape(matching.windows_path_to_traktor_key(t.path))}"></PRIMARYKEY></ENTRY>'
            for t in tracks
        )
        new_open = re.sub(r'(ENTRIES=")(\d+)(")',
                          lambda mm: mm.group(1) + str(len(tracks)) + mm.group(3),
                          open_tag, count=1)
        return new_open + entries + close_tag

    return pattern.sub(repl, nml_content, count=1)


def playlist_entry_keys(nml_content: str, uuid: str) -> list[str]:
    """The canonical track keys currently stored in a playlist, in order."""
    pattern = re.compile(_PLAYLIST_BY_UUID_RE_TMPL.format(uuid=re.escape(uuid)), re.DOTALL)
    m = pattern.search(nml_content)
    if not m:
        return []
    # ``findall`` returns raw attribute text, so decode here -- the reader gets
    # the same values already decoded by ElementTree.
    return [matching.traktor_key_to_path_key(matching.decode_entities(k))
            for k in re.findall(r'<PRIMARYKEY\b[^>]*\bKEY="([^"]*)"', m.group(2))]


def rename_playlist_node(nml_content: str, uuid: str, new_name: str) -> tuple[str, bool]:
    """Rewrite the ``NAME`` of the ``<NODE TYPE="PLAYLIST">`` wrapping the
    playlist with this UUID. Returns ``(new_content, renamed)``.

    Anchored on the UUID rather than the old name: the UUID is what survives a
    rename, and two playlists in different folders may share a name. The lazy
    ``<NODE …>`` prefix keeps this attribute-order agnostic, and the trailing
    ``<PLAYLIST … UUID=…>`` (which Traktor writes immediately after the NODE
    tag) is what scopes the edit to exactly one node.
    """
    pattern = re.compile(
        r'(<NODE\b[^>]*?)\bNAME="[^"]*"([^>]*>\s*<PLAYLIST\b[^>]*\bUUID="'
        + re.escape(uuid) + r'")')
    escaped = escape(new_name).replace('"', "&quot;")
    state = {"hit": False}

    def repl(m):
        state["hit"] = True
        # A function replacement, so backslashes in the name are never read as
        # group references.
        return f'{m.group(1)}NAME="{escaped}"{m.group(2)}'

    new_content = pattern.sub(repl, nml_content, count=1)
    return (new_content, True) if state["hit"] else (nml_content, False)


def create_playlist_node(nml_content: str, folder_names: tuple[str, ...], name: str,
                         uuid: str, tracks: list[Track]) -> str:
    """Create a brand-new PLAYLIST node named ``name`` inside the folder chain
    ``folder_names`` (created under $ROOT if any segment doesn't exist yet —
    handled by the caller via ``ensure_folder_chain``), then populate it.

    This function only appends the leaf PLAYLIST node inside the SUBNODES of
    the (already-guaranteed-to-exist) target folder, matched by walking
    ``folder_names`` node-by-node. If the chain can't be resolved, the
    playlist is appended under $ROOT's own SUBNODES as a fallback.
    """
    entries = "".join(
        f'<ENTRY><PRIMARYKEY TYPE="TRACK" KEY="{escape(matching.windows_path_to_traktor_key(t.path))}"></PRIMARYKEY></ENTRY>'
        for t in tracks
    )
    node_xml = (
        f'<NODE TYPE="PLAYLIST" NAME={quoteattr(name)}>'
        f'<PLAYLIST ENTRIES="{len(tracks)}" TYPE="LIST" UUID="{uuid}">'
        f'{entries}</PLAYLIST></NODE>'
    )

    # Mirror the source's folder structure: create any missing FOLDER nodes so
    # the playlist lands in the same place VirtualDJ keeps it. This is also what
    # keeps repeated syncs idempotent -- the engine's lookup resolves the same
    # path the writer used, instead of failing and appending a fresh node.
    nml_content = ensure_folder_chain(nml_content, folder_names)

    target_subnodes = _find_subnodes_region(nml_content, folder_names)
    if target_subnodes is None:
        # Safety net only (the chain should exist after ensure_folder_chain):
        # fall back to $ROOT's own SUBNODES.
        target_subnodes = _find_subnodes_region(nml_content, ())
    if target_subnodes is None:
        return re.sub(r"</PLAYLISTS>", lambda _m: node_xml + "</PLAYLISTS>",
                      nml_content, count=1)

    _open_after, close_start, count_start, count_end, count_val = target_subnodes
    new_count = str(int(count_val) + 1)
    # Rebuild: [... up to count digits] + new_count + [after count digits up to
    # the matching </SUBNODES>] + new playlist node + [rest of file].
    return (nml_content[:count_start] + new_count + nml_content[count_end:close_start]
           + node_xml + nml_content[close_start:])


def ensure_folder_chain(nml_content: str, folder_names: tuple[str, ...]) -> str:
    """Create any FOLDER nodes in ``folder_names`` that don't exist yet.

    Walks the chain from $ROOT downward; for each segment with no
    (case-insensitive) match, inserts an empty folder node into its parent's
    <SUBNODES> and bumps that parent's COUNT. The chain is re-probed each
    iteration because every insert shifts offsets.

    Returns the content unchanged if a parent can't be resolved (the caller
    then falls back to $ROOT).
    """
    for depth in range(1, len(folder_names) + 1):
        chain = folder_names[:depth]
        if _find_subnodes_region(nml_content, chain) is not None:
            continue                      # this level already exists
        parent = _find_subnodes_region(nml_content, chain[:-1])
        if parent is None:
            return nml_content            # can't place it; caller falls back
        _open_after, close_start, count_start, count_end, count_val = parent
        folder_xml = (f'<NODE TYPE="FOLDER" NAME={quoteattr(chain[-1])}>'
                      f'<SUBNODES COUNT="0"></SUBNODES></NODE>\n')
        new_count = str(int(count_val) + 1)
        nml_content = (nml_content[:count_start] + new_count
                       + nml_content[count_end:close_start] + folder_xml
                       + nml_content[close_start:])
    return nml_content


# ===================== folder rename / node relocation =====================
def _find_folder_node_span(content: str, folder_names: tuple[str, ...]):
    """``(node_start, node_end, name_start, name_end)`` for the FOLDER node at
    ``folder_names``, or ``None``.

    Derived from :func:`_find_subnodes_region`, which already locates that
    folder's ``<SUBNODES>`` and its matching close tag: the node opens just
    before the one and closes just after the other. ``folder_names == ()`` is
    ``$ROOT``, which is never renameable or movable -- rejected here so callers
    can't reach it by accident.
    """
    if not folder_names:
        return None
    region = _find_subnodes_region(content, folder_names)
    if region is None:
        return None
    open_after, close_start, _cs, _ce, _cv = region

    node_start = content.rfind("<NODE", 0, open_after)
    if node_start < 0:
        return None
    open_tag = content[node_start:open_after]
    nm = re.search(r'\bNAME="([^"]*)"', open_tag)
    if nm is None or nm.group(1).lower() != folder_names[-1].lower():
        return None                       # not the node we resolved; refuse

    end_m = re.compile(r"</SUBNODES>\s*</NODE>\n?").match(content, close_start)
    if end_m is None:
        return None
    return (node_start, end_m.end(),
            node_start + nm.start(1), node_start + nm.end(1))


def rename_folder_node(nml_content: str, folder_names: tuple[str, ...],
                       new_name: str) -> tuple[str, bool]:
    """Rewrite the ``NAME`` of the FOLDER node at ``folder_names``.

    Folders carry no UUID -- unlike :func:`rename_playlist_node` this can only be
    anchored on the path. Returns ``(new_content, renamed)``.
    """
    span = _find_folder_node_span(nml_content, folder_names)
    if span is None:
        return nml_content, False
    _start, _end, name_start, name_end = span
    escaped = escape(new_name).replace('"', "&quot;")
    return nml_content[:name_start] + escaped + nml_content[name_end:], True


def _playlist_node_span(nml_content: str, uuid: str):
    """``(start, end)`` of the ``<NODE …><PLAYLIST … UUID=…>…</NODE>`` block, or
    ``None``. ``<PLAYLIST>`` can't nest, so the lazy match is exact, and the NODE
    tag must sit immediately before it, which scopes this to the right one."""
    m = re.search(
        r'<NODE\b[^>]*>\s*<PLAYLIST\b[^>]*\bUUID="' + re.escape(uuid)
        + r'".*?</PLAYLIST>\s*</NODE>\n?', nml_content, re.DOTALL)
    return (m.start(), m.end()) if m else None


def _cut_node(nml_content: str, span, src_parent: tuple[str, ...]):
    """Lift the node text at ``span`` out of ``src_parent``'s SUBNODES and
    decrement that COUNT. Returns ``(new_content, block)``, or
    ``(nml_content, None)`` when the parent can't be resolved.

    Shared by removal and by the cut half of a move -- taking the block verbatim
    is what preserves everything inside it, a playlist's UUID and entries or a
    folder's whole subtree.
    """
    start, end = span
    block = nml_content[start:end]
    cut = nml_content[:start] + nml_content[end:]

    src = _find_subnodes_region(cut, src_parent)
    if src is None:
        return nml_content, None
    _open_after, _close, count_start, count_end, count_val = src
    return cut[:count_start] + str(max(0, int(count_val) - 1)) + cut[count_end:], block


def remove_playlist_node(nml_content: str, uuid: str,
                         folder_names: tuple[str, ...]) -> tuple[str, bool]:
    """Delete the PLAYLIST node with this UUID from ``folder_names``'s SUBNODES.

    Only the playlist node goes -- the COLLECTION entries its tracks point at are
    untouched (that is :func:`remove_entry_from_nml`'s job).
    """
    span = _playlist_node_span(nml_content, uuid)
    if span is None:
        return nml_content, False
    content, block = _cut_node(nml_content, span, folder_names)
    return (content, True) if block is not None else (nml_content, False)


def remove_folder_node(nml_content: str, folder_names: tuple[str, ...]) -> tuple[str, bool]:
    """Delete the FOLDER node at ``folder_names`` and everything inside it.
    ``$ROOT`` is refused by :func:`_find_folder_node_span`."""
    span = _find_folder_node_span(nml_content, folder_names)
    if span is None:
        return nml_content, False
    content, block = _cut_node(nml_content, span[:2], folder_names[:-1])
    return (content, True) if block is not None else (nml_content, False)


def _cut_and_paste(nml_content: str, span, src_parent: tuple[str, ...],
                   dst_parent: tuple[str, ...]) -> tuple[str, bool]:
    """Lift the node text at ``span`` out of ``src_parent``'s SUBNODES and splice
    it into ``dst_parent``'s, fixing both COUNTs.

    Every step re-probes, because each edit shifts the offsets the previous probe
    returned (the same reason :func:`ensure_folder_chain` re-probes per level).
    If the destination can't be resolved once the block is out, the ORIGINAL
    content is returned: a failed move must never be able to lose the subtree.
    """
    cut, block = _cut_node(nml_content, span, src_parent)
    if block is None:
        return nml_content, False

    pasted = ensure_folder_chain(cut, dst_parent)
    dst = _find_subnodes_region(pasted, dst_parent)
    if dst is None:
        return nml_content, False
    _open_after, close_start, count_start, count_end, count_val = dst
    # Every slice indexes `pasted`, so a count that changes width (9 -> 10)
    # doesn't invalidate close_start -- same construction as create_playlist_node.
    return (pasted[:count_start] + str(int(count_val) + 1)
            + pasted[count_end:close_start] + block + pasted[close_start:]), True


def move_playlist_node(nml_content: str, uuid: str, from_folder: tuple[str, ...],
                       to_folder: tuple[str, ...]) -> tuple[str, bool]:
    """Relocate the PLAYLIST node with this UUID to ``to_folder``."""
    span = _playlist_node_span(nml_content, uuid)
    if span is None:
        return nml_content, False
    return _cut_and_paste(nml_content, span, from_folder, to_folder)


def move_folder_node(nml_content: str, from_path: tuple[str, ...],
                     to_folder: tuple[str, ...]) -> tuple[str, bool]:
    """Relocate the FOLDER node at ``from_path`` (contents and all) into
    ``to_folder``.

    Refuses to move a folder into its own subtree *before* cutting anything:
    afterwards the destination path no longer exists, and ensure_folder_chain
    would helpfully create a fresh empty one to paste into -- silently replacing
    the subtree with a stub.
    """
    if path_ci(to_folder[:len(from_path)]) == path_ci(from_path):
        return nml_content, False
    span = _find_folder_node_span(nml_content, from_path)
    if span is None:
        return nml_content, False
    return _cut_and_paste(nml_content, span[:2], from_path[:-1], to_folder)


def _find_subnodes_region(content: str, folder_names: tuple[str, ...]):
    """Locate the <SUBNODES> element a new node should be inserted into.

    Starts at $ROOT's SUBNODES, then descends case-sensitively through each
    name in ``folder_names``. Returns
    ``(insert_scan_start, close_tag_start, count_digit_start, count_digit_end,
    count_value)`` for the deepest resolved SUBNODES, or ``None`` if any folder
    segment can't be resolved. ``folder_names == ()`` targets $ROOT itself
    (this is the normal top-level case and must never raise).

    A pragmatic regex walk (not a full DOM parse), consistent with the app's
    text-surgical philosophy; sufficient for playlists a few folders deep.
    """
    pl_m = re.search(r"<PLAYLISTS>.*?</PLAYLISTS>", content, re.DOTALL)
    if not pl_m:
        return None
    region = content[pl_m.start():pl_m.end()]
    base = pl_m.start()

    # Enter $ROOT's SUBNODES (fall back to the very first SUBNODES if a given
    # NML doesn't wrap its tree in a $ROOT folder node).
    root_m = re.search(r'<NODE TYPE="FOLDER" NAME="\$ROOT"[^>]*><SUBNODES COUNT="(\d+)">', region)
    if root_m is None:
        root_m = re.search(r'<SUBNODES COUNT="(\d+)">', region)
    if root_m is None:
        return None
    cursor = root_m.end()
    count_start = base + root_m.start(1)
    count_end = base + root_m.end(1)
    count_val = root_m.group(1)

    # Descend into each named folder, scoped to what remains after the cursor.
    # Matching is case-insensitive so a VirtualDJ "Breaks" folder resolves to an
    # existing Traktor "breaks" rather than spawning a near-duplicate twin (and,
    # critically, so the writer and the engine's lookup agree on placement).
    for name in folder_names:
        m = re.search(
            r'<NODE TYPE="FOLDER" NAME="' + re.escape(name) + r'"[^>]*><SUBNODES COUNT="(\d+)">',
            region[cursor:], re.IGNORECASE,
        )
        if not m:
            return None
        count_start = base + cursor + m.start(1)
        count_end = base + cursor + m.end(1)
        count_val = m.group(1)
        cursor += m.end()

    # Find the matching </SUBNODES> for the current SUBNODES via depth counting.
    depth = 1
    i = cursor
    while depth > 0:
        nm = re.search(r"<SUBNODES\b[^>]*>|</SUBNODES>", region[i:])
        if not nm:
            return None
        depth += -1 if nm.group(0).startswith("</") else 1
        i += nm.end()
    close_start = base + i - len("</SUBNODES>")

    return (base + cursor, close_start, count_start, count_end, count_val)
