"""Phase-3 collection operations: removing entries, deleting files, importing.

These are the destructive writes, and the only ones that go through
:meth:`CollectionController.guarded_write` -- confirm, warn if VirtualDJ is open,
snapshot the whole library, *then* write. Playlist-membership edits and the
sidebar's structure operations deliberately do not: they touch no track and no
audio file.
"""

from __future__ import annotations

import os

from PySide6.QtWidgets import QMessageBox

from ...io import backup, probe, recycle
from ...model import matching
# No program constants: every program-specific fact comes from its adapter.
from ...model.library import Track


class CollectionController:
    """Extracted from MainWindow; see ``controllers/__init__.py`` for why it
    holds the window rather than its dependencies."""

    def __init__(self, win):
        self.win = win

    # -------------------- the guard --------------------
    def guarded_write(self, title: str, msg: str, writes_vdj: bool) -> bool:
        """Confirm a destructive/collection write, warn about any program that
        could clobber it on exit, and snapshot a whole-library backup first.
        Returns True to proceed.

        ``writes_vdj`` is kept as the parameter name because ~20 call sites and
        tests use it; it means "this write can be undone by a program rewriting
        its own database on exit", and every such program is now checked rather
        than VirtualDJ specifically.
        """
        win = self.win
        if QMessageBox.warning(win, title, msg,
                               QMessageBox.Yes | QMessageBox.No,
                               QMessageBox.No) != QMessageBox.Yes:
            return False
        # Ask the adapters, not the OS: only they know whether their program
        # rewrites its database on exit, and how to tell if it is open.
        if writes_vdj:
            for adapter in win.adapters.values():
                if not (adapter.clobbers_on_exit and adapter.is_running()):
                    continue
                name = adapter.display_name
                if QMessageBox.warning(
                        win, f"{name} is running",
                        f"{name} appears to be open. It rewrites its database on "
                        "exit and can overwrite these changes. Close it before "
                        "continuing.\n\nContinue anyway?",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No) != QMessageBox.Yes:
                    return False
        try:
            backup.snapshot(win.settings.paths)
        except OSError:
            pass
        return True

    # -------------------- queued collection writes --------------------
    def enqueue_entry_removal(self, source: str, key: str):
        """Queue the write that deletes ``key``'s collection entry from the
        given program."""
        self.win._writer.submit(self.win.adapters[source].remove_entry_job(key))

    def enqueue_entry_creation(self, source: str, track: Track):
        """Queue the write that appends ``track`` to the given program's
        collection (the adapter reuses its sync writer's append-when-absent
        path)."""
        self.win._writer.submit(self.win.adapters[source].create_entry_job(track))

    def strip_from_all_playlists(self, lib, key: str):
        """Remove ``key`` from every playlist in ``lib`` (in-memory + queued file
        writes), so deleting its collection entry leaves no dangling references."""
        for node in list(lib.playlists_containing(key)):
            self.win._edit_membership(
                node, [k for k in node.playlist.track_keys if k != key])

    def program_list(self, ids) -> str:
        """"Traktor and VirtualDJ", or "A, B and C" -- so a confirmation names
        exactly the programs it is about to write to."""
        names = [self.win.adapters[a].display_name for a in ids if a in self.win.adapters]
        if not names:
            return "no programs"
        if len(names) == 1:
            return names[0]
        return f"{', '.join(names[:-1])} and {names[-1]}"

    @staticmethod
    def track_label(track, fallback: str) -> str:
        if track is not None and (track.title or track.artist):
            return f"{track.artist} — {track.title}".strip(" —")
        return fallback

    # -------------------- the operations --------------------
    def remove_with_entry(self, track_key: str):
        """Remove a track from ALL of the current program's playlists and delete
        its collection entry (the other program is left untouched)."""
        win = self.win
        source = win._current_source
        lib = win.libraries[source]
        track = lib.tracks.get(track_key)
        adapter = win.adapters[source]
        prog, coll = adapter.display_name, adapter.collection_label
        label = self.track_label(track, track_key)
        if not self.guarded_write(
                "Remove from collection",
                f"Remove “{label}” from ALL {prog} playlists and delete its "
                f"{coll} entry?\n\nA backup is made first, so this is recoverable.",
                writes_vdj=win.adapters[source].clobbers_on_exit):
            return
        self.strip_from_all_playlists(lib, track_key)
        self.enqueue_entry_removal(source, track_key)
        win._writer.flush()
        lib.tracks.pop(track_key, None)
        win._reload_preserving_view()
        win.statusBar().showMessage(f"Removed “{label}” from {prog}", 5000)

    def delete_file(self, track_key: str):
        """Send the audio file to the Recycle Bin and strip the track from EVERY
        loaded program (all playlists + every collection)."""
        win = self.win
        # Every loaded program, not a hardcoded two: leaving a third program's
        # entries behind would point them at a file that no longer exists.
        loaded = [(aid, lib) for aid, lib in win.libraries.items() if lib is not None]
        track = next((lib.tracks[track_key] for _aid, lib in loaded
                      if track_key in lib.tracks), None)
        if track is None:
            return
        path = track.path
        label = self.track_label(track, os.path.basename(path))
        names = self.program_list(aid for aid, _lib in loaded)
        if not self.guarded_write(
                "Delete file",
                f"Send this file to the Recycle Bin and remove “{label}” from "
                f"{names} (all playlists + every collection)?\n\n{path}\n\n"
                f"The audio file moves to the Recycle Bin (recoverable). A library "
                f"backup is made first.",
                writes_vdj=True):
            return
        for source, lib in loaded:
            self.strip_from_all_playlists(lib, track_key)
            self.enqueue_entry_removal(source, track_key)
        win._writer.flush()
        for _source, lib in loaded:
            lib.tracks.pop(track_key, None)
        recycled = recycle.send_to_recycle_bin(path)
        win._reload_preserving_view()
        if recycled:
            win.statusBar().showMessage(
                f"Deleted to Recycle Bin: {os.path.basename(path)}", 6000)
        else:
            QMessageBox.warning(
                win, "Delete file",
                "The track was removed from every library, but the file could not "
                f"be moved to the Recycle Bin (missing or in use):\n{path}")

    def import_files(self, node, paths: list[str], at_index: int = -1):
        """Import audio files into ``node``'s program: create a collection entry
        for any not already known (ffprobe metadata), then add them to the
        playlist. Files already in the collection are just added."""
        win = self.win
        if node is None or node.playlist is None or not paths:
            return
        source = node.source
        lib = win.libraries[source]
        keys: list[str] = []
        new_tracks: list[Track] = []
        for p in paths:
            if os.path.splitext(p)[1].lower() not in probe.AUDIO_EXTS:
                continue
            key = matching.path_key(p)
            keys.append(key)
            if key not in lib.tracks and all(t.key != key for t in new_tracks):
                t = probe.probe_track(p, source) or Track(
                    key=key, path=p, source=source, title=os.path.basename(p), pois=[])
                new_tracks.append(t)
        if not keys:
            win.statusBar().showMessage("No audio files to import.", 4000)
            return
        prog = win.adapters[source].display_name
        if new_tracks and not self.guarded_write(
                "Import files",
                f"Import {len(new_tracks)} new file(s) into {prog}'s collection and add "
                f"{len(keys)} track(s) to “{node.name}”?",
                writes_vdj=win.adapters[source].clobbers_on_exit):
            return
        for t in new_tracks:
            lib.tracks[t.key] = t
            self.enqueue_entry_creation(source, t)
        existing = list(node.playlist.track_keys)
        if 0 <= at_index <= len(existing):
            merged = existing[:at_index] + keys + existing[at_index:]
        else:
            merged = existing + keys
        win._edit_membership(node, merged)
        if new_tracks:
            win._writer.flush()
            win._reload_preserving_view()
        win.statusBar().showMessage(f"Imported {len(keys)} track(s) into {node.name}", 5000)

    # -------------------- drop handlers --------------------
    def on_files_dropped_on_table(self, table, paths: list[str], at_index: int = -1):
        if len(table.nodes) != 1:
            self.win.statusBar().showMessage(
                "Drop files onto a single playlist to import.", 4000)
            return
        self.import_files(table.nodes[0], paths, at_index)

    def on_files_dropped_on_sidebar(self, node, paths: list[str]):
        self.import_files(node, paths)
