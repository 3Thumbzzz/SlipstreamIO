"""Sync Update and an edit made inside the app, and never claiming more than disk.

Two reported defects, both about a sync's relationship with the truth.

**Update skipped the tracks you had just edited.** It asks change attribution
"does the source own this change?", and attribution is only ever computed by a
full diff. A track that was in sync beforehand is *absent* from the owners map,
so a freshly-edited one answered "nobody" and was skipped. Overwrite ignores the
guard, which is why only it appeared to work.

**A write that changed no file still marked the track synced.** The write path
updated its in-memory copy of the target regardless of whether the writer found
anything to change, so two in-memory tracks agreed and the row went **green** for
a change that never reached disk — and it stayed green through a refresh, because
a refresh re-diffs memory rather than re-reading the file.

The rule both enforce: **the app must never show a track as synced that is not
synced on disk.**
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                              # noqa: E402

from trackcleaner.config.paths import Settings                          # noqa: E402
from trackcleaner.io import backup as backup_mod                        # noqa: E402
from trackcleaner.io import traktor_read, vdj_read                      # noqa: E402
from trackcleaner.model import baseline as bl_mod                       # noqa: E402
from trackcleaner.model import status as st                             # noqa: E402
from trackcleaner.model.library import TRAKTOR, VDJ                     # noqa: E402
from trackcleaner.sync.engine import NEW, OVERWRITE, UPDATE, SyncEngine  # noqa: E402
from fixtures import write_sample                                       # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(tmp_path / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(tmp_path / "settings.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(tmp_path / "baseline.csv"))
    monkeypatch.setattr(paths_mod, "app_dir", lambda: str(tmp_path))
    from trackcleaner.ui.main_window import MainWindow
    settings = Settings()
    settings.paths = write_sample(tmp_path)
    w = MainWindow(settings)
    w.show()
    yield w
    w.close()


def _synced_pair(window):
    """A Traktor playlist already synced into VirtualDJ, and one shared key.

    VirtualDJ's library is re-read afterwards: the engine writes the .vdjfolder,
    but the in-memory playlist tree only learns about it on a reload -- which is
    what the app does after a sync.
    """
    node = next(n for n in window.libraries[TRAKTOR].playlists()
                if n.playlist and n.playlist.track_keys)
    window.sync_engine.sync_playlist(node, TRAKTOR, NEW, target=VDJ)
    paths = window.settings.paths
    window.libraries[VDJ] = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    window.sync_engine.libraries = window.libraries
    window._recompute_status()
    return node, node.playlist.track_keys[0]


def _vdj_node_holding(window, key):
    return next(n for n in window.libraries[VDJ].playlists()
                if n.playlist and key in n.playlist.track_keys)


# ===================== an in-app edit records who made it ===================
def test_an_edit_names_the_program_it_was_made_in(window):
    """Attribution is otherwise only ever computed by a full diff, and a track
    that was in sync is absent from the map entirely."""
    node, key = _synced_pair(window)
    track = window.libraries[VDJ].tracks[key]

    window._apply_metadata([track], VDJ, {"comment": "EDITED"})

    pair = bl_mod.pair_id(VDJ, TRAKTOR)
    assert window._owners.get(pair, {}).get(key) == VDJ


def test_the_live_engine_sees_it_immediately(window):
    """Without this the sync in *this* session still reads the old map -- the
    owners dict is only rebuilt by a full recompute."""
    node, key = _synced_pair(window)
    track = window.libraries[VDJ].tracks[key]

    window._apply_metadata([track], VDJ, {"comment": "EDITED"})

    pair = bl_mod.pair_id(VDJ, TRAKTOR)
    assert window.sync_engine.owners.get(pair, {}).get(key) == VDJ


def test_it_survives_into_the_baseline_file(window, tmp_path):
    """So the verdict is still there next session, like every other attribution."""
    node, key = _synced_pair(window)
    track = window.libraries[VDJ].tracks[key]

    window._apply_metadata([track], VDJ, {"comment": "EDITED"})

    from trackcleaner.model.baseline import Baseline
    stored = Baseline(str(tmp_path / "baseline.csv")).load()
    assert stored.for_pair(VDJ, TRAKTOR).get(key).owner == VDJ


def test_recording_an_edit_for_a_track_a_program_lacks_is_a_no_op(window):
    from trackcleaner.model.baseline import Baseline
    store = Baseline("")

    bl_mod.record_edit(store, "not-a-key", VDJ, [TRAKTOR], window.libraries)

    assert store.for_pair(VDJ, TRAKTOR).get("not-a-key") is None


# ========================= Update now writes the edit =======================
def test_update_writes_a_tag_edited_in_the_app(window):
    """The reported bug: Update did nothing, Overwrite worked."""
    node, key = _synced_pair(window)
    window._apply_metadata([window.libraries[VDJ].tracks[key]], VDJ,
                           {"comment": "EDITED"})
    vdj_node = _vdj_node_holding(window, key)

    result = window.sync_engine.sync_playlist(vdj_node, VDJ, UPDATE, target=TRAKTOR)

    assert result.tracks_skipped == 0
    assert window.libraries[TRAKTOR].tracks[key].comment == "EDITED"


def test_the_edit_reaches_the_file_not_just_memory(window):
    node, key = _synced_pair(window)
    window._apply_metadata([window.libraries[VDJ].tracks[key]], VDJ,
                           {"comment": "EDITED"})
    vdj_node = _vdj_node_holding(window, key)

    window.sync_engine.sync_playlist(vdj_node, VDJ, UPDATE, target=TRAKTOR)

    on_disk = traktor_read.read(window.settings.paths.nml)
    assert on_disk.tracks[key].comment == "EDITED"


def test_update_still_refuses_to_clobber_the_other_side(window):
    """The guard's whole purpose. Syncing INTO the program that holds the edit
    must still skip, or Update would overwrite work it has not been told about.

    Both sides carry a value here: with a **blank** source the engine now has
    nothing to write anyway, so the guard would never be consulted -- see the
    test below.
    """
    node, key = _synced_pair(window)
    window.libraries[TRAKTOR].tracks[key].comment = "FROM TRAKTOR"
    window._apply_metadata([window.libraries[VDJ].tracks[key]], VDJ,
                           {"comment": "EDITED IN VDJ"})

    result = window.sync_engine.sync_playlist(node, TRAKTOR, UPDATE, target=VDJ)

    assert result.tracks_skipped >= 1
    assert window.libraries[VDJ].tracks[key].comment == "EDITED IN VDJ"


def test_a_blank_source_field_becomes_a_backfill(window):
    """The reported data loss, from the other end: VirtualDJ carries no comment
    for most tracks, so syncing one into Traktor was erasing comments only
    Traktor had. Refusing to erase was right, but merely declining left the two
    sides permanently different and permanently yellow. The value is copied back
    into the source instead -- nothing is lost and the pair converges."""
    node, key = _synced_pair(window)
    window.libraries[TRAKTOR].tracks[key].comment = "Jazz"
    window.libraries[VDJ].tracks[key].comment = ""
    engine = window.sync_engine
    fields = engine.syncable_fields(VDJ, TRAKTOR)

    to_target, to_source = engine.carried_fields(
        window.libraries[VDJ].tracks[key],
        window.libraries[TRAKTOR].tracks[key], fields)

    assert "comment" not in to_target, "must never erase the target"
    assert to_source["comment"] == "Jazz", "must fill the side that lacks it"
    assert not engine.content_in_sync(window.libraries[VDJ].tracks[key],
                                      window.libraries[TRAKTOR].tracks[key],
                                      fields), "there is work to do"


def test_a_populated_source_still_fills_a_blank_target(window):
    """The ordinary direction must keep working, or "never erase" would quietly
    mean "never sync"."""
    node, key = _synced_pair(window)
    window.libraries[TRAKTOR].tracks[key].comment = "Jazz"
    window.libraries[VDJ].tracks[key].comment = ""
    engine = window.sync_engine

    to_target, to_source = engine.carried_fields(
        window.libraries[TRAKTOR].tracks[key],
        window.libraries[VDJ].tracks[key],
        engine.syncable_fields(TRAKTOR, VDJ))

    assert to_target["comment"] == "Jazz"
    assert to_source == {}


def test_both_blank_writes_nothing_either_way(window):
    node, key = _synced_pair(window)
    window.libraries[TRAKTOR].tracks[key].comment = ""
    window.libraries[VDJ].tracks[key].comment = ""
    engine = window.sync_engine

    to_target, to_source = engine.carried_fields(
        window.libraries[VDJ].tracks[key],
        window.libraries[TRAKTOR].tracks[key],
        engine.syncable_fields(VDJ, TRAKTOR))

    assert "comment" not in to_target and "comment" not in to_source


def test_overwrite_is_unaffected(window):
    """It never consulted attribution and still must not."""
    node, key = _synced_pair(window)
    window._apply_metadata([window.libraries[VDJ].tracks[key]], VDJ,
                           {"comment": "EDITED"})
    vdj_node = _vdj_node_holding(window, key)

    window.sync_engine.sync_playlist(vdj_node, VDJ, OVERWRITE, target=TRAKTOR)

    assert traktor_read.read(window.settings.paths.nml).tracks[key].comment == "EDITED"


# ============== a write that did nothing must not claim success =============
class _RefusingAdapter:
    """Wraps a real adapter, but its metadata writer always reports "no entry".

    Refuses at the batched seam, which is the one the engine uses: writes happen
    once per kind for the whole run, not once per track."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def write_metadata_jobs(self, by_key, paths=None):
        return lambda: {k: False for k in by_key}

    def write_metadata_job(self, key, fields):
        return lambda: False


