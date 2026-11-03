"""Capturing a binding: Tab, mouse buttons, and standing the app's shortcuts down.

`QKeySequenceEdit` could do none of these. **Tab never reached it** — Qt's focus
handling turns Tab into a focus change inside `QWidget.event`, before any
subclass sees it — and `QKeySequence` has no representation for a mouse button at
all, so one could not be recorded even in principle.

The key tests deliver events with `QTest`, following `test_key_routing.py`: a
direct `keyPressEvent` call bypasses precisely the machinery that was eating Tab,
so it would pass against the broken widget.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt                          # noqa: E402
from PySide6.QtGui import QMouseEvent                                   # noqa: E402
from PySide6.QtTest import QTest                                        # noqa: E402
from PySide6.QtWidgets import QApplication                              # noqa: E402

from trackcleaner.config import controls as controls_mod                # noqa: E402
from trackcleaner.ui.controls_dialog import (ShortcutEdit,              # noqa: E402
                                             mouse_event_binding)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def edit(app):
    w = ShortcutEdit()
    w.show()
    yield w
    w.close()


def _press(widget, button, modifiers=Qt.NoModifier):
    event = QMouseEvent(QEvent.MouseButtonPress, QPointF(5, 5), QPointF(5, 5),
                        button, button, modifiers)
    widget.mousePressEvent(event)
    return event


# ============================== keys ========================================
def test_tab_is_recorded_rather_than_moving_focus(edit):
    """The reported bug. Driven through QTest because a direct keyPressEvent
    call skips the focus handling that was consuming it."""
    QTest.keyClick(edit, Qt.Key_Tab)

    assert edit.binding() == "Tab"


def test_shift_tab_is_recorded_under_the_name_people_use(edit):
    """Qt calls this chord "Backtab" and spells the sequence that way, so the
    field showed "Shift+Backtab" for something every user calls Shift+Tab."""
    from PySide6.QtGui import QKeySequence
    QTest.keyClick(edit, Qt.Key_Backtab, Qt.ShiftModifier)

    assert edit.binding() == "Shift+Tab"
    assert not QKeySequence(edit.binding()).isEmpty()


def test_a_shift_tab_binding_actually_fires(app):
    """The renaming is only safe because Qt canonicalises the two when matching
    -- it treats the sequences themselves as unequal. A binding that reads nicely
    and never fires would be worse than an ugly one that works, so this drives a
    real QShortcut with a real press."""
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtWidgets import QWidget
    assert QKeySequence("Shift+Tab") != QKeySequence("Shift+Backtab")

    host = QWidget()
    host.show()
    host.activateWindow()
    QTest.qWaitForWindowActive(host, 200)
    fired = []
    shortcut = QShortcut(QKeySequence("Shift+Tab"), host)
    shortcut.setContext(Qt.ApplicationShortcut)
    shortcut.activated.connect(lambda: fired.append(True))

    QTest.keyClick(host, Qt.Key_Backtab, Qt.ShiftModifier)   # what Qt delivers
    app.processEvents()

    if not host.isActiveWindow():
        pytest.skip("offscreen platform will not activate a window")
    assert fired == [True]
    host.close()


def test_a_chord_keeps_qts_spelling(edit):
    """So a binding written here is one QKeySequence can read back."""
    QTest.keyClick(edit, Qt.Key_E, Qt.ControlModifier | Qt.ShiftModifier)

    assert edit.binding() == "Ctrl+Shift+E"


def test_a_function_key_is_recorded(edit):
    QTest.keyClick(edit, Qt.Key_F5)

    assert edit.binding() == "F5"


def test_a_bare_modifier_is_not_a_binding(edit):
    """Half a chord. Recording it would assign Ctrl the moment you reached for
    Ctrl+something."""
    QTest.keyClick(edit, Qt.Key_F5)

    QTest.keyClick(edit, Qt.Key_Control, Qt.ControlModifier)

    assert edit.binding() == "F5"


def test_capturing_a_key_reports_the_change(edit):
    """The dialog re-checks conflicts off this, as it did off
    QKeySequenceEdit's own signal."""
    seen = []
    edit.binding_changed.connect(lambda: seen.append(edit.binding()))

    QTest.keyClick(edit, Qt.Key_F7)

    assert seen == ["F7"]


def test_the_field_cannot_be_typed_into(edit):
    """It is a capture display, not a text box -- a hand-typed "Ctrl+" would be
    stored as a binding nothing can ever match."""
    assert edit.isReadOnly()


# ============================== the freeze ==================================
def test_it_claims_shortcut_override(edit):
    """Qt's own mechanism for "these keys are mine". Accepting the event is what
    stops every QShortcut in the app firing the action being rebound."""
    event = QEvent(QEvent.ShortcutOverride)

    handled = edit.event(event)

    assert handled and event.isAccepted()


def test_a_real_shortcut_does_not_fire_while_capturing(app, edit):
    """End to end: a QShortcut on the same chord must stand down."""
    from PySide6.QtGui import QKeySequence, QShortcut
    fired = []
    shortcut = QShortcut(QKeySequence("Ctrl+E"), edit)
    shortcut.setContext(Qt.ApplicationShortcut)
    shortcut.activated.connect(lambda: fired.append(True))

    QTest.keyClick(edit, Qt.Key_E, Qt.ControlModifier)

    assert edit.binding() == "Ctrl+E"
    assert fired == [], "the shortcut fired instead of being captured"


# ============================== mouse =======================================
def test_the_middle_button_is_recorded(edit):
    _press(edit, Qt.MiddleButton)

    assert edit.binding() == "Mouse:Middle"


