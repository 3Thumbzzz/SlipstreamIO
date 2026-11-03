"""Edit the text metadata of one or several tracks.

The multi-track rule is the whole design. A field whose value differs across the
selection shows :data:`VARIOUS`; leaving that placeholder alone leaves every
track at its own value, and typing over it sets them all to what you typed. So
the dialog returns **only the fields the user actually changed**, and the
adapters treat an absent field as "leave it alone".

Nothing is written until **Apply**. Cancel and the window's X are the same
action, which is why both route through ``reject()`` -- a dialog that wrote on
close would make the X a destructive button that looks like an escape.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QGridLayout, QLabel,
                               QLineEdit, QVBoxLayout)

#: Shown for a field the selected tracks disagree on. Not a legal tag value
#: anyone would type by accident, and visibly not real text.
VARIOUS = "<various>"

#: Field id -> label, in the order they appear. Built from the adapter's
#: EDITABLE_FIELDS at construction so BPM/key cannot creep in here separately.
LABELS = {
    "title": "Title",
    "artist": "Artist",
    "album": "Album",
    "genre": "Genre",
    "comment": "Comment",
}


def common_values(tracks, fields) -> dict:
    """``{field: shared value}``, with :data:`VARIOUS` where they differ.

    Separated from the widget so the rule can be tested without a dialog: it is
    the part that decides what a multi-track edit is even offering to do.
    """
    out = {}
    for field in fields:
        seen = {getattr(t, field, "") or "" for t in tracks}
        out[field] = seen.pop() if len(seen) == 1 else VARIOUS
    return out


def changed_fields(original: dict, edited: dict) -> dict:
    """Only the fields the user actually altered.

    A field still showing :data:`VARIOUS` was not touched, so it is omitted and
    each track keeps its own value. Everything else is compared against what was
    shown, so re-typing the same text writes nothing.
    """
    return {f: v for f, v in edited.items()
            if v != original.get(f) and not (v == VARIOUS)}


class MetadataDialog(QDialog):
    """Modal editor for the selected tracks' text metadata."""

    def __init__(self, tracks: list, fields=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit track" if len(tracks) == 1
                            else f"Edit {len(tracks)} tracks")
        self.setMinimumWidth(460)
        self._fields = [f for f in (fields or LABELS) if f in LABELS]
        self._original = common_values(tracks, self._fields)
        self._edits: dict[str, QLineEdit] = {}

        grid = QGridLayout()
        for row, field in enumerate(self._fields):
            grid.addWidget(QLabel(LABELS[field] + ":"), row, 0)
            edit = QLineEdit(self._original[field])
            if self._original[field] == VARIOUS:
                edit.setPlaceholderText("differs across the selection")
            grid.addWidget(edit, row, 1)
            self._edits[field] = edit
        grid.setColumnStretch(1, 1)

        # Apply/Cancel, bottom right. RejectRole on Cancel means the window's X
        # and the button land in the same place -- reject() -- so closing can
        # never be mistaken for confirming.
        buttons = QDialogButtonBox(QDialogButtonBox.Apply | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        if len(tracks) > 1:
            note = QLabel(f"Editing {len(tracks)} tracks. A field showing "
                          f"“{VARIOUS}” is left as it is unless you change it.")
            note.setWordWrap(True)
            lay.addWidget(note)
        lay.addLayout(grid)
        lay.addStretch(1)
        lay.addWidget(buttons, 0, Qt.AlignRight)

    def edited_values(self) -> dict:
        return {f: e.text() for f, e in self._edits.items()}

    def result_fields(self) -> dict:
        """The fields to write. Empty when nothing was altered, so the caller can
        skip the write entirely rather than queue a no-op."""
        return changed_fields(self._original, self.edited_values())
