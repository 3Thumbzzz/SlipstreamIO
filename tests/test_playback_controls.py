"""Stepping through tracks, and the playback pane's context menu.

The stepping *rule* is a pure function so the wrap-around and the
nothing-playing-yet cases can be checked without audio. What Next and Previous
choose to step **through** is the "on end" mode — see `test_next_previous.py`,
which covers that and the play history.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                             # noqa: E402

from trackcleaner.config.paths import Settings                         # noqa: E402
from trackcleaner.io import backup as backup_mod                       # noqa: E402
from trackcleaner.model import status as st                            # noqa: E402
from trackcleaner.model.library import TRAKTOR, Track, VDJ             # noqa: E402
from trackcleaner.ui.track_table import TrackTable                     # noqa: E402
from fixtures import write_sample                                      # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _tracks(n=4):
    return [Track(key=f"k{i}", path=f"p{i}", source=VDJ, title=f"T{i}")
            for i in range(n)]


def _lookup(tracks):
    by_key = {t.key: t for t in tracks}
    return by_key.get


# =============================== the rule ===================================
def _step(keys, cur, delta, lookup):
    from trackcleaner.ui.main_window import MainWindow
    return MainWindow._step(keys, cur, delta, lookup)


def test_next_moves_one_forward(app):
    tracks = _tracks()
    got = _step([t.key for t in tracks], "k1", 1, _lookup(tracks))
    assert got.key == "k2"


def test_previous_moves_one_back(app):
    tracks = _tracks()
    got = _step([t.key for t in tracks], "k1", -1, _lookup(tracks))
    assert got.key == "k0"


def test_next_wraps_at_the_end(app):
    tracks = _tracks()
    got = _step([t.key for t in tracks], "k3", 1, _lookup(tracks))
    assert got.key == "k0"


def test_previous_wraps_at_the_start(app):
    tracks = _tracks()
    got = _step([t.key for t in tracks], "k0", -1, _lookup(tracks))
    assert got.key == "k3"


def test_with_nothing_playing_next_starts_at_the_top(app):
    tracks = _tracks()
    got = _step([t.key for t in tracks], "", 1, _lookup(tracks))
    assert got.key == "k0"


def test_with_nothing_playing_previous_starts_at_the_bottom(app):
    """Otherwise Previous on a fresh list would do nothing, which reads as
    broken rather than as a deliberate refusal."""
    tracks = _tracks()
    got = _step([t.key for t in tracks], "", -1, _lookup(tracks))
    assert got.key == "k3"


def test_a_key_that_does_not_resolve_is_skipped(app):
    """A playlist can reference a track the collection no longer has; stepping
    onto it would load nothing and look like the button failed."""
    tracks = _tracks()
    keys = ["k0", "missing", "k1"]
    got = _step(keys, "k0", 1, _lookup(tracks))
    assert got.key == "k1"


def test_an_empty_list_steps_nowhere(app):
    assert _step([], "k0", 1, _lookup(_tracks())) is None
    assert _step(["missing"], "", 1, _lookup(_tracks())) is None


# ========================= what the window steps over =======================
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


def test_preview_rows_are_not_stepped_onto(app):
    """A "gaining" ghost is a track this playlist does not contain; playing it
    from a Next press would be playing something that is not in the list."""
    real = _tracks(2)
    ghost = Track(key="ghost", path="gp", source=VDJ, title="G")
    table = TrackTable()
    table.set_rows([(real[0], st.GREEN), (real[1], st.GREEN), (ghost, "green2")])

    assert table.real_visual_keys() == ["k0", "k1"]


def test_next_loads_a_track_from_the_focused_list(window, monkeypatch):
    """The integration half: the window resolves "the list" from the focused tab
    and hands a real track to the panel. Which track it picks is the pure-function
    tests above -- the fixture's playlists hold one track each, so ordering
    cannot be shown here without inventing a library."""
    loaded = []
    monkeypatch.setattr(window.waveform_panel, "load_track",
                        lambda track, play=False: loaded.append(track.key))
    lib = window.libraries[TRAKTOR]
    node = next(n for n in lib.playlists() if n.playlist.track_keys)
    window._on_playlists_selected([node], TRAKTOR)

    window._step_track(1)

    assert loaded and loaded[0] in lib.tracks


def test_stepping_does_not_start_playback_when_paused(window, monkeypatch):
    """Next while paused should move, not start the music."""
    seen = []
    monkeypatch.setattr(window.waveform_panel, "load_track",
                        lambda track, play=False: seen.append(play))
    monkeypatch.setattr(window.waveform_panel.player, "is_playing", lambda: False)
    lib = window.libraries[TRAKTOR]
    node = next(n for n in lib.playlists() if n.playlist.track_keys)
    window._on_playlists_selected([node], TRAKTOR)

    window._step_track(1)

    assert seen == [False]


def test_stepping_keeps_playing_when_it_was_playing(window, monkeypatch):
    seen = []
    monkeypatch.setattr(window.waveform_panel, "load_track",
                        lambda track, play=False: seen.append(play))
    monkeypatch.setattr(window.waveform_panel.player, "is_playing", lambda: True)
    lib = window.libraries[TRAKTOR]
    node = next(n for n in lib.playlists() if n.playlist.track_keys)
    window._on_playlists_selected([node], TRAKTOR)

    window._step_track(1)

    assert seen == [True]


def test_the_transport_buttons_ask_the_window_to_step(window, monkeypatch):
    """The panel does not know what "the list" is -- that is the focused tab's
    business -- so it asks rather than resolving it itself."""
    steps = []
    monkeypatch.setattr(type(window), "_step_track",
                        lambda self, delta: steps.append(delta))

    window.waveform_panel.next_btn.click()
    window.waveform_panel.prev_btn.click()

    assert steps == [1, -1]


# ==================== the playback pane's context menu ======================
def test_the_title_emits_a_context_request(app):
    from trackcleaner.ui.waveform_panel import _TitleLabel
    from PySide6.QtCore import QPoint
    label = _TitleLabel("Track")
    seen = []
    label.context_requested.connect(lambda pos: seen.append(pos))

    label.customContextMenuRequested.emit(QPoint(3, 3))

    assert len(seen) == 1


def test_both_menus_are_built_from_one_bag(window):
    """The list and the playback pane must offer the same actions: two bags
    would drift the moment one gained an entry."""
    lib = window.libraries[TRAKTOR]
    key = next(iter(lib.tracks))

    bag = window._track_callbacks(key, TRAKTOR, row=-1)

    for name in ("sync_track", "edit", "show_playlists", "check_integrity",
                 "reveal", "remove_from_playlist", "remove_with_entry",
                 "delete_file"):
        assert hasattr(bag, name), name


def test_the_playback_menu_treats_its_track_as_a_real_member(window):
    """What is loaded is a real track by definition -- there is no preview-row
    case, so the removal entries must not be suppressed."""
    lib = window.libraries[TRAKTOR]
    key = next(iter(lib.tracks))

    bag = window._track_callbacks(key, TRAKTOR, row=-1)

    assert bag.is_tombstone is False


def test_no_menu_when_nothing_is_loaded(window):
    window.waveform_panel._track = None

    window._on_playback_title_context(None)      # must simply do nothing


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
