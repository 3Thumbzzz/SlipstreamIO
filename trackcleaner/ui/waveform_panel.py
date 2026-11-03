"""Waveform preview panel: envelope, transport, 8 hotcue buttons, volume.

Sits full width between the top bar and the main splitter. Single-clicking a
track loads it; double-clicking loads and plays it.

Hotcue rules live in :mod:`trackcleaner.audio.transport` (a plain state machine,
so they are testable without audio); playback lives in
:mod:`trackcleaner.audio.player`. This module is the view plus the wiring.
"""

from __future__ import annotations

from PySide6.QtCore import (QMimeData, QObject, QRunnable, Qt, QThreadPool,
                            QTimer, QUrl, Signal)
from PySide6.QtGui import QColor, QDrag, QFont, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QSlider, QVBoxLayout, QWidget)

from ..audio import peaks as peaks_mod
from ..audio.player import Player
from ..audio.transport import PAUSE, PLAY, SEEK, Transport
from ..model.library import Track
from . import theme as theme_mod

CUE_COUNT = 8
_EPS = 1e-6


class _TitleLabel(QLabel):
    """The loaded-track title. Dragging it out carries the track's file path as a
    ``text/uri-list`` url, so it can be dropped on a playlist to add/import the
    file being previewed (the same drop path Explorer files use).

    Right-clicking it opens the same menu as right-clicking the track in a list:
    while a track is loaded this label *is* that track, and offering a different
    set of actions depending on where you clicked the same thing would be an
    arbitrary distinction to remember.
    """

    context_requested = Signal(object)          # global position

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.path = ""
        self._press = None
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self.context_requested.emit(self.mapToGlobal(pos)))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.path:
            self._press = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._press is None or not (event.buttons() & Qt.LeftButton)
                or not self.path):
            return super().mouseMoveEvent(event)
        if (event.position().toPoint() - self._press).manhattanLength() < 8:
            return
        self._press = None
        drag = QDrag(self)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(self.path)])
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)

# Tempo fader: 0.1% steps over -35%..+35%.
TEMPO_MIN = -350
TEMPO_MAX = 350

# What plays when a track finishes (the "On end" selector). main_window resolves
# the actual next track from these keys, since it owns the tab/playlist context.
ON_END_SINGLE = "single"
ON_END_ORDER = "order"
ON_END_SORT = "sort"
ON_END_RANDOM_PLAYLIST = "random_playlist"
ON_END_RANDOM_SUBFOLDER = "random_subfolder"
ON_END_RANDOM_LIBRARY = "random_library"
ON_END_MODES = [
    (ON_END_SINGLE, "Single file"),
    (ON_END_ORDER, "Order (playlist)"),
    (ON_END_SORT, "Sort order"),
    (ON_END_RANDOM_PLAYLIST, "Random (playlist)"),
    (ON_END_RANDOM_SUBFOLDER, "Random (subfolder)"),
    (ON_END_RANDOM_LIBRARY, "Random (library)"),
]


def _title_artist(track: Track | None) -> str:
    if track is None:
        return ""
    parts = [p for p in (track.title, track.artist) if p]
    return " - ".join(parts)


