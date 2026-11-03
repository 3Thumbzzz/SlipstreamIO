"""Creating and renaming playlists from the sidebar.

The inline editor is driven through ``LibraryTree``'s own seams
(``begin_new_node`` / ``commit_pending`` / ``discard_pending``) rather than
by synthesising key events -- the same convention the drop tests follow for
``QDropEvent``. ``commit_pending`` emits on the next event-loop tick, so the
tests call the main window's handler directly after starting the edit, which is
exactly what that emit does.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint                                   # noqa: E402
from PySide6.QtWidgets import QApplication                          # noqa: E402

from trackcleaner.config.paths import Settings                      # noqa: E402
from trackcleaner.io import backup as backup_mod                    # noqa: E402
from trackcleaner.io import traktor_read, traktor_write             # noqa: E402
from trackcleaner.model.library import TRAKTOR, VDJ, find_node      # noqa: E402
from trackcleaner.ui import context_menus                           # noqa: E402
from fixtures import ROOT_SUBNODES_COUNT, SAMPLE_NML, write_sample  # noqa: E402

TEST_UUID = "f8df219fc1ea42a7834f2fe64556a58d"      # the "test" playlist in breaks
LOOPS_UUID = "389ba1f701cf4fa491b68b550da72852"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    """A real MainWindow over the tiny nml/xml fixture, with backups, settings
    and baseline all redirected into tmp_path (see test_membership_editing)."""
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(tmp_path / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(tmp_path / "settings.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(tmp_path / "baseline.json"))
    from trackcleaner.ui.main_window import MainWindow   # deferred: needs QT_QPA_PLATFORM
    paths = write_sample(tmp_path)
    settings = Settings()
    settings.paths = paths
    w = MainWindow(settings)
    w.show()
    yield w
    w.close()


def _nml(window) -> str:
    window._writer.flush()
    return open(window.settings.paths.nml, encoding="utf-8").read()


def _node(window, source, name):
    lib = window.libraries.get(VDJ) if source == VDJ else window.libraries.get(TRAKTOR)
    return next(n for n in lib.playlists() if n.name == name)


def _folders(window) -> str:
    return window.settings.paths.vdj_folders_dir()


# ============================ the NML rename writer =========================
def test_rename_playlist_node_targets_only_its_uuid():
    out, renamed = traktor_write.rename_playlist_node(SAMPLE_NML, TEST_UUID, "Renamed")

    assert renamed is True
    assert re.search(r'NAME="Renamed"><PLAYLIST ENTRIES="1"[^>]*UUID="%s"' % TEST_UUID, out)
    assert 'NAME="test"' not in out
    # every other node untouched
    assert 'NAME="_LOOPS"' in out and 'NAME="_RECORDINGS"' in out
    assert 'NAME="Preparation"' in out
    assert re.search(r'NAME="\$ROOT"><SUBNODES COUNT="%d"' % ROOT_SUBNODES_COUNT, out)


def test_rename_playlist_node_preserves_entries_and_uuid():
    out, _ = traktor_write.rename_playlist_node(SAMPLE_NML, TEST_UUID, "Renamed")
    tlib = _lib_from(out)
    node = next(n for n in tlib.playlists() if n.name == "Renamed")
    assert node.playlist.traktor_uuid == TEST_UUID
    assert len(node.playlist.track_keys) == 1
    assert node.node_path == ("breaks", "Renamed")


def test_rename_playlist_node_unknown_uuid_is_a_no_op():
    out, renamed = traktor_write.rename_playlist_node(SAMPLE_NML, "deadbeef", "X")
    assert renamed is False
    assert out == SAMPLE_NML


def test_rename_playlist_node_escapes_the_name(tmp_path):
    out, _ = traktor_write.rename_playlist_node(SAMPLE_NML, LOOPS_UUID, 'A & B "quoted"')
    assert 'NAME="A &amp; B &quot;quoted&quot;"' in out
    tlib = _lib_from(out, tmp_path)
    assert any(n.name == 'A & B "quoted"' for n in tlib.playlists())


def test_rename_playlist_node_keeps_a_backslash_literal(tmp_path):
    """A function replacement, so \\1 in the name is not read as a group ref."""
    out, _ = traktor_write.rename_playlist_node(SAMPLE_NML, LOOPS_UUID, r"back\1slash")
    assert r'NAME="back\1slash"' in out


_LIB_N = [0]


def _lib_from(nml_text: str, tmp_path=None):
    """Round-trip NML text back through the reader."""
    import tempfile
    _LIB_N[0] += 1
    d = tempfile.mkdtemp()
    p = os.path.join(d, "c%d.nml" % _LIB_N[0])
    with open(p, "w", encoding="utf-8") as f:
        f.write(nml_text)
    return traktor_read.read(p)


# ================================= creation =================================
def test_create_traktor_playlist_at_root(window):
    window._create_node(TRAKTOR, None, "Fresh")
    out = _nml(window)

    assert re.search(r'NAME="Fresh"><PLAYLIST ENTRIES="0"', out)
    assert re.search(r'NAME="\$ROOT"><SUBNODES COUNT="%d"' % (ROOT_SUBNODES_COUNT + 1), out)
    assert _lib_from(out).find_playlist(("Fresh",)) is not None


def test_create_traktor_playlist_inside_a_folder(window):
    breaks = find_node(window.libraries.get(TRAKTOR).root, ("breaks",))
    window._create_node(TRAKTOR, breaks, "Fresh")
    out = _nml(window)

    assert re.search(r'NAME="breaks"[^>]*><SUBNODES COUNT="2">', out)
    assert re.search(r'NAME="\$ROOT"><SUBNODES COUNT="%d"' % ROOT_SUBNODES_COUNT, out)
    node = _lib_from(out).find_playlist(("breaks", "Fresh"))
    assert node is not None and node.playlist.track_keys == []


def test_create_vdj_playlist_at_root(window):
    window._create_node(VDJ, None, "Fresh")
    window._writer.flush()

    dest = os.path.join(_folders(window), "Fresh.vdjfolder")
    assert os.path.exists(dest)
    body = open(dest, encoding="utf-8").read()
    assert 'noDuplicates="yes"' in body        # VirtualDJ's own default: no ∞
    assert "<song " not in body


def test_create_vdj_playlist_inside_a_folder(window):
    window._create_node(VDJ, None, "Parent")
    parent = _node(window, VDJ, "Parent")
    # a VDJ playlist node can still take children -- it becomes a folder on disk
    parent.kind = "folder"
    window._create_node(VDJ, parent, "Child")
    window._writer.flush()

    assert os.path.exists(os.path.join(_folders(window),
                                       "Parent.subfolders", "Child.vdjfolder"))


def test_created_node_is_live_in_memory_and_in_the_tree(window):
    window._create_node(TRAKTOR, None, "Fresh")

    node = _node(window, TRAKTOR, "Fresh")
    assert node.node_path == ("Fresh",)
    assert node.playlist.traktor_uuid                      # a real UUID was minted
    assert node in window.libraries.get(TRAKTOR).root.children
    tree = window.playlist_panel.tree_for(TRAKTOR)
    assert id(node) in tree._by_nodeid, "bound to the row without a reload"
    assert tree._by_nodeid[id(node)].text(0) == "Fresh"


def test_duplicate_name_is_auto_suffixed(window):
    window._create_node(TRAKTOR, None, "Fresh")
    window._create_node(TRAKTOR, None, "Fresh")
    out = _nml(window)

    names = [n.name for n in window.libraries.get(TRAKTOR).playlists()]
    assert "Fresh" in names and "Fresh (2)" in names
    assert out.count('NAME="Fresh"') == 1
    assert out.count('NAME="Fresh (2)"') == 1


def test_collision_with_an_existing_folder_is_suffixed_too(window):
    """A VDJ playlist name also collides with a same-named .subfolders dir."""
    window._create_node(TRAKTOR, None, "breaks")
    assert any(n.name == "breaks (2)" for n in window.libraries.get(TRAKTOR).playlists())


def test_illegal_filename_characters_are_folded_for_vdj(window):
    window._create_node(VDJ, None, 'Hip/Hop: "90s"')
    window._writer.flush()

    node = next(n for n in window.libraries.get(VDJ).playlists())
    assert node.name == "Hip_Hop_ _90s_"
    assert os.path.exists(os.path.join(_folders(window), node.name + ".vdjfolder"))


def test_traktor_names_are_not_sanitised(window):
    window._create_node(TRAKTOR, None, "Hip/Hop")
    assert _node(window, TRAKTOR, "Hip/Hop").node_path == ("Hip/Hop",)


def test_cancelling_writes_nothing(window):
    tree = window.playlist_panel.tree_for(VDJ)
    before = tree.topLevelItemCount()

    tree.begin_new_node(None)
    assert tree.topLevelItemCount() == before + 1
    tree.discard_pending()

    assert tree.topLevelItemCount() == before
    window._writer.flush()
    assert not os.path.exists(os.path.join(_folders(window), "New playlist.vdjfolder"))


def test_empty_name_discards_the_placeholder(window):
    tree = window.playlist_panel.tree_for(VDJ)
    before = tree.topLevelItemCount()
    tree.begin_new_node(None)
    tree.commit_pending("   ")
    assert tree.topLevelItemCount() == before


def test_creating_does_not_reload_or_rediff(window, monkeypatch):
    """The whole point of mutating in place: no library re-read, no re-diff."""
    called = []
    monkeypatch.setattr(window, "_reload_preserving_view",
                        lambda *a, **k: called.append("reload"))
    monkeypatch.setattr(window, "_recompute_status", lambda: called.append("diff"))
    window._create_node(TRAKTOR, None, "Fresh")
    assert called == []


# ===================== the editor -> main window chain ======================
# The tests above call the handlers directly; these drive the whole path the
# editor really takes, including the deferred emit.
def test_inline_commit_creates_the_playlist(window, app):
    tree = window.playlist_panel.tree_for(VDJ)
    tree.begin_new_node(None)
    tree.commit_pending("From the editor")
    app.processEvents()                    # the QTimer.singleShot(0) emit
    window._writer.flush()

    assert os.path.exists(os.path.join(_folders(window), "From the editor.vdjfolder"))
    assert any(n.name == "From the editor" for n in window.libraries.get(VDJ).playlists())
    assert tree._pending_item is None, "the placeholder was bound, not left dangling"


def test_inline_commit_renames_the_playlist(window, app):
    node = _node(window, TRAKTOR, "test")
    tree = window.playlist_panel.tree_for(TRAKTOR)
    tree.begin_rename(node)
    tree.commit_pending("Renamed")
    app.processEvents()

    assert node.name == "Renamed"
    assert 'NAME="Renamed"' in _nml(window)


def test_escape_in_the_editor_discards(window, app):
    from PySide6.QtWidgets import QAbstractItemDelegate
    tree = window.playlist_panel.tree_for(VDJ)
    before = tree.topLevelItemCount()

    tree.begin_new_node(None)
    tree._on_editor_closed(None, QAbstractItemDelegate.RevertModelCache)
    app.processEvents()
    window._writer.flush()

    assert tree.topLevelItemCount() == before
    assert list(window.libraries.get(VDJ).playlists()) == []
    assert not os.path.exists(os.path.join(_folders(window), "New playlist.vdjfolder"))


def test_editor_commit_hint_uses_the_item_text(window, app):
    """Committing without changing the default text emits no itemChanged, which
    is why closeEditor (not itemChanged) drives this."""
    from PySide6.QtWidgets import QAbstractItemDelegate
    tree = window.playlist_panel.tree_for(VDJ)
    tree.begin_new_node(None)
    tree._on_editor_closed(None, QAbstractItemDelegate.NoHint)
    app.processEvents()
    window._writer.flush()

    assert any(n.name == "New playlist" for n in window.libraries.get(VDJ).playlists())


# ================================== rename ==================================
def test_rename_traktor_playlist(window):
    node = _node(window, TRAKTOR, "test")
    window._rename_node(TRAKTOR, node, "Renamed")
    out = _nml(window)

    assert re.search(r'NAME="Renamed"><PLAYLIST ENTRIES="1"[^>]*UUID="%s"' % TEST_UUID, out)
    assert 'NAME="test"' not in out
    assert _lib_from(out).find_playlist(("breaks", "Renamed")) is not None


def test_rename_updates_the_model_and_the_row(window):
    node = _node(window, TRAKTOR, "test")
    keys_before = list(node.playlist.track_keys)

    window._rename_node(TRAKTOR, node, "Renamed")

    assert node.name == "Renamed"
    assert node.playlist.name == "Renamed"
    assert node.node_path == ("breaks", "Renamed")
    assert node.playlist.track_keys == keys_before, "tracks carry across untouched"
    tree = window.playlist_panel.tree_for(TRAKTOR)
    assert tree._by_nodeid[id(node)].text(0).endswith("Renamed")


def test_rename_vdj_playlist_moves_the_file(window):
    window._create_node(VDJ, None, "Original")
    window._writer.flush()
    node = _node(window, VDJ, "Original")
    old_file = node.playlist.vdj_file

    window._rename_node(VDJ, node, "Renamed")
    window._writer.flush()

    new_file = os.path.join(_folders(window), "Renamed.vdjfolder")
    assert os.path.exists(new_file)
    assert not os.path.exists(old_file)
    assert node.playlist.vdj_file == new_file


def test_rename_vdj_playlist_keeps_its_songs(window):
    window._create_node(VDJ, None, "Original")
    node = _node(window, VDJ, "Original")
    keys = list(window.libraries.get(VDJ).tracks)[:1] or []
    if keys:
        window._edit_membership(node, keys)
    window._writer.flush()
    before = open(node.playlist.vdj_file, encoding="utf-8").read()

    window._rename_node(VDJ, node, "Renamed")
    window._writer.flush()

    assert open(node.playlist.vdj_file, encoding="utf-8").read() == before


def test_rename_to_the_same_name_writes_nothing(window):
    node = _node(window, TRAKTOR, "test")
    before = _nml(window)
    window._rename_node(TRAKTOR, node, "test")
    assert _nml(window) == before
    assert node.node_path == ("breaks", "test")


def test_rename_onto_a_sibling_is_auto_suffixed(window):
    breaks = find_node(window.libraries.get(TRAKTOR).root, ("breaks",))
    window._create_node(TRAKTOR, breaks, "Sibling")
    node = _node(window, TRAKTOR, "test")

    window._rename_node(TRAKTOR, node, "Sibling")

    assert node.name == "Sibling (2)"
    assert node.node_path == ("breaks", "Sibling (2)")


def test_rename_migrates_the_edited_overlay(window):
    """The overlay is keyed by path, so a rename has to carry it across -- and
    carry the editor's identity with it, since that is what picks the glyph."""
    node = _node(window, TRAKTOR, "test")
    window._edited_paths[("breaks", "test")] = TRAKTOR

    window._rename_node(TRAKTOR, node, "Renamed")

    assert ("breaks", "test") not in window._edited_paths
    assert window._edited_paths[("breaks", "renamed")] == TRAKTOR


