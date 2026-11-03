"""Vertical auto-scroll while dragging, shared by the sidebar trees and the
track tables.

Three things live here:

* :class:`DragAutoScroller` -- holding a dragged item over the top or bottom
  entry of a view scrolls that way, **accelerating** the longer it is held.
  Qt's own ``setAutoScroll`` is not used: it doesn't accelerate, and on the
  track table it is deliberately off because it re-centres the *horizontal*
  scrollbar on click (see ``track_table.py``). This is vertical only.

* the drag-target registry -- which view a drag is currently over.

* the mid-drag wheel. Windows' ``DoDragDrop`` loop never dispatches
  ``WM_MOUSEWHEEL``, so no ``QWheelEvent`` is produced while a drag is in
  flight and an application event filter has nothing to see. The messages are
  still sitting in the calling thread's queue though -- and that is the same
  thread running the drag loop -- so a timer (Qt timers *do* fire in there,
  which is why edge auto-scroll works) can peek them out directly.

Both the ramp and the geometry are pure functions so the behaviour can be
tested without a ``QDropEvent`` -- constructing one segfaults this PySide6
build.
"""

from __future__ import annotations

import atexit
import sys

from PySide6.QtCore import QDateTime, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QAbstractItemView


def _now_ms() -> float:
    return QDateTime.currentMSecsSinceEpoch()


# "The top/bottom entry" of the panel, in pixels. A little over one row at the
# default font, so the zone is reachable without being easy to enter by accident.
EDGE_MARGIN = 28
TICK_MS = 50
# Speed is expressed in ROWS per second, not pixels per tick: "two items a
# second" is what reads as slow to a person, and it stays honest across fonts
# and row heights.
BASE_ROWS_PER_SEC = 2.0
MAX_ROWS_PER_SEC = 15.0
RAMP_MS = 4000