def test_a_refused_write_leaves_the_in_memory_copy_alone(window):
    """The green-that-was-not-synced, in one assertion: memory must never claim
    more than disk."""
    node, key = _synced_pair(window)
    window.libraries[TRAKTOR].tracks[key].comment = "FROM TRAKTOR"
    engine = window.sync_engine
    engine.adapters[VDJ] = _RefusingAdapter(engine.adapters[VDJ])

    engine._write_one(window.libraries[TRAKTOR].tracks[key], TRAKTOR, VDJ)

    assert window.libraries[VDJ].tracks[key].comment != "FROM TRAKTOR"


def test_a_refused_write_keeps_the_track_unsynced(window):
    """And the status agrees, which is the point -- a refresh re-diffs memory,
    so a lie told here survives one."""
    node, key = _synced_pair(window)
    window.libraries[TRAKTOR].tracks[key].comment = "FROM TRAKTOR"
    engine = window.sync_engine
    engine.adapters[VDJ] = _RefusingAdapter(engine.adapters[VDJ])

    engine._write_one(window.libraries[TRAKTOR].tracks[key], TRAKTOR, VDJ)
    window._recompute_status()

    assert window.reports[VDJ].track_status.get(key) != st.GREEN


def test_a_refused_write_is_reported(window):
    """Silently doing nothing is what made this invisible for a whole release."""
    node, key = _synced_pair(window)
    window.libraries[TRAKTOR].tracks[key].comment = "FROM TRAKTOR"
    engine = window.sync_engine
    engine.adapters[VDJ] = _RefusingAdapter(engine.adapters[VDJ])
    vdj_target_node = next(n for n in window.libraries[TRAKTOR].playlists()
                           if n.playlist and key in n.playlist.track_keys)

    result = engine.sync_playlist(vdj_target_node, TRAKTOR, OVERWRITE, target=VDJ)

    assert key in result.unwritten_tags


