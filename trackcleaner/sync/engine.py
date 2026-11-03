"""The sync engine: orchestrates the three playlist-sync modes across the
writers, always backing up the target file first.

**Cue positions go through the native layer.** A position is read out of the
source on its own timeline, converted to canonical time (seconds from the first
decoded audio sample), and converted back onto the target's — see
:func:`_retimed_pois`. No pairwise Traktor<->VirtualDJ rule survives in here.

**Every write goes through an adapter.** Nothing in here names a program or a
file format: the target's adapter supplies the collection path to back up, the
job that writes a track, and the jobs that create or rewrite a playlist. Adding
a program needs no edit to this module.

The adapters' jobs are *run inline* rather than queued, because a sync is
synchronous by design -- the busy overlay and the reload that follows it both
assume the writes have landed by the time the call returns.

Modes (confirmed with user):
    NEW       Add tracks present in source but missing from target's playlist;
              sync cues only for those newly-added tracks. Existing target
              tracks/cues are left alone.
    UPDATE    Add new tracks (as above) AND, for tracks already in both, push
              cues from source -> target only when source is newer
              (status.Report / recency guard). If target is newer, skip it.
    OVERWRITE Force target playlist membership and cues to match source
              unconditionally (caller must have already confirmed with the
              user — this module does not prompt).

A track missing from the *target collection entirely* is always added there
(with whatever metadata is available) as part of adding it to the playlist —
per the confirmed behaviour, once added+synced it should read as fully
synced (green), not flagged.
"""

from __future__ import annotations

import time
from contextlib import ExitStack
from dataclasses import dataclass, field

from ..adapters import LibraryAdapter, build_adapters
from ..io import backup, synclog
from ..io.synclog import shown
from ..config import paths as paths_mod
from ..model import baseline as bl, matching, status as status_mod
from ..model.cues import StartTimeCache, grid_anchor_at_or_after_zero
# No program constants and no format writers: every write goes through the
# adapter for whichever program is the target.
from ..model.library import Library, Poi, Track, TreeNode, path_ci

NEW = "new"
UPDATE = "update"
OVERWRITE = "overwrite"

#: Why an UPDATE declined to write a track. Ids, so the UI owns the wording.
SKIP_TARGET_OWNS = "target_owns"
SKIP_CONFLICT = "conflict"
SKIP_UNKNOWN = "unknown_history"


@dataclass
class SyncResult:
    backup_path: str = ""
    tracks_added: int = 0            # entries created in the target COLLECTION
    tracks_cue_synced: int = 0
    tracks_skipped: int = 0
    playlists_created: int = 0        # target playlists that did not exist before
    playlist_entries_added: int = 0   # members added to the target playlist
    playlist_entries_removed: int = 0  # members removed from the target playlist
    #: Why the skipped tracks were skipped, keyed by reason. Split out because
    #: one total reported as "the other side owns the change" was wrong for two
    #: of the three cases, and reading it next to a square that says "you changed
    #: this" is what made the app look like it disagreed with itself.
    skipped_by_reason: dict = field(default_factory=dict)
    #: Tracks whose tags could not be written because the target's writer found
    #: no matching entry. Reported rather than swallowed: the in-memory copy is
    #: deliberately left alone for these, so they keep showing as unsynced, and
    #: the user needs to know why rather than watching a sync do nothing.
    unwritten_tags: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class _Plan:
    """Everything a run has decided to write, grouped by the pass that writes it.

    One dict per kind, because that is what a batched write takes: the loop
    decides, and each of these becomes exactly one walk of the collection
    instead of one walk per track.
    """
    #: key -> the retimed copy whose cues go to the target
    cues: dict = field(default_factory=dict)
    #: key -> the source Track itself, for the memory update afterwards
    sources: dict = field(default_factory=dict)
    #: key -> the log line indices this track's write outcomes belong on. A
    #: *list*, because a playlist may hold the same track more than once and
    #: each occurrence logs its own decision block: a single index kept only the
    #: last, leaving the earlier line reserved forever, which used to raise
    #: mid-write and cost the whole log file.
    slots: dict = field(default_factory=dict)
    tags: dict = field(default_factory=dict)          # key -> fields, target
    keys: dict = field(default_factory=dict)          # key -> 0..23, target
    source_tags: dict = field(default_factory=dict)   # key -> fields, backfill
    source_keys: dict = field(default_factory=dict)   # key -> 0..23, backfill


def _evented(name: str, **fields) -> str:
    """One log line in exactly the shape ``SyncLog.event`` renders, built here
    because a batched write fills its per-track lines in after the fact -- which
    is what keeps a log reading as it always did rather than moving every
    outcome into a section of its own."""
    rendered = "  ".join(f"{k}={shown(v)}" for k, v in fields.items())
    return f"  {name}" + (f"  {rendered}" if rendered else "")


def _tag_fields(fields: dict) -> dict:
    return {"fields": ", ".join(f"{k}={shown(v)}" for k, v in fields.items())}


def _key_fields(adapter, program: str):
    """How a key write describes itself: the canonical number and what that
    program will show for it, so a log says "22 -> A#m" rather than just 22."""
    def describe(key_value: int) -> dict:
        return {"key_value": key_value,
                "shown_as": adapter.display_key(
                    Track(key="", path="", source=program, key_value=key_value))}
    return describe


def _target_provably_newer(src, tgt) -> bool:
    """True only when BOTH modification times are known and the target's is
    strictly newer.

    UPDATE mode converges on the source ("source wins") whenever the cue sets
    differ, *except* when we can actually prove the other software has newer
    cue data — that is the "if other software track data has newer cues then do
    not update" rule. An unknown timestamp is not proof, so it does not block
    the write (otherwise nothing ever syncs and deletions never propagate).
    """
    if src is None or tgt is None or src.modified is None or tgt.modified is None:
        return False
    return tgt.modified > src.modified


def _bpm_of(track) -> float | None:
    return getattr(track, "bpm", None) or None