def format_time(ms: float, remaining: bool = False) -> str:
    """``mm:ss:ms`` -- or ``-mm:ss:ms`` when showing time remaining."""
    sign = "-" if remaining else ""
    ms = max(0, int(round(ms)))
    minutes, rest = divmod(ms, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{sign}{minutes:02d}:{seconds:02d}:{millis:03d}"


def cue_positions(track: Track | None) -> dict[int, float]:
    """Hotcue number (1-8) -> position in seconds, for cues this track has."""
    if track is None:
        return {}
    out = {}
    for poi in track.cue_pois():
        if poi.num is not None and 1 <= poi.num <= CUE_COUNT:
            out.setdefault(int(poi.num), float(poi.pos))
    return out


def grid_cue_numbers(track: Track | None) -> set[int]:
    """Cues that sit on a beatgrid anchor -- Traktor "Beat Marker"s.

    ``pois_from_cue_v2`` emits a beatgrid Poi *and* a cue Poi at the same
    position for TYPE=4, which is the only trace of the original cue type left
    in our model.
    """
    if track is None:
        return set()
    grid = [p.pos for p in track.grid_pois()]
    if not grid:
        return set()
    return {n for n, pos in cue_positions(track).items()
            if any(abs(pos - g) < 1e-4 for g in grid)}


# ----------------------------- decode worker -----------------------------
class _PeakSignals(QObject):
    done = Signal(int, object)          # generation, Peaks | None


class _PeakJob(QRunnable):
    """Decodes off the UI thread. The generation counter lets the panel drop
    results for a track the user has already clicked away from."""

    def __init__(self, path: str, generation: int, signals: _PeakSignals):
        super().__init__()
        self._path, self._gen, self._signals = path, generation, signals

    def run(self):
        result = peaks_mod.load_peaks(self._path)
        try:
            self._signals.done.emit(self._gen, result)
        except RuntimeError:
            # The panel was destroyed while we were decoding (app closing).
            # There is nobody left to deliver to, which is not an error.
            pass


# ------------------------------ waveform view -----------------------------
class WaveformView(QWidget):
    """Paints the envelope, hotcue ticks and the playhead."""

    seek_requested = Signal(float)       # fraction 0..1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(70)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._peaks = None
        self._message = "Click a track to load its waveform"
        self._duration = 0.0            # seconds
        self._position = 0.0            # seconds
        self._cues: dict[int, float] = {}
        self._grid_cues: set[int] = set()

    def set_message(self, text: str):
        self._peaks = None
        self._message = text
        self.update()

    def set_peaks(self, peaks, duration: float):
        self._peaks = peaks
        self._message = ""
        self._duration = duration
        self.update()

    def set_duration(self, seconds: float):
        self._duration = seconds
        self.update()

    def set_cues(self, cues: dict[int, float], grid_cues: set[int]):
        self._cues, self._grid_cues = cues, grid_cues
        self.update()

    def set_position(self, seconds: float):
        self._position = seconds
        self.update()

    def paintEvent(self, _event):
        th = theme_mod.current()
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(theme_mod.WAVE_BG[th]))

        if self._peaks is None:
            p.setPen(QColor(theme_mod.WAVE_TEXT[th]))
            p.drawText(self.rect(), Qt.AlignCenter, self._message)
            return

        mid = h / 2.0
        p.setPen(QPen(QColor(theme_mod.WAVE_MID[th]), 1))
        p.drawLine(0, int(mid), w, int(mid))

        lo, hi = self._peaks.binned(w)
        p.setPen(QPen(QColor(theme_mod.WAVE_FG[th]), 1))
        for x in range(min(w, len(lo))):
            y1 = mid - hi[x] * mid
            y2 = mid - lo[x] * mid
            p.drawLine(x, int(y1), x, int(y2))

        if self._duration > _EPS:
            for num, pos in self._cues.items():
                x = int(pos / self._duration * w)
                if not 0 <= x <= w:
                    continue
                colour = (theme_mod.CUE_GRID if num in self._grid_cues
                          else theme_mod.CUE_PLAIN)[th]
                p.setPen(QPen(QColor(colour), 2))
                p.drawLine(x, 0, x, h)
                p.setFont(QFont(self.font().family(), 7, QFont.Bold))
                p.drawText(x + 3, 11, str(num))

            x = int(self._position / self._duration * w)
            p.setPen(QPen(QColor(theme_mod.WAVE_PLAYHEAD[th]), 2))
            p.drawLine(x, 0, x, h)

    # click / drag to seek
    def mousePressEvent(self, event):
        self._emit_seek(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._emit_seek(event)

    def _emit_seek(self, event):
        if self._peaks is None or self.width() <= 0:
            return
        self.seek_requested.emit(
            max(0.0, min(1.0, event.position().x() / self.width())))


# ------------------------------ tempo fader -------------------------------
class TempoSlider(QSlider):
    """Vertical pitch fader: top = -35%, centre = 0%, bottom = +35%.

    Inverted appearance puts the minimum (-350) at the top, matching a request
    where slowing the track is upward. Double-clicking snaps back to 0%.
    """

    reset_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(Qt.Vertical, parent)
        self.setRange(TEMPO_MIN, TEMPO_MAX)
        self.setValue(0)
        self.setInvertedAppearance(True)
        self.setTickPosition(QSlider.TicksBothSides)
        self.setTickInterval(TEMPO_MAX)          # a mark at -35/0/+35
        self.setFixedWidth(34)
        self.setToolTip("Tempo 0.0%  (double-click to reset)")

    def mouseDoubleClickEvent(self, event):
        self.setValue(0)
        self.reset_requested.emit()
        event.accept()


# ------------------------------- the panel --------------------------------
class WaveformPanel(QWidget):
    """Time readout, waveform, and the transport row."""

    track_finished = Signal()            # the current track played to its end
    # The audio could not be read: the file is missing, unreadable or not
    # decodable. Carries the track key so the window can mark it in the same
    # place the integrity check does, rather than the panel reaching into it.
    track_unreadable = Signal(str)
    # Step through the visible list. The panel does not know what "the list" is
    # -- that is the focused tab's business -- so it asks rather than resolves.
    next_requested = Signal()
    previous_requested = Signal()
    # Right-click on the loaded track's name: global position.
    title_context_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.player = Player(self)
        self.transport = Transport()
        self._track: Track | None = None
        self._cues: dict[int, float] = {}
        self._generation = 0
        self._pool = QThreadPool.globalInstance()
        self._peak_signals = _PeakSignals(self)      # lifetime tied to the panel
        self._peak_signals.done.connect(self._on_peaks)
        self._show_remaining = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 2, 4, 2)
        outer.setSpacing(3)

        # Top row: Title - Artist | elapsed time | BPM.
        mono = QFont("Consolas")
        mono.setPointSize(11)
        info_row = QHBoxLayout()
        self.title_label = _TitleLabel("")
        self.title_label.context_requested.connect(self.title_context_requested)
        self.title_label.setToolTip(
            "Title - Artist of the loaded track — drag onto a playlist to add it")
        # Ignored width so a long title clips rather than shoving the centred
        # time off-centre.
        self.title_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        info_row.addWidget(self.title_label, 1, Qt.AlignLeft | Qt.AlignVCenter)

        self.time_label = QLabel(f"{format_time(0)} / {format_time(0)}")
        self.time_label.setAlignment(Qt.AlignCenter)
        self.time_label.setCursor(Qt.PointingHandCursor)
        self.time_label.setToolTip("Click to switch between elapsed and remaining")
        self.time_label.setFont(mono)
        self.time_label.mousePressEvent = self._toggle_time_mode
        info_row.addWidget(self.time_label, 0, Qt.AlignCenter)

        self.bpm_label = QLabel("-- BPM")
        self.bpm_label.setFont(mono)
        self.bpm_label.setToolTip("BPM, adjusted by the tempo fader")
        self.bpm_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        info_row.addWidget(self.bpm_label, 1, Qt.AlignRight | Qt.AlignVCenter)
        outer.addLayout(info_row)

        # Middle row: waveform view + the vertical tempo fader on its right.
        view_row = QHBoxLayout()
        view_row.setSpacing(4)
        self.view = WaveformView()
        self.view.seek_requested.connect(self._on_seek_fraction)
        view_row.addWidget(self.view, 1)
        self.tempo = TempoSlider()
        self.tempo.valueChanged.connect(self._on_tempo)
        view_row.addWidget(self.tempo)
        outer.addLayout(view_row, 1)

        outer.addLayout(self._build_transport())

        self.player.duration_changed.connect(self._on_duration)
        self.player.playing_changed.connect(self._on_playing)
        self.player.finished.connect(self._on_finished)

        self._tick = QTimer(self)
        self._tick.setInterval(33)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()
        self._refresh_time()

    def _build_transport(self) -> QHBoxLayout:
        row = QHBoxLayout()
        # Previous / next step through the list you are looking at, in its own
        # visible order -- deliberately not the "on end" mode, which includes
        # random choices nobody wants from a Next button.
        self.prev_btn = QPushButton("⏮")
        self.prev_btn.setFixedWidth(36)
        self.prev_btn.setToolTip("Previous track")
        self.prev_btn.clicked.connect(self.previous_requested)
        row.addWidget(self.prev_btn)

        self.play_btn = QPushButton("▶")
        self.play_btn.setFixedWidth(44)
        self.play_btn.setToolTip("Play / pause  (W)")
        self.play_btn.clicked.connect(self.toggle_play)
        row.addWidget(self.play_btn)

        self.next_btn = QPushButton("⏭")
        self.next_btn.setFixedWidth(36)
        self.next_btn.setToolTip("Next track")
        self.next_btn.clicked.connect(self.next_requested)
        row.addWidget(self.next_btn)
        row.addSpacing(8)

        self.cue_buttons = []
        for n in range(1, CUE_COUNT + 1):
            b = QPushButton(str(n))
            b.setFixedWidth(34)
            b.setToolTip(f"Hotcue {n}  (key {n})")
            b.pressed.connect(lambda num=n: self.cue_pressed(num))
            b.released.connect(lambda num=n: self.cue_released(num))
            row.addWidget(b)
            self.cue_buttons.append(b)

        row.addStretch(1)
        row.addWidget(QLabel("On end"))
        self.on_end = QComboBox()
        for key, label in ON_END_MODES:
            self.on_end.addItem(label, key)
        self.on_end.setToolTip("What plays when the current track finishes")
        row.addWidget(self.on_end)
        row.addSpacing(10)

        row.addWidget(QLabel("Vol"))
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setFixedWidth(110)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.valueChanged.connect(lambda v: self.player.set_volume(v / 100.0))
        row.addWidget(self.volume)
        return row

    # ------------------------------ loading ------------------------------
    def click_load(self, track: Track | None) -> bool:
        """Single-click semantics: load only while nothing is playing.

        Browsing the track list must not cut the audio, so a click during
        playback does nothing at all here -- the row still highlights, because
        that is the table's own doing and we simply decline to react. A held
        hotcue preview counts as playing and is likewise left alone.

        Returns whether it loaded, so the caller can tell a real change of track
        from a click it declined -- the play history must not record the latter.
        """
        if self.player.is_playing() or track is None:
            return False
        self.load_track(track)
        return True

    def load_track(self, track: Track | None, play: bool = False):
        """Show ``track``. ``play=True`` (double-click) starts playback at once
        without waiting for the waveform to decode."""
        if track is None:
            return
        if self.player.is_loaded(track.path) and self._track is not None:
            # Same track: this is the double-click that follows the click which
            # already loaded it, so just start playing rather than reloading.
            if play and not self.transport.playing:
                self.toggle_play()
            return

        self._track = track
        self.transport.track_changed()
        self._cues = cue_positions(track)
        self.view.set_cues(self._cues, grid_cue_numbers(track))
        self._sync_cue_buttons()
        self.title_label.setText(_title_artist(track))
        self.title_label.path = track.path or ""   # draggable onto a playlist
        self._refresh_bpm()                        # recompute from this track's BPM

        duration = float(track.length or 0.0)
        self.view.set_duration(duration)
        self.view.set_position(0.0)
        self.view.set_message("decoding…")
        # Show the metadata length straight away; the player's own duration
        # replaces it a moment later, but the readout must not sit at 00:00:000.
        self._refresh_time()

        if not self.player.load(track.path, play=play):
            self.view.set_message("File not found")
            return
        # setSource reset the rate; re-assert the fader so playback starts at the
        # chosen tempo (the fader deliberately stays put across track changes).
        self.player.set_playback_rate(self._tempo_rate())
        if play:
            self.transport.playing = True
            self._update_play_button(True)

        self._generation += 1
        self._pool.start(_PeakJob(track.path, self._generation, self._peak_signals))

    def _on_peaks(self, generation: int, result):
        if generation != self._generation:
            return                      # user clicked on to another track
        if result is None:
            self.view.set_message("Could not read audio for this track")
            # A track that will not decode is a real finding, not a transient UI
            # state: report it so it is marked wherever it appears, exactly like
            # a file the integrity check found missing.
            if self._track is not None:
                self.track_unreadable.emit(self._track.key)
            return
        duration = result.duration or float(self._track.length or 0.0)
        self.view.set_peaks(result, duration)

    # ----------------------------- transport -----------------------------
    def _apply(self, intents):
        for intent in intents:
            if intent.kind == SEEK and intent.pos is not None:
                self.player.seek(intent.pos)
            elif intent.kind == PLAY:
                self.player.play()
            elif intent.kind == PAUSE:
                self.player.pause()
        self._refresh_time()

    def toggle_play(self):
        if self._track is None:
            return
        self._apply(self.transport.play_pressed())

    def cue_pressed(self, num: int):
        pos = self._cues.get(num)
        if self._track is None or pos is None:
            return
        self._apply(self.transport.cue_pressed(num, int(pos * 1000)))

    def cue_released(self, num: int):
        if self._track is None or num not in self._cues:
            return
        self._apply(self.transport.cue_released(num))

    def _on_seek_fraction(self, fraction: float):
        duration = self.player.duration()
        if duration > 0:
            self.player.seek(int(fraction * duration))
            self._refresh_time()

    # ------------------------------ feedback -----------------------------
    def _sync_cue_buttons(self):
        th = theme_mod.current()
        grid = grid_cue_numbers(self._track)
        for i, btn in enumerate(self.cue_buttons, 1):
            present = i in self._cues
            btn.setEnabled(present)
            if present:
                colour = (theme_mod.CUE_GRID if i in grid else theme_mod.CUE_PLAIN)[th]
                fg = "#ffffff" if i not in grid or th == theme_mod.LIGHT else "#222222"
                btn.setStyleSheet(
                    f"background:{colour}; color:{fg}; font-weight:bold; border-radius:3px;")
            else:
                btn.setStyleSheet(f"background:{theme_mod.CUE_ABSENT[th]}; color:#888888;")

    def _update_play_button(self, playing: bool):
        self.play_btn.setText("⏸" if playing else "▶")

    def _on_playing(self, playing: bool):
        self._update_play_button(playing)

    def _on_duration(self, ms: int):
        if ms > 0:
            self.view.set_duration(ms / 1000.0)
        self._refresh_time()

    def _on_finished(self):
        self.transport.playback_finished()
        self._update_play_button(False)
        self.track_finished.emit()          # main_window may auto-advance

    # ----------------------------- on-end mode ---------------------------
    def on_end_mode(self) -> str:
        return self.on_end.currentData()

    def set_on_end_mode(self, key: str):
        i = self.on_end.findData(key)
        if i >= 0:
            self.on_end.setCurrentIndex(i)

    def _on_tick(self):
        if self.player.duration() <= 0:
            return
        self.view.set_position(self.player.position() / 1000.0)
        self._refresh_time()

    def _toggle_time_mode(self, _event):
        self._show_remaining = not self._show_remaining
        self._refresh_time()

    def _refresh_time(self):
        total = self.player.duration()
        if total <= 0 and self._track is not None:
            total = int((self._track.length or 0) * 1000)
        pos = self.player.position()
        shown = (total - pos) if self._show_remaining else pos
        self.time_label.setText(
            f"{format_time(shown, self._show_remaining)} / {format_time(total)}")

    # -------------------------------- tempo ------------------------------
    def tempo_percent(self) -> float:
        return self.tempo.value() / 10.0

    def _tempo_rate(self) -> float:
        return 1.0 + self.tempo_percent() / 100.0

    def _on_tempo(self, _value: int):
        pct = self.tempo_percent()
        self.player.set_playback_rate(self._tempo_rate())
        self.tempo.setToolTip(f"Tempo {pct:+.1f}%  (double-click to reset)")
        self._refresh_bpm()

    def _refresh_bpm(self):
        base = self._track.bpm if self._track is not None else None
        if not base:
            self.bpm_label.setText("-- BPM")
            self.bpm_label.setToolTip("This track has no BPM in its metadata")
            return
        adjusted = base * self._tempo_rate()
        self.bpm_label.setText(f"{adjusted:.1f} BPM")
        self.bpm_label.setToolTip(f"Base {base:.1f} BPM, adjusted by the tempo fader")

    # ------------------------------- theme -------------------------------
    def apply_theme(self):
        self._sync_cue_buttons()
        self.view.update()

    # --------------------------- state plumbing --------------------------
    def volume_value(self) -> int:
        return self.volume.value()

    def set_volume_value(self, value: int):
        self.volume.setValue(max(0, min(100, int(value))))

    def show_remaining(self) -> bool:
        return self._show_remaining

    def set_show_remaining(self, value: bool):
        self._show_remaining = bool(value)
        self._refresh_time()

    def tempo_value(self) -> int:
        return self.tempo.value()

    def set_tempo_value(self, value: int):
        self.tempo.setValue(max(TEMPO_MIN, min(TEMPO_MAX, int(value))))
