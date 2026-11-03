"""The left-column playlist trees: Hidden, VDJ structure, Traktor structure.

Each software tree is a :class:`LibraryTree` (a QTreeWidget) supporting
Ctrl/Shift multi-select, collapsible folders, per-playlist status squares, and
folder selection that implicitly covers every playlist beneath it. Nodes in the
hidden-set are omitted and surfaced in the :class:`HiddenTree`; dragging a
playlist onto the Hidden panel (or the Hide context action) hides it.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QItemSelectionModel, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QPixmap
from PySide6.QtWidgets import (QAbstractItemDelegate, QAbstractItemView,
                               QHBoxLayout, QLabel, QSplitter,
                               QStyledItemDelegate, QToolButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from ..config import paths as paths_mod
from ..model.library import Library, TreeNode, VDJ, TRAKTOR, can_move_into
from . import drag_scroll
from .status_squares import status_icon
from .track_table import TRACK_MIME, TrackTable

_NODE_ROLE = Qt.UserRole
_SRC_ROLE = Qt.UserRole + 1


class LibraryTree(QTreeWidget):
    """Renders one Library.root minus any hidden node paths."""

    playlists_selected = Signal(list, str)   # [TreeNode], source
    node_context_requested = Signal(object, object)  # TreeNode, global pos
    # Empty space was right-clicked: there is no node, so the source has to
    # travel with the signal (the main window's _current_source tracks the
    # focused *tab*, which may well be the other program).
    blank_context_requested = Signal(str, object)     # source, global pos
    new_playlist_committed = Signal(str, object, str)  # source, parent node|None, name
    rename_committed = Signal(str, object, str)       # source, node, new name
    # A node of this tree was dragged onto another row (or empty space): move it.
    node_move_requested = Signal(str, list, object)   # source, [TreeNode], target|None
    tracks_dropped = Signal(object, list, str)   # target TreeNode, keys, drag source
    files_dropped = Signal(object, list)     # target TreeNode, external file paths

    def __init__(self, source: str, parent=None):
        super().__init__(parent)
        self.source = source
        self.setHeaderHidden(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.setUniformRowHeights(True)
        self.setDragEnabled(True)
        # Still drags OUT its own playlist nodes (e.g. onto the Hidden panel);
        # now also accepts a TRACK_MIME drop from a track table to add tracks to
        # the playlist dropped on.
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.itemSelectionChanged.connect(self._on_selection)
        self.customContextMenuRequested.connect(self._on_context)
        self._by_nodeid: dict[int, QTreeWidgetItem] = {}
        self._hidden_paths: set[tuple] = set()
        self.hidden_nodes: list[TreeNode] = []   # top-most hidden nodes (for panel)
        # The playlist item a drag is currently hovering, painted transiently so
        # it's obvious which one will receive the drop -- WITHOUT touching the
        # selection (that would reload the shown playlist mid-drag).
        self._hover_item: QTreeWidgetItem | None = None
        # In-place naming of a new/renamed playlist. An explicit delegate is
        # installed so there is a stable object to take closeEditor from.
        self.setItemDelegate(QStyledItemDelegate(self))
        self.itemDelegate().closeEditor.connect(self._on_editor_closed)
        self._reset_pending()
        # Hovering a drag over the top/bottom entry scrolls, accelerating.
        self._scroller = drag_scroll.DragAutoScroller(self)

    def _reset_pending(self):
        self._pending_item: QTreeWidgetItem | None = None
        self._pending_mode = ""          # "create" | "rename"
        self._pending_parent: TreeNode | None = None
        self._pending_node: TreeNode | None = None
        self._pending_old_text = ""

    # -------------------- build --------------------
    def load(self, root: TreeNode | None, hidden_paths: set[tuple] | None = None,
             yield_fn=None):
        # Block signals across the whole rebuild: self.clear() on a tree that had
        # a selection fires itemSelectionChanged -> _on_selection -> emits
        # playlists_selected([]), which the main window treats as "the user
        # deselected everything" and blanks the focused tab (clearing its
        # context). Rebuilding the tree must never masquerade as a user action;
        # callers repopulate/re-select explicitly and silently.
        self.blockSignals(True)
        try:
            self.clear()
            self._reset_pending()      # every item is about to be destroyed
            self._by_nodeid.clear()
            self._hidden_paths = set(hidden_paths or ())
            self.hidden_nodes = []
            if root is None:
                return
            self._build_count = 0
            self._build_yield = yield_fn
            for child in root.children:
                item = self._build(child)
                if item is not None:
                    self.addTopLevelItem(item)
            self._build_yield = None
        finally:
            self.blockSignals(False)

    def _build(self, node: TreeNode) -> QTreeWidgetItem | None:
        if node.node_path in self._hidden_paths:
            self.hidden_nodes.append(node)
            return None
        item = QTreeWidgetItem([node.name])
        item.setData(0, _NODE_ROLE, node)
        self._by_nodeid[id(node)] = item
        if node.playlist is not None:
            item.setIcon(0, status_icon("grey"))
        yf = getattr(self, "_build_yield", None)
        if yf is not None:
            self._build_count += 1
            if (self._build_count & 0x7F) == 0:
                yf()
        for child in node.children:
            child_item = self._build(child)
            if child_item is not None:
                item.addChild(child_item)
        return item

    # -------------------- status --------------------
    def apply_playlist_status(self, status_fn, dup_fn=None):
        """Paint each playlist's status square. When ``dup_fn(node)`` is true the
        item text gets an ∞ prefix (right after the square) to flag that the
        playlist permits duplicate tracks."""
        for item in self._by_nodeid.values():
            node = item.data(0, _NODE_ROLE)
            if node is not None and node.playlist is not None:
                item.setIcon(0, status_icon(status_fn(node)))
                if dup_fn is not None:
                    item.setText(0, ("∞  " if dup_fn(node) else "") + node.name)

    def visible_playlist_nodes(self) -> list[TreeNode]:
        """Every playlist node this tree shows, in tree order. Hidden ones are
        not built, so they are not here either -- what the user hid stays out
        of anything derived from this tree."""
        return [node for node in (item.data(0, _NODE_ROLE)
                                  for item in self._by_nodeid.values())
                if node is not None and node.playlist is not None]

    # -------------------- keyboard stepping --------------------
    def current_or_first(self):
        """The row the keyboard is on, falling back to the first visible one, so
        stepping works before anything has been clicked."""
        item = self.currentItem()
        if item is not None:
            return item
        return self.topLevelItem(0) if self.topLevelItemCount() else None

    def step_row(self, delta: int) -> TreeNode | None:
        """Move the selection one visible row down (+1) or up (-1).

        ``itemBelow``/``itemAbove`` walk what is actually on screen, so a
        collapsed folder's children are skipped without us tracking expansion
        ourselves. Selecting the row is what loads it: the tree's own signal
        does the rest, exactly as a click would.
        """
        item = self.current_or_first()
        if item is None:
            return None
        nxt = self.itemBelow(item) if delta > 0 else self.itemAbove(item)
        if nxt is None:
            return None                        # at an end: stay put
        self.setCurrentItem(nxt)
        self.clearSelection()
        nxt.setSelected(True)
        return nxt.data(0, _NODE_ROLE)

    def current_is_folder(self) -> bool:
        """Whether the keyboard is on a row the expand gesture applies to. The
        arrow router asks first, so only folders pay the double-tap wait."""
        item = self.current_or_first()
        return item is not None and item.childCount() > 0

    def set_current_expanded(self, expanded: bool) -> bool:
        """Expand or collapse the folder the keyboard is on. False when the
        current row is not a folder, so the caller can fall back to stepping."""
        item = self.current_or_first()
        if item is None or not item.childCount():
            return False
        item.setExpanded(bool(expanded))
        return True

    # -------------------- selection --------------------
    def _resolve(self, items) -> list[TreeNode]:
        nodes: list[TreeNode] = []
        seen: set[int] = set()
        for item in items:
            node = item.data(0, _NODE_ROLE)
            if node is None:
                continue
            for pl in node.iter_playlists():
                if id(pl) not in seen:
                    seen.add(id(pl))
                    nodes.append(pl)
        return nodes

    def _on_selection(self):
        self.playlists_selected.emit(self._resolve(self.selectedItems()), self.source)

    def selected_top_nodes(self) -> list[TreeNode]:
        """The literally-selected nodes (folders NOT expanded to playlists)."""
        out = []
        for item in self.selectedItems():
            node = item.data(0, _NODE_ROLE)
            if node is not None:
                out.append(node)
        return out

    def selected_playlist_nodes(self) -> list[TreeNode]:
        return self._resolve(self.selectedItems())

    def select_all_playlists(self):
        self.selectAll()

    def select_node(self, node: TreeNode):
        item = self._by_nodeid.get(id(node))
        if item is None:
            return
        self.clearSelection()
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()
        item.setSelected(True)
        self.scrollToItem(item)
        self.setCurrentItem(item)

    def select_nodes(self, nodes) -> None:
        """Select several nodes as one user action: a single
        ``playlists_selected`` for the lot, not one per row, so the tab loads
        once. Nodes this tree does not show are skipped."""
        items = [self._by_nodeid[id(n)] for n in nodes if id(n) in self._by_nodeid]
        if not items:
            return
        self.blockSignals(True)
        self.clearSelection()
        for item in items:
            parent = item.parent()
            while parent is not None:
                parent.setExpanded(True)
                parent = parent.parent()
            item.setSelected(True)
        # NoUpdate: a plain setCurrentItem is ClearAndSelect, which would keep
        # only the last row.
        self.setCurrentItem(items[-1], 0, QItemSelectionModel.NoUpdate)
        self.scrollToItem(items[0])
        self.blockSignals(False)
        self._on_selection()

    def highlight_node(self, node: TreeNode) -> None:
        """Move the selection to ``node`` WITHOUT emitting ``playlists_selected``
        -- used to mirror the focused tab in the sidebar, where re-emitting would
        reload (and re-scroll) the very table that just took focus. Expands any
        ancestor folders and scrolls the node into view."""
        item = self._by_nodeid.get(id(node))
        if item is None:
            return
        self.blockSignals(True)
        self.clearSelection()
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()
        item.setSelected(True)
        self.setCurrentItem(item)
        self.scrollToItem(item)
        self.blockSignals(False)

    # -------------------- view snapshot (by node_path) --------------------
    # A rebuild replaces every node object, so expansion/selection can only be
    # carried across a reload by node_path, which is stable.
    def _iter_items(self):
        for item in self._by_nodeid.values():
            node = item.data(0, _NODE_ROLE)
            if node is not None:
                yield item, node

    def _items_by_path(self) -> dict[tuple, "QTreeWidgetItem"]:
        return {tuple(node.node_path): item for item, node in self._iter_items()}

    def expanded_paths(self) -> set[tuple]:
        return {tuple(node.node_path) for item, node in self._iter_items()
                if item.isExpanded()}

    def selected_paths(self) -> set[tuple]:
        return {tuple(node.node_path) for item, node in self._iter_items()
                if item.isSelected()}

    def apply_expanded_paths(self, paths: set[tuple]) -> None:
        by_path = self._items_by_path()
        for path in paths:
            item = by_path.get(tuple(path))
            if item is not None:
                item.setExpanded(True)

    def select_paths(self, paths: set[tuple]) -> None:
        """Restore selection without re-emitting -- the caller drives the
        reload of the table itself, so a signal here would double the work."""
        by_path = self._items_by_path()
        self.blockSignals(True)
        self.clearSelection()
        current = None
        for path in paths:
            item = by_path.get(tuple(path))
            if item is not None:
                item.setSelected(True)
                current = item
        if current is not None:
            # NoUpdate, or the restore keeps only the last selected playlist: a
            # plain setCurrentItem is ClearAndSelect.
            self.setCurrentItem(current, 0, QItemSelectionModel.NoUpdate)
            self.scrollToItem(current)
        self.blockSignals(False)

    def mousePressEvent(self, event):
        # A right-click must NOT change the selection: that would fire
        # playlists_selected and reload the focused tab onto whatever was
        # right-clicked. The context menu still arrives via contextMenuEvent, and
        # its actions operate on the node under the cursor, not the selection.
        if event.button() == Qt.RightButton:
            event.accept()
            return
        super().mousePressEvent(event)

    def _on_context(self, pos):
        item = self.itemAt(pos)
        global_pos = self.viewport().mapToGlobal(pos)
        if item is None:
            self.blank_context_requested.emit(self.source, global_pos)
            return
        node = item.data(0, _NODE_ROLE)
        self.node_context_requested.emit(node, global_pos)

    # -------------------- in-place naming (create / rename) --------------------
    # Creating writes nothing until a name is committed: the placeholder item
    # below carries no TreeNode until the main window binds one to it, so an
    # Escape simply removes the row and no file is ever touched. Renaming runs
    # the same machinery on an item that already has a node.
    #
    # Every insert/edit call is wrapped in blockSignals: editItem moves the
    # current item, which fires itemSelectionChanged -> playlists_selected([])
    # (a placeholder resolves to no node) and would blank the focused tab --
    # the same trap load() documents.
    def begin_new_node(self, parent_node: TreeNode | None,
                       label: str = "New playlist") -> None:
        """``label`` seeds the editor: "New folder" for a folder, or the
        pre-filled "Name (2)" a Duplicate starts from."""
        parent_item = (self._by_nodeid.get(id(parent_node))
                       if parent_node is not None else None)
        if parent_node is not None and parent_item is None:
            return                       # parent isn't on screen; nothing to hang it off
        item = QTreeWidgetItem([label])
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        self.blockSignals(True)
        try:
            if parent_item is None:
                self.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
                parent_item.setExpanded(True)
            self._pending_item = item
            self._pending_mode = "create"
            self._pending_parent = parent_node
            self._pending_node = None
            self._pending_old_text = ""
            self.scrollToItem(item)
            self.editItem(item, 0)
        finally:
            self.blockSignals(False)

    def begin_rename(self, node: TreeNode) -> None:
        item = self._by_nodeid.get(id(node))
        if item is None:
            return
        self._pending_item = item
        self._pending_mode = "rename"
        self._pending_parent = None
        self._pending_node = node
        self._pending_old_text = item.text(0)
        self.blockSignals(True)
        try:
            item.setFlags(item.flags() | Qt.ItemIsEditable)
            # The ∞ that apply_playlist_status prefixes is decoration, not part
            # of the name -- edit the real one.
            item.setText(0, node.name)
            self.scrollToItem(item)
            self.editItem(item, 0)
        finally:
            self.blockSignals(False)

    def _on_editor_closed(self, _editor, hint) -> None:
        """Commit and cancel both arrive here. Qt runs commitData (which writes
        the typed value into the item) before closeEditor on a commit, and
        writes nothing at all on Escape -- so ``item.text(0)`` is the answer
        either way. itemChanged is deliberately NOT used: committing the
        unchanged default text emits no itemChanged, while apply_playlist_status
        emits one for every item on every repaint."""
        item = self._pending_item
        if item is None:
            return
        if hint == QAbstractItemDelegate.RevertModelCache:
            self.discard_pending()
            return
        self.commit_pending(item.text(0))

    def commit_pending(self, name: str) -> None:
        """Finish the pending edit with ``name``, emitting on the next
        event-loop tick so the editor is fully torn down before the main window
        mutates the tree underneath it (cf. the deferred scroll restore)."""
        name = (name or "").strip()
        item = self._pending_item
        if item is None:
            return
        if not name:
            self.discard_pending()
            return
        mode, node, parent = self._pending_mode, self._pending_node, self._pending_parent
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        if mode == "create":
            # _pending_item stays set: the placeholder is unbound until the main
            # window answers with bind_new_playlist() or discard_pending().
            QTimer.singleShot(
                0, lambda: self.new_playlist_committed.emit(self.source, parent, name))
        else:
            self._reset_pending()
            QTimer.singleShot(
                0, lambda: self.rename_committed.emit(self.source, node, name))

    def discard_pending(self) -> None:
        """Drop the pending edit: a placeholder row disappears (nothing was ever
        written), a rename reverts to the text it had."""
        item, mode, old_text = (self._pending_item, self._pending_mode,
                                self._pending_old_text)
        self._reset_pending()
        if item is None:
            return
        self.blockSignals(True)
        try:
            if mode == "create":
                parent_item = item.parent()
                if parent_item is not None:
                    parent_item.removeChild(item)
                else:
                    self.takeTopLevelItem(self.indexOfTopLevelItem(item))
            else:
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setText(0, old_text)
        finally:
            self.blockSignals(False)

    def drop_target_node(self, item) -> TreeNode | None:
        """The folder a drop on this row means: a folder row takes the drop
        itself, a playlist row means "next to me" so its parent folder answers,
        and empty space (``item is None``) means the tree root.

        Reads the parent off the widget hierarchy rather than the library, so no
        QDropEvent is needed to test it -- constructing one segfaults this
        PySide6 build (see HANDOFF.md, "Development conventions")."""
        if item is None:
            return None
        node = item.data(0, _NODE_ROLE)
        if node is not None and node.is_folder():
            return node
        parent_item = item.parent()
        return parent_item.data(0, _NODE_ROLE) if parent_item is not None else None

    def set_node_text(self, node: TreeNode) -> None:
        """Write ``node.name`` back onto its row. A rename may be auto-suffixed,
        so what the editor left in the item is not necessarily the final name;
        apply_playlist_status re-adds the ∞ prefix on the repaint that follows."""
        item = self._by_nodeid.get(id(node))
        if item is not None:
            item.setText(0, node.name)

    def bind_new_node(self, node: TreeNode) -> None:
        """Attach a freshly created TreeNode to the placeholder, turning it into
        an ordinary row. No rebuild, so every open tab's node references (and
        the rest of this tree) stay valid."""
        item = self._pending_item
        self._reset_pending()
        self.blockSignals(True)
        try:
            if item is None:
                # Created without going through the in-place editor: build the
                # row now, so the caller never has to care which path it took.
                item = QTreeWidgetItem([node.name])
                parent_item = self._items_by_path().get(tuple(node.node_path)[:-1])
                if parent_item is None:
                    self.addTopLevelItem(item)
                else:
                    parent_item.addChild(item)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(0, _NODE_ROLE, node)
            item.setText(0, node.name)
            if node.playlist is not None:
                item.setIcon(0, status_icon("grey"))   # folders have no square
            self._by_nodeid[id(node)] = item
        finally:
            self.blockSignals(False)

    # -------------------- drag in --------------------
    # Three kinds of drop land here: TRACK_MIME (tracks from a table), URLs
    # (files from Explorer), and this tree's OWN rows being dragged to another
    # folder. Anything else -- notably a drag from the other program's tree --
    # is refused outright.
    def _is_internal(self, event) -> bool:
        mime = event.mimeData()
        return (event.source() is self and not mime.hasFormat(TRACK_MIME)
                and not mime.hasUrls())

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasFormat(TRACK_MIME) or mime.hasUrls() or self._is_internal(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        mime = event.mimeData()
        pos = event.position().toPoint()
        item = self.itemAt(pos)
        drag_scroll.note_drag_over(self)
        self._scroller.update(pos)
        if self._is_internal(event):
            # Highlight the FOLDER that would receive the move (a track drag
            # highlights playlists instead -- different target entirely).
            target = self.drop_target_node(item)
            if all(can_move_into(n, target) for n in self.selected_top_nodes()):
                event.acceptProposedAction()
                self._set_hover(self._by_nodeid.get(id(target)) if target else None)
            else:
                event.ignore()            # cursor shows the refusal
                self._set_hover(None)
            return
        if mime.hasFormat(TRACK_MIME) or mime.hasUrls():
            event.acceptProposedAction()
            # Highlight the playlist under the cursor so it's obvious which one
            # will receive the drop -- a transient background paint, NOT a
            # selection change (setCurrentItem would fire playlists_selected and
            # reload the shown playlist onto whatever the cursor passes over).
            if item is not None and item.data(0, _NODE_ROLE) is not None \
                    and item.data(0, _NODE_ROLE).playlist is not None:
                self._set_hover(item)
            else:
                self._set_hover(None)
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._set_hover(None)
        self._end_drag()
        super().dragLeaveEvent(event)

    def _end_drag(self):
        self._scroller.stop()
        drag_scroll.clear_drag_over(self)

    def _set_hover(self, item):
        if item is self._hover_item:
            return
        if self._hover_item is not None:
            self._hover_item.setBackground(0, QBrush())    # reset to default
        self._hover_item = item
        if item is not None:
            item.setBackground(0, self.palette().highlight())

    def dropEvent(self, event):
        internal = self._is_internal(event)
        self._set_hover(None)
        self._end_drag()
        mime = event.mimeData()
        if internal:
            return self._drop_internal(event)
        if not mime.hasFormat(TRACK_MIME) and not mime.hasUrls():
            # Never super(): QTreeWidget's default handler inserts a *copy* of
            # the dragged row into the widget -- a phantom that the Library and
            # the files on disk know nothing about, which then vanishes on the
            # next Refresh while nothing has actually moved.
            event.ignore()
            return
        item = self.itemAt(event.position().toPoint())
        node = item.data(0, _NODE_ROLE) if item is not None else None
        if node is None or node.playlist is None:
            return
        event.acceptProposedAction()
        if not mime.hasFormat(TRACK_MIME):        # Explorer file import
            paths = TrackTable._parse_urls(mime)
            if paths:
                self.files_dropped.emit(node, paths)
            return
        # Reuses TrackTable's own MIME parsing -- a tree never drags TRACK_MIME
        # itself, so this is never a self-drop.
        result = TrackTable._parse_drop(mime, is_self=False)
        if result is not None:
            keys, source = result
            self.tracks_dropped.emit(node, keys, source)

    def _drop_internal(self, event):
        """One of this tree's own rows was dropped on another: move it."""
        target = self.drop_target_node(self.itemAt(event.position().toPoint()))
        nodes = [n for n in self.selected_top_nodes() if can_move_into(n, target)]
        # IgnoreAction, not the proposed MoveAction: QAbstractItemView.startDrag
        # calls removeRows on the source model when exec() returns MoveAction,
        # which would tear the real row out from under the move we do ourselves.
        event.setDropAction(Qt.IgnoreAction)
        event.accept()
        if nodes:
            self.node_move_requested.emit(self.source, nodes, target)