def test_a_successful_write_does_update_memory(window):
    """The other direction, so the guard cannot be satisfied by never updating."""
    node, key = _synced_pair(window)
    window.libraries[TRAKTOR].tracks[key].comment = "FROM TRAKTOR"

    window.sync_engine._write_one(window.libraries[TRAKTOR].tracks[key],
                                    TRAKTOR, VDJ)

    assert window.libraries[VDJ].tracks[key].comment == "FROM TRAKTOR"
    assert window.sync_engine.unwritten_tags == []


def test_a_newly_created_entry_needs_no_second_write(window):
    """It is rendered from the source track and already carries the tags, so
    reporting it as unwritten would be a false alarm. The entry has to be absent
    from the FILE, not just from memory -- that is what makes it a creation."""
    from trackcleaner.model.library import Track
    _synced_pair(window)
    fresh = Track(key="d:/music/newtrack.mp3", path="D:/Music/NewTrack.mp3",
                  source=TRAKTOR, title="Brand New", comment="C")
    window.libraries[TRAKTOR].tracks[fresh.key] = fresh
    window.sync_engine.adapters[VDJ] = _RefusingAdapter(
        window.sync_engine.adapters[VDJ])

    created = window.sync_engine._write_one(fresh, TRAKTOR, VDJ)

    assert created is True
    assert window.sync_engine.unwritten_tags == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ===================== a skip says which kind of skip it is ==================
