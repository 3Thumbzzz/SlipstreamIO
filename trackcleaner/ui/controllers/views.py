"""The track tabs and what goes in them: rows, columns, and the sidebar↔tab link.

This is the layer between the model and the visible table. It owns:

* **row building** -- turning playlist nodes into ``(Track, status)`` rows,
  including the sync-preview markers (grey = a track this playlist will lose,
  deep-green = one it will gain);
* **tab population**, with the scroll-restore subtlety a live refresh needs;
* **node re-resolution by ``node_path``** -- the only identity stable across a
  reload, since a reload replaces every ``TreeNode`` object;
* the **lazy restore** of tabs saved from the last session.

Most other controllers call in here after they mutate the model, which they do
through the window (``win._refresh_playlist_everywhere(...)``).
"""

from __future__ import annotations

from PySide6.QtCore import QTimer

from ...model import status as status_mod
from ...model.library import TRAKTOR, VDJ, path_ci
from .. import watermark


class ViewController:
    """Extracted from MainWindow; see ``controllers/__init__.py`` for why it
    holds the window rather than its dependencies."""

    def __init__(self, win):
        self.win = win

    # -------------------- tables / columns --------------------
    def table(self):
        """The focused track table -- the target of sidebar clicks, search and
        context menus."""
        return self.win.track_tabs.focused_table()

    def init_table(self, table):
        """Apply the global UI prefs to a freshly created table (new tab/split).
        Runs during TrackTabs construction too, so it must not reach back into
        ``win.track_tabs`` (not yet assigned)."""
        table.set_hidden_fields(set(self.win._hidden_fields))
        table.set_font_point_size(getattr(self.win, "_font_pt", 9))
        table.set_watermark_opacity(
            getattr(self.win, "_watermark_opacity", watermark.DEFAULT_OPACITY))
        timing = getattr(self.win, "_timing", None) or {}
        if timing.get("double_click_ms"):
            table.set_double_click_ms(timing["double_click_ms"])
        # An in-place cell edit takes the same write path as the Edit… dialog,
        # so the two cannot diverge in what they persist or what they mark.
        table.metadata_edited.connect(
            lambda key, field, value, t=table:
            self.win._on_cell_edited(t, key, field, value))

    def on_font_changed(self, pt: int):
        """Alt-wheel zoom is global, so split panes keep equal row heights."""
        self.win._font_pt = pt
        for t in self.win.track_tabs.all_tables():
            t.set_font_point_size(pt)

    def set_column_visible(self, col_id: str, visible: bool):
        win = self.win
        if visible:
            win._hidden_fields.discard(col_id)
        else:
            win._hidden_fields.add(col_id)
        for t in win.track_tabs.all_tables():
            t.set_hidden_fields(set(win._hidden_fields))

    def on_header_column_toggled(self, col_id: str, visible: bool):
        """A column toggled from a table's header menu -- apply it everywhere
        and keep the View → Columns checkmark in sync."""
        self.set_column_visible(col_id, visible)
        act = self.win._column_actions.get(col_id)
        if act is not None:
            act.setChecked(visible)

    def status_for(self, source: str, report, key: str, default: str) -> str:
        # A file the integrity check found missing shows red everywhere.
        if key in self.win._missing_keys.get(source, ()):
            return status_mod.RED
        return report.track_status.get(key, default) if report else default

    def out_of_sync_count(self, nodes: list, source: str) -> int:
        """How many of these playlists' tracks are **not** green.

        The number that answers "why is this playlist yellow?". A playlist square
        is yellow when any track in it differs, and one differing track among 517
        is not findable by scrolling -- which is how a perfectly correct yellow
        square came to be reported as a bug.

        Read off the report already in hand, through :meth:`status_for` so a
        missing file counts too; no diffing happens here.
        """
        report = self.win.reports.get(source)
        if report is None:
            return 0
        seen, bad = set(), 0
        for node in nodes:
            if node.playlist is None:
                continue
            for key in node.playlist.track_keys:
                if key in seen:
                    continue
                seen.add(key)
                if self.status_for(source, report, key,
                                   status_mod.BLACK) != status_mod.GREEN:
                    bad += 1
        return bad

    # -------------------- node resolution --------------------
    def nodes_by_paths(self, paths, source: str) -> list:
        """The live playlist nodes at these ``node_path``s, skipping any that no
        longer exist. node_path is the only stable identity across a reload (or
        across a whole session, for the restored tabs)."""
        win = self.win
        # .get, not [source]: an empty tab carries source "", which is not a
        # program. The old two-way ternary quietly answered "Traktor" for it.
        lib = win.libraries.get(source)
        if lib is None:
            return []
        by_path = {tuple(n.node_path): n for n in lib.playlists()}
        return [by_path[tuple(p)] for p in paths if tuple(p) in by_path]

    def resolve_nodes(self, nodes: list, source: str) -> list:
        """Re-resolve stale node objects to the freshly-loaded ones, by
        ``node_path`` (a reload replaces every TreeNode)."""
        return self.nodes_by_paths([n.node_path for n in nodes], source)

    # -------------------- row building --------------------
    def playlist_rows(self, nodes: list, source: str, *, include_tombstones: bool):
        """Build ``[(Track, status)]`` for the union of ``nodes``' playlists.

        Returns ``(rows, ghost_count)``. Sync-preview markers are shown on a
        sidebar click but NOT on a live edit (a just-removed track is simply
        gone, not greyed): a **losing** track keeps its real row but recolours
        grey, and a **gaining** track is appended as a deep-green ghost.
        """
        win = self.win
        source_lib = win.libraries.get(source)
        if source_lib is None:
            return [], 0                 # an empty tab has no program, so no rows
        # A ghost row is a track some other program will send, so its Track has
        # to be borrowed from wherever it exists. The reference is tried first
        # because that is what the divergence was computed against; any other
        # loaded program will do as a fallback, since all the row needs is the
        # metadata to display.
        others = [win.libraries[a] for a in
                  ([win.reference_program()] +
                   [b for b in win.libraries if b != win.reference_program()])
                  if a != source and win.libraries.get(a) is not None]
        report = win.reports.get(source)

        losing: set[str] = set()
        gaining: list = []                      # (node, [keys]) preserving order
        if include_tombstones:
            for node in nodes:
                div = (win._divergence or {}).get(path_ci(node.node_path), {}).get(source)
                if not div:
                    continue
                losing |= set(div.get("losing", ()))
                gaining.append(div.get("gaining", ()))

        seen: set[str] = set()
        rows = []
        for node in nodes:
            if node.playlist is None:
                continue
            # Only playlists that forbid duplicates collapse repeats; Traktor and
            # noDuplicates="no" VDJ playlists show every occurrence as its own row
            # (so visual order == track_keys exactly, and positional insert/reorder
            # land on the right slot).
            dedup = node.playlist.no_duplicates
            for key in node.playlist.track_keys:
                if dedup and key in seen:
                    continue
                seen.add(key)
                track = source_lib.tracks.get(key)
                if track is None:
                    continue
                status = (status_mod.LOSING if key in losing
                          else self.status_for(source, report, key, "black"))
                rows.append((track, status))

        ghosts = 0
        for keys in gaining:
            for key in keys:
                if key in seen:
                    continue
                seen.add(key)
                track = source_lib.tracks.get(key)
                if track is None:
                    track = next((lib.tracks[key] for lib in others
                                  if key in lib.tracks), None)
                if track is None:
                    continue
                rows.append((track, status_mod.GAINING))
                ghosts += 1
        return rows, ghosts

    def populate_table(self, table, nodes: list, source: str, *,
                       include_tombstones: bool, preserve_scroll: bool,
                       keep_anchor: bool = False):
        """Fill one table's rows from ``nodes``.

        Two ways to hold the view still, because they fail differently:

        ``preserve_scroll`` restores the raw scrollbar values. Right for a live
        refresh of the *same* rows, where the pixel offset still means what it
        did.

        ``keep_anchor`` remembers the **top visible track** and scrolls back to
        it afterwards. Right after a sync or a reload, where the row count
        changes -- consumed preview rows disappear -- so a pixel offset points
        somewhere else entirely, and a plain restore would either jump or land
        on a blank region.
        """
        if table is None:
            return 0
        vbar, hbar = table.verticalScrollBar(), table.horizontalScrollBar()
        v, h = vbar.value(), hbar.value()
        anchor = self._top_visible_key(table) if keep_anchor else ""
        # set_rows resets the model, which drops the selection -- so a refresh, a
        # sync or a tag edit cleared the highlight in every pane. Held by KEY: a
        # sync consumes preview rows and reorders what is left, so the row a
        # track sat on means something else afterwards.
        selected = table.selected_keys()
        rows, ghosts = self.playlist_rows(nodes, source,
                                          include_tombstones=include_tombstones)
        table.set_rows(rows)
        table.set_context(source, nodes)
        if selected:
            # Synchronously, unlike the scroll restore below. Selecting a row
            # needs only the model, which set_rows has already rebuilt; it is
            # scrolling that needs the view's recomputed ranges. Deferring this
            # one put a callback into the event loop that fired during whatever
            # ran next, which is a cost with nothing to buy.
            table.select_keys(selected)
        if preserve_scroll:
            # Restore on the NEXT event-loop tick: set_rows just did a full model
            # reset, and the view hasn't recomputed its scroll range yet, so a
            # synchronous setValue is unreliable -- it either clamps to 0 (list
            # snaps to the top) or lands on a now-blank region (the tab looks
            # empty until re-selected). Deferring lets the range settle first.
            def _restore(t=table, vv=v, hh=h):
                try:
                    t.verticalScrollBar().setValue(vv)
                    t.horizontalScrollBar().setValue(hh)
                except RuntimeError:
                    pass                       # table torn down before the tick
            QTimer.singleShot(0, _restore)
        elif anchor:
            def _to_anchor(t=table, key=anchor):
                try:
                    t.scroll_to_key(key)
                except RuntimeError:
                    pass                       # table torn down before the tick
            QTimer.singleShot(0, _to_anchor)
        return len(rows) - ghosts, ghosts

    @staticmethod
    def _top_visible_key(table) -> str:
        """The track key of the row at the top of the viewport, or ""."""
        try:
            index = table.indexAt(table.viewport().rect().topLeft())
        except RuntimeError:
            return ""
        if not index.isValid():
            return ""
        return table.visual_key_at(index.row())

    # -------------------- selection --------------------
    def on_playlists_selected(self, nodes: list, source: str):
        win = self.win
        win._current_source = source
        win._current_nodes = nodes
        win._clear_search_silently()   # showing a playlist resets an active search
        table = self.table()
        if not nodes:
            if table is not None:
                table.set_rows([])
                table.set_context(source, [])
            win.status_label.setText("")
            return
        win.playlist_panel.clear_other_selection(source)
        # What this playlist is being measured against, which is the reference
        # rather than "the other one".
        reference = win.reference_program()
        target = reference if reference != source else next(
            (a for a in win.libraries if a != source), source)
        target_name = win.adapters[target].display_name \
            if target in win.adapters else target

        real, ghosts = self.populate_table(table, nodes, source,
                                           include_tombstones=True,
                                           preserve_scroll=False)
        names = ", ".join(n.name for n in nodes[:3]) + (" …" if len(nodes) > 3 else "")
        if table is not None:
            win.track_tabs.label_current(names, source)
        ghost_note = f"   |   {ghosts} incoming on sync" if ghosts else ""
        out_of_sync = self.out_of_sync_count(nodes, source)
        sync_note = f"   |   {out_of_sync} out of sync" if out_of_sync else ""
        win.status_label.setText(
            f"{source.upper()} → {target_name}   |   {real} tracks"
            f"{sync_note}{ghost_note}   |   {names}")

    def refresh_playlist_everywhere(self, node):
        """After an in-memory edit to ``node``'s playlist, re-populate every
        table currently showing it -- matched by node_path + source, using each
        table's OWN context so tabs never get reassigned and duplicate tabs
        refresh together. No re-diff, no tombstones, scroll preserved."""
        if node is None:
            return
        want = tuple(node.node_path)
        for table in self.win.track_tabs.all_tables():
            if table.source != node.source:
                continue
            if any(tuple(n.node_path) == want for n in table.nodes):
                self.populate_table(table, list(table.nodes), table.source,
                                    include_tombstones=False, preserve_scroll=True)

    def on_tab_focus_changed(self):
        """Mirror the focused tab in the sidebar: move the playlist selection to
        whatever the now-focused tab shows, so the sidebar 'indicator' always
        points at the active tab. Done silently -- no table reload -- and only
        when the tab shows exactly one playlist (a folder/multi union has no
        single node to point at)."""
        win = self.win
        table = win.track_tabs.focused_table()
        if table is None:
            return
        # A tab restored from the last session fills the first time it is shown.
        self.fill_pending(table)
        # BEFORE the node guard below, which is about which *row* to highlight,
        # not which program is current. Leaving this out is what made Left/Right
        # keep walking the previous tab's tree after switching programs, and made
        # the Playlists… popup answer for whatever was last selected in the
        # sidebar rather than for the tab the track is in.
        if table.source:
            win._current_source = table.source
        if len(table.nodes) != 1:
            return
        node = table.nodes[0]
        if getattr(node, "playlist", None) is None:
            return
        win.playlist_panel.clear_other_selection(table.source)
        win.playlist_panel.tree_for(table.source).highlight_node(node)

    # -------------------- whole-area refreshes --------------------
    def refresh_all_tabs(self):
        """After a library reload, re-point EVERY open tab to the fresh node
        objects (matched by source + node_path) and repopulate it, scroll
        preserved. Each tab keeps its own playlist -- no tab is ever reassigned
        to whatever happened to be synced/selected."""
        for table in self.win.track_tabs.all_tables():
            if not table.nodes:
                continue
            fresh = self.resolve_nodes(list(table.nodes), table.source)
            if fresh:
                # preserve_scroll stays False: restoring the raw offset after a
                # full set_rows reset left the viewport blank (rows present,
                # nothing painted). keep_anchor instead scrolls back to the track
                # that was on top, which repaints cleanly AND survives the row
                # count changing -- a sync consumes the preview rows, so the old
                # pixel offset would point at a different track anyway.
                self.populate_table(table, fresh, table.source,
                                    include_tombstones=True, preserve_scroll=False,
                                    keep_anchor=True)
            else:
                table.set_rows([])
                table.set_context(table.source, [])

    def clear_tabs_for(self, nodes: list) -> None:
        """Empty every tab showing one of ``nodes``. They have just been detached
        from the library, so a tab still holding one would keep displaying a
        playlist that no longer exists."""
        win = self.win
        removed = {id(n) for n in nodes}
        for table in win.track_tabs.all_tables():
            if not any(id(n) in removed for n in table.nodes):
                continue
            table.set_rows([])
            table.set_context(table.source, [])
            win.track_tabs.label_table(table, "", table.source)
        win._current_nodes = [n for n in win._current_nodes
                              if id(n) not in removed]

    def relabel_tabs_for(self, node):
        """Refresh the tab text of every tab showing ``node`` -- tab labels are
        otherwise only written on selection, so a rename would leave stale names
        on any tab that isn't the focused one."""
        win = self.win
        for table in win.track_tabs.all_tables():
            if table.source != node.source or not any(n is node for n in table.nodes):
                continue
            names = (", ".join(n.name for n in table.nodes[:3])
                     + (" …" if len(table.nodes) > 3 else ""))
            win.track_tabs.label_table(table, names, table.source)

    def rebuild_trees_preserving_view(self):
        """Re-render both sidebar trees from the (already mutated) libraries,
        keeping expansion and selection. Widget-only: the TreeNode objects are
        reused, so every open tab's references survive."""
        win = self.win
        snap = win.playlist_panel.snapshot_view()
        win.playlist_panel.load(win.libraries, win._hidden)
        win.playlist_panel.restore_view(snap)
        win._reapply_status_icons()

    # -------------------- session restore --------------------
    def restore_tabs(self, spec: dict) -> None:
        """Rebuild the saved tab layout, filling every tab that is on screen.

        The rest carry a ``pending_restore`` and fill the first time they are
        shown (see :meth:`on_tab_focus_changed`), so launch time doesn't grow
        with the number of tabs left open on a big library. "On screen" is every
        group's current tab, not just the focused one -- a split layout shows two
        panels at once and both need their rows."""
        win = self.win
        for table, source, node_paths in win.track_tabs.restore_layout(spec):
            table.set_context(source, [])
            table.pending_restore = (source, node_paths)
        for table in win.track_tabs.visible_tables():
            self.fill_pending(table)

    def fill_pending(self, table) -> bool:
        """Populate a tab restored from the last session. A playlist that has
        since been renamed or deleted simply resolves to nothing and the tab
        stays empty, keeping the layout you left."""
        pending = getattr(table, "pending_restore", None)
        if pending is None:
            return False
        table.pending_restore = None
        source, node_paths = pending
        nodes = self.nodes_by_paths(node_paths, source)
        if not nodes:
            table.set_context(source, [])
            return True
        self.populate_table(table, nodes, source,
                            include_tombstones=True, preserve_scroll=False)
        self.win.track_tabs.label_table(
            table, ", ".join(n.name for n in nodes[:3])
            + (" …" if len(nodes) > 3 else ""), source)
        return True
