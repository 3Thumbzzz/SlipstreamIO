"""Left-column 'playlist metadata fields' panel: a checkbox list mirroring the
track table's visible/hidden columns. Ticking here and ticking the header
right-click menu stay in sync via the shared hidden-set."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QListWidget, QListWidgetItem

from .fields import ALL_COLUMN_IDS, LABEL_BY_ID, STATUS_COL


class FieldsPanel(QListWidget):
    field_toggled = Signal(str, bool)   # col_id, visible

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: dict[str, QListWidgetItem] = {}
        for col_id in ALL_COLUMN_IDS:
            it = QListWidgetItem(LABEL_BY_ID[col_id])
            it.setData(Qt.UserRole, col_id)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            if col_id == STATUS_COL:
                it.setFlags(it.flags() & ~Qt.ItemIsUserCheckable)  # always on
            self.addItem(it)
            self._items[col_id] = it
        self.itemChanged.connect(self._on_changed)

    def _on_changed(self, item: QListWidgetItem):
        col_id = item.data(Qt.UserRole)
        self.field_toggled.emit(col_id, item.checkState() == Qt.Checked)

    def set_hidden(self, hidden: set[str]):
        self.blockSignals(True)
        for col_id, it in self._items.items():
            it.setCheckState(Qt.Unchecked if col_id in hidden else Qt.Checked)
        self.blockSignals(False)