def _retimed_pois(track: Track, source: LibraryAdapter,
                  target: LibraryAdapter, fallback_bpm=None) -> list[Poi]:
    """``track``'s cues expressed on the *target's* timeline, via the canonical
    one: ``source -> native -> target``.

    This is the whole point of the native layer in two conversions. Each adapter
    knows only how its own stored numbers relate to true audio time (seconds from
    the first decoded audio sample), so neither has to know the other exists --
    which is what makes a third program free instead of another pairwise
    correction to derive and verify.

    Behaviourally this is the same verified Class-A/B correction as before, now
    expressed as a composition rather than as a Traktor<->VirtualDJ special case;
    ``tests/test_adapter_timeline.py`` pins the composed result against the
    original pairwise functions exactly. An adapter with no start-time cache
    reports a zero offset, so unknown files are still never shifted
    (HANDOFF.md, "Cue timing").

    **A grid marker is not floored on the way.** The floor is right for a cue
    and wrong for a grid anchor, which sets where every beat line falls: the
    halfway floor turned VirtualDJ's -4.9 ms into Traktor's 51.5 ms where 46.7
    ms was right, and its -1.588 s into a grid half a beat off. A grid is moved
    by both offsets in one step, and only an anchor that still lands below zero
    is stepped forward by whole bars (``cues.grid_anchor_at_or_after_zero``).
    The bar is the source's tempo, else ``fallback_bpm`` -- the target copy's:
    5 of the 26 grids the 31 Oct sync floored had a tempo only in Traktor.
    """
    if source is target:
        return list(track.pois)
    out = []
    for p in track.pois:
        if p.kind == "beatgrid":
            pos = (p.pos + source.timeline_offset(track.path)
                   - target.timeline_offset(track.path))
            pos = grid_anchor_at_or_after_zero(pos, track.bpm or fallback_bpm)
        else:
            pos = target.from_native_pos(source.to_native_pos(p.pos, track.path),
                                         track.path)
        out.append(Poi(pos, p.num, p.kind))
    return out


