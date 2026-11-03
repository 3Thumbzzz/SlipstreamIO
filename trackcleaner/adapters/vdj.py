"""VirtualDJ: ``database.xml`` for tracks, plus a ``Folders/`` tree on disk for
playlists.

The structural difference that shapes this whole adapter: a VirtualDJ playlist
**is a file** (``Name.vdjfolder``) and a folder **is a directory**
(``Name.subfolders``). So renaming and moving are the same operation -- the path
changes -- and both are ``os.replace``, not text surgery. Only the track
collection in ``database.xml`` is edited textually.
"""

from __future__ import annotations

import os
from typing import Optional

from ..io import recycle, vdj_read, vdj_write
from ..io.vdj_process import is_vdj_running
from ..io.write_queue import atomic_write_text, atomic_write_xml
from ..model import keys
from ..model.library import Library, Playlist, TreeNode, VDJ
from .base import Job, LibraryAdapter, PathField


class VdjAdapter(LibraryAdapter):
    id = VDJ
    display_name = "VirtualDJ"
    collection_label = "database.xml"
    logo_file = "vdjl.png"
    tab_bg = "#b23b3b"
    tab_fg = "#ffffff"
    tab_bg_inactive = "#7f3030"
    # VirtualDJ rewrites database.xml on exit and can overwrite our edits.
    clobbers_on_exit = True
    # A playlist IS a .vdjfolder file, so removing one recycles it, and its name
    # has to survive as a filename.
    playlists_are_files = True
    supports_duplicate_policy = True

    # Characters a Windows filename cannot carry. Replaced rather than rejected,
    # so a name typed in the sidebar always lands somewhere sensible.
    _ILLEGAL = str.maketrans({c: "_" for c in r'\/:*?"<>|'})

    def sanitize_node_name(self, name: str) -> str:
        return name.translate(self._ILLEGAL).rstrip(" .")

    # ---------------------------------------------------------------- config
    def path_fields(self) -> list[PathField]:
        return [PathField("xml", "VirtualDJ database.xml:", "Select database.xml",
                          "database.xml (*.xml)")]

    def is_configured(self) -> bool:
        return bool(self.paths.xml) and os.path.exists(self.paths.xml)

    def accepts_path(self, path: str) -> bool:
        """VirtualDJ keeps **one database per drive**: ``X:\\VirtualDJ\\database.xml``
        holds the songs on X:, and the home database those on drives without
        one. A song from another drive written into a drive's database is one
        VirtualDJ does not expect there -- three C: tracks created in the D:
        database were part of what it reported as "Database D: is corrupt".

        Only a drive's own database (``X:\\VirtualDJ``) is held to its drive.
        Any other location -- the home database, or one elsewhere -- accepts
        every path: which drives VirtualDJ files there depends on settings this
        app does not read."""
        db = self.paths.xml
        song_drive = os.path.splitdrive(path or "")[0].upper()
        if not db or not song_drive:
            return True
        db_drive = os.path.splitdrive(os.path.abspath(db))[0].upper()
        drive_db_dir = os.path.join(db_drive + os.sep, "VirtualDJ")
        if os.path.normcase(os.path.dirname(os.path.abspath(db))) == os.path.normcase(drive_db_dir):
            return song_drive == db_drive
        return True

    @property
    def collection_path(self) -> str:
        return self.paths.xml

    def is_running(self) -> bool:
        return is_vdj_running()

    @property
    def _folders_root(self) -> str:
        return self.paths.vdj_folders_dir()

    # ------------------------------------------------------------------ read
    def read(self, yield_fn=None) -> Library:
        return vdj_read.read(self.paths.xml, self._folders_root, yield_fn)

    # ------------------------------------------------- playlists and folders
    def new_folder_job(self, node_path: tuple) -> Job:
        folders_root = self._folders_root

        def job():
            vdj_write.subfolder_dir_for(folders_root, node_path)

        return job

    def new_playlist(self, name: str, node_path: tuple, tracks: list,
                     copy_of: Optional[TreeNode],
                     no_duplicates: Optional[bool] = None) -> tuple[Playlist, Job]:
        dest = vdj_write.vdjfolder_path_for(self._folders_root, node_path)
        # VirtualDJ's own default for a hand-made playlist (see vdj_read's note
        # on the absent attribute), so a new one doesn't arrive wearing the ∞
        # marker. The sync engine passes False for playlists IT creates; both
        # are deliberate. A duplicate inherits its original's.
        if no_duplicates is not None:
            nodup = no_duplicates
        else:
            nodup = copy_of.playlist.no_duplicates if copy_of is not None else True
        playlist = Playlist(name=name, source=VDJ,
                            track_keys=[t.key for t in tracks],
                            vdj_file=dest, no_duplicates=nodup)

        def job():
            atomic_write_text(dest, vdj_write.render_vdjfolder(tracks, nodup))

        return playlist, job

    def relocate_playlist_job(self, node: TreeNode, new_path: tuple,
                              lib: Library) -> Optional[Job]:
        old_file = node.playlist.vdj_file
        new_file = vdj_write.vdjfolder_path_for(self._folders_root, new_path)
        tracks = [lib.tracks[k] for k in node.playlist.track_keys if k in lib.tracks]
        nodup = node.playlist.no_duplicates

        def job():
            if old_file and os.path.exists(old_file):
                # Uniqueness was settled by the caller's name resolution, so this
                # can never land on a sibling's file.
                os.replace(old_file, new_file)
            else:
                # An order-file ghost with no backing file: write a real one.
                atomic_write_text(new_file, vdj_write.render_vdjfolder(tracks, nodup))

        node.playlist.vdj_file = new_file
        return job

    def relocate_folder_job(self, old_path: tuple, new_path: tuple) -> Optional[Job]:
        root = self._folders_root
        old_dir = vdj_write.subfolder_dir_for(root, old_path, create=False)
        new_dir = vdj_write.subfolder_dir_for(root, new_path, create=False)
        # A VDJ folder can also be its own list; the .vdjfolder beside the
        # directory has to travel with it or the two come apart.
        old_list = os.path.join(
            vdj_write.subfolder_dir_for(root, old_path[:-1], create=False),
            old_path[-1] + ".vdjfolder")
        new_list = os.path.join(
            vdj_write.subfolder_dir_for(root, new_path[:-1], create=False),
            new_path[-1] + ".vdjfolder")

        def job():
            vdj_write.subfolder_dir_for(root, new_path[:-1])
            if os.path.isdir(old_dir):
                os.replace(old_dir, new_dir)
            if os.path.exists(old_list):
                os.replace(old_list, new_list)

        return job

    def remove_job(self, node: TreeNode) -> Optional[Job]:
        if node.is_folder():
            root = self._folders_root
            path = tuple(node.node_path)
            # The directory and the same-named .vdjfolder are one thing, exactly
            # as relocate_folder_job moves them together.
            targets = [
                vdj_write.subfolder_dir_for(root, path, create=False),
                os.path.join(
                    vdj_write.subfolder_dir_for(root, path[:-1], create=False),
                    node.name + ".vdjfolder"),
            ]
        else:
            targets = [node.playlist.vdj_file] if node.playlist else []

        def job():
            for target in targets:
                if target and os.path.exists(target):
                    recycle.send_to_recycle_bin(target)

        return job

    # ------------------------------------------------------------ membership
    def write_membership_job(self, node: TreeNode, tracks: list) -> Job:
        dest = node.playlist.vdj_file or vdj_write.vdjfolder_path_for(
            self._folders_root, node.node_path)
        nodup = node.playlist.no_duplicates

        def job():
            # The rows already there are kept as VirtualDJ wrote them.
            try:
                with open(dest, encoding="utf-8", errors="replace") as f:
                    existing = f.read()
            except OSError:
                existing = None
            atomic_write_text(dest, vdj_write.render_vdjfolder(tracks, nodup, existing))

        return job

    # ---------------------------------------------------- collection entries
    def _edit_db(self, mutate) -> Job:
        """Read database.xml, hand it to ``mutate``, write it back only if
        something changed. **Returns whether it was rewritten** -- see
        ``TraktorAdapter._edit`` for why a caller needs that answer."""
        path = self.paths.xml

        def job() -> bool:
            content = self._load_collection(path)
            new_content, changed = mutate(content)
            if changed:
                self._store_collection(path, new_content)
            return bool(changed)

        return job

    def create_entries_job(self, tracks) -> Job:
        path = self.paths.xml
        snapshot = list(tracks)

        def job() -> dict:
            content = self._load_collection(path)
            new_content, created = vdj_write.sync_tracks_into_xml(
                content, snapshot)
            self._store_collection(path, new_content)
            return created

        return job

    def remove_entry_job(self, key: str) -> Job:
        return self._edit_db(lambda c: vdj_write.remove_song_from_xml(c, key))

    def read_tracks(self, by_key: dict) -> dict:
        """Locate these ``<Song>`` blocks in database.xml and parse each alone.

        ``locate_songs`` answers ``None`` for anything it will not vouch for, and
        that answer is respected rather than worked around: nothing is reported,
        so the caller falls back to whatever it would have done anyway.
        """
        if not by_key:
            return {}
        content = self._load_collection(self.paths.xml)
        found = vdj_write.locate_songs(content, dict(by_key))
        if not found:
            return {}
        out = {}
        for key, match in found.items():
            track = vdj_read._track_from_song(match.group(0))
            if track is not None:
                out[key] = track
        return out

    def repath_tracks_job(self, by_key: dict) -> Job:
        """Move the ``FilePath`` in ``database.xml`` **and** the ``path=`` on
        every ``.vdjfolder`` row naming the track.

        Playlist membership here is a path written into each playlist file, so
        rewriting only the collection would leave the track in no playlist at
        all. The tree is walked once, and a file is rewritten only if it really
        held one of these tracks.

        A key counts as relinked if **either** moved, as on Traktor: a playlist
        may name a track ``database.xml`` has no ``<Song>`` for, and moving that
        reference is still the work that was asked for.
        """
        moves = dict(by_key)
        root = self._folders_root
        path = self.paths.xml

        def job() -> dict:
            content = self._load_collection(path)
            new_content, done = vdj_write.repath_songs_in_xml(content, moves)
            if any(done.values()):
                self._store_collection(path, new_content)
            in_playlists: set = set()
            for folder_file in self._vdjfolder_files(root):
                try:
                    with open(folder_file, "r", encoding="utf-8",
                              errors="ignore") as f:
                        text = f.read()
                except OSError:
                    continue
                new_text, changed = vdj_write.repath_songs_in_vdjfolder(
                    text, moves, in_playlists)
                if changed:
                    atomic_write_text(folder_file, new_text)
            return {k: done.get(k, False) or k in in_playlists for k in moves}

        return job

    @staticmethod
    def _vdjfolder_files(root: str) -> list:
        """Every ``.vdjfolder`` under ``root``, at any depth."""
        found = []
        for base, _dirs, files in os.walk(root or ""):
            found.extend(os.path.join(base, f) for f in files
                         if f.lower().endswith(".vdjfolder"))
        return found

    def write_metadata_jobs(self, by_key: dict, paths: dict | None = None) -> Job:
        snapshot = dict(paths) if paths else None
        return self._edit_db_many(
            lambda c: vdj_write.set_songs_metadata(c, dict(by_key), snapshot))

    def write_playcount_jobs(self, by_key: dict) -> Job:
        return self._edit_db_many(
            lambda c: vdj_write.set_songs_playcount(c, dict(by_key)))

    def key_fields(self, key_value: int) -> dict:
        return {"key": keys.musical_key(key_value)}

    def _edit_db_many(self, mutate) -> Job:
        """:meth:`_edit_db` for a mutation that answers per key."""
        path = self.paths.xml

        def job() -> dict:
            content = self._load_collection(path)
            new_content, changed = mutate(content)
            if any(changed.values()):
                self._store_collection(path, new_content)
            return changed

        return job
