"""A serialized, atomic background file-write queue.

Playlist edits must feel instant: the in-memory model updates and the views
refresh on the main thread, while the (potentially slow, MB-sized) collection
file write happens off it. Two safety properties matter:

* **Serialized** -- a single worker processes jobs FIFO, so two rapid edits to
  the same file cannot interleave (each job reads-modifies-writes a whole,
  already-committed file).
* **Atomic** -- each write goes to ``<path>.tmp`` then ``os.replace`` onto the
  target, so a concurrent reader (e.g. a manual Refresh re-parsing the file on
  the main thread) never sees a half-written file.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class MalformedWrite(ValueError):
    """A write was refused because the content it would store is not valid XML."""


def _existing_newline(path: str):
    r"""The line ending ``path`` already uses -- ``"\r\n"`` or ``"\n"`` -- or
    None (the platform's) for a new file or one with no line break yet.

    Files are read with universal newlines, so ``content`` arrives holding
    ``\n`` only. Writing it back through a plain text handle turned every one
    into ``\r\n``: harmless for VirtualDJ, whose files are CRLF already, but
    Traktor writes ``collection.nml`` with LF, and a one-attribute relink
    rewrote all 1.1 million of its line ends.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return None
    lf = head.find(b"\n")
    if lf < 0:
        return None
    return "\r\n" if lf > 0 and head[lf - 1:lf] == b"\r" else "\n"


def atomic_write_text(path: str, content: str, encoding: str = "utf-8") -> None:
    """Write ``content`` to ``path`` atomically (temp file + ``os.replace``),
    keeping the line ending the file already has.

    ``os.replace`` is atomic on the same filesystem, so a reader either sees the
    old file or the new one, never a partial write.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    newline = _existing_newline(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding=encoding, newline=newline) as f:
        f.write(content)
    os.replace(tmp, path)


def atomic_write_xml(path: str, content: str, encoding: str = "utf-8") -> None:
    """As :func:`atomic_write_text`, but **refuse to store malformed XML**.

    Every collection write in this app is a regex edit on hand-shaped XML, and a
    result that will not parse is always a bug -- never something worth
    persisting over a user's library. A stripping pattern that could not span a
    ``<CUE_V2>`` with a nested ``<GRID>`` child once left an orphan closing tag
    behind and produced a ``collection.nml`` Traktor refused to open; the damage
    was only visible after the fact, in a file that had already replaced the
    original.

    So the check happens **before** ``os.replace``: the original file is left
    exactly as it was and the caller gets an exception it can report. Cost is one
    parse of a string already in memory.
    """
    try:
        ET.fromstring(content)
    except ET.ParseError as exc:
        raise MalformedWrite(
            f"refusing to write malformed XML to {path}: {exc}") from exc
    atomic_write_text(path, content, encoding)


def atomic_write_bytes(path: str, content: bytes, validate=None) -> None:
    """The binary counterpart, for formats that are not XML (Serato's).

    ``validate(content)`` raises ``ValueError`` on anything that must not be
    stored, and is run **before** ``os.replace`` -- the same never-persist-what
    -will-not-parse guard :func:`atomic_write_xml` gives the XML collections.
    """
    if validate is not None:
        try:
            validate(content)
        except ValueError as exc:
            raise MalformedWrite(f"refusing to write {path}: {exc}") from exc
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(content)
    os.replace(tmp, path)


class _Job(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self._fn()
        except Exception:               # a background write must never crash the app
            pass


class WriteQueue(QObject):
    """Runs submitted callables one at a time on a background thread."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)     # FIFO single worker -> serialized

    def submit(self, fn) -> None:
        self._pool.start(_Job(fn))

    def flush(self, msec: int = -1) -> bool:
        """Block until the queue drains (call on close). ``-1`` waits forever."""
        return self._pool.waitForDone(msec)


class _CallableRunnable(QRunnable):
    def __init__(self, fn, on_done, on_failed):
        super().__init__()
        self._fn, self._done, self._failed = fn, on_done, on_failed

    def run(self):
        try:
            result = self._fn()
        except Exception as e:                 # marshalled back, never crashes
            self._failed(e)
            return
        self._done(result)


class BackgroundTask(QObject):
    """Runs one callable off the main thread and re-emits the outcome on the
    thread this object lives in (``done(result)`` / ``failed(exception)``).

    Used to keep the Qt event loop -- and therefore main-thread audio playback --
    alive during a long, CPU-heavy library parse + diff. The callable MUST be
    self-contained (no Qt widget access, no shared mutable state); it returns a
    plain result that the main-thread slot then installs.
    """

    done = Signal(object)
    failed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

    def run(self, fn) -> None:
        self._pool.start(_CallableRunnable(
            fn, self.done.emit, self.failed.emit))

    def flush(self, msec: int = -1) -> bool:
        return self._pool.waitForDone(msec)
