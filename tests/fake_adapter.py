"""An adapter over nothing at all.

``FakeAdapter`` keeps its whole library in a Python dict. It has no file, no
format and no vendor, which is exactly the point: it is the control case for the
adapter layer. If the shared contract in ``test_adapter_contract.py`` passes for
this, the contract is genuinely about *being an adapter* and has not quietly
grown assumptions about XML, about paths on disk, or about Traktor and
VirtualDJ specifically.

It is also the seam to reach for when testing something that merely needs *a*
program -- pipeline behaviour, sidebar behaviour, tab behaviour -- so that test
does not depend on either real format or need a fixture library on disk.

Deliberately **not** registered in ``ADAPTER_CLASSES``: the app must never ship a
program that exists only for tests. Tests parametrise over
``ADAPTER_CLASSES + [FakeAdapter]`` instead.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.adapters.base import Job, LibraryAdapter, PathField
from trackcleaner.model.library import Library, Playlist, TreeNode

FAKE = "fake"


class FakeAdapter(LibraryAdapter):
    id = FAKE
    display_name = "Fake"
    tab_bg = "#6a4f7a"
    tab_fg = "#efe4f5"
    tab_bg_inactive = "#46344f"

    def __init__(self, paths=None):
        super().__init__(paths)
        # {node_path tuple: list[track key]} for playlists, plus a set of folder
        # node paths. A dict rather than a tree because the tree is derived --
        # keeping one source of truth is what makes moves impossible to get
        # half-right.
        self.playlists: dict[tuple, list] = {}
        self.folders: set[tuple] = set()
        self.tracks: dict[str, object] = {}

    # ---------------------------------------------------------------- config
    def path_fields(self) -> list[PathField]:
        return []                      # nothing on disk, nothing to configure

    def is_configured(self) -> bool:
        return True

    # ------------------------------------------------------------------ read
    def read(self, yield_fn=None) -> Library:
        lib = Library(source=self.id)
        lib.tracks = dict(self.tracks)
        lib.root = TreeNode(name="Fake Playlists", kind="folder", source=self.id)
        for node_path in sorted(self.folders | set(self.playlists)):
            self._graft(lib.root, node_path)
            if yield_fn is not None:
                yield_fn()
        return lib

    def _graft(self, root: TreeNode, node_path: tuple) -> None:
        """Materialise ``node_path`` into the tree, creating the folders above it
        on the way down."""
        node = root
        for depth, name in enumerate(node_path, start=1):
            child = next((c for c in node.children
                          if c.name.lower() == name.lower()), None)
            if child is None:
                here = node_path[:depth]
                keys = self.playlists.get(here)
                child = TreeNode(
                    name=name,
                    kind="playlist" if keys is not None else "folder",
                    source=self.id, node_path=here,
                    playlist=(Playlist(name=name, source=self.id,
                                       track_keys=list(keys), no_duplicates=False)
                              if keys is not None else None))
                node.children.append(child)
            node = child

    # ------------------------------------------------- playlists and folders
    def new_folder_job(self, node_path: tuple) -> Job:
        node_path = tuple(node_path)
        return lambda: self.folders.add(node_path)

    def new_playlist(self, name: str, node_path: tuple, tracks: list,
                     copy_of: Optional[TreeNode]) -> tuple[Playlist, Job]:
        node_path = tuple(node_path)
        keys = [t.key for t in tracks]
        playlist = Playlist(name=name, source=self.id, track_keys=list(keys),
                            no_duplicates=False)

        def job():
            self.folders.update(node_path[:d] for d in range(1, len(node_path)))
            self.playlists[node_path] = list(keys)

        return playlist, job

    def relocate_playlist_job(self, node: TreeNode, new_path: tuple,
                              lib: Library) -> Optional[Job]:
        return self._relocate(tuple(node.node_path), tuple(new_path))

    def relocate_folder_job(self, old_path: tuple, new_path: tuple) -> Optional[Job]:
        return self._relocate(tuple(old_path), tuple(new_path))

    def _relocate(self, old_path: tuple, new_path: tuple) -> Optional[Job]:
        if old_path not in self.playlists and old_path not in self.folders:
            self._fail(f"No such node: {'/'.join(old_path)}")
            return None

        def job():
            n = len(old_path)
            # Everything at or under old_path moves with it, so a folder brings
            # its contents rather than orphaning them.
            for path in [p for p in self.playlists if p[:n] == old_path]:
                self.playlists[new_path + path[n:]] = self.playlists.pop(path)
            for path in [p for p in self.folders if p[:n] == old_path]:
                self.folders.discard(path)
                self.folders.add(new_path + path[n:])
            self.folders.update(new_path[:d] for d in range(1, len(new_path)))

        return job

    def remove_job(self, node: TreeNode) -> Optional[Job]:
        node_path = tuple(node.node_path)

        def job():
            n = len(node_path)
            for path in [p for p in self.playlists if p[:n] == node_path]:
                del self.playlists[path]
            for path in [p for p in self.folders if p[:n] == node_path]:
                self.folders.discard(path)

        return job

    # ------------------------------------------------------------ membership
    def write_membership_job(self, node: TreeNode, tracks: list) -> Job:
        node_path = tuple(node.node_path)
        keys = [t.key for t in tracks]
        return lambda: self.playlists.__setitem__(node_path, list(keys))

    # ---------------------------------------------------- collection entries
    def create_entry_job(self, track) -> Job:
        return lambda: self.tracks.__setitem__(track.key, track)

    def remove_entry_job(self, key: str) -> Job:
        return lambda: self.tracks.pop(key, None)

    def read_tracks(self, by_key: dict) -> dict:
        """This store *is* the collection, so reading it back is a lookup.

        Still returns copies: a caller that mutated what it got would be editing
        the collection behind the adapter's back, which no real format allows.
        """
        return {k: dataclasses.replace(self.tracks[k])
                for k in by_key if k in self.tracks}

    def repath_tracks_job(self, by_key: dict) -> Job:
        """Membership here is by key, so it moves with the track -- the same
        obligation Traktor's primary keys and VirtualDJ's ``.vdjfolder`` rows
        have, expressed in the simplest possible store."""
        moves = dict(by_key)

        def job() -> dict:
            done = {}
            for old_key, new_path in moves.items():
                new_key = new_path.lower()
                track = self.tracks.pop(old_key, None)
                if track is not None:
                    # Replaced, not mutated: the caller still holds this object,
                    # and a real adapter edits a file rather than the Track
                    # handed to it.
                    self.tracks[new_key] = dataclasses.replace(
                        track, key=new_key, path=new_path)
                # Membership moves whether or not there was a collection entry:
                # a playlist may name a track the collection never held.
                in_a_playlist = False
                for node_path, keys in self.playlists.items():
                    if old_key not in keys:
                        continue
                    in_a_playlist = True
                    self.playlists[node_path] = [
                        new_key if k == old_key else k for k in keys]
                done[old_key] = track is not None or in_a_playlist
            return done

        return job

    def write_metadata_jobs(self, by_key: dict, paths: dict | None = None) -> Job:
        """The plural is the real one here too, so the base's singular wrapper
        supplies ``write_metadata_job`` and there is only one implementation to
        keep honest.

        ``paths`` is ignored: this store is a dict, so there is no text to locate
        an entry in. Accepting it anyway is the point of the argument being
        optional -- a format with nothing to accelerate is not a special case.
        """
        snapshot = {k: {f: v for f, v in fields.items()
                        if f in self.EDITABLE_FIELDS}
                    for k, fields in by_key.items()}

        def job() -> dict:
            written = {}
            for key, fields in snapshot.items():
                track = self.tracks.get(key)
                # Editing a track we do not have is a no-op, and says so.
                written[key] = track is not None
                for field, value in fields.items() if track else ():
                    setattr(track, field, value)
            return written

        return job
