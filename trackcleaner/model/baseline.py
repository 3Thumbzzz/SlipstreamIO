"""Change attribution: remember what each side looked like, so we can tell
*which* program actually edited a track.

Timestamps cannot answer this. VirtualDJ's ``Infos/@LastModified`` means "VDJ
last touched this record" -- it moves on rescan, on play, on app open, and is
bulk-stamped across tens of thousands of songs at once. A cue genuinely edited
in Traktor therefore looks *older* than an untouched VirtualDJ record.

So instead of trusting clocks we remember, per track and per side, a fingerprint
of that side's own content. On the next refresh, whichever side's fingerprint
moved is the side that was edited.

Each side is only ever compared against **its own** previous fingerprint, so the
legitimate Class-A +/-51.536 ms offset between the two programs never registers
as a change.

**Attribution is always between a pair of programs**, because "who changed it"
only means something relative to something else. With more than two programs
loaded there is a pair per (program, reference), and each pair keeps its own
fingerprints -- sharing one set would have each pair's refresh overwrite the
last's, and every track would look permanently conflicted.

Storage is a compact CSV (``pair,kind,key,a_fp,b_fp,owner,members``) written next
to the app, mirroring the ``StartTimeCache`` pattern in
:mod:`trackcleaner.model.cues`. A file written before pairs existed has no
``pair`` column and no ``a_fp``/``b_fp``; those rows are read as the
VirtualDJ<->Traktor pair, so an existing baseline keeps its history instead of
everything going yellow once on upgrade.
"""

from __future__ import annotations

import csv
import hashlib
import os
from dataclasses import dataclass
from typing import Optional

BOTH = "both"
#: Diverged, but never compared while the two agreed -- so nothing can be
#: deduced about who moved. Distinct from BOTH, which is a real conflict.
UNKNOWN = "unknown"

# The pair a pre-pair baseline file must have been about.
LEGACY_A, LEGACY_B = "traktor", "vdj"

# Retained: callers still compare an owner against these two by name.
VDJ_OWNER = "vdj"
TRAKTOR_OWNER = "traktor"

_TRACK = "track"
_PLAYLIST = "playlist"


def pair_id(a: str, b: str) -> str:
    """Stable name for a program pair, order-independent -- ``(vdj, traktor)``
    and ``(traktor, vdj)`` are the same comparison and must not accumulate two
    sets of fingerprints."""
    lo, hi = sorted((a, b))
    return f"{lo}|{hi}"


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def fingerprint(track) -> str:
    """A stable hash of one side's cue set + compared metadata.

    Positions are rounded to milliseconds so float noise never looks like an
    edit. Import is local to avoid a circular import with ``status``.
    """
    from .status import COMPARED_FIELDS

    pois = sorted(
        (p.kind, -1 if p.num is None else p.num, round(p.pos, 3))
        for p in (track.pois or ())
    )
    meta = [(getattr(track, f, "") or "").strip().casefold() for f in COMPARED_FIELDS]
    return _short_hash(repr(pois) + "|" + repr(meta))


def playlist_fingerprint(node) -> str:
    """Hash of a playlist's ordered membership (order changes count)."""
    keys = list(node.playlist.track_keys) if node.playlist else []
    return _short_hash(repr(keys))


# Playlist membership is stored joined by "|", which cannot occur in a Windows
# path, so it never collides with a track key.
_MEMBER_SEP = "|"


@dataclass
class _Row:
    """One key's record for one pair. ``a_fp``/``b_fp`` belong to the pair's two
    programs in sorted id order -- ``_PairView`` is what maps them back to the
    caller's own sense of "mine" and "theirs"."""

    a_fp: str = ""
    b_fp: str = ""
    owner: str = ""
    members: str = ""      # playlist rows only: the last *agreed* membership


