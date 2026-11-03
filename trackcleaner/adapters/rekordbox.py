"""Rekordbox, through its XML exchange file.

Rekordbox's live library is an encrypted database Pioneer does not document,
so this adapter reads and writes ``rekordbox.xml`` -- the format Rekordbox itself
exports (File → Export Collection in xml format) and imports (the "rekordbox
xml" view in its browser). The round trip is therefore manual on Rekordbox's
side: export before a Refresh here, import after a sync. Nothing here touches
``master.db``.

Everything is text-surgical against that one file (``io/rekordbox_write.py``).
Playlists carry no stable identifier, so they are addressed by path, the way
Traktor's folders are.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from typing import Optional

from ..io import rekordbox_read, rekordbox_write
from ..io.rekordbox_read import REKORDBOX
from ..model import keys
from ..model.library import Library, Playlist, TreeNode
from .base import Job, LibraryAdapter, PathField


class RekordboxAdapter(LibraryAdapter):
    id = REKORDBOX
    display_name = "Rekordbox"
    collection_label = "rekordbox.xml"
    tab_bg = "#1d2a3a"
    tab_fg = "#f2f2f2"
    tab_bg_inactive = "#16202c"

    # ---------------------------------------------------------------- config
    def path_fields(self) -> list[PathField]:
        return [PathField("rekordbox", "Rekordbox XML (rekordbox.xml):",
                          "Select rekordbox.xml", "rekordbox XML (*.xml)")]

    def is_configured(self) -> bool:
        return bool(self.paths.rekordbox) and os.path.exists(self.paths.rekordbox)

    @property
    def collection_path(self) -> str:
        return self.paths.rekordbox

    # -------------------------------------------------------- the timeline
    def timeline_offset(self, stored_path: str) -> float:
        """**Not yet calibrated.** Rekordbox's stored seconds are taken as true
        audio time until a measured set of tracks says otherwise -- the same
        way the Traktor correction was established (HANDOFF.md, "Cue timing").
        Whether Rekordbox honours an MP3's encoder delay is exactly the question
        that measurement answers; it is not derivable from the file."""
        return 0.0

    # ------------------------------------------------------------------ read
    def read(self, yield_fn=None) -> Library:
        if not self.is_configured():
            return Library(source=REKORDBOX,
                           root=TreeNode("Rekordbox Playlists", "folder", REKORDBOX))
        return rekordbox_read.read(self.paths.rekordbox, yield_fn)

    # ------------------------------------------------------------- the key
    @classmethod
    def display_key(cls, track) -> str:
        """Rekordbox's own notation, which spells every key with flats."""
        return rekordbox_write.tonality(getattr(track, "key_value", None)) or \
            getattr(track, "key_musical", "")

    def key_fields(self, key_value: int) -> dict:
        return {"key": rekordbox_write.tonality(key_value)}

    # -------------------------------------------------- a read-modify-write
    def _edit(self, mutate) -> Job:
        """Read the XML, mutate it, write it back only if something changed.
        Returns whether the file was rewritten, like every other adapter."""
        path = self.paths.rekordbox

        def job() -> bool:
            content = self._load_collection(path)
            new_content, changed = mutate(content)
            if changed:
                self._store_collection(path, new_content)
            return bool(changed)

        return job

    def _edit_many(self, mutate) -> Job:
        path = self.paths.rekordbox

        def job() -> dict:
            content = self._load_collection(path)
            new_content, changed = mutate(content)
            if any(changed.values()):
                self._store_collection(path, new_content)
            return changed

        return job

    # ------------------------------------------------- playlists and folders
    def new_folder_job(self, node_path: tuple) -> Job:
        node_path = tuple(node_path)
        return self._edit(lambda c: rekordbox_write.ensure_folders(c, node_path))

    def new_playlist(self, name: str, node_path: tuple, tracks: list,
                     copy_of: Optional[TreeNode],
                     no_duplicates: Optional[bool] = None) -> tuple[Playlist, Job]:
        node_path = tuple(node_path)
        snapshot = list(tracks)
        playlist = Playlist(name=name, source=REKORDBOX,
                            track_keys=[t.key for t in snapshot],
                            no_duplicates=False)

        def mutate(content):
            # A playlist names its members by TrackID, so any track the
            # collection does not hold yet gets an entry first.
            content, ids = rekordbox_write.ensure_tracks(content, snapshot)
            return rekordbox_write.create_playlist(
                content, node_path, [ids[t.key] for t in snapshot]), True

        return playlist, self._edit(mutate)

    def relocate_playlist_job(self, node: TreeNode, new_path: tuple,
                              lib: Library) -> Optional[Job]:
        return self._relocate(tuple(node.node_path), tuple(new_path))

    def relocate_folder_job(self, old_path: tuple, new_path: tuple) -> Optional[Job]:
        return self._relocate(tuple(old_path), tuple(new_path))

    def _relocate(self, old_path: tuple, new_path: tuple) -> Optional[Job]:
        """Rename and move are one operation on a path-addressed tree: move the
        node under its new parent, then give it its new name."""
        moved = new_path[:-1] != old_path[:-1]

        def mutate(content):
            if rekordbox_write.find_node(rekordbox_write.scan_nodes(content),
                                         old_path) is None:
                return content, False
            changed = False
            at = old_path
            if moved:
                content, _ = rekordbox_write.ensure_folders(content, new_path[:-1])
                content, changed = rekordbox_write.move_node(
                    content, old_path, new_path[:-1])
                if changed:
                    at = new_path[:-1] + (old_path[-1],)
            if new_path[-1] != at[-1]:
                content, renamed = rekordbox_write.rename_node(
                    content, at, new_path[-1])
                changed = changed or renamed
            return content, changed

        return self._edit(mutate)

    def remove_job(self, node: TreeNode) -> Optional[Job]:
        path = tuple(node.node_path)
        return self._edit(lambda c: rekordbox_write.remove_node(c, path))

    # ------------------------------------------------------------ membership
    def write_membership_job(self, node: TreeNode, tracks: list) -> Job:
        path = tuple(node.node_path)
        snapshot = list(tracks)

        def mutate(content):
            content, ids = rekordbox_write.ensure_tracks(content, snapshot)
            return rekordbox_write.set_playlist(
                content, path, [(ids[t.key], t.path) for t in snapshot])

        return self._edit(mutate)

    # ---------------------------------------------------- collection entries
    def create_entries_job(self, tracks) -> Job:
        snapshot = list(tracks)
        path = self.paths.rekordbox

        def job() -> dict:
            content = self._load_collection(path)
            new_content, created = rekordbox_write.sync_tracks(content, snapshot)
            self._store_collection(path, new_content)
            return created

        return job

    def remove_entry_job(self, key: str) -> Job:
        return self._edit(lambda c: rekordbox_write.remove_track(c, key))

    def read_tracks(self, by_key: dict) -> dict:
        if not by_key:
            return {}
        content = self._load_collection(self.paths.rekordbox)
        out = {}
        for key, block in rekordbox_write.find_tracks(content, by_key).items():
            try:
                track = rekordbox_read.track_from_element(ET.fromstring(block))
            except ET.ParseError:
                continue
            if track is not None:
                out[key] = track
        return out

    def repath_tracks_job(self, by_key: dict) -> Job:
        moves = dict(by_key)
        return self._edit_many(lambda c: rekordbox_write.repath_tracks(c, moves))

    def write_metadata_jobs(self, by_key: dict, paths: dict | None = None) -> Job:
        # ``paths`` is an accelerator this writer does not need: a 20,000-track
        # rekordbox.xml is walked in well under a second.
        snapshot = {k: dict(v) for k, v in by_key.items()}
        return self._edit_many(
            lambda c: rekordbox_write.set_tracks_metadata(c, snapshot))
