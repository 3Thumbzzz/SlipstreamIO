"""Backups of the library files.

Two flavours:

* ``make_backup`` -- the sync engine's per-write, single-file backup next to the
  original (``database_backup_…xml`` / ``nml_backup_…xml``). Unchanged.
* ``snapshot`` / ``list_backups`` / ``revert`` -- whole-state snapshots taken on
  program open, manually and by autosave. A snapshot is a timestamped directory
  under ``<app_dir>/backups/`` holding ``collection.nml``, ``database.xml`` and a
  copy of the VDJ ``Folders/`` tree (the ``.vdjfolder`` files and ``.subfolders``
  structure -- VDJ playlists live there, **not** in the xml, so a snapshot that
  skipped it could not restore a playlist edit).

  Every *other* library file ``Paths`` names -- SlipStream's ``library.slp``,
  ``rekordbox.xml``, whatever a future program adds -- is copied too, under
  ``files/<field>/``, so the snapshot taken before a destructive change covers
  every program rather than the two that predate the adapter layer. The
  nml/xml/Folders layout is unchanged, so older snapshots still restore.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import fields
from datetime import datetime

from ..config.paths import Paths, app_dir
from ..model.library import TRAKTOR, VDJ

_STAMP_FMT = "%Y_%m_%d_%H%M%S"
_KEEP = 20                              # snapshots are large; retain the newest N


#: Filename prefixes kept for the two programs that predate the adapter layer,
#: so old backups keep sorting next to new ones. Any other program derives its
#: prefix from its own file's name.
_PREFIXES = {VDJ: "database_backup", TRAKTOR: "nml_backup"}

#: ``Paths`` fields a snapshot handles by name (the legacy layout), or that are
#: not a library at all (the start-time cache is rebuilt by probing).
_NOT_EXTRA = {"nml", "xml", "csv"}


def _extra_files(paths: Paths) -> dict:
    """``{field: path}`` for every other library file that exists."""
    out = {}
    for f in fields(Paths):
        value = getattr(paths, f.name, "")
        if f.name not in _NOT_EXTRA and value and os.path.isfile(value):
            out[f.name] = value
    return out


def backup_path(target_path: str, source: str) -> str:
    stamp = datetime.now().strftime("%Y_%m_%d_%H%M%S")
    stem = os.path.splitext(os.path.basename(target_path))[0] or source
    prefix = _PREFIXES.get(source, f"{stem}_backup")
    ext = os.path.splitext(target_path)[1]
    return os.path.join(os.path.dirname(target_path), f"{prefix}_{stamp}{ext}")


def make_backup(target_path: str, source: str) -> str:
    """Copy target_path to a timestamped backup; return the backup path.
    No-op (returns "") if target_path doesn't exist yet."""
    if not target_path or not os.path.exists(target_path):
        return ""
    dest = backup_path(target_path, source)
    shutil.copyfile(target_path, dest)
    return dest


# ==================== whole-state snapshots (open / manual / autosave) ========
def backups_dir() -> str:
    return os.path.join(app_dir(), "backups")


def snapshot(paths: Paths, stamp: str | None = None,
             root: str | None = None) -> str | None:
    """Copy nml + xml + VDJ ``Folders/`` into a fresh timestamped directory.

    Missing sources are skipped (a partly-configured setup still snapshots what
    exists). Returns the directory, or ``None`` if there was nothing to copy.
    """
    stamp = stamp or datetime.now().strftime(_STAMP_FMT)
    base = os.path.join(root or backups_dir(), stamp)
    # Two snapshots in the same second must not land in the same directory --
    # copytree would fail on the existing Folders/. Suffix a fresh one.
    dest, n = base, 1
    while os.path.exists(dest):
        dest = f"{base}_{n}"
        n += 1
    os.makedirs(dest)

    wrote = False
    if paths.nml and os.path.exists(paths.nml):
        shutil.copy2(paths.nml, os.path.join(dest, "collection.nml"))
        wrote = True
    if paths.xml and os.path.exists(paths.xml):
        shutil.copy2(paths.xml, os.path.join(dest, "database.xml"))
        wrote = True
    folders = paths.vdj_folders_dir()
    if folders and os.path.isdir(folders):
        shutil.copytree(folders, os.path.join(dest, "Folders"))
        wrote = True
    for name, source in _extra_files(paths).items():
        target_dir = os.path.join(dest, "files", name)
        os.makedirs(target_dir, exist_ok=True)
        shutil.copy2(source, os.path.join(target_dir, os.path.basename(source)))
        wrote = True

    if not wrote:
        try:
            os.rmdir(dest)          # don't leave an empty stub behind
        except OSError:
            pass
        return None
    _prune(root or backups_dir())
    return dest


def _prune(root: str, keep: int | None = None) -> None:
    """A full snapshot is tens of MB; keep only the newest ``keep`` so an
    on-open + autosave cadence cannot fill the disk."""
    keep = _KEEP if keep is None else keep
    dirs = [d for _, d in list_backups(root=root)]     # newest first
    for old in dirs[keep:]:
        try:
            shutil.rmtree(old)
        except OSError:
            pass


def list_backups(root: str | None = None) -> list[tuple[str, str]]:
    """``[(timestamp, dir)]`` newest first, for the revert picker."""
    root = root or backups_dir()
    if not os.path.isdir(root):
        return []
    out = [(name, os.path.join(root, name)) for name in os.listdir(root)
           if os.path.isdir(os.path.join(root, name))]
    out.sort(reverse=True)          # timestamp names sort chronologically
    return out


def revert(backup_dir: str, paths: Paths) -> None:
    """Restore a snapshot over the live files. ``Folders/`` is replaced wholesale
    so a stale playlist file cannot survive the restore."""
    nml_bak = os.path.join(backup_dir, "collection.nml")
    if os.path.exists(nml_bak) and paths.nml:
        shutil.copy2(nml_bak, paths.nml)
    xml_bak = os.path.join(backup_dir, "database.xml")
    if os.path.exists(xml_bak) and paths.xml:
        shutil.copy2(xml_bak, paths.xml)
    folders_bak = os.path.join(backup_dir, "Folders")
    live_folders = paths.vdj_folders_dir()
    if os.path.isdir(folders_bak) and live_folders:
        if os.path.isdir(live_folders):
            shutil.rmtree(live_folders)
        shutil.copytree(folders_bak, live_folders)
    # Each extra file goes back to wherever Paths names it *now*: a snapshot
    # restores a library, and the user may have moved where that library lives.
    files_bak = os.path.join(backup_dir, "files")
    for name in (os.listdir(files_bak) if os.path.isdir(files_bak) else ()):
        live = getattr(paths, name, "")
        saved = os.listdir(os.path.join(files_bak, name))
        if live and len(saved) == 1:
            shutil.copy2(os.path.join(files_bak, name, saved[0]), live)