class Baseline:
    """Persistent per-pair, per-key record of both sides' last-seen fingerprints
    and the attributed owner of any current divergence.

    Callers do not use this directly for attribution -- they take a
    :meth:`for_pair` view, which fixes which two programs are being compared.
    """

    def __init__(self, path: str = ""):
        self.path = path
        # {pair_id: {key: _Row}}
        self._tracks: dict[str, dict[str, _Row]] = {}
        self._playlists: dict[str, dict[str, _Row]] = {}
        self._dirty = False

    # ------------------------- persistence -------------------------
    def load(self) -> "Baseline":
        if not self.path or not os.path.exists(self.path):
            return self
        legacy = pair_id(LEGACY_A, LEGACY_B)
        try:
            with open(self.path, "r", newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    key = (row.get("key") or "").strip()
                    if not key:
                        continue
                    store = (self._playlists if row.get("kind") == _PLAYLIST
                             else self._tracks)
                    pair = (row.get("pair") or "").strip() or legacy
                    if "a_fp" in row or "b_fp" in row:
                        a_fp = row.get("a_fp", "") or ""
                        b_fp = row.get("b_fp", "") or ""
                    else:
                        # Pre-pair file: its columns were named for the only two
                        # programs there were. LEGACY_A/B are sorted, so traktor
                        # is the "a" side.
                        a_fp = row.get("traktor_fp", "") or ""
                        b_fp = row.get("vdj_fp", "") or ""
                    store.setdefault(pair, {})[key] = _Row(
                        a_fp, b_fp, row.get("owner", "") or "",
                        row.get("members", "") or "")
        except (OSError, csv.Error):
            pass
        return self

    def save(self) -> None:
        if not self.path or not self._dirty:
            return
        try:
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["pair", "kind", "key", "a_fp", "b_fp", "owner", "members"])
                for kind, store in ((_TRACK, self._tracks),
                                    (_PLAYLIST, self._playlists)):
                    for pair, bucket in store.items():
                        for key, row in bucket.items():
                            w.writerow([pair, kind, key, row.a_fp, row.b_fp,
                                        row.owner, row.members])
            self._dirty = False
        except OSError:
            pass

    # --------------------------- access ----------------------------
    def for_pair(self, a: str, b: str) -> "_PairView":
        """A view fixed to comparing ``a`` against ``b``. ``a`` and ``b`` are the
        caller's own sense of "this side" and "the other side"; storage order is
        normalised so the view is the same regardless of which way round."""
        return _PairView(self, a, b)

    def _bucket(self, pair: str, kind: str) -> dict[str, _Row]:
        store = self._playlists if kind == _PLAYLIST else self._tracks
        return store.setdefault(pair, {})

    def pairs(self) -> list[str]:
        return sorted(set(self._tracks) | set(self._playlists))

    def __len__(self) -> int:
        return (sum(len(b) for b in self._tracks.values())
                + sum(len(b) for b in self._playlists.values()))


@dataclass
class _View:
    """What :meth:`_PairView.get` hands back: fingerprints named from the
    caller's side, not from storage order."""

    mine_fp: str = ""
    theirs_fp: str = ""
    owner: str = ""
    members: str = ""


