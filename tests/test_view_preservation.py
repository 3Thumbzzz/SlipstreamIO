"""The playlist tree view survives a reload (as happens after a Sync).

A reload rebuilds every tree item from scratch, so expansion and selection can
only be carried across by node_path. These tests pin that round-trip and that
restoring the selection does not re-fire the selection signal.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                        # noqa: E402

from trackcleaner.model.library import (Library, Playlist, TreeNode,  # noqa: E402
                                        TRAKTOR, VDJ)
from trackcleaner.ui.playlist_trees import PlaylistPanel          # noqa: E402
from trackcleaner.model.library import TRAKTOR, VDJ                  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _lib(source):
    """root / FolderA / (Sub / P1, P2)  +  root / P3."""
    def pl(name, path):
        return TreeNode(name=name, kind="playlist", source=source, node_path=path,
                        playlist=Playlist(name=name, source=source, track_keys=[]))
    sub = TreeNode(name="Sub", kind="folder", source=source, node_path=("FolderA", "Sub"),
                   children=[pl("P1", ("FolderA", "Sub", "P1"))])
    folder = TreeNode(name="FolderA", kind="folder", source=source, node_path=("FolderA",),
                      children=[sub, pl("P2", ("FolderA", "P2"))])
    root = TreeNode(name="root", kind="folder", source=source,
                    children=[folder, pl("P3", ("P3",))])
    return Library(source=source, root=root)


def _fresh(source):
    return _lib(source)


@pytest.fixture
def panel(app):
    p = PlaylistPanel()
    # load() is keyed by adapter id rather than taking a fixed pair, so the
    # panel supports however many programs are registered.
    p.load({VDJ: _fresh(VDJ), TRAKTOR: _fresh(TRAKTOR)}, {})
    p.resize(300, 760)
    p.show()
    app.processEvents()
    yield p
    p.close()


def _item(tree, path):
    return tree._items_by_path()[path]


def test_expanded_and_selected_paths_round_trip_a_reload(app, panel):
    tree = panel.trees[VDJ]
    _item(tree, ("FolderA",)).setExpanded(True)
    _item(tree, ("FolderA", "Sub")).setExpanded(True)
    _item(tree, ("FolderA", "Sub", "P1")).setSelected(True)
    app.processEvents()

    snap = panel.snapshot_view()
    # a Sync would do exactly this: rebuild both trees, then restore
    panel.load({VDJ: _fresh(VDJ), TRAKTOR: _fresh(TRAKTOR)}, {})
    panel.restore_view(snap)
    app.processEvents()

    tree = panel.trees[VDJ]
    assert _item(tree, ("FolderA",)).isExpanded()
    assert _item(tree, ("FolderA", "Sub")).isExpanded()
    assert ("FolderA", "Sub", "P1") in tree.selected_paths()


def test_collapsed_folder_stays_collapsed(app, panel):
    tree = panel.trees[VDJ]
    _item(tree, ("FolderA",)).setExpanded(True)          # only the top folder
    app.processEvents()
    snap = panel.snapshot_view()
    panel.load({VDJ: _fresh(VDJ), TRAKTOR: _fresh(TRAKTOR)}, {})
    panel.restore_view(snap)
    app.processEvents()
    tree = panel.trees[VDJ]
    assert _item(tree, ("FolderA",)).isExpanded()
    assert not _item(tree, ("FolderA", "Sub")).isExpanded()


def test_restoring_selection_does_not_re_emit(app, panel):
    """The caller reloads the table itself after restore, so a selection signal
    here would double the work (and could fight the restore)."""
    tree = panel.trees[VDJ]
    fired = []
    panel.playlists_selected.connect(lambda nodes, src: fired.append(src))
    tree.select_paths({("P3",)})
    app.processEvents()
    assert fired == [], "select_paths must be silent"
    assert ("P3",) in tree.selected_paths()


def test_each_tree_keeps_its_own_selection(app, panel):
    panel.trees[VDJ].select_paths({("P3",)})
    panel.trees[TRAKTOR].select_paths({("FolderA", "P2")})
    app.processEvents()
    snap = panel.snapshot_view()
    panel.load({VDJ: _fresh(VDJ), TRAKTOR: _fresh(TRAKTOR)}, {})
    panel.restore_view(snap)
    app.processEvents()
    assert ("P3",) in panel.trees[VDJ].selected_paths()
    assert ("FolderA", "P2") in panel.trees[TRAKTOR].selected_paths()


def test_a_vanished_path_is_ignored_on_restore(app, panel):
    """A playlist deleted by the sync just drops out of the restore, silently."""
    snap = {VDJ: {"expanded": {("Nonexistent",)}, "selected": {("Gone",)}},
            TRAKTOR: {"expanded": set(), "selected": set()}}
    panel.restore_view(snap)                 # must not raise
    app.processEvents()
    assert panel.trees[VDJ].selected_paths() == set()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

