"""Waveform panel: time formatting, cue mapping and cue colouring."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                       # noqa: E402

from trackcleaner.model.cues import Poi                          # noqa: E402
from trackcleaner.model.library import Track, VDJ                # noqa: E402
from trackcleaner.ui import theme                                # noqa: E402
from trackcleaner.ui.waveform_panel import (WaveformPanel, cue_positions,  # noqa: E402
                                            format_time, grid_cue_numbers)


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    theme.capture_defaults(a)
    return a


def _track(pois):
    return Track(key="k", path=r"D:\Music\x.mp3", source=VDJ, length=210.0, pois=pois)


# ------------------------------ time format -------------------------------
def test_elapsed_format_is_mm_ss_ms():
    assert format_time(0) == "00:00:000"
    assert format_time(1234) == "00:01:234"
    assert format_time(62_150) == "01:02:150"
    assert format_time(201_400) == "03:21:400"


def test_remaining_format_is_signed():
    assert format_time(62_150, remaining=True) == "-01:02:150"
    assert format_time(0, remaining=True) == "-00:00:000"


def test_negative_input_clamps_rather_than_showing_junk():
    # total-position can go slightly negative at the very end of a track
    assert format_time(-5) == "00:00:000"


def test_over_an_hour_keeps_counting_minutes():
    assert format_time(3_723_000) == "62:03:000"


# ------------------------------ cue mapping -------------------------------
def test_cue_positions_indexes_by_number():
    t = _track([Poi(1.5, 1, "cue"), Poi(30.0, 3, "cue"), Poi(90.0, 8, "cue")])
    assert cue_positions(t) == {1: 1.5, 3: 30.0, 8: 90.0}


def test_cues_outside_one_to_eight_are_ignored():
    t = _track([Poi(1.0, 0, "cue"), Poi(2.0, 9, "cue"), Poi(3.0, 4, "cue")])
    assert cue_positions(t) == {4: 3.0}


def test_beatgrid_pois_are_not_hotcues():
    t = _track([Poi(0.4, None, "beatgrid"), Poi(5.0, 2, "cue")])
    assert cue_positions(t) == {2: 5.0}


def test_no_track_yields_no_cues():
    assert cue_positions(None) == {} and grid_cue_numbers(None) == set()


# --------------------------- beat-marker detection ------------------------
def test_cue_sharing_a_beatgrid_position_is_a_grid_cue():
    """A Traktor Beat Marker (CUE_V2 TYPE=4) emits both a beatgrid and a cue
    Poi at the same position -- the only trace of the cue type our model keeps."""
    t = _track([Poi(12.5, None, "beatgrid"), Poi(12.5, 1, "cue"), Poi(60.0, 2, "cue")])
    assert grid_cue_numbers(t) == {1}


def test_a_track_with_no_beatgrid_has_no_grid_cues():
    t = _track([Poi(12.5, 1, "cue")])
    assert grid_cue_numbers(t) == set()


# ------------------------------- the panel --------------------------------
def test_only_present_cues_are_enabled(app):
    panel = WaveformPanel()
    panel.load_track(_track([Poi(1.0, 1, "cue"), Poi(2.0, 3, "cue"), Poi(3.0, 8, "cue")]))
    enabled = [i for i, b in enumerate(panel.cue_buttons, 1) if b.isEnabled()]
    assert enabled == [1, 3, 8]


def test_grid_and_plain_cues_get_different_colours(app):
    theme.apply(app, theme.DARK)
    panel = WaveformPanel()
    panel.load_track(_track([Poi(9.0, None, "beatgrid"),
                             Poi(9.0, 1, "cue"), Poi(40.0, 2, "cue")]))
    grid_style = panel.cue_buttons[0].styleSheet()
    plain_style = panel.cue_buttons[1].styleSheet()
    assert theme.CUE_GRID[theme.DARK] in grid_style
    assert theme.CUE_PLAIN[theme.DARK] in plain_style
    assert grid_style != plain_style
    theme.apply(app, theme.LIGHT)


def test_absent_cue_buttons_use_the_disabled_colour(app):
    panel = WaveformPanel()
    panel.load_track(_track([Poi(1.0, 1, "cue")]))
    assert theme.CUE_ABSENT[theme.current()] in panel.cue_buttons[4].styleSheet()


def test_total_time_shows_metadata_length_before_the_media_loads(app):
    """The readout must not sit at 00:00:000 while the player is still opening
    the file -- the track's own length is known immediately."""
    panel = WaveformPanel()
    panel.load_track(_track([Poi(1.0, 1, "cue")]))
    assert panel.time_label.text().endswith("03:30:000")


