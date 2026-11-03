"""What a refresh, a sync and a failed load do to the track view.

Four reported defects, all about state that should survive an operation or be
established by it:

* not-yet-added rows jumped to the top of the list;
* a sync reset the scroll position;
* a refresh did not notice that a file had gone missing;
* a track whose audio would not load stayed looking fine.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                                          # noqa: E402
from PySide6.QtWidgets import QApplication                             # noqa: E402

from trackcleaner.config.paths import Settings                         # noqa: E402
from trackcleaner.io import backup as backup_mod                       # noqa: E402
from trackcleaner.model import status as st                            # noqa: E402
from trackcleaner.model.library import (Playlist, Track, TreeNode,     # noqa: E402
                                        TRAKTOR, VDJ)
from trackcleaner.ui.fields import ALL_COLUMN_IDS, ORDER_COL           # noqa: E402
from trackcleaner.ui.track_table import TrackTable                     # noqa: E402
from fixtures import write_sample                                      # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _rows(n_real=3, n_ghost=2):
    out = [(Track(key=f"k{i}", path=f"p{i}", source=VDJ, title=f"T{i}"), st.GREEN)
           for i in range(n_real)]
    out += [(Track(key=f"g{i}", path=f"gp{i}", source=VDJ, title=f"G{i}"), "green2")
            for i in range(n_ghost)]
    return out


# ================== preview rows stay where they were put ===================
def test_preview_rows_stay_at_the_bottom_by_default(app):
    """Reported: they jumped to the top. Cause: setSortingEnabled left the proxy
    sorting on column 0 (status) DESCENDING before any header was ever clicked,
    and "green2" sorts above "green" -- so every untouched table was silently
    ordered by status rather than by playlist order."""
    t = TrackTable()

    t.set_rows(_rows())

    assert t.visual_keys() == ["k0", "k1", "k2", "g0", "g1"]


def test_an_untouched_table_is_not_sorted_at_all(app):
    t = TrackTable()
    t.set_rows(_rows())

    assert not t.sorted_by_user()


def test_they_stay_at_the_bottom_across_a_refresh(app):
    """The repopulate a refresh does must not re-introduce the status sort."""
    t = TrackTable()
    t.set_rows(_rows())

    t.set_rows(_rows())               # what a refresh does

    assert t.visual_keys() == ["k0", "k1", "k2", "g0", "g1"]


def test_a_chosen_sort_still_survives_a_refresh(app):
    """Fixing the default must not throw away a sort the user asked for."""
    t = TrackTable()
    t.set_rows(_rows())
    t.sortByColumn(ALL_COLUMN_IDS.index(ORDER_COL), Qt.DescendingOrder)
    sorted_keys = t.visual_keys()
    assert t.sorted_by_user()

    t.set_rows(_rows())

    assert t.visual_keys() == sorted_keys


def test_clearing_the_sort_returns_to_playlist_order(app):
    t = TrackTable()
    t.set_rows(_rows())
    t.sortByColumn(ALL_COLUMN_IDS.index("title"), Qt.DescendingOrder)

    t.clear_sort()

    assert t.visual_keys() == ["k0", "k1", "k2", "g0", "g1"]


# ========================= scroll survives a reload =========================
def test_scroll_to_key_finds_a_row(app):
    t = TrackTable()
    t.set_rows(_rows(n_real=50, n_ghost=0))

    assert t.scroll_to_key("k30") is True


def test_scroll_to_a_track_that_is_gone_is_declined(app):
    """A sync consumes the preview rows, so the anchor can legitimately vanish --
    the caller leaves the view where it is rather than guessing."""
    t = TrackTable()
    t.set_rows(_rows())

    assert t.scroll_to_key("not-here") is False
    assert t.scroll_to_key("") is False


def test_the_anchor_is_the_top_visible_track(app):
    """A pixel offset cannot survive a sync -- the row count changes when the
    preview rows are consumed -- so the position is remembered as a track."""
    from trackcleaner.ui.controllers.views import ViewController

    t = TrackTable()
    t.set_rows(_rows(n_real=50, n_ghost=0))
    t.resize(400, 300)
    t.show()
    app.processEvents()
    t.scroll_to_key("k20")
    app.processEvents()

    assert ViewController._top_visible_key(t) == "k20"
    t.close()


# ===================== refresh checks the files exist =======================
def _make_window(tmp_path, monkeypatch):
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(tmp_path / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(tmp_path / "settings.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(tmp_path / "baseline.csv"))
    from trackcleaner.ui.main_window import MainWindow
    settings = Settings()
    settings.paths = write_sample(tmp_path)
    w = MainWindow(settings)
    w.show()
    return w


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    yield w
    w.close()


def test_the_scan_flags_a_track_whose_file_is_gone(window, tmp_path):
    """Reported: a refresh did not notice a file had disappeared. The scan now
    runs as part of loading, with no dialog and nobody asking for it.

    The path is set deliberately rather than relying on the fixture's: SAMPLE_NML
    carries real paths from the machine this was captured on, so whether they
    exist is not something a test can assume either way.
    """
    lib = window.libraries[TRAKTOR]
    key = next(iter(lib.tracks))
    lib.tracks[key].path = str(tmp_path / "definitely_not_here.mp3")

    window._scan_integrity_all()

    assert key in window._missing_keys[TRAKTOR]


def test_the_scan_runs_as_part_of_loading(window, monkeypatch):
    """Not merely available -- actually wired into the load, which is the
    reported defect."""
    called = []
    monkeypatch.setattr(type(window), "_scan_integrity_all",
                        lambda self, yield_fn=None: called.append(True))

    window.load_libraries()

    assert called, "loading did not run the integrity scan"


def test_the_scan_is_silent(window, monkeypatch):
    """It runs on every refresh, so it must not put a dialog in the way."""
    from PySide6.QtWidgets import QMessageBox
    shown = []
    monkeypatch.setattr(QMessageBox, "information",
                        lambda *a, **k: shown.append(a))

    window._scan_integrity_all()

    assert shown == []


def test_the_scan_clears_a_verdict_once_the_file_is_back(window, tmp_path):
    """Stale verdicts must not stick: a re-check is what clears them."""
    lib = window.libraries[TRAKTOR]
    key = next(iter(lib.tracks))
    # Both states are arranged explicitly. The fixture's own paths come from the
    # machine SAMPLE_NML was captured on, so whether they exist is not something
    # a test can assume in either direction.
    lib.tracks[key].path = str(tmp_path / "gone.mp3")
    window._scan_integrity(TRAKTOR, [key])
    assert key in window._missing_keys[TRAKTOR]

    real = tmp_path / "now_here.mp3"
    real.write_bytes(b"\0")
    lib.tracks[key].path = str(real)
    window._scan_integrity(TRAKTOR, [key])

    assert key not in window._missing_keys[TRAKTOR]


def test_missing_keys_covers_every_program(window):
    """It was initialised for two programs by name; a third would have had to
    rely on setdefault to avoid a KeyError."""
    assert set(window._missing_keys) == set(window.adapters)


# =================== a failed waveform load marks the track =================
def test_an_unreadable_track_is_marked(window):
    """Reported: a double-click that could not load the waveform left the track
    looking fine. It is a real finding, so it goes in the same store the
    integrity check uses -- and renders as the red X."""
    window._current_source = TRAKTOR
    key = next(iter(window.libraries[TRAKTOR].tracks))
    window._missing_keys[TRAKTOR].discard(key)

    window.waveform_panel.track_unreadable.emit(key)

    assert key in window._missing_keys[TRAKTOR]


def test_marking_the_same_track_twice_is_harmless(window):
    window._current_source = TRAKTOR
    key = next(iter(window.libraries[TRAKTOR].tracks))

    window.waveform_panel.track_unreadable.emit(key)
    window.waveform_panel.track_unreadable.emit(key)

    assert key in window._missing_keys[TRAKTOR]


def test_an_unreadable_track_renders_red(window):
    """The point of marking it: RED now draws as an X rather than a square."""
    from trackcleaner.ui import status_squares

    window._current_source = TRAKTOR
    key = next(iter(window.libraries[TRAKTOR].tracks))
    window.waveform_panel.track_unreadable.emit(key)

    got = window.views.status_for(TRAKTOR, None, key, st.GREEN)
    assert got == st.RED
    assert status_squares.shape_for(got) == status_squares.CROSS


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