def test_an_unknown_history_skip_is_not_reported_as_the_other_side(window):
    """The line that made the app look self-contradictory: every skip claimed
    "the other side owns the change", including tracks the other side had never
    touched."""
    from trackcleaner.sync.engine import SKIP_UNKNOWN
    node, key = _synced_pair(window)
    window.libraries[TRAKTOR].tracks[key].comment = "FROM TRAKTOR"
    window.libraries[VDJ].tracks[key].comment = "FROM VDJ"
    window._owners.clear()                      # never compared while agreeing
    window.sync_engine.owners = window._owners
    vdj_node = _vdj_node_holding(window, key)

    result = window.sync_engine.sync_playlist(vdj_node, VDJ, UPDATE, target=TRAKTOR)

    assert result.skipped_by_reason.get(SKIP_UNKNOWN) == result.tracks_skipped
    assert result.tracks_skipped >= 1


def test_a_real_conflict_is_reported_as_a_conflict(window):
    from trackcleaner.model import baseline as bl
    from trackcleaner.sync.engine import SKIP_CONFLICT
    node, key = _synced_pair(window)
    window.libraries[TRAKTOR].tracks[key].comment = "FROM TRAKTOR"
    window.libraries[VDJ].tracks[key].comment = "FROM VDJ"
    pair = bl.pair_id(VDJ, TRAKTOR)
    window._owners[pair] = {key: bl.BOTH}
    window.sync_engine.owners = window._owners
    vdj_node = _vdj_node_holding(window, key)

    result = window.sync_engine.sync_playlist(vdj_node, VDJ, UPDATE, target=TRAKTOR)

    assert result.skipped_by_reason.get(SKIP_CONFLICT) == 1


def test_the_wording_differs_per_reason():
    from trackcleaner.sync.engine import (SKIP_CONFLICT, SKIP_TARGET_OWNS,
                                          SKIP_UNKNOWN)
    from trackcleaner.ui.main_window import MainWindow

    text = MainWindow._skip_lines(
        {"skipped": 3, "skip_reasons": {SKIP_UNKNOWN: 1, SKIP_CONFLICT: 1,
                                        SKIP_TARGET_OWNS: 1}})

    assert "never compared before" in text
    assert "changed on both sides" in text
    assert "the other program owns the change" in text


def test_nothing_skipped_says_nothing():
    """A clean run must read exactly as it did before."""
    from trackcleaner.ui.main_window import MainWindow

    assert MainWindow._skip_lines({"skipped": 0, "skip_reasons": {}}) == ""


# ============================ carrying the key ==============================
# Reported as "many traktor keys do not get translated and pushed to VDJ". On the
# real pair, 60,120 of 81,107 VirtualDJ songs carry no key at all, and the key is
# deliberately not a compared field -- so nothing in the compare-then-write loop
# was ever going to give them one. The rendering fix that came before this only
# reached entries being CREATED, and in the logs every track already existed.
def test_a_traktor_key_fills_a_blank_virtualdj_one(window):
    node, key = _synced_pair(window)
    engine = window.sync_engine
    window.libraries[TRAKTOR].tracks[key].key_value = 22        # A#m / 8m
    window.libraries[VDJ].tracks[key].key_value = None

    to_target, to_source = engine.carried_key(
        window.libraries[TRAKTOR].tracks[key], window.libraries[VDJ].tracks[key])

    assert (to_target, to_source) == (22, None)
    assert not engine.content_in_sync(
        window.libraries[TRAKTOR].tracks[key], window.libraries[VDJ].tracks[key],
        engine.syncable_fields(TRAKTOR, VDJ)), "a keyless target is work to do"


def test_the_key_reaches_the_file_in_virtualdjs_notation(window):
    """Not Traktor's. Passing the stored text through put "8d" into a field
    VirtualDJ reads as a musical key."""
    node, key = _synced_pair(window)
    window.libraries[TRAKTOR].tracks[key].key_value = 22
    window.libraries[VDJ].tracks[key].key_value = None

    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)

    paths = window.settings.paths
    on_disk = vdj_read.read(paths.xml, paths.vdj_folders_dir()).tracks[key]
    assert on_disk.key_musical == "A#m"
    assert on_disk.key_value == 22


def test_a_key_written_into_traktor_lands_in_traktors_own_notation(window):
    node, key = _synced_pair(window)
    window.libraries[TRAKTOR].tracks[key].key_value = None
    window.libraries[VDJ].tracks[key].key_value = 22

    window.sync_engine.sync_playlist(node, VDJ, OVERWRITE, target=TRAKTOR)

    on_disk = traktor_read.read(window.settings.paths.nml).tracks[key]
    assert on_disk.key_value == 22, "MUSICAL_KEY carries the canonical value"
    assert on_disk.key_musical == "8m", "INFO/@KEY carries Traktor's own notation"


