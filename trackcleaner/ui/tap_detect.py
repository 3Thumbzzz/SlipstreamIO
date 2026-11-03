"""Double-tap detection for the arrow-key playlist navigation.

Kept apart from the widgets, like :mod:`trackcleaner.ui.drag_scroll`, because the
timing rule is the whole feature and synthesising two Qt key events milliseconds
apart is a poor way to check it. Here it is a function of (key, timestamp).

The rule: two presses of the **same** key within :data:`DOUBLE_TAP_MS` are a
double tap. A different key in between resets, so alternating left/right quickly
never registers as one -- that would make stepping back and forth expand folders
at random.
"""

from __future__ import annotations

import time

#: Two presses closer together than this are one gesture. 200 ms is the value in
#: the request; it is comfortably above a deliberate double-tap and below the
#: rate a held key repeats at.
DOUBLE_TAP_MS = 200


class DoubleTapDetector:
    """Answers "was that a double tap?" for a stream of key presses."""

    def __init__(self, window_ms: int = DOUBLE_TAP_MS):
        self.window_ms = window_ms
        self._last_key = None
        self._last_ms = 0.0

    def tap(self, key, now_ms: float | None = None) -> bool:
        """Record a press of ``key``; True when it completes a double tap.

        ``now_ms`` is injectable so a test can state the timing outright rather
        than sleeping. A completed double tap **consumes** the history, so three
        fast presses read as one double tap and then a fresh single -- not two
        overlapping doubles.
        """
        now = time.monotonic() * 1000.0 if now_ms is None else now_ms
        doubled = (key == self._last_key
                   and (now - self._last_ms) <= self.window_ms)
        if doubled:
            self._last_key, self._last_ms = None, 0.0
        else:
            self._last_key, self._last_ms = key, now
        return doubled

    def reset(self) -> None:
        """Forget any part-formed gesture -- used when focus moves away, so a
        tap before you left and one after you came back are not a pair."""
        self._last_key, self._last_ms = None, 0.0
