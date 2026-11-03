"""Tabbed, splittable track area.

The area is a container of **one or two tab groups** in a splitter. Each group is
its own tab strip whose every real tab holds a single :class:`TrackTable`, plus a
persistent **"+" tab** pinned last that opens a new tab in that group.

Splitting a tab moves it into a *second* group (it "jumps over" to the split
view); the group it leaves keeps a fresh empty tab so a group is never left with
only its "+". Unsplit merges the second group back into the first.

Rows (with their status colours) are built by ``main_window`` and pushed into
``focused_table()``, so the diff/status logic stays in one place.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QMenu, QSplitter, QTabBar, QTabWidget,
                               QVBoxLayout, QWidget)

from ..model.library import TRAKTOR, VDJ
from . import theme as theme_mod
from .track_table import TRACK_MIME, TrackTable

_PLUS = "__plus__"      # tabData marker for the "+" pseudo-tab


# One definition, shared with the unfocused pane's selection colour: the dim tab
# marker and the grey selection both mean "not the active group".
_MUTED_ACCENT = theme_mod.HIGHLIGHT_MUTED

# ---------------------------- tab widths ----------------------------------
# A tab is as wide as its name needs, within bounds. Qt's own hint cannot be
# used: this bar paints its text and its "×" itself, with padding the default
# hint knows nothing about, so long names were being clipped mid-word.
TAB_PAD_LEFT = 8          # matches the drawText inset in paintEvent
TAB_PAD_RIGHT = 8         # …and the gap after the "×"
TAB_MIN_W = 72            # narrower than this and no name is legible at all
TAB_MAX_W = 220           # a very long playlist name must not swallow the strip
PLUS_TAB_W = 28           # the "+" holds one glyph and should stay square-ish


def natural_width(text_px: int, close_px: int) -> int:
    """What a tab wants: its text plus the chrome this bar draws, bounded."""
    wanted = text_px + TAB_PAD_LEFT + close_px + TAB_PAD_RIGHT
    return max(TAB_MIN_W, min(TAB_MAX_W, wanted))


def fitted_widths(naturals: list[int], available: int) -> list[int]:
    """Shrink the strip toward :data:`TAB_MIN_W` before Qt starts scrolling it.

    Scroll arrows hide tabs entirely; narrowing them keeps every tab reachable,
    which is the point of a tab strip. Below the minimum there is nothing left to
    give and the arrows are the honest answer.
    """
    if not naturals:
        return []
    total = sum(naturals)
    if total <= available or available <= 0:
        return list(naturals)
    factor = available / total
    return [max(TAB_MIN_W, int(n * factor)) for n in naturals]


class _SourceTabBar(QTabBar):
    """Paints each tab in its program's colours (the style ignores per-tab
    background, so we draw it ourselves). The "+" tab paints neutral.

    ``group_focused`` marks the tab strip whose current tab is *the* active
    target (matters once the area is split): its current tab paints bright with a
    thick accent; a non-focused group dims its current tab to a thin muted line.
    """

    _CLOSE = 12          # side length of the "×" hit box, in px

    def __init__(self, parent=None):
        super().__init__(parent)
        self.group_focused = True
        self.setAcceptDrops(True)          # drop tracks onto a tab to add them
        # Qt stretches tabs to fill the bar by default, which overrides
        # tabSizeHint entirely: every tab came out the same width whatever its
        # name said, and the "+" grew to match. Off, so widths follow the text.
        self.setExpanding(False)

    # ---- widths ----
    def _natural_widths(self) -> list[int]:
        """Every tab's wanted width, "+" included, in tab order."""
        metrics = self.fontMetrics()
        out = []
        for i in range(self.count()):
            if self.tabData(i) == _PLUS:
                out.append(PLUS_TAB_W)
            else:
                out.append(natural_width(
                    metrics.horizontalAdvance(self.tabText(i)), self._CLOSE))
        return out

    def tabSizeHint(self, index: int):
        base = super().tabSizeHint(index)
        naturals = self._natural_widths()
        if index < 0 or index >= len(naturals):
            return base
        if self.tabData(index) == _PLUS:
            return QSize(PLUS_TAB_W, base.height())
        # Widths depend only on the tab *texts* and the bar's own width, never on
        # what this hint returned last time, so re-laying out cannot oscillate.
        return QSize(fitted_widths(naturals, self.width())[index], base.height())

    def minimumTabSizeHint(self, index: int):
        base = super().minimumTabSizeHint(index)
        width = PLUS_TAB_W if self.tabData(index) == _PLUS else TAB_MIN_W
        return QSize(width, base.height())

    # ---- per-tab close "×" ----
    def _close_rect(self, i: int) -> QRect:
        """Hit box for tab ``i``'s close glyph, in the top-right corner."""
        r = self.tabRect(i)
        s = self._CLOSE
        return QRect(r.right() - s - 4, r.top() + (r.height() - s) // 2, s, s)

    def mousePressEvent(self, event):
        # A left click on a real tab's "×" closes it, without first selecting or
        # focusing the tab (so no context menu, no tab switch).
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            group = self.parent()
            for i in range(self.count()):
                if self.tabData(i) == _PLUS:
                    continue
                if self._close_rect(i).contains(pos):
                    if isinstance(group, _TabGroup):
                        group._ctl.close_tab(group, i)
                    event.accept()
                    return
        super().mousePressEvent(event)

    # ---- drop tracks onto a tab (always appends, per the default rule) ----
    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(TRACK_MIME):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(TRACK_MIME):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        mime = event.mimeData()
        group = self.parent()              # the _TabGroup that owns this bar
        if not mime.hasFormat(TRACK_MIME) or not isinstance(group, _TabGroup):
            return super().dropEvent(event)
        i = self.tabAt(event.position().toPoint())
        if i < 0 or self.tabData(i) == _PLUS:
            return
        event.acceptProposedAction()
        group.drop_tracks_on_tab(i, mime)

    def paintEvent(self, _event):
        p = QPainter(self)
        for i in range(self.count()):
            data = self.tabData(i)
            rect = self.tabRect(i)
            if data == _PLUS:
                p.fillRect(rect, QColor(theme_mod.TAB_BG_INACTIVE[""]))
                p.setPen(QColor(theme_mod.TAB_FG[""]))
                p.drawText(rect, Qt.AlignCenter, "+")
                continue
            source = data or ""
            selected = i == self.currentIndex()
            # A non-focused group's current tab reads as inactive so it is clear
            # which group is the active target.
            active = selected and self.group_focused
            bg = (theme_mod.TAB_BG if active else theme_mod.TAB_BG_INACTIVE).get(
                source, theme_mod.TAB_BG_INACTIVE[""])
            fg = theme_mod.TAB_FG.get(source, theme_mod.TAB_FG[""])
            p.fillRect(rect, QColor(bg))
            if active:
                p.fillRect(rect.left(), rect.top(), rect.width(), 3, QColor(fg))
            elif selected:
                # current tab of a non-focused group: a faint marker only
                p.fillRect(rect.left(), rect.top(), rect.width(), 1, QColor(_MUTED_ACCENT))
            p.setPen(QColor(fg))
            # Reserve the right edge for the close glyph so text never runs under it.
            text_rect = rect.adjusted(TAB_PAD_LEFT, 0,
                                      -(self._CLOSE + TAB_PAD_RIGHT), 0)
            # Elide rather than clip: a name too long for a shrunken tab used to
            # be cut off mid-character, which reads as a rendering fault.
            label = self.fontMetrics().elidedText(
                self.tabText(i), Qt.ElideRight, text_rect.width())
            p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, label)
            p.drawText(self._close_rect(i), Qt.AlignCenter, "×")


