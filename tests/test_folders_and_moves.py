"""Folder create/rename, playlist duplication, and drag-to-move.

The move path is driven through ``_move_nodes`` and the pure ``can_move_into`` /
``drop_target_node`` seams rather than a synthesised ``QDropEvent`` -- building
one segfaults this PySide6 build (HANDOFF.md, "Development conventions"). The
drop *wiring* that calls
them is a handful of lines in ``LibraryTree.dropEvent``.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox             # noqa: E402

from trackcleaner.config.paths import Settings                      # noqa: E402
from trackcleaner.io import backup as backup_mod                    # noqa: E402
from trackcleaner.io import recycle as recycle_mod                  # noqa: E402
from trackcleaner.io import traktor_read, traktor_write, vdj_write  # noqa: E402
from trackcleaner.model.library import (TRAKTOR, VDJ, Playlist,     # noqa: E402
                                        TreeNode, can_move_into,
                                        find_node, repath, subtree_paths)
from fixtures import ROOT_SUBNODES_COUNT, SAMPLE_NML, write_sample  # noqa: E402

TEST_UUID = "f8df219fc1ea42a7834f2fe64556a58d"      # "test", inside "breaks"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(tmp_path / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(tmp_path / "settings.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(tmp_path / "baseline.json"))
    from trackcleaner.ui.main_window import MainWindow
    paths = write_sample(tmp_path)
    settings = Settings()
    settings.paths = paths
    w = MainWindow(settings)
    w.show()
    yield w
    w.close()


_N = [0]


def _lib_from(nml_text: str):
    """Round-trip NML text back through the reader -- proves it stays parseable."""
    import tempfile
    _N[0] += 1
    p = os.path.join(tempfile.mkdtemp(), "c%d.nml" % _N[0])
    with open(p, "w", encoding="utf-8") as f:
        f.write(nml_text)
    return traktor_read.read(p)


def _nml(window) -> str:
    window._writer.flush()
    return open(window.settings.paths.nml, encoding="utf-8").read()


def _node(window, source, name):
    lib = window.libraries.get(VDJ) if source == VDJ else window.libraries.get(TRAKTOR)
    return next(n for n in lib.playlists() if n.name == name)


def _folders(window) -> str:
    return window.settings.paths.vdj_folders_dir()


def _tree_node(lib, path):
    return find_node(lib.root, path)


# ================================ model units ===============================
def _fake_tree():
    root = TreeNode("root", "folder", VDJ, node_path=())
    outer = TreeNode("Outer", "folder", VDJ, node_path=("Outer",))
    inner = TreeNode("Inner", "folder", VDJ, node_path=("Outer", "Inner"))
    leaf = TreeNode("Leaf", "playlist", VDJ, node_path=("Outer", "Inner", "Leaf"),
                    playlist=Playlist(name="Leaf", source=VDJ))
    inner.children.append(leaf)
    outer.children.append(inner)
    root.children.append(outer)
    return root, outer, inner, leaf


def test_repath_updates_the_whole_subtree():
    root, outer, inner, leaf = _fake_tree()
    outer.name = "Renamed"
    repath(outer, ())
    assert outer.node_path == ("Renamed",)
    assert inner.node_path == ("Renamed", "Inner")
    assert leaf.node_path == ("Renamed", "Inner", "Leaf")


def test_subtree_paths_zips_old_to_new():
    root, outer, inner, leaf = _fake_tree()
    before = subtree_paths(outer)
    outer.name = "Renamed"
    repath(outer, ())
    after = subtree_paths(outer)
    assert dict(zip(before, after)) == {
        ("Outer",): ("Renamed",),
        ("Outer", "Inner"): ("Renamed", "Inner"),
        ("Outer", "Inner", "Leaf"): ("Renamed", "Inner", "Leaf"),
    }


def test_can_move_into_refuses_self_descendant_and_current_parent():
    root, outer, inner, leaf = _fake_tree()
    assert can_move_into(inner, None) is True          # out to the root: fine
    assert can_move_into(leaf, outer) is True
    assert can_move_into(outer, outer) is False        # itself
    assert can_move_into(outer, inner) is False        # its own descendant
    assert can_move_into(leaf, inner) is False         # already there
    assert can_move_into(outer, None) is False         # already at the root


# =============================== writer units ===============================
def test_rename_folder_node_keeps_the_playlist_inside_it():
    out, renamed = traktor_write.rename_folder_node(SAMPLE_NML, ("breaks",), "Beats")

    assert renamed is True
    assert '<NODE TYPE="FOLDER" NAME="Beats"' in out
    assert 'NAME="breaks"' not in out
    node = _lib_from(out).find_playlist(("Beats", "test"))
    assert node is not None
    assert node.playlist.traktor_uuid == TEST_UUID
    assert len(node.playlist.track_keys) == 1


def test_rename_folder_node_rejects_unknown_and_root():
    assert traktor_write.rename_folder_node(SAMPLE_NML, ("nope",), "X") == (SAMPLE_NML, False)
    assert traktor_write.rename_folder_node(SAMPLE_NML, (), "X") == (SAMPLE_NML, False)


def test_move_playlist_node_to_the_root():
    out, moved = traktor_write.move_playlist_node(SAMPLE_NML, TEST_UUID, ("breaks",), ())

    assert moved is True
    assert re.search(r'NAME="breaks"[^>]*><SUBNODES COUNT="0">', out)
    assert re.search(r'NAME="\$ROOT"><SUBNODES COUNT="%d"' % (ROOT_SUBNODES_COUNT + 1), out)
    node = _lib_from(out).find_playlist(("test",))
    assert node is not None
    assert node.playlist.traktor_uuid == TEST_UUID, "the UUID survives a move"
    assert len(node.playlist.track_keys) == 1, "and so do its entries"


def test_move_playlist_node_into_a_folder():
    out, _ = traktor_write.move_playlist_node(SAMPLE_NML, TEST_UUID, ("breaks",), ())
    out2, moved = traktor_write.move_playlist_node(out, TEST_UUID, (), ("breaks",))

    assert moved is True
    assert _lib_from(out2).find_playlist(("breaks", "test")) is not None
    assert re.search(r'NAME="\$ROOT"><SUBNODES COUNT="%d"' % ROOT_SUBNODES_COUNT, out2)


def test_move_playlist_node_unknown_uuid_is_a_no_op():
    assert traktor_write.move_playlist_node(SAMPLE_NML, "nope", ("breaks",), ()) \
        == (SAMPLE_NML, False)


def test_move_folder_node_carries_its_subtree():
    # park a second folder at the root to move "breaks" into
    staged = traktor_write.ensure_folder_chain(SAMPLE_NML, ("Target",))
    out, moved = traktor_write.move_folder_node(staged, ("breaks",), ("Target",))

    assert moved is True
    lib = _lib_from(out)
    node = lib.find_playlist(("Target", "breaks", "test"))
    assert node is not None and node.playlist.traktor_uuid == TEST_UUID
    assert re.search(r'NAME="Target"><SUBNODES COUNT="1">', out)


def test_move_folder_into_its_own_descendant_is_refused():
    """The dangerous case: after the cut the destination no longer exists, and
    ensure_folder_chain would happily create a fresh empty one to paste into."""
    out, moved = traktor_write.move_folder_node(SAMPLE_NML, ("breaks",),
                                                ("breaks", "test"))
    assert moved is False
    assert out == SAMPLE_NML, "content must be byte-identical"


def test_move_folder_node_unknown_path_is_a_no_op():
    assert traktor_write.move_folder_node(SAMPLE_NML, ("nope",), ()) == (SAMPLE_NML, False)


def test_subfolder_dir_for_builds_the_whole_chain(tmp_path):
    root = str(tmp_path / "Folders")
    d = vdj_write.subfolder_dir_for(root, ("A", "B"))
    assert d == os.path.join(root, "A.subfolders", "B.subfolders")
    assert os.path.isdir(d)
    # ensure_subfolder_chain is the same minus the playlist name
    assert vdj_write.ensure_subfolder_chain(root, ("A", "B", "P")) == d


# ============================== folder creation =============================
def _new_folder(window, source, parent, name):
    window._pending_create = {"kind": "folder", "copy_of": None}
    window._create_node(source, parent, name)


def test_create_traktor_folder_at_root(window):
    _new_folder(window, TRAKTOR, None, "Fresh")
    out = _nml(window)

    assert '<NODE TYPE="FOLDER" NAME="Fresh"' in out
    assert re.search(r'NAME="\$ROOT"><SUBNODES COUNT="%d"' % (ROOT_SUBNODES_COUNT + 1), out)
    node = _tree_node(window.libraries.get(TRAKTOR), ("Fresh",))
    assert node is not None and node.is_folder() and node.playlist is None


def test_create_traktor_folder_inside_a_folder(window):
    breaks = _tree_node(window.libraries.get(TRAKTOR), ("breaks",))
    _new_folder(window, TRAKTOR, breaks, "Sub")
    out = _nml(window)

    assert re.search(r'NAME="breaks"[^>]*><SUBNODES COUNT="2">', out)
    assert _tree_node(window.libraries.get(TRAKTOR), ("breaks", "Sub")) is not None


def test_create_vdj_folder_makes_the_subfolders_dir(window):
    _new_folder(window, VDJ, None, "Fresh")
    window._writer.flush()
    assert os.path.isdir(os.path.join(_folders(window), "Fresh.subfolders"))


def test_a_playlist_can_be_created_inside_a_new_folder(window):
    _new_folder(window, VDJ, None, "Fresh")
    folder = _tree_node(window.libraries.get(VDJ), ("Fresh",))
    window._pending_create = {"kind": "playlist", "copy_of": None}
    window._create_node(VDJ, folder, "Inside")
    window._writer.flush()

    assert os.path.exists(os.path.join(_folders(window), "Fresh.subfolders",
                                       "Inside.vdjfolder"))
    assert _node(window, VDJ, "Inside").node_path == ("Fresh", "Inside")


def test_new_folder_row_has_no_status_square(window):
    _new_folder(window, TRAKTOR, None, "Fresh")
    node = _tree_node(window.libraries.get(TRAKTOR), ("Fresh",))
    item = window.playlist_panel.tree_for(TRAKTOR)._by_nodeid[id(node)]
    assert item.icon(0).isNull(), "folders carry no status square"


# =============================== folder rename ==============================
def test_rename_folder_carries_its_playlist(window):
    breaks = _tree_node(window.libraries.get(TRAKTOR), ("breaks",))
    child = _node(window, TRAKTOR, "test")

    window._rename_node(TRAKTOR, breaks, "Beats")
    out = _nml(window)

    assert breaks.node_path == ("Beats",)
    assert child.node_path == ("Beats", "test"), "descendants were re-pathed"
    assert _lib_from(out).find_playlist(("Beats", "test")) is not None


def test_rename_vdj_folder_moves_the_directory(window):
    _new_folder(window, VDJ, None, "Fresh")
    folder = _tree_node(window.libraries.get(VDJ), ("Fresh",))
    window._pending_create = {"kind": "playlist", "copy_of": None}
    window._create_node(VDJ, folder, "Inside")
    window._writer.flush()

    window._rename_node(VDJ, folder, "Renamed")
    window._writer.flush()

    assert not os.path.isdir(os.path.join(_folders(window), "Fresh.subfolders"))
    assert os.path.exists(os.path.join(_folders(window), "Renamed.subfolders",
                                       "Inside.vdjfolder"))


def test_rename_folder_keeps_a_hidden_descendant_hidden(window):
    """_hidden is keyed by the exact node_path, so it has to be migrated or the
    playlist silently reappears."""
    breaks = _tree_node(window.libraries.get(TRAKTOR), ("breaks",))
    window._hidden[TRAKTOR].add(("breaks", "test"))

    window._rename_node(TRAKTOR, breaks, "Beats")

    assert ("breaks", "test") not in window._hidden[TRAKTOR]
    assert ("Beats", "test") in window._hidden[TRAKTOR]


# ================================= duplicate ================================
def _duplicate(window, source, node, name=None):
    parent = window._parent_for(source, node)
    lib = window.libraries.get(VDJ) if source == VDJ else window.libraries.get(TRAKTOR)
    suggested = window._resolve_node_name(
        source, parent if parent is not None else lib.root, node.name)
    window._pending_create = {"kind": "playlist", "copy_of": node}
    window._create_node(source, parent, name or suggested)
    return suggested


def test_duplicate_traktor_playlist_copies_the_tracks(window):
    node = _node(window, TRAKTOR, "test")
    keys = list(node.playlist.track_keys)
    assert keys, "fixture playlist must have a track to make this meaningful"

    suggested = _duplicate(window, TRAKTOR, node)

    assert suggested == "test (2)"
    copy = _node(window, TRAKTOR, "test (2)")
    assert copy.playlist.track_keys == keys
    assert copy.node_path == ("breaks", "test (2)")
    out = _nml(window)
    assert re.search(r'NAME="test \(2\)"><PLAYLIST ENTRIES="%d"' % len(keys), out)


def test_duplicate_mints_a_fresh_uuid(window):
    """Two Traktor nodes sharing a UUID would make every writer that matches on
    it (membership, rename, move) hit whichever twin it found first."""
    node = _node(window, TRAKTOR, "test")
    _duplicate(window, TRAKTOR, node)
    copy = _node(window, TRAKTOR, "test (2)")

    assert copy.playlist.traktor_uuid
    assert copy.playlist.traktor_uuid != node.playlist.traktor_uuid
    out = _nml(window)
    assert out.count(node.playlist.traktor_uuid) == 1


def test_duplicate_is_independent_of_the_original(window):
    node = _node(window, TRAKTOR, "test")
    keys = list(node.playlist.track_keys)
    _duplicate(window, TRAKTOR, node)
    copy = _node(window, TRAKTOR, "test (2)")

    window._edit_membership(copy, [])

    assert node.playlist.track_keys == keys, "the original keeps its tracks"
    assert traktor_write.playlist_entry_keys(
        _nml(window), node.playlist.traktor_uuid) == keys


def test_duplicate_vdj_playlist_copies_the_flag_and_songs(window):
    window._pending_create = {"kind": "playlist", "copy_of": None}
    window._create_node(VDJ, None, "Original")
    node = _node(window, VDJ, "Original")
    node.playlist.no_duplicates = False           # ∞: must be inherited
    keys = list(window.libraries.get(VDJ).tracks)[:1]
    if keys:
        window._edit_membership(node, keys)

    _duplicate(window, VDJ, node)
    window._writer.flush()

    copy = _node(window, VDJ, "Original (2)")
    assert copy.playlist.no_duplicates is False
    assert copy.playlist.track_keys == list(node.playlist.track_keys)
    body = open(os.path.join(_folders(window), "Original (2).vdjfolder"),
                encoding="utf-8").read()
    assert 'noDuplicates="no"' in body
    assert body.count("<song ") == len(keys)


def test_duplicating_twice_keeps_counting(window):
    node = _node(window, TRAKTOR, "test")
    _duplicate(window, TRAKTOR, node)
    assert _duplicate(window, TRAKTOR, node) == "test (3)"
    names = [n.name for n in window.libraries.get(TRAKTOR).playlists()]
    assert "test (2)" in names and "test (3)" in names


def test_escape_after_duplicate_writes_nothing(window):
    node = _node(window, TRAKTOR, "test")
    tree = window.playlist_panel.tree_for(TRAKTOR)
    before = _nml(window)

    window._begin_duplicate(TRAKTOR, node)
    assert tree._pending_item is not None
    tree.discard_pending()

    assert _nml(window) == before
    assert [n.name for n in window.libraries.get(TRAKTOR).playlists()].count("test") == 1


# ================================== moving ==================================
def test_move_traktor_playlist_out_of_its_folder(window):
    node = _node(window, TRAKTOR, "test")

    window._move_nodes(TRAKTOR, [node], None)      # None target = the root
    out = _nml(window)

    assert node.node_path == ("test",)
    assert out.count('NAME="test"') == 1, "moved, not copied -- the reported bug"
    assert re.search(r'NAME="breaks"[^>]*><SUBNODES COUNT="0">', out)
    lib = _lib_from(out)
    assert lib.find_playlist(("test",)) is not None
    assert lib.find_playlist(("breaks", "test")) is None


def test_move_traktor_playlist_into_a_folder(window):
    _new_folder(window, TRAKTOR, None, "Target")
    target = _tree_node(window.libraries.get(TRAKTOR), ("Target",))
    node = _node(window, TRAKTOR, "_LOOPS")

    window._move_nodes(TRAKTOR, [node], target)
    out = _nml(window)

    assert node.node_path == ("Target", "_LOOPS")
    assert out.count('NAME="_LOOPS"') == 1
    assert _lib_from(out).find_playlist(("Target", "_LOOPS")) is not None


def test_moved_playlist_keeps_its_tracks_and_uuid(window):
    node = _node(window, TRAKTOR, "test")
    uid, keys = node.playlist.traktor_uuid, list(node.playlist.track_keys)

    window._move_nodes(TRAKTOR, [node], None)

    out = _nml(window)
    assert traktor_write.playlist_entry_keys(out, uid) == keys
    assert _lib_from(out).find_playlist(("test",)).playlist.traktor_uuid == uid


def test_move_vdj_playlist_moves_its_file(window):
    _new_folder(window, VDJ, None, "Target")
    window._pending_create = {"kind": "playlist", "copy_of": None}
    window._create_node(VDJ, None, "Roamer")
    window._writer.flush()
    node = _node(window, VDJ, "Roamer")
    old_file = node.playlist.vdj_file
    target = _tree_node(window.libraries.get(VDJ), ("Target",))

    window._move_nodes(VDJ, [node], target)
    window._writer.flush()

    new_file = os.path.join(_folders(window), "Target.subfolders", "Roamer.vdjfolder")
    assert os.path.exists(new_file)
    assert not os.path.exists(old_file)
    assert node.playlist.vdj_file == new_file
    assert node.node_path == ("Target", "Roamer")


def test_move_a_folder_carries_its_children(window):
    _new_folder(window, TRAKTOR, None, "Target")
    target = _tree_node(window.libraries.get(TRAKTOR), ("Target",))
    breaks = _tree_node(window.libraries.get(TRAKTOR), ("breaks",))
    child = _node(window, TRAKTOR, "test")

    window._move_nodes(TRAKTOR, [breaks], target)
    out = _nml(window)

    assert breaks.node_path == ("Target", "breaks")
    assert child.node_path == ("Target", "breaks", "test")
    assert _lib_from(out).find_playlist(("Target", "breaks", "test")) is not None


def test_move_into_own_descendant_is_refused(window):
    breaks = _tree_node(window.libraries.get(TRAKTOR), ("breaks",))
    inside = _node(window, TRAKTOR, "test")
    before = _nml(window)

    window._move_nodes(TRAKTOR, [breaks], inside)

    assert breaks.node_path == ("breaks",)
    assert _nml(window) == before


def test_move_to_the_parent_it_is_already_in_is_a_no_op(window):
    node = _node(window, TRAKTOR, "test")
    breaks = _tree_node(window.libraries.get(TRAKTOR), ("breaks",))
    before = _nml(window)

    window._move_nodes(TRAKTOR, [node], breaks)

    assert _nml(window) == before


def test_move_onto_a_name_clash_is_auto_suffixed(window):
    window._pending_create = {"kind": "playlist", "copy_of": None}
    window._create_node(TRAKTOR, None, "test")     # a root "test" alongside breaks/test
    by_path = {n.node_path: n for n in window.libraries.get(TRAKTOR).playlists()}
    outer, inner = by_path[("test",)], by_path[("breaks", "test")]

    window._move_nodes(TRAKTOR, [inner], None)

    assert inner.node_path == ("test (2)",)
    assert outer.node_path == ("test",), "the incumbent keeps its name"
    assert _lib_from(_nml(window)).find_playlist(("test (2)",)) is not None


def test_move_migrates_the_hidden_set(window):
    _new_folder(window, TRAKTOR, None, "Target")
    target = _tree_node(window.libraries.get(TRAKTOR), ("Target",))
    breaks = _tree_node(window.libraries.get(TRAKTOR), ("breaks",))
    window._hidden[TRAKTOR].add(("breaks", "test"))

    window._move_nodes(TRAKTOR, [breaks], target)

    assert ("breaks", "test") not in window._hidden[TRAKTOR]
    assert ("Target", "breaks", "test") in window._hidden[TRAKTOR]


def test_moving_does_not_reload_or_rediff(window, monkeypatch):
    called = []
    monkeypatch.setattr(window, "_reload_preserving_view",
                        lambda *a, **k: called.append("reload"))
    monkeypatch.setattr(window, "_recompute_status", lambda: called.append("diff"))
    window._move_nodes(TRAKTOR, [_node(window, TRAKTOR, "test")], None)
    assert called == []


def test_move_keeps_an_open_tab_working(window):
    node = _node(window, TRAKTOR, "test")
    window._on_playlists_selected([node], TRAKTOR)
    table = window._table()

    window._move_nodes(TRAKTOR, [node], None)

    assert table.nodes == [node], "the tab holds the same TreeNode object"
    assert node.node_path == ("test",)


# ============================== the drop seam ===============================
def _tree(window, source):
    return window.playlist_panel.tree_for(source)


def test_drop_target_node_resolves_folders_leaves_and_blank(window):
    tree = _tree(window, TRAKTOR)
    breaks = _tree_node(window.libraries.get(TRAKTOR), ("breaks",))
    leaf = _node(window, TRAKTOR, "test")
    tree.select_node(leaf)                          # expands breaks so rows exist

    assert tree.drop_target_node(None) is None                      # empty space
    assert tree.drop_target_node(tree._by_nodeid[id(breaks)]) is breaks
    # a playlist row means "next to me": its parent folder takes the drop
    assert tree.drop_target_node(tree._by_nodeid[id(leaf)]) is breaks


def test_drop_target_of_a_top_level_playlist_is_the_root(window):
    tree = _tree(window, TRAKTOR)
    loops = _node(window, TRAKTOR, "_LOOPS")
    assert tree.drop_target_node(tree._by_nodeid[id(loops)]) is None


def test_internal_drag_is_only_this_trees_own(window):
    """A drag from the other program's tree must not be treated as a move."""
    from PySide6.QtCore import QMimeData

    class _Ev:
        def __init__(self, src):
            self._src = src
            self._mime = QMimeData()

        def source(self):
            return self._src

        def mimeData(self):
            return self._mime

    vdj, traktor = _tree(window, VDJ), _tree(window, TRAKTOR)
    assert vdj._is_internal(_Ev(vdj)) is True
    assert vdj._is_internal(_Ev(traktor)) is False
    assert vdj._is_internal(_Ev(None)) is False


