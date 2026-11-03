"""Drag auto-scroll geometry, the acceleration ramp, and the drag-target
registry the mid-drag wheel uses.

Everything is driven through the pure functions and by calling
``DragAutoScroller`` directly -- no synthesised ``QDropEvent`` anywhere, since
constructing one segfaults this PySide6 build (HANDOFF.md, "Development
conventions"). The wiring that
calls these from ``dragMoveEvent`` is three lines per view.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QListWidget                # noqa: E402

from trackcleaner.ui import drag_scroll                                # noqa: E402
from trackcleaner.ui.drag_scroll import (BASE_ROWS_PER_SEC,            # noqa: E402
                                         EDGE_MARGIN, MAX_ROWS_PER_SEC,
                                         RAMP_MS, TICK_MS,
                                         DragAutoScroller, edge_zone,
                                         rows_per_sec, rows_to_steps)

_WM_MOUSEMOVE = 0x0200


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def clean_registry():
    """Module state; never let one test leak into the next -- and never leave a
    system-wide mouse hook installed after the suite."""
    yield
    drag_scroll._target = None
    if drag_scroll._wheel_timer is not None:
        drag_scroll._wheel_timer.stop()
    drag_scroll._remove_hook()
    drag_scroll._drain_wheel_delta()


# ================================ edge_zone =================================
def test_edge_zone_top_middle_bottom():
    h = 400
    assert edge_zone(0, h) == -1
    assert edge_zone(EDGE_MARGIN - 1, h) == -1
    assert edge_zone(h // 2, h) == 0
    assert edge_zone(h - 1, h) == 1
    assert edge_zone(h - EDGE_MARGIN, h) == 1


def test_edge_zone_just_inside_the_margins_is_neutral():
    h = 400
    assert edge_zone(EDGE_MARGIN, h) == 0
    assert edge_zone(h - EDGE_MARGIN - 1, h) == 0


def test_edge_zone_in_a_short_viewport_never_reports_both():
    """A panel shorter than two margins would otherwise have overlapping zones,
    where a single point is simultaneously 'top' and 'bottom'."""
    for h in range(1, 2 * EDGE_MARGIN + 2):
        zones = {edge_zone(y, h) for y in range(h)}
        assert zones <= {-1, 0, 1}
        for y in range(h):
            top = y < min(EDGE_MARGIN, max(1, h // 2))
            assert not (top and edge_zone(y, h) == 1)


def test_edge_zone_of_an_unrealised_viewport_is_neutral():
    assert edge_zone(0, 0) == 0
    assert edge_zone(10, -5) == 0


# =============================== rows_per_sec ===============================
def test_rows_per_sec_starts_at_two_items_a_second():
    assert rows_per_sec(0) == BASE_ROWS_PER_SEC == 2.0
    assert rows_per_sec(-10) == BASE_ROWS_PER_SEC


def test_rows_per_sec_accelerates_and_then_clamps():
    """The longer the item is held there, the faster it goes -- to a ceiling."""
    speeds = [rows_per_sec(t) for t in range(0, RAMP_MS * 2, 50)]
    assert speeds == sorted(speeds), "must never slow down while held"
    assert speeds[0] == BASE_ROWS_PER_SEC
    assert rows_per_sec(RAMP_MS) == MAX_ROWS_PER_SEC
    assert rows_per_sec(RAMP_MS * 10) == MAX_ROWS_PER_SEC
    assert max(speeds) == MAX_ROWS_PER_SEC, "and never overshoot the ceiling"


def test_rows_per_sec_is_meaningfully_faster_midway():
    assert rows_per_sec(RAMP_MS // 2) > 2 * BASE_ROWS_PER_SEC


# =============================== rows_to_steps ==============================
def test_rows_to_steps_per_item_is_one_unit_per_row(view):
    from PySide6.QtWidgets import QAbstractItemView
    view.setVerticalScrollMode(QAbstractItemView.ScrollPerItem)
    assert rows_to_steps(view, 4) == 4
    assert rows_to_steps(view, 0) == 0


def test_rows_to_steps_per_pixel_uses_the_row_height(view):
    from PySide6.QtWidgets import QAbstractItemView
    view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    height = view.sizeHintForRow(0)
    assert height > 0
    assert rows_to_steps(view, 4) == 4 * height


def test_rows_to_steps_survives_an_empty_view(app):
    """sizeHintForRow returns -1 with no rows; a zero or negative step would
    silently freeze the auto-scroll."""
    from PySide6.QtWidgets import QAbstractItemView, QListWidget
    empty = QListWidget()
    empty.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    assert rows_to_steps(empty, 3) > 0


# ============================ DragAutoScroller ==============================
class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def view(app):
    """A real scrollable view with far more rows than fit."""
    w = QListWidget()
    w.addItems([f"row {i}" for i in range(500)])
    w.resize(200, 300)
    w.show()
    return w


def _scroller(view):
    clock = _Clock()
    return DragAutoScroller(view, clock=clock), clock


class _Pt:
    def __init__(self, y):
        self._y = y

    def y(self):
        return self._y


def _run(scroller, clock, ms):
    """Drive ``ms`` worth of ticks at the real tick interval."""
    for _ in range(int(ms // TICK_MS)):
        clock.t += TICK_MS
        scroller._tick()


def test_scroller_moves_down_at_the_bottom_edge(view):
    scroller, clock = _scroller(view)
    view.verticalScrollBar().setValue(0)

    scroller.update(_Pt(view.viewport().height() - 1))
    _run(scroller, clock, 1000)

    assert view.verticalScrollBar().value() > 0


def test_scroller_moves_up_at_the_top_edge(view):
    scroller, clock = _scroller(view)
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    before = bar.value()

    scroller.update(_Pt(0))
    _run(scroller, clock, 1000)

    assert bar.value() < before


def test_a_fresh_hover_crawls_at_two_rows_a_second(view):
    """The reported bug: it took off far too fast. A single tick must not move
    anything, and a second at the base rate must be worth ~2 rows."""
    scroller, clock = _scroller(view)
    bar = view.verticalScrollBar()
    bar.setValue(0)
    scroller.update(_Pt(view.viewport().height() - 1))

    clock.t += TICK_MS
    scroller._tick()
    assert bar.value() == 0, "one tick is a fraction of a row, not a whole one"

    _run(scroller, clock, 1000 - TICK_MS)
    moved = bar.value() - 0
    assert 1 <= moved <= 3, f"~2 rows in the first second, got {moved}"


def test_scroller_ticks_further_the_longer_it_is_held(view):
    scroller, clock = _scroller(view)
    bar = view.verticalScrollBar()
    bar.setValue(0)
    scroller.update(_Pt(view.viewport().height() - 1))

    _run(scroller, clock, 1000)
    early = bar.value()
    clock.t += RAMP_MS                       # held at the edge past the full ramp
    scroller._last_tick = clock.t
    before_late = bar.value()
    _run(scroller, clock, 1000)
    late = bar.value() - before_late

    assert late > early, "a long hold covers more ground per second"
    assert late >= MAX_ROWS_PER_SEC - 2


def test_scroller_stops_in_the_middle(view):
    scroller, clock = _scroller(view)
    scroller.update(_Pt(view.viewport().height() - 1))
    assert scroller._timer.isActive()

    scroller.update(_Pt(view.viewport().height() // 2))

    assert not scroller._timer.isActive()
    before = view.verticalScrollBar().value()
    _run(scroller, clock, 2000)
    assert view.verticalScrollBar().value() == before


def test_re_entering_the_edge_starts_slow_again(view):
    """Leaving and re-entering must reset the ramp, or a second hover would
    begin at whatever speed the first had built up to."""
    scroller, clock = _scroller(view)
    bottom = view.viewport().height() - 1
    scroller.update(_Pt(bottom))
    _run(scroller, clock, RAMP_MS * 2)       # ramped to full speed
    scroller.stop()

    bar = view.verticalScrollBar()
    bar.setValue(0)
    scroller.update(_Pt(bottom))             # fresh hover
    _run(scroller, clock, 1000)

    assert 1 <= bar.value() <= 3, "back to the crawl, not the speed it reached"


def test_crossing_to_the_other_edge_resets_the_ramp(view):
    scroller, clock = _scroller(view)
    scroller.update(_Pt(view.viewport().height() - 1))
    _run(scroller, clock, RAMP_MS * 2)
    scroller.update(_Pt(0))                  # straight to the top edge

    bar = view.verticalScrollBar()
    bar.setValue(200)
    _run(scroller, clock, 1000)

    assert 197 <= bar.value() <= 199


def test_scroller_clamps_at_the_ends(view):
    scroller, clock = _scroller(view)
    bar = view.verticalScrollBar()
    bar.setValue(0)
    scroller.update(_Pt(0))                  # up, already at the top
    _run(scroller, clock, 3000)
    assert bar.value() == bar.minimum()

    scroller.update(_Pt(view.viewport().height() - 1))
    _run(scroller, clock, 120_000)
    assert bar.value() == bar.maximum()


def test_stop_is_safe_when_never_started(view):
    scroller, _clock = _scroller(view)
    scroller.stop()
    scroller._tick()                          # must not raise


# ========================== drag-target registry ============================
@pytest.fixture
def dragging(monkeypatch):
    """Pretend a mouse button is held, i.e. a drag really is in flight."""
    monkeypatch.setattr(drag_scroll, "_buttons_held", lambda: True)


def test_registry_tracks_the_view_under_the_drag(view, dragging):
    assert drag_scroll.drag_target() is None
    drag_scroll.note_drag_over(view)
    assert drag_scroll.drag_target() is view
    drag_scroll.clear_drag_over(view)
    assert drag_scroll.drag_target() is None


def test_a_late_leave_does_not_clear_the_new_target(app, dragging):
    """Qt can deliver the leave for the view you just left *after* the enter for
    the one you moved to; clearing blindly would drop the live target."""
    a, b = QListWidget(), QListWidget()
    drag_scroll.note_drag_over(a)
    drag_scroll.note_drag_over(b)          # moved on to b
    drag_scroll.clear_drag_over(a)         # a's leave arrives late

    assert drag_scroll.drag_target() is b


def test_a_target_left_behind_by_a_cancelled_drag_is_dropped(view):
    """Escape or a drop outside the app can skip the leave event. Without this
    the stale target would swallow ordinary wheel scrolling for good."""
    drag_scroll.note_drag_over(view)
    assert drag_scroll._target is view          # registered...

    assert drag_scroll.drag_target() is None    # ...but no button is held
    assert drag_scroll._target is None, "and it is cleared, not just hidden"


def test_wheel_scroll_moves_the_target(view):
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    before = bar.value()

    assert drag_scroll.wheel_scroll(view, -120) is True
    assert bar.value() > before
    assert drag_scroll.wheel_scroll(view, 120) is True
    assert bar.value() == before


def test_wheel_scroll_is_a_no_op_without_a_target_or_a_delta(view):
    assert drag_scroll.wheel_scroll(None, -120) is False
    assert drag_scroll.wheel_scroll(view, 0) is False


def test_wheel_scroll_is_a_no_op_when_nothing_can_scroll(app):
    tiny = QListWidget()
    tiny.addItem("only row")
    tiny.resize(200, 300)
    assert drag_scroll.wheel_scroll(tiny, -120) is False


# ============================ mid-drag wheel pump ===========================
# WM_MOUSEWHEEL (0x020A) is inside the WM_MOUSEFIRST..WM_MOUSELAST range that
# DoDragDrop peeks with PM_REMOVE, so peeking for it ourselves was a race we
# lost: the wheel only worked when spammed. A WH_MOUSE_LL hook sees it before it
# reaches any queue. The hook can only be fired by real hardware input, so what
# is pinned here is everything around it -- the accumulator, the signed delta,
# and the install/remove lifecycle.
def test_signed_hiword_reads_both_directions():
    """The delta is the SIGNED high word. Read unsigned, one scroll direction
    silently breaks while the other looks fine -- easy to miss by hand."""
    assert drag_scroll._signed_hiword(120 << 16) == 120
    assert drag_scroll._signed_hiword((-120 & 0xFFFF) << 16) == -120
    assert drag_scroll._signed_hiword(0) == 0


def test_the_accumulator_sums_and_clears():
    drag_scroll._add_wheel_delta(120)
    drag_scroll._add_wheel_delta(-360)

    assert drag_scroll._drain_wheel_delta() == -240
    assert drag_scroll._drain_wheel_delta() == 0, "a notch must not apply twice"


def test_the_wheel_pump_runs_only_while_a_drag_is_over_a_view(view, dragging):
    drag_scroll.note_drag_over(view)
    assert drag_scroll._wheel_timer.isActive()

    drag_scroll.clear_drag_over(view)
    assert not drag_scroll._wheel_timer.isActive()


def test_the_pump_scrolls_the_target_by_the_accumulated_delta(view, dragging):
    bar = view.verticalScrollBar()
    bar.setValue(0)
    drag_scroll.note_drag_over(view)
    drag_scroll._add_wheel_delta(-120)        # what the hook would have recorded

    drag_scroll._pump_wheel_once()

    assert bar.value() > 0
    assert drag_scroll._drain_wheel_delta() == 0, "consumed, not left pending"


def test_the_pump_stops_itself_once_the_drag_is_over(view):
    """No button held means the drag ended without a leave event."""
    drag_scroll.note_drag_over(view)
    bar = view.verticalScrollBar()
    bar.setValue(0)
    drag_scroll._add_wheel_delta(-120)

    drag_scroll._pump_wheel_once()           # _buttons_held() is false here

    assert bar.value() == 0
    assert not drag_scroll._wheel_timer.isActive()


# ------------------------------ hook lifecycle ------------------------------
def test_the_hook_installs_idempotently_and_comes_down(view, dragging):
    """A leaked system-wide hook would keep swallowing the wheel for the rest of
    the session, so the handle is asserted directly."""
    drag_scroll.note_drag_over(view)
    first = drag_scroll._hook_handle
    drag_scroll.note_drag_over(view)          # every drag move calls this

    assert drag_scroll._hook_handle == first, "must not stack hooks"

    drag_scroll.clear_drag_over(view)
    assert drag_scroll._hook_handle is None
    assert drag_scroll._hook_proc is None, "the callback thunk is released too"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only hook")
def test_windows_really_unhooks_not_just_our_variable():
    """Regression: with no restype declared, SetWindowsHookExW's HHOOK came back
    truncated to 32 bits, so UnhookWindowsHookEx was handed a handle Windows
    never issued. Asserting our own _hook_handle went None could never have
    caught that -- only the API's own answer can."""
    drag_scroll._install_hook()
    assert drag_scroll._hook_handle is not None

    assert drag_scroll._remove_hook() is True, "Windows must report a real unhook"
    assert drag_scroll._remove_hook() is False, "and nothing left to remove"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only hook")