class _TabGroup(QTabWidget):
    """One tab strip. Real tabs hold a TrackTable; the last tab is always "+"."""

    def __init__(self, controller: "TrackTabs", parent=None):
        super().__init__(parent)
        self._ctl = controller
        self.setTabBar(_SourceTabBar(self))
        self.setMovable(True)
        self.setDocumentMode(True)
        self.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tabBar().customContextMenuRequested.connect(self._menu)
        self.tabBarClicked.connect(self._on_tab_clicked)
        self.currentChanged.connect(self._on_current_changed)

        # The persistent "+" tab (a blank placeholder page, never really shown).
        self.addTab(QWidget(), "+")
        self.tabBar().setTabData(0, _PLUS)

    # ------------------------------ helpers ------------------------------
    def _plus_index(self) -> int:
        for i in range(self.count()):
            if self.tabBar().tabData(i) == _PLUS:
                return i
        return self.count() - 1

    def real_count(self) -> int:
        return self.count() - 1                 # minus the "+" tab

    def tables(self) -> list[TrackTable]:
        return [self.widget(i) for i in range(self.count())
                if self.tabBar().tabData(i) != _PLUS]

    def current_table(self):
        w = self.currentWidget()
        return w if isinstance(w, TrackTable) else None

    # ------------------------------ mutation -----------------------------
    def add_table(self, table: TrackTable, label: str = "empty",
                  source: str = "") -> int:
        idx = self.insertTab(self._plus_index(), table, label or "empty")
        self.tabBar().setTabData(idx, source or "")
        self.setCurrentIndex(idx)
        return idx

    def new_tab(self) -> TrackTable:
        table = self._ctl._make_table()
        self.add_table(table)
        self._ctl._set_focused(table)
        return table

    def drop_tracks_on_tab(self, index: int, mime):
        """A track drop landed on tab ``index`` -- add to that tab's playlist
        (always appends, per the default rule for non-body drops)."""
        table = self.widget(index)
        if not isinstance(table, TrackTable):
            return
        parsed = TrackTable._parse_drop(mime, is_self=False)
        if parsed is None or self._ctl._on_tracks_dropped is None:
            return
        keys, source = parsed
        self._ctl._on_tracks_dropped(table, keys, source, -1)

    def detach_current(self):
        """Remove the current real tab (without deleting it) and return
        ``(table, label, source)`` for re-attaching elsewhere."""
        i = self.currentIndex()
        if self.tabBar().tabData(i) == _PLUS:
            return None
        table = self.widget(i)
        label = self.tabText(i)
        source = self.tabBar().tabData(i) or ""
        self.removeTab(i)                       # does not delete the widget
        return table, label, source

    # ------------------------------ events -------------------------------
    def _on_tab_clicked(self, index):
        if self.tabBar().tabData(index) == _PLUS:
            self.new_tab()
            return
        # Clicking a tab always focuses its group -- including clicking the
        # already-current tab of a non-focused group, where currentChanged does
        # not fire. Idempotent with the currentChanged path when it does switch.
        table = self.widget(index)
        if isinstance(table, TrackTable):
            self._ctl._set_focused(table)
            table.setFocus()

    def _on_current_changed(self, index):
        if index < 0:
            return
        if self.tabBar().tabData(index) == _PLUS and self.real_count() > 0:
            # Never leave the "+" tab as the shown page.
            self.setCurrentIndex(max(0, index - 1))
            return
        table = self.current_table()
        if table is not None:
            self._ctl._set_focused(table)

    def _menu(self, pos):
        i = self.tabBar().tabAt(pos)
        if i < 0 or self.tabBar().tabData(i) == _PLUS:
            return
        self.setCurrentIndex(i)
        menu = QMenu(self)
        if self._ctl.is_split():
            menu.addAction("Unsplit", self._ctl.unsplit)
        else:
            menu.addAction("Split vertical",
                           lambda: self._ctl.split_from(self, Qt.Horizontal))
            menu.addAction("Split horizontal",
                           lambda: self._ctl.split_from(self, Qt.Vertical))
        menu.addSeparator()
        menu.addAction("Close tab", lambda: self._ctl.close_tab(self, i))
        menu.exec(self.tabBar().mapToGlobal(pos))