# ================================= removal ==================================
@pytest.fixture
def yes(monkeypatch):
    """Accept the removal confirmation."""
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)


@pytest.fixture
def no(monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.No)


@pytest.fixture
def recycled(monkeypatch):
    """Record what would go to the Recycle Bin -- never touch the real one."""
    seen = []

    def fake(path):
        seen.append(path)
        if os.path.isdir(path):
            import shutil
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)
        return True

    monkeypatch.setattr(recycle_mod, "send_to_recycle_bin", fake)
    return seen


# ----------------------------- writer units --------------------------------
def test_remove_playlist_node_leaves_its_siblings_alone():
    out, removed = traktor_write.remove_playlist_node(SAMPLE_NML, TEST_UUID, ("breaks",))

    assert removed is True
    assert 'NAME="test"' not in out
    assert re.search(r'NAME="breaks"[^>]*><SUBNODES COUNT="0">', out)
    assert re.search(r'NAME="\$ROOT"><SUBNODES COUNT="%d"' % ROOT_SUBNODES_COUNT, out)
    for kept in ("_LOOPS", "_RECORDINGS", "Preparation"):
        assert 'NAME="%s"' % kept in out
    assert _lib_from(out).find_playlist(("breaks", "test")) is None


def test_remove_playlist_node_keeps_the_collection_entries():
    """Removing a playlist must not touch the tracks it pointed at."""
    out, _ = traktor_write.remove_playlist_node(SAMPLE_NML, TEST_UUID, ("breaks",))
    assert '<COLLECTION ENTRIES="2"' in out
    assert len(_lib_from(out).tracks) == 2


