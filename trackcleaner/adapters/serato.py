"""Serato DJ: ``database V2``, ``Subcrates/*.crate``, and cues in the audio files.

Three stores, and this adapter is the only thing that knows they belong
together:

* the **database** holds tracks and their tags (``io/serato_db.py``);
* **crates** are one file each, their place in the tree spelled in the file
  name (``Sets%%2024.11 BTTB Set.crate``), with ``neworder.pref`` giving the
  sidebar order;
* **cues and the beatgrid** are ``GEOB`` frames inside each audio file
  (``io/serato_tags.py``) -- the one place this app writes into audio files.

Serato has no empty folders: a folder is a crate that has crates below it. So a
new folder is an empty crate file, which reads back as an empty list until
something is placed inside it (``holds_empty_folders`` is False).

Paths are relative to the drive the database is on; a file on another drive
cannot be named and is refused rather than written wrong.

Reading cues means opening every audio file, ~24 s for 20,000. A cache keyed on
each file's size and modification time makes every later load re-read only the
files that changed.
"""

from __future__ import annotations

import os
from typing import Optional

from ..config import paths as paths_mod
from ..io import recycle, serato_db as sdb, serato_tags, vdj_process
from ..io.write_queue import atomic_write_bytes
from ..model import keys, matching
from ..model.library import Library, Playlist, Track, TreeNode, path_ci
from .base import Job, LibraryAdapter, PathField

SERATO = "serato"

_PROCESSES = ("Serato DJ Pro.exe", "Serato DJ Lite.exe", "Serato DJ.exe",
              "Serato Scratch Live.exe")


def _to_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _length(text: str) -> Optional[float]:
    """``"04:04.45"`` -> 244.45 seconds."""
    try:
        minutes, seconds = text.split(":")
        return int(minutes) * 60 + float(seconds)
    except ValueError:
        return None


def _bitrate(text: str) -> Optional[int]:
    """``"320.0kbps"`` -> 320."""
    value = _to_float(text.lower().replace("kbps", "").strip())
    return int(value) if value else None


