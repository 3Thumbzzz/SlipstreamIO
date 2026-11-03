"""Next / Previous follow the "on end" mode, and Previous walks a play history.

Next used to step through the visible list whatever the mode said. It now
triggers, on demand, whatever would have happened when the track ended —
including a random pick under the shuffle modes.

That makes "previous" ambiguous: under shuffle there is no previous track in any
list, only the one you actually heard. So there is a session **play history**,
browser-style, and Previous walks back up it before falling back to the mode's
own predecessor.

The fixture collection holds two entries, which cannot show an ordering, so these
stock the in-memory library and the focused table directly.
"""

import os
import random
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                             # noqa: E402

from trackcleaner.config.paths import Settings                         # noqa: E402
from trackcleaner.io import backup as backup_mod                       # noqa: E402
from trackcleaner.model import status as st                            # noqa: E402
from trackcleaner.model.library import TRAKTOR, Track                  # noqa: E402
from trackcleaner.ui import waveform_panel as wf                       # noqa: E402
from fixtures import write_sample                                      # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _make_window(tmp_path, monkeypatch):
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
    return w


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    yield w
    w.close()


class _Loads(list):
    """What the panel was asked to play, in order."""

    @property
    def keys(self):
        return [t.key for t, _play in self]


@pytest.fixture
def stage(window, monkeypatch):
    """A five-track playlist whose stored order is the reverse of what is shown.

    Sort mode and Order mode then pick different successors, which is the only
    way to tell them apart.
    """
    tracks = [Track(key=f"k{i}", source=TRAKTOR, path=f"D:/M/{i}.mp3",
                    title=f"T{i}") for i in range(5)]
    lib = window.libraries[TRAKTOR]
    lib.tracks.update({t.key: t for t in tracks})

    table = window.track_tabs.focused_table()
    table.set_rows([(t, st.GREEN) for t in tracks])          # k0..k4 on screen
    node = types.SimpleNamespace(
        playlist=types.SimpleNamespace(track_keys=[t.key for t in reversed(tracks)]),
        node_path=("Root", "P"))
    table.set_context(TRAKTOR, [node])

    loads = _Loads()
    monkeypatch.setattr(window.waveform_panel, "load_track",
                        lambda track, play=False: loads.append((track, play)))
    monkeypatch.setattr(window.waveform_panel.player, "is_playing", lambda: True)

    def playing(key):
        window.waveform_panel._track = lib.tracks[key]

    playing("k2")
    return types.SimpleNamespace(window=window, loads=loads, playing=playing,
                                 keys=[t.key for t in tracks])


def _mode(stage, mode):
    stage.window.waveform_panel.set_on_end_mode(mode)


# ============================ Next follows the mode =========================
def test_sort_mode_steps_through_what_is_shown(stage):
    _mode(stage, wf.ON_END_SORT)

    stage.window._step_track(1)

    assert stage.loads.keys == ["k3"]


def test_order_mode_steps_through_the_playlists_stored_order(stage):
    """The stored order is reversed here, so this is a different answer from
    Sort -- which is the whole point of the mode existing."""
    _mode(stage, wf.ON_END_ORDER)

    stage.window._step_track(1)

    assert stage.loads.keys == ["k1"]


def test_single_file_still_moves_on_an_explicit_next(stage):
    """"Do not auto-advance" is about the end of a track. A button press has to
    do something."""
    _mode(stage, wf.ON_END_SINGLE)

    stage.window._step_track(1)

    assert stage.loads.keys == ["k3"]


def test_random_playlist_mode_picks_from_the_playlist(stage, monkeypatch):
    _mode(stage, wf.ON_END_RANDOM_PLAYLIST)
    monkeypatch.setattr(random, "choice", lambda pool: sorted(pool)[-1])

    stage.window._step_track(1)

    assert stage.loads.keys == ["k4"]


def test_random_mode_is_not_just_the_next_in_the_list(stage, monkeypatch):
    """The behaviour that was asked for: Next under shuffle must actually
    shuffle, not quietly walk the list."""
    _mode(stage, wf.ON_END_RANDOM_PLAYLIST)
    monkeypatch.setattr(random, "choice", lambda pool: sorted(pool)[0])

    stage.window._step_track(1)

    assert stage.loads.keys == ["k0"]          # k3 would be "the next in the list"


def test_random_never_returns_the_track_already_playing(stage):
    _mode(stage, wf.ON_END_RANDOM_PLAYLIST)

    for _ in range(12):
        stage.window._step_track(1)

    assert "k2" not in stage.loads.keys[:1]


def test_next_still_does_not_start_playback_when_paused(stage, monkeypatch):
    monkeypatch.setattr(stage.window.waveform_panel.player, "is_playing",
                        lambda: False)
    _mode(stage, wf.ON_END_SORT)

    stage.window._step_track(1)

    assert stage.loads == [(stage.window.libraries[TRAKTOR].tracks["k3"], False)]


