"""One read and one write per collection file, per sync run.

Every collection write has the same shape -- read the whole file, hand the text
to a pure ``str -> (str, changed)`` mutation, write it back atomically. That is
fine once. Done per track it cost **292 of one 329-second run**: a 243-track
playlist made 118 whole-file rewrites of a 43 MB ``database.xml``, each one
re-reading it, re-running the regex over it and re-parsing the document to
validate it.

A session holds the text in memory for the run so the mutations compose. The
mutations are untouched and still applied in the same order, which is why the
property that matters is testable at all: **the batched file is byte-identical to
the unbatched one.**
"""

import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                              # noqa: E402

from trackcleaner.config.paths import Settings                          # noqa: E402
from trackcleaner.io import backup as backup_mod                        # noqa: E402
from trackcleaner.model.library import TRAKTOR, VDJ                     # noqa: E402
from trackcleaner.sync.engine import NEW, OVERWRITE                     # noqa: E402
from fixtures import write_sample                                       # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _window(app, where, monkeypatch):
    from trackcleaner.config import paths as paths_mod
    where.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(where / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(where / "settings.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(where / "baseline.csv"))
    monkeypatch.setattr(paths_mod, "app_dir", lambda: str(where))
    from trackcleaner.ui.main_window import MainWindow
    settings = Settings()
    settings.paths = write_sample(where)
    return MainWindow(settings)


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    w = _window(app, tmp_path, monkeypatch)
    w.show()
    yield w
    w.close()


def _node(window):
    """The sample playlist, holding every track the sample has. One track cannot
    tell a per-track write from a batched one -- both make exactly one."""
    node = next(n for n in window.libraries[TRAKTOR].playlists()
                if n.playlist and n.playlist.track_keys)
    node.playlist.track_keys = list(window.libraries[TRAKTOR].tracks)
    return node


def _a_vdj_key(window):
    """A key VirtualDJ's collection really holds. Its sample database starts
    empty, so something has to put one there first."""
    node = _node(window)
    window.sync_engine.sync_playlist(node, TRAKTOR, NEW, target=VDJ)
    from trackcleaner.io import vdj_read
    paths = window.settings.paths
    window.libraries[VDJ] = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    return next(iter(window.libraries[VDJ].tracks))


# ========================= the counters tell the truth ======================
def test_a_sync_writes_the_target_collection_once(window):
    """The performance contract. Nothing else would catch a regression: the file
    a per-track write produces and the one a batched write produces are the same
    file, so only the count says which happened.

    Every track is given a change, so an unbatched run would make at least one
    rewrite each -- which is how this test tells the two apart at all.
    """
    node = _node(window)
    keys = node.playlist.track_keys
    assert len(keys) >= 2, "one track proves nothing here"
    for n, key in enumerate(keys):
        window.libraries[TRAKTOR].tracks[key].genre = f"Changed {n}"
    adapter = window.sync_engine.adapter_for(VDJ)

    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)

    assert adapter.collection_writes == 1, f"{len(keys)} changed tracks, one write"
    assert adapter.collection_reads == 1


def test_the_count_is_per_run_not_per_session(window):
    """Cleared at the start of each sync, or the second run would report the
    first one's work."""
    node = _node(window)
    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)
    first = window.sync_engine.timing.file_writes

    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)

    assert window.sync_engine.timing.file_writes <= max(first, 2)


def test_a_sync_that_changes_nothing_writes_nothing(window):
    """A session flushes only what was actually touched."""
    node = _node(window)
    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)

    window.sync_engine.sync_playlist(node, TRAKTOR, NEW, target=VDJ)

    assert window.sync_engine.timing.file_writes == 0


# ===================== batching changes no byte of output ===================
def test_the_batched_file_is_identical_to_the_unbatched_one(app, tmp_path,
                                                            monkeypatch):
    """The property everything else rests on. Run the same Overwrite twice --
    once with the sessions suppressed, once as shipped -- and compare the files
    the two produce, byte for byte."""
    from contextlib import contextmanager
    from trackcleaner.adapters.base import LibraryAdapter

    def run(where, batched):
        win = _window(app, where, monkeypatch)
        if not batched:
            @contextmanager
            def _no_session(self):
                yield self                       # every write goes straight out
            monkeypatch.setattr(LibraryAdapter, "collection_session", _no_session)
        node = next(n for n in win.libraries[TRAKTOR].playlists()
                    if n.playlist and n.playlist.track_keys)
        win.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)
        paths = win.settings.paths
        out = (open(paths.xml, encoding="utf-8").read(),
               open(paths.nml, encoding="utf-8").read())
        win.close()
        monkeypatch.undo()
        return out

    one_at_a_time = run(tmp_path / "unbatched", False)
    all_at_once = run(tmp_path / "batched", True)

    assert all_at_once[0] == one_at_a_time[0], "database.xml differs"
    assert all_at_once[1] == one_at_a_time[1], "collection.nml differs"


