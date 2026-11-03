"""Is VirtualDJ running?

Its own module because two callers now need it: the sync path warns before
writing ``database.xml``, and the Phase-3 collection writes warn before touching
it too. A platform/process query, so it sits beside ``recycle.py`` rather than in
the UI.
"""

from __future__ import annotations


def is_vdj_running() -> bool:
    """Best-effort check whether VirtualDJ.exe is running (it rewrites
    database.xml on exit and can clobber a sync's output — HANDOFF.md,
    "Running it"). Not fatal to get wrong; just a warning."""
    return is_any_running(("VirtualDJ.exe",))


def is_any_running(image_names) -> bool:
    """Whether a process with any of these executable names is running.
    Windows only (``tasklist``); elsewhere, and on any failure, "can't tell"."""
    try:
        import subprocess
        import sys
        # No console window: a packaged (windowed) build has none to share,
        # and this runs before every guarded write.
        no_window = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                     if sys.platform == "win32" else 0)
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=5, text=True,
                             creationflags=no_window).stdout
    except (OSError, ValueError, Exception):             # noqa: BLE001
        return False
    running = {line.split(",")[0].strip('"').lower() for line in out.splitlines()}
    return any(name.lower() in running for name in image_names)
