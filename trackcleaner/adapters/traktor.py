"""Traktor: one ``collection.nml`` holding both the collection and the playlist
tree.

Everything is text-surgical against that single file: a playlist is a ``<NODE
TYPE="PLAYLIST">`` inside the ``<PLAYLISTS>`` tree, matched by its **UUID** --
the one identifier that survives a rename -- and folders are matched by path
because they carry no UUID at all.
"""

from __future__ import annotations

import os
import uuid as uuidlib
import xml.etree.ElementTree as ET
from typing import Optional

from ..io import traktor_read, traktor_write
from ..io.write_queue import atomic_write_text, atomic_write_xml
from ..model import keys, matching
from ..model.cues import CORRECTION_OFFSET_SEC, classify
from ..model.library import Library, Playlist, TRAKTOR, TreeNode
from .base import Job, LibraryAdapter, PathField


class TraktorAdapter(LibraryAdapter):
    id = TRAKTOR
    display_name = "Traktor"
    collection_label = "collection.nml"
    logo_file = "traktor.png"
    tab_bg = "#6a6a6a"
    tab_fg = "#22d3ee"
    tab_bg_inactive = "#4f4f4f"

    # ---------------------------------------------------------------- config
    def path_fields(self) -> list[PathField]:
        return [PathField("nml", "Traktor collection.nml:", "Select collection.nml",
                          "collection.nml (*.nml)")]

    def is_configured(self) -> bool:
        return bool(self.paths.nml) and os.path.exists(self.paths.nml)

    @property
    def collection_path(self) -> str:
        return self.paths.nml

    # -------------------------------------------------------- the timeline
    def timeline_offset(self, stored_path: str) -> float:
        """Traktor's stored numbers run **51.536 ms late** on a file whose
        container carries a decoder-honoured delay (Class A), so reaching the
        canonical audio position means subtracting it. Class B and unknown pass
        through -- an unknown file is never shifted, deliberately.

        The class lives only in the audio container, never in either database
        (HANDOFF.md, "Cue timing"), which is why this needs the start-time cache
        and cannot be answered from the NML alone. Verified constant.
        """
        if self.start_times is None:
            return 0.0
        local = matching.resolve_local_path(stored_path)
        if classify(self.start_times.get(local)) == "A":
            return -CORRECTION_OFFSET_SEC
        return 0.0

    # ------------------------------------------------------------------ read
    def read(self, yield_fn=None) -> Library:
        if not self.is_configured():
            return Library(source=TRAKTOR,
                           root=TreeNode("Traktor Playlists", "folder", TRAKTOR))
        return traktor_read.read(self.paths.nml, yield_fn)

    # ------------------------------------------------------------- the key
    @classmethod
    def display_key(cls, track) -> str:
        """Open Key ("8d"/"11m"), which is what Traktor shows. Its stored
        ``INFO/@KEY`` text is not: on a real collection it holds musical notation
        on some entries and nothing at all on others, while Traktor itself
        displays from the analysed ``MUSICAL_KEY`` number every time."""
        return keys.open_key(getattr(track, "key_value", None)) or \
            getattr(track, "key_musical", "")

    # -------------------------------------------------- a read-modify-write
    def _edit(self, mutate) -> Job:
        """Every Traktor write is the same shape: read the NML, hand it to
        ``mutate``, write it back atomically only if something changed.

        **Returns whether the file was actually rewritten.** A mutate that
        matched nothing answers False, so a caller can tell "done" from "there
        was nothing there" instead of assuming success -- which is how an
        unwritten tag came to show as synced.
        """
        nml_path = self.paths.nml

        def job() -> bool:
            content = self._load_collection(nml_path)
            result = mutate(content)
            if result is None:
                return False
            new_content, changed = result
            if changed:
                self._store_collection(nml_path, new_content)
            return bool(changed)

        return job

    def _edit_many(self, mutate) -> Job:
        """:meth:`_edit` for a mutation that answers per key rather than with one
        boolean. Written back only if something changed, so a run whose writes
        all missed leaves the file alone."""
        nml_path = self.paths.nml

        def job() -> dict:
            content = self._load_collection(nml_path)
            new_content, changed = mutate(content)
            if any(changed.values()):
                self._store_collection(nml_path, new_content)
            return changed

        return job

    # ------------------------------------------------- playlists and folders
    def new_folder_job(self, node_path: tuple) -> Job:
        return self._edit(
            lambda c: (traktor_write.ensure_folder_chain(c, node_path), True))

    def new_playlist(self, name: str, node_path: tuple, tracks: list,
                     copy_of: Optional[TreeNode],
                     no_duplicates: Optional[bool] = None) -> tuple[Playlist, Job]:
        # Always a fresh UUID, even for a duplicate: every writer that touches a
        # playlist matches on it, so two nodes sharing one would make membership,
        # rename and move all hit whichever twin they found first.
        uid = uuidlib.uuid4().hex
        playlist = Playlist(name=name, source=TRAKTOR,
                            track_keys=[t.key for t in tracks],
                            traktor_uuid=uid, no_duplicates=False)
        folder_names = tuple(node_path)[:-1]
        job = self._edit(lambda c: (traktor_write.create_playlist_node(
            c, folder_names, name, uid, tracks), True))
        return playlist, job

    def relocate_playlist_job(self, node: TreeNode, new_path: tuple,
                              lib: Library) -> Optional[Job]:
        uid = node.playlist.traktor_uuid
        if not uid:
            self._fail(f"{node.name}: no playlist UUID, cannot move or rename.")
            return None
        old_path = tuple(node.node_path)
        name = new_path[-1]
        moved = new_path[:-1] != old_path[:-1]

        def mutate(content):
            changed = False
            if moved:
                content, changed = traktor_write.move_playlist_node(
                    content, uid, old_path[:-1], new_path[:-1])
            if name != old_path[-1]:
                content, renamed = traktor_write.rename_playlist_node(
                    content, uid, name)
                changed = changed or renamed
            return content, changed

        return self._edit(mutate)

    def relocate_folder_job(self, old_path: tuple, new_path: tuple) -> Optional[Job]:
        moved = new_path[:-1] != old_path[:-1]
        name = new_path[-1]

        def mutate(content):
            changed = False
            at = old_path
            if moved:
                content, changed = traktor_write.move_folder_node(
                    content, old_path, new_path[:-1])
                if changed:
                    at = new_path[:-1] + (old_path[-1],)
            if name != at[-1]:
                content, renamed = traktor_write.rename_folder_node(
                    content, at, name)
                changed = changed or renamed
            return content, changed

        return self._edit(mutate)

    def remove_job(self, node: TreeNode) -> Optional[Job]:
        path = tuple(node.node_path)
        is_folder = node.is_folder()
        uid = node.playlist.traktor_uuid if node.playlist is not None else ""
        if not is_folder and not uid:
            self._fail(f"{node.name}: no playlist UUID, cannot remove.")
            return None

        def mutate(content):
            if is_folder:
                return traktor_write.remove_folder_node(content, path)
            return traktor_write.remove_playlist_node(content, uid, path[:-1])

        return self._edit(mutate)

    # ------------------------------------------------------------ membership
    def can_write_membership(self, node: TreeNode) -> bool:
        # The UUID is the only handle the writer has; without one there is no
        # node to edit and a caller should create the playlist instead.
        return bool(node.playlist is not None and node.playlist.traktor_uuid)

    def write_membership_job(self, node: TreeNode, tracks: list) -> Job:
        uuid = node.playlist.traktor_uuid
        return self._edit(lambda c: (
            traktor_write.set_playlist_entries(c, uuid, tracks), True))

    # ---------------------------------------------------- collection entries
    def create_entries_job(self, tracks) -> Job:
        nml_path = self.paths.nml
        snapshot = list(tracks)

        def job() -> dict:
            content = self._load_collection(nml_path)
            new_content, created = traktor_write.sync_tracks_into_nml(
                content, snapshot)
            self._store_collection(nml_path, new_content)
            return created

        return job

    def remove_entry_job(self, key: str) -> Job:
        return self._edit(lambda c: traktor_write.remove_entry_from_nml(c, key))

    def read_tracks(self, by_key: dict) -> dict:
        """Locate these entries in the NML and parse each on its own.

        ``locate_entries`` returns ``None`` for anything it will not vouch for --
        a name shared by too many folders, two entries claiming one path, a miss
        it cannot prove is an absence. That answer is respected here rather than
        worked around: no entry is reported, so the caller falls back to what it
        would have done without a targeted read.
        """
        if not by_key:
            return {}
        content = self._load_collection(self.paths.nml)
        found = traktor_write.locate_entries(content, dict(by_key))
        if not found:
            return {}
        out = {}
        for key, match in found.items():
            try:
                entry = ET.fromstring(match.group(0))
            except ET.ParseError:
                continue
            track = traktor_read._track_from_entry(entry)
            if track is not None:
                out[key] = track
        return out

    def repath_tracks_job(self, by_key: dict) -> Job:
        return self._edit_many(
            lambda c: traktor_write.repath_entries_in_nml(c, dict(by_key)))

    def write_playcount_jobs(self, by_key: dict) -> Job:
        return self._edit_many(
            lambda c: traktor_write.set_entries_playcount(c, dict(by_key)))

    def write_metadata_jobs(self, by_key: dict, paths: dict | None = None) -> Job:
        snapshot = dict(paths) if paths else None
        return self._edit_many(
            lambda c: traktor_write.set_entries_metadata(c, dict(by_key),
                                                        snapshot))

    def key_fields(self, key_value: int) -> dict:
        # Both, and they must agree: Traktor reads MUSICAL_KEY and everything
        # else reads INFO/@KEY.
        return {"key": keys.open_key(key_value), "key_value": str(key_value)}
