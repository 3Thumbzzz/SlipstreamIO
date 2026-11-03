"""Play / hotcue transport rules (no Qt, no audio device).

The hold-to-preview behaviour has more branches than it looks, so each rule and
each overlapping-hold case gets its own test.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.audio.transport import PAUSE, PLAY, SEEK, Transport  # noqa: E402


def kinds(intents):
    return [i.kind for i in intents]


# --------------------------- press while stopped ---------------------------
def test_press_while_stopped_jumps_and_plays():
    t = Transport()
    out = t.cue_pressed(3, 12345)
    assert kinds(out) == [SEEK, PLAY]
    assert out[0].pos == 12345
    assert t.playing and t.previewing


def test_release_snaps_back_to_the_cue_and_pauses():
    t = Transport()
    t.cue_pressed(3, 12345)
    out = t.cue_released(3)
    assert kinds(out) == [PAUSE, SEEK]
    assert out[1].pos == 12345, "must return to the cue, not wherever it got to"
    assert not t.playing and not t.previewing


# --------------------------- press while playing ---------------------------
def test_press_while_playing_jumps_and_keeps_playing():
    t = Transport()
    t.play_pressed()                       # normal playback
    out = t.cue_pressed(5, 900)
    assert kinds(out) == [SEEK], "must not restart or pause"
    assert out[0].pos == 900
    assert t.playing
    assert not t.previewing, "plain playback must not arm a snap-back"


def test_release_after_press_while_playing_does_nothing():
    t = Transport()
    t.play_pressed()
    t.cue_pressed(5, 900)
    assert t.cue_released(5) == []
    assert t.playing, "letting go must not stop deliberate playback"


# ------------------------------ overlapping holds --------------------------
def test_second_cue_takes_over_the_preview():
    t = Transport()
    t.cue_pressed(1, 100)                  # preview armed on cue 1
    out = t.cue_pressed(2, 200)            # press 2 without releasing 1
    assert kinds(out) == [SEEK] and out[0].pos == 200
    assert t.playing

    assert t.cue_released(1) == [], "the older cue no longer owns the preview"
    assert t.playing, "releasing the older cue must not cut playback"

    out = t.cue_released(2)
    assert kinds(out) == [PAUSE, SEEK] and out[1].pos == 200
    assert not t.playing


def test_releasing_a_cue_that_was_never_held_is_ignored():
    t = Transport()
    t.cue_pressed(1, 100)
    assert t.cue_released(7) == []
    assert t.playing and t.previewing


# --------------------------- play button / W key ---------------------------
def test_play_toggles_from_a_standing_start():
    t = Transport()
    assert kinds(t.play_pressed()) == [PLAY]
    assert t.playing
    assert kinds(t.play_pressed()) == [PAUSE]
    assert not t.playing


def test_play_during_a_preview_converts_it_to_normal_playback():
    """Once you explicitly ask for playback, letting go of the cue must not
    silently stop it."""
    t = Transport()
    t.cue_pressed(4, 4000)
    assert t.previewing
    t.play_pressed()                       # cancels the preview
    assert not t.previewing

    assert t.cue_released(4) == [], "release must no longer pause"
    # play_pressed toggled a playing transport, so it is paused but not snapped
    assert not t.playing


def test_play_after_preview_release_starts_again():
    t = Transport()
    t.cue_pressed(2, 500)
    t.cue_released(2)
    assert kinds(t.play_pressed()) == [PLAY]
    assert t.playing


# ------------------------------- lifecycle ---------------------------------
def test_changing_track_drops_all_state():
    t = Transport()
    t.cue_pressed(6, 60)
    out = t.track_changed()
    assert kinds(out) == [PAUSE]
    assert not t.playing and not t.previewing
    assert t.cue_released(6) == [], "stale hold must not resurrect a preview"


def test_finishing_playback_clears_state():
    t = Transport()
    t.play_pressed()
    t.playback_finished()
    assert not t.playing and not t.previewing
    assert kinds(t.play_pressed()) == [PLAY], "play must work again after the end"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