class HiddenTree(QTreeWidget):
    """The Hidden panel: one group per program, holding its hidden playlist
    nodes. Accepts drops from the library trees to hide, and offers Unhide.

    Groups are created on demand from whatever :meth:`populate` is given, so a
    program that is added -- or switched off for the session -- needs no change
    here."""

    hide_requested = Signal()               # hide current library-tree selection
    unhide_requested = Signal(list)         # [(source, node_path)]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context)
        self._groups: dict[str, QTreeWidgetItem] = {}

    def _group(self, source: str) -> QTreeWidgetItem:
        if source not in self._groups:
            from ..adapters import display_name
            item = QTreeWidgetItem([f"{display_name(source)} Playlists"])
            self._groups[source] = item
            self.addTopLevelItem(item)
        return self._groups[source]

    def populate(self, nodes_by_source: dict):
        for source, nodes in nodes_by_source.items():
            grp = self._group(source)
            grp.takeChildren()
            for node in nodes:
                it = QTreeWidgetItem([node.path_str()])
                it.setData(0, _NODE_ROLE, node)
                it.setData(0, _SRC_ROLE, node.source)
                if node.playlist is not None:
                    it.setIcon(0, status_icon("grey"))
                grp.addChild(it)
            grp.setExpanded(True)

    def dragEnterEvent(self, event):
        event.acceptProposedAction()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        event.acceptProposedAction()
        self.hide_requested.emit()

    def _on_context(self, pos):
        items = self.selectedItems()
        payload = []
        for it in items:
            node = it.data(0, _NODE_ROLE)
            if node is not None:
                payload.append((node.source, node.node_path))
        if payload:
            from .context_menus import hidden_menu

            class _CB:
                unhide = staticmethod(lambda: self.unhide_requested.emit(payload))
            hidden_menu(self, _CB).exec(self.viewport().mapToGlobal(pos))