def test_remove_playlist_node_unknown_uuid_is_a_no_op():
    assert traktor_write.remove_playlist_node(SAMPLE_NML, "nope", ("breaks",)) \
        == (SAMPLE_NML, False)


def test_remove_folder_node_takes_its_contents():
    out, removed = traktor_write.remove_folder_node(SAMPLE_NML, ("breaks",))

    assert removed is True
    assert 'NAME="breaks"' not in out and 'NAME="test"' not in out
    assert re.search(r'NAME="\$ROOT"><SUBNODES COUNT="%d"' % (ROOT_SUBNODES_COUNT - 1), out)
    lib = _lib_from(out)
    assert lib.find_playlist(("breaks", "test")) is None
    assert len(lib.tracks) == 2, "the collection is untouched"


def test_remove_folder_node_rejects_unknown_and_root():
    assert traktor_write.remove_folder_node(SAMPLE_NML, ("nope",)) == (SAMPLE_NML, False)
    assert traktor_write.remove_folder_node(SAMPLE_NML, ()) == (SAMPLE_NML, False)


# ------------------------------ window level -------------------------------
def test_remove_traktor_playlist(window, yes):
    node = _node(window, TRAKTOR, "test")

    window._remove_node(TRAKTOR, node)
    out = _nml(window)

    assert 'NAME="test"' not in out
    assert not any(n.name == "test" for n in window.libraries.get(TRAKTOR).playlists())
    assert id(node) not in window.playlist_panel.tree_for(TRAKTOR)._by_nodeid
    assert _lib_from(out).find_playlist(("breaks", "test")) is None


