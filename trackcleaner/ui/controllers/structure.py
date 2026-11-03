"""Sidebar tree structure: create, duplicate, rename, move and remove nodes.

Everything here writes through the same background queue as a membership edit,
and everything except removal deliberately skips ``_guarded_write``: creating is
additive, renaming and moving are reversible, and none of them touches a track,
a collection entry or an audio file. A modal confirmation would only interrupt
the flow of naming the row you just made.

Nothing here reloads or re-diffs either. The ``TreeNode`` objects are mutated in
place, so open tabs keep their references and playback never stutters; a move or
a removal rebuilds the tree *widgets* only.

Rename and move are the **same operation** on the VirtualDJ side -- the name is
the filename, so both are just "the path changed" -- which is why they share
``relocate_playlist_job`` / ``relocate_folder_job``.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

from ...model.library import (VDJ, TreeNode, can_move_into, find_node, path_ci,
                              repath)


class StructureController:
    """Extracted from MainWindow; see ``controllers/__init__.py`` for why it
    holds the window rather than its dependencies."""

    def __init__(self, win):
        self.win = win

    # -------------------- naming / placement --------------------
    def resolve_name(self, source: str, parent, name: str, node=None) -> str:
        """A name that is safe to write and unique among ``parent``'s children.

        What "safe" means is the adapter's business -- a name becomes a filename
        where playlists are files, and an XML attribute where they are not.
        Collisions auto-suffix " (2)", " (3)", … -- folders count too, because a
        file-backed playlist also collides with a same-named directory.
        ``node`` (when renaming) is excluded so a playlist never collides with
        itself.
        """
        name = (name or "").strip()
        adapter = self.win.adapters.get(source)
        if adapter is not None:
            name = adapter.sanitize_node_name(name)
        name = name.strip() or "New playlist"
        taken = {c.name.lower() for c in (parent.children if parent is not None else ())
                 if c is not node}
        if name.lower() not in taken:
            return name
        n = 2
        while f"{name} ({n})".lower() in taken:
            n += 1
        return f"{name} ({n})"

    def parent_for(self, source: str, node):
        """The folder a new sibling/child of ``node`` belongs in: a folder takes
        it inside, a leaf takes it alongside. ``None`` means the tree root, which
        is not itself a row."""
        win = self.win
        lib = win.libraries[source]
        if lib is None or lib.root is None or node is None:
            return None
        # is_folder(), not "has no playlist": VDJ has folder nodes that are also
        # their own list, and those take a new child inside them.
        parent = node if node.is_folder() else find_node(
            lib.root, tuple(node.node_path)[:-1])
        return None if parent is lib.root else parent

    # -------------------- starting an in-place edit --------------------
    def begin_new(self, source: str, node, kind: str):
        """Put a placeholder row in the tree and start editing its name.

        The commit signal only carries (source, parent, name), so *what* is being
        created is remembered on the window and read back in :meth:`create`."""
        win = self.win
        lib = win.libraries[source]
        if lib is None or lib.root is None:
            return
        win._pending_create = {"kind": kind, "copy_of": None}
        label = "New folder" if kind == "folder" else "New playlist"
        win.playlist_panel.tree_for(source).begin_new_node(
            self.parent_for(source, node), label)

    def begin_duplicate(self, source: str, node):
        """Duplicate is the creation path with the source playlist's tracks: the
        row opens pre-filled with the free sibling name, so Enter accepts it and
        Escape leaves no stray copy (nothing is written until the commit)."""
        win = self.win
        lib = win.libraries[source]
        if node is None or node.playlist is None or lib is None or lib.root is None:
            return
        parent = self.parent_for(source, node)
        suggested = self.resolve_name(
            source, parent if parent is not None else lib.root, node.name)
        win._pending_create = {"kind": "playlist", "copy_of": node}
        win.playlist_panel.tree_for(source).begin_new_node(parent, suggested)

    def begin_rename(self, source: str, node):
        if node is None:
            return
        self.win.playlist_panel.tree_for(source).begin_rename(node)

    # -------------------- creating --------------------
    def create(self, source: str, parent_node, name: str):
        """Commit a New playlist / New folder / Duplicate edit: build the model
        objects, queue the file write, and bind the placeholder row that is
        already on screen. What to build comes from ``win._pending_create``, set
        when the edit started."""
        win = self.win
        tree = win.playlist_panel.tree_for(source)
        lib = win.libraries[source]
        if lib is None or lib.root is None:
            tree.discard_pending()
            return
        adapter = win.adapters[source]
        if not adapter.is_configured():
            tree.discard_pending()
            win.statusBar().showMessage(
                f"{adapter.display_name} is not configured — set its paths under "
                "File → Paths first.", 6000)
            return

        spec = getattr(win, "_pending_create", None) or {"kind": "playlist"}
        kind = spec.get("kind", "playlist")
        copy_of = spec.get("copy_of")
        parent = parent_node if parent_node is not None else lib.root
        name = self.resolve_name(source, parent, name)
        node_path = tuple(parent.node_path) + (name,)

        if kind == "folder":
            node = self.build_folder(source, name, node_path)
        else:
            node = self.build_playlist(source, name, node_path, copy_of, lib)
        parent.children.append(node)
        tree.bind_new_node(node)
        win._repaint_tree(source)
        if node.playlist is not None:
            tree.select_node(node)   # shows the new playlist in the focused tab
        what = "folder" if kind == "folder" else "playlist"
        win.statusBar().showMessage(f"Created {what} “{name}”", 4000)

    def build_folder(self, source: str, name: str, node_path: tuple) -> TreeNode:
        """Model node + queued write for a new, empty folder."""
        win = self.win
        win._writer.submit(win.adapters[source].new_folder_job(node_path))
        return TreeNode(name=name, kind="folder", source=source, node_path=node_path)

    def build_playlist(self, source: str, name: str, node_path: tuple,
                       copy_of, lib) -> TreeNode:
        """Model node + queued write for a new playlist. When ``copy_of`` is
        given this is a Duplicate: same tracks in the same order, same duplicate
        policy. The adapter owns the program-specific bits of the Playlist -- a
        Traktor UUID, a VirtualDJ file path -- because the caller must not invent
        them."""
        win = self.win
        keys = list(copy_of.playlist.track_keys) if copy_of is not None else []
        ordered = [lib.tracks[k] for k in keys if k in lib.tracks]

        playlist, job = win.adapters[source].new_playlist(
            name, node_path, ordered, copy_of)
        win._writer.submit(job)
        return TreeNode(name=name, kind="playlist", source=source,
                        playlist=playlist, node_path=node_path)

    # -------------------- renaming --------------------
    def rename(self, source: str, node, name: str):
        """Commit a rename. Text-surgical on the Traktor side (a playlist is
        matched by UUID -- the one identifier a rename doesn't change; a folder
        by path, since folders have none) and a plain file/directory move on the
        VDJ side, so the contents carry across untouched."""
        win = self.win
        if node is None:
            return
        lib = win.libraries[source]
        parent = find_node(lib.root, tuple(node.node_path)[:-1]) if lib else None
        name = self.resolve_name(source, parent, name, node=node)
        if name == node.name:
            return                       # unchanged -- don't touch the file at all
        old_path = tuple(node.node_path)
        new_path = old_path[:-1] + (name,)

        job = (self.relocate_folder_job(source, old_path, new_path)
               if node.is_folder()
               else self.relocate_playlist_job(source, node, new_path, lib))
        if job is None:
            return

        win._writer.submit(job)
        node.name = name
        if node.playlist is not None:
            node.playlist.name = name
        # repath, not a single assignment: a folder rename moves every
        # descendant's path too, and node_path is what the caches key off.
        repath(node, old_path[:-1])
        self.migrate_paths(source, old_path, new_path)
        tree = win.playlist_panel.tree_for(source)
        tree.set_node_text(node)
        win._repaint_tree(source)        # re-adds the ∞ prefix and the square
        win._relabel_tabs_for(node)
        win.statusBar().showMessage(f"Renamed to “{name}”", 4000)

    # -------------------- the relocate jobs (rename AND move) --------------------
    def relocate_playlist_job(self, source: str, node, new_path: tuple, lib):
        """The queued write that puts ``node``'s playlist at ``new_path``. Rename
        and move are the same operation -- on VirtualDJ the name IS the filename,
        so both are just "the path changed"; the adapter knows which."""
        win = self.win
        adapter = win.adapters[source]
        job = adapter.relocate_playlist_job(node, new_path, lib)
        if job is None:
            win.statusBar().showMessage(adapter.last_error(), 6000)
        return job

    def relocate_folder_job(self, source: str, old_path: tuple, new_path: tuple):
        """The queued write that puts the folder at ``old_path`` at ``new_path``
        -- a rename, a move, or both."""
        return self.win.adapters[source].relocate_folder_job(old_path, new_path)

    # -------------------- the path-keyed caches --------------------
    def migrate_paths(self, source: str, old_path: tuple, new_path: tuple) -> None:
        """Carry the path-keyed caches across a rename/move of a whole subtree.

        ``_hidden`` matters most: it is persisted and keyed by the *exact*
        node_path (``LibraryTree._build`` compares it verbatim), so without this
        a hidden playlist silently reappears when its folder moves.
        ``_edited_paths`` and ``_divergence`` are keyed by ``path_ci`` instead --
        hence the two forms below. Divergence markers are dropped rather than
        remapped: they described a comparison against the old path."""
        win = self.win
        n = len(old_path)
        old_ci, new_t = path_ci(old_path), tuple(new_path)
        hidden = win._hidden.get(source, set())
        for path in [p for p in hidden if path_ci(p)[:n] == old_ci]:
            hidden.discard(path)
            hidden.add(new_t + tuple(path)[n:])
        for key in [k for k in win._edited_paths if k[:n] == old_ci]:
            win._edited_paths[path_ci(new_t) + key[n:]] = win._edited_paths.pop(key)
        if win._divergence:
            for key in [k for k in win._divergence if k[:n] == old_ci]:
                win._divergence.pop(key, None)

    def forget_paths(self, source: str, path: tuple) -> None:
        """Drop the path-keyed caches for a removed subtree -- the mirror of
        :meth:`migrate_paths`. ``_hidden`` is persisted and keyed by the exact
        node_path, so without this a removed folder leaves its children behind in
        the hidden set forever."""
        win = self.win
        n = len(path)
        path_lower = path_ci(path)
        hidden = win._hidden.get(source, set())
        for p in [p for p in hidden if path_ci(p)[:n] == path_lower]:
            hidden.discard(p)
        for key in [k for k in win._edited_paths if k[:n] == path_lower]:
            win._edited_paths.pop(key, None)
        if win._divergence:
            for key in [k for k in win._divergence if k[:n] == path_lower]:
                win._divergence.pop(key, None)

    # -------------------- moving --------------------
    def move(self, source: str, nodes: list, target):
        """A drag inside one library tree: re-parent the dragged nodes under
        ``target`` (``None`` = that software's root).

        The file/NML side is a cut-and-paste, so a playlist keeps its UUID and a
        folder keeps its whole subtree. Nothing here reloads or re-diffs: the
        TreeNode objects are mutated in place, so open tabs keep working, and only
        the tree *widgets* are rebuilt."""
        win = self.win
        lib = win.libraries[source]
        if lib is None or lib.root is None:
            return
        dest = target if target is not None else lib.root
        moved = 0
        for node in list(nodes):
            if not can_move_into(node, None if dest is lib.root else dest):
                continue
            old_path = tuple(node.node_path)
            old_parent = find_node(lib.root, old_path[:-1]) or lib.root
            name = self.resolve_name(source, dest, node.name, node=node)
            new_path = tuple(dest.node_path) + (name,)

            job = (self.relocate_folder_job(source, old_path, new_path)
                   if node.is_folder()
                   else self.relocate_playlist_job(source, node, new_path, lib))
            if job is None:
                continue
            win._writer.submit(job)

            node.name = name
            if node.playlist is not None:
                node.playlist.name = name
            if node in old_parent.children:
                old_parent.children.remove(node)
            dest.children.append(node)
            repath(node, dest.node_path)
            self.migrate_paths(source, old_path, new_path)
            moved += 1

        if not moved:
            return
        # A move changes the tree's *structure*, so binding one row isn't enough
        # -- but this rebuilds the widgets only, over a few hundred rows. It is
        # not a library re-read and not a re-diff, so audio never stutters.
        win._rebuild_trees_preserving_view()
        where = target.name if target is not None else "the top level"
        win.statusBar().showMessage(f"Moved {moved} item(s) to {where}", 4000)

    # -------------------- removing --------------------
    def _removal_targets(self, source: str, node) -> list:
        """The nodes a Remove acts on: the whole sidebar selection when the
        right-clicked node is part of it, otherwise just that node.

        The same rule ``_sync_selected`` uses -- right-click deliberately does
        not change the selection, so acting only on the node under the cursor
        would silently ignore the other rows the user had highlighted.

        A node inside another selected folder is dropped: removing the folder
        takes it anyway, and removing it twice would look for a parent that is
        already gone.
        """
        tree = self.win.playlist_panel.tree_for(source)
        selected = tree.selected_top_nodes()
        if node is not None and not any(n is node for n in selected):
            selected = [node]
        paths = {tuple(n.node_path) for n in selected}
        return [n for n in selected
                if not any(tuple(n.node_path)[:len(p)] == p and tuple(n.node_path) != p
                           for p in paths)]

    def remove(self, source: str, node):
        """Delete playlists/folders (contents and all) from one program.

        Confirmed but NOT backed up: this removes playlist/folder entries only --
        no track, no collection entry and no audio file is touched, so the
        whole-library snapshot that guards the Phase-3 collection ops isn't
        warranted. On VirtualDJ the files go to the Recycle Bin and stay
        recoverable; a Traktor removal is a text edit inside collection.nml, whose
        recovery path is Revert to backup.
        """
        win = self.win
        lib = win.libraries[source]
        if node is None or lib is None or lib.root is None:
            return
        targets = self._removal_targets(source, node)
        if not targets:
            return
        if QMessageBox.warning(win, "Remove", self._removal_message(source, targets),
                               QMessageBox.Yes | QMessageBox.No,
                               QMessageBox.No) != QMessageBox.Yes:
            return

        removed = []
        for target in targets:
            job = self.remove_job(source, target)
            if job is None:
                continue          # the adapter said why; keep going with the rest
            win._writer.submit(job)

            path = tuple(target.node_path)
            parent = find_node(lib.root, path[:-1]) or lib.root
            if target in parent.children:
                parent.children.remove(target)
            win._clear_tabs_for(list(target.iter_playlists()))
            self.forget_paths(source, path)
            removed.append(target)
        if not removed:
            return
        win._rebuild_trees_preserving_view()
        if len(removed) == 1:
            what = "folder" if removed[0].is_folder() else "playlist"
            win.statusBar().showMessage(f"Removed {what} “{removed[0].name}”", 4000)
        else:
            win.statusBar().showMessage(f"Removed {len(removed)} items", 4000)

    def _removal_message(self, source: str, targets: list) -> str:
        """One confirmation covering everything that is about to go, naming it --
        "Remove 3 items?" alone would not let you check it is the right three."""
        win = self.win
        if len(targets) == 1:
            node = targets[0]
            inside = [n for n in node.iter_playlists() if n is not node]
            if node.is_folder():
                detail = (f" and the {len(inside)} playlist(s) inside it" if inside
                          else " (it is empty)")
                msg = f"Remove folder “{node.name}”{detail}?"
            else:
                msg = f"Remove playlist “{node.name}”?"
            msg += "\n\nThe tracks themselves are not affected — only this "
            msg += "folder." if node.is_folder() else "playlist."
        else:
            names = ", ".join(n.name for n in targets[:6])
            if len(targets) > 6:
                names += f", … (+{len(targets) - 6} more)"
            folders = sum(1 for n in targets if n.is_folder())
            nested = sum(len([p for p in n.iter_playlists() if p is not n])
                         for n in targets if n.is_folder())
            msg = f"Remove these {len(targets)} items?\n\n{names}"
            if folders:
                msg += (f"\n\n{folders} of them are folders, taking {nested} "
                        f"playlist(s) inside them.")
            msg += "\n\nThe tracks themselves are not affected — only these entries."
        adapter = win.adapters.get(source)
        if adapter is not None and adapter.playlists_are_files:
            msg += " The file(s) go to the Recycle Bin."
        return msg

    def remove_job(self, source: str, node):
        """The queued write that deletes ``node`` from its program's storage."""
        win = self.win
        adapter = win.adapters[source]
        job = adapter.remove_job(node)
        if job is None:
            win.statusBar().showMessage(adapter.last_error(), 6000)
        return job
