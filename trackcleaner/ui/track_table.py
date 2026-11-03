"""The track display table.

A :class:`QTableView` backed by a :class:`QStandardItemModel` through a custom
sort proxy. Provides everything the spec asks of the track area:

- movable columns (drag headers to reorder) + interactive resize,
- click-to-sort that toggles ascending/descending, numeric columns compared
  numerically and text columns alphabetically,
- Alt + mouse-wheel scales the table font up/down,
- a Status column of coloured squares, an Order column (playlist position),
- right-click the header for a tick menu to hide/show fields,
- horizontal (bottom) + vertical scrollbars always available.
"""

from __future__ import annotations

import os

from PySide6.QtCore import (QItemSelection, QItemSelectionModel, QMimeData,
                            QPersistentModelIndex, QSortFilterProxyModel, Qt,
                            QTimer, QUrl, Signal)
from PySide6.QtGui import (QBrush, QColor, QDrag, QPainter, QPalette,
                           QStandardItem, QStandardItemModel)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFrame,
                               QHeaderView, QMenu, QTableView)

from ..adapters import registry
from ..io.probe import AUDIO_EXTS as _AUDIO_EXTS
from ..model import matching
from ..model.library import Track
from . import drag_scroll
from . import watermark
from .fields import ALL_COLUMN_IDS, FIELD_BY_ID, LABEL_BY_ID, ORDER_COL, STATUS_COL
from .status_squares import TOMBSTONE_STATUSES, status_icon
from . import theme as theme_mod

_SORT_ROLE = Qt.UserRole + 1
_KEY_ROLE = Qt.UserRole + 2

# MIME type for dragging tracks within the app. The payload is the selected
# track keys, newline-joined; the source program travels alongside so a drop
# target can tell same-source from cross-source.
TRACK_MIME = "application/x-slipstream-tracks"
SOURCE_MIME = "application/x-slipstream-source"

#: Columns a user may edit in place. Deliberately the same set the adapters
#: declare as EDITABLE_FIELDS -- the column ids and the Track field names are the
#: same words, so there is nothing to map and nothing to keep in step.
EDITABLE_COLUMNS = ("title", "artist", "album", "genre", "comment")


class _SortProxy(QSortFilterProxyModel):
    """Numeric-aware sort: numbers compared as numbers, everything else as
    case-insensitive text."""

    def lessThan(self, left, right):
        lv = self.sourceModel().data(left, _SORT_ROLE)
        rv = self.sourceModel().data(right, _SORT_ROLE)
        if isinstance(lv, (int, float)) and isinstance(rv, (int, float)):
            return lv < rv
        return str(lv).lower() < str(rv).lower()