def test_remove_traktor_folder_takes_its_playlists(window, yes):
    breaks = _tree_node(window.libraries.get(TRAKTOR), ("breaks",))

    window._remove_node(TRAKTOR, breaks)
    out = _nml(window)

    assert _tree_node(window.libraries.get(TRAKTOR), ("breaks",)) is None
    assert not any(n.node_path == ("breaks", "test")
                   for n in window.libraries.get(TRAKTOR).playlists())
    assert 'NAME="breaks"' not in out


# ---------------------- removing several at once ---------------------------
# Reported: with several playlists highlighted, Remove took only the one under
# the cursor. Right-click deliberately does not change the selection, so acting
# on the moused-over node alone silently ignored the rest.
def _select(window, source, nodes):
    tree = window.playlist_panel.tree_for(source)
    tree.clearSelection()
    for node in nodes:
        tree.select_node(node)
    return tree


def test_removing_takes_the_whole_selection(window, yes):
    window._pending_create = {"kind": "playlist", "copy_of": None}
    window._create_node(TRAKTOR, None, "First")
    window._pending_create = {"kind": "playlist", "copy_of": None}
    window._create_node(TRAKTOR, None, "Second")
    window._writer.flush()
    first = _node(window, TRAKTOR, "First")
    second = _node(window, TRAKTOR, "Second")
    tree = window.playlist_panel.tree_for(TRAKTOR)
    monkey = [first, second]
    tree.selected_top_nodes = lambda: list(monkey)

    window._remove_node(TRAKTOR, first)      # right-clicked one of the two
    window._writer.flush()

    names = [n.name for n in window.libraries.get(TRAKTOR).playlists()]
    assert "First" not in names
    assert "Second" not in names, "the rest of the selection was ignored"