def test_rename_relabels_an_open_tab(window):
    node = _node(window, TRAKTOR, "test")
    window._on_playlists_selected([node], TRAKTOR)
    table = window._table()

    window._rename_node(TRAKTOR, node, "Renamed")

    group = window.track_tabs._group_of(table)
    assert group.tabText(group.indexOf(table)) == "Renamed"


def test_rename_of_a_ghost_without_a_file_writes_one(window):
    """An order-file ghost has no .vdjfolder to move; one gets written."""
    window._create_node(VDJ, None, "Ghost")
    node = _node(window, VDJ, "Ghost")
    window._writer.flush()
    os.remove(node.playlist.vdj_file)

    window._rename_node(VDJ, node, "Real")
    window._writer.flush()

    assert os.path.exists(os.path.join(_folders(window), "Real.vdjfolder"))


def test_begin_rename_works_on_folders_and_leaves(window):
    """Folders became renameable this phase; only a missing node is refused."""
    tree = window.playlist_panel.tree_for(TRAKTOR)

    window._begin_rename(TRAKTOR, None)
    assert tree._pending_item is None

    for node in (find_node(window.libraries.get(TRAKTOR).root, ("breaks",)),
                 _node(window, TRAKTOR, "test")):
        window._begin_rename(TRAKTOR, node)
        assert tree._pending_item is not None
        tree.discard_pending()