# ============================== the play history ============================
def test_previous_returns_the_track_you_actually_heard(stage):
    """Under shuffle this is the only meaningful reading of "previous"."""
    _mode(stage, wf.ON_END_RANDOM_PLAYLIST)
    stage.window._record_play(stage.window.libraries[TRAKTOR].tracks["k2"])
    stage.window._step_track(1)
    first_pick = stage.loads.keys[-1]
    stage.playing(first_pick)

    stage.window._step_track(-1)

    assert stage.loads.keys[-1] == "k2"


def test_previous_walks_further_back(stage):
    win = stage.window
    for key in ("k0", "k1", "k4"):
        win._record_play(win.libraries[TRAKTOR].tracks[key])

    win._step_track(-1)
    win._step_track(-1)

    assert stage.loads.keys == ["k1", "k0"]


def test_next_replays_forward_through_the_history(stage):
    win = stage.window
    for key in ("k0", "k1", "k4"):
        win._record_play(win.libraries[TRAKTOR].tracks[key])
    win._step_track(-1)                       # back to k1

    win._step_track(1)

    assert stage.loads.keys == ["k1", "k4"]


def test_replaying_does_not_grow_the_history(stage):
    """Appending each replay would trap you bouncing between two tracks."""
    win = stage.window
    for key in ("k0", "k1", "k4"):
        win._record_play(win.libraries[TRAKTOR].tracks[key])

    win._step_track(-1)
    win._step_track(1)
    win._step_track(-1)

    assert win._play_history == ["k0", "k1", "k4"]


def test_playing_something_new_mid_history_drops_the_forward_tail(stage):
    """Browser rule: go back, then take a different turn, and the old forward
    path is gone rather than lingering behind a Next press."""
    win = stage.window
    for key in ("k0", "k1", "k4"):
        win._record_play(win.libraries[TRAKTOR].tracks[key])
    win._step_track(-1)                       # at k1, k4 ahead

    win._record_play(win.libraries[TRAKTOR].tracks["k3"])

    assert win._play_history == ["k0", "k1", "k3"]


def test_previous_with_no_history_falls_back_to_the_list(stage):
    """A fresh session with nothing behind you still has to go somewhere."""
    _mode(stage, wf.ON_END_SORT)

    stage.window._step_track(-1)

    assert stage.loads.keys == ["k1"]


def test_previous_in_order_mode_uses_the_stored_order(stage):
    _mode(stage, wf.ON_END_ORDER)

    stage.window._step_track(-1)

    assert stage.loads.keys == ["k3"]


def test_previous_under_shuffle_without_history_does_not_randomise(stage):
    """Otherwise Previous would be indistinguishable from Next."""
    _mode(stage, wf.ON_END_RANDOM_LIBRARY)

    stage.window._step_track(-1)

    assert stage.loads.keys == ["k1"]


def test_reloading_the_same_track_is_not_a_new_history_entry(stage):
    win = stage.window
    win._record_play(win.libraries[TRAKTOR].tracks["k0"])

    win._record_play(win.libraries[TRAKTOR].tracks["k0"])

    assert win._play_history == ["k0"]


def test_a_history_entry_that_no_longer_resolves_falls_back(stage):
    """A reload replaces every Track object and a removal drops one outright, so
    a recorded key can stop resolving. Previous then behaves as if there were no
    history rather than crashing or doing nothing -- and it does not move the
    history pointer onto the dead entry."""
    _mode(stage, wf.ON_END_SORT)
    win = stage.window
    win._record_play(win.libraries[TRAKTOR].tracks["k0"])
    win._record_play(win.libraries[TRAKTOR].tracks["k1"])
    for lib in win.libraries.values():
        lib.tracks.pop("k0", None)

    win._step_track(-1)

    assert stage.loads.keys == ["k1"]         # the shown list's predecessor
    assert win._history_pos == 1


# ===================== what gets recorded in the first place ================
def test_auto_advance_is_recorded(stage):
    _mode(stage, wf.ON_END_SORT)

    stage.window._auto_advance()

    assert stage.window._play_history[-1] == "k3"


def test_a_double_click_is_recorded(stage):
    stage.window._on_track_double_clicked("k4")

    assert stage.window._play_history[-1] == "k4"


def test_a_click_declined_during_playback_is_not_recorded(stage, monkeypatch):
    """click_load stands down while audio is playing, so nothing was heard and
    nothing belongs in the history."""
    monkeypatch.setattr(stage.window.waveform_panel.player, "is_playing",
                        lambda: True)

    stage.window._on_track_clicked("k4")

    assert stage.window._play_history == []


def test_a_click_that_did_load_is_recorded(stage, monkeypatch):
    monkeypatch.setattr(stage.window.waveform_panel.player, "is_playing",
                        lambda: False)

    stage.window._on_track_clicked("k4")

    assert stage.window._play_history == ["k4"]


def test_the_history_starts_empty(window):
    assert window._play_history == []
    assert window._history_pos == -1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