class SeratoAdapter(LibraryAdapter):
    id = SERATO
    display_name = "Serato DJ"
    collection_label = "database V2"
    tab_bg = "#2b2b2b"
    tab_fg = "#f5f5f5"
    tab_bg_inactive = "#1f1f1f"
    clobbers_on_exit = True
    playlists_are_files = True
    holds_empty_folders = False

    _ILLEGAL = str.maketrans({c: "_" for c in r'\/:*?"<>|'})

    # ---------------------------------------------------------------- config
    def path_fields(self) -> list[PathField]:
        return [PathField("serato", "Serato database (_Serato_\\database V2):",
                          "Select Serato's database V2", "Serato database (database V2)")]

    def is_configured(self) -> bool:
        return bool(self.paths.serato) and os.path.isfile(self.paths.serato)

    @property
    def collection_path(self) -> str:
        return self.paths.serato

    @property
    def _root(self) -> str:
        return sdb.volume_root(self.paths.serato)

    @property
    def _subcrates(self) -> str:
        return os.path.join(os.path.dirname(self.paths.serato), "Subcrates")

    @property
    def _order_file(self) -> str:
        return os.path.join(os.path.dirname(self.paths.serato), "neworder.pref")

    def is_running(self) -> bool:
        # Through the module, so a test can answer for the machine it runs on.
        return vdj_process.is_any_running(_PROCESSES)

    def sanitize_node_name(self, name: str) -> str:
        # "%%" is the crate file name's level separator, so it cannot be in one.
        return name.translate(self._ILLEGAL).replace(sdb.SEPARATOR, "%").rstrip(" .")

    # -------------------------------------------------------- the timeline
    def timeline_offset(self, stored_path: str) -> float:
        """**Not yet calibrated**, like Rekordbox: Serato's milliseconds are
        taken as true audio time until measured tracks say otherwise."""
        return 0.0

    # ------------------------------------------------------------- the key
    @classmethod
    def display_key(cls, track) -> str:
        return keys.flat_key(getattr(track, "key_value", None)) or \
            getattr(track, "key_musical", "")

    def key_fields(self, key_value: int) -> dict:
        return {"key": keys.flat_key(key_value)}

    # ------------------------------------------------------- binary storage
    def _read_collection_file(self, path: str):
        self.collection_reads += 1
        with open(path, "rb") as f:
            return f.read()

    def _write_collection_file(self, path: str, content) -> None:
        self.collection_writes += 1
        atomic_write_bytes(path, content, sdb.validate)

    # ------------------------------------------------------------------ read
    def _track(self, f: dict, pois=None) -> Optional[Track]:
        pfil = f.get("pfil")
        if pfil is None:
            return None
        path = sdb.to_path(sdb.text(pfil), self._root)
        get = lambda tag: sdb.text(f[tag]) if tag in f else ""   # noqa: E731
        key_text = get("tkey")
        return Track(
            key=matching.path_key(path), path=path, source=SERATO,
            title=matching.normalize_text(get("tsng")),
            artist=matching.normalize_text(get("tart")),
            album=matching.normalize_text(get("talb")),
            genre=matching.normalize_text(get("tgen")),
            comment=matching.normalize_text(get("tcom")),
            bpm=_to_float(get("tbpm")), length=_length(get("tlen")),
            bitrate=_bitrate(get("tbit")),
            key_musical=key_text, key_value=keys.parse_vdj_key(key_text),
            pois=list(pois or []))

    def read(self, yield_fn=None) -> Library:
        root = TreeNode("Serato Crates", "folder", SERATO)
        lib = Library(source=SERATO, root=root)
        if not self.is_configured():
            return lib
        cache = serato_tags.CueCache(paths_mod.serato_cue_cache_path())
        data = self._load_collection(self.paths.serato)
        for i, (tag, payload) in enumerate(sdb.parse(data)):
            if tag != "otrk":
                continue
            track = self._track(sdb.fields(payload))
            if track is not None:
                track.pois = cache.points(track.path)
                lib.tracks[track.key] = track
            if yield_fn is not None and (i & 0x7F) == 0:
                yield_fn()
        cache.save()
        self._build_tree(lib, yield_fn)
        return lib

    def _build_tree(self, lib: Library, yield_fn=None) -> None:
        crates = sdb.crate_files(self._subcrates)
        order = {name.lower(): i for i, name in enumerate(sdb.read_order(self._order_file))}
        nodes: dict = {}

        def node_at(path: tuple) -> TreeNode:
            key = path_ci(path)
            if key not in nodes:
                parent = node_at(path[:-1]) if len(path) > 1 else lib.root
                node = TreeNode(name=path[-1], kind="playlist", source=SERATO,
                                node_path=path)
                parent.children.append(node)
                nodes[key] = node
            return nodes[key]

        for node_path, crate in sorted(crates.items()):
            node = node_at(node_path)
            try:
                with open(crate, "rb") as f:
                    members = sdb.crate_members(f.read())
            except (OSError, ValueError):
                members = []
            track_keys = []
            for stored in members:
                key = sdb.stored_key(stored, self._root)
                if key not in lib.tracks:
                    # Named by a crate, absent from the database: shown, but not
                    # really in the collection -- a sync must create it.
                    lib.tracks[key] = Track(key=key, path=sdb.to_path(stored, self._root),
                                            source=SERATO, in_database=False)
                track_keys.append(key)
            node.playlist = Playlist(name=node.name, source=SERATO,
                                     track_keys=track_keys, no_duplicates=False)
            if yield_fn is not None:
                yield_fn()

        def finish(node: TreeNode) -> None:
            if node.children:
                node.kind = "folder"
                node.children.sort(key=lambda c: (
                    order.get(sdb.SEPARATOR.join(c.node_path).lower(), len(order)),
                    c.name.lower()))
                for child in node.children:
                    finish(child)

        finish(lib.root)

    # ------------------------------------------------------- crate helpers
    def _crate(self, node_path) -> str:
        return sdb.crate_file(self._subcrates, node_path)

    def _stored(self, tracks) -> list:
        return [s for s in (sdb.to_stored(t.path, self._root) for t in tracks)
                if s is not None]

    def _write_crate(self, node_path, tracks) -> None:
        path = self._crate(node_path)
        try:
            with open(path, "rb") as f:
                existing = f.read()
        except OSError:
            existing = None
        atomic_write_bytes(path, sdb.crate_with_members(existing, self._stored(tracks)),
                           sdb.validate)

    def _ensure_entries(self, tracks) -> None:
        """Every crate member gets a database record, as Serato's own crates'
        members have."""
        db = self.paths.serato
        content = self._load_collection(db)
        new_content, created = sdb.ensure_tracks(content, self._root, tracks)
        if any(created.values()):
            self._store_collection(db, new_content)

    def _update_order(self, renames=None, added=(), removed=()) -> None:
        path = self._order_file
        if not os.path.isfile(path):
            return
        with open(path, "rb") as f:
            raw = f.read()
        atomic_write_bytes(path, sdb.order_with(raw, renames, added, removed))

    def _crates_under(self, node_path) -> dict:
        """``{node_path: file}`` for the crate at ``node_path`` and below it."""
        want = path_ci(node_path)
        return {p: f for p, f in sdb.crate_files(self._subcrates).items()
                if path_ci(p)[:len(want)] == want}

    # ------------------------------------------------- playlists and folders
    def new_folder_job(self, node_path: tuple) -> Job:
        node_path = tuple(node_path)

        def job():
            if not os.path.exists(self._crate(node_path)):
                self._write_crate(node_path, [])
                self._update_order(added=[sdb.SEPARATOR.join(node_path)])

        return job

    def new_playlist(self, name: str, node_path: tuple, tracks: list,
                     copy_of: Optional[TreeNode],
                     no_duplicates: Optional[bool] = None) -> tuple[Playlist, Job]:
        node_path = tuple(node_path)
        snapshot = list(tracks)
        playlist = Playlist(name=name, source=SERATO,
                            track_keys=[t.key for t in snapshot], no_duplicates=False)

        def job():
            self._ensure_entries(snapshot)
            self._write_crate(node_path, snapshot)
            self._update_order(added=[sdb.SEPARATOR.join(node_path)])

        return playlist, job

    def relocate_playlist_job(self, node: TreeNode, new_path: tuple,
                              lib: Library) -> Optional[Job]:
        return self._relocate(tuple(node.node_path), tuple(new_path))

    def relocate_folder_job(self, old_path: tuple, new_path: tuple) -> Optional[Job]:
        return self._relocate(tuple(old_path), tuple(new_path))

    def _relocate(self, old_path: tuple, new_path: tuple) -> Optional[Job]:
        """Rename and move are the same thing here: every crate file at or
        under ``old_path`` takes the new prefix."""
        if not self._crates_under(old_path):
            self._fail(f"No crate at {' / '.join(old_path)}.")
            return None
        n = len(old_path)

        def job():
            moves = {f: self._crate(new_path + p[n:])
                     for p, f in self._crates_under(old_path).items()}
            clash = [d for d in moves.values()
                     if os.path.exists(d) and d.lower() not in {s.lower() for s in moves}]
            if clash:
                raise FileExistsError(f"a crate already exists at {clash[0]}")
            for src, dst in moves.items():
                os.replace(src, dst)
            self._update_order(renames={sdb.SEPARATOR.join(old_path):
                                        sdb.SEPARATOR.join(new_path)})

        return job

    def remove_job(self, node: TreeNode) -> Optional[Job]:
        node_path = tuple(node.node_path)

        def job():
            for f in self._crates_under(node_path).values():
                recycle.send_to_recycle_bin(f)
            self._update_order(removed=[sdb.SEPARATOR.join(node_path)])

        return job

    # ------------------------------------------------------------ membership
    def write_membership_job(self, node: TreeNode, tracks: list) -> Job:
        node_path = tuple(node.node_path)
        snapshot = list(tracks)

        def job():
            self._ensure_entries(snapshot)
            self._write_crate(node_path, snapshot)

        return job

    # ---------------------------------------------------- collection entries
    def _edit_many(self, mutate) -> Job:
        db = self.paths.serato

        def job() -> dict:
            content = self._load_collection(db)
            new_content, changed = mutate(content)
            if any(changed.values()):
                self._store_collection(db, new_content)
            return changed

        return job

    def create_entries_job(self, tracks) -> Job:
        """Database records for the tracks Serato lacks, and each track's cues
        and grid written into its audio file. A file that cannot take them --
        missing, another container, or a write that did not verify -- keeps
        what it had, and a read-back shows exactly that."""
        snapshot = list(tracks)
        db = self.paths.serato
        backups = paths_mod.serato_tag_backup_dir()

        def job() -> dict:
            content = self._load_collection(db)
            new_content, created = sdb.ensure_tracks(content, self._root, snapshot)
            if any(created.values()):
                self._store_collection(db, new_content)
            for track in snapshot:
                serato_tags.write_points(track.path, track, backups)
            return created

        return job

    def remove_entry_job(self, key: str) -> Job:
        db = self.paths.serato

        def job() -> bool:
            content = self._load_collection(db)
            new_content, removed = sdb.remove_track(content, self._root, key)
            if removed:
                self._store_collection(db, new_content)
            return removed

        return job

    def read_tracks(self, by_key: dict) -> dict:
        """Read back from the database *and the files*, bypassing the cue cache:
        a targeted refresh must describe what is stored now."""
        if not by_key:
            return {}
        content = self._load_collection(self.paths.serato)
        out = {}
        for key, f in sdb.find_tracks(content, self._root, by_key).items():
            track = self._track(f)
            if track is not None:
                track.pois = serato_tags.read_points(track.path) or []
                out[key] = track
        return out

    def repath_tracks_job(self, by_key: dict) -> Job:
        """The database record and every crate naming the track -- a crate
        names its members by path."""
        moves = dict(by_key)
        db = self.paths.serato

        def job() -> dict:
            content = self._load_collection(db)
            new_content, done = sdb.repath_tracks(content, self._root, moves)
            if any(done.values()):
                self._store_collection(db, new_content)
            for crate in sdb.crate_files(self._subcrates).values():
                with open(crate, "rb") as f:
                    data = f.read()
                new_data, moved = sdb.repath_crate(data, self._root, moves)
                if moved:
                    atomic_write_bytes(crate, new_data, sdb.validate)
                    for key in moved:
                        done[key] = True
            return done

        return job

    def write_metadata_jobs(self, by_key: dict, paths: dict | None = None) -> Job:
        # ``paths`` is unused: the database is parsed whole, in 0.03 s.
        snapshot = {k: dict(v) for k, v in by_key.items()}
        root = self._root
        return self._edit_many(lambda c: sdb.set_tracks_metadata(c, root, snapshot))