def test_the_callback_passes_non_wheel_events_through_without_raising():
    """The console-spam bug: lParam is a 64-bit pointer, and with no argtypes
    declared ctypes tried to squeeze it into a C int -- raising OverflowError on
    EVERY mouse move while the hook was up. Only the wheel branch was covered,
    so nothing caught it."""
    import ctypes
    drag_scroll._install_hook()
    proc = drag_scroll._hook_proc
    assert proc is not None

    buf = ctypes.create_string_buffer(64)     # a realistic 64-bit lParam
    l_param = ctypes.cast(buf, ctypes.c_void_p).value
    before = drag_scroll._drain_wheel_delta()

    result = proc(drag_scroll._HC_ACTION, _WM_MOUSEMOVE, l_param)

    assert isinstance(result, int), "must return through the hook chain cleanly"
    assert drag_scroll._drain_wheel_delta() == before, "a move is not a scroll"
    drag_scroll._remove_hook()


def test_a_drag_that_ends_without_a_leave_still_unhooks(view):
    """Escape or a drop outside the app can skip the leave event; the stale-target
    guard has to take the hook down as well as stop the pump."""
    drag_scroll.note_drag_over(view)
    assert drag_scroll._hook_handle is not None or sys.platform != "win32"

    assert drag_scroll.drag_target() is None   # no button held -> drag is over

    assert drag_scroll._hook_handle is None


