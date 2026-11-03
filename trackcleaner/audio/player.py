"""Buffered audio playback: decode to memory, play through a pull QAudioSink.

Why not QMediaPlayer: it streams and decodes on demand, so changing the playback
rate re-negotiates the pipeline (the sound drops out) and a seek re-decodes (cue
jumps lag by hundreds of ms). Here the whole track is decoded up front
(:mod:`trackcleaner.audio.pcm`) and we feed a :class:`QAudioSink` ourselves from
a pull device. Then:

* **tempo** is just a resample ratio the pull callback reads each buffer -- no
  restart, no cutout;
* **a cue jump** is moving a sample cursor -- no decode, so it lands almost at
  once.

The public surface (``load / play / pause / stop / seek / position / duration /
is_playing / set_volume / set_playback_rate`` + the ``duration_changed /
playing_changed / finished / error`` signals) is unchanged, so the waveform panel
and the transport state machine did not have to change.

Footgun carried over from the old QMediaPlayer version: the sink, the pull
source and the sample buffers must be **held as attributes**. A QAudioSink (or
its QIODevice) left to the garbage collector goes silent with no error.
"""

from __future__ import annotations

import os

import numpy as np
from PySide6.QtCore import (QIODevice, QObject, QRunnable, Qt, QThreadPool,
                            Signal)
from PySide6.QtMultimedia import (QAudio, QAudioFormat, QAudioSink,
                                  QMediaDevices)

from . import pcm as pcm_mod

# Residual cue latency = how much audio the sink queues ahead. ~20 ms at 48 kHz
# is close to Traktor's 21 ms and runs clean on the main thread; the lever to
# pull if cues still feel late.
_BUFFER_MS = 20

# PySide6 returns a fresh QAudio.State wrapper from state()/the signal whose
# ``==`` against the enum member is False even when identical -- only ``.value``
# compares reliably. Compare by value everywhere.
_ACTIVE = QAudio.State.ActiveState.value
_IDLE = QAudio.State.IdleState.value


def _state_value(state) -> int:
    return state.value if hasattr(state, "value") else int(state)


class _PcmSource(QIODevice):
    """Pull device the sink reads from -- kept running for the whole time a
    track is loaded (a "warm stream"), so play/pause/cue never has to cold-start
    the audio device. It resamples on the fly for tempo, takes a pending seek so
    a cue lands exactly once on the next buffer, and outputs silence while
    paused.

    ``readData`` runs on the main thread (verified), so the shared scalars
    (``cursor``, ``ratio``, ``pending_seek``, ``playing``) need no locking.
    """

    ended = Signal()                # emitted once when playback runs off the end

    def __init__(self, pcm: pcm_mod.Pcm, parent=None):
        super().__init__(parent)
        self._pcm = pcm
        self._n = pcm.frames
        self.cursor = 0.0            # fractional frame index
        self.ratio = 1.0            # playback rate (also shifts pitch)
        self.pending_seek: float | None = None
        self.playing = False        # when False, readData emits silence
        self.exhausted = False
        self.open(QIODevice.ReadOnly)

    def isSequential(self) -> bool:
        return True

    def bytesAvailable(self) -> int:
        # There is always something to give (silence or audio), so the sink
        # never idles from our side -- it just keeps pulling.
        return 1 << 20

    def _effective_cursor(self) -> float:
        return self.pending_seek if self.pending_seek is not None else self.cursor

    def seek_frame(self, frame: float) -> None:
        self.pending_seek = max(0.0, min(float(frame), float(self._n)))
        self.exhausted = False

    def position_frame(self) -> float:
        return self._effective_cursor()

    def readData(self, maxlen: int) -> bytes:
        frames = int(maxlen) // 4
        if frames <= 0:
            return bytes()

        # Apply a queued seek first, even while paused, so position() tracks it.
        if self.pending_seek is not None:
            self.cursor = self.pending_seek
            self.pending_seek = None

        if not self.playing:
            return b"\x00\x00\x00\x00" * frames     # silence, cursor frozen

        idx = self.cursor + np.arange(frames, dtype=np.float64) * self.ratio
        # Keep only samples that have a right-hand neighbour to interpolate to.
        idx = idx[idx < self._n - 1]
        if idx.size == 0:
            self.cursor = float(self._n)
            self.playing = False
            if not self.exhausted:
                self.exhausted = True
                self.ended.emit()               # queued -> runs after readData
            return b"\x00\x00\x00\x00" * frames

        i0 = idx.astype(np.int64)
        frac = idx - i0
        out = np.empty(idx.size * 2, dtype=np.int16)
        for ch, dst in ((self._pcm.left, 0), (self._pcm.right, 1)):
            a = ch[i0].astype(np.float32)
            b = ch[i0 + 1].astype(np.float32)
            out[dst::2] = (a + (b - a) * frac).astype(np.int16)

        self.cursor += idx.size * self.ratio
        if self.cursor >= self._n - 1:
            self.playing = False
            if not self.exhausted:
                self.exhausted = True
                self.ended.emit()
        # Pad to the full requested size so the sink stays fed (avoids an idle).
        tail = frames - idx.size
        return out.tobytes() + (b"\x00\x00\x00\x00" * tail if tail > 0 else b"")