def test_time_mode_toggles_to_remaining(app):
    panel = WaveformPanel()
    panel.load_track(_track([Poi(1.0, 1, "cue")]))
    assert not panel.time_label.text().startswith("-")
    panel.set_show_remaining(True)
    assert panel.time_label.text().startswith("-")
    assert panel.show_remaining() is True


def test_pressing_a_cue_the_track_lacks_is_ignored(app):
    panel = WaveformPanel()
    panel.load_track(_track([Poi(1.0, 1, "cue")]))
    panel.cue_pressed(7)                       # no such cue
    assert not panel.transport.playing


def test_transport_keys_do_nothing_without_a_track(app):
    panel = WaveformPanel()
    panel.toggle_play()
    panel.cue_pressed(1)
    assert not panel.transport.playing


def test_transport_keys_stand_down_while_typing(app):
    """1-8 and W must not fire hotcues while the Search box has focus -- both
    are perfectly ordinary things to type into a filter."""
    from PySide6.QtWidgets import QComboBox, QLineEdit, QWidget
    from trackcleaner.ui.main_window import transport_keys_blocked

    holder = QWidget()
    edit, combo, plain = QLineEdit(holder), QComboBox(holder), QWidget(holder)
    plain.setFocusPolicy(pytest.importorskip("PySide6.QtCore").Qt.StrongFocus)
    holder.show()

    edit.setFocus()
    app.processEvents()
    assert transport_keys_blocked() is True

    combo.setFocus()
    app.processEvents()
    assert transport_keys_blocked() is True

    plain.setFocus()
    app.processEvents()
    assert transport_keys_blocked() is False, "keys must work outside text fields"
    holder.close()


# --------------------------- click while playing --------------------------
class _FakePlayer:
    """Stands in for the real player so the lock can be tested without audio."""

    def __init__(self, playing=False):
        self._playing = playing
        self.loaded = []
        self._rate = 1.0

    def set_playback_rate(self, rate):
        self._rate = rate

    def playback_rate(self):
        return self._rate

    def is_playing(self):
        return self._playing

    def is_loaded(self, path):
        return False

    def load(self, path, play=False):
        self.loaded.append((path, play))
        return True

    def duration(self):
        return 0

    def position(self):
        return 0

    def pause(self):
        self._playing = False

    def seek(self, ms):
        pass

    def play(self):
        self._playing = True


def test_single_click_is_ignored_while_playing(app):
    """Browsing the list must not cut the audio or swap the waveform."""
    panel = WaveformPanel()
    first = _track([Poi(1.0, 1, "cue")])
    panel.load_track(first)
    panel.player = _FakePlayer(playing=True)

    panel.click_load(_track([Poi(5.0, 2, "cue")]))
    assert panel._track is first, "the loaded track must not change"
    assert panel.player.loaded == [], "nothing may be handed to the player"


def test_single_click_loads_when_stopped(app):
    panel = WaveformPanel()
    panel.load_track(_track([Poi(1.0, 1, "cue")]))
    panel.player = _FakePlayer(playing=False)

    other = _track([Poi(5.0, 2, "cue")])
    panel.click_load(other)
    assert panel._track is other
    assert panel.player.loaded and panel.player.loaded[0][1] is False


def test_double_click_still_switches_while_playing(app):
    """Double-click stays the deliberate 'play this now' action."""
    panel = WaveformPanel()
    panel.load_track(_track([Poi(1.0, 1, "cue")]))
    panel.player = _FakePlayer(playing=True)

    other = _track([Poi(5.0, 2, "cue")])
    panel.load_track(other, play=True)
    assert panel._track is other
    assert panel.player.loaded == [(other.path, True)]


