"""Playlist membership: which tracks are in a playlist, and in what order.

Every edit here is **instant and non-blocking** -- mutate the in-memory model,
refresh the views showing that playlist, and queue the file write. Deliberately
no library reload and no re-diff: that is the ~6 s freeze the cooperative reload
exists to avoid, and it would interrupt playback.

"Remove from playlist" and drag-to-add-onto-a-table act on *the table's*
membership, which is ambiguous when it shows a folder or a multi-select union
(``table.nodes`` has >1 entries) -- so those are gated to single-playlist views.
Remove-duplicates and drag-to-add-onto-a-sidebar-node are exempt: they always
come from one specific node, never a table's current display.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

from ...model.library import TRAKTOR, VDJ, path_ci


class MembershipController:
    """Extracted from MainWindow; see ``controllers/__init__.py`` for why it
    holds the window rather than its dependencies."""

    def __init__(self, win):
        self.win = win

    # -------------------- the core edit --------------------
    def edit(self, node, new_keys: list[str]):
        """Set ``node``'s playlist membership to ``new_keys``, in order.

        Instant + non-blocking: mutate the in-memory model, refresh every view of
        this playlist (scroll preserved), and write the file on the background
        queue. Deliberately does **not** reload the libraries or re-diff (that is
        the freeze), and never touches the waveform, so playback keeps going.
        A no-op if the membership is unchanged.
        """
        win = self.win
        if node is None or node.playlist is None:
            return
        if list(node.playlist.track_keys) == list(new_keys):
            return
        node.playlist.track_keys = list(new_keys)
        # A membership edit invalidates the last diff's tombstones for this path;
        # drop them so a later sidebar click doesn't resurrect grey rows.
        if win._divergence:
            win._divergence.pop(path_ci(node.node_path), None)
        # Flag the playlist as edited-but-unsynced, recording WHO edited it, and
        # repaint every tree -- cheap icon redraw, no re-diff, no playback
        # impact. Every program's copy is now stale, not just this one's.
        win._edited_paths[path_ci(node.node_path)] = node.source
        win._reapply_status_icons()
        win._refresh_playlist_everywhere(node)
        self.enqueue_write(node, list(new_keys))

    def enqueue_write(self, node, keys: list[str]):
        """Queue the file write for ``node``'s new membership. The ordered Track
        snapshot is captured now; the adapter's job renders + atomically writes
        it off the main thread."""
        win = self.win
        lib = win.libraries[node.source]
        ordered = [lib.tracks[k] for k in keys if k in lib.tracks]
        win._writer.submit(
            win.adapters[node.source].write_membership_job(node, ordered))

    def reorder(self, table, new_keys: list[str]):
        """A within-playlist drag reorder: apply the new full key order."""
        if table is None or len(table.nodes) != 1:
            return
        self.edit(table.nodes[0], new_keys)

    # -------------------- duplicate policy --------------------
    def set_allow_duplicates(self, node, allow: bool):
        """Toggle a VDJ playlist's ``noDuplicates`` policy. Turning it off
        collapses existing repeats. Always rewrites the .vdjfolder (a plain flag
        flip changes no keys, so edit()'s no-op guard wouldn't), then
        refreshes the open views and the ∞ indicator."""
        win = self.win
        if node is None or node.playlist is None or node.source != VDJ:
            return
        node.playlist.no_duplicates = not allow
        if not allow:
            seen: set[str] = set()
            deduped = []
            for k in node.playlist.track_keys:
                if k in seen:
                    continue
                seen.add(k)
                deduped.append(k)
            node.playlist.track_keys = deduped
        self.enqueue_write(node, list(node.playlist.track_keys))
        win._refresh_playlist_everywhere(node)
        win._repaint_tree(node.source)
        win.statusBar().showMessage(
            f"{node.name}: duplicate tracks {'allowed' if allow else 'disallowed'}", 4000)

    def remove_duplicates(self, node):
        """Same key (== same filepath) kept at its lowest order number; later
        repeats dropped. Always well-defined -- called from one specific
        right-clicked sidebar node, never a table's possibly-mixed display."""
        if node is None or node.playlist is None:
            return
        seen: set[str] = set()
        new_keys = []
        for k in node.playlist.track_keys:
            if k in seen:
                continue
            seen.add(k)
            new_keys.append(k)
        removed = len(node.playlist.track_keys) - len(new_keys)
        if not removed:
            self.win.statusBar().showMessage("No duplicate tracks found.", 3000)
            return
        self.edit(node, new_keys)
        self.win.statusBar().showMessage(
            f"Removed {removed} duplicate track(s) from {node.name}", 4000)

    # -------------------- removal --------------------
    def remove_keys(self, node, keys: list[str]):
        """No confirmation -- per the original spec, only the P3 collection/file
        removals are confirmed; this only ever affects playlist membership."""
        if node is None or node.playlist is None:
            return
        remove_set = set(keys)
        new_keys = [k for k in node.playlist.track_keys if k not in remove_set]
        removed = len(node.playlist.track_keys) - len(new_keys)
        if not removed:
            return
        self.edit(node, new_keys)
        self.win.statusBar().showMessage(
            f"Removed {removed} track(s) from {node.name}", 4000)

    def remove_positions(self, node, positions):
        """Remove specific ``track_keys`` indices (position-aware, so one copy of
        a duplicated track can be removed without the others)."""
        if node is None or node.playlist is None:
            return
        drop = {p for p in positions if 0 <= p < len(node.playlist.track_keys)}
        if not drop:
            return
        new_keys = [k for i, k in enumerate(node.playlist.track_keys) if i not in drop]
        self.edit(node, new_keys)
        self.win.statusBar().showMessage(
            f"Removed {len(drop)} track(s) from {node.name}", 4000)

    def remove_by_row(self, track_key: str, row: int):
        """Remove the right-clicked track. Operates on the current selection when
        the clicked row is part of it, else on just that row. For a playlist that
        permits duplicates in native order this removes the exact position(s);
        otherwise (unique keys, or a sorted view) it removes by key."""
        win = self.win
        table = win._table()
        if table is None or len(table.nodes) != 1:
            win.statusBar().showMessage("Select a single playlist to edit it.", 4000)
            return
        node = table.nodes[0]
        rows = table.selected_visual_rows()
        if row not in rows:
            rows = [row]
        if not node.playlist.no_duplicates and table._in_native_order():
            # duplicates possible + rows map straight to stored positions
            self.remove_positions(node, rows)
        else:
            keys = [table.visual_key_at(r) for r in rows]
            self.remove_keys(node, keys)

    # -------------------- adding --------------------
    def add_tracks(self, node, keys: list[str], drag_source: str, at_index: int = -1):
        """Insert ``keys`` into ``node``'s playlist. ``at_index >= 0`` splices at
        that position (Order-mode drop); otherwise appends (highest order).

        A cross-program drop is confirmed once for the whole batch; missing
        tracks are staged into the target collection in memory now and written on
        the background queue -- no busy overlay, no full reload, so the UI stays
        responsive and playback continues.
        """
        win = self.win
        if node is None or node.playlist is None or not keys:
            return
        target = node.source
        if drag_source and drag_source != target:
            target_name = win.adapters[target].display_name
            source_name = win.adapters[drag_source].display_name
            reply = QMessageBox.question(
                win, "Add tracks across programs",
                f"Add {len(keys)} track(s) from {source_name} into "
                f"{target_name}'s \"{node.name}\"?\n\nAny track not already in "
                f"{target_name}'s collection will be created there first.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
            for k in keys:
                # The target is the playlist we dropped onto, not "the other
                # program" -- with three on, letting the engine guess would mean
                # no collection entry got created at all.
                job = win.sync_engine.stage_track_in_target(k, drag_source, target)
                if job is not None:
                    win._writer.submit(job)   # in-memory add already happened

        existing = list(node.playlist.track_keys)
        add = list(keys)
        if node.playlist.no_duplicates:
            # This playlist forbids repeats: drop anything already present or
            # repeated within the drop itself.
            present = set(existing)
            deduped = []
            for k in add:
                if k in present:
                    continue
                present.add(k)
                deduped.append(k)
            skipped = len(add) - len(deduped)
            add = deduped
            if not add:
                win.statusBar().showMessage(
                    f"{node.name} does not allow duplicate tracks — nothing added.", 4000)
                return
        else:
            skipped = 0
        if 0 <= at_index <= len(existing):
            new_keys = existing[:at_index] + add + existing[at_index:]
        else:
            new_keys = existing + add
        self.edit(node, new_keys)
        note = f" ({skipped} duplicate(s) skipped)" if skipped else ""
        win.statusBar().showMessage(
            f"Added {len(add)} track(s) to {node.name}{note}", 4000)

    # -------------------- drop handlers --------------------
    def on_tracks_dropped_on_table(self, table, keys: list[str], drag_source: str,
                                   at_index: int = -1):
        if len(table.nodes) != 1:
            self.win.statusBar().showMessage(
                "Select a single playlist to edit it.", 4000)
            return
        self.add_tracks(table.nodes[0], keys, drag_source, at_index)

    def on_tracks_dropped_on_sidebar(self, node, keys: list[str], drag_source: str):
        self.add_tracks(node, keys, drag_source)      # sidebar drops always append
