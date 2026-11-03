r"""Sync Track acts on the selection, in one pass.

Two things were wrong with syncing a multi-selection, and only the first is
visible: ``_sync_track`` took a single key and never asked the table what was
selected, so selecting several tracks and choosing Sync Track wrote **one**.

The second would not have been visible until it was slow. Each run opens a
collection session per program and flushes it on the way out, and that flush
re-parses and rewrites the whole file -- about 6 s on the reported pair. Looping
the singular over a selection pays that once per track, so a fifty-track sync
would take minutes while appearing to work. The selection is therefore planned
together and flushed once, exactly as a playlist sync is.

The rule itself is shared with Edit and Remove: the whole selection when the
clicked row is part of it, else that row alone -- right-click deliberately does
not move the selection, so a click outside it has to speak for itself.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                              # noqa: E402

from trackcleaner.config.paths import Settings                          # noqa: E402
from trackcleaner.io import backup as backup_mod                        # noqa: E402
from trackcleaner.model.library import TRAKTOR, VDJ                     # noqa: E402
from fixtures import write_sample                                       # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    from trackcleaner.config import paths as paths_mod
    from trackcleaner.ui.main_window import MainWindow
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(tmp_path / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(tmp_path / "s.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(tmp_path / "b.csv"))
    monkeypatch.setattr(paths_mod, "app_dir", lambda: str(tmp_path))
    settings = Settings()
    settings.paths = write_sample(tmp_path)
    w = MainWindow(settings)
    w._current_source = TRAKTOR
    yield w
    w.close()


@pytest.fixture
def quiet(monkeypatch):
    """Swallow the result dialog; return what it would have said."""
    from trackcleaner.ui import main_window as mw
    said = []
    monkeypatch.setattr(mw.QMessageBox, "information",
                        lambda *a, **k: said.append(a[2]))
    monkeypatch.setattr(mw.QMessageBox, "warning",
                        lambda *a, **k: said.append(a[2]))
    return said


def _keys(window, n=2):
    """``n`` real track keys. Asserted, so a fixture with fewer tracks fails
    here rather than quietly weakening every test below."""
    lib = window.libraries[TRAKTOR]
    keys = list(lib.tracks)[:n]
    assert len(keys) == n, f"the sample library has {len(lib.tracks)} tracks"
    return keys


def _select(window, keys):
    """Put ``keys`` in the focused table's selection, as a user drag would."""
    table = window._table()
    assert table is not None, "no focused track table"
    table.selected_keys = lambda _k=list(keys): list(_k)
    return table


def _synced(window, monkeypatch):
    """Record every (keys, source, target) the engine is asked to sync."""
    calls = []
    real = window.sync_engine.sync_tracks

    def spy(keys, source, target=None):
        calls.append((list(keys), source, target))
        return real(keys, source, target)

    monkeypatch.setattr(window.sync_engine, "sync_tracks", spy)
    return calls


# ============================== the rule ===================================
def test_the_whole_selection_is_synced(window, quiet, monkeypatch):
    """The reported bug: several rows selected, one written."""
    keys = _keys(window)
    _select(window, keys)
    calls = _synced(window, monkeypatch)

    window._sync_track(keys[0], VDJ)

    assert calls, "nothing was synced"
    assert calls[0][0] == keys


def test_a_click_outside_the_selection_syncs_only_that_row(window, quiet,
                                                           monkeypatch):
    """Right-click does not move the selection, so a row outside it has to speak
    for itself -- the same rule Edit follows."""
    keys = _keys(window)
    _select(window, [keys[0]])
    calls = _synced(window, monkeypatch)

    window._sync_track(keys[1], VDJ)

    assert calls[0][0] == [keys[1]]


def test_it_matches_what_edit_would_act_on(window):
    """One definition of the rule, not two. Edit had it and Sync did not, which
    is the whole of this bug."""
    keys = _keys(window)
    _select(window, [keys[0]])

    for clicked in keys:
        edit = [t.key for t in window._edit_targets(clicked, TRAKTOR)]
        assert window._selected_track_keys(clicked) == edit


# ========================= one pass, not one each ==========================
def test_a_selection_writes_each_collection_once(window, quiet, monkeypatch):
    """The invisible half. Looping the singular would produce the same result
    and take one whole-file rewrite per track -- minutes on a real library."""
    keys = _keys(window)
    _select(window, keys)
    before = {a: ad.collection_writes for a, ad in window.adapters.items()}

    window._sync_track(keys[0], VDJ)

    after = {a: ad.collection_writes for a, ad in window.adapters.items()}
    written = {a: after[a] - before[a] for a in after}
    assert written[VDJ] <= 1, f"the target was rewritten {written[VDJ]} times"
    assert written[TRAKTOR] <= 1, f"the source was rewritten {written[TRAKTOR]} times"


def test_the_engine_is_called_once_per_target_not_once_per_track(window, quiet,
                                                                 monkeypatch):
    keys = _keys(window)
    _select(window, keys)
    calls = _synced(window, monkeypatch)

    window._sync_track(keys[0], VDJ)

    assert len(calls) == 1, f"{len(calls)} runs for one target"


def test_every_synced_track_is_recoloured(window, quiet, monkeypatch):
    """A targeted refresh that only knew about the clicked row would leave the
    rest of the selection showing a stale colour."""
    keys = _keys(window)
    _select(window, keys)
    seen = []
    monkeypatch.setattr(window, "_restatus_tracks",
                        lambda programs, ks: seen.append(list(ks)))

    window._sync_track(keys[0], VDJ)

    assert seen and seen[0] == keys


# ============================== the report =================================
def test_the_report_names_how_many_tracks(window, quiet):
    keys = _keys(window)
    _select(window, keys)

    window._sync_track(keys[0], VDJ)

    assert quiet and f"{len(keys)} tracks" in quiet[0], quiet


def test_one_track_still_reads_in_the_singular(window, quiet):
    keys = _keys(window, 1)
    _select(window, keys)

    window._sync_track(keys[0], VDJ)

    assert quiet and "Track cues synced" in quiet[0], quiet


# ============================== the engine =================================
def test_sync_tracks_reports_every_track_it_wrote(window):
    keys = _keys(window)

    result = window.sync_engine.sync_tracks(keys, TRAKTOR, VDJ)

    assert result.tracks_cue_synced == len(keys)
    assert not result.errors


def test_a_key_the_source_does_not_have_is_named_not_silent(window):
    keys = _keys(window)

    result = window.sync_engine.sync_tracks(keys + ["d:/nope.mp3"], TRAKTOR, VDJ)

    assert result.tracks_cue_synced == len(keys), "the real ones still went"
    assert any("nope.mp3" in e for e in result.errors), result.errors


def test_syncing_one_key_reads_exactly_as_it_always_did(window):
    """``sync_track`` stays the one-item door onto the same path."""
    key = _keys(window, 1)[0]

    result = window.sync_engine.sync_track(key, TRAKTOR, VDJ)

    assert result.tracks_cue_synced == 1 and not result.errors


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