def test_click_load_ignores_none(app):
    panel = WaveformPanel()
    panel.player = _FakePlayer(playing=False)
    panel.click_load(None)
    assert panel.player.loaded == []


def test_volume_round_trips_through_the_slider(app):
    panel = WaveformPanel()
    panel.set_volume_value(35)
    assert panel.volume_value() == 35
    assert panel.player.volume() == pytest.approx(0.35, abs=0.02)


# --------------------------------- tempo ----------------------------------
def _track_meta(bpm=None, title="", artist=""):
    return Track(key="k", path=r"D:\Music\x.mp3", source=VDJ, length=210.0,
                 bpm=bpm, title=title, artist=artist,
                 pois=[Poi(1.0, 1, "cue")])


def test_tempo_slider_geometry_matches_the_spec(app):
    from trackcleaner.ui.waveform_panel import TEMPO_MAX, TEMPO_MIN
    panel = WaveformPanel()
    s = panel.tempo
    assert (s.minimum(), s.maximum()) == (TEMPO_MIN, TEMPO_MAX)
    assert s.value() == 0, "defaults to centre / 0%"
    assert s.invertedAppearance(), "top must be the minimum (-35%), i.e. slower"


def test_tempo_anchor_points_map_to_expected_rates(app):
    from trackcleaner.ui.waveform_panel import TEMPO_MAX, TEMPO_MIN
    panel = WaveformPanel()
    for value, rate in ((TEMPO_MIN, 0.65), (0, 1.0), (TEMPO_MAX, 1.35)):
        panel.tempo.setValue(value)
        assert panel._tempo_rate() == pytest.approx(rate, abs=1e-9)
        assert panel.player.playback_rate() == pytest.approx(rate, abs=1e-9)


def test_double_click_resets_tempo_to_centre(app):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    panel = WaveformPanel()
    panel.tempo.setValue(240)
    ev = QMouseEvent(QEvent.MouseButtonDblClick, QPointF(4, 4), QPointF(4, 4),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    panel.tempo.mouseDoubleClickEvent(ev)
    assert panel.tempo.value() == 0
    assert panel.player.playback_rate() == pytest.approx(1.0)


def test_bpm_label_tracks_the_fader(app):
    panel = WaveformPanel()
    panel.load_track(_track_meta(bpm=120.0))
    assert panel.bpm_label.text() == "120.0 BPM"
    panel.tempo.setValue(100)                # +10%
    assert panel.bpm_label.text() == "132.0 BPM"
    panel.tempo.setValue(-100)               # -10%
    assert panel.bpm_label.text() == "108.0 BPM"


def test_bpm_label_handles_a_track_without_bpm(app):
    panel = WaveformPanel()
    panel.load_track(_track_meta(bpm=None))
    assert panel.bpm_label.text() == "-- BPM"


def test_title_artist_label(app):
    panel = WaveformPanel()
    panel.load_track(_track_meta(title="One Step Ahead", artist="Aretha Franklin"))
    assert panel.title_label.text() == "One Step Ahead - Aretha Franklin"


def test_title_label_omits_a_missing_field(app):
    panel = WaveformPanel()
    panel.load_track(_track_meta(title="Untitled", artist=""))
    assert panel.title_label.text() == "Untitled"


def test_fader_stays_put_across_tracks_and_bpm_recomputes(app):
    """A physical pitch fader does not reset when you load a new record; the BPM
    readout must recompute from the new track's own base."""
    panel = WaveformPanel()
    panel.load_track(_track_meta(bpm=120.0))
    panel.tempo.setValue(100)                # +10%
    assert panel.bpm_label.text() == "132.0 BPM"

    panel.load_track(_track_meta(bpm=100.0, title="Next"))
    assert panel.tempo.value() == 100, "fader must not reset on load"
    assert panel.bpm_label.text() == "110.0 BPM", "BPM uses the new track's base"


def test_tempo_value_round_trips(app):
    panel = WaveformPanel()
    panel.set_tempo_value(175)
    assert panel.tempo_value() == 175
    panel.set_tempo_value(9999)              # clamped to max
    assert panel.tempo_value() == 350


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
