"""The Controls dialog: assign a key or a mouse button to any bindable action.

Bindings are *captured* rather than typed — nobody should have to know Qt's
portable spelling of "Ctrl+Shift+Backslash". :class:`ShortcutEdit` does the
capturing; ``QKeySequenceEdit`` could not, because Qt's focus handling eats Tab
before that widget sees it and ``QKeySequence`` cannot represent a mouse button
at all.

Conflicts are surfaced live rather than resolved silently. Two actions on one
chord means Qt fires whichever it finds first and the other never works, which
looks like a broken shortcut rather than a clash the user created.

Nothing is written until OK, matching the metadata dialog: Cancel and the
window's X are the same action.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QKeyCombination, Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QScrollArea, QSlider, QVBoxLayout,
                               QWidget)

from ..config import controls as controls_mod


#: Qt button -> the name a binding is written with. Only these are bindable;
#: anything else a mouse reports is left alone.
_BUTTON_NAMES = {
    Qt.LeftButton: "Left",
    Qt.RightButton: "Right",
    Qt.MiddleButton: "Middle",
    Qt.BackButton: "Back",
    Qt.ForwardButton: "Forward",
}

_MODIFIER_FLAGS = (
    (Qt.ControlModifier, "Ctrl"),
    (Qt.AltModifier, "Alt"),
    (Qt.ShiftModifier, "Shift"),
    (Qt.MetaModifier, "Meta"),
)


def _modifier_names(modifiers) -> list:
    return [name for flag, name in _MODIFIER_FLAGS if modifiers & flag]


def mouse_event_binding(event):
    """A press -> ``(modifiers, button)`` in the form
    :func:`controls.parse_mouse_binding` produces, or None.

    Shared with the dispatcher so a captured gesture and a dispatched one are
    compared as the same tuple. Going through the binding *string* keeps the two
    directions honest: whatever this widget can record is exactly what can fire.
    """
    name = _BUTTON_NAMES.get(event.button())
    if name is None:
        return None
    return controls_mod.parse_mouse_binding(
        controls_mod.format_mouse_binding(name, _modifier_names(event.modifiers())))


class ShortcutEdit(QLineEdit):
    """Captures one binding: a key chord, or a mouse button.

    Replaces ``QKeySequenceEdit``, which cannot do either of the things asked of
    it here. **Tab** never reaches that widget -- Qt's focus handling consumes it
    first, so pressing Tab moved to the next control instead of being recorded --
    and ``QKeySequence`` has no representation for a mouse button at all.

    Read-only: the text is written by capture, never typed.
    """

    #: A press that was deliberately not recorded, with the reason. The dialog
    #: shows it, so a rejected assignment says why instead of appearing dead.
    rejected = Signal(str)
    #: Emitted whenever the captured binding changes, so the dialog can re-check
    #: for conflicts exactly as QKeySequenceEdit's signal let it.
    binding_changed = Signal()

    def __init__(self, binding: str = "", parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText("press keys, or a mouse button")
        self._binding = ""
        self.set_binding(binding)

    # ------------------------------------------------------------- state
    def binding(self) -> str:
        return self._binding

    def set_binding(self, binding: str) -> None:
        binding = binding or ""
        if binding == self._binding:
            return
        self._binding = binding
        self.setText(binding)
        self.binding_changed.emit()

    def clear(self) -> None:
        self.set_binding("")

    # ------------------------------------------------------------ capture
    def event(self, event):
        """Take key events before Qt's focus machinery does.

        This is the whole reason for the override: ``keyPressEvent`` is too late
        for Tab and Backtab, which ``QWidget.event`` turns into focus changes
        before any subclass sees them.

        ``ShortcutOverride`` is accepted while this field has focus, which is
        Qt's own way of saying "these keys are mine" -- it stops every QShortcut
        in the app firing the action you are in the middle of rebinding, with no
        need to disable and restore them by hand.
        """
        etype = event.type()
        # No hasFocus() guard: Qt delivers both of these to the focus widget, so
        # one arriving here IS the proof. Testing it as well would have made the
        # field silently uncapturable anywhere focus is not literally set --
        # every headless test, for one.
        if etype == QEvent.ShortcutOverride:
            event.accept()
            return True
        if etype == QEvent.KeyPress:
            self._capture_key(event)
            return True
        return super().event(event)

    def _capture_key(self, event) -> None:
        key = event.key()
        # A modifier on its own is half a chord, not a binding.
        if key in (Qt.Key_Control, Qt.Key_Alt, Qt.Key_Shift, Qt.Key_Meta,
                   Qt.Key_unknown):
            return
        if key == Qt.Key_Backtab:
            # Qt reports Shift+Tab as its own key, "Backtab", and QKeySequence
            # spells it that way -- so the field showed "Shift+Backtab" for a
            # chord every user calls Shift+Tab. Recorded under the name people
            # use; verified that a QShortcut bound to "Shift+Tab" really does
            # fire on this press, since Qt treats the two sequences as unequal
            # and only canonicalises them when matching.
            self.set_binding(QKeySequence(
                QKeyCombination(event.modifiers(), Qt.Key_Tab)).toString())
            return
        self.set_binding(QKeySequence(event.keyCombination()).toString())

    def mousePressEvent(self, event):
        name = _BUTTON_NAMES.get(event.button())
        if name is None:
            return super().mousePressEvent(event)
        modifiers = _modifier_names(event.modifiers())
        if name in controls_mod.MOUSE_NEEDS_MODIFIER and not modifiers:
            # A bare left click is how this field gets focus in the first place,
            # and both buttons do too much elsewhere to be taken bare. Say so --
            # a press that silently does nothing reads as a broken dialog.
            self.rejected.emit(
                f"{name} mouse button needs a modifier — hold Ctrl, Alt or "
                f"Shift while clicking to assign it.")
            return super().mousePressEvent(event)
        self.set_binding(controls_mod.format_mouse_binding(name, modifiers))
        self.setFocus()
        event.accept()


class _MsSlider(QWidget):
    """A labelled millisecond slider with a live readout.

    Two of these rather than one shared value: a double-click and a double-tap
    are different gestures, and the comfortable timing for the mouse is not the
    comfortable timing for holding down an arrow key.
    """

    def __init__(self, label: str, hint: str, value: int, parent=None):
        super().__init__(parent)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(controls_mod.TIMING_MIN_MS, controls_mod.TIMING_MAX_MS)
        self.slider.setValue(value)
        self._readout = QLabel()
        self._readout.setMinimumWidth(56)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        name = QLabel(label)
        name.setToolTip(hint)
        name.setMinimumWidth(180)
        row.addWidget(name)
        row.addWidget(self.slider, 1)
        row.addWidget(self._readout)
        self.slider.valueChanged.connect(self._show)
        self._show(value)

    def _show(self, value: int) -> None:
        self._readout.setText(f"{value} ms")

    def value(self) -> int:
        return self.slider.value()

    def set_value(self, value: int) -> None:
        self.slider.setValue(value)


class ControlsDialog(QDialog):
    def __init__(self, bindings: dict, timing: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Controls")
        self.setMinimumSize(520, 560)
        self._edits: dict[str, QKeySequenceEdit] = {}

        body = QWidget()
        grid = QGridLayout(body)
        row = 0
        for group in controls_mod.GROUPS:
            header = QLabel(group)
            header.setStyleSheet("font-weight: bold; margin-top: 8px;")
            grid.addWidget(header, row, 0, 1, 3)
            row += 1
            for action in [a for a in controls_mod.ACTIONS if a.group == group]:
                grid.addWidget(QLabel(action.label), row, 0)
                edit = ShortcutEdit(bindings.get(action.id, ""))
                edit.binding_changed.connect(self._refresh_conflicts)
                edit.rejected.connect(self._show_rejection)
                grid.addWidget(edit, row, 1)
                clear = QPushButton("Clear")
                clear.setFixedWidth(58)
                clear.clicked.connect(lambda _=False, e=edit: e.clear())
                grid.addWidget(clear, row, 2)
                self._edits[action.id] = edit
                row += 1
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(row, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)

        # Timing sits OUTSIDE the scroll area: it is two controls, and hiding
        # them below a long list of bindings would make them undiscoverable.
        timing = timing or dict(controls_mod.TIMING_DEFAULTS)
        timing_header = QLabel("Timing")
        timing_header.setStyleSheet("font-weight: bold; margin-top: 8px;")
        self._double_click = _MsSlider(
            "Double-click",
            "How long a click waits to see whether a second one follows. Also "
            "sets the double-click interval the whole app uses.",
            self._seed_double_click(timing.get("double_click_ms", 0)))
        self._double_tap = _MsSlider(
            "Double-tap (folder expand)",
            "Two presses of Left or Right closer together than this expand or "
            "collapse a folder instead of stepping past it.",
            timing.get("double_tap_ms", controls_mod.TIMING_DEFAULTS["double_tap_ms"]))

        self._conflict_label = QLabel("")
        self._conflict_label.setWordWrap(True)
        self._conflict_label.setStyleSheet("color: #c0392b;")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        reset = buttons.addButton("Reset to defaults", QDialogButtonBox.ResetRole)
        reset.clicked.connect(self._reset)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Click a field and press the keys you want. "
                             "Clear a field to unbind it."))
        lay.addWidget(scroll, 1)
        lay.addWidget(timing_header)
        lay.addWidget(self._double_click)
        lay.addWidget(self._double_tap)
        lay.addWidget(self._conflict_label)
        lay.addWidget(buttons)
        self._refresh_conflicts()

    @staticmethod
    def _seed_double_click(saved: int) -> int:
        """A slider cannot show "0 = follow the OS", so it opens on whatever that
        currently resolves to. Moving it is what pins a value."""
        if saved:
            return saved
        return max(controls_mod.TIMING_MIN_MS,
                   min(controls_mod.TIMING_MAX_MS,
                       QApplication.doubleClickInterval()))

    # ---------------------------------------------------------------- state
    def bindings(self) -> dict:
        return {aid: e.binding() for aid, e in self._edits.items()}

    def timing(self) -> dict:
        return {"double_click_ms": self._double_click.value(),
                "double_tap_ms": self._double_tap.value()}

    def _reset(self) -> None:
        for aid, seq in controls_mod.defaults().items():
            if aid in self._edits:
                self._edits[aid].set_binding(seq)
        self._double_click.set_value(self._seed_double_click(0))
        self._double_tap.set_value(controls_mod.TIMING_DEFAULTS["double_tap_ms"])
        self._refresh_conflicts()

    def _show_rejection(self, message: str) -> None:
        """A press that was not recorded, shown where conflicts are shown -- one
        place to look when an assignment does not take."""
        self._conflict_label.setText(message)

    def _refresh_conflicts(self) -> None:
        clashes = controls_mod.conflicts(self.bindings())
        if not clashes:
            self._conflict_label.setText("")
            return
        lines = []
        for seq, ids in sorted(clashes.items()):
            names = ", ".join(controls_mod.ACTION_BY_ID[i].label for i in ids)
            lines.append(f"{seq} is assigned to: {names}")
        self._conflict_label.setText(
            "Conflicting shortcuts — only one of each will work:\n"
            + "\n".join(lines))