class _PairView:
    """:class:`Baseline` scoped to one program pair.

    Everything above this reasons in terms of "my side" and "the other side";
    everything below stores in sorted-id order. Doing the flip in one place is
    what stops ``(vdj, traktor)`` and ``(traktor, vdj)`` becoming two histories.
    """

    def __init__(self, store: Baseline, a: str, b: str):
        self.store = store
        self.a, self.b = a, b
        self.pair = pair_id(a, b)
        # True when the caller's "a" is the second of the sorted pair.
        self._flipped = a != sorted((a, b))[0]

    def get(self, key: str, kind: str = _TRACK) -> Optional[_View]:
        row = self.store._bucket(self.pair, kind).get(key)
        if row is None:
            return None
        mine, theirs = ((row.b_fp, row.a_fp) if self._flipped
                        else (row.a_fp, row.b_fp))
        return _View(mine, theirs, row.owner, row.members)

    def set(self, key: str, mine_fp: str, theirs_fp: str, owner: str,
            kind: str = _TRACK) -> None:
        bucket = self.store._bucket(self.pair, kind)
        prev = bucket.get(key)
        a_fp, b_fp = ((theirs_fp, mine_fp) if self._flipped
                      else (mine_fp, theirs_fp))
        bucket[key] = _Row(a_fp, b_fp, owner or "",
                           prev.members if prev else "")
        self.store._dirty = True

    # ---- playlist membership snapshot ("last agreed state") ----
    def get_members(self, key: str) -> list[str]:
        row = self.store._bucket(self.pair, _PLAYLIST).get(key)
        if row is None or not row.members:
            return []
        return row.members.split(_MEMBER_SEP)

    def has_members(self, key: str) -> bool:
        row = self.store._bucket(self.pair, _PLAYLIST).get(key)
        return row is not None and bool(row.members)

    def set_members(self, key: str, keys: list[str]) -> None:
        bucket = self.store._bucket(self.pair, _PLAYLIST)
        row = bucket.get(key) or _Row()
        row.members = _MEMBER_SEP.join(keys)
        bucket[key] = row
        self.store._dirty = True

    def save(self) -> None:
        self.store.save()


def record_edit(store: Baseline, key: str, editor: str, peers,
                libraries: dict, kind: str = _TRACK) -> None:
    """Note that ``editor`` just changed ``key``, for every pair it belongs to.

    An edit made **inside the app** knows exactly who made it and where, which is
    strictly better information than comparing fingerprints can reconstruct
    afterwards. Without this the next sync sees no owner at all: attribution only
    runs on a full diff, and a track that was in sync before the edit is absent
    from the owners map entirely -- so UPDATE, which asks "does the source own
    this change?", skipped exactly the tracks the user had just edited.

    The editor's own fingerprint is deliberately NOT written: leaving it stale is
    what lets the next real diff still see that this side moved. Only the owner
    verdict is recorded.
    """
    edited = libraries.get(editor)
    if edited is None or key not in edited.tracks:
        return
    for peer in peers:
        if peer == editor or libraries.get(peer) is None:
            continue
        view = store.for_pair(editor, peer)
        prev = view.get(key, kind)
        view.set(key,
                 prev.mine_fp if prev else "",
                 prev.theirs_fp if prev else "",
                 editor, kind)


def attribute(key: str, mine_fp: str, theirs_fp: str, in_sync: bool,
              view: _PairView, kind: str = _TRACK) -> Optional[str]:
    """Decide who owns a divergence, and record the new fingerprints.

    Returns ``None`` (in sync), one of the pair's two program ids, ``"both"`` or
    ``"unknown"``.

        in sync                  -> owner cleared
        only one side's fp moved -> that side owns it
        both moved               -> "both" (a real conflict)
        neither moved            -> keep the stored owner (sticky)
        no history               -> "unknown" (never compared while they agreed)

    **"Unknown" and "both" were the same answer until now, and that was
    misleading in a way that mattered.** Attribution can only name a culprit by
    comparing each side against a fingerprint from a moment when the two
    *agreed*. A track whose first appearance here is already diverged has no such
    moment, so nothing can be deduced -- which is not the same claim as "both
    sides edited it". The verdict is then stored and sticks (nothing moves
    afterwards), so a first-contact library reported conflicts everywhere and
    Sync Update declined every one of them citing the other side.
    """
    prev = view.get(key, kind)
    if in_sync:
        view.set(key, mine_fp, theirs_fp, "", kind)
        return None

    if prev is None or not (prev.mine_fp and prev.theirs_fp):
        owner = UNKNOWN                    # never compared while they agreed
    else:
        mine_changed = mine_fp != prev.mine_fp
        theirs_changed = theirs_fp != prev.theirs_fp
        if mine_changed and theirs_changed:
            owner = BOTH
        elif mine_changed:
            owner = view.a
        elif theirs_changed:
            owner = view.b
        else:
            owner = prev.owner or BOTH     # nothing moved -> keep prior verdict
    view.set(key, mine_fp, theirs_fp, owner, kind)
    return owner