def test_a_failure_part_way_still_flushes_what_succeeded(window, tmp_path):
    """The flush is in a ``finally``: before batching each track landed as it
    went, and an interrupted run must not throw away the work that did happen."""
    key = _a_vdj_key(window)
    adapter = window.sync_engine.adapter_for(VDJ)
    path = adapter.collection_path
    before = open(path, encoding="utf-8").read()

    with pytest.raises(RuntimeError):
        with adapter.collection_session():
            adapter.write_metadata_job(key, {"genre": "Flushed Anyway"})()
            raise RuntimeError("interrupted")

    after = open(path, encoding="utf-8").read()
    assert after != before and "Flushed Anyway" in after


def test_the_file_is_still_valid_after_a_batched_run(window):
    """Same guard as before, once per run instead of once per track: the new
    content is parsed before it replaces the file."""
    import xml.etree.ElementTree as ET
    node = _node(window)

    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)

    ET.parse(window.settings.paths.xml)
    ET.parse(window.settings.paths.nml)


def test_nesting_a_session_does_not_write_twice(window):
    """``sync_track`` opens one of its own; a caller may already hold one."""
    key = _a_vdj_key(window)
    adapter = window.sync_engine.adapter_for(VDJ)
    adapter.collection_writes = 0

    with adapter.collection_session():
        with adapter.collection_session():
            adapter.write_metadata_job(key, {"genre": "Inner"})()
        assert adapter.collection_writes == 0, "the inner block must not flush"
    assert adapter.collection_writes == 1


def test_outside_a_session_a_write_still_lands_immediately(window):
    """Every other caller in the app -- the metadata dialog, the sidebar -- is
    unchanged: no session, straight to disk."""
    key = _a_vdj_key(window)
    adapter = window.sync_engine.adapter_for(VDJ)

    adapter.write_metadata_job(key, {"genre": "Immediate"})()

    assert "Immediate" in open(adapter.collection_path, encoding="utf-8").read()


# ======================= one walk per kind, not per track ===================
# Batching the I/O left the real cost untouched: a write still had to FIND its
# entry, and that meant walking every entry in the document. These pin the walk,
# which is the thing the file-write count cannot see.
def test_a_sync_walks_the_target_once_per_kind(window):
    """Not once per track. With three kinds of change on every track, a
    per-track writer would walk 3N times."""
    node = _node(window)
    keys = node.playlist.track_keys
    for n, key in enumerate(keys):
        window.libraries[TRAKTOR].tracks[key].genre = f"Changed {n}"

    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)

    assert window.sync_engine.timing.walks <= 4, (
        f"{len(keys)} tracks, {window.sync_engine.timing.walks} walks")


def test_a_kind_with_nothing_to_write_costs_no_walk(window):
    """An empty batch never opens the file at all, so a cues-only sync is one
    walk and not four."""
    node = _node(window)
    window.sync_engine.sync_playlist(node, TRAKTOR, NEW, target=VDJ)

    assert window.sync_engine.timing.walks == 1, "cues only"


def test_the_walk_count_is_per_run(window):
    node = _node(window)
    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)
    first = window.sync_engine.timing.walks

    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)

    assert window.sync_engine.timing.walks <= first


def test_the_entry_counts_add_up(window):
    """Every track the run wrote is accounted for as edited or appended."""
    node = _node(window)
    window.sync_engine.sync_playlist(node, TRAKTOR, NEW, target=VDJ)
    timing = window.sync_engine.timing

    assert timing.entries_appended == len(node.playlist.track_keys)
    assert timing.entries_edited == 0


def test_a_multi_track_tag_edit_submits_one_job(window):
    """Same defect one floor up: editing 50 tracks used to queue 50 whole-file
    rewrites, each walking the collection to find one song."""
    submitted = []
    window._writer.submit = lambda job: submitted.append(job)
    tracks = list(window.libraries[TRAKTOR].tracks.values())
    assert len(tracks) >= 2

    window._apply_metadata(tracks, TRAKTOR, {"genre": "One Job"})

    assert len(submitted) == 1
