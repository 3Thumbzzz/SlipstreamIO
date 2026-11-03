"""Buffered audio playback engine.

Runs against a real QAudioSink but never needs to be *heard*: volume is held at
zero and the assertions are about load/duration/seek/state. The resample maths
of the pull device is tested separately, with no audio device at all.
"""

import os
import subprocess
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer                     # noqa: E402
from PySide6.QtWidgets import QApplication                        # noqa: E402

from trackcleaner.audio import pcm as pcm_mod                     # noqa: E402
from trackcleaner.audio.player import Player, _PcmSource          # noqa: E402


def _have_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=15)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_ffmpeg = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg not on PATH")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def tone(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("audio") / "tone.mp3")
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=4", path],
        capture_output=True, timeout=60,
    )
    return path


def pump(app, ms=1500, until=None):
    """Spin the event loop until ``until`` is true or ``ms`` elapses."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    if until is not None:
        tick = QTimer()
        tick.timeout.connect(lambda: loop.quit() if until() else None)
        tick.start(20)
    loop.exec()


# ===================== pull device resample maths (no audio) ==============
def _ramp_source(frames=1000, playing=True):
    """A source whose samples are 0,1,2,... so an output value *is* its frame
    index -- resampling is then trivial to check. Audio only flows when playing."""
    ramp = np.arange(frames, dtype=np.int16)
    pcm = pcm_mod.Pcm(left=ramp.copy(), right=ramp.copy(), rate=pcm_mod.SAMPLE_RATE)
    src = _PcmSource(pcm)
    src.playing = playing
    return src


def _read_left(src, frames):
    raw = src.readData(frames * 4)
    inter = np.frombuffer(raw, dtype=np.int16)
    return inter[0::2]                       # left channel == frame index


def test_ratio_one_is_identity():
    out = _read_left(_ramp_source(), 8)
    assert list(out) == [0, 1, 2, 3, 4, 5, 6, 7]


def test_ratio_two_halves_the_sample_span():
    """At 2x, consecutive output frames step by two source frames."""
    src = _ramp_source()
    src.ratio = 2.0
    out = _read_left(src, 8)
    assert list(out) == [0, 2, 4, 6, 8, 10, 12, 14]


def test_paused_source_emits_silence_and_freezes_the_cursor():
    src = _ramp_source(playing=False)
    out = _read_left(src, 8)
    assert list(out) == [0, 0, 0, 0, 0, 0, 0, 0], "paused output is silence"
    assert src.position_frame() == 0, "cursor must not advance while paused"


def test_seek_applies_even_while_paused():
    """Seeking a stopped track must move the position so the playhead follows
    and playback resumes from there."""
    src = _ramp_source(playing=False)
    src.seek_frame(500)
    assert src.position_frame() == 500
    _read_left(src, 4)                       # a paused read applies the seek
    assert src.position_frame() == 500, "still frozen at the sought frame"
    src.playing = True
    assert list(_read_left(src, 3)) == [500, 501, 502]


def test_pending_seek_lands_on_the_next_read_only():
    src = _ramp_source()
    _read_left(src, 4)                       # advance to frame 4
    src.seek_frame(500)
    assert src.position_frame() == 500, "position reflects the seek immediately"
    assert list(_read_left(src, 3)) == [500, 501, 502], "applies on the next read"
    assert src.pending_seek is None, "and exactly once"
    assert list(_read_left(src, 2)) == [503, 504], "then playback continues"


def test_reading_past_the_end_emits_ended_once():
    src = _ramp_source(frames=10)
    fired = []
    src.ended.connect(lambda: fired.append(True))
    _read_left(src, 100)                     # asks for far more than exists
    assert src.exhausted
    assert not src.playing, "playback stops at the end"
    assert fired == [True], "ended fires exactly once"
    _read_left(src, 8)                       # further reads are just silence
    assert fired == [True]


# ============================ the player ==================================
@needs_ffmpeg
def test_load_reports_a_duration_matching_the_file(app, tone):
    p = Player()
    p.set_volume(0.0)
    assert p.load(tone) is True
    pump(app, until=lambda: p.duration() > 0)
    assert p.duration() == pytest.approx(4000, abs=200)


@needs_ffmpeg
def test_seek_updates_position_instantly(app, tone):
    p = Player()
    p.set_volume(0.0)
    p.load(tone)
    pump(app, until=lambda: p.duration() > 0)
    p.seek(2500)
    assert p.position() == pytest.approx(2500, abs=20), "no decode wait"


@needs_ffmpeg
def test_sink_and_buffer_survive_load(app, tone):
    """A QAudioSink or its pull device left to the garbage collector plays
    silently with no error -- the player must keep its own references."""
    p = Player()
    p.set_volume(0.0)
    p.load(tone)
    pump(app, until=lambda: p.duration() > 0)
    assert p._sink is not None
    assert p._source is not None
    assert p._pcm is not None


@needs_ffmpeg
def test_stream_is_warm_immediately_after_load(app, tone):
    """The sink runs (silently) the moment a track is loaded, even without play,
    so a later cue has no cold start to wait through."""
    from PySide6.QtMultimedia import QAudio
    p = Player()
    p.set_volume(0.0)
    p.load(tone)
    pump(app, until=lambda: p.duration() > 0)
    pump(app, 200)
    # PySide6 enum equality on state() is unreliable; compare by value.
    assert p._sink.state().value == QAudio.State.ActiveState.value
    assert not p.is_playing()
    assert p.position() == 0, "paused stream does not advance"


@needs_ffmpeg
def test_hold_cue_from_a_stopped_track_starts_playing_from_the_cue(app, tone):
    """The reported regression: a stopped track must preview from the cue. With
    the warm stream, seek+play just flips the flag -- no cold start."""
    p = Player()
    p.set_volume(0.0)
    p.load(tone)                             # loaded, never played
    pump(app, until=lambda: p.duration() > 0)
    pump(app, 100)
    p.seek(2000)
    p.play()
    pump(app, 300, until=lambda: p.position() > 2000)
    assert p.is_playing()
    assert p.position() > 2000, "playback advanced from the cue point"


@needs_ffmpeg
def test_play_requested_before_the_media_is_ready_still_starts(app, tone):
    """load(play=True) is issued while the decode is still running; the request
    must be honoured, not dropped."""
    p = Player()
    p.set_volume(0.0)
    p.load(tone, play=True)
    pump(app, 4000, until=lambda: p.is_playing())
    assert p.is_playing(), "play-when-ready was dropped"
    p.pause()


@needs_ffmpeg
def test_tempo_change_mid_playback_does_not_cut_out(app, tone):
    """The headline fix: changing rate keeps playback going and the position
    advancing -- no restart."""
    p = Player()
    p.set_volume(0.0)
    p.load(tone, play=True)
    pump(app, 4000, until=lambda: p.is_playing() and p.position() > 100)
    before = p.position()
    p.set_playback_rate(1.35)
    pump(app, 300)
    mid = p.position()
    p.set_playback_rate(0.65)
    pump(app, 300)
    after = p.position()
    assert p.is_playing(), "tempo change must not stop playback"
    assert before < mid < after, "position must keep advancing across the change"
    p.pause()


@needs_ffmpeg
def test_pause_stops_playing(app, tone):
    p = Player()
    p.set_volume(0.0)
    p.load(tone, play=True)
    pump(app, 4000, until=lambda: p.is_playing())
    p.pause()
    pump(app, 500, until=lambda: not p.is_playing())
    assert not p.is_playing()


@needs_ffmpeg
def test_volume_round_trips(app, tone):
    p = Player()
    p.set_volume(0.25)
    assert p.volume() == pytest.approx(0.25, abs=0.02)
    p.set_volume(5.0)                       # clamped
    assert p.volume() == pytest.approx(1.0, abs=0.02)
    p.set_volume(-1.0)
    assert p.volume() == pytest.approx(0.0, abs=0.02)


@needs_ffmpeg
def test_is_loaded_is_case_insensitive(app, tone):
    p = Player()
    p.set_volume(0.0)
    p.load(tone)
    assert p.is_loaded(tone.upper())
    assert not p.is_loaded(tone + "x")


def test_playback_rate_round_trips_and_is_clamped(app):
    p = Player()
    p.set_playback_rate(1.2)
    assert p.playback_rate() == pytest.approx(1.2, abs=1e-6)
    p.set_playback_rate(99.0)                # clamped
    assert p.playback_rate() <= 4.0
    p.set_playback_rate(0.0)
    assert p.playback_rate() >= 0.25


@needs_ffmpeg
def test_rate_is_applied_to_a_newly_loaded_track(app, tone):
    """The fader stays put across tracks, so a fresh decode must start at the
    stored rate rather than 1.0."""
    p = Player()
    p.set_volume(0.0)
    p.set_playback_rate(0.8)
    p.load(tone)
    pump(app, until=lambda: p.duration() > 0)
    assert p._source.ratio == pytest.approx(0.8, abs=1e-6)


@needs_ffmpeg
def test_reaching_the_end_emits_finished(app, tmp_path):
    """Playing off the end of the buffer must stop and fire finished (which the
    panel routes to the transport's playback_finished)."""
    path = str(tmp_path / "short.mp3")
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=0.4", path],
        capture_output=True, timeout=60,
    )
    p = Player()
    p.set_volume(0.0)
    done = []
    p.finished.connect(lambda: done.append(True))
    p.load(path, play=True)
    pump(app, 5000, until=lambda: bool(done))
    assert done, "finished never fired at end of track"
    assert not p.is_playing()


def test_missing_file_reports_an_error_instead_of_raising(app, tmp_path):
    p = Player()
    seen = []
    p.error.connect(seen.append)
    assert p.load(str(tmp_path / "nope.mp3")) is False
    assert seen and "not found" in seen[0].lower()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
