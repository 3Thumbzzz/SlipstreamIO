"""SlipStream itself, as a program.

Modelling our own library as just another adapter means its playlists get the
whole sidebar for free -- new/duplicate/rename/move/remove, drag, hide, session
restore, the Programs menu -- with no special-casing anywhere. The ``.slp`` file
is simply its database.

Two things differ from a vendor adapter, both because we own the format
outright:

* **Whole-document writes.** The surgical rule exists to protect data we do not
  understand; there is none of that here, so each job re-renders the file. It
  still reads-modifies-writes inside the job, so queued edits compose correctly.
* **It is always configured.** A missing ``.slp`` is not an error, it is an
  empty library that the first write creates.
"""

from __future__ import annotations

import os
from typing import Optional

from ..model import keys
from ..model.library import Library, Playlist, TreeNode
from ..native import slp
from .base import Job, LibraryAdapter, PathField


class SlipStreamAdapter(LibraryAdapter):
    id = slp.SLIPSTREAM
    display_name = "SlipStream"
    collection_label = "the .slp library"
    logo_file = "Slipstream.png"
    # Distinct from Traktor's grey and VirtualDJ's red, so a tab reads at a
    # glance as "mine" rather than a vendor's.
    tab_bg = "#3f6f5f"
    tab_fg = "#d8f5e8"
    tab_bg_inactive = "#2c4c42"

    # ---------------------------------------------------------------- config
    def path_fields(self) -> list[PathField]:
        return [PathField("slp", "SlipStream library (.slp):",
                          "SlipStream library", "SlipStream library (*.slp)",
                          must_exist=False)]

    def is_configured(self) -> bool:
        # Only a location is needed; the file itself is created on first write.
        return bool(self.paths.slp)

    @property
    def collection_path(self) -> str:
        return self.paths.slp

    # ------------------------------------------------------------------ read
    def read(self, yield_fn=None) -> Library:
        return slp.read(self.paths.slp, yield_fn)

    # ------------------------------------------- read-modify-write, whole doc
    # This one buffers the parsed document rather than text, so a session saves
    # the same parse-once/write-once as the text formats get.
    def _read_collection_file(self, path: str):
        self.collection_reads += 1
        return slp.load_doc(path)

    def _write_collection_file(self, path: str, root) -> None:
        self.collection_writes += 1
        slp.save_doc(path, root)

    def _edit(self, mutate) -> Job:
        """Load the .slp, hand it to ``mutate``, save unless it answered False.
        **Returns whether it was saved** -- see ``TraktorAdapter._edit``."""
        path = self.paths.slp

        def job() -> bool:
            root = self._load_collection(path)
            if mutate(root) is False:
                return False
            self._store_collection(path, root)
            return True

        return job

    # ------------------------------------------------- playlists and folders
    def new_folder_job(self, node_path: tuple) -> Job:
        return self._edit(lambda root: slp.ensure_folder(root, node_path))

    def new_playlist(self, name: str, node_path: tuple, tracks: list,
                     copy_of: Optional[TreeNode],
                     no_duplicates: Optional[bool] = None) -> tuple[Playlist, Job]:
        playlist = Playlist(name=name, source=self.id,
                            track_keys=[t.key for t in tracks],
                            no_duplicates=False)
        snapshot = list(tracks)
        return playlist, self._edit(
            lambda root: slp.create_playlist(root, node_path, snapshot))

    def relocate_playlist_job(self, node: TreeNode, new_path: tuple,
                              lib: Library) -> Optional[Job]:
        old_path = tuple(node.node_path)
        return self._edit(lambda root: slp.relocate(root, old_path, new_path))

    def relocate_folder_job(self, old_path: tuple, new_path: tuple) -> Optional[Job]:
        return self._edit(lambda root: slp.relocate(root, old_path, new_path))

    def remove_job(self, node: TreeNode) -> Optional[Job]:
        path = tuple(node.node_path)
        return self._edit(lambda root: slp.remove_node(root, path))

    # ------------------------------------------------------------ membership
    def write_membership_job(self, node: TreeNode, tracks: list) -> Job:
        path = tuple(node.node_path)
        snapshot = list(tracks)
        return self._edit(
            lambda root: slp.set_playlist_entries(root, path, snapshot))

    # ---------------------------------------------------- collection entries
    def create_entries_job(self, tracks) -> Job:
        path = self.paths.slp
        snapshot = list(tracks)

        def job() -> dict:
            root = self._load_collection(path)
            created = {}
            for track in snapshot:
                created[track.key] = slp.find_track(root, track.path) is None
                slp.upsert_track(root, track)
            self._store_collection(path, root)
            return created

        return job

    def remove_entry_job(self, key: str) -> Job:
        return self._edit(lambda root: slp.remove_track(root, key))

    def read_tracks(self, by_key: dict) -> dict:
        """Pick these tracks out of the parsed document.

        No locating to do: this format is held as a tree rather than as text,
        and it is this app's own file -- small, because it carries only what
        SlipStream owns.
        """
        if not by_key:
            return {}
        return slp.tracks_by_key(self._load_collection(self.paths.slp), by_key)

    def repath_tracks_job(self, by_key: dict) -> Job:
        return self._edit_many(
            lambda root: slp.repath_tracks(root, dict(by_key)))

    def write_metadata_jobs(self, by_key: dict, paths: dict | None = None) -> Job:
        # ``paths`` ignored: this format is held as a parsed tree, so there is
        # no text to locate an entry in.
        return self._edit_many(
            lambda root: slp.set_tracks_metadata(root, dict(by_key)))

    def key_fields(self, key_value: int) -> dict:
        # SlipStream's own format follows VirtualDJ's notation, which is what its
        # KEY attribute has always held.
        return {"key": keys.musical_key(key_value)}

    def _edit_many(self, mutate) -> Job:
        """:meth:`_edit` for a mutation that answers per key."""
        path = self.paths.slp

        def job() -> dict:
            root = self._load_collection(path)
            changed = mutate(root)
            if any(changed.values()):
                self._store_collection(path, root)
            return changed

        return job
