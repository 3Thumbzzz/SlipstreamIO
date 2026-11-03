"""Play / hotcue transport rules, deliberately free of Qt and of audio.

The hold-to-preview behaviour is the subtlest part of the waveform panel:

    press a hotcue while playing  -> jump there and keep playing
    press a hotcue while stopped  -> jump there and play *only while held*,
                                     then snap back to the cue on release

Keeping the rules in a plain state machine that returns *intents* -- rather than
calling a player -- means every branch, including the awkward overlapping-hold
cases, is unit-testable without an audio device.

The widget owns the mapping from a cue number to a position and simply feeds
this class positions in milliseconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# intent kinds
SEEK = "seek"
PLAY = "play"
PAUSE = "pause"


@dataclass(frozen=True)
class Intent:
    """One thing the player should do. ``pos`` is milliseconds, SEEK only."""

    kind: str
    pos: Optional[int] = None


@dataclass
class Transport:
    """Playback intent state. ``playing`` mirrors what the player is doing."""

    playing: bool = False
    # The cue currently holding a preview open, and the position to snap back
    # to. Both None when no preview is armed.
    _preview_cue: Optional[int] = None
    _preview_pos: Optional[int] = None
    # Cues physically held down, so an overlapping release cannot end a preview
    # it did not start.
    _held: set = field(default_factory=set)

    # ------------------------------ queries ------------------------------
    @property
    def previewing(self) -> bool:
        return self._preview_cue is not None

    # ------------------------------ actions ------------------------------
    def cue_pressed(self, cue: int, pos: int) -> list[Intent]:
        """Jump to ``pos``. Continue playing if we already were; otherwise play
        only for as long as the cue is held."""
        self._held.add(cue)
        if self.playing:
            # Already playing (including during someone else's preview): this
            # becomes plain playback from the new point, and no longer snaps
            # back -- but a preview stays owned by its cue until released.
            if self.previewing:
                # Hand the preview to the newest cue, so releasing *it* is what
                # ends the preview and returns here.
                self._preview_cue = cue
                self._preview_pos = pos
            return [Intent(SEEK, pos)]
        self._preview_cue = cue
        self._preview_pos = pos
        self.playing = True
        return [Intent(SEEK, pos), Intent(PLAY)]

    def cue_released(self, cue: int) -> list[Intent]:
        """End the preview only if this cue is the one that armed it."""
        self._held.discard(cue)
        if cue != self._preview_cue:
            return []                       # not ours -- someone else's hold
        pos = self._preview_pos
        self._clear_preview()
        self.playing = False
        return [Intent(PAUSE), Intent(SEEK, pos)]

    def play_pressed(self) -> list[Intent]:
        """Play/pause toggle -- the play button and the W key.

        Cancels any armed preview: once you have explicitly asked for playback,
        letting go of the cue must not stop it.
        """
        self._clear_preview()
        if self.playing:
            self.playing = False
            return [Intent(PAUSE)]
        self.playing = True
        return [Intent(PLAY)]

    def track_changed(self) -> list[Intent]:
        """Loading a different track drops all state."""
        self._clear_preview()
        self._held.clear()
        self.playing = False
        return [Intent(PAUSE)]

    def playback_finished(self) -> None:
        """The player reached the end on its own."""
        self._clear_preview()
        self.playing = False

    # ------------------------------ internal -----------------------------
    def _clear_preview(self) -> None:
        self._preview_cue = None
        self._preview_pos = None