class SyncEngine:
    """Bound to the loaded libraries + a start_time cache; call
    :meth:`sync_playlist` / :meth:`sync_track` from the UI's context-menu
    handlers.

    ``libraries`` is ``{adapter_id: Library}`` -- the same dict the window holds,
    covering however many programs the session has on. Every sync names its
    ``source`` and its ``target`` explicitly; with more than two programs on
    there is no such thing as "the other one".
    """

    def __init__(self, libraries: dict, paths,
                start_time_cache: StartTimeCache | None = None,
                owners: dict | None = None, adapters: dict | None = None):
        self.libraries = libraries
        self.paths = paths
        self.start_time_cache = start_time_cache
        # {pair_id: {track_key: owner}} from status.attribute_changes, one entry
        # per program pair that has been diffed. None means attribution is
        # unavailable at all (see _update_should_write).
        self.owners = owners
        # Adapters answer the timeline questions, so cue positions convert
        # source -> native -> target rather than by a pairwise rule in here. The
        # window passes its own (already holding the loaded cache); building them
        # when it doesn't keeps direct engine use working, and is equivalent
        # because the cache is all they need.
        self.adapters = adapters if adapters is not None else build_adapters(paths)
        # Filled by _apply_written when a write finds no entry; copied onto
        # each SyncResult and reset per operation.
        self.unwritten_tags: list[str] = []
        # Diagnostic sink. The null one records nothing, so the engine needs no
        # "if logging" branches and a sync behaves identically either way -- the
        # Debug toggles swap this and change nothing else.
        self.log = synclog.NullLog()
        # Baseline held for the length of one run; see _baseline_store. None =
        # not loaded, False = load failed, otherwise the store.
        self._baseline_cache = None
        #: How many times the baseline was read this run. Asserted in the tests:
        #: it is a performance contract, and nothing else would catch a
        #: regression back to once-per-track.
        self.baseline_loads = 0
        #: Timings for the current run. Set here as well as in _begin_run because
        #: the write paths are reachable directly (drag-add, tests) without going
        #: through a sync entry point.
        self.timing = synclog.Timings()
        for adapter in self.adapters.values():
            if adapter.start_times is None:
                adapter.start_times = start_time_cache

    def adapter_for(self, source: str) -> LibraryAdapter:
        return self.adapters[source]

    def library_for(self, source: str) -> Library:
        return self.libraries.get(source)

    def default_target(self, source: str) -> str | None:
        """The program a sync goes to when the caller doesn't say.

        Only meaningful with exactly two libraries loaded. With three there is
        no defensible default -- picking one would silently write to a program
        the user did not name -- so this returns ``None`` and the caller must ask.
        """
        others = [aid for aid in self.libraries if aid != source]
        return others[0] if len(others) == 1 else None

    def _begin_run(self) -> None:
        """Reset the per-run caches and counters. Called by every entry point,
        so a second sync in the same session cannot read a stale baseline."""
        self._baseline_cache = None
        self.baseline_loads = 0
        self.timing = synclog.Timings()
        for adapter in self.adapters.values():
            adapter.collection_reads = adapter.collection_writes = 0

    def _baseline_store(self):
        """The baseline, loaded **once per run**.

        This was reloaded and re-parsed for every single track, which on a real
        library cost roughly half a second each: two logged runs that wrote
        nothing at all still took 68 s for 165 tracks and 26 s for 48. A
        diagnostic has to be cheap enough not to change the thing it measures.
        """
        if self._baseline_cache is None:
            try:
                self._baseline_cache = bl.Baseline(paths_mod.baseline_path()).load()
            except Exception:                    # noqa: BLE001 - never break a sync
                self._baseline_cache = False     # "tried and failed", not "untried"
            self.baseline_loads += 1
        return self._baseline_cache or None

    def _ever_agreed(self, key: str, source: str, target: str):
        """Whether the baseline holds fingerprints for **both** sides of this
        pair -- i.e. whether attribution ever had a moment of agreement to reason
        from. ``None`` when the store cannot be read.

        Diagnostics only, but the diagnostic that matters: without it a verdict
        of "both" cannot be told apart from "never compared", and that ambiguity
        cost a full round of misdiagnosis.
        """
        store = self._baseline_store()
        if store is None:
            return None
        try:
            row = store.for_pair(source, target).get(key)
        except Exception:                        # noqa: BLE001
            return None
        return bool(row and row.mine_fp and row.theirs_fp)

    @staticmethod
    def carried_fields(src_track, tgt_t, fields) -> tuple:
        """What a sync should write, and **in which direction**, per field.

        Returns ``(to_target, to_source)``. A blank is treated as *missing data*
        rather than as a value:

            source "Funk", target ""/"Other"  -> write "Funk" to the target
            source "",     target "Other"     -> write "Other" back to the SOURCE
            both blank                        -> nothing

        The backfill is what makes these tracks reachable. Refusing to erase was
        right -- VirtualDJ carries no comment for most tracks, and syncing one
        into Traktor was turning ``COMMENT="Jazz"`` into ``COMMENT=""`` -- but
        merely declining left the two sides permanently different, and therefore
        permanently yellow, with nothing the user could do. Filling the gap
        instead loses nothing and actually converges.

        One comparison produces both directions, so the write and the decision to
        write cannot disagree: a difference the gate sees but the writer declines
        is a track rewritten on every sync forever, the trap ``comment2`` needed
        :meth:`syncable_fields` for.
        """
        to_target, to_source = {}, {}
        for field in fields:
            src = (getattr(src_track, field, "") or "").strip()
            tgt = ((getattr(tgt_t, field, "") or "").strip()
                   if tgt_t is not None else "")
            # The same comparison the status model makes, Unicode spelling
            # included, so a field it calls equal is never rewritten.
            if src and status_mod._norm(src) != status_mod._norm(tgt):
                to_target[field] = getattr(src_track, field, "") or ""
            elif not src and tgt:
                to_source[field] = getattr(tgt_t, field, "") or ""
        return to_target, to_source

    @staticmethod
    def carried_key(src_track, tgt_t) -> tuple:
        """The musical key to write, and in which direction:
        ``(to_target, to_source)``, each the canonical 0..23 value or None.

        **Only ever fills a blank side.** Two programs that both hold a key are
        left alone even when they disagree, which is deliberate on two counts.
        A key each side analysed for itself is not a conflict this app can
        settle, and the spellings differ harmlessly: VirtualDJ writes "Ebm" where
        we would render "D#m", so "converging" 1,771 tracks in the reported
        library would rewrite them to say exactly what they already say.

        Filling the empty side, on the other hand, can only add -- the same
        argument :meth:`_backfill` rests on -- and it is the whole of the
        reported bug: 60,120 of 81,107 VirtualDJ songs carry no key at all, and
        nothing in the compare-then-write loop was ever going to give them one,
        because the key is deliberately not a compared field.
        """
        src = getattr(src_track, "key_value", None)
        tgt = getattr(tgt_t, "key_value", None) if tgt_t is not None else None
        if src is not None and tgt is None:
            return src, None
        if src is None and tgt is not None:
            return None, tgt
        return None, None

    def content_in_sync(self, src_track, tgt_t, fields) -> bool:
        """Whether a sync has anything left to do for this track, in either
        direction. Cues are compared as ever; metadata and the key only count
        when there is an actual write to make."""
        to_target, to_source = self.carried_fields(src_track, tgt_t, fields)
        if to_target or to_source:
            return False
        if any(v is not None for v in self.carried_key(src_track, tgt_t)):
            return False
        return status_mod.cues_equivalent(src_track, tgt_t)

    def _log_track_decision(self, key, src_track, tgt_t, source, target,
                            fields, should_write, reason) -> None:
        """Everything about one track's write decision.

        Only assembled when a log is actually attached: the field-by-field
        comparison and the attribution lookup are real work, and a sync of a
        large playlist should not pay for them when nobody is reading.
        """
        if not self.log.enabled:
            return
        owners = self.owners_for(source, target)
        owner = None if owners is None else owners.get(key)
        self.log.event(
            "track", title=getattr(src_track, "title", ""),
            decision="WRITE" if should_write else "skip", reason=reason)
        self.log.line(f"      key={key}")
        self.log.event(
            "  state", in_target_collection=bool(tgt_t is not None
                                                 and tgt_t.in_database),
            cues_equivalent=(tgt_t is not None
                             and status_mod.cues_equivalent(src_track, tgt_t)),
            metadata_matches=(tgt_t is not None
                              and status_mod.metadata_matches(src_track, tgt_t,
                                                              fields)))
        # `has_verdict` alone was actively misleading: it is true from the first
        # diff onward, which reads as "we know who changed it" when the verdict
        # may be the no-history one. So the *stored baseline row* is reported
        # too -- that is what attribution actually had to work from.
        self.log.event("  attribution", owner=owner,
                       ever_agreed=self._ever_agreed(key, source, target),
                       attribution_available=owners is not None)
        if tgt_t is not None:
            self.log.field_comparison(fields, src_track, tgt_t)

    def syncable_fields(self, source: str, target: str) -> tuple:
        """The metadata a sync between these two can actually carry.

        The fields both adapters declare they can store, intersected with what
        the status model compares. Asked of the adapters rather than listed here,
        so nothing outside ``adapters/`` learns which program keeps what.

        This is what the write gate compares on, and the distinction matters: a
        field that is **compared but not writable** is one a sync would rewrite
        forever without ever converging. ``comment2`` is exactly that -- Traktor
        has one and VirtualDJ has nowhere to put it -- so such a track is left
        alone (and stays yellow) instead of being rewritten on every sync.
        """
        src = set(getattr(self.adapter_for(source), "EDITABLE_FIELDS", ()))
        tgt = set(getattr(self.adapter_for(target), "EDITABLE_FIELDS", ()))
        return tuple(f for f in status_mod.COMPARED_FIELDS if f in src and f in tgt)

    def owners_for(self, source: str, target: str) -> dict | None:
        """Attribution for one program pair, or ``None`` when there is none.

        ``self.owners`` is ``{pair_id: {key: owner}}``. It has to be per pair for
        the same reason the baseline does: "who changed this" is only meaningful
        relative to something, and a track can be owned by one program against
        one peer and by its peer against another. A flat map would let one pair's
        verdict decide another pair's write.
        """
        if self.owners is None:
            return None
        # A pair with no recorded verdicts is NOT "no attribution": it is
        # attribution that found nothing to say. Returning {} makes the caller
        # skip, which is the safe direction -- falling through to the timestamp
        # rule would overwrite the target on a signal we know is untrustworthy.
        return self.owners.get(bl.pair_id(source, target), {})

    def _update_should_write(self, key: str, source: str, target: str,
                             src_track, tgt_t) -> bool:
        """UPDATE-mode guard: may we overwrite the target's cues?

        Driven by change *attribution* rather than timestamps, because
        VirtualDJ's LastModified is not a "cues changed" signal (see
        model/baseline.py). What shows yellow is therefore what will sync:

            owner == this side -> write   (this side made the change)
            owner == other side-> skip    (don't clobber their edit)
            owner == "both"    -> skip    (conflict; Overwrite forces it)
            owner missing      -> skip    (no history yet; safe)

        When there is no attribution for this pair the caller has none available
        (direct engine use, or a pair that has never been diffed), so we fall
        back to the old timestamp rule.
        """
        pair_owners = self.owners_for(source, target)
        if pair_owners is None:
            return not _target_provably_newer(src_track, tgt_t), ""
        owner = pair_owners.get(key)
        if owner == source:
            return True, ""
        if owner == bl.UNKNOWN or owner is None:
            # Never compared while the two agreed, so nothing can be deduced.
            # Still declined -- the guarantee is worth more than the convenience
            # -- but it is emphatically NOT "the other side owns the change",
            # which is what it used to be reported as.
            return False, SKIP_UNKNOWN
        if owner == bl.BOTH:
            return False, SKIP_CONFLICT
        return False, SKIP_TARGET_OWNS

    # ==================================================================
    def sync_playlist(self, node: TreeNode, source: str, mode: str,
                      target: str | None = None) -> SyncResult:
        """Sync one playlist node from ``source`` into ``target``.

        ``target`` defaults to the only other loaded program, which is the
        two-program case the UI has always had. With more loaded there is no
        default and the caller must name one.
        """
        result = SyncResult()
        target = target or self.default_target(source)
        if target is None:
            result.errors.append(
                "No sync target: more than one other program is loaded, "
                "so the target has to be named.")
            return result
        source_lib = self.library_for(source)
        target_lib = self.library_for(target)
        if source_lib is None or target_lib is None:
            result.errors.append(f"Not loaded: {source if source_lib is None else target}")
            return result

        pl = node.playlist
        if pl is None:
            return result

        clash = self._name_collision(node, source_lib)
        if clash:
            # Refused before the backup, so a playlist that cannot be written
            # does not even leave a side effect behind. Both twins are refused,
            # not just the loser: "the other one worked" is precisely the
            # illusion that let this destroy 511 tracks silently.
            result.errors.append(clash)
            self.log.section(f"Run: {mode} {source} -> {target}")
            self.log.event("playlist", name=node.name,
                           node_path="/".join(node.node_path))
            self.log.event("REFUSED", reason=clash)
            return result

        target_path = self.adapter_for(target).collection_path
        self.unwritten_tags = []
        # Started before the backup on purpose: copying a 52 MB collection is
        # part of what the user waits through, and timing that began after it
        # simply did not show the second and a half it costs.
        self._begin_run()
        with self.timing.phase("backup"):
            result.backup_path = backup.make_backup(target_path, target)

        self.log.section(f"Run: {mode} {source} -> {target}")
        self.log.event("playlist", name=node.name,
                       node_path="/".join(node.node_path), tracks=len(pl.track_keys))
        self.log.note_file("source collection",
                           self.adapter_for(source).collection_path)
        self.log.note_file("target collection", target_path)
        self.log.event("backup", path=result.backup_path)
        # Before AND after: a file that parsed before this run and not after is
        # this run's doing, which is a far stronger statement than "it is broken".
        with self.timing.phase("parse check (before)"):
            self.log.check_parses("before", target_path)

        # Membership: which source tracks are missing from the target *playlist*.
        with self.timing.phase("read target playlist membership"):
            existing_target_keys = self._target_playlist_keys(node, target_lib, target)
        playlist_new_members = [k for k in pl.track_keys if k not in existing_target_keys]
        self.log.section("Playlist membership, before")
        self.log.event("counts", source_tracks=len(pl.track_keys),
                       already_in_target=len(existing_target_keys),
                       to_add=len(playlist_new_members))

        # Cue writing keys off *collection* membership, not playlist membership,
        # so adding a playlist of tracks that already live in the target
        # collection never clobbers their existing cues (unless the mode calls
        # for it). See plan Bug C.
        #
        # The "already in sync" test below covers **cues AND metadata**. Testing
        # cues alone short-circuited a tag-only difference to "nothing to do"
        # before the mode branches were even reached, so an edited comment could
        # not be pushed by any mode, in either direction -- it simply stayed
        # yellow forever.
        fields = self.syncable_fields(source, target)
        self.log.section(f"Tracks ({len(pl.track_keys)}), syncable fields: "
                         f"{', '.join(fields)}")
        # ------------------------------------------------------------------
        # One read and one write per collection file for the WHOLE run, not
        # per track. Each write used to re-read the file, re-run the regex and
        # re-parse the document to validate it: 118 of those cost 292 of one
        # 329-second run on a real 43 MB database.xml. The mutations inside are
        # unchanged and still applied in the same order, so the file that lands
        # is byte-identical -- there is simply one of it.
        #
        # The source is in a session too: that is where a backfill writes, and
        # against the 133 MB collection.nml those were the 10-second tracks.
        plan = _Plan()
        with ExitStack() as collections:
            collections.enter_context(self.adapter_for(target).collection_session())
            collections.enter_context(self.adapter_for(source).collection_session())
            for key in pl.track_keys:
                src_track = source_lib.tracks.get(key)
                if src_track is None:
                    result.errors.append(f"Source track missing for {key}")
                    self.log.event("track", key=key, decision="ERROR",
                                   reason="not in the source library")
                    continue
                tgt_t = target_lib.tracks.get(key)
                # A VirtualDJ track known only from a .vdjfolder has no <Song>
                # block, so it is NOT really in the collection and must be created.
                in_collection = tgt_t is not None and tgt_t.in_database
                should_write = False
                reason = ""
                if not in_collection:
                    should_write = True            # add missing track (with cues)
                    reason = "not in the target collection"
                elif self.content_in_sync(src_track, tgt_t, fields):
                    reason = "already in sync"     # nothing to do
                elif mode == OVERWRITE:
                    should_write = True
                    reason = "overwrite forces it"
                elif mode == UPDATE:
                    allowed, why = self._update_should_write(
                        key, source, target, src_track, tgt_t)
                    if allowed:
                        should_write = True
                        reason = "update: the source owns the change"
                    else:
                        result.tracks_skipped += 1
                        result.skipped_by_reason[why] = \
                            result.skipped_by_reason.get(why, 0) + 1
                        reason = f"update: declined ({why or 'target is newer'})"
                else:
                    reason = "new-only: already in the collection"
                # mode == NEW and already in collection -> leave cues untouched.
                self._log_track_decision(key, src_track, tgt_t, source, target,
                                         fields, should_write, reason)
                if should_write:
                    self._plan_track(plan, src_track, source, target,
                                     allow_backfill=mode != NEW)

            # One pass per kind per file, for every track the loop decided on.
            try:
                created = self._flush_writes(plan, source, target)
            except OSError as e:
                result.errors.append(str(e))
                self.log.event("write FAILED", error=str(e))
                created = {}
            # Counted from the writers' own answers, not the in-memory guess, so
            # the numbers always match what really happened.
            result.tracks_added = sum(1 for v in created.values() if v)
            result.tracks_cue_synced = sum(1 for v in created.values() if not v)

            # Playlist membership write (structure): add ALL source tracks (create
            # the node, or append only its missing members to an existing node).
            try:
                with self.timing.phase("playlist membership write"):
                    created, entries_added, entries_removed = self._write_playlist_membership(
                        node, source, target, source_lib, playlist_new_members, mode)
                result.playlists_created += 1 if created else 0
                result.playlist_entries_added += entries_added
                result.playlist_entries_removed += entries_removed
            except OSError as e:
                result.errors.append(f"Playlist write failed: {e}")
                self.log.event("membership write FAILED", error=str(e))

            # Closed here rather than at the end of the ``with``, so the one
            # whole-file write per collection -- and the parse guard in front of
            # it -- is a phase of its own instead of vanishing into the loop.
            # Closing twice is harmless: the stack is empty after the first.
            with self.timing.phase("collection flush"):
                collections.close()

        result.unwritten_tags = list(self.unwritten_tags)
        self._end_run()
        self._log_result(result, node, source, target, target_path)
        return result

    def _end_run(self) -> None:
        """Total the run's timings. Outside the log path on purpose: the counters
        are the adapters' own, and a caller may want them with logging off."""
        self.timing.file_reads = sum(a.collection_reads
                                     for a in self.adapters.values())
        self.timing.file_writes = sum(a.collection_writes
                                      for a in self.adapters.values())
        # Freeze first, then charge whatever is left to the loop: the two have to
        # be read off the same instant or the phases do not add up to the total.
        total = self.timing.stop()
        self.timing.add("decision loop",
                        max(0.0, total - sum(self.timing.phases.values())))

    def _log_result(self, result, node, source, target, target_path) -> None:
        """The run's outcome, and whether the target file is still readable."""
        if not self.log.enabled:
            return
        self.log.section("Playlist membership, after (re-read from disk)")
        wanted = list(node.playlist.track_keys) if node.playlist else []
        # Re-read the target through its adapter rather than asking the
        # in-memory library: the write went to a FILE, and the in-memory copy is
        # not reloaded until the window does it. Reading it here said "1005
        # missing" about a playlist that had just been written in full -- a
        # diagnostic lying about the exact symptom it exists to measure.
        # Only done when a log is attached, so an ordinary sync never pays for it.
        try:
            fresh = self.adapter_for(target).read()
            after = self._target_playlist_keys(node, fresh, target)
            missing = [k for k in wanted if k not in set(after)]
            self.log.event("counts", source_tracks=len(wanted),
                           in_target_after=len(after),
                           missing_from_target=len(missing))
            for key in missing[:50]:
                self.log.line(f"      MISSING: {key}")
            if len(missing) > 50:
                self.log.line(f"      …and {len(missing) - 50} more")
        except Exception as exc:                 # noqa: BLE001 - diagnostics only
            self.log.event("counts", source_tracks=len(wanted),
                           reread_failed=str(exc))

        self.log.section("Result")
        self.log.event("counts", added=result.tracks_added,
                       cues_synced=result.tracks_cue_synced,
                       skipped=result.tracks_skipped,
                       playlists_created=result.playlists_created,
                       entries_added=result.playlist_entries_added,
                       entries_removed=result.playlist_entries_removed)
        for key in result.unwritten_tags[:50]:
            self.log.line(f"      TAGS NOT WRITTEN (no entry matched): {key}")
        for err in result.errors[:20]:
            self.log.line(f"      ERROR: {err}")
        self.log.check_parses("after", target_path)
        self.log.timings(self.timing)

    def ensure_track_in_target(self, key: str, source: str,
                               target: str | None = None) -> bool:
        """Create ``key`` (looked up in ``source``'s library) in ``target``'s
        collection if it is not already there, with cue-corrected positions --
        the same write path :meth:`sync_track` uses. Used for cross-program
        drag-add, so a track dragged from one program into another's playlist
        exists in that program's collection first.

        Returns whether a new entry was created (False if it was already there,
        in which case nothing is written). Callers doing several of these in one
        batch should back up once themselves beforehand, the way
        :meth:`sync_playlist` backs up once per call rather than once per track.
        """
        target = target or self.default_target(source)
        source_lib = self.library_for(source)
        target_lib = self.library_for(target)
        if source_lib is None or target_lib is None:
            return False
        src_track = source_lib.tracks.get(key)
        if src_track is None:
            return False
        tgt_t = target_lib.tracks.get(key)
        if tgt_t is not None and tgt_t.in_database:
            return False               # already a real collection entry
        return self._write_one(src_track, source, target, allow_backfill=False)

    def stage_track_in_target(self, key: str, source: str,
                              target: str | None = None):
        """Cross-program drag-add, split for a responsive UI: add the track to
        the target's in-memory library **now** (so the view shows it at once) and
        return a callable that performs the file write off the main thread, or
        ``None`` if the track already has a real entry (nothing to do).

        The returned job reads-modifies-writes the target file itself, capturing
        only the immutable ``shifted`` Track, so it is safe to run on a worker.
        """
        target = target or self.default_target(source)
        source_lib = self.library_for(source)
        target_lib = self.library_for(target)
        if source_lib is None or target_lib is None:
            return None
        src_track = source_lib.tracks.get(key)
        if src_track is None:
            return None
        tgt_t = target_lib.tracks.get(key)
        if tgt_t is not None and tgt_t.in_database:
            return None

        pois = _retimed_pois(src_track, self.adapter_for(source),
                             self.adapter_for(target), _bpm_of(tgt_t))
        shifted = Track(**{**src_track.__dict__, "pois": pois})
        target_lib.tracks[shifted.key] = shifted        # main-thread in-memory add
        # The adapter's own job already reads-modifies-writes the file and
        # captures nothing mutable, which is exactly what this needs to hand to
        # the background writer.
        return self.adapter_for(target).create_entry_job(shifted)

    def sync_track(self, key: str, source: str,
                   target: str | None = None) -> SyncResult:
        """Sync a single track's cues.

        The one-item form of :meth:`sync_tracks`, kept so every caller and test
        that has one track reads exactly as it always did.
        """
        return self.sync_tracks([key], source, target)

    def sync_tracks(self, keys, source: str,
                    target: str | None = None) -> SyncResult:
        """Sync these tracks' cues (used by the track context menu).

        **One run for the whole selection, not one per track.** Each run opens a
        collection session per program and flushes it on the way out, and that
        flush re-parses and rewrites the whole file -- about 6 s on a real pair.
        Looping the singular over a 50-track selection would pay that fifty
        times. Planning them together means one walk per write kind and one
        flush, exactly as a playlist sync does.
        """
        result = SyncResult()
        target = target or self.default_target(source)
        source_lib = self.library_for(source)
        if target is None or source_lib is None:
            result.errors.append("No sync target for this track.")
            return result
        wanted = [k for k in dict.fromkeys(keys) if k]
        tracks = [source_lib.tracks[k] for k in wanted if k in source_lib.tracks]
        for missing in (k for k in wanted if k not in source_lib.tracks):
            result.errors.append(f"Track not found in source library: {missing}")
        if not tracks:
            if not result.errors:
                result.errors.append("Track not found in source library")
            return result

        target_path = self.adapter_for(target).collection_path
        self.unwritten_tags = []
        self._begin_run()
        # **No per-run backup here, deliberately.** A playlist sync copies the
        # whole collection first (see sync_playlist) because it can rewrite
        # membership and hundreds of entries at once. A track sync touches one
        # entry, and copying 44 MB beside the user's live database for every
        # single-track sync left them accumulating without bound -- 88 MB from
        # two syncs 42 seconds apart in the reported logs.
        #
        # What still covers it: the whole-library snapshot taken at launch, plus
        # Ctrl-S and the autosave timer. The trade is that a track sync is
        # recoverable to the session's starting state rather than to the instant
        # before that one write.
        self.log.section(f"Track sync: {source} -> {target}")
        self.log.note_file("target collection", target_path)
        with self.timing.phase("parse check (before)"):
            self.log.check_parses("before", target_path)
        tgt_lib = self.library_for(target)
        fields = self.syncable_fields(source, target)
        for src_track in tracks:
            self.log.event("track", title=getattr(src_track, "title", ""),
                           key=src_track.key)
            self._log_track_decision(
                src_track.key, src_track,
                tgt_lib.tracks.get(src_track.key) if tgt_lib else None,
                source, target, fields, True, "track sync always writes")
        try:
            # Same batching as a playlist sync: the decision loop only plans,
            # and one flush writes each collection once however many tracks were
            # selected. The source session is here so a backfill shares it
            # rather than rewriting a second whole file.
            with ExitStack() as collections:
                collections.enter_context(
                    self.adapter_for(target).collection_session())
                collections.enter_context(
                    self.adapter_for(source).collection_session())
                plan = _Plan()
                for src_track in tracks:
                    self._plan_track(plan, src_track, source, target)
                self._flush_writes(plan, source, target)
                with self.timing.phase("collection flush"):
                    collections.close()
            result.tracks_cue_synced = len(tracks)
            result.unwritten_tags = list(self.unwritten_tags)
        except OSError as e:
            result.errors.append(str(e))
            self.log.event("write FAILED", error=str(e))
        self._end_run()
        self.log.section("Result")
        for tag_key in result.unwritten_tags:
            self.log.line(f"      TAGS NOT WRITTEN (no entry matched): {tag_key}")
        self.log.check_parses("after", target_path)
        self.log.timings(self.timing)
        return result

    # ==================================================================
    @staticmethod
    def _name_collision(node: TreeNode, source_lib: Library) -> str:
        """Why this playlist cannot be synced, or "" if it can.

        Two playlists whose paths differ only in case are one path to everything
        downstream: the same ``.vdjfolder`` file, the same ``path_ci`` key. There
        is no correct choice between them, so neither is written and both are
        named -- renaming one in the source is the only thing that fixes it, and
        the user is the only one who can decide which.
        """
        collisions = source_lib.colliding_playlist_paths()
        nodes = collisions.get(path_ci(node.node_path))
        if not nodes:
            return ""
        names = ", ".join(f'"{n.name}"' for n in nodes)
        where = "/".join(node.node_path[:-1]) or "the top level"
        return (f"{names} in {where} differ only in capitalisation, and the "
                f"target keeps one file per playlist -- writing both would "
                f"leave only the last one. Rename one and sync again.")

    def _target_playlist_keys(self, source_node: TreeNode, target_lib: Library,
                              target: str) -> set[str]:
        """Find the equivalent playlist in the target library by node path
        (same folder chain + name); empty set if it doesn't exist yet."""
        wanted = path_ci(source_node.node_path)
        for pl_node in target_lib.playlists():
            if path_ci(pl_node.node_path) == wanted:
                return set(pl_node.playlist.track_keys)
        return set()

    def _write_one(self, src_track: Track, source: str, target: str,
                   allow_backfill: bool = True) -> bool:
        """Plan and write a single track. The one-item form of the batched path,
        so a context-menu sync and a 1005-track playlist go through exactly the
        same code -- there is no second write path to keep in step."""
        plan = _Plan()
        self._plan_track(plan, src_track, source, target,
                         allow_backfill=allow_backfill)
        return bool(self._flush_writes(plan, source, target).get(src_track.key))

    def _plan_track(self, plan, src_track: Track, source: str, target: str,
                    allow_backfill: bool = True) -> None:
        """Work out everything this track needs written, and add it to ``plan``.

        Deciding and writing used to happen together, one track at a time, and
        each write walked the whole collection to find its one entry -- O(tracks
        x entries), which was the entire runtime. Now the loop only decides; the
        writes happen once per kind in :meth:`_flush_writes`.

        ``allow_backfill`` is off for New Only: that mode means "add the missing
        tracks and touch nothing else", so writing back into the source library
        under it would be a surprise.
        """
        key = src_track.key
        plan.sources[key] = src_track
        plan.slots.setdefault(key, []).append(self.log.reserve())

        target_lib = self.library_for(target)
        tgt_now = target_lib.tracks.get(key) if target_lib else None
        fields, backfill = self.carried_fields(
            src_track, tgt_now, self.syncable_fields(source, target))
        if fields:
            plan.tags[key] = fields
        if backfill and allow_backfill:
            plan.source_tags[key] = backfill
        key_to_target, key_to_source = self.carried_key(src_track, tgt_now)
        if key_to_target is not None:
            plan.keys[key] = key_to_target
        if key_to_source is not None and allow_backfill:
            plan.source_keys[key] = key_to_source

    def _prime_start_times(self, plan, source: str, target: str) -> None:
        """Measure every planned track's audio start time, in parallel.

        Only the tracks the run will actually write, and only the ones the cache
        has never seen -- exactly what the per-track path would have probed one
        at a time. A source and target on the same timeline need no measurement
        at all, which is why the adapters are compared first.
        """
        cache = self.start_time_cache
        src_ad, tgt_ad = self.adapter_for(source), self.adapter_for(target)
        if cache is None or src_ad is tgt_ad:
            return
        cache.prime([matching.resolve_local_path(t.path)
                     for t in plan.sources.values() if t.path])

    def _flush_writes(self, plan, source: str, target: str) -> dict:
        """Run the planned writes -- one pass per kind per file -- and bring
        memory into line with what the writers actually answered.

        Returns ``{key: created}``. Nothing here decides anything: every decision
        was made in :meth:`_plan_track`, so the only new information is the
        writers' answers.
        """
        if not plan.sources:
            return {}
        tgt = self.adapter_for(target)
        src = self.adapter_for(source)
        lines: dict = {k: [] for k in plan.sources}

        # Retiming is done here, not in the loop, so every audio file the run
        # needs measured is known at once and can be probed in parallel. Doing it
        # per track meant one ffprobe at a time: 28 ms each, 12 seconds of an
        # 865-track run.
        with self.timing.phase("audio timing probe"):
            self._prime_start_times(plan, source, target)
        src_ad, tgt_ad = self.adapter_for(source), self.adapter_for(target)
        target_tracks = self.libraries[target].tracks
        plan.cues = {
            key: Track(**{**t.__dict__, "pois": _retimed_pois(
                t, src_ad, tgt_ad, _bpm_of(target_tracks.get(key)))})
            for key, t in plan.sources.items()}

        with self.timing.phase("collection write (cues)"):
            created = tgt.create_entries_job(list(plan.cues.values()))()
        self.timing.walks += 1
        self.timing.entries_appended += sum(1 for v in created.values() if v)
        self.timing.entries_edited += sum(1 for v in created.values() if not v)
        for key, was_created in created.items():
            lines[key].append(_evented("  create_entry_job", key=key,
                                       created=was_created,
                                       cues=len(plan.cues[key].pois)))

        # A brand-new entry was rendered from the source track and already
        # carries its tags and its key; writing them again would be a second walk
        # of the file for no change. An EXISTING entry is the case that was
        # broken -- create_entries_job touches only cues and playcount on one.
        tags = {k: f for k, f in plan.tags.items() if not created.get(k)}
        keys = {k: v for k, v in plan.keys.items() if not created.get(k)}
        # The paths let each writer *locate* its entry instead of walking the
        # whole collection -- 2.29 s of a backfilling run on a 133.5 MB one. The
        # target is asked for its own spelling first, because it may store a
        # different case; a mismatch only costs a fall back to the walk.
        target_lib = self.library_for(target)
        tgt_paths = {k: ((target_lib.tracks[k].path
                          if target_lib is not None and k in target_lib.tracks
                          else t.path) or t.path)
                     for k, t in plan.sources.items()}
        src_paths = {k: t.path for k, t in plan.sources.items()}
        tags_written, keys_written = self._write_meta_batch(
            tgt, "collection write (tags + key)", tags, keys, lines,
            "write_metadata_job", "write_key_job", _key_fields(tgt, target),
            tgt_paths)
        src_tags_written, src_keys_written = self._write_meta_batch(
            src, "source write (backfill)", plan.source_tags, plan.source_keys,
            lines, "backfill (source <- target)",
            "write_key_job (source <- target)", _key_fields(src, source),
            src_paths)

        for key, parts in lines.items():
            text = "\n".join(parts)
            # Every occurrence, not just the last: a track listed twice in the
            # playlist gets two decision blocks and both want their outcome.
            for slot in plan.slots.get(key, ()):
                self.log.fill(slot, text)

        for key, shifted in plan.cues.items():
            self._apply_written(plan, key, shifted, source, target,
                                created.get(key, False), tags_written,
                                keys_written, src_tags_written, src_keys_written)
        return created

    def _write_meta_batch(self, adapter, phase: str, tags: dict, keys: dict,
                          lines: dict, tag_label: str, key_label: str,
                          key_describe, paths: dict | None = None) -> tuple:
        """Tags and musical key on one file in a **single** pass.

        A key write is a metadata write with program-specific attributes
        (``adapter.key_fields``), so a track needing both used to cost two walks
        of the same collection -- 2.7 s and 2.1 s of one run, the second of them
        for a single track. Merged, the writer sees each entry once.

        The log is unchanged: both lines are still written per track, filled from
        the one answer the writer gave for that entry. Returns
        ``(tags_written, keys_written)``.
        """
        merged: dict = {}
        for key, fields in tags.items():
            merged[key] = dict(fields)
        for key, value in keys.items():
            merged.setdefault(key, {}).update(adapter.key_fields(value))
        if not merged:
            return {}, {}
        written = self._run_batch(
            phase, lambda payload: adapter.write_metadata_jobs(payload, paths),
            merged, tag_label)
        for key, fields in tags.items():
            lines[key].append(_evented(
                f"  {tag_label}", key=key, changed=bool(written.get(key)),
                **_tag_fields(fields)))
        for key, value in keys.items():
            lines[key].append(_evented(
                f"  {key_label}", key=key, changed=bool(written.get(key)),
                **key_describe(value)))
        return ({k: written.get(k, False) for k in tags},
                {k: written.get(k, False) for k in keys})

    def _run_batch(self, phase: str, make_job, payload: dict, label: str) -> dict:
        """The write itself: timed, counted as one walk, and never allowed to
        take the run down over a file the OS would not give us."""
        try:
            with self.timing.phase(phase):
                written = make_job(payload)()
            self.timing.walks += 1
            self.timing.entries_edited += sum(1 for v in written.values() if v)
        except OSError as exc:
            self.log.event(f"{label} FAILED", error=str(exc))
            return {k: False for k in payload}
        return written

    def _apply_written(self, plan, key: str, shifted: Track, source: str,
                       target: str, created: bool, tags_written: dict,
                       keys_written: dict, src_tags_written: dict,
                       src_keys_written: dict) -> None:
        """Memory follows the file, never leads it.

        A write the writer declined leaves the in-memory copy alone: two
        in-memory tracks that agree read as green, so updating on a False would
        tell the user a track is synced that never reached disk -- a lie that
        survives a refresh, because a refresh re-diffs memory rather than
        re-reading the file.
        """
        src_track = plan.sources[key]
        if key in plan.source_tags:
            if src_tags_written.get(key):
                for field, value in plan.source_tags[key].items():
                    setattr(src_track, field, value)
            else:
                self.unwritten_tags.append(key)
        if key in plan.source_keys and src_keys_written.get(key):
            src_track.key_value = plan.source_keys[key]

        fields = plan.tags.get(key, {})
        # A created entry was rendered with the tags and the key already.
        wrote_tags = created or bool(tags_written.get(key))
        if fields and not created and not tags_written.get(key):
            self.unwritten_tags.append(key)
        target_lib = self.library_for(target)
        if target_lib is None:
            return
        existing = target_lib.tracks.get(key)
        if created or existing is None:
            target_lib.tracks[key] = shifted
            return
        # Mutate in place: other rows may already hold this Track object.
        existing.pois = shifted.pois
        existing.playcount = shifted.playcount
        if keys_written.get(key):
            existing.key_value = plan.keys[key]
        if wrote_tags:
            for field, value in fields.items():
                setattr(existing, field, value)
        # It has a real collection entry now, whatever it was before -- a
        # VirtualDJ track previously known only from a .vdjfolder included.
        existing.in_database = True

    def _write_playlist_membership(self, node: TreeNode, source: str, target: str,
                                   source_lib: Library, new_keys: list[str],
                                   mode: str) -> tuple[bool, int, int]:
        """Create/update the target playlist. Returns
        ``(playlist_created, entries_added, entries_removed)`` so the caller can
        report the work -- without this the whole playlist write was invisible.

        NEW is add-only. UPDATE and OVERWRITE **mirror** the source: the target's
        membership becomes exactly the source's, in the source's order, so a
        track dropped on the source side is dropped here too.
        """
        adapter = self.adapter_for(target)
        target_lib = self.library_for(target)
        ordered_tracks = [source_lib.tracks[k] for k in node.playlist.track_keys
                         if k in source_lib.tracks]
        mirror = mode in (UPDATE, OVERWRITE)
        existing = (self._find_target_node(node, target_lib)
                    if target_lib is not None else None)

        if (existing is None or existing.playlist is None
                or not adapter.can_write_membership(existing)):
            # The target has no playlist here (or none this adapter can address),
            # so make one. no_duplicates=False because a synced list mirrors the
            # source, which may legitimately name a track twice -- the opposite
            # of the default a hand-made playlist gets.
            _playlist, job = adapter.new_playlist(
                node.name, tuple(node.node_path), ordered_tracks, None,
                no_duplicates=False)
            job()
            return True, len(ordered_tracks), 0

        current_keys = list(existing.playlist.track_keys)
        src_keys = [t.key for t in ordered_tracks]
        if mirror:
            gained = len([k for k in src_keys if k not in current_keys])
            dropped = len([k for k in current_keys if k not in src_keys])
            # Skip an identical rewrite: a mirror that changes nothing should not
            # touch the file at all.
            if gained or dropped or src_keys != current_keys:
                adapter.write_membership_job(existing, ordered_tracks)()
            return False, gained, dropped

        # NEW: add-only. Keep what the target already has, append what it lacks.
        add_tracks = [t for t in ordered_tracks if t.key in new_keys]
        merged_keys = current_keys + [t.key for t in add_tracks
                                      if t.key not in current_keys]
        added = len(merged_keys) - len(current_keys)
        if added:
            merged = [target_lib.tracks.get(k) or source_lib.tracks.get(k)
                      for k in merged_keys]
            adapter.write_membership_job(existing, [t for t in merged if t])()
        return False, added, 0

    def _find_target_node(self, source_node: TreeNode, target_lib: Library) -> TreeNode | None:
        """The target's copy of this playlist, or None.

        **The exact spelling wins.** Traktor holds ``Diggs/temp`` (511 tracks)
        beside ``Diggs/Temp`` (42); VirtualDJ has only ``Temp``. Taking the
        first case-insensitive match synced VirtualDJ's 42 into the 511-track
        ``temp`` and mirrored 469 tracks out of it. The status model already
        insists on the exact name for such a path (``Report.playlist_status``),
        so the write now asks the same question. With no exact match the first
        case-insensitive one is used, and any surplus is left untouched."""
        wanted = path_ci(source_node.node_path)
        exact = tuple(source_node.node_path)
        first = None
        for pl_node in target_lib.playlists():
            if path_ci(pl_node.node_path) == wanted:
                if tuple(pl_node.node_path) == exact:
                    return pl_node
                first = first or pl_node
        return first
