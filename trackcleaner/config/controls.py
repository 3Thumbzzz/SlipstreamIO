"""Assignable keyboard shortcuts, and their ``userControls.ini``.

Every bindable action has a **stable id** that never changes, because that id is
what ends up in the user's file. Labels and menu wording may be reworded freely;
ids may not.

Stored as INI (``configparser``) rather than beside the JSON session state: it is
a file a user is expected to open and edit by hand, and one ``key = value`` line
per action reads better for that than nested JSON.

Unknown ids in the file are ignored rather than dropped, so a file written by a
build that knew more actions survives a downgrade -- the same tolerance
``enabled_programs`` has.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass

from . import paths as paths_mod

CONTROLS_FILENAME = "userControls.ini"
SECTION = "shortcuts"
TIMING_SECTION = "timing"

#: How long a gesture has to complete. Kept beside the shortcuts because they are
#: the same kind of thing -- how the app reads your input -- and the Controls
#: dialog is where you would look for either.
#:
#: ``double_click_ms = 0`` means **follow the operating system**. Windows has its
#: own double-click time (typically 500 ms) that other applications honour;
#: silently overriding it at startup would be a behaviour change nobody asked
#: for, so nothing is pinned until the slider is moved.
TIMING_DEFAULTS = {
    "double_click_ms": 0,
    "double_tap_ms": 200,      # the Left/Right folder-expand gesture
}

#: Bounds the dialog offers and the loader clamps to. Below ~100 ms no gesture is
#: performable; above a second it stops feeling like one action.
TIMING_MIN_MS = 100
TIMING_MAX_MS = 1000

# --------------------------------------------------------------------------
# Mouse buttons as bindings.
#
# QKeySequence cannot represent a mouse button -- there is no spelling for it --
# so a binding string may instead name one, optionally with modifiers, in the
# same "Ctrl+Shift+X" shape Qt uses for keys:
#
#     Mouse:Middle          Ctrl+Mouse:Left          Ctrl+Shift+Mouse:Back
#
# Parsing lives here, in one function, so the dialog that writes these and the
# filter that dispatches them cannot drift apart about what one means.
# --------------------------------------------------------------------------
MOUSE_PREFIX = "Mouse:"

#: The buttons that can be bound, and the name each is written as. Ordered as a
#: mouse is laid out rather than by Qt's enum value.
MOUSE_BUTTONS = ("Left", "Right", "Middle", "Back", "Forward")

#: Left and right do too much elsewhere to be taken bare -- a bare left click is
#: how you focus the field you would be binding it in. With a modifier they are
#: free, because nothing in the app claims a modified click.
MOUSE_NEEDS_MODIFIER = ("Left", "Right")

#: Modifier names, in the order Qt renders them, so a binding built here matches
#: one Qt would have spelled.
MODIFIER_NAMES = ("Ctrl", "Alt", "Shift", "Meta")


def is_mouse_binding(binding: str) -> bool:
    return MOUSE_PREFIX in (binding or "")


def format_mouse_binding(button: str, modifiers=()) -> str:
    """``("Left", ["Ctrl"])`` -> ``"Ctrl+Mouse:Left"``.

    Modifiers are emitted in a fixed order so the same gesture always produces
    the same string -- otherwise two identical bindings could fail to compare
    equal and the conflict check would miss them.
    """
    ordered = [m for m in MODIFIER_NAMES if m in set(modifiers or ())]
    return "+".join(ordered + [MOUSE_PREFIX + button])


def parse_mouse_binding(binding: str):
    """``"Ctrl+Mouse:Left"`` -> ``(("Ctrl",), "Left")``, or None.

    None for anything that is not a mouse binding *or* names a button this build
    does not know, so a file from a future build degrades to "unbound" rather
    than to a crash -- the same tolerance unknown action ids get.
    """
    if not is_mouse_binding(binding):
        return None
    parts = binding.split("+")
    button = parts[-1][len(MOUSE_PREFIX):]
    if button not in MOUSE_BUTTONS:
        return None
    modifiers = tuple(p for p in parts[:-1] if p in MODIFIER_NAMES)
    if button in MOUSE_NEEDS_MODIFIER and not modifiers:
        return None                  # never dispatch a bare left/right click
    return modifiers, button


def controls_path() -> str:
    """Beside the settings file. Resolved through the *module* so a test's
    ``monkeypatch.setattr(paths_mod, "app_dir", …)`` actually takes effect -- a
    from-import would bind the original and quietly write the real file, which
    HANDOFF.md states under "Development conventions"."""
    return os.path.join(paths_mod.app_dir(), CONTROLS_FILENAME)


@dataclass(frozen=True)
class Action:
    """One bindable thing. ``group`` only organises the Controls dialog."""

    id: str
    label: str
    group: str
    default: str = ""


#: Everything a key can be bound to. Adding an entry here is all it takes for it
#: to appear in the Controls dialog and be persisted.
ACTIONS: list[Action] = [
    # -- tabs ------------------------------------------------------------
    Action("tab.next", "Next tab", "Tabs", "Ctrl+Tab"),
    Action("tab.previous", "Previous tab", "Tabs", "Ctrl+Shift+Tab"),
    Action("tab.new", "New tab", "Tabs", "Ctrl+T"),
    Action("tab.close", "Close tab", "Tabs", "Ctrl+W"),
    Action("tab.split", "Split view", "Tabs", "Ctrl+\\"),
    Action("tab.unsplit", "Combine split view", "Tabs", "Ctrl+Shift+\\"),
    # F6 is the conventional "next pane" key on Windows, and nothing here uses it.
    Action("pane.focus_other", "Focus other track pane", "Tabs", "F6"),
    # -- playback --------------------------------------------------------
    Action("play.next", "Next track", "Playback", "Ctrl+Right"),
    Action("play.previous", "Previous track", "Playback", "Ctrl+Left"),
    # -- the track menu --------------------------------------------------
    # Not F2: that is conventionally rename, which the sidebar already uses.
    Action("track.edit", "Edit track…", "Track", "Ctrl+E"),
    Action("track.reveal", "Go to file location", "Track", ""),
    Action("track.playlists", "Playlists…", "Track", ""),
    Action("track.integrity", "Check file integrity", "Track", ""),
    Action("track.remove", "Remove from playlist", "Track", ""),
    Action("track.remove_entry", "Remove from playlist + collection entry", "Track", ""),
    Action("track.delete_file", "Delete file (Recycle Bin)", "Track", ""),
    # -- the playlist menu ------------------------------------------------
    Action("playlist.open_tab", "Open in new tab", "Playlist", ""),
    Action("playlist.select_all", "Select all playlists", "Playlist", ""),
    Action("playlist.remove_duplicates", "Remove duplicate tracks", "Playlist", ""),
    Action("playlist.integrity", "Check file integrity", "Playlist", ""),
    Action("playlist.new_playlist", "New playlist", "Playlist", ""),
    Action("playlist.new_folder", "New folder", "Playlist", ""),
    Action("playlist.duplicate", "Duplicate playlist", "Playlist", ""),
    Action("playlist.rename", "Rename", "Playlist", "F2"),
    Action("playlist.remove", "Remove playlist / folder", "Playlist", ""),
    Action("playlist.hide", "Hide", "Playlist", ""),
    # -- everything else --------------------------------------------------
    Action("app.refresh", "Refresh libraries", "General", "F5"),
    Action("app.backup", "Save backup", "General", ""),
]

ACTION_BY_ID = {a.id: a for a in ACTIONS}
GROUPS = list(dict.fromkeys(a.group for a in ACTIONS))


def defaults() -> dict:
    return {a.id: a.default for a in ACTIONS}


def conflicts(bindings: dict) -> dict:
    """``{key sequence: [action ids]}`` for any chord bound more than once.

    Two actions on one chord is the obvious way to make this feel broken -- one
    of them silently never fires -- so the dialog surfaces it rather than
    letting Qt pick.
    """
    seen: dict = {}
    for action_id, seq in bindings.items():
        if seq:
            seen.setdefault(seq, []).append(action_id)
    return {seq: ids for seq, ids in seen.items() if len(ids) > 1}


def load(path: str | None = None) -> dict:
    """The saved bindings, defaults filling anything absent."""
    out = defaults()
    target = path or controls_path()
    if not os.path.exists(target):
        return out
    parser = configparser.ConfigParser()
    try:
        parser.read(target, encoding="utf-8")
    except (OSError, configparser.Error):
        return out                       # unreadable: fall back, never crash
    if not parser.has_section(SECTION):
        return out
    for action_id, seq in parser.items(SECTION):
        if action_id in ACTION_BY_ID:    # ignore ids this build does not know
            out[action_id] = seq.strip()
    return out


def clean_ms(value, key: str) -> int:
    """One timing value, whatever the file actually holds.

    ``0`` is preserved for ``double_click_ms`` because it is meaningful there
    ("follow the OS"); everything else is clamped into range. The file is meant
    to be hand-editable, so a typo has to land on something usable rather than
    raise during startup.
    """
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return TIMING_DEFAULTS[key]
    if ms == 0 and key == "double_click_ms":
        return 0
    return max(TIMING_MIN_MS, min(TIMING_MAX_MS, ms))


def load_timing(path: str | None = None) -> dict:
    """The saved gesture timings, defaults filling anything absent."""
    out = dict(TIMING_DEFAULTS)
    target = path or controls_path()
    if not os.path.exists(target):
        return out
    parser = configparser.ConfigParser()
    try:
        parser.read(target, encoding="utf-8")
    except (OSError, configparser.Error):
        return out
    if not parser.has_section(TIMING_SECTION):
        return out
    for key, value in parser.items(TIMING_SECTION):
        if key in TIMING_DEFAULTS:
            out[key] = clean_ms(value, key)
    return out


def save(bindings: dict, path: str | None = None, timing: dict | None = None) -> bool:
    """Write the bindings, and the timings when given.

    Only known ids and keys are stored, so a stray entry in memory cannot leak
    into the file. ``timing`` comes after ``path`` because ``save(bindings, path)``
    is already called positionally.
    """
    parser = configparser.ConfigParser()
    parser[SECTION] = {a.id: (bindings.get(a.id) or "") for a in ACTIONS}
    values = dict(TIMING_DEFAULTS)
    values.update({k: clean_ms(v, k) for k, v in (timing or {}).items()
                   if k in TIMING_DEFAULTS})
    parser[TIMING_SECTION] = {k: str(v) for k, v in values.items()}
    try:
        with open(path or controls_path(), "w", encoding="utf-8") as f:
            f.write("# SlipStreamIO keyboard shortcuts.\n"
                    "# One action per line; clear a value to unbind it.\n"
                    "# [timing] is in milliseconds; double_click_ms = 0 follows\n"
                    "# the operating system's own double-click time.\n")
            parser.write(f)
    except OSError:
        return False
    return True