_QWIDGETSIZE_MAX = 16777215


class Section(QWidget):
    """A titled pane that can collapse to just its title bar.

    Hiding the inner widget is not enough on its own: inside a QSplitter the
    pane keeps every pixel it was given and simply shows empty space. So
    collapsing also clamps ``maximumHeight`` to the header (which stops the
    pane being dragged back open). *Where* the freed space goes is decided by
    :meth:`PlaylistPanel._relayout` off the ``toggled`` signal, not here --
    QSplitter's own redistribution spreads it across every pane and drifts the
    stack downward, which is the opposite of what we want.
    """

    toggled = Signal()
    lock_toggled = Signal()          # this section's lock button was clicked
    drag_swap_requested = Signal(float)   # total Y delta of a header drag, on release

    def __init__(self, title: str, widget: QWidget, parent=None,
                 logo_file: str = ""):
        super().__init__(parent)
        self._widget = widget
        self._collapsed = False
        self.locked = True
        self._drag_start_y: float | None = None
        self._logo_file = logo_file or ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(1)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(2)
        self.toggle_btn = QToolButton()
        self.toggle_btn.setAutoRaise(True)
        self.toggle_btn.setFixedSize(16, 16)
        self.toggle_btn.setArrowType(Qt.DownArrow)
        self.toggle_btn.setToolTip("Collapse this section")
        self.toggle_btn.clicked.connect(self.toggle)
        header.addWidget(self.toggle_btn)

        self.label = QLabel(title)
        self.label.setStyleSheet("font-weight: bold; padding: 2px;")

        # The program's logo, before its name. Sized to the label's text height
        # so it tracks the font rather than pinning the header to a fixed height.
        # Hidden has no program and correctly gets none.
        self.logo = QLabel()
        self.logo.setVisible(False)
        header.addWidget(self.logo)
        header.addWidget(self.label, 1)
        self._apply_logo()

        # Locks/unlocks drag-reordering of the sidebar sections themselves
        # (Hidden / VDJ Playlists / Traktor Playlists), not anything inside them.
        self.lock_btn = QToolButton()
        self.lock_btn.setAutoRaise(True)
        self.lock_btn.setFixedSize(16, 16)
        self.lock_btn.clicked.connect(self.lock_toggled)
        header.addWidget(self.lock_btn)

        lay.addLayout(header)
        lay.addWidget(widget)
        self._header_height = max(self.toggle_btn.height(),
                                  self.label.sizeHint().height()) + 8
        self.set_locked(True)

    def _apply_logo(self) -> None:
        """Load the icon, or leave the slot empty and invisible.

        A missing file is not an error -- the artwork is loose files in
        ``Images/``, and a section with no icon simply reads as it always did.
        """
        if not self._logo_file:
            return
        path = paths_mod.image_path(self._logo_file)
        if not os.path.exists(path):
            return
        source = QPixmap(path)
        if source.isNull():
            return
        side = max(12, self.label.sizeHint().height() - 4)
        self.logo.setPixmap(source.scaled(side, side, Qt.KeepAspectRatio,
                                          Qt.SmoothTransformation))
        self.logo.setVisible(True)

    @property
    def header_height(self) -> int:
        return self._header_height

    def is_collapsed(self) -> bool:
        return self._collapsed

    def toggle(self):
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed: bool):
        collapsed = bool(collapsed)
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self._widget.setVisible(not collapsed)
        self.setMaximumHeight(self._header_height if collapsed else _QWIDGETSIZE_MAX)
        self.toggle_btn.setArrowType(Qt.RightArrow if collapsed else Qt.DownArrow)
        self.toggle_btn.setToolTip(
            "Expand this section" if collapsed else "Collapse this section")
        self.toggled.emit()

    # ---- section-reorder lock (shared across all sections; see PlaylistPanel) ----
    def set_locked(self, locked: bool):
        self.locked = bool(locked)
        self.lock_btn.setText("🔒" if self.locked else "🔓")
        self.lock_btn.setToolTip(
            "Locked — click to allow dragging sections to reorder them" if self.locked
            else "Unlocked — drag this title bar up/down to reorder sections")

    # ---- drag-to-swap: press+release on the header while unlocked ----
    def mousePressEvent(self, event):
        if (not self.locked and event.button() == Qt.LeftButton
                and event.position().y() <= self._header_height):
            self._drag_start_y = event.globalPosition().y()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_start_y is not None:
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_start_y is not None:
            delta = event.globalPosition().y() - self._drag_start_y
            self._drag_start_y = None
            self.drag_swap_requested.emit(delta)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _RelayoutSplitter(QSplitter):
    """A splitter that tells its owner once a resize has actually been applied.

    The hook cannot live on the parent widget's ``resizeEvent`` or in an event
    filter: both run before ``QSplitter::resizeEvent`` has redistributed, so
    ``sizes()`` there still reports the old layout and any correction is computed
    against a stale total.
    """

    def __init__(self, orientation, on_resized):
        super().__init__(orientation)
        self._on_resized = on_resized

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._on_resized()