def test_ending_capture_drops_an_unapplied_delta(view):
    """Otherwise a notch rolled just as the drag ended would fire into whatever
    the next drag happened to be over."""
    drag_scroll.note_drag_over(view)
    drag_scroll._add_wheel_delta(-999)

    drag_scroll.clear_drag_over(view)

    assert drag_scroll._drain_wheel_delta() == 0


def test_removing_a_hook_that_was_never_installed_is_safe():
    drag_scroll._remove_hook()
    drag_scroll._remove_hook()


def test_the_pump_survives_the_view_it_first_ran_over(app, dragging):
    """Regression: parenting the shared timer to a view tied its C++ lifetime to
    that widget, so closing the tab you last dragged over made the next drag
    raise 'Internal C++ object already deleted'."""
    doomed = QListWidget()
    doomed.addItems([str(i) for i in range(50)])
    drag_scroll.note_drag_over(doomed)
    drag_scroll.clear_drag_over(doomed)

    doomed.deleteLater()
    doomed.setParent(None)
    del doomed
    app.processEvents()                       # the C++ object really goes away

    survivor = QListWidget()
    survivor.addItems([str(i) for i in range(50)])
    drag_scroll.note_drag_over(survivor)      # must not raise
    assert drag_scroll._wheel_timer.isActive()
    drag_scroll.clear_drag_over(survivor)


