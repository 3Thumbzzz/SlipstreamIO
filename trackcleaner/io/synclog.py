"""Per-run diagnostic logs for sync operations.

Written for a specific problem: the interesting sync failures only happen against
**real** libraries. The fixtures have no nested ``<GRID>`` cue, no attribution
history and a handful of tracks, so a class of defect is invisible to the test
suite and can only be caught by recording what actually happened on a user's own
collection.

Each run writes **one file**, because a run is the unit you would send to someone
or diff against another run.

Two rules this module lives by:

**Logging never changes a sync.** :class:`NullLog` is the default sink and does
nothing; every event call is wrapped so a logging failure is swallowed rather
than aborting a write half way through. A diagnostic that breaks the thing it is
diagnosing is worse than no diagnostic.

**It records decisions, not just outcomes.** "Skipped" is what the user can
already see; *which branch* skipped it, and what the attribution verdict was, is
the thing that answers why.
"""

from __future__ import annotations

import contextlib
import os
import re
import time
import xml.etree.ElementTree as ET

from ..config import paths as paths_mod
from .write_queue import atomic_write_text

LOGS_DIRNAME = "logs"

#: Value shown for a field that is empty, so "blank" is visible in the log rather
#: than looking like a missing column.
_BLANK = "<blank>"


def logs_dir() -> str:
    """Where run logs are written. Resolved through the *module* so a test's
    ``monkeypatch.setattr(paths_mod, "app_dir", …)`` takes effect -- a
    from-import would bind the original and write the developer's real folder,
    which HANDOFF.md states under "Development conventions"."""
    return os.path.join(paths_mod.app_dir(), LOGS_DIRNAME)