class PlaylistPanel(QWidget):
    playlists_selected = Signal(list, str)
    node_context_requested = Signal(object, object)
    blank_context_requested = Signal(str, object)
    new_playlist_committed = Signal(str, object, str)
    rename_committed = Signal(str, object, str)
    node_move_requested = Signal(str, list, object)
    hide_requested = Signal()
    unhide_requested = Signal(list)
    tracks_dropped = Signal(object, list, str)   # target TreeNode, keys, drag source
    files_dropped = Signal(object, list)         # target TreeNode, external file paths

    def __init__(self, adapters=None, parent=None):
        """``adapters`` is ``{id: LibraryAdapter}`` -- one section and one tree
        is built per program, in registration order. Defaults to every known
        program so a bare ``PlaylistPanel()`` still works in tests."""
        super().__init__(parent)
        if adapters is None:
            from ..adapters import build_adapters
            adapters = build_adapters(None)
        self.hidden_tree = HiddenTree()
        self.trees: dict[str, LibraryTree] = {
            aid: LibraryTree(aid) for aid in adapters
        }

        for t in self.trees.values():
            t.playlists_selected.connect(self.playlists_selected)
            t.node_context_requested.connect(self.node_context_requested)
            t.blank_context_requested.connect(self.blank_context_requested)
            t.new_playlist_committed.connect(self.new_playlist_committed)
            t.rename_committed.connect(self.rename_committed)
            t.node_move_requested.connect(self.node_move_requested)
            t.tracks_dropped.connect(self.tracks_dropped)
            t.files_dropped.connect(self.files_dropped)
        self.hidden_tree.hide_requested.connect(self.hide_requested)
        self.hidden_tree.unhide_requested.connect(self.unhide_requested)

        # Section keys are "hidden" plus each adapter id, so the persisted
        # section order / collapsed set from older sessions still resolves.
        self.sections = {"hidden": Section("Hidden", self.hidden_tree)}
        for aid, adapter in adapters.items():
            self.sections[aid] = Section(f"{adapter.display_name} Playlists",
                                         self.trees[aid],
                                         logo_file=adapter.logo_file)
        self.splitter = _RelayoutSplitter(Qt.Vertical, self._on_splitter_resized)
        self._order = list(self.sections)          # top-to-bottom
        for section in self.sections.values():
            self.splitter.addWidget(section)
        # A trailing filler pane soaks up leftover height. It is 0 whenever any
        # section is expanded, but when all collapse it holds the surplus so the
        # title bars stack at the top instead of the splitter centring them (Qt
        # spreads unassigned space across the panes otherwise).
        self._filler = QWidget()
        self.splitter.addWidget(self._filler)
        self.splitter.setStretchFactor(len(self._order), 1)
        self.splitter.setCollapsible(len(self._order), True)
        self.splitter.setSizes([120] + [320] * len(self.trees) + [0])
        filler_handle = self.splitter.handle(self.splitter.count() - 1)
        if filler_handle is not None:              # not user-draggable
            filler_handle.setEnabled(False)
        # Remembered expanded height per section, so collapse/expand can place
        # the freed space deliberately instead of letting the splitter spread
        # it across every pane.
        self._weights = {"hidden": 120}
        self._weights.update({aid: 320 for aid in self.trees})
        self._weights_seeded = False
        self.splitter.splitterMoved.connect(self._remember_weights)
        self._sections_locked = True
        for key, section in self.sections.items():
            section.toggled.connect(self._relayout)
            section.set_locked(self._sections_locked)
            section.lock_toggled.connect(
                lambda k=key: self.set_sections_locked(not self._sections_locked))
            section.drag_swap_requested.connect(
                lambda delta, k=key: self._on_section_dragged(k, delta))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.splitter)
        self._ready = True

    def _on_splitter_resized(self):
        """The pane got taller or shorter; re-apply our own rule for where the
        difference goes. Qt's is stretch-factor based, which hands it all to the
        filler and leaves a dead strip under the sections."""
        if getattr(self, "_ready", False):
            self._relayout()

    # ---- which programs this session uses --------------------------------
    def set_enabled_programs(self, enabled) -> None:
        """Show only the chosen programs. A program switched off simply is not
        in the pane; its tree object is kept (already emptied by ``load``) so
        turning it back on needs no rebuild and no re-wiring of its signals.

        Everything downstream asks ``isHidden()``, never ``isVisible()``: this
        runs from ``MainWindow.__init__``, before the window is shown, and until
        then *every* child reads as not-visible. Taking that at face value would
        make ``_relayout`` treat all programs as switched off and hand the whole
        pane to the filler -- which is exactly the layout the session restore
        then saves. ``isHidden()`` means explicitly hidden and ignores ancestors.
        """
        wanted = set(enabled)
        for aid in self.trees:
            self.sections[aid].setVisible(aid in wanted)
        self._relayout()

    def enabled_programs(self) -> list[str]:
        return [aid for aid in self.trees if not self.sections[aid].isHidden()]

    # ---- section reordering: one shared lock, drag-the-header-to-swap ----
    def sections_locked(self) -> bool:
        return self._sections_locked

    def set_sections_locked(self, locked: bool):
        self._sections_locked = bool(locked)
        for section in self.sections.values():
            section.set_locked(self._sections_locked)

    def section_order(self) -> list[str]:
        return list(self._order)

    def set_section_order(self, order: list[str]):
        wanted = [k for k in order if k in self.sections]
        wanted += [k for k in self._order if k not in wanted]   # keep any missing
        for i, key in enumerate(wanted):
            self.splitter.insertWidget(i, self.sections[key])
        self._order = wanted
        self._relayout()

    def _on_section_dragged(self, key: str, delta: float):
        threshold = 20   # px -- resolved on release, not live during the drag
        idx = self._order.index(key)
        if delta > threshold and idx < len(self._order) - 1:
            self._swap(idx, idx + 1)
        elif delta < -threshold and idx > 0:
            self._swap(idx - 1, idx)

    def _swap(self, i: int, j: int):
        self._order[i], self._order[j] = self._order[j], self._order[i]
        self.splitter.insertWidget(i, self.sections[self._order[i]])
        self.splitter.insertWidget(j, self.sections[self._order[j]])
        self._relayout()

    # ---- collapse magnet: anchor the top, bottom-most expanded absorbs ----
    def _sane(self, sizes) -> bool:
        floor = sum(s.header_height for s in self.sections.values())
        return sum(sizes) > floor

    def restore_sizes(self, sizes) -> None:
        """Apply saved section heights **and adopt them as the weights**.

        Just calling ``splitter.setSizes`` is not enough: any later
        ``_relayout()`` -- and ``set_collapsed_sections`` / ``set_section_order``
        both trigger one -- rebuilds every pane from ``self._weights``. During a
        session restore the seeding guard in ``_relayout`` can't help, because
        the window has not been shown yet so ``_sane()`` is false and the
        constructor defaults (120/320/320) win. Seeding them here is what makes
        the restored layout survive.

        The weights come from the list we were handed, deliberately, and not
        from reading the splitter back after applying it: this runs before the
        window is shown, so the splitter is still at its pre-show height and
        would scale every size down to fit it. Adopting *that* would bake the
        shrunken layout in permanently. The requested sizes are what the user
        actually left the pane at; ``_relayout`` reconciles them with the real
        height once the resize on show comes through.
        """
        if not sizes:
            return
        for key, size in zip(self._order, sizes):
            if not self.sections[key].is_collapsed() and size > 0:
                self._weights[key] = size
        self._weights_seeded = True
        self.splitter.setSizes(list(sizes))
        # Normalise now as well, so the relayout that set_collapsed_sections /
        # set_section_order fire during restore is a no-op rather than a jump.
        self._relayout()

    def _remember_weights(self, *_):
        """A manual drag updates the remembered height of each expanded
        section, so a later collapse/expand keeps the sizes the user chose."""
        sizes = self.splitter.sizes()
        if not self._sane(sizes):
            return
        for key, size in zip(self._order, sizes):
            section = self.sections[key]
            if not section.isHidden() and not section.is_collapsed():
                self._weights[key] = size

    def _relayout(self):
        """Recompute pane sizes so collapsing never moves the sections above
        the change: collapsed panes take their header height, expanded panes
        keep their remembered weight, and the whole leftover is dropped on the
        bottom-most expanded section -- which is what makes the section below a
        collapse rise to meet it while Hidden stays put.

        Weights are seeded once, from the pane sizes at the first toggle: a
        maximumHeight clamp does not take effect until the next event loop, so
        at that moment the sizes still read the true expanded heights (verified).
        They are never re-read from a collapsed layout afterwards, or the
        absorbing section's temporary growth would poison the weights and break
        the expand round-trip -- only a real user drag updates them.
        """
        sizes = self.splitter.sizes()
        total = sum(sizes)
        # Only once the pane is on screen: before that Qt reports placeholder
        # heights (76 px each, below a section's 91 px minimum), and adopting
        # them left every section but the last clamped to its minimum.
        if not self._weights_seeded and self.isVisible() and self._sane(sizes):
            for key, size in zip(self._order, sizes):
                self._weights[key] = size
            self._weights_seeded = True

        out = []
        expanded_idx = []
        for i, key in enumerate(self._order):
            section = self.sections[key]
            if section.isHidden():
                out.append(0)            # a program switched off for the session
            elif section.is_collapsed():
                out.append(section.header_height)
            else:
                out.append(self._weights.get(key, section.header_height))
                expanded_idx.append(i)
        if expanded_idx:
            # Sections collapsed BELOW the last expanded one collapse "into
            # themselves": their header stays put and the space they gave up
            # stays under them, rather than the section above growing and
            # dragging their title bar down to the bottom edge. Only that much is
            # withheld, so genuinely new height -- a taller window -- still goes
            # to the bottom-most expanded section as usual.
            bottom = expanded_idx[-1]
            reserved = 0
            for key in self._order[bottom + 1:]:
                section = self.sections[key]
                if section.isHidden() or not section.is_collapsed():
                    continue
                reserved += max(0, self._weights.get(key, section.header_height)
                                - section.header_height)
            leftover = total - sum(out) - reserved
            if leftover < 0:
                # The remembered heights do not fit (a fresh layout with many
                # programs on, or a shorter window): shrink the expanded
                # sections in proportion. Taking the whole shortfall from the
                # bottom one squeezed it to nothing, and Qt then clamped every
                # section to its minimum -- which is what got remembered.
                fixed = sum(out) - sum(out[i] for i in expanded_idx)
                weights = sum(out[i] for i in expanded_idx)
                scale = max(total - reserved - fixed, 0) / weights if weights else 0
                for i in expanded_idx:
                    out[i] = max(self.sections[self._order[i]].header_height,
                                 int(out[i] * scale))
                leftover = total - sum(out) - reserved
            out[bottom] = max(self.sections[self._order[bottom]].header_height,
                              out[bottom] + leftover)
            filler = max(0, total - sum(out))
        else:
            # Nothing to absorb it into: the filler takes the surplus so the
            # collapsed title bars stack at the top.
            filler = max(0, total - sum(out))
        out.append(filler)
        self.splitter.setSizes(out)

    def load(self, libraries: dict, hidden: dict, yield_fn=None):
        """``libraries`` and ``hidden`` are both keyed by adapter id. A program
        with no library yet is simply skipped."""
        for source, tree in self.trees.items():
            lib = libraries.get(source)
            root = lib.root if lib is not None else None
            tree.load(root, hidden.get(source, set()), yield_fn)
        self.hidden_tree.populate(
            {source: tree.hidden_nodes for source, tree in self.trees.items()})

    def active_tree(self):
        """Whichever library tree currently has a selection, in display order."""
        for tree in self.trees.values():
            if tree.selectedItems():
                return tree
        return None

    def tree_for(self, source: str) -> LibraryTree:
        return self.trees[source]

    # ---- view snapshot across a reload ----
    def snapshot_view(self) -> dict:
        return {
            source: {"expanded": tree.expanded_paths(),
                     "selected": tree.selected_paths()}
            for source, tree in self.trees.items()
        }

    def restore_view(self, snap: dict) -> None:
        for source, tree in self.trees.items():
            data = snap.get(source)
            if not data:
                continue
            tree.apply_expanded_paths(data.get("expanded", set()))
            tree.select_paths(data.get("selected", set()))

    # ---- collapsed state, for session persistence ----
    def collapsed_sections(self) -> list[str]:
        return sorted(k for k, s in self.sections.items() if s.is_collapsed())

    def set_collapsed_sections(self, names) -> None:
        wanted = set(names or ())
        for key, section in self.sections.items():
            section.set_collapsed(key in wanted)

    def clear_other_selection(self, keep_source: str):
        """Only one program's tree holds a selection at a time -- clearing the
        others silently, since a signal here would reload the tab that just took
        focus."""
        for source, tree in self.trees.items():
            if source == keep_source:
                continue
            tree.blockSignals(True)
            tree.clearSelection()
            tree.blockSignals(False)