def edge_zone(y: int, height: int, margin: int = EDGE_MARGIN) -> int:
    """``-1`` to scroll up, ``+1`` to scroll down, ``0`` to stay put.

    ``y`` is in viewport coordinates. A viewport too short to hold two margins
    splits the difference, so the two zones can never overlap and report both.
    """
    if height <= 0:
        return 0
    margin = min(margin, max(1, height // 2))
    if y < margin:
        return -1
    if y >= height - margin:
        return 1
    return 0


def rows_per_sec(held_ms: float) -> float:
    """Rows per second after holding at the edge for ``held_ms``.

    Linear from ``BASE_ROWS_PER_SEC`` to ``MAX_ROWS_PER_SEC`` over ``RAMP_MS``,
    then flat: a nudge barely moves, a deliberate hold builds up.
    """
    if held_ms <= 0:
        return BASE_ROWS_PER_SEC
    ramp = min(1.0, held_ms / float(RAMP_MS))
    return BASE_ROWS_PER_SEC + (MAX_ROWS_PER_SEC - BASE_ROWS_PER_SEC) * ramp


def rows_to_steps(view, rows: int) -> int:
    """Convert whole rows into this view's scrollbar units.

    Neither the trees nor the tables set ``verticalScrollMode`` explicitly, so
    both modes have to be handled rather than assumed.
    """
    if rows == 0:
        return 0
    if view.verticalScrollMode() == QAbstractItemView.ScrollPerItem:
        return rows
    height = view.sizeHintForRow(0)          # -1 on an empty view
    if height <= 0:
        height = max(1, view.verticalScrollBar().singleStep())
    return rows * height


class DragAutoScroller:
    """Drives one view's vertical scrollbar while a drag hovers its edges.

    ``update`` is called from ``dragMoveEvent`` and ``stop`` from
    ``dragLeaveEvent``/``dropEvent``; nothing else is needed at the call site.
    """

    def __init__(self, view, clock=None):
        self._view = view
        self._clock = clock or _now_ms      # injectable: tests advance it by hand
        self._timer = QTimer(view)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._direction = 0
        self._entered_at = 0.0
        # Fractional rows owed. At the base rate a tick is worth 0.1 of a row, so
        # without this the smallest possible move (one row per tick) would be
        # 20 rows/second -- ten times too fast.
        self._carry = 0.0
        self._last_tick = 0.0

    # -------------------- called from the drag handlers --------------------
    def update(self, pos) -> None:
        """``pos`` is a QPoint in viewport coordinates."""
        viewport = self._view.viewport()
        direction = edge_zone(pos.y(), viewport.height())
        if direction == 0:
            self.stop()
            return
        if direction != self._direction:
            # Entering an edge (or crossing to the other one) restarts the ramp,
            # so a fresh hover always begins slow rather than at the speed the
            # previous hold had built up to.
            self._direction = direction
            self._entered_at = self._last_tick = self._clock()
            self._carry = 0.0
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self._direction = 0
        self._entered_at = 0.0
        self._carry = 0.0

    # -------------------- internals --------------------
    def _tick(self) -> None:
        if self._direction == 0:
            self._timer.stop()
            return
        now = self._clock()
        # Measured, not assumed: a tick delayed by a busy main thread should
        # still be worth its real elapsed time rather than a nominal TICK_MS.
        elapsed = max(0.0, now - self._last_tick) or TICK_MS
        self._last_tick = now
        self._carry += rows_per_sec(now - self._entered_at) * (elapsed / 1000.0)
        rows = int(self._carry)
        if rows <= 0:
            return
        self._carry -= rows
        bar = self._view.verticalScrollBar()
        target = bar.value() + self._direction * rows_to_steps(self._view, rows)
        bar.setValue(max(bar.minimum(), min(bar.maximum(), target)))


# ====================== which view is under the drag ======================
# Set on every dragMoveEvent and cleared on leave/drop, so it is true for any
# drag the app can see -- including one that started in Explorer.
_target = None
_wheel_timer = None


def note_drag_over(view) -> None:
    global _target
    _target = view
    _install_hook()                  # idempotent: this runs on every drag move
    _wheel_pump().start()


def clear_drag_over(view) -> None:
    """Only clears if ``view`` is still the target: a leave event for the view
    the cursor just left can arrive *after* the enter for the one it moved to."""
    global _target
    if _target is view:
        _target = None
        _end_wheel_capture()


def _end_wheel_capture() -> None:
    """Stop the pump and take the hook down. Every path that ends a drag routes
    through here, so the system-wide hook can't outlive the drag."""
    if _wheel_timer is not None:
        _wheel_timer.stop()
    _remove_hook()
    _drain_wheel_delta()             # drop anything unapplied, don't leak it


_VK_LBUTTON = 0x01


def _buttons_held() -> bool:
    """Is a mouse button physically down?

    ``QGuiApplication.mouseButtons()`` cannot answer this mid-drag: ``DoDragDrop``
    owns the mouse and Qt never receives the events, so it reports NoButton and
    :func:`drag_target` concludes the drag ended -- clearing the target and
    stopping the wheel pump on the very first tick, which left the wheel working
    only in the sliver between a mouse move and the next tick.
    ``GetAsyncKeyState`` reads the hardware state and is unaffected by a message
    queue nobody is pumping.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            # Mask to 16 bits first: ctypes' default int restype sign-extends
            # the SHORT this returns, so a raw `& 0x8000` is unreliable.
            state = ctypes.windll.user32.GetAsyncKeyState(_VK_LBUTTON) & 0xFFFF
            return bool(state & 0x8000)
        except (OSError, AttributeError):
            pass
    return bool(QGuiApplication.mouseButtons())


def drag_target():
    """The view under the drag, or None.

    A cancelled drag doesn't always deliver a leave to the view it was over, so
    a stale target is possible; no mouse button held means the drag is long over,
    and leaving it latched would hijack ordinary wheel scrolling for good.
    """
    global _target
    if _target is not None and not _buttons_held():
        _target = None
        _end_wheel_capture()
    return _target


def wheel_scroll(view, angle_delta_y: int) -> bool:
    """Scroll ``view`` by one wheel notch. Returns whether anything happened, so
    the caller knows whether to consume the event."""
    if view is None or not angle_delta_y:
        return False
    bar = view.verticalScrollBar()
    if bar is None or bar.maximum() <= bar.minimum():
        return False
    notches = angle_delta_y / 120.0
    step = rows_to_steps(view, 3) or max(1, bar.singleStep()) * 3
    target = bar.value() - int(round(notches * step))
    bar.setValue(max(bar.minimum(), min(bar.maximum(), target)))
    return True


# ======================== the mid-drag wheel pump =========================
# Why a low-level hook and not a message peek:
#
# WM_MOUSEWHEEL (0x020A) sits inside the WM_MOUSEFIRST(0x0200)-WM_MOUSELAST
# (0x020E) range that DoDragDrop -- the modal loop Qt enters for QDrag.exec() --
# peeks with PM_REMOVE to track the drag. Peeking for it ourselves is a race
# against a loop spinning far tighter than any timer, and we lose it nearly
# every time: the wheel appeared to work only when spammed.
#
# A WH_MOUSE_LL hook is called BEFORE the message reaches any thread queue, so
# there is nothing to race. Low-level hooks are invoked on the installing thread
# as it pumps messages, and DoDragDrop does pump -- which is why the timer-driven
# edge auto-scroll already worked through a drag.
_WM_MOUSEWHEEL = 0x020A
_WH_MOUSE_LL = 14
_HC_ACTION = 0

_pending_delta = 0        # filled by the hook, drained by the pump
_hook_handle = None
_hook_proc = None         # MUST stay referenced -- see _install_hook


def _drain_wheel_delta() -> int:
    """Read and clear the delta the hook has accumulated (120 per notch,
    positive = away from the user). Clearing is what stops a notch being applied
    twice if two pump ticks land close together."""
    global _pending_delta
    total, _pending_delta = _pending_delta, 0
    return total


def _install_hook() -> None:
    """Install the low-level mouse hook for the duration of a drag. Idempotent:
    ``note_drag_over`` calls this on every dragMoveEvent.

    No-op off Windows, and a silent no-op if the install fails -- the same
    degrade-gracefully posture as ``io/recycle.py``.
    """
    global _hook_handle, _hook_proc
    if _hook_handle is not None or sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("pt", wintypes.POINT),
                    ("mouseData", wintypes.DWORD),
                    ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p)]

    # Every signature is declared. ctypes otherwise defaults both parameters and
    # returns to C int, which on 64-bit means (a) passing lParam -- a pointer --
    # raises OverflowError on EVERY non-wheel mouse event, and (b) the HHOOK from
    # SetWindowsHookExW comes back truncated, so the handle handed to
    # UnhookWindowsHookEx is not the one Windows issued.
    LRESULT = ctypes.c_ssize_t
    proto = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int,
                               wintypes.WPARAM, wintypes.LPARAM)
    user32 = ctypes.windll.user32
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, proto,
                                         wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = LRESULT

    def _on_ll_mouse(n_code, w_param, l_param):
        # Deliberately minimal. Windows silently drops a hook whose callback
        # exceeds LowLevelHooksTimeout (~300ms), and this runs for EVERY system
        # mouse event -- including every move during the drag. So: accumulate
        # only; the scrolling happens on the pump timer.
        if n_code == _HC_ACTION and w_param == _WM_MOUSEWHEEL:
            info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            _add_wheel_delta(_signed_hiword(info.mouseData))
            return 1                    # consume: nothing else should see it
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    try:
        # _hook_proc holds the only reference to the C-callable thunk. Without
        # it Python garbage-collects the thunk while Windows still holds the
        # pointer, and the next mouse event calls freed memory.
        _hook_proc = proto(_on_ll_mouse)
        handle = user32.SetWindowsHookExW(_WH_MOUSE_LL, _hook_proc, None, 0)
    except (OSError, AttributeError, ValueError):
        _hook_proc = None
        return
    if handle:
        _hook_handle = handle
    else:
        _hook_proc = None               # install failed; drop the thunk too


def _remove_hook() -> bool:
    """Uninstall the hook. Must run on every path that ends a drag -- a leaked
    system-wide hook would keep intercepting the wheel for the whole session.

    Returns whether Windows actually unhooked, so a caller (and the tests) can
    tell a real uninstall from merely clearing our own variable.
    """
    global _hook_handle, _hook_proc
    handle, _hook_handle, _hook_proc = _hook_handle, None, None
    if handle is None or sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        return bool(user32.UnhookWindowsHookEx(handle))
    except (OSError, AttributeError, ValueError):
        return False


def _signed_hiword(value: int) -> int:
    """The wheel delta is the *signed* high word of ``mouseData``. Reading it
    unsigned silently breaks one scroll direction only, which is easy to miss."""
    hi = (int(value) >> 16) & 0xFFFF
    return hi - 0x10000 if hi & 0x8000 else hi


def _add_wheel_delta(delta: int) -> None:
    # Same thread as the pump (a low-level hook is called on the thread that
    # installed it), so no locking is needed.
    global _pending_delta
    _pending_delta += delta


atexit.register(_remove_hook)


def _wheel_pump() -> QTimer:
    """The timer that drains the wheel while a drag is in flight -- one per
    process, deliberately **unparented**.

    Parenting it to a view would tie its C++ lifetime to that widget: close the
    tab you last dragged over and the timer is destroyed underneath this
    module-level reference, so the next drag raises "Internal C++ object already
    deleted". With no parent, Python's reference here is the only owner.
    """
    global _wheel_timer
    if _wheel_timer is None:
        _wheel_timer = QTimer()
        _wheel_timer.setInterval(TICK_MS)
        _wheel_timer.timeout.connect(_pump_wheel_once)
    return _wheel_timer


def _pump_wheel_once() -> None:
    target = drag_target()          # also tears everything down if the drag ended
    if target is None:
        _end_wheel_capture()
        return
    delta = _drain_wheel_delta()
    if delta:
        wheel_scroll(target, delta)