def test_draining_the_wheel_never_raises(app):
    """Must always answer with an int rather than break the drag."""
    assert isinstance(drag_scroll._drain_wheel_delta(), int)


def test_buttons_held_never_raises(app):
    """Also Windows-only ctypes. The real mid-drag answer can only come from an
    actual drag; what matters here is that it always answers."""
    assert isinstance(drag_scroll._buttons_held(), bool)


def test_the_pump_survives_ticks_with_no_mouse_movement(view, dragging, monkeypatch):
    """The reported bug: drag_target() asked Qt whether a button was held, but
    Windows withholds that during a drag, so the very first tick decided the drag
    had ended and stopped the pump. Only the next dragMoveEvent revived it -- so
    the wheel worked solely in the gap between a mouse move and the next tick.

    A still mouse means note_drag_over() is NOT called again; the pump has to
    keep working on its own.
    """
    monkeypatch.setattr(drag_scroll, "_drain_wheel_delta", lambda: -120)
    bar = view.verticalScrollBar()
    bar.setValue(0)
    drag_scroll.note_drag_over(view)

    seen = []
    for _ in range(5):
        drag_scroll._pump_wheel_once()        # no dragMoveEvent in between
        seen.append(bar.value())

    assert drag_scroll.drag_target() is view, "target must survive its own ticks"
    assert drag_scroll._wheel_timer.isActive()
    assert seen == sorted(seen) and seen[-1] > 0
    assert len(set(seen)) == len(seen), "every tick scrolled, not just the first"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