def test_removing_an_unselected_node_takes_only_that_one(window, yes):
    """Right-click on a row outside the selection acts on that row alone -- the
    same rule Sync uses, so the two behave alike."""
    window._pending_create = {"kind": "playlist", "copy_of": None}
    window._create_node(TRAKTOR, None, "Lonely")
    window._writer.flush()
    lonely = _node(window, TRAKTOR, "Lonely")
    other = _node(window, TRAKTOR, "test")
    tree = window.playlist_panel.tree_for(TRAKTOR)
    tree.selected_top_nodes = lambda: [other]

    window._remove_node(TRAKTOR, lonely)
    window._writer.flush()

    names = [n.name for n in window.libraries.get(TRAKTOR).playlists()]
    assert "Lonely" not in names
    assert "test" in names, "a node outside the selection must not be touched"


def test_a_node_inside_a_selected_folder_is_not_removed_twice(window, yes):
    """Removing the folder takes it anyway; removing it again would look for a
    parent that has already gone."""
    breaks = _tree_node(window.libraries.get(TRAKTOR), ("breaks",))
    inside = _node(window, TRAKTOR, "test")
    tree = window.playlist_panel.tree_for(TRAKTOR)
    tree.selected_top_nodes = lambda: [breaks, inside]

    window._remove_node(TRAKTOR, breaks)
    window._writer.flush()

    assert _tree_node(window.libraries.get(TRAKTOR), ("breaks",)) is None
    assert not any(n.name == "test"
                   for n in window.libraries.get(TRAKTOR).playlists())