def test_a_backfill_locates_its_entry_instead_of_walking(window, monkeypatch):
    """The backfill is the only write this engine makes to the *source*, and on
    a real library that source is Traktor's 133.5 MB ``collection.nml``. Walking
    it to reach one entry was 2.29 s -- the whole of the reported
    ``source write (backfill)`` phase.

    Pinned on the call, and on the paths handed over: the writer can only locate
    an entry if the engine gives it somewhere to look, and a path that does not
    match simply falls back to the walk, so a silently wrong one would show up
    as nothing at all.
    """
    from trackcleaner.io import traktor_write
    node, key = _synced_pair(window)
    # Blank on the source, filled on the target: the shape that backfills.
    window.libraries[TRAKTOR].tracks[key].comment = ""
    window.libraries[VDJ].tracks[key].comment = "Filled In VDJ"
    source_path = window.libraries[TRAKTOR].tracks[key].path

    # Spied on the *metadata* writer, not on the locate: the cue writer locates
    # too, so a locate call alone would say nothing about the backfill.
    handed = []
    real = traktor_write.set_entries_metadata
    monkeypatch.setattr(
        traktor_write, "set_entries_metadata",
        lambda c, bk, paths=None: handed.append((dict(bk), paths))
        or real(c, bk, paths))

    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)

    backfills = [(bk, paths) for bk, paths in handed if key in bk]
    assert backfills, "no backfill reached the source collection"
    _fields, paths = backfills[0]
    assert paths is not None, "the writer was left to walk the whole NML"
    assert paths.get(key) == source_path
    on_disk = traktor_read.read(window.settings.paths.nml).tracks[key]
    assert on_disk.comment == "Filled In VDJ", "and it actually wrote"


def test_a_blank_source_key_is_backfilled_from_the_target(window):
    """The other direction, and the only write this engine makes to the source
    library. Safe for the same reason the tag backfill is: it can only add."""
    node, key = _synced_pair(window)
    window.libraries[TRAKTOR].tracks[key].key_value = None      # the source
    window.libraries[VDJ].tracks[key].key_value = 22            # the target

    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)

    on_disk = traktor_read.read(window.settings.paths.nml).tracks[key]
    assert on_disk.key_value == 22
    assert on_disk.key_musical == "8m"


def test_two_keys_that_disagree_are_left_alone(window):
    """Neither side is overwritten. A key each program analysed for itself is not
    a conflict this app can settle, and the spellings differ harmlessly --
    VirtualDJ writes "Ebm" where we render "D#m", so converging the 1,771 tracks
    that spell it that way would rewrite them to say what they already say."""
    node, key = _synced_pair(window)
    engine = window.sync_engine
    window.libraries[TRAKTOR].tracks[key].key_value = 22
    window.libraries[VDJ].tracks[key].key_value = 15

    assert engine.carried_key(window.libraries[TRAKTOR].tracks[key],
                              window.libraries[VDJ].tracks[key]) == (None, None)


def test_an_existing_virtualdj_key_is_never_respelled(window):
    node, key = _synced_pair(window)
    before = open(window.settings.paths.xml, encoding="utf-8").read()
    window.libraries[TRAKTOR].tracks[key].key_value = 15        # would be "D#m"
    window.libraries[VDJ].tracks[key].key_value = 15            # already stored

    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)

    after = open(window.settings.paths.xml, encoding="utf-8").read()
    assert 'Key="D#m"' not in after or 'Key="D#m"' in before


def test_neither_side_having_a_key_writes_nothing(window):
    node, key = _synced_pair(window)
    engine = window.sync_engine
    window.libraries[TRAKTOR].tracks[key].key_value = None
    window.libraries[VDJ].tracks[key].key_value = None

    assert engine.carried_key(window.libraries[TRAKTOR].tracks[key],
                              window.libraries[VDJ].tracks[key]) == (None, None)


def test_new_only_performs_no_key_backfill(window):
    """New Only means "add the missing tracks and touch nothing else"; writing
    back into the source library under it would be a surprise."""
    node, key = _synced_pair(window)
    paths = window.settings.paths
    before = vdj_read.read(paths.xml, paths.vdj_folders_dir()).tracks[key].key_musical
    window.libraries[VDJ].tracks[key].key_value = None          # the source
    window.libraries[TRAKTOR].tracks[key].key_value = 22        # the target

    window.sync_engine.sync_playlist(node, VDJ, NEW, target=TRAKTOR)

    after = vdj_read.read(paths.xml, paths.vdj_folders_dir()).tracks[key].key_musical
    assert after == before, "the source library must not be written under New Only"
