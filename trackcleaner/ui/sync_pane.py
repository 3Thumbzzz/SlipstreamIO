"""The "needs sync" pane: every playlist whose square is not green, per program.

A worklist rather than a browser. The sidebar trees show a library's whole
structure, where one yellow square among hundreds of playlists has to be found
by scrolling -- the same problem the status bar's out-of-sync count solves for
rows. This lists only the playlists that need managing, each under its program,
with the very glyph its sidebar row shows.

**It computes nothing.** The main window hands it the status functions the
sidebar was painted with (edited-yellow overlay included), so the two can never
disagree about a playlist. Rows are flat ``Folder / Playlist`` paths: the point
is to see them all at once, not to navigate a hierarchy.

**It owns no actions.** Its selection is mirrored into the program's sidebar
tree, so loading goes through exactly the path a sidebar click takes, and
right-clicking opens that program's ordinary playlist menu. Sync, Clean up and
the rest act on "the whole selection when the clicked row is part of it" --
the sidebar's rule -- so Shift/Ctrl-selecting rows here syncs them together
with no second implementation.

**A selection belongs to one program**, as in the sidebar, where only one tree
holds a selection at a time: a sync goes *from* one program. Ctrl-clicking a
row of another program starts a new selection there.

**Below the playlists, the songs.** Every track of the highlighted playlists
that is not green, with its glyph and a few words on what differs ("cues,
genre", "file missing", "added in Traktor"). One yellow track among 517 rows
is what made a correct yellow square look like a bug; this names it. The rows
and their glyphs are the track table's own, handed over by the window. Songs
are selected like any track list (Shift/Ctrl), the selection is mirrored into
the track table, and right-click opens the ordinary track menu for all of it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QHBoxLayout,
                               QLabel, QSplitter, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from ..model import status as status_mod
from .status_squares import status_icon

_NODE_ROLE = Qt.UserRole
_GROUP_ROLE = Qt.UserRole + 1
_STATUS_ROLE = Qt.UserRole + 2
_KEY_ROLE = Qt.UserRole + 3

#: Statuses that mean "this playlist needs managing". BLACK -- not in the
#: reference yet -- is opt-in: a library holding many single-program playlists
#: would otherwise bury the handful that really diverge.
NEEDS_SYNC = (status_mod.RED,) + status_mod.YELLOWS


def needs_sync(status: str, include_missing: bool = False) -> bool:
    return status in NEEDS_SYNC or (include_missing and status == status_mod.BLACK)


class _PaneTree(QTreeWidget):
    def mousePressEvent(self, event):
        # Right-click must not move the selection, exactly as in the sidebar:
        # the menu acts on the whole selection when the clicked row is part of
        # it, which is what lets several rows be synced at once.
        if event.button() == Qt.RightButton:
            event.accept()
            return
        super().mousePressEvent(event)


class SyncPane(QWidget):
    """Grouped list of the playlists that are not in sync, refreshed by the
    window whenever the sidebar squares are repainted."""

    selection_changed = Signal(list, str)             # [TreeNode], source
    # TreeNode, global pos, [the selected TreeNodes of that row's program]
    node_context_requested = Signal(object, object, list)
    include_missing_changed = Signal(bool)
    songs_selected = Signal(list, str)                # [track key], source
    # track key, global pos, [the selected track keys]
    song_context_requested = Signal(str, object, list)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Needs sync")
        title.setStyleSheet("font-weight: bold; padding: 2px;")
        header.addWidget(title)
        header.addStretch(1)
        self.count_label = QLabel("")
        header.addWidget(self.count_label)
        lay.addLayout(header)

        self.include_missing = QCheckBox("Include playlists missing from the reference")
        self.include_missing.setToolTip(
            "Also list playlists the reference program does not have yet "
            "(black squares)")
        self.include_missing.toggled.connect(self.include_missing_changed)
        lay.addWidget(self.include_missing)

        self.tree = _PaneTree()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.itemSelectionChanged.connect(self._on_selection)
        self.tree.customContextMenuRequested.connect(self._on_context)

        songs = QWidget()
        songs_lay = QVBoxLayout(songs)
        songs_lay.setContentsMargins(0, 0, 0, 0)
        songs_lay.setSpacing(2)
        songs_header = QHBoxLayout()
        songs_title = QLabel("Songs to sync")
        songs_title.setStyleSheet("font-weight: bold; padding: 2px;")
        songs_header.addWidget(songs_title)
        songs_header.addStretch(1)
        self.songs_count = QLabel("")
        songs_header.addWidget(self.songs_count)
        songs_lay.addLayout(songs_header)
        # Selected like a playlist's track list -- Shift/Ctrl for several, and a
        # right-click that leaves the selection alone -- and mirrored into the
        # track table, so the ordinary track menu acts on all of it.
        self.songs = _PaneTree()
        self.songs.setColumnCount(3)
        self.songs.setHeaderLabels(["Title", "Artist", "What differs"])
        self.songs.setRootIsDecorated(False)
        self.songs.setUniformRowHeights(True)
        self.songs.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.songs.setContextMenuPolicy(Qt.CustomContextMenu)
        self.songs.itemSelectionChanged.connect(self._on_song_selection)
        self.songs.customContextMenuRequested.connect(self._on_song_context)
        songs_lay.addWidget(self.songs, 1)
        self._songs_source = ""

        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.tree)
        self.splitter.addWidget(songs)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        lay.addWidget(self.splitter, 1)
        self.populate_songs([], "")

    # -------------------- contents --------------------
    def populate(self, groups: list, note: str = "", missing: int = 0) -> None:
        """``groups`` is ``[(title, [(node, status, dup_allowed), …]), …]`` in
        display order; a group with no rows is left out. ``note`` replaces the
        list entirely -- for "nothing to compare" rather than "nothing to do".

        ``missing`` counts the BLACK playlists left out because the checkbox is
        off. An empty list must not claim "everything is in sync" about a
        library where nothing has been synced yet."""
        collapsed = {self.tree.topLevelItem(i).data(0, _GROUP_ROLE)
                     for i in range(self.tree.topLevelItemCount())
                     if not self.tree.topLevelItem(i).isExpanded()}
        # Kept by path: a refresh replaces every node object. Restored silently,
        # since nothing the user did changed what is selected.
        keep = {_path_key(n) for n in self.selected_nodes()}
        self.tree.blockSignals(True)
        try:
            self._fill(groups, note, missing, collapsed)
            for item in self._items():
                if _path_key(item.data(0, _NODE_ROLE)) in keep:
                    item.setSelected(True)
        finally:
            self.tree.blockSignals(False)

    def _fill(self, groups, note, missing, collapsed) -> None:
        self.tree.clear()
        total = sum(len(rows) for _title, rows in groups)
        extra = (f" · {missing} not in the reference" if missing else "")
        if note or not total:
            if not note:
                note = ("No playlist has diverged." if missing
                        else "Every playlist is in sync.")
            item = QTreeWidgetItem([note])
            item.setDisabled(True)
            self.tree.addTopLevelItem(item)
            self.count_label.setText(extra.lstrip(" ·"))
            return
        for title, rows in groups:
            if not rows:
                continue
            group = QTreeWidgetItem([f"{title}  ({len(rows)})"])
            group.setData(0, _GROUP_ROLE, title)
            group.setFlags(group.flags() & ~Qt.ItemIsSelectable)
            self.tree.addTopLevelItem(group)
            for node, status, dup in rows:
                child = QTreeWidgetItem([("∞  " if dup else "") + node.path_str()])
                child.setIcon(0, status_icon(status))
                child.setData(0, _NODE_ROLE, node)
                child.setData(0, _STATUS_ROLE, status)
                child.setToolTip(0, node.path_str())
                group.addChild(child)
            # Collapsed stays collapsed across a refresh; everything else opens.
            group.setExpanded(title not in collapsed)
        self.count_label.setText(
            f"{total} playlist{'' if total == 1 else 's'}{extra}")

    def populate_songs(self, rows: list, source: str, note: str = "") -> None:
        """``rows`` is ``[(key, title, artist, status, what_differs), …]`` for
        the highlighted playlists' tracks that are not green, in table order.
        ``note`` stands in for an empty list ("select a playlist above").

        The selection is kept by key across a refresh -- a sync of three
        selected songs leaves the others that still differ selected -- and
        restored silently, since the user changed nothing."""
        keep = set(self.selected_song_keys()) if source == self._songs_source else set()
        self._songs_source = source
        self.songs.blockSignals(True)
        try:
            self._fill_songs(rows, source, note)
            for i in range(self.songs.topLevelItemCount()):
                item = self.songs.topLevelItem(i)
                if item.data(0, _KEY_ROLE) in keep:
                    item.setSelected(True)
        finally:
            self.songs.blockSignals(False)

    def _fill_songs(self, rows: list, source: str, note: str) -> None:
        self.songs.clear()
        if not rows:
            item = QTreeWidgetItem([note or ("Every song in the selection is in sync."
                                             if source else
                                             "Select a playlist above to list its songs.")])
            item.setFirstColumnSpanned(True)
            item.setDisabled(True)
            self.songs.addTopLevelItem(item)
            self.songs_count.setText("")
            return
        for key, title, artist, status, reason in rows:
            item = QTreeWidgetItem([title or "", artist or "", reason or ""])
            item.setIcon(0, status_icon(status))
            item.setData(0, _KEY_ROLE, key)
            item.setData(0, _STATUS_ROLE, status)
            item.setToolTip(0, key)
            item.setToolTip(2, reason or "")
            self.songs.addTopLevelItem(item)
        for column in range(2):
            self.songs.resizeColumnToContents(column)
        n = len(rows)
        self.songs_count.setText(f"{n} song{'' if n == 1 else 's'}")

    def song_rows(self) -> list:
        """``[(key, status, what_differs), …]`` as listed."""
        out = []
        for i in range(self.songs.topLevelItemCount()):
            item = self.songs.topLevelItem(i)
            if item.data(0, _KEY_ROLE) is not None:
                out.append((item.data(0, _KEY_ROLE), item.data(0, _STATUS_ROLE),
                            item.text(2)))
        return out

    def selected_song_keys(self) -> list:
        """The selected songs' track keys, in list order."""
        return [self.songs.topLevelItem(i).data(0, _KEY_ROLE)
                for i in range(self.songs.topLevelItemCount())
                if self.songs.topLevelItem(i).isSelected()
                and self.songs.topLevelItem(i).data(0, _KEY_ROLE) is not None]

    def _on_song_selection(self):
        keys = self.selected_song_keys()
        if keys:
            self.songs_selected.emit(keys, self._songs_source)

    def _on_song_context(self, pos):
        item = self.songs.itemAt(pos)
        key = item.data(0, _KEY_ROLE) if item is not None else None
        if key:
            self.song_context_requested.emit(
                key, self.songs.viewport().mapToGlobal(pos),
                self.selected_song_keys())

    def _items(self):
        """Every playlist row, in display order."""
        for i in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            for j in range(group.childCount()):
                if group.child(j).data(0, _NODE_ROLE) is not None:
                    yield group.child(j)

    def rows(self) -> list:
        """``[(node, status), …]`` for every listed playlist, in display order."""
        return [(item.data(0, _NODE_ROLE), item.data(0, _STATUS_ROLE))
                for item in self._items()]

    def nodes(self) -> list:
        """Every listed playlist node, in display order."""
        return [node for node, _status in self.rows()]

    def selected_nodes(self) -> list:
        """The selected playlist nodes, in display order."""
        return [item.data(0, _NODE_ROLE) for item in self._items()
                if item.isSelected()]

    # -------------------- interaction --------------------
    def _on_selection(self):
        """Keep the selection to one program, then report it.

        The program is the current item's -- the row just clicked -- so
        Ctrl-clicking into another group moves the selection there rather than
        straddling two programs a sync could not come from."""
        current = self.tree.currentItem()
        node = current.data(0, _NODE_ROLE) if current is not None else None
        selected = self.selected_nodes()
        if node is not None and any(n.source != node.source for n in selected):
            self.tree.blockSignals(True)
            for item in self._items():
                if item.data(0, _NODE_ROLE).source != node.source:
                    item.setSelected(False)
            self.tree.blockSignals(False)
            selected = self.selected_nodes()
        # An empty selection reports nothing: emptying the sidebar would blank
        # the tab that is open, which deselecting a worklist row should not do.
        if selected:
            self.selection_changed.emit(selected, selected[0].source)

    def _on_context(self, pos):
        item = self.tree.itemAt(pos)
        node = item.data(0, _NODE_ROLE) if item is not None else None
        if node is not None:
            selected = [n for n in self.selected_nodes() if n.source == node.source]
            self.node_context_requested.emit(
                node, self.tree.viewport().mapToGlobal(pos), selected)


def _path_key(node) -> tuple:
    return (node.source, tuple(node.node_path))
