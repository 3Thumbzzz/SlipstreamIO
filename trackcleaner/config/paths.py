"""Application paths + persisted settings.

Resolves the "app directory" in a ``sys.frozen``-aware way (works both when run
as ``python app.py`` and when packaged with PyInstaller), loads/saves the user's
chosen database.xml / collection.nml / start_time_report.csv paths, and
guarantees a start_time_report.csv exists in the app dir on first run.

Settings are stored as JSON in ``<app_dir>/trackcleaner_settings.json``.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from dataclasses import dataclass, asdict, field, fields
from typing import Optional

SETTINGS_FILENAME = "trackcleaner_settings.json"
BASELINE_FILENAME = "sync_baseline.csv"
DEFAULT_CSV_NAME = "start_time_report.csv"
IMAGES_DIRNAME = "Images"


def app_dir() -> str:
    """Directory the app "lives in": the exe's folder when frozen, else the
    project root (parent of the ``trackcleaner`` package).

    **A packaged macOS app keeps its files in Application Support instead.**
    The exe's folder there is ``SlipStreamIO.app/Contents/MacOS``: writing into
    a bundle breaks its code signature, and a downloaded app launched in place
    runs from a read-only translocated copy, so settings and backups would
    either fail or vanish. The Windows build stays portable -- everything next
    to the exe -- as it always was.
    """
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            support = os.path.join(os.path.expanduser("~"), "Library",
                                   "Application Support", "SlipStreamIO")
            os.makedirs(support, exist_ok=True)
            return support
        return os.path.dirname(sys.executable)
    # <root>/trackcleaner/config/paths.py -> <root>
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def settings_path() -> str:
    return os.path.join(app_dir(), SETTINGS_FILENAME)


def baseline_path() -> str:
    """Change-attribution store (see :mod:`trackcleaner.model.baseline`)."""
    return os.path.join(app_dir(), BASELINE_FILENAME)


def serato_cue_cache_path() -> str:
    """Serato's cue points as last read from each audio file (see
    ``io/serato_tags.CueCache``). Rebuildable: deleting it costs one slow load."""
    return os.path.join(app_dir(), "serato_cues.json")


def serato_tag_backup_dir() -> str:
    """Where the Serato frames an audio file held are saved before this app
    rewrites them -- the one write that lands inside audio files."""
    return os.path.join(app_dir(), "backups", "serato_tags")


def images_dir() -> str:
    """Bundled artwork: program logos, tutorial animations.

    Read from disk at runtime rather than compiled into a Qt resource, so a logo
    can be replaced without a rebuild -- which is also what lets the tutorial
    GIFs be dropped in later.

    A packaged build also carries a copy inside the package (PyInstaller's
    ``sys._MEIPASS``). An ``Images`` folder beside the app wins, so replacing a
    logo still needs no rebuild; without one, the bundled copy is used.
    """
    local = os.path.join(app_dir(), IMAGES_DIRNAME)
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled and not os.path.isdir(local):
        return os.path.join(bundled, IMAGES_DIRNAME)
    return local


def image_path(name: str) -> str:
    """Absolute path to one bundled image. Returns the path whether or not the
    file is there; callers decide what a missing asset means, because for a
    watermark it is merely nothing to draw."""
    return os.path.join(images_dir(), name)


@dataclass
class Paths:
    """Where each program's library lives, plus the start-time cache.

    One field per adapter path (see ``LibraryAdapter.path_fields``), so adding a
    program means adding a field here and nothing else.
    """

    nml: str = ""          # Traktor collection.nml
    xml: str = ""          # VirtualDJ database.xml
    slp: str = ""          # SlipStream's own library
    rekordbox: str = ""    # rekordbox.xml (Rekordbox's XML exchange file)
    serato: str = ""       # Serato's "database V2" inside a _Serato_ folder
    csv: str = ""          # start_time_report.csv (cache)

    def vdj_folders_dir(self) -> str:
        """VirtualDJ ``Folders/`` dir sits next to database.xml."""
        if not self.xml:
            return ""
        return os.path.join(os.path.dirname(self.xml), "Folders")

    def is_complete(self) -> bool:
        return bool(self.nml and self.xml)


@dataclass
class Settings:
    """Everything persisted between sessions. ``ui_state`` is an opaque blob
    owned by :mod:`trackcleaner.config.state`."""

    paths: Paths = field(default_factory=Paths)
    start_time_source: str = "update"   # cache | probe | update
    backup_autosave_minutes: int = 0    # 0 = autosave off
    ui_state: dict = field(default_factory=dict)

    # ---- persistence -------------------------------------------------
    @classmethod
    def load(cls) -> "Settings":
        p = settings_path()
        if not os.path.exists(p):
            return cls()
        try:
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return cls()
        stored = raw.get("paths", {}) or {}
        paths = Paths(**{f.name: stored.get(f.name, "") for f in fields(Paths)})
        return cls(
            paths=paths,
            start_time_source=raw.get("start_time_source", "update"),
            backup_autosave_minutes=int(raw.get("backup_autosave_minutes", 0) or 0),
            ui_state=raw.get("ui_state", {}) or {},
        )

    def save(self) -> None:
        data = {
            "paths": asdict(self.paths),
            "start_time_source": self.start_time_source,
            "backup_autosave_minutes": self.backup_autosave_minutes,
            "ui_state": self.ui_state,
        }
        try:
            with open(settings_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass


def ensure_start_time_csv(settings: Settings) -> str:
    """Guarantee a usable start_time CSV path.

    If the configured csv path is empty or missing, default it to
    ``<app_dir>/start_time_report.csv`` and create an empty (header-only) file
    there so the cache is immediately referenceable. Returns the resolved path.
    """
    path = settings.paths.csv
    if not path:
        path = os.path.join(app_dir(), DEFAULT_CSV_NAME)
        settings.paths.csv = path
    if not os.path.exists(path):
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(["start_time", "bucket", "path"])
        except OSError:
            pass
    return path
