"""Sync-status diff engine.

Computes the coloured-square status for tracks and playlists, always in a
**direction**: ``source`` library (the software whose playlist the user clicked)
-> ``target`` library (the other software).

A status carries **two** things: a colour for the state, and a shape for whose
change caused it.

    colour   green  synced · yellow changed · black absent · red error
    shape    square    this copy's relationship to the REFERENCE program
             triangle  self reference -- this copy is the one that changed
             fork      another, non-reference program changed

    GREEN         square    synced to the reference
    BLACK         square    not in the reference yet
    RED           X         referenced but missing from the collection
    YELLOW_SELF   triangle  this copy changed
    YELLOW_REF    square    the reference changed (this copy is behind)
    YELLOW_OTHER  fork      some other program changed

THE LAW
-------
**Green means "matches the reference", and nothing else.** A copy that differs is
never green, whoever made the change — the shape says who, so the colour does not
have to. For a track or playlist viewed from copy S:

    S missing                    -> RED
    not in the target at all     -> BLACK
    content matches              -> GREEN
    differs, S changed it        -> YELLOW_SELF
    differs, the reference did   -> YELLOW_REF
    differs, another program did -> YELLOW_OTHER
    differs, nobody attributed   -> YELLOW_REF  (still not green)

That last line matters: attribution needs history, and the absence of a verdict is
not evidence of being in sync. Returning green there is what made a playlist show
"synced" while its rows displayed incoming tracks.

Earlier this was direction-based -- yellow marked only the side "with something to
push", so the un-edited side stayed green and showed preview markers instead. That
reads as "in sync" under a reference model, which is why it changed.

"Content" = cues AND the metadata fields in :data:`COMPARED_FIELDS`. A field
present on one side and blank on the other counts as a difference.

``key`` is deliberately NOT compared: Traktor stores Open-Key ("12m", "3m")
while VirtualDJ stores musical notation ("Am") and leaves it blank for ~90% of
tracks, so comparing it would mark nearly every track permanently yellow.

``comment2`` is plumbed through but neither database actually stores one today
(VirtualDJ's <Tags> has no comment attribute -- its comment is the <Comment>
element -- and the NML carries no COMMENT2), so it is blank on both sides and
always compares equal. VirtualDJ's ``Grouping`` was rejected as a stand-in
because it is frequently populated and would flag mass false yellows.

Cue equivalence tolerates the verified Class-A offset: two cue sets are
considered "synced" when, after allowing a per-track constant shift of 0 or
CORRECTION_OFFSET_SEC, their sorted positions line up within EPS. This keeps a
correctly-migrated Class-A track green instead of perpetually "needs update"
(Traktor 7.865 vs VirtualDJ 7.8135 is the *same* musical position).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from . import cues as cues_mod, matching
from .cues import CORRECTION_OFFSET_SEC, Poi
# No program constants: every function here works on whichever two libraries it
# is handed and takes their names from ``Library.source``.
from .library import Library, Track, path_ci

GREEN = "green"
BLACK = "black"
RED = "red"

# The three yellows. There is no bare "yellow": a change always has an author, so
# it always has a shape.
YELLOW_SELF = "yellow_self"      # triangle -- this copy changed
YELLOW_REF = "yellow_ref"        # square   -- the reference changed
YELLOW_OTHER = "yellow_other"    # fork     -- another program changed

YELLOWS = (YELLOW_SELF, YELLOW_REF, YELLOW_OTHER)


def is_changed(status: str) -> bool:
    """True for any "this differs" status, whoever caused it. For callers that
    care that something needs attention but not who is to blame."""
    return status in YELLOWS


def changed_glyph(viewer: str, reference: str, changed_by) -> str:
    """Which yellow ``viewer`` shows, given the set of programs that changed.

    Precedence is per side: your own edit is the most useful thing to tell you,
    then the reference's (you are behind it), then anyone else's. So a change made
    on two sides shows a triangle on both, rather than needing a colour of its own.

    An empty ``changed_by`` still yields a yellow, and specifically the triangle.
    This is only ever called once a real difference has been established, so no
    verdict means no *history* -- which is not evidence that the two agree. The
    triangle keeps the long-standing "with no history both sides light up" rule;
    a square there would have the reference telling itself the reference changed.
    """
    changed_by = changed_by or ()
    if viewer in changed_by:
        return YELLOW_SELF
    if reference in changed_by:
        return YELLOW_REF
    if changed_by:
        return YELLOW_OTHER
    return YELLOW_SELF


_EPS = 0.002  # 2 ms tolerance for position comparison


@dataclass
class Report:
    """Result of diffing source->target for a set of tracks."""

    track_status: dict[str, str]              # track_key -> colour

    def playlist_status(self, source_node, target_index,
                        changed_by=None, reference: str = "",
                        colliding=()) -> str:
        """Status for one source playlist, relative to the target software.

        **Existence is checked first.** Track colours describe whether a
        *track's cues* are in sync, and a track can be green purely because some
        *other* playlist already carried it across. Aggregating track colours
        alone therefore made a playlist that does not exist in the other program
        at all look yellow/green. So:

            not present in target        -> BLACK  (nothing synced yet)
            present, a track unfound     -> RED
            present and fully in sync    -> GREEN
            anything else                -> a yellow, shaped by who changed

        Membership **and track order** are both compared. ``changed_by`` is the
        set of programs that own a divergence for this playlist, from
        :func:`attribute_playlists` aggregated across every pair -- a single pair
        cannot report that some *third* program is the one that moved.

        ``target_index`` is a ``Library.playlist_index()`` mapping.

        ``colliding`` holds the paths this library spells more than one way (see
        ``Library.colliding_playlist_paths``). Two playlists on one path is not
        an *error here* -- Traktor is perfectly happy holding "temp" and "Temp"
        side by side -- so this is not a colour of its own. It only means the
        usual case-insensitive lookup cannot say which of them the target's
        single copy is, and it kept whichever came first: a "temp" that was
        never written showed the rows and colours of the "Temp" that was.

        So for an ambiguous path, and only then, the names must match exactly.
        The twin the target really holds reads as it always did and the other
        reads BLACK -- "not there yet", which is simply true.
        """
        path = path_ci(source_node.node_path)
        target_node = target_index.get(path)
        if (target_node is not None and path in colliding
                and tuple(target_node.node_path) != tuple(source_node.node_path)):
            target_node = None
        if target_node is None or target_node.playlist is None:
            return BLACK

        keys = list(source_node.playlist.track_keys)
        target_keys = list(target_node.playlist.track_keys)
        if not keys and not target_keys:
            return GREEN            # both sides empty -> in sync

        colours = [self.track_status.get(k, BLACK) for k in keys]
        if any(c == RED for c in colours):
            return RED

        # Same members, same order, and every track agrees -> genuinely synced.
        if keys == target_keys and all(c == GREEN for c in colours):
            return GREEN
        return changed_glyph(source_node.source, reference, changed_by)

    def reference_playlist_status(self, node, peer_indices, changed_by=None,
                                  reference: str = "", colliding=()) -> str:
        """Status for the **reference's own** copy of a playlist.

        The reference has no single library to be compared against, so it is
        compared against all of them: green only when every program that has this
        playlist agrees with it, black when none has it yet. It can never be
        "behind the reference", so its only yellows are the triangle (it changed)
        and the fork (somebody else did).

        ``peer_indices`` is a list of ``Library.playlist_index()`` mappings.
        """
        path = path_ci(node.node_path)
        exact = path in colliding      # see playlist_status
        peers = [idx[path] for idx in peer_indices
                 if idx.get(path) is not None and idx[path].playlist is not None
                 and (not exact
                      or tuple(idx[path].node_path) == tuple(node.node_path))]
        if not peers:
            return BLACK            # no other program has it yet

        keys = list(node.playlist.track_keys)
        colours = [self.track_status.get(k, BLACK) for k in keys]
        if any(c == RED for c in colours):
            return RED
        if all(list(p.playlist.track_keys) == keys for p in peers) \
                and all(c == GREEN for c in colours):
            return GREEN
        return changed_glyph(reference, reference, changed_by)


def _positions(pois: list[Poi], kind: str) -> list[float]:
    return sorted(p.pos for p in pois if p.kind == kind)


def _lists_match(a: list[float], b: list[float], shift: float) -> bool:
    """True when ``b == max(a - shift, 0)`` elementwise.

    The floor at zero mirrors what the writer actually does
    (``cues.corrected_pos_nml_to_vdj`` = ``max(pos - C, 0.0)``): a cue at t=0
    lands on 0.0 rather than a negative position. Without modelling that clamp,
    a correctly-synced Class-A track whose earliest cue sits at 0 breaks the
    uniform-shift test and can never read as in sync -- it stays yellow forever
    and Overwrite rewrites identical values, so it never converges.
    """
    if len(a) != len(b):
        return False
    return all(abs(max(x - shift, 0.0) - y) <= _EPS for x, y in zip(a, b))


def cues_equivalent(src: Track, tgt: Track) -> bool:
    """True if target cue/grid data already represents src (allowing the
    constant Class-A shift in either direction).

    The reverse direction is tested as ``(tb, sa, +C)`` rather than a negative
    shift so the floor-at-zero in :func:`_lists_match` is applied to whichever
    side is the *Traktor* one. The clamp destroys information (both 0.0 and
    0.051536 map to 0.0 on the VirtualDJ side), so the test has to be run in the
    direction the data actually flowed to stay symmetric.
    """
    return all(_kind_equivalent(kind, _positions(src.pois, kind),
                                _positions(tgt.pois, kind), src, tgt)
               for kind in ("cue", "beatgrid"))


def _grids_in_phase(sa: list[float], tb: list[float], bpm_a, bpm_b) -> bool:
    """True when two single-marker grids start on the same downbeat: anchors a
    whole number of bars apart, allowing the Class-A shift either way.

    The writer steps a negative anchor forward by bars
    (``cues.grid_anchor_at_or_after_zero``), so VirtualDJ's -1.588 s and the
    1.017 s written for it into Traktor are one grid; without this they would
    never compare in sync. Unlike :func:`_lists_match` there is no floor --
    flooring a grid anchor is the defect this undoes. A grid with several
    markers (a tempo change) is not a single phase, so it compares exactly.

    **Tempo is not compared, as it never was.** The bar is tried at each side's
    BPM, because the two programs often disagree in the decimals and sometimes
    beyond (VirtualDJ 120.0, Traktor 120.49 on "Freaky Dancin"); the writer
    steps by the source's. Requiring equal tempos left 13 of 38 repaired grids
    reading as different although their anchors sat exactly a bar apart.
    """
    if len(sa) != 1 or len(tb) != 1:
        return False
    bars_s = {b for b in (cues_mod.bar_seconds(bpm_a), cues_mod.bar_seconds(bpm_b)) if b}
    for bar in bars_s:
        for shift in (0.0, CORRECTION_OFFSET_SEC, -CORRECTION_OFFSET_SEC):
            bars = (tb[0] - sa[0] - shift) / bar
            if abs(bars - round(bars)) * bar <= _EPS:
                return True
    return False


# Metadata compared alongside cues. NOTE: "key" is intentionally absent -- see
# the module docstring.
from .baseline import BOTH as bl_BOTH, UNKNOWN as bl_UNKNOWN  # noqa: E402

COMPARED_FIELDS = ("title", "artist", "album", "genre", "comment", "comment2")


def _norm(value) -> str:
    """Trimmed, case-folded, and with one canonical line ending.

    The newline fold matters as much as the case fold: Traktor hands back
    ``\\r\\n`` where VirtualDJ hands back ``\\n`` for the very same comment, and
    a field that can never compare equal is one Overwrite rewrites on every run
    while the track stays yellow. The readers already normalise; doing it here
    too means no future reader can reintroduce the problem.

    **And one Unicode spelling.** ``Tony! Toni! Toné!`` with a composed é and
    with ``e`` + U+0301 is the same text on screen and was a permanent
    difference here -- 60 of the tracks relinked on 2026-11-03. Comparing NFC
    forms changes nothing that is written; paths are a different matter and
    are still never normalised (see ``model/cleanup.py``).
    """
    return unicodedata.normalize(
        "NFC", matching.normalize_text(value)).strip().casefold()


def metadata_matches(a: Track, b: Track, fields=None) -> bool:
    """True when every compared metadata field is equal (trimmed, case-folded).
    A value on one side and a blank on the other counts as a difference.

    ``fields`` narrows the comparison. The sync engine passes the fields it can
    actually *write* into the target, because a field it compares but cannot
    write is one it would rewrite on every single sync and never converge --
    ``comment2`` is exactly that, being Traktor-only.
    """
    return all(_norm(getattr(a, f, "")) == _norm(getattr(b, f, ""))
               for f in (COMPARED_FIELDS if fields is None else fields))


def content_matches(a: Track, b: Track, fields=None) -> bool:
    """True when the two copies of a track carry the same cues and metadata."""
    return cues_equivalent(a, b) and metadata_matches(a, b, fields)


def what_differs(track: Track, others) -> list[str]:
    """What keeps ``track`` from matching its other copies, in words.

    ``["cues", "genre"]`` and the like, in a fixed order, for the needs-sync
    pane's song list -- the answer to "why is this row yellow?" without opening
    two programs side by side. Built from the same comparisons the status uses
    (:func:`cues_equivalent`, :func:`_norm`), so it names exactly what the
    colour is about. ``others`` with no copy in it answers ``[]``.
    """
    out: list[str] = []
    for other in others:
        if other is None:
            continue
        for kind, label in (("cue", "cues"), ("beatgrid", "grid")):
            a, b = _positions(track.pois, kind), _positions(other.pois, kind)
            if not _kind_equivalent(kind, a, b, track, other) and label not in out:
                out.append(label)
        for field in COMPARED_FIELDS:
            if (_norm(getattr(track, field, "")) != _norm(getattr(other, field, ""))
                    and field not in out):
                out.append(field)
    return out


def _kind_equivalent(kind: str, a: list, b: list, ta: Track, tb: Track) -> bool:
    """One kind's share of :func:`cues_equivalent`: the same positions, or the
    same shifted by the Class-A offset whichever side is the NML one, or -- for
    a grid -- the same downbeat a whole number of bars along."""
    if len(a) != len(b):
        return False
    return (_lists_match(a, b, 0.0) or _lists_match(a, b, CORRECTION_OFFSET_SEC)
            or _lists_match(b, a, CORRECTION_OFFSET_SEC)
            or (kind == "beatgrid" and _grids_in_phase(a, b, ta.bpm, tb.bpm)))


def newer_side(a: Track, b: Track):
    """Which of the two is provably newer: ``"a"``, ``"b"``, or ``None``.

    ``None`` means undecidable -- either timestamp is missing, or they are
    equal. Callers treat that as "flag both sides", never as "a wins".
    """
    if a is None or b is None or a.modified is None or b.modified is None:
        return None
    if a.modified > b.modified:
        return "a"
    if b.modified > a.modified:
        return "b"
    return None


def source_is_newer(src: Track, tgt: Track) -> bool:
    """Back-compat shim: True unless the target is provably newer."""
    return newer_side(src, tgt) != "b"


def _view(baseline, lib_a: Library, lib_b: Library):
    """The baseline scoped to this pair of programs.

    Accepts a whole :class:`Baseline` or an already-scoped view, so callers that
    have one of each (the window scopes per pair; tests often pass the store)
    both work.
    """
    return (baseline.for_pair(lib_a.source, lib_b.source)
            if hasattr(baseline, "for_pair") else baseline)


def attribute_changes(shared_keys, lib_a: Library, lib_b: Library,
                      baseline, yield_fn=None) -> dict[str, str]:
    """Work out, once per refresh, which of the two programs edited each
    diverging track.

    Returns ``{key: owner}`` in absolute terms (an adapter id, or ``"both"``) so
    both directional reports agree. Keys that are in sync are absent from the
    result. ``yield_fn`` (optional) is called every ~256 keys so a caller can
    keep an event loop responsive during a big diff.
    """
    from . import baseline as bl

    view = _view(baseline, lib_a, lib_b)
    owners: dict[str, str] = {}
    for i, key in enumerate(shared_keys):
        a = lib_a.tracks.get(key)
        b = lib_b.tracks.get(key)
        if a is None or b is None:
            continue
        owner = bl.attribute(key, bl.fingerprint(a), bl.fingerprint(b),
                             content_matches(a, b), view)
        if owner:
            owners[key] = owner
        if yield_fn is not None and (i & 0x7F) == 0:
            yield_fn()
    return owners


def attribute_playlists(lib_a: Library, lib_b: Library, baseline,
                        yield_fn=None) -> dict:
    """Same attribution for playlist membership/order, keyed by ``path_ci``.
    ``yield_fn`` is called per playlist so a big fingerprint pass stays smooth."""
    from . import baseline as bl

    view = _view(baseline, lib_a, lib_b)
    a_index = lib_a.playlist_index()
    b_index = lib_b.playlist_index()
    owners: dict = {}
    for i, (path, v_node) in enumerate(a_index.items()):
        if yield_fn is not None and (i & 0x1F) == 0:
            yield_fn()
        t_node = b_index.get(path)
        if t_node is None:
            continue
        v_fp = bl.playlist_fingerprint(v_node)
        t_fp = bl.playlist_fingerprint(t_node)
        v_keys = list(v_node.playlist.track_keys)
        t_keys = list(t_node.playlist.track_keys)
        in_sync = (v_keys == t_keys)
        key = "/".join(path)
        owner = bl.attribute(key, v_fp, t_fp, in_sync, view, kind="playlist")
        # The membership snapshot is the last *agreed* state. Refreshing it on
        # every pass would absorb a removal immediately and the tombstone would
        # vanish on the next refresh, so it only moves once the two sides carry
        # the same members (order may still differ).
        if set(v_keys) == set(t_keys):
            view.set_members(key, v_keys)
        if owner:
            owners[path] = owner
    return owners


# Divergence shades (see ui/status_squares.py for the colours). The marker
# lives on the *un-edited* copy of a playlist and previews what the next sync
# will do to it: LOSING = a track it will lose, GAINING = one it will gain.
LOSING = "grey"         # this side still has a track the other side removed
GAINING = "green2"      # this side lacks a track the other side added


def playlist_divergence(lib_a: Library, lib_b: Library, baseline,
                        yield_fn=None) -> dict:
    """Per playlist, the sync-preview markers for each side's *own* view.

    Returns ``{path_ci: {source: {"losing": [keys], "gaining": [keys]}}}``.
    Derived purely from the last-agreed membership snapshot, so the marker always
    lands on the copy that did NOT make the change (the edited side's sets come
    out empty):

        losing  = keys this side HAS, that were in the snapshot, that the other
                  side no longer has  -> the other side removed them; a sync will
                  remove them here too (grey, recolours a real member row).
        gaining = keys this side LACKS, that were NOT in the snapshot, that the
                  other side now has  -> the other side added them; a sync will
                  add them here (deep-green, an appended ghost row).

    ``yield_fn`` is called per playlist so a big divergence pass stays smooth.
    """
    view = _view(baseline, lib_a, lib_b)
    a_index = lib_a.playlist_index()
    b_index = lib_b.playlist_index()
    out: dict = {}
    for i, (path, v_node) in enumerate(a_index.items()):
        if yield_fn is not None and (i & 0x1F) == 0:
            yield_fn()
        t_node = b_index.get(path)
        if t_node is None:
            continue
        key = "/".join(path)
        snapshot = set(view.get_members(key))
        v_keys = list(v_node.playlist.track_keys)
        t_keys = list(t_node.playlist.track_keys)

        def preview(mine, theirs):
            mine_set, theirs_set = set(mine), set(theirs)
            gaining, seen = [], set()
            for k in theirs:                       # one ghost per incoming key
                if k not in snapshot and k not in mine_set and k not in seen:
                    seen.add(k)
                    gaining.append(k)
            return {"losing": [k for k in mine if k in snapshot and k not in theirs_set],
                    "gaining": gaining}

        # Keyed by each library's own id, so a third program's markers land
        # under its own name rather than borrowing one of the original two.
        out[path] = {lib_a.source: preview(v_keys, t_keys),
                     lib_b.source: preview(t_keys, v_keys)}
    return out


def diff_tracks(track_keys, source: Library, target: Library,
                changed_by: dict | None = None, yield_fn=None,
                reference: str = "") -> Report:
    """Diff each key's sync state from source into target (see THE LAW).

    ``changed_by`` is ``{track_key: set(programs that changed it)}``, aggregated
    across every pair by the caller -- one pair cannot report that some *third*
    program is the one that moved. ``yield_fn`` (optional) is called every ~256
    keys to keep a caller's event loop -- and audio -- alive during a big diff.
    """
    return diff_tracks_against(track_keys, source, [target], changed_by,
                               yield_fn, reference)


def diff_tracks_against(track_keys, source: Library, targets: list,
                        changed_by: dict | None = None, yield_fn=None,
                        reference: str = "") -> Report:
    """As :func:`diff_tracks`, against several targets at once.

    Used for the reference program, which has no single library to be compared
    with: a track is green only when every program that has it agrees, and black
    when none of them has it yet.
    """
    status: dict[str, str] = {}
    for i, key in enumerate(track_keys):
        src = source.tracks.get(key)
        if src is None:
            status[key] = RED           # playlist references a track we can't find
            continue
        found = [t.tracks[key] for t in targets if key in t.tracks]
        if not found:
            status[key] = BLACK         # not in any target collection yet
        elif all(content_matches(src, tgt) for tgt in found):
            status[key] = GREEN
        else:
            # A real difference; the shape says whose (see model/baseline.py).
            status[key] = changed_glyph(source.source, reference,
                                        (changed_by or {}).get(key))
        if yield_fn is not None and (i & 0x7F) == 0:
            yield_fn()
    return Report(track_status=status)


def merge_owner(changed_by: dict, key, owner: str, a: str, b: str) -> None:
    """Fold one pair's verdict for ``key`` into the cross-program tally.

    ``owner`` is an adapter id, ``"both"`` or ``"unknown"``; a pair only ever
    names its own two members, so the union over every pair is what lets a copy
    know that a program it is not paired with is the one that moved.

    ``unknown`` folds in the same way as ``both``: with no evidence either way,
    each side has to be shown as possibly-changed. They differ in what a *sync*
    does about it, not in what a square looks like.
    """
    if not owner:
        return
    who = {a, b} if owner in (bl_BOTH, bl_UNKNOWN) else {owner}
    changed_by.setdefault(key, set()).update(who)