def test_one_confirmation_covers_the_whole_selection(window, monkeypatch):
    """Three dialogs for three playlists would be worse than the bug."""
    from PySide6.QtWidgets import QMessageBox
    asked = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: asked.append(a[2]) or QMessageBox.Yes)
    window._pending_create = {"kind": "playlist", "copy_of": None}
    window._create_node(TRAKTOR, None, "A")
    window._pending_create = {"kind": "playlist", "copy_of": None}
    window._create_node(TRAKTOR, None, "B")
    window._writer.flush()
    a, b = _node(window, TRAKTOR, "A"), _node(window, TRAKTOR, "B")
    window.playlist_panel.tree_for(TRAKTOR).selected_top_nodes = lambda: [a, b]

    window._remove_node(TRAKTOR, a)
    window._writer.flush()

    assert len(asked) == 1, "one confirmation, not one per item"
    assert "A" in asked[0] and "B" in asked[0], "it must name what is going"


def test_declining_removes_nothing(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.No)
    node = _node(window, TRAKTOR, "test")

    window._remove_node(TRAKTOR, node)

    assert any(n.name == "test" for n in window.libraries.get(TRAKTOR).playlists())


def test_remove_vdj_playlist_recycles_its_file(window, yes, recycled):
    window._pending_create = {"kind": "playlist", "copy_of": None}
    window._create_node(VDJ, None, "Doomed")
    window._writer.flush()
    node = _node(window, VDJ, "Doomed")
    path = node.playlist.vdj_file
    assert os.path.exists(path)

    window._remove_node(VDJ, node)
    window._writer.flush()

    assert recycled == [path], "never a permanent delete"
    assert not os.path.exists(path)
    assert not any(n.name == "Doomed" for n in window.libraries.get(VDJ).playlists())