@pytest.mark.parametrize("button, name", [
    (Qt.MiddleButton, "Middle"),
    (Qt.BackButton, "Back"),
    (Qt.ForwardButton, "Forward"),
])
def test_every_spare_button_is_bindable_bare(edit, button, name):
    """Nothing in the app claims these, so they need no modifier."""
    _press(edit, button)

    assert edit.binding() == f"Mouse:{name}"


def test_modifiers_are_kept(edit):
    _press(edit, Qt.MiddleButton, Qt.ControlModifier | Qt.ShiftModifier)

    assert edit.binding() == "Ctrl+Shift+Mouse:Middle"


@pytest.mark.parametrize("button, name", [
    (Qt.LeftButton, "Left"), (Qt.RightButton, "Right"),
])
def test_a_bare_left_or_right_click_is_refused(edit, button, name):
    """A bare left click is how this field gets focus in the first place, so
    binding one would break the thing you used to bind it."""
    QTest.keyClick(edit, Qt.Key_F8)
    warnings = []
    edit.rejected.connect(warnings.append)

    _press(edit, button)

    assert edit.binding() == "F8", "the refused press must not overwrite"
    assert warnings and name in warnings[0]


def test_the_refusal_says_what_to_do_instead(edit):
    """A press that silently does nothing reads as a broken dialog."""
    warnings = []
    edit.rejected.connect(warnings.append)

    _press(edit, Qt.LeftButton)

    assert "modifier" in warnings[0].lower()


@pytest.mark.parametrize("button, expected", [
    (Qt.LeftButton, "Ctrl+Mouse:Left"),
    (Qt.RightButton, "Ctrl+Mouse:Right"),
])
def test_left_and_right_are_bindable_with_a_modifier(edit, button, expected):
    _press(edit, button, Qt.ControlModifier)

    assert edit.binding() == expected


def test_an_unbindable_button_is_left_alone(edit):
    """Some mice report more buttons than are worth naming; those stay ordinary
    clicks rather than becoming an unparseable binding."""
    QTest.keyClick(edit, Qt.Key_F9)

    _press(edit, Qt.ExtraButton7)

    assert edit.binding() == "F9"


# ============================== the format ==================================
def test_a_mouse_binding_round_trips():
    text = controls_mod.format_mouse_binding("Left", ["Ctrl"])

    assert controls_mod.parse_mouse_binding(text) == (("Ctrl",), "Left")


def test_modifier_order_is_fixed():
    """Two identical gestures must produce the same string, or the conflict
    check would not see them as a clash."""
    a = controls_mod.format_mouse_binding("Middle", ["Shift", "Ctrl"])
    b = controls_mod.format_mouse_binding("Middle", ["Ctrl", "Shift"])

    assert a == b == "Ctrl+Shift+Mouse:Middle"


def test_a_key_binding_is_not_a_mouse_binding():
    assert controls_mod.parse_mouse_binding("Ctrl+E") is None
    assert not controls_mod.is_mouse_binding("Ctrl+E")


def test_a_bare_left_binding_never_parses():
    """Defence in depth: even a hand-edited ini cannot arm a bare left click."""
    assert controls_mod.parse_mouse_binding("Mouse:Left") is None
    assert controls_mod.parse_mouse_binding("Mouse:Right") is None


def test_an_unknown_button_parses_as_nothing():
    """A file from a build that knew more buttons degrades to unbound, like an
    unknown action id does."""
    assert controls_mod.parse_mouse_binding("Mouse:Pinky") is None


def test_an_event_and_a_stored_binding_agree(edit):
    """The capture side and the dispatch side must produce the same tuple, or a
    binding could be assignable and never fire."""
    event = QMouseEvent(QEvent.MouseButtonPress, QPointF(1, 1), QPointF(1, 1),
                        Qt.MiddleButton, Qt.MiddleButton, Qt.ControlModifier)

    assert mouse_event_binding(event) == \
        controls_mod.parse_mouse_binding("Ctrl+Mouse:Middle")


def test_a_bare_left_event_resolves_to_nothing():
    event = QMouseEvent(QEvent.MouseButtonPress, QPointF(1, 1), QPointF(1, 1),
                        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)

    assert mouse_event_binding(event) is None


# ============================== the dialog ==================================
def test_a_mouse_binding_survives_the_dialog(app):
    from trackcleaner.ui.controls_dialog import ControlsDialog
    d = ControlsDialog({"app.refresh": "Ctrl+Mouse:Middle"})

    assert d.bindings()["app.refresh"] == "Ctrl+Mouse:Middle"


def test_two_identical_mouse_bindings_conflict():
    """Mouse bindings are plain strings, so the existing conflict check covers
    them with no special case."""
    clashes = controls_mod.conflicts({"a": "Mouse:Middle", "b": "Mouse:Middle"})

    assert clashes == {"Mouse:Middle": ["a", "b"]}


def test_the_dialog_shows_a_refusal(app):
    from trackcleaner.ui.controls_dialog import ControlsDialog
    d = ControlsDialog(controls_mod.defaults())

    _press(d._edits["app.refresh"], Qt.LeftButton)

    assert "modifier" in d._conflict_label.text().lower()


def test_a_mouse_binding_round_trips_through_the_file(tmp_path):
    path = str(tmp_path / "userControls.ini")
    bindings = controls_mod.defaults()
    bindings["app.refresh"] = "Ctrl+Mouse:Back"

    controls_mod.save(bindings, path)

    assert controls_mod.load(path)["app.refresh"] == "Ctrl+Mouse:Back"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