class TrackTable(QTableView):
    track_clicked = Signal(str)          # track key -- loads the waveform
    track_double_clicked = Signal(str)   # track key -- loads and plays
    track_context_requested = Signal(str, int, object)   # key, visual row, global pos
    # One cell edited in place: track key, field id, new text.
    metadata_edited = Signal(str, str, str)
    focused = Signal()                   # this table became the focused one
    column_toggled = Signal(str, bool)   # col_id, visible -- from the header menu
    tracks_dropped = Signal(list, str, int)  # dropped keys, source, insert index (-1=append)
    tracks_reordered = Signal(list)      # new full track-key order (self-drag reorder)
    files_dropped = Signal(list, int)    # external file paths dropped from Explorer, insert index
    font_changed = Signal(int)           # Alt-wheel -- applied globally to every table

    def __init__(self, parent=None):
        super().__init__(parent)
        self._font_pt = 9
        self._column_ids = list(ALL_COLUMN_IDS)
        self._hidden: set[str] = set()
        # Context of what this table shows -- set by the pane so main_window can
        # resolve auto-advance / drop targets. Rows themselves live in the model.
        self.source: str = ""
        self.nodes: list = []
        # Current search filter; a filtered view can't map a visual row to a
        # stored playlist position, so positional insert is disabled while set.
        self._filter_text: str = ""
        # Faint program logo behind the rows. The file is named by the adapter
        # (LibraryAdapter.logo_file) and pushed in by the pane, so the table
        # still knows nothing about which programs exist.
        self._logo_file: str = ""
        self._watermark_opacity: int = watermark.DEFAULT_OPACITY
        # Rows selected when the mouse went down -- see should_edit_on_click.
        self._was_selected: set = set()
        # A click that may yet turn out to be half of a double-click. None means
        # "whatever Qt is configured to use"; the window overrides it.
        self._double_click_ms: int | None = None
        self._pending_edit = None
        self._skip_next_click_edit = False
        self._edit_timer = QTimer(self)
        self._edit_timer.setSingleShot(True)
        self._edit_timer.timeout.connect(self._open_pending_edit)

        self._model = QStandardItemModel(self)
        self._proxy = _SortProxy(self)
        self._proxy.setSortRole(_SORT_ROLE)
        self._proxy.setFilterKeyColumn(-1)   # match against every column
        self._proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self._proxy.setSourceModel(self._model)
        self.setModel(self._proxy)

        # Header clicks sort, but nothing is sorted until one happens: the
        # default view is the playlist's own stored order. setSortingEnabled
        # leaves the proxy sorting on column 0 DESCENDING, which quietly ordered
        # every table by status -- putting the "green2" preview rows above the
        # real "green" members, which is what made not-yet-added tracks appear at
        # the top after a refresh.
        self.setSortingEnabled(True)
        self._user_sorted = False
        self.horizontalHeader().sortIndicatorChanged.connect(self._on_sort_indicator)
        self._proxy.sort(-1)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # Editing happens ONLY through the deferred click path below. Qt's
        # defaults (DoubleClicked | EditKeyPressed | AnyKeyPressed) were a second,
        # independent way in: a double-click opened an editor from inside Qt's own
        # press handling, so it both played the track and started an edit however
        # carefully our own path stood down. The explicit edit(index) call is a
        # slot and ignores this setting, so the deliberate path still works.
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # Off: Qt's auto-scroll re-centres the horizontal scrollbar on the
        # clicked cell, which is jarring in a wide metadata table.
        self.setAutoScroll(False)
        self.verticalHeader().setVisible(False)

        hh = self.horizontalHeader()
        hh.setSectionsMovable(True)
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        # resizeColumnsToContents() (called on every set_rows) otherwise scans up
        # to 1000 rows -- a ~150ms block for a big playlist that would stutter
        # audio mid-reload. Sample a small, fixed number of rows instead.
        hh.setResizeContentsPrecision(24)
        hh.setContextMenuPolicy(Qt.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._show_header_menu)

        # Item drag (not rubber-band highlight): a press-and-move on selected
        # rows starts a drag carrying the track keys. Multi-select still works
        # via click / Ctrl / Shift (ExtendedSelection, above).
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDropIndicatorShown(True)

        # `clicked` rather than the selection model, which set_rows resets.
        self.clicked.connect(self._on_click)
        self.doubleClicked.connect(self._on_double)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context)

        self._track_by_key: dict[str, Track] = {}
        self._was_active = True
        # set_rows rebuilds every item; without this the rebuild would look like
        # a page of user edits and queue a write per cell.
        self._populating = False
        self._model.itemChanged.connect(self._on_item_changed)

        # A thin accent line drawn during a drag to show where a positional
        # insert will land (only when the view is in the playlist's native order).
        self._drop_line = QFrame(self.viewport())
        self._drop_line.setFrameShape(QFrame.HLine)
        self._drop_line.setStyleSheet("background:#3a8fd8; border:none;")
        self._drop_line.setFixedHeight(2)
        self._drop_line.hide()
        # Hovering a drag over the top/bottom row scrolls, accelerating. Not
        # Qt's setAutoScroll (off above, and it never accelerates).
        self._scroller = drag_scroll.DragAutoScroller(self)
        # A tab restored from the last session but not yet shown: (source,
        # node_paths) it will be filled from the first time it is focused.
        self.pending_restore: tuple | None = None
        # Whether this pane is the active drop/selection target (see
        # set_pane_active). A lone, unsplit pane always is.
        self.pane_active: bool = True

        self._apply_font()
        self._set_headers()

    # -------------------- population --------------------
    def set_rows(self, rows: list[tuple]):
        """rows: list of ``(track, status_color)``. A ``(None, label)`` item is a
        **separator row** (used by collection search to divide groups) -- rendered
        disabled, non-selectable and spanned, and key-less so it's inert to
        selection/drag. Order column = enumeration of the real rows."""
        # Every row is about to be replaced, so a click still waiting to become
        # an edit names a cell that will not be there.
        self.cancel_pending_edit()
        # Kept so a drag out can offer the real files: the model stores only the
        # displayed strings, and re-deriving a path from the library would mean
        # the table and the drag could disagree about which track a row is.
        self._track_by_key = {t.key: t for t, _s in rows if t is not None}
        has_sep = any(track is None for track, _ in rows)
        # A separator view relies on insertion order (grouped results); a live
        # column sort would scatter the groups, so suppress sorting while one is
        # shown and restore it (only if we were the ones who turned it off).
        if has_sep and self.isSortingEnabled():
            self.setSortingEnabled(False)
            self._sorting_suppressed = True
        elif not has_sep and getattr(self, "_sorting_suppressed", False):
            self.setSortingEnabled(True)
            self._sorting_suppressed = False

        # One reset for the whole rebuild, so the proxy and the view re-read once
        # instead of per row. Deliberately NOT model.clear() in here: clear()
        # performs its own begin/endResetModel, which nested two resets -- four Qt
        # warnings on every playlist switch, and clear()'s inner end closed our
        # outer begin, leaving the view's reset bookkeeping unbalanced.
        # removeRows is enough because _set_headers re-establishes the columns
        # itself; that was the only other thing clear() did for us.
        self._populating = True
        self._model.beginResetModel()
        self._model.removeRows(0, self._model.rowCount())
        self._set_headers()
        sep_rows = []
        order = 0
        for track, status in rows:
            if track is None:
                sep_rows.append(self._model.rowCount())
                self._model.appendRow(self._make_separator(status))
                continue
            order += 1
            self._model.appendRow(
                [self._make_item(col_id, track, order, status) for col_id in ALL_COLUMN_IDS])
        self._model.endResetModel()
        self._populating = False
        if not self._user_sorted:
            # The reset re-sorts on whatever column the proxy last held; put it
            # back to source order so an untouched view stays in playlist order.
            self._proxy.sort(-1)
        self._apply_hidden()
        self.resizeColumnsToContents()
        self.clearSpans()
        for r in sep_rows:
            self.setSpan(r, 0, 1, self._model.columnCount())

    def _make_separator(self, label: str) -> list[QStandardItem]:
        grey = QBrush(QColor(theme_mod.TOMBSTONE_FG[theme_mod.current()]))
        items = []
        for c, _col_id in enumerate(ALL_COLUMN_IDS):
            it = QStandardItem(label if c == 0 else "")
            it.setEditable(False)
            it.setFlags(Qt.ItemIsEnabled)      # visible, but not selectable/keyed
            it.setForeground(grey)
            items.append(it)
        return items

    def _make_item(self, col_id: str, track: Track, order: int, status: str) -> QStandardItem:
        it = QStandardItem()
        # Editable only where a tag actually lives, and never on a sync-preview
        # row: that track is not a member of this playlist, so there is nothing
        # here to edit.
        it.setEditable(col_id in EDITABLE_COLUMNS
                       and status not in TOMBSTONE_STATUSES)
        it.setData(track.key, _KEY_ROLE)
        if col_id == STATUS_COL:
            it.setIcon(status_icon(status))
            it.setData(status, _SORT_ROLE)
        elif col_id == ORDER_COL:
            it.setText(str(order))
            it.setData(order, _SORT_ROLE)
        else:
            field = FIELD_BY_ID[col_id]
            it.setText(str(field.getter(track)))
            if field.sort_getter is not None:
                it.setData(field.sort_getter(track), _SORT_ROLE)
            else:
                it.setData(str(field.getter(track)).lower(), _SORT_ROLE)
        if status in TOMBSTONE_STATUSES:
            # A sync-preview marker on the un-edited copy: grey = a track this
            # playlist will lose on the next sync; green2 = one it will gain.
            it.setForeground(QBrush(QColor(theme_mod.TOMBSTONE_FG[theme_mod.current()])))
            it.setToolTip(
                "Pending removal — the other program dropped this track; a sync "
                "will remove it from this playlist too." if status == "grey" else
                "Pending addition — the other program added this track; a sync "
                "will add it to this playlist.")
        return it

    def _set_headers(self):
        self._model.setColumnCount(len(ALL_COLUMN_IDS))
        for c, col_id in enumerate(ALL_COLUMN_IDS):
            self._model.setHeaderData(c, Qt.Horizontal, LABEL_BY_ID[col_id])

    # -------------------- column hide/show --------------------
    def set_hidden_fields(self, hidden: set[str]):
        self._hidden = set(hidden)
        self._apply_hidden()

    def hidden_fields(self) -> set[str]:
        return set(self._hidden)

    def set_filter(self, text: str):
        self._filter_text = text or ""
        self._proxy.setFilterFixedString(self._filter_text)

    def _apply_hidden(self):
        for c, col_id in enumerate(ALL_COLUMN_IDS):
            self.setColumnHidden(c, col_id in self._hidden)

    def toggle_field(self, col_id: str):
        if col_id in self._hidden:
            self._hidden.discard(col_id)
        else:
            self._hidden.add(col_id)
        self._apply_hidden()

    def _show_header_menu(self, pos):
        menu = QMenu(self)
        for col_id in ALL_COLUMN_IDS:
            if col_id == STATUS_COL:
                continue   # status is always shown
            act = menu.addAction(LABEL_BY_ID[col_id])
            act.setCheckable(True)
            visible = col_id not in self._hidden
            act.setChecked(visible)
            # Emit so main_window applies it globally (all tables + View menu),
            # keeping the header menu, View → Columns and every table in sync.
            act.triggered.connect(
                lambda _=False, cid=col_id, vis=visible: self.column_toggled.emit(cid, not vis))
        menu.exec(self.horizontalHeader().mapToGlobal(pos))

    # -------------------- context / ordering accessors --------------------
    def set_context(self, source: str, nodes: list):
        """Record which playlist(s)/source these rows came from."""
        self.source = source
        self.nodes = list(nodes)
        # The watermark follows the source from here rather than from each of
        # the six callers, so a new one cannot forget to update it.
        self.set_watermark(registry.logo_file(source))

    def visual_keys(self) -> list[str]:
        """Track keys in the table's current on-screen order (sort + filter
        applied) -- what 'sort order' auto-advance follows."""
        out = []
        for r in range(self._proxy.rowCount()):
            idx = self._proxy.index(r, 0)
            key = self._row_key(idx)
            if key:
                out.append(key)
        return out

    def selected_keys(self) -> list[str]:
        """Selected track keys, in the table's visual order."""
        rows = sorted({i.row() for i in self.selectionModel().selectedRows()})
        return [self._row_key(self._proxy.index(r, 0)) for r in rows]

    def rowCount(self) -> int:
        """Rows currently on screen (sort + filter applied)."""
        return self._proxy.rowCount()

    def select_visual_row(self, row: int) -> None:
        """Select ``row`` and scroll it into view -- what Up/Down do."""
        if not (0 <= row < self._proxy.rowCount()):
            return
        index = self._proxy.index(row, 0)
        self.setCurrentIndex(index)
        self.selectRow(row)
        self.scrollTo(index)

    def select_keys(self, keys) -> int:
        """Reselect these track keys, returning how many were found.

        **By key, not by row index.** A refresh or a sync reorders rows and
        consumes preview rows, so the row a track was on means something else
        afterwards; a key that has gone is simply dropped rather than
        approximated onto whatever took its place.
        """
        wanted = set(keys or ())
        if not wanted:
            return 0
        model = self.selectionModel()
        if model is None:
            return 0
        selection = QItemSelection()
        last = None
        for row in range(self._proxy.rowCount()):
            if self.visual_key_at(row) in wanted:
                index = self._proxy.index(row, 0)
                selection.select(index, index)
                last = index
        if last is None:
            return 0
        model.select(selection, QItemSelectionModel.ClearAndSelect
                     | QItemSelectionModel.Rows)
        # Current index too: it is what the keyboard steps from, and a selection
        # with no current row leaves Up/Down starting from the top again.
        model.setCurrentIndex(last, QItemSelectionModel.NoUpdate)
        return len(selection.indexes())

    def selected_visual_rows(self) -> list[int]:
        """Selected proxy row indices, ascending -- the positional handle a
        duplicate-bearing playlist needs (a key can occur at several rows)."""
        return sorted({i.row() for i in self.selectionModel().selectedRows()})

    def visual_key_at(self, row: int) -> str:
        """Track key at proxy ``row`` (visual order)."""
        return self._row_key(self._proxy.index(row, 0))

    # -------------------- drag out --------------------
    def startDrag(self, actions):
        keys = self.selected_keys()
        if not keys:
            return
        mime = QMimeData()
        mime.setData(TRACK_MIME, "\n".join(keys).encode("utf-8"))
        mime.setData(SOURCE_MIME, (self.source or "").encode("utf-8"))
        # Also offer the files themselves, so a drag into any other application
        # -- a player, Explorer, anything -- is accepted instead of showing the
        # "no entry" cursor. Purely additive: every drop handler in this app
        # tests TRACK_MIME *first*, so an internal drag still takes the internal
        # path and never degrades into a file import.
        urls = self._selected_urls(keys)
        if urls:
            mime.setUrls(urls)
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction | Qt.MoveAction, Qt.CopyAction)

    def _selected_urls(self, keys: list[str]) -> list:
        """``file://`` URLs for the dragged tracks, skipping any whose file is
        not on disk -- offering a path that does not resolve makes the receiving
        application report the error, which looks like our bug."""
        urls = []
        for key in keys:
            track = self._track_by_key.get(key)
            if track is None or not track.path:
                continue
            local = matching.resolve_local_path(track.path)
            if local and os.path.exists(local):
                urls.append(QUrl.fromLocalFile(local))
        return urls

    # -------------------- drag in (drop-to-add) --------------------
    # A self-drag (dragging a table's own rows back onto itself) is simply
    # ignored -- there is no row-reorder feature (that was a misreading of the
    # original request; the lock/reorder gesture is for the sidebar sections,
    # see playlist_trees.Section). Only drops that arrive from elsewhere are
    # meaningful here, and main_window resolves same- vs cross-program from the
    # carried source.
    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasFormat(TRACK_MIME) or mime.hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        mime = event.mimeData()
        pos = event.position().toPoint()
        drag_scroll.note_drag_over(self)
        self._scroller.update(pos)
        if mime.hasFormat(TRACK_MIME) or mime.hasUrls():
            # Files from Explorer land at the drop position exactly like an
            # internal drag does (dropEvent already computes _drop_index for
            # them), so they get the same indicator. Without it the insert point
            # was real but invisible, which read as "it just appends".
            event.acceptProposedAction()
            self._show_drop_line(pos)
        else:
            super().dragMoveEvent(event)

    def dragLeaveEvent(self, event):
        self._drop_line.hide()
        self._end_drag()
        super().dragLeaveEvent(event)

    def _end_drag(self):
        self._scroller.stop()
        drag_scroll.clear_drag_over(self)

    def dropEvent(self, event):
        self._drop_line.hide()
        self._end_drag()
        mime = event.mimeData()
        if not mime.hasFormat(TRACK_MIME):
            paths = self._parse_urls(mime)          # Explorer file import
            if paths:
                event.acceptProposedAction()
                self.files_dropped.emit(paths, self._drop_index(event.position().toPoint()))
                return
            return super().dropEvent(event)
        event.acceptProposedAction()
        pos = event.position().toPoint()
        if event.source() is self:
            # Reorder within this same playlist. Only well-defined in native
            # order (a visual row maps straight to a stored position); a sorted
            # or multi-playlist view can't be reordered, so the drop is dropped.
            if self._in_native_order():
                tk = list(self.nodes[0].playlist.track_keys)
                new = self._reorder_keys(tk, self.selected_visual_rows(),
                                         self._drop_index(pos))
                if new != tk:
                    self.tracks_reordered.emit(new)
            return
        parsed = self._parse_drop(mime, is_self=False)
        if parsed is None:
            return
        keys, source = parsed
        self.tracks_dropped.emit(keys, source, self._drop_index(pos))

    @staticmethod
    def _reorder_keys(track_keys: list, sel_rows: list, insert_at: int) -> list:
        """Move the tracks at ``sel_rows`` (positions into ``track_keys``) so they
        sit, in their original relative order, at ``insert_at`` -- the standard
        multi-row move, adjusting the target for rows pulled out ahead of it."""
        sel = [r for r in sel_rows if 0 <= r < len(track_keys)]
        if not sel:
            return list(track_keys)
        sel_set = set(sel)
        moved = [track_keys[r] for r in sel]
        remaining = [k for i, k in enumerate(track_keys) if i not in sel_set]
        insert_at = max(0, min(insert_at, len(track_keys)))
        pos = insert_at - sum(1 for r in sel if r < insert_at)
        pos = max(0, min(pos, len(remaining)))
        return remaining[:pos] + moved + remaining[pos:]

    @staticmethod
    def _parse_urls(mime: QMimeData) -> list[str]:
        """Local audio-file paths from a ``text/uri-list`` (Explorer) drop.
        Pulled out (like ``_parse_drop``) so it is unit-testable against a real
        ``QMimeData`` without a segfault-prone synthetic ``QDropEvent``."""
        if not mime.hasUrls():
            return []
        out = []
        for url in mime.urls():
            p = url.toLocalFile()
            if p and os.path.splitext(p)[1].lower() in _AUDIO_EXTS:
                out.append(p)
        return out

    @staticmethod
    def _parse_drop(mime: QMimeData, is_self: bool):
        """The MIME-decoding logic ``dropEvent`` uses, pulled out so it can be
        unit-tested directly against a real ``QMimeData`` -- constructing a
        ``QDropEvent`` by hand to drive this is not viable, it segfaults this
        PySide6 build outright. Returns ``(keys, source)`` or ``None``."""
        if is_self:
            return None                  # nothing to reorder onto itself
        keys = [k for k in bytes(mime.data(TRACK_MIME)).decode("utf-8").splitlines() if k]
        if not keys:
            return None
        source = bytes(mime.data(SOURCE_MIME)).decode("utf-8") if mime.hasFormat(SOURCE_MIME) else ""
        return keys, source

    # -------------------- positional insert (Order/native view only) --------
    def _in_native_order(self) -> bool:
        """True when this table shows a single playlist in its own stored order
        (default view or an explicit ascending Order sort) -- the only case where
        a visual row maps cleanly to a playlist position.

        The test is that the displayed keys are a same-order *subsequence* of
        ``track_keys``, not an exact match: ``_playlist_rows`` dedupes repeated
        keys and drops tracks missing from the library, so an exact compare would
        wrongly reject any playlist with a duplicate or an unresolved track and
        silently fall back to appending. A real re-sort breaks the subsequence
        (rows no longer follow stored order); a filter can't map a visual row to
        a stored position, so it disables positional insert too.

        **Sync-preview rows are skipped**, not failed. A "gaining" ghost is a
        track this playlist does not contain, so its key is absent from
        ``track_keys`` and the subsequence test used to reject the whole view --
        which silently disabled the drop line and positional insert on exactly
        the playlists that are out of sync, i.e. the ones being worked on."""
        if len(self.nodes) != 1 or self.nodes[0].playlist is None:
            return False
        if self._filter_text:
            return False
        vk = self.real_visual_keys()
        it = iter(self.nodes[0].playlist.track_keys)
        return bool(vk) and all(k in it for k in vk)

    def real_visual_keys(self) -> list[str]:
        """Visible keys excluding sync-preview rows -- the rows that correspond
        to actual playlist members."""
        return [self.visual_key_at(r) for r in range(self._proxy.rowCount())
                if not self.is_tombstone_row(r) and self.visual_key_at(r)]

    def _drop_index(self, pos) -> int:
        """Playlist ``track_keys`` insert index for a drop at ``pos``, or ``-1``
        to append. Resolved by the boundary row's key (not its raw row number) so
        it lands at the right stored position even when earlier rows were deduped
        or dropped as missing."""
        if not self._in_native_order():
            return -1
        tk = list(self.nodes[0].playlist.track_keys)
        # Preview rows are not members, so they cannot be an insert boundary.
        vk = self.real_visual_keys()
        idx = self.indexAt(pos)
        if not idx.isValid():
            return len(tk)                         # below the last row -> append
        boundary = idx.row()
        rect = self.visualRect(idx)
        if pos.y() > rect.center().y():
            boundary += 1
        if boundary >= len(vk):
            return len(tk)                         # after the last visible row
        return tk.index(vk[boundary])              # first occurrence, insert-before

    def _show_drop_line(self, pos):
        if not self._in_native_order():
            self._drop_line.hide()
            return
        idx = self.indexAt(pos)
        if idx.isValid():
            rect = self.visualRect(idx)
            y = rect.bottom() if pos.y() > rect.center().y() else rect.top()
        elif self._proxy.rowCount():
            y = self.visualRect(self._proxy.index(self._proxy.rowCount() - 1, 0)).bottom()
        else:
            y = 0
        self._drop_line.setGeometry(0, y, self.viewport().width(), 2)
        self._drop_line.show()
        self._drop_line.raise_()

    # -------------------- watermark --------------------
    def set_watermark(self, logo_file: str) -> None:
        """Which artwork to fade in behind the rows. ``""`` for none."""
        if logo_file == self._logo_file:
            return
        self._logo_file = logo_file or ""
        self.viewport().update()

    def set_watermark_opacity(self, percent: int) -> None:
        percent = max(0, min(100, int(percent)))
        if percent == self._watermark_opacity:
            return
        self._watermark_opacity = percent
        self.viewport().update()

    def watermark_opacity(self) -> int:
        return self._watermark_opacity

    def current_watermark(self):
        """The pixmap this pane would draw right now, or ``None``."""
        return watermark.watermark_pixmap(
            self._logo_file, self.viewport().size(), self._watermark_opacity)

    def paintEvent(self, event):
        """Rows first, then the logo over them.

        Under them would be invisible: alternating row colours are opaque, so a
        watermark painted first survives only in the empty strip below the last
        row -- which is the one place nobody is looking. See ui/watermark.py.
        """
        super().paintEvent(event)
        pixmap = self.current_watermark()
        if pixmap is None:
            return
        painter = QPainter(self.viewport())
        watermark.draw_watermark(painter, self.viewport().rect(), pixmap)
        painter.end()

    # -------------------- pane focus --------------------
    def set_pane_active(self, active: bool) -> None:
        """Paint this pane's selection as the active target, or muted grey.

        Qt swaps to the ``Inactive`` palette group only when the whole *window*
        deactivates, never when a sibling pane takes focus -- so with the track
        area split, both panes would otherwise show their selection as if they
        were the target. The override goes on **both** groups so the muting holds
        whether or not the window is frontmost.

        The base is the *application* palette, so the active state is exactly the
        theme's own colours (light mode has never defined a highlight of its own
        -- it restores Qt's startup palette verbatim) and a theme switch is
        picked up the next time this is called.
        """
        self.pane_active = bool(active)
        pal = QPalette(QApplication.palette())
        if not active:
            hl = QColor(theme_mod.HIGHLIGHT_MUTED)
            fg = QColor(theme_mod.HIGHLIGHT_MUTED_TEXT[theme_mod.current()])
            for group in (QPalette.Active, QPalette.Inactive):
                pal.setColor(group, QPalette.Highlight, hl)
                pal.setColor(group, QPalette.HighlightedText, fg)
        self.setPalette(pal)

    # -------------------- interaction --------------------
    def focusInEvent(self, event):
        self.focused.emit()
        super().focusInEvent(event)

    def mousePressEvent(self, event):
        # Both of these are captured BEFORE anything else runs, for the same
        # reason. Emitting focused() makes this pane active, and
        # super().mousePressEvent() moves the selection onto the row you hit --
        # so by the time `clicked` arrives (on RELEASE) both already describe the
        # world after the click. Editing has to key off the state the pane was in
        # when the button went down, or every first click on a row would open an
        # editor and bringing a pane forward would open one too.
        self._was_active = self.pane_active
        self._was_selected = set(self.selected_visual_rows())
        # A new press anywhere makes a pending edit stale: it names a cell you
        # have just clicked away from.
        self.cancel_pending_edit()
        self.focused.emit()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """The second click of a double-click. Qt delivers this instead of a
        second ``mousePressEvent``, so the cancel has to be here as well.

        Cancelling is not enough on its own: Qt emits ``clicked`` *again* on the
        release that follows the double-click press, which would schedule a fresh
        edit right after this cancelled the old one. So the next ``clicked`` is
        marked to be ignored.
        """
        self.cancel_pending_edit()
        self._skip_next_click_edit = True
        super().mouseDoubleClickEvent(event)

    def _row_key(self, proxy_index) -> str:
        src = self._proxy.mapToSource(proxy_index)
        item = self._model.item(src.row(), 0)
        return item.data(_KEY_ROLE) if item is not None else ""

    def _on_sort_indicator(self, section: int, _order) -> None:
        """A header click is the only thing that turns sorting on. Recorded so a
        later repopulate knows whether to keep the user's sort or fall back to
        the playlist's own order."""
        self._user_sorted = section >= 0

    def sorted_by_user(self) -> bool:
        return self._user_sorted

    def clear_sort(self) -> None:
        """Back to the playlist's stored order."""
        self._user_sorted = False
        self._proxy.sort(-1)

    def scroll_to_key(self, key: str) -> bool:
        """Put the row for ``key`` at the top of the viewport. False when the
        track is no longer shown -- the caller leaves the view where it is
        rather than guessing at a position."""
        if not key:
            return False
        for row in range(self._proxy.rowCount()):
            if self.visual_key_at(row) == key:
                self.scrollTo(self._proxy.index(row, 0),
                              QAbstractItemView.PositionAtTop)
                return True
        return False

    def row_status(self, proxy_row: int) -> str:
        """The status a visual row was built with.

        Read back off the status column's own sort data rather than recomputed
        from the report: the row was drawn from this value, so anything deriving
        it a second time can disagree with what is on screen -- which is exactly
        how the sidebar square and the row markers came apart.
        """
        col = ALL_COLUMN_IDS.index(STATUS_COL)
        src = self._proxy.mapToSource(self._proxy.index(proxy_row, 0))
        item = self._model.item(src.row(), col)
        return (item.data(_SORT_ROLE) or "") if item is not None else ""

    def is_tombstone_row(self, proxy_row: int) -> bool:
        """True for a sync-preview row -- a track this playlist does not actually
        contain (yet, or any more)."""
        return self.row_status(proxy_row) in TOMBSTONE_STATUSES

    # -------------------- deferred click-to-edit --------------------
    def set_double_click_ms(self, ms: int) -> None:
        """How long a click waits to see whether a second one is coming.

        Must not be shorter than Qt's own double-click interval or a double-click
        could still arrive after the editor opened -- which is why the Controls
        dialog sets both from one slider.
        """
        self._double_click_ms = max(0, int(ms))

    def double_click_ms(self) -> int:
        """The configured wait, falling back to whatever Qt is using."""
        if self._double_click_ms is not None:
            return self._double_click_ms
        return QApplication.doubleClickInterval()

    def cancel_pending_edit(self) -> None:
        """Drop a click that was waiting to become an edit."""
        self._edit_timer.stop()
        self._pending_edit = None

    def has_pending_edit(self) -> bool:
        return self._edit_timer.isActive()

    def _open_pending_edit(self) -> None:
        """The wait elapsed with no second click, so it really was a single one.

        The index is persistent: a refresh between the click and here would
        otherwise open an editor on whichever row slid into that position.
        """
        pending, self._pending_edit = self._pending_edit, None
        if pending is None or not pending.isValid():
            return
        self.edit(self._proxy.index(pending.row(), pending.column()))

    def _on_click(self, index):
        self.track_clicked.emit(self._row_key(index))
        if self._skip_next_click_edit:
            self._skip_next_click_edit = False   # the tail of a double-click
            return
        if not self.should_edit_on_click(index):
            return
        # Deferred, not opened: a double-click on an already-selected row would
        # otherwise open an editor AND start playback, since Qt emits `clicked`
        # before it can know a second click is coming.
        self._pending_edit = QPersistentModelIndex(index)
        self._edit_timer.start(self.double_click_ms())

    # The arrow keys are NOT handled here. They are routed application-wide from
    # MainWindow's event filter (Left/Right walk the sidebar, Up/Down this list),
    # because handling them in whichever view has focus is exactly what left them
    # dead after a sidebar click. See route_arrow_key.

    def focusOutEvent(self, event):
        # Focus leaving means you went somewhere else, not that you were lining
        # up an edit.
        self.cancel_pending_edit()
        super().focusOutEvent(event)

    def _on_item_changed(self, item) -> None:
        """One cell was edited in place: report (key, field, new text)."""
        if self._populating:
            return
        col_id = ALL_COLUMN_IDS[item.column()]
        if col_id not in EDITABLE_COLUMNS:
            return
        key = item.data(_KEY_ROLE)
        if key:
            self.metadata_edited.emit(key, col_id, item.text())

    def should_edit_on_click(self, index) -> bool:
        """Whether a single click on ``index`` opens its editor.

        Three conditions, each of which has to hold: the pane was **already**
        focused (so a refocus click cannot start an edit), the row was **already
        selected before this click** (clicking a different row is navigation, not
        editing), and the cell is one of the editable tag columns.

        Both "already" states come from ``mousePressEvent``. Asking the live
        selection here cannot work: this runs from ``clicked``, which Qt emits on
        release, by which point the clicked row is selected whatever it was
        before -- so the check passed for every click and the first click on a
        row opened an editor.
        """
        if not index.isValid() or not getattr(self, "_was_active", False):
            return False
        if index.row() not in getattr(self, "_was_selected", set()):
            return False
        src = self._proxy.mapToSource(index)
        item = self._model.item(src.row(), src.column())
        return bool(item is not None and item.isEditable())

    def _on_double(self, index):
        self.track_double_clicked.emit(self._row_key(index))

    def _on_context(self, pos):
        index = self.indexAt(pos)
        if index.isValid():
            self.track_context_requested.emit(
                self._row_key(index), index.row(), self.viewport().mapToGlobal(pos))

    # -------------------- alt-scroll font scaling --------------------
    @staticmethod
    def _zoom_step(angle_delta) -> int:
        """``+1`` / ``-1`` / ``0`` from a wheel delta.

        Reads whichever axis actually carries it. With Alt held, Qt on Windows
        delivers the wheel as a **horizontal** event: the delta arrives in
        ``x()`` and ``y()`` is 0. Testing ``y()`` alone made ``0 > 0`` false on
        every notch, so the font could only ever shrink. ``0`` for no movement,
        so a stray empty event can't resize anything either.
        """
        delta = angle_delta.y() or angle_delta.x()
        if delta > 0:
            return 1
        return -1 if delta < 0 else 0

    def wheelEvent(self, event):
        if event.modifiers() & Qt.AltModifier:
            step = self._zoom_step(event.angleDelta())
            if step:
                # Emit rather than resize only self, so main_window applies the
                # same size to every table -- split panes must keep equal rows.
                self.font_changed.emit(max(6, min(28, self._font_pt + step)))
            event.accept()
            return
        super().wheelEvent(event)

    def _apply_font(self):
        f = self.font()
        f.setPointSize(self._font_pt)
        self.setFont(f)
        self.horizontalHeader().setFont(f)
        self.verticalHeader().setDefaultSectionSize(int(self._font_pt * 2.1))

    def _scale_columns(self, factor: float) -> None:
        """Grow/shrink column widths with the font so the layout keeps its shape.

        Proportional, not ``resizeColumnsToContents()``: widths are
        user-draggable and persisted with the session, so re-fitting to contents
        would throw away hand-set columns on every notch of the wheel.
        """
        if factor == 1.0:
            return
        header = self.horizontalHeader()
        for i in range(header.count()):
            if header.isSectionHidden(i):
                continue
            header.resizeSection(
                i, max(20, int(round(header.sectionSize(i) * factor))))

    def font_point_size(self) -> int:
        return self._font_pt

    def set_font_point_size(self, pt: int):
        pt = max(6, min(28, int(pt)))
        if pt == self._font_pt:
            return              # every new tab is initialised at the current size
        factor = pt / float(self._font_pt)
        self._font_pt = pt
        self._apply_font()      # font + row height
        self._scale_columns(factor)

    # -------------------- persistence --------------------
    def save_header_state(self):
        return bytes(self.horizontalHeader().saveState().toHex()).decode("ascii")

    def restore_header_state(self, hexstr: str):
        if not hexstr:
            return
        from PySide6.QtCore import QByteArray
        ba = QByteArray.fromHex(QByteArray(hexstr.encode("ascii")))
        self.horizontalHeader().restoreState(ba)