def test_remove_vdj_folder_recycles_the_directory(window, yes, recycled):
    _new_folder(window, VDJ, None, "Doomed")
    folder = _tree_node(window.libraries.get(VDJ), ("Doomed",))
    window._pending_create = {"kind": "playlist", "copy_of": None}
    window._create_node(VDJ, folder, "Inside")
    window._writer.flush()
    d = os.path.join(_folders(window), "Doomed.subfolders")
    assert os.path.isdir(d)

    window._remove_node(VDJ, folder)
    window._writer.flush()

    assert d in recycled
    assert not os.path.isdir(d)
    assert _tree_node(window.libraries.get(VDJ), ("Doomed",)) is None
    assert not any(n.name == "Inside" for n in window.libraries.get(VDJ).playlists())


def test_declining_the_confirmation_changes_nothing(window, no):
    node = _node(window, TRAKTOR, "test")
    before = _nml(window)

    window._remove_node(TRAKTOR, node)

    assert _nml(window) == before
    assert _node(window, TRAKTOR, "test") is node


def test_remove_folder_purges_a_hidden_descendant(window, yes):
    breaks = _tree_node(window.libraries.get(TRAKTOR), ("breaks",))
    window._hidden[TRAKTOR].add(("breaks", "test"))

    window._remove_node(TRAKTOR, breaks)

    assert ("breaks", "test") not in window._hidden[TRAKTOR]


def test_remove_clears_a_tab_showing_the_playlist(window, yes):
    node = _node(window, TRAKTOR, "test")
    window._on_playlists_selected([node], TRAKTOR)
    table = window._table()
    assert table.nodes == [node]

    window._remove_node(TRAKTOR, node)

    assert table.nodes == []
    assert table.model().rowCount() == 0
    group = window.track_tabs._group_of(table)
    assert group.tabText(group.indexOf(table)) == "empty"
    assert window._current_nodes == []


def test_removing_does_not_reload_or_rediff(window, yes, monkeypatch):
    called = []
    monkeypatch.setattr(window, "_reload_preserving_view",
                        lambda *a, **k: called.append("reload"))
    monkeypatch.setattr(window, "_recompute_status", lambda: called.append("diff"))
    window._remove_node(TRAKTOR, _node(window, TRAKTOR, "test"))
    assert called == []


def test_remove_keeps_the_tracks_in_the_collection(window, yes):
    """The whole point of not snapshotting: no track goes anywhere."""
    before = set(window.libraries.get(TRAKTOR).tracks)
    window._remove_node(TRAKTOR, _node(window, TRAKTOR, "test"))
    assert set(window.libraries.get(TRAKTOR).tracks) == before
    assert len(_lib_from(_nml(window)).tracks) == len(before)


# =============================== menu order =================================
_MENU_CB = dict(select_all=lambda: None, sync=lambda m, t: None, hide=lambda: None,
                open_in_new_tab=lambda: None, check_integrity=lambda: None,
                clean_up=lambda: None,
                remove_duplicates=lambda: None, new_playlist=lambda: None,
                new_folder=lambda: None, duplicate=lambda: None,
                rename=lambda: None, remove=lambda: None,
                # One other program on, which is the shape the menu has always
                # had; the multi-target shape is covered further down.
                sync_targets=[(TRAKTOR, "Traktor")])


class _CB:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _labels(menu):
    return [("---" if a.isSeparator() else a.text()) for a in menu.actions()]


def test_playlist_menu_is_in_the_requested_order(app):
    from trackcleaner.ui import context_menus
    menu = context_menus.playlist_menu(
        None, VDJ, _CB(can_duplicate=True, rename_label="Rename playlist",
                       remove_label="Remove playlist", duplicates_allowed=False,
                       toggle_duplicates=lambda c: None, **_MENU_CB))

    assert _labels(menu) == [
        "Open in new tab",
        "Select all playlists",
        "Remove duplicate tracks",
        "Allow duplicate tracks",
        "Check file integrity",
        "Clean up…",
        "Sync selected → Traktor",
        "---",
        "New playlist",
        "New folder",
        "Duplicate playlist",
        "Rename playlist",
        "---",
        "Remove playlist",
        "---",
        "Hide",
    ]


def test_folder_menu_labels_and_gating(app):
    from trackcleaner.ui import context_menus
    menu = context_menus.playlist_menu(
        None, TRAKTOR, _CB(can_duplicate=False, rename_label="Rename folder",
                           remove_label="Remove folder", **_MENU_CB))
    labels = _labels(menu)

    # no "Allow duplicate tracks" (Traktor / not a VDJ playlist)
    assert "Allow duplicate tracks" not in labels
    assert labels[-3:] == ["Remove folder", "---", "Hide"]
    dup = next(a for a in menu.actions() if a.text() == "Duplicate playlist")
    assert not dup.isEnabled(), "still folders-only"
    assert next(a for a in menu.actions() if a.text() == "Remove folder").isEnabled()


