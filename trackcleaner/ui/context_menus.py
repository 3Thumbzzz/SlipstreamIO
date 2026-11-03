"""Right-click context menus for playlists/folders and tracks.

Builders return a configured QMenu; the caller supplies a callback bag. The
layout is final: entries that work now (P1) are wired; the destructive editing
entries are present but disabled until their phase lands, so the menu never
changes shape under the user.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMenu

# No program imports on purpose: nothing in here branches on which program it is
# building a menu for any more. Everything program-specific -- the sync targets,
# the collection file's name, whether duplicates are a thing -- arrives in the
# callback bag, which is what lets a new program appear in these menus with no
# edit to this file.

SYNC_MODES = [("new", "Sync New Only"),
              ("update", "Sync Update"),
              ("overwrite", "Sync Overwrite…")]

#: The fan-out entry: one action that syncs to every active program at once.
#: Shown only when there are two or more targets -- with one, "all" and that one
#: program are the same thing, and offering both invites the reader to wonder how
#: they differ.
ALL_TARGETS_LABEL = "All programs"


def _stub(menu: QMenu, label: str):
    """A present-but-disabled entry, so the menu layout is stable across phases."""
    a = menu.addAction(label)
    a.setEnabled(False)
    return a


def _submenu(parent_menu: QMenu, title: str) -> QMenu:
    """Add a submenu that survives long enough to be used.

    ``QMenu.addMenu(title)`` builds the menu and returns it, but nothing holds a
    Python reference -- so shiboken is free to collect the wrapper, taking the
    C++ QMenu with it and leaving an entry that raises when opened. A single
    level got away with it; nesting one submenu inside another does not, and the
    failure is intermittent because it depends on when the collector runs.
    Parenting it explicitly *and* keeping a reference on its owner makes it
    deterministic.
    """
    sub = QMenu(title, parent_menu)
    parent_menu.addMenu(sub)
    kept = getattr(parent_menu, "_kept_submenus", None)
    if kept is None:
        kept = parent_menu._kept_submenus = []
    kept.append(sub)
    return sub


def _add_sync_menu(menu: QMenu, callbacks) -> QMenu:
    """The 'Sync selected → …' entry, shaped by how many programs are on.

    ``callbacks.sync_targets`` is ``[(adapter_id, display_name), …]`` -- every
    loaded program except the one being synced from. With exactly one the menu
    keeps its familiar single level and names the target in its title; with more
    it gains a level per target, because "the other one" no longer identifies a
    program and silently picking one would write somewhere the user never named.

    Two or more targets also get an :data:`ALL_TARGETS_LABEL` entry first, which
    fans the sync out to all of them -- otherwise "push this playlist everywhere"
    costs one trip through the menu per program, and syncing to one and
    forgetting the other is the drift the app exists to prevent.
    """
    targets = list(getattr(callbacks, "sync_targets", []) or [])
    if not targets:
        # Nothing to sync into -- shown disabled rather than hidden, so the menu
        # doesn't change shape depending on the session.
        return _stub(menu, "Sync selected")

    if len(targets) == 1:
        target_id, target_name = targets[0]
        sub = _submenu(menu, f"Sync selected → {target_name}")
        _add_sync_modes(sub, callbacks, target_id)
        return sub

    sub = _submenu(menu, "Sync selected →")
    _add_sync_modes(_submenu(sub, ALL_TARGETS_LABEL), callbacks,
                    [tid for tid, _name in targets])
    sub.addSeparator()
    for target_id, target_name in targets:
        _add_sync_modes(_submenu(sub, target_name), callbacks, target_id)
    return sub


def _add_sync_modes(menu: QMenu, callbacks, target) -> None:
    """``target`` is one adapter id, or a list of them for the fan-out entry.
    Passed through untouched -- this module knows nothing about programs, so the
    window is what decides that a list means "each of these in turn"."""
    for mode, label in SYNC_MODES:
        a = menu.addAction(label)
        a.triggered.connect(
            lambda _=False, m=mode, t=target: callbacks.sync(m, t))


def playlist_menu(parent, source: str, callbacks) -> QMenu:
    """callbacks: .select_all(), .sync(mode), .hide(), .open_in_new_tab(),
    .check_integrity(), .clean_up(), .remove_duplicates(), .new_playlist(),
    .new_folder(), .duplicate(), .rename(), .remove(). Optional: .duplicates_allowed
    (bool | None) and .toggle_duplicates(bool) -- when duplicates_allowed is not
    None a checkable 'Allow duplicate tracks' entry is shown (VDJ playlists);
    .rename_label / .remove_label name those entries for the node kind;
    .can_duplicate (bool) greys out Duplicate on folders, which would need a
    recursive subtree copy.

    Three groups, in use-order: what you do WITH the playlist, then what you do
    TO the tree, then the destructive/visibility entries kept apart from both.
    """
    menu = QMenu(parent)

    menu.addAction("Open in new tab", callbacks.open_in_new_tab)
    menu.addAction("Select all playlists", callbacks.select_all)
    menu.addAction("Remove duplicate tracks", callbacks.remove_duplicates)
    allowed = getattr(callbacks, "duplicates_allowed", None)
    if allowed is not None:
        act = menu.addAction("Allow duplicate tracks")
        act.setCheckable(True)
        act.setChecked(bool(allowed))
        act.toggled.connect(lambda checked: callbacks.toggle_duplicates(checked))
    menu.addAction("Check file integrity", callbacks.check_integrity)
    menu.addAction("Clean up…", callbacks.clean_up)

    _add_sync_menu(menu, callbacks)
    menu.addSeparator()

    menu.addAction("New playlist", callbacks.new_playlist)
    menu.addAction("New folder", callbacks.new_folder)
    if getattr(callbacks, "can_duplicate", False):
        menu.addAction("Duplicate playlist", callbacks.duplicate)
    else:
        _stub(menu, "Duplicate playlist")
    menu.addAction(getattr(callbacks, "rename_label", "Rename"), callbacks.rename)
    menu.addSeparator()

    menu.addAction(getattr(callbacks, "remove_label", "Remove"), callbacks.remove)
    menu.addSeparator()

    menu.addAction("Hide", callbacks.hide)
    return menu


def blank_menu(parent, source: str, callbacks) -> QMenu:
    """The menu for empty space in a library tree: there is no node under the
    cursor, so the only meaningful actions create at that software's root.
    callbacks: .new_playlist(), .new_folder()."""
    menu = QMenu(parent)
    menu.addAction("New playlist", callbacks.new_playlist)
    menu.addAction("New folder", callbacks.new_folder)
    return menu


def hidden_menu(parent, callbacks) -> QMenu:
    menu = QMenu(parent)
    menu.addAction("Unhide", callbacks.unhide)
    return menu


def track_menu(parent, source: str, callbacks) -> QMenu:
    """callbacks: .sync_track(target), .edit(), .show_playlists(),
    .check_integrity(), .reveal(), .remove_from_playlist(),
    .remove_with_entry(), .delete_file().

    Optional: .sync_targets (as in :func:`playlist_menu`); .collection_label, the
    name of this program's collection file for the "remove entry" wording;
    .can_reveal (False greys out "Go to file location" when the file is not
    there); .is_tombstone; .source_has_entry (False greys out Sync Track); and
    .bring_in_from, ``[(program_id, name)]``, with .bring_in(program_id) --
    "Add to this playlist from …" on a preview row of a track another program
    added.

    ``is_tombstone`` marks a **sync-preview row** -- a track this playlist does
    not actually contain yet, shown so you can see what a sync will bring in.
    Removing it is meaningless and "Delete file" would act on a file the playlist
    never claimed, so those entries are left out entirely rather than disabled:
    the row is not a member, so the operations do not belong on its menu at all.
    """
    menu = QMenu(parent)
    coll = getattr(callbacks, "collection_label", "") or "the collection"
    is_tombstone = bool(getattr(callbacks, "is_tombstone", False))

    # A preview row of a track another program added: what can be done is to
    # bring it in from there. ``bring_in_from`` lists the programs that hold it.
    for holder_id, holder_name in getattr(callbacks, "bring_in_from", None) or ():
        menu.addAction(f"Add to this playlist from {holder_name}",
                       lambda h=holder_id: callbacks.bring_in(h))

    targets = list(getattr(callbacks, "sync_targets", []) or [])
    if not getattr(callbacks, "source_has_entry", True):
        # Nothing here to sync *from*: the track is not in this program's
        # collection. Offering it gave "Track not found in source library".
        _stub(menu, "Sync Track (not in this program's collection)")
    elif not targets:
        _stub(menu, "Sync Track")
    elif len(targets) == 1:
        target_id, target_name = targets[0]
        menu.addAction(f"Sync Track → {target_name}",
                       lambda t=target_id: callbacks.sync_track(t))
    else:
        sub = _submenu(menu, "Sync Track →")
        # Same fan-out entry as the playlist menu, so the two do not diverge.
        all_ids = [tid for tid, _name in targets]
        sub.addAction(ALL_TARGETS_LABEL,
                      lambda t=all_ids: callbacks.sync_track(t))
        sub.addSeparator()
        for target_id, target_name in targets:
            sub.addAction(target_name,
                          lambda t=target_id: callbacks.sync_track(t))
    menu.addAction("Edit…", callbacks.edit)
    menu.addAction("Playlists…", callbacks.show_playlists)
    menu.addAction("Check file integrity", callbacks.check_integrity)
    reveal = menu.addAction("Go to file location", callbacks.reveal)
    reveal.setEnabled(bool(getattr(callbacks, "can_reveal", True)))
    if not is_tombstone:
        menu.addSeparator()
        menu.addAction("Remove from playlist", callbacks.remove_from_playlist)
        menu.addAction(f"Remove from playlist + {coll} entry…",
                       callbacks.remove_with_entry)
        menu.addAction("Delete file (Recycle Bin)…", callbacks.delete_file)
    return menu
