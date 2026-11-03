"""Open the OS file browser with a file selected.

Windows: ``explorer /select,"<path>"`` opens a new Explorer window on the file's
folder with the file highlighted. On any other platform (or if the file is gone)
this is a no-op returning ``False``, the same degradation
:mod:`trackcleaner.io.recycle` uses -- the caller decides how to report it.

The command line matters more than it looks. Explorer wants the switch
**unquoted** with only the path quoted::

    explorer /select,"D:\\Music\\02 Homenaje.mp3"      <- navigates and selects
    explorer "/select,D:\\Music\\02 Homenaje.mp3"      <- ignored: opens Documents

which is why it is built as a **string**, not an argv list. Passing a list makes
``subprocess.list2cmdline`` quote the whole ``/select,<path>`` token as soon as
the path contains a space -- producing the second form above, so Explorer opened
on the default folder and selected nothing. Paths without spaces happened to
work, which is what made it look intermittent.

``shell=False`` (the default) stays, so nothing interprets metacharacters, and a
Windows filename cannot contain ``"``, so the path cannot break out of the quotes.

``explorer.exe``'s exit code is deliberately **not** checked: it returns 1 even
on success, a long-standing Windows quirk, so a truthy return here means "we
launched it", not "a window definitely appeared".
"""

from __future__ import annotations

import os
import subprocess
import sys


def reveal_in_file_browser(path: str) -> bool:
    """Show ``path`` selected in the OS file browser. False when it cannot be
    done (wrong platform, no path, or the file is not there)."""
    if not path or not os.path.exists(path):
        return False
    if sys.platform != "win32":
        return False
    try:
        subprocess.Popen(select_command(path))
    except OSError:
        return False
    return True


def open_folder(path: str) -> bool:
    """Open a *folder* itself, rather than selecting something inside it.

    Deliberately not ``select_command`` with the folder as the target: that
    highlights the folder in its parent, which is not what "open the log folder"
    means. False on the same terms as :func:`reveal_in_file_browser`, so the
    caller can fall back to just telling the user the path.
    """
    if not path or not os.path.isdir(path):
        return False
    if sys.platform != "win32":
        return False
    try:
        subprocess.Popen(f'explorer "{os.path.normpath(path)}"')
    except OSError:
        return False
    return True


def select_command(path: str) -> str:
    """The exact command line used to reveal ``path``.

    Separated out so it can be asserted on directly: the previous version was
    tested by inspecting the argv list handed to ``Popen``, which said nothing
    about what Windows would actually receive once ``list2cmdline`` had quoted it.
    """
    return f'explorer /select,"{os.path.normpath(path)}"'