class _DecodeSignals(QObject):
    done = Signal(int, object)          # generation, Pcm | None


class _DecodeJob(QRunnable):
    """Decodes off the UI thread; the generation lets a rapid track change drop
    a stale result."""

    def __init__(self, path: str, generation: int, signals: _DecodeSignals):
        super().__init__()
        self._path, self._gen, self._signals = path, generation, signals

    def run(self):
        result = pcm_mod.load_pcm(self._path)
        try:
            self._signals.done.emit(self._gen, result)
        except RuntimeError:
            pass                        # player torn down mid-decode


class Player(QObject):
    """Plays one track at a time. Positions and durations are milliseconds."""

    duration_changed = Signal(int)
    playing_changed = Signal(bool)
    finished = Signal()
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fmt = QAudioFormat()
        self._fmt.setSampleRate(pcm_mod.SAMPLE_RATE)
        self._fmt.setChannelCount(pcm_mod.CHANNELS)
        self._fmt.setSampleFormat(QAudioFormat.Int16)

        # Held for the object's lifetime -- see the module docstring.
        self._device = QMediaDevices.defaultAudioOutput()
        self._sink: QAudioSink | None = None
        self._source: _PcmSource | None = None
        self._pcm: pcm_mod.Pcm | None = None

        self._path = ""
        self._rate = 1.0
        self._volume = 0.8
        self._playing = False
        self._play_when_ready = False
        self._generation = 0

        self._pool = QThreadPool.globalInstance()
        self._decode_signals = _DecodeSignals(self)
        self._decode_signals.done.connect(self._on_decoded)

    # ------------------------------ loading ------------------------------
    def load(self, path: str, play: bool = False) -> bool:
        if not path or not os.path.exists(path):
            self._teardown()
            self._path = ""
            self.error.emit(f"File not found: {path}")
            return False
        self._path = path
        self._play_when_ready = play
        self._teardown()                # stop and drop the previous track
        self._generation += 1
        self._pool.start(_DecodeJob(path, self._generation, self._decode_signals))
        return True

    def _on_decoded(self, generation: int, result) -> None:
        if generation != self._generation:
            return                      # a newer load supersedes this one
        if result is None:
            self.error.emit(f"Cannot play this file: {os.path.basename(self._path)}")
            return
        self._pcm = result
        self._source = _PcmSource(result, self)
        self._source.ratio = self._rate
        self._source.ended.connect(self._on_ended, Qt.QueuedConnection)
        self._sink = QAudioSink(self._device, self._fmt, self)
        self._sink.setVolume(self._volume)
        buffer_bytes = int(pcm_mod.SAMPLE_RATE * _BUFFER_MS / 1000) * 4
        self._sink.setBufferSize(buffer_bytes)
        self._sink.stateChanged.connect(self._on_sink_state)
        # Start the stream immediately and leave it running -- it outputs silence
        # until we set playing. This is what removes the cold-start latency that
        # made a hold-to-preview from a stopped track produce no sound.
        self._sink.start(self._source)

        self.duration_changed.emit(self.duration())
        if self._play_when_ready:
            self._play_when_ready = False
            self._start()

    # ------------------------------ queries ------------------------------
    @property
    def path(self) -> str:
        return self._path

    def is_loaded(self, path: str) -> bool:
        return bool(self._path) and os.path.normcase(self._path) == os.path.normcase(path or "")

    def position(self) -> int:
        if self._source is None:
            return 0
        return int(self._source.position_frame() / pcm_mod.SAMPLE_RATE * 1000)

    def duration(self) -> int:
        if self._pcm is None:
            return 0
        return int(self._pcm.duration * 1000)

    def is_playing(self) -> bool:
        return self._playing

    # ----------------------------- transport -----------------------------
    def play(self) -> None:
        if self._source is None:
            self._play_when_ready = True    # not decoded yet; _on_decoded starts
            return
        self._start()

    def _start(self) -> None:
        if self._source is None or self._sink is None:
            return
        # Restart from the top if the track had played out.
        if self._source.exhausted or self._source.position_frame() >= self._pcm.frames - 1:
            self._source.seek_frame(0)
        # The stream is normally already running (started in _on_decoded); only
        # restart it if it actually stopped (e.g. an underrun). Restarting an
        # already-active sink would cold-start it and defeat the whole point.
        if _state_value(self._sink.state()) not in (_ACTIVE, _IDLE):
            self._sink.start(self._source)
        self._source.playing = True
        self._set_playing(True)

    def pause(self) -> None:
        self._play_when_ready = False
        if self._source is not None:        # sink keeps running, now silent
            self._source.playing = False
        self._set_playing(False)

    def stop(self) -> None:
        self._play_when_ready = False
        if self._source is not None:
            self._source.playing = False
            self._source.seek_frame(0)
        self._set_playing(False)

    def seek(self, ms: int) -> None:
        if self._source is None:
            return
        frame = max(0, int(ms)) / 1000.0 * pcm_mod.SAMPLE_RATE
        self._source.seek_frame(frame)

    # ------------------------------- volume ------------------------------
    def set_volume(self, value: float) -> None:
        self._volume = max(0.0, min(1.0, float(value)))
        if self._sink is not None:
            self._sink.setVolume(self._volume)

    def volume(self) -> float:
        return self._volume

    # -------------------------------- tempo ------------------------------
    def set_playback_rate(self, rate: float) -> None:
        """Live speed multiplier (also shifts pitch). No restart -> no cutout."""
        self._rate = max(0.25, min(4.0, float(rate)))
        if self._source is not None:
            self._source.ratio = self._rate

    def playback_rate(self) -> float:
        return self._rate

    # ------------------------------ internals ----------------------------
    def _set_playing(self, playing: bool) -> None:
        if playing != self._playing:
            self._playing = playing
            self.playing_changed.emit(playing)

    def _on_ended(self) -> None:
        # Reached the end of the track (from the source's ended signal). The sink
        # stays running, silent, ready for the next cue.
        self._set_playing(False)
        self.finished.emit()

    def _on_sink_state(self, state) -> None:
        # We always feed a full buffer (silence or audio), so the sink should not
        # idle from our side; if it does the device underran, so restart it to
        # keep the stream warm.
        if _state_value(state) == _IDLE and self._sink is not None \
                and self._source is not None:
            self._sink.start(self._source)

    def _teardown(self) -> None:
        if self._sink is not None:
            try:
                self._sink.stateChanged.disconnect(self._on_sink_state)
            except (RuntimeError, TypeError):
                pass
            self._sink.stop()
        self._sink = None
        self._source = None
        self._pcm = None
        self._set_playing(False)