class TrackTabs(QWidget):
    """Container of one or two tab groups. Public API (``focused_table``,
    ``all_tables``, ``new_tab``, ``label_current``) is what ``main_window`` uses;
    it is unchanged by the split-into-groups refactor."""

    focused_changed = Signal()

    def __init__(self, on_clicked, on_double, on_context, on_created=None,
                 on_column_toggled=None, on_tracks_dropped=None,
                 on_font_changed=None, on_tracks_reordered=None,
                 on_files_dropped=None, parent=None):
        super().__init__(parent)
        self._on_clicked = on_clicked
        self._on_double = on_double
        self._on_context = on_context
        self._on_created = on_created
        self._on_column_toggled = on_column_toggled
        self._on_tracks_dropped = on_tracks_dropped
        self._on_font_changed = on_font_changed
        self._on_tracks_reordered = on_tracks_reordered
        self._on_files_dropped = on_files_dropped
        self._focused: TrackTable | None = None
        self._focused_group: _TabGroup | None = None

        self.splitter = QSplitter(Qt.Horizontal, self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.splitter)

        group = self._new_group()
        self.splitter.addWidget(group)
        group.new_tab()                          # start with one empty tab

    # ------------------------------ groups ------------------------------
    def _groups(self) -> list[_TabGroup]:
        return [self.splitter.widget(i) for i in range(self.splitter.count())]

    def _new_group(self) -> _TabGroup:
        return _TabGroup(self, self)

    def is_split(self) -> bool:
        return self.splitter.count() > 1

    def split_from(self, group: _TabGroup, orientation):
        if self.is_split():
            return                               # max two groups
        detached = group.detach_current()
        if detached is None:
            return
        table, label, source = detached
        if group.real_count() == 0:
            group.new_tab()                      # never leave a group empty
        other = self._new_group()
        self.splitter.setOrientation(orientation)
        self.splitter.addWidget(other)
        self.splitter.setSizes([1, 1])
        other.add_table(table, label, source)
        self._set_focused(table)

    def unsplit(self):
        if not self.is_split():
            return
        first, second = self._groups()[0], self._groups()[1]
        while second.real_count() > 0:
            second.setCurrentIndex(0)
            table, label, source = second.detach_current()
            first.add_table(table, label, source)
        second.setParent(None)
        second.deleteLater()
        self._set_focused(first.current_table())

    def close_tab(self, group: _TabGroup, index: int):
        table = group.widget(index)
        if table is self._focused:
            self._focused = None                 # avoid pointing at a dead table
        group.removeTab(index)
        if table is not None:
            table.deleteLater()
        if group.real_count() == 0:
            if self.is_split():
                # collapse this now-empty group; the other becomes the sole group
                other = next(g for g in self._groups() if g is not group)
                group.setParent(None)
                group.deleteLater()
                self._set_focused(other.current_table())
            else:
                group.new_tab()                  # never zero tabs overall
        elif self._focused is None:
            self._set_focused(group.current_table())

    # ------------------------------ tables ------------------------------
    def _make_table(self) -> TrackTable:
        t = TrackTable()
        t.track_clicked.connect(self._on_clicked)
        t.track_double_clicked.connect(self._on_double)
        t.track_context_requested.connect(self._on_context)
        t.focused.connect(lambda tbl=t: self._set_focused(tbl))
        if self._on_column_toggled is not None:
            t.column_toggled.connect(self._on_column_toggled)
        if self._on_tracks_dropped is not None:
            t.tracks_dropped.connect(
                lambda keys, source, at, tbl=t: self._on_tracks_dropped(tbl, keys, source, at))
        if self._on_tracks_reordered is not None:
            t.tracks_reordered.connect(
                lambda new_keys, tbl=t: self._on_tracks_reordered(tbl, new_keys))
        if self._on_files_dropped is not None:
            t.files_dropped.connect(
                lambda paths, at, tbl=t: self._on_files_dropped(tbl, paths, at))
        if self._on_font_changed is not None:
            t.font_changed.connect(self._on_font_changed)
        if self._on_created is not None:
            self._on_created(t)
        return t

    def all_tables(self) -> list[TrackTable]:
        out = []
        for g in self._groups():
            out.extend(g.tables())
        return out

    def visible_tables(self) -> list[TrackTable]:
        """The current table of *each* group -- i.e. what is actually on screen.
        With the area split that is two tables, not one, and both of them need
        their rows."""
        return [t for t in (g.current_table() for g in self._groups())
                if t is not None]

    def _group_of(self, table: TrackTable) -> _TabGroup | None:
        for g in self._groups():
            if table in g.tables():
                return g
        return None

    def _set_focused(self, table):
        if table is None:
            return
        changed = table is not self._focused
        # Always recompute the owning group and repaint focus: after a split the
        # focused table object is unchanged but has moved to a new group.
        self._focused = table
        self._focused_group = self._group_of(table)
        self._refresh_group_focus()
        if changed:
            self.focused_changed.emit()

    def _refresh_group_focus(self):
        """Mark each group so only the focused one reads as the active target --
        its tab bar's accent, and its tables' selection colour."""
        for g in self._groups():
            active = (g is self._focused_group)
            g.tabBar().group_focused = active
            g.tabBar().update()
            for table in g.tables():
                table.set_pane_active(active)

    def refresh_pane_focus(self):
        """Re-assert the per-pane styling. A widget palette override outlives an
        app-wide theme change, so the theme path has to call this."""
        self._refresh_group_focus()

    def focused_table(self):
        if self._focused is not None and self._group_of(self._focused) is not None:
            return self._focused
        groups = self._groups()
        return groups[0].current_table() if groups else None

    def focus_other_group(self) -> bool:
        """Move focus to the other split pane. False when there is no other one.

        Inert rather than splitting: splitting is a deliberate action from the
        tab menu, and a focus key that silently rearranged the layout would be a
        surprising thing to lean on.
        """
        groups = self._groups()
        if len(groups) < 2:
            return False
        current = self._focused_group or groups[0]
        other = next((g for g in groups if g is not current), None)
        table = other.current_table() if other is not None else None
        if table is None:
            return False
        self._set_focused(table)
        table.setFocus()
        return True

    # ---------------------------- labelling -----------------------------
    def new_tab(self):
        group = self._focused_group or (self._groups()[0] if self._groups() else None)
        if group is not None:
            group.new_tab()

    def step_tab(self, delta: int) -> None:
        """Move to the next/previous tab in the focused group, wrapping.

        Unlike the sidebar, wrapping here is wanted: a tab strip is short and
        cycling it is the standard behaviour of every Ctrl+Tab anyone has used.
        The "+" tab is skipped -- it is a button, not a tab.
        """
        group = self._focused_group or (self._groups()[0] if self._groups() else None)
        if group is None:
            return
        real = [i for i in range(group.count())
                if group.tabBar().tabData(i) != _PLUS]
        if len(real) < 2:
            return
        try:
            here = real.index(group.currentIndex())
        except ValueError:
            here = 0
        group.setCurrentIndex(real[(here + delta) % len(real)])

    def close_current_tab(self) -> None:
        group = self._focused_group or (self._groups()[0] if self._groups() else None)
        if group is not None and group.tabBar().tabData(group.currentIndex()) != _PLUS:
            self.close_tab(group, group.currentIndex())

    def split_focused(self) -> None:
        """Split the focused group. A no-op when already split -- the area holds
        two groups by design, so a third would have nowhere to go."""
        from PySide6.QtCore import Qt as _Qt
        group = self._focused_group or (self._groups()[0] if self._groups() else None)
        if group is not None and not self.is_split():
            self.split_from(group, _Qt.Horizontal)

    def label_current(self, text: str, source: str):
        group = self._focused_group or (self._groups()[0] if self._groups() else None)
        if group is None:
            return
        i = group.currentIndex()
        if group.tabBar().tabData(i) == _PLUS:
            return
        self._label(group, i, text, source)

    def label_table(self, table, text: str, source: str):
        """Relabel a specific tab. Tab text is otherwise only written when a
        playlist is selected, so after a rename every *other* tab showing it
        would keep the stale name until it was next clicked."""
        group = self._group_of(table)
        if group is None:
            return
        i = group.indexOf(table)
        if i >= 0:
            self._label(group, i, text, source)

    @staticmethod
    def _label(group, i: int, text: str, source: str):
        group.setTabText(i, text or "empty")
        group.tabBar().setTabData(i, source or "")
        group.tabBar().update()

    # ------------------------- session persistence ------------------------
    # The layout is JSON-safe and self-contained: each tab's table already
    # knows its own source and nodes, so nothing has to be threaded in.
    def capture_layout(self) -> dict:
        groups = []
        focused = [0, 0]
        for gi, group in enumerate(self._groups()):
            tabs = []
            for ti, table in enumerate(group.tables()):
                index = group.indexOf(table)
                # A tab restored last session but never shown has no nodes yet --
                # save what it is still waiting for, or quitting without clicking
                # it would silently forget its playlist.
                pending = getattr(table, "pending_restore", None)
                if pending is not None:
                    source, paths = pending
                    node_paths = [list(p) for p in paths]
                else:
                    source = table.source or ""
                    node_paths = [list(n.node_path) for n in table.nodes]
                tabs.append({
                    "source": source,
                    "label": group.tabText(index),
                    "node_paths": node_paths,
                })
                if table is self._focused:
                    focused = [gi, ti]
            groups.append({"current": max(0, group.currentIndex()), "tabs": tabs})
        return {
            "orientation": "v" if self.splitter.orientation() == Qt.Vertical else "h",
            "sizes": self.splitter.sizes(),
            "focused": focused,
            "groups": groups,
        }

    def restore_layout(self, spec: dict) -> list:
        """Rebuild the groups/tabs from ``capture_layout`` output.

        Returns ``[(table, source, node_paths), …]`` -- the *contents* are the
        caller's job, since building rows needs the libraries and the status
        diff that live in main_window. A spec with no usable tabs is ignored, so
        a corrupt session file can never leave the area with nothing in it.
        """
        groups_spec = [g for g in (spec or {}).get("groups") or [] if g.get("tabs")]
        if not groups_spec:
            return []

        for group in self._groups():
            group.setParent(None)
            group.deleteLater()
        self._focused = None
        self._focused_group = None

        self.splitter.setOrientation(
            Qt.Vertical if spec.get("orientation") == "v" else Qt.Horizontal)

        out = []
        focus_group, focus_tab = (spec.get("focused") or [0, 0])[:2]
        for gi, gspec in enumerate(groups_spec[:2]):        # two groups, max
            group = self._new_group()
            self.splitter.addWidget(group)
            for tspec in gspec["tabs"]:
                table = self._make_table()
                group.add_table(table, tspec.get("label") or "empty",
                                tspec.get("source") or "")
                out.append((table, tspec.get("source") or "",
                            [tuple(p) for p in tspec.get("node_paths") or []]))
            current = min(max(0, gspec.get("current", 0)), group.real_count() - 1)
            group.setCurrentIndex(current)
            if gi == focus_group:
                tables = group.tables()
                if tables:
                    self._set_focused(tables[min(focus_tab, len(tables) - 1)])

        sizes = spec.get("sizes")
        if sizes and len(sizes) == self.splitter.count():
            self.splitter.setSizes(sizes)
        if self._focused is None:
            self._set_focused(self._groups()[0].current_table())
        return out