# ---------------------- sync menu: however many programs are on -------------
def _menu_with_targets(targets):
    from trackcleaner.ui import context_menus
    cb = dict(_MENU_CB)
    cb["sync_targets"] = targets
    return context_menus.playlist_menu(
        None, VDJ, _CB(can_duplicate=True, rename_label="Rename playlist",
                       remove_label="Remove playlist", **cb))


def _sync_action(menu):
    return next(a for a in menu.actions() if a.text().startswith("Sync selected"))


def test_two_programs_keep_the_familiar_flat_sync_menu(app):
    """One target: the title names it, and the modes are one level down --
    exactly the menu that existed before there could be a third program."""
    from trackcleaner.ui import context_menus
    menu = _menu_with_targets([(TRAKTOR, "Traktor")])

    act = _sync_action(menu)
    assert act.text() == "Sync selected → Traktor"
    assert [a.text() for a in act.menu().actions()] == \
        [label for _, label in context_menus.SYNC_MODES]


def test_more_programs_add_a_level_per_target(app):
    """"The other one" stops identifying a program, so the user picks -- and the
    fan-out entry leads, ahead of a separator."""
    from trackcleaner.ui import context_menus
    menu = _menu_with_targets([(TRAKTOR, "Traktor"), ("slipstream", "SlipStream")])

    act = _sync_action(menu)
    assert act.text() == "Sync selected →"
    entries = [a for a in act.menu().actions() if not a.isSeparator()]
    assert [a.text() for a in entries] == \
        [context_menus.ALL_TARGETS_LABEL, "Traktor", "SlipStream"]
    for entry in entries:
        assert [a.text() for a in entry.menu().actions()] == \
            [label for _, label in context_menus.SYNC_MODES], \
            f"{entry.text()} must offer every mode"


def test_the_fan_out_entry_is_separated_from_the_individual_targets(app):
    """It does something categorically different -- writing to every program at
    once -- so it must not read as just another program in the list."""
    from trackcleaner.ui import context_menus
    menu = _menu_with_targets([(TRAKTOR, "Traktor"), ("slipstream", "SlipStream")])

    actions = _sync_action(menu).menu().actions()

    assert actions[0].text() == context_menus.ALL_TARGETS_LABEL
    assert actions[1].isSeparator()


def test_one_target_gets_no_fan_out_entry(app):
    """"All programs" and that one program would be the same thing; offering
    both only invites the reader to wonder how they differ."""
    from trackcleaner.ui import context_menus
    menu = _menu_with_targets([(TRAKTOR, "Traktor")])

    labels = [a.text() for a in _sync_action(menu).menu().actions()]
    assert context_menus.ALL_TARGETS_LABEL not in labels


def test_the_fan_out_passes_every_target_id(app):
    """The whole feature in one assertion: one click, every active program."""
    from trackcleaner.ui import context_menus
    seen = []
    cb = dict(_MENU_CB)
    cb["sync_targets"] = [(TRAKTOR, "Traktor"), ("slipstream", "SlipStream")]
    cb["sync"] = lambda mode, target: seen.append((mode, target))
    menu = context_menus.playlist_menu(
        None, VDJ, _CB(can_duplicate=True, rename_label="Rename playlist",
                       remove_label="Remove playlist", **cb))

    targets_menu = _sync_action(menu).menu()
    all_entry = next(a for a in targets_menu.actions()
                     if a.text() == context_menus.ALL_TARGETS_LABEL)
    modes_menu = all_entry.menu()
    modes_menu.actions()[1].trigger()             # Sync Update

    assert seen == [(context_menus.SYNC_MODES[1][0], [TRAKTOR, "slipstream"])]


def test_the_only_program_on_gets_a_disabled_sync_entry(app):
    """Shown-but-disabled rather than absent, so the menu doesn't change shape
    depending on the session."""
    menu = _menu_with_targets([])

    act = _sync_action(menu)
    assert act.text() == "Sync selected"
    assert not act.isEnabled()


def test_picking_a_target_passes_that_target_to_the_callback(app):
    """The point of the whole change: the sync goes where the user pointed."""
    from trackcleaner.ui import context_menus
    seen = []
    cb = dict(_MENU_CB)
    cb["sync_targets"] = [(TRAKTOR, "Traktor"), ("slipstream", "SlipStream")]
    cb["sync"] = lambda mode, target: seen.append((mode, target))
    menu = context_menus.playlist_menu(
        None, VDJ, _CB(can_duplicate=True, rename_label="Rename playlist",
                       remove_label="Remove playlist", **cb))

    # Each menu is held in a local: letting one fall out of scope mid-chain has
    # shiboken collect the wrapper and the C++ QMenu with it.
    targets_menu = _sync_action(menu).menu()
    slipstream = next(a for a in targets_menu.actions() if a.text() == "SlipStream")
    modes_menu = slipstream.menu()
    modes_menu.actions()[0].trigger()                 # first mode

    assert seen == [(context_menus.SYNC_MODES[0][0], "slipstream")]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
