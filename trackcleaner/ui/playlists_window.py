"""'Playlists…' popup: every playlist, in every loaded program, that contains a
given track.

Grouped by program with a header per section, because "where does this track
live" is a question about the whole library set rather than about whichever pane
you happened to right-click in. Double-clicking a row navigates to it and snaps
the sidebar highlight (handled by the parent via the ``navigate`` signal), which
is why each row carries its program alongside its node.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QLabel, QListWidget, QListWidgetItem,
                               QVBoxLayout)

from ..model.library import TreeNode

_NODE_ROLE = Qt.UserRole
_SOURCE_ROLE = Qt.UserRole + 1


class PlaylistsWindow(QDialog):
    navigate = Signal(object, str)   # TreeNode, source

    def __init__(self, libraries: dict, track_key: str, track_label: str,
                 first_source: str = "", parent=None):
        """``libraries`` is ``{adapter_id: Library}`` -- the window's own dict.

        ``first_source`` is listed first: the program whose pane the track was
        right-clicked in is the one the question is most likely about, so it
        should not be somewhere down the list.
        """
        super().__init__(parent)
        self.setWindowTitle("Playlists containing this track")
        self.resize(460, 420)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{track_label}</b>"))

        self.list = QListWidget()
        total = 0
        for source in self._ordered(libraries, first_source):
            library = libraries.get(source)
            if library is None:
                continue
            nodes = library.playlists_containing(track_key)
            total += len(nodes)
            self.list.addItem(self._header(
                f"{source.upper()} — {len(nodes) if nodes else 'none'}"))
            for node in nodes:
                item = QListWidgetItem("    " + node.path_str())
                item.setData(_NODE_ROLE, node)
                item.setData(_SOURCE_ROLE, source)
                self.list.addItem(item)

        if not total:
            self.list.addItem(self._header("(not in any playlist)"))
        self.list.itemDoubleClicked.connect(self._on_activate)
        lay.addWidget(self.list)

    @staticmethod
    def _ordered(libraries: dict, first_source: str) -> list:
        """Loaded programs, the track's own first, the rest in their own order."""
        rest = [s for s in libraries if s != first_source]
        return ([first_source] if first_source in libraries else []) + rest

    @staticmethod
    def _header(text: str) -> QListWidgetItem:
        """A section label. Deliberately not selectable, so double-clicking one
        cannot try to navigate to a program rather than to a playlist."""
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemIsEnabled)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        return item

    def _on_activate(self, item: QListWidgetItem):
        node = item.data(_NODE_ROLE)
        source = item.data(_SOURCE_ROLE)
        if isinstance(node, TreeNode) and source:
            self.navigate.emit(node, source)
            self.accept()
