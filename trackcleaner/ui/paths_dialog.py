"""Paths configuration dialog (Paths menu -> Configure paths…).

Lets the user point at each program's library file and the start_time_report.csv
cache, plus the start_time source mode. Persisted via the Settings object it is
given.

The program rows are built from the registered adapters' ``path_fields()``, so
adding a program adds its row here for free -- there is no list of programs in
this file.
"""

from __future__ import annotations

import os

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout)

from ..adapters import PathField, all_path_fields
from ..config.paths import Settings, ensure_start_time_csv
from . import theme

# The start-time cache belongs to no program, so it is stated here rather than
# coming off an adapter.
CSV_FIELD = PathField("csv", "start_time_report.csv:", "start_time cache",
                      "CSV (*.csv)")


class PathsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Configure paths")
        self.setMinimumWidth(640)

        grid = QGridLayout()
        self.fields = list(all_path_fields(settings.paths)) + [CSV_FIELD]
        self.edits: dict[str, QLineEdit] = {}
        for row, field in enumerate(self.fields):
            self.edits[field.key] = self._row(
                grid, row, field, getattr(settings.paths, field.key, ""))

        mode_row = len(self.fields)
        grid.addWidget(QLabel("start_time source:"), mode_row, 0)
        self._mode = QComboBox()
        self._mode.addItems(["update", "cache", "probe"])
        self._mode.setCurrentText(settings.start_time_source)
        grid.addWidget(self._mode, mode_row, 1)

        note = QLabel("VirtualDJ must be CLOSED while writing — it rewrites "
                      "database.xml on exit. Folders/ is read next to database.xml.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.WARNING_FG[theme.current()]};")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(grid)
        lay.addWidget(note)
        lay.addWidget(buttons)

    def _row(self, grid, r, field: PathField, value):
        grid.addWidget(QLabel(field.label), r, 0)
        edit = QLineEdit(value)
        grid.addWidget(edit, r, 1)
        btn = QPushButton("Browse…")
        btn.clicked.connect(lambda: self._pick(field, edit))
        grid.addWidget(btn, r, 2)
        return edit

    def _pick(self, field: PathField, edit: QLineEdit):
        start = edit.text() or ""
        chooser = (QFileDialog.getOpenFileName if field.must_exist
                   else QFileDialog.getSaveFileName)
        path, _ = chooser(self, field.caption, start, field.file_filter)
        if path:
            edit.setText(path)

    def _accept(self):
        for field in self.fields:
            setattr(self.settings.paths, field.key,
                    self.edits[field.key].text().strip())
        self.settings.start_time_source = self._mode.currentText()
        ensure_start_time_csv(self.settings)
        self.settings.save()
        self.accept()