def test_cancelling_a_rename_restores_the_row(window):
    node = _node(window, TRAKTOR, "test")
    tree = window.playlist_panel.tree_for(TRAKTOR)
    item = tree._by_nodeid[id(node)]
    before = item.text(0)

    tree.begin_rename(node)
    item.setText(0, "half typed")
    tree.discard_pending()

    assert item.text(0) == before
    assert node.name == "test"


# ============================== menu plumbing ===============================
class _CB:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# playlist_menu's full layout and per-kind gating live in
# test_folders_and_moves.py, which owns the entries that shaped it.
def test_blank_menu_offers_only_creation(app):
    menu = context_menus.blank_menu(
        None, VDJ, _CB(new_playlist=lambda: None, new_folder=lambda: None))
    assert [a.text() for a in menu.actions()] == ["New playlist", "New folder"]


# Standalone trees, deliberately NOT the window's: those are wired to handlers
# that open a QMenu with exec(), which never returns under offscreen Qt.
def _tree(source, tmp_path):
    from trackcleaner.ui.playlist_trees import LibraryTree
    paths = write_sample(tmp_path)
    tree = LibraryTree(source)
    tree.load(traktor_read.read(paths.nml).root)
    return tree


def test_blank_space_context_carries_its_own_source(app, tmp_path):
    """The main window can't infer the source: _current_source follows the
    focused tab, which may well be the other program."""
    for source in (VDJ, TRAKTOR):
        tree = _tree(source, tmp_path)
        tree.itemAt = lambda _pos: None           # cursor over empty space
        seen = []
        tree.blank_context_requested.connect(lambda s, _p, out=seen: out.append(s))
        tree.node_context_requested.connect(lambda *_: seen.append("node"))

        tree._on_context(QPoint(5, 400))

        assert seen == [source]


def test_node_context_still_fires_for_a_row(app, tmp_path):
    from trackcleaner.ui.playlist_trees import _NODE_ROLE
    tree = _tree(TRAKTOR, tmp_path)
    item = next(iter(tree._by_nodeid.values()))
    node = item.data(0, _NODE_ROLE)
    tree.itemAt = lambda _pos: item
    seen = []
    tree.node_context_requested.connect(lambda n, _p: seen.append(n))
    tree.blank_context_requested.connect(lambda *_: seen.append("blank"))

    tree._on_context(QPoint(5, 5))

    assert seen == [node]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