def _slug(text: str, limit: int = 40) -> str:
    """A filename-safe fragment of ``text``."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", str(text or "")).strip("-")
    return (cleaned[:limit] or "untitled")


def log_path(kind: str, label: str, when=None) -> str:
    """``logs/sync_update_2026-07-31_0142-33_Zero-To-Break-Chill.log``.

    The timestamp carries seconds so two runs a moment apart cannot collide, and
    sorts chronologically as plain text.
    """
    stamp = time.strftime("%Y-%m-%d_%H%M-%S", time.localtime(when))
    return os.path.join(logs_dir(), f"{_slug(kind, 24)}_{stamp}_{_slug(label)}.log")


def shown(value) -> str:
    """A field value as it should read in the log."""
    text = "" if value is None else str(value)
    return text if text.strip() else _BLANK


class Timings:
    """Wall-clock totals per phase, plus the per-track worst offenders.

    Always collected -- it is a handful of ``time.perf_counter()`` calls and a
    dict, far cheaper than the thing it measures -- but only *written out* when
    the performance toggle is on. Keeping it unconditional means the numbers are
    the same whether or not anyone is reading them, which is the point.
    """

    def __init__(self):
        self.phases: dict = {}
        #: Passes over a whole collection document. One per write kind per run is
        #: the contract; one per track is the regression this counts to catch.
        self.walks = 0
        self.entries_edited = 0
        self.entries_appended = 0
        #: Whole-file reads and writes of a collection. The number the
        #: "one rewrite per track" suspicion stands or falls on.
        self.file_reads = 0
        self.file_writes = 0
        self._started = time.perf_counter()
        self._stopped = None

    def add(self, phase: str, seconds: float) -> None:
        self.phases[phase] = self.phases.get(phase, 0.0) + seconds

    def stop(self) -> float:
        """Freeze the total where the run actually ends.

        Without this the total was read when the *log* was written, which is
        after the log re-reads the whole collection to check it still parses --
        about 6.5 seconds of a 52 MB run, charged to a sync that had already
        finished. Idempotent, so a second call cannot stretch it.
        """
        if self._stopped is None:
            self._stopped = time.perf_counter() - self._started
        return self._stopped

    def elapsed(self) -> float:
        """Seconds since the run began, or its frozen total once :meth:`stop`
        has been called."""
        if self._stopped is not None:
            return self._stopped
        return time.perf_counter() - self._started

    @contextlib.contextmanager
    def phase(self, name: str):
        start = time.perf_counter()
        try:
            yield
        finally:
            self.add(name, time.perf_counter() - start)


#: A line whose text is not known yet; see ``SyncLog.reserve``. Never written.
_RESERVED = object()


class NullLog:
    """The default sink: records nothing, costs a call.

    Exists so the engine has no ``if logging_enabled`` branches -- the only thing
    the Debug toggles change is which sink is attached.
    """

    enabled = False

    # Positional-only: the field names are the caller's vocabulary, and one of
    # them is "name". Without the "/" that collides with the parameter.
    def event(self, event_name: str, /, **fields) -> None:
        pass

    def section(self, title: str) -> None:
        pass

    def line(self, text: str = "") -> None:
        pass

    def reserve(self) -> int:
        return -1

    def fill(self, slot: int, text: str) -> None:
        pass

    def note_file(self, label: str, path: str) -> None:
        pass

    def field_comparison(self, fields, source_track, target_track) -> None:
        pass

    def check_parses(self, when: str, path: str) -> bool:
        """No parsing when nothing is being logged: reading a 40 MB database.xml
        to answer a question nobody asked would slow every sync."""
        return True

    def timings(self, timing) -> None:
        pass


class SyncLog(NullLog):
    """Collects a run's events and writes them once, at the end.

    Buffered rather than streamed: a sync is short, and holding the file open
    across it would leave a half-written log behind if the run raised -- which is
    exactly the run worth reading.
    """

    enabled = True

    #: Set by the caller from the "Log sync performance" toggle. Off by default
    #: so an ordinary log is not padded with numbers nobody asked for.
    want_timings = False

    #: Set from the "Verify collection parses" toggle. **Off by default because
    #: it is the single most expensive thing a log does**: two whole-document
    #: parses, 1.13 s each on a 44 MB database.xml, on a sync that may have
    #: touched one track. That was 2.3 s of a 5 s single-track run.
    #:
    #: Defaulting it off costs little. :func:`write_queue.atomic_write_xml`
    #: already parses the exact string it is about to store and refuses to
    #: replace the file with malformed XML, so the *after* check re-proves a
    #: guarantee that already holds. The *before* check answers a different
    #: question -- was the file already broken when the run started -- which is
    #: worth having and is what you switch this on for.
    want_parse_checks = False

    def __init__(self, kind: str, label: str):
        self.kind = kind
        self.label = label
        self.started = time.time()
        self._lines: list[str] = []
        self.path = ""
        #: Why the last :meth:`write` produced nothing, so the caller can say so
        #: instead of leaving the user with a missing log and no reason.
        self.error = ""

    # ------------------------------------------------------------ writing
    def line(self, text: str = "") -> None:
        self._lines.append(str(text))

    def reserve(self) -> int:
        """Hold a line's place and hand back its index.

        Writes are batched now -- they happen once per kind after every track has
        been decided -- so their outcomes are known long after the track's own
        block was written. Reserving the line keeps a log reading exactly as it
        did, per track and in order, instead of exiling every outcome to a
        section at the end.
        """
        self._lines.append(_RESERVED)
        return len(self._lines) - 1

    def fill(self, slot: int, text: str) -> None:
        """Fill a reserved line. Empty text leaves it reserved, and a line still
        reserved when the log is written simply is not there -- a track with
        nothing to report should not leave a blank behind."""
        if 0 <= slot < len(self._lines) and text:
            self._lines[slot] = text

    def section(self, title: str) -> None:
        self.line()
        self.line(title)
        self.line("-" * len(title))

    def event(self, event_name: str, /, **fields) -> None:
        """One decision point: a label, then ``key=value`` pairs in the order
        given -- the order is chosen at the call site to read as a sentence, so
        it is not sorted here."""
        rendered = "  ".join(f"{k}={shown(v)}" for k, v in fields.items())
        self.line(f"  {event_name}" + (f"  {rendered}" if rendered else ""))

    def field_comparison(self, fields, source_track, target_track) -> None:
        """Per-field source-vs-target, which is what shows a blank source about
        to erase a populated target."""
        for field in fields:
            a = getattr(source_track, field, "")
            b = getattr(target_track, field, "") if target_track is not None else None
            same = (a or "").strip().casefold() == (b or "").strip().casefold()
            self.line(f"      {field:9} source={shown(a):32} "
                      f"target={shown(b):32} {'same' if same else 'DIFFERS'}")

    # ------------------------------------------------- file health checks
    def check_parses(self, when: str, path: str) -> bool:
        """Record whether ``path`` is well-formed XML.

        The point of doing it **before and after**: a file that parsed before a
        run and not after is the run's own doing, and that is a far stronger
        statement than "the file is broken".

        Skipped unless :attr:`want_parse_checks` is on -- see that attribute for
        why it is off by default. Silent when skipped rather than logging "not
        checked": a line saying nothing happened, on every run, is noise.
        """
        if not self.want_parse_checks:
            return True
        if not path or not os.path.exists(path):
            self.event(f"parse-{when}", path=path, result="missing")
            return False
        try:
            ET.parse(path)
        except Exception as exc:                 # noqa: BLE001 - reporting, not handling
            self.event(f"parse-{when}", path=path, result="MALFORMED", error=str(exc))
            self._dump_around_error(path, str(exc))
            return False
        self.event(f"parse-{when}", path=path, result="ok",
                   bytes=os.path.getsize(path))
        return True

    def _dump_around_error(self, path: str, error: str) -> None:
        """The ``<ENTRY>`` the parse error points at, so the offending element
        travels with the log instead of being hunted for by hand."""
        m = re.search(r"line (\d+)", error)
        if not m:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except OSError:
            return
        centre = max(0, int(m.group(1)) - 1)
        lo, hi = max(0, centre - 12), min(len(lines), centre + 6)
        self.line(f"    --- {path} lines {lo + 1}-{hi} ---")
        for i in range(lo, hi):
            marker = ">>" if i == centre else "  "
            self.line(f"    {marker}{i + 1:>7}: {lines[i].rstrip()}")
        self.line("    --- end ---")

    def timings(self, timing) -> None:
        """The performance section. Only written when the caller asks for it, so
        an ordinary log is not padded with numbers nobody wanted."""
        if timing is None or not self.want_timings:
            return
        self.section("Performance")
        self.event("total", seconds=f"{timing.elapsed():.2f}")
        self.event("whole-file collection reads", count=timing.file_reads)
        self.event("whole-file collection writes", count=timing.file_writes)
        # The number this section exists for now that the I/O is one read and one
        # write: a walk is a pass over every entry in the collection, and doing
        # one per track was 46.6 of one 110.9-second run.
        self.event("document walks", count=timing.walks)
        self.event("entries", edited=timing.entries_edited,
                   appended=timing.entries_appended)
        for name, seconds in sorted(timing.phases.items(), key=lambda kv: -kv[1]):
            self.line(f"    {seconds:8.2f}s  {name}")

    def note_file(self, label: str, path: str) -> None:
        try:
            size = os.path.getsize(path)
            mtime = time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(os.path.getmtime(path)))
        except OSError:
            size, mtime = "?", "?"
        self.event(label, path=path, bytes=size, modified=mtime)

    # ------------------------------------------------------------ output
    def write(self) -> str:
        """Flush to disk and return the path (``""`` if it could not be written).

        Never raises: a log that cannot be saved must not turn a completed sync
        into a failed one.

        The text is built **before** anything is opened, and stored through
        :func:`atomic_write_text` (temp file + ``os.replace``, the same way every
        other file this app writes goes down). The old shape opened the real path
        first -- truncating it -- and only then joined and encoded, so a failure
        anywhere after the ``open`` left a **0-byte file** and, because the
        caller swallowed the exception, no word of it. Two of the four playlists
        in the reported folder sync produced exactly that.

        ``backslashreplace`` is deliberate: an unencodable character (an
        unpaired surrogate out of a Windows filename is the usual one) should
        cost one mangled word, not the entire 1 MB log.

        A line still holding the :data:`_RESERVED` sentinel is dropped, which is
        what :meth:`fill` promises. It is also what turned a lost slot into a
        lost *file*: the old ``"\\n".join`` raised ``TypeError`` on the sentinel
        after the real path had already been truncated, and the caller swallowed
        it. Rendering the sentinel instead, as this did for one round, only
        traded a 0-byte log for ``<object object at 0x…>`` in the middle of a
        good one.
        """
        header = [
            f"SlipStreamIO sync log - {self.kind}",
            f"started  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.started))}",
            f"finished {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"subject  {self.label}",
            "=" * 72,
        ]
        try:
            written = [ln for ln in self._lines if ln is not _RESERVED]
            body = "\n".join(header + [str(ln) for ln in written]) + "\n"
            body = body.encode("utf-8", "backslashreplace").decode("utf-8")
            os.makedirs(logs_dir(), exist_ok=True)
            path = log_path(self.kind, self.label, self.started)
            atomic_write_text(path, body)
        except Exception as exc:                     # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            return ""
        self.path = path
        return path

    def text(self) -> str:
        """What would be written -- for tests, and for showing it without a file."""
        return "\n".join(self._lines)
