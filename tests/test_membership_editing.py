"""Phase 2: remove-from-playlist, remove-duplicates, drag-to-add (same- and
cross-program), and the single-playlist-view gate.

These exercise the real bound methods on a real ``MainWindow`` over the tiny
nml/xml fixture, since the editing logic lives there (it needs the loaded
libraries, the sync engine and the busy overlay). Dragging itself is not
simulated here -- constructing a ``QDropEvent`` by hand segfaults this PySide6
build outright (verified), so the MIME-parsing seam
(``TrackTable._parse_drop``) and the drop *wiring* are covered separately in
``test_track_table.py`` / ``test_track_tabs.py`` / ``test_playlist_sections.py``;
this file covers what happens once main_window receives the dropped keys.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox            # noqa: E402

from trackcleaner.config.paths import Settings                     # noqa: E402
from trackcleaner.io import backup as backup_mod                   # noqa: E402
from trackcleaner.io import traktor_write                          # noqa: E402
from fixtures import write_sample                                  # noqa: E402
from trackcleaner.model.library import TRAKTOR, VDJ                  # noqa: E402
from trackcleaner.model import status as st                           # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    """A real MainWindow over the tiny nml/xml fixture. Backup snapshots AND the
    settings file are redirected into tmp_path so the test never touches the
    project's real backups/ dir or ``trackcleaner_settings.json`` -- MainWindow
    snapshots on open and ``closeEvent`` calls ``settings.save()``, which would
    otherwise overwrite the developer's real library paths on every run."""
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(tmp_path / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(tmp_path / "settings.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(tmp_path / "baseline.json"))
    from trackcleaner.ui.main_window import MainWindow   # deferred: needs QT_QPA_PLATFORM set
    paths = write_sample(tmp_path)
    settings = Settings()
    settings.paths = paths
    w = MainWindow(settings)
    w.show()
    yield w
    w.close()


def _test_node(window):
    return next(n for n in window.libraries.get(TRAKTOR).playlists() if n.name == "test")


def _entry_keys(window, uuid):
    # Membership writes are now atomic + backgrounded; wait for the queue to
    # drain before reading the file (the model already updated synchronously).
    window._writer.flush()
    nml = open(window.settings.paths.nml, encoding="utf-8").read()
    return traktor_write.playlist_entry_keys(nml, uuid)


# ------------------------------ remove from playlist -----------------------
def test_remove_from_playlist_shrinks_membership(window):
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    uuid = node.playlist.traktor_uuid
    node.playlist.track_keys = list(keys)          # both fixture tracks

    window._remove_from_playlist(node, [keys[0]])

    assert _entry_keys(window, uuid) == [keys[1]]


def test_remove_from_playlist_multi_selection(window):
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    uuid = node.playlist.traktor_uuid
    node.playlist.track_keys = list(keys)

    window._remove_from_playlist(node, keys)        # remove both at once

    assert _entry_keys(window, uuid) == []


def test_remove_from_playlist_is_a_no_op_for_keys_not_present(window):
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    uuid = node.playlist.traktor_uuid
    node.playlist.track_keys = [keys[0]]
    before = open(window.settings.paths.nml, encoding="utf-8").read()

    window._remove_from_playlist(node, ["not-a-real-key"])
    window._writer.flush()

    after = open(window.settings.paths.nml, encoding="utf-8").read()
    assert before == after, "nothing to remove must not touch the file"


# -------------------------------- remove duplicates -------------------------
def test_remove_duplicates_keeps_the_first_occurrence(window):
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    uuid = node.playlist.traktor_uuid
    node.playlist.track_keys = [keys[0], keys[1], keys[0]]   # dup at the end

    window._remove_duplicates(node)

    assert _entry_keys(window, uuid) == [keys[0], keys[1]]


def test_remove_duplicates_reports_when_none_found(window):
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    node.playlist.track_keys = [keys[0], keys[1]]    # already unique
    before = open(window.settings.paths.nml, encoding="utf-8").read()

    window._remove_duplicates(node)
    window._writer.flush()

    after = open(window.settings.paths.nml, encoding="utf-8").read()
    assert before == after
    assert "no duplicate" in window.statusBar().currentMessage().lower()


# ------------------------------------ add ------------------------------------
def test_add_tracks_appends_at_the_end_without_deduping(window):
    """New tracks land last (highest order); an add never dedupes -- that is
    what Remove duplicates is for."""
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    uuid = node.playlist.traktor_uuid
    node.playlist.track_keys = [keys[0]]

    window._add_tracks(node, [keys[0], keys[1]], "traktor")   # same program

    assert _entry_keys(window, uuid) == [keys[0], keys[0], keys[1]]


def test_same_program_add_does_not_prompt(window, monkeypatch):
    asked = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: asked.append(1) or QMessageBox.Yes))
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    node.playlist.track_keys = []

    window._add_tracks(node, [keys[0]], "traktor")

    assert asked == []


def test_cross_program_add_prompts_exactly_once_for_the_whole_drop(window, monkeypatch):
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: asked.append(1) or QMessageBox.Yes))
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    uuid = node.playlist.traktor_uuid
    node.playlist.track_keys = []

    window._add_tracks(node, keys, "vdj")     # both keys at once, cross-program

    assert len(asked) == 1, "one prompt for the whole batch, not per track"
    assert _entry_keys(window, uuid) == keys


def test_cross_program_add_creates_the_missing_target_entry(window, monkeypatch):
    from trackcleaner.model.library import Track, VDJ
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    node = _test_node(window)
    new_key = r"d:\music\funk\brand new.mp3"
    window.libraries.get(VDJ).tracks[new_key] = Track(
        key=new_key, path=r"D:\Music\Funk\Brand New.mp3", source=VDJ,
        title="Brand New", artist="Nobody", pois=[])
    node.playlist.track_keys = []
    uuid = node.playlist.traktor_uuid

    window._add_tracks(node, [new_key], "vdj")

    assert _entry_keys(window, uuid) == [new_key]
    nml = open(window.settings.paths.nml, encoding="utf-8").read()
    assert 'TITLE="Brand New"' in nml, "the missing collection entry must be created"


def test_declining_the_cross_program_prompt_adds_nothing(window, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    # Deliberately do NOT touch node.playlist.track_keys here: declining must
    # mean _edit_membership is never even reached, so the file must stay
    # byte-identical to whatever it already was -- not "empty".
    before = open(window.settings.paths.nml, encoding="utf-8").read()

    window._add_tracks(node, keys, "vdj")
    window._writer.flush()

    after = open(window.settings.paths.nml, encoding="utf-8").read()
    assert after == before, "declining must not write anything at all"


# --------------------------- single-playlist-view gate -----------------------
class _FakeTable:
    """A stand-in with just the attribute _on_tracks_dropped_on_table reads --
    no real TrackTable/drag machinery needed to test the gate."""
    def __init__(self, nodes):
        self.nodes = nodes


def test_gate_blocks_a_table_showing_no_playlist(window):
    window._on_tracks_dropped_on_table(_FakeTable([]), ["k1"], "traktor")
    assert "single playlist" in window.statusBar().currentMessage().lower()


def test_gate_blocks_a_table_showing_multiple_playlists(window):
    node_a, node_b = list(window.libraries.get(TRAKTOR).playlists())[:2]
    window._on_tracks_dropped_on_table(_FakeTable([node_a, node_b]), ["k1"], "traktor")
    assert "single playlist" in window.statusBar().currentMessage().lower()


def test_gate_allows_a_table_showing_exactly_one_playlist(window):
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    uuid = node.playlist.traktor_uuid
    node.playlist.track_keys = []

    window._on_tracks_dropped_on_table(_FakeTable([node]), [keys[0]], "traktor")

    assert _entry_keys(window, uuid) == [keys[0]]


def test_sidebar_drop_is_exempt_from_the_single_node_gate(window):
    """Dropping on a sidebar node is always unambiguous -- it always comes from
    one specific node, unlike a table's possibly-mixed display."""
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    uuid = node.playlist.traktor_uuid
    node.playlist.track_keys = []

    window._on_tracks_dropped_on_sidebar(node, [keys[0]], "traktor")

    assert _entry_keys(window, uuid) == [keys[0]]


# ---------------- live refresh: no full reload, no tab reassignment ----------
def _other_node(window):
    node = _test_node(window)
    return next(n for n in window.libraries.get(TRAKTOR).playlists()
                if n.playlist and tuple(n.node_path) != tuple(node.node_path))


def test_edit_refreshes_every_view_of_that_playlist_but_no_others(window):
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    node.playlist.track_keys = list(keys)
    tabs = window.track_tabs

    # tab A shows 'test'
    tabA = tabs.focused_table()
    window._on_playlists_selected([node], "traktor")
    # tab B shows a different playlist
    tabs.new_tab()
    tabB = tabs.focused_table()
    other = _other_node(window)
    other.playlist.track_keys = list(keys)
    window._on_playlists_selected([other], "traktor")
    # tab C is a duplicate of 'test'
    tabs.new_tab()
    tabC = tabs.focused_table()
    window._on_playlists_selected([node], "traktor")

    window._remove_from_playlist(node, [keys[0]])

    # both 'test' tabs refreshed to the shrunk membership; removed key is *gone*,
    # not a grey tombstone
    assert tabA.visual_keys() == [keys[1]]
    assert tabC.visual_keys() == [keys[1]]
    assert keys[0] not in tabA.visual_keys()
    # the other-playlist tab kept its own context and rows
    assert tuple(tabB.nodes[0].node_path) == tuple(other.node_path)
    assert set(tabB.visual_keys()) == set(keys)


def test_edit_clears_the_stale_divergence_for_that_path(window):
    from trackcleaner.model.library import path_ci
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    node.playlist.track_keys = list(keys)
    window._divergence = {path_ci(node.node_path):
                          {"traktor": {"losing": [keys[0]], "gaining": []}}}

    window._remove_from_playlist(node, [keys[0]])

    assert path_ci(node.node_path) not in window._divergence


def test_add_tracks_at_index_splices_in_place(window):
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    node.playlist.track_keys = [keys[0], keys[1]]

    window._add_tracks(node, [keys[0]], "traktor", at_index=1)   # same program

    assert node.playlist.track_keys == [keys[0], keys[0], keys[1]]


def test_add_tracks_default_appends(window):
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    node.playlist.track_keys = [keys[0]]

    window._add_tracks(node, [keys[1]], "traktor")               # at_index defaults -1

    assert node.playlist.track_keys == [keys[0], keys[1]]


# ------------------------- edited-yellow overlay ----------------------------
def test_edit_flags_the_playlist_as_edited_and_shows_yellow(window):
    from trackcleaner.model.library import path_ci
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    node.playlist.track_keys = list(keys)

    window._remove_from_playlist(node, [keys[0]])

    # Recorded by path, with WHO edited it -- every program's copy is now stale,
    # and each one's glyph depends on whether the editor was the reference.
    assert window._edited_paths[path_ci(node.node_path)] == node.source
    # The overlay speaks for that node regardless of its real diff status: the
    # editor sees the "I changed this" triangle.
    overlaid = window._edited_overlay(node.source, lambda n: st.GREEN)
    assert overlaid(node) == st.YELLOW_SELF


def test_overlay_leaves_unedited_playlists_alone(window):
    node = _test_node(window)
    other = _other_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    node.playlist.track_keys = list(keys)
    window._remove_from_playlist(node, [keys[0]])         # edit 'node', not 'other'
    overlaid = window._edited_overlay(other.source, lambda n: "green")
    assert overlaid(other) == "green"


def test_recompute_status_clears_the_edited_overlay(window):
    from trackcleaner.model.library import path_ci
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    node.playlist.track_keys = list(keys)
    window._remove_from_playlist(node, [keys[0]])
    assert path_ci(node.node_path) in window._edited_paths

    window._recompute_status()               # a full diff reconciles disk state

    assert window._edited_paths == {}


# ------------------------- duplicates + reorder -----------------------------
def test_add_keeps_duplicates_when_the_playlist_allows_them(window):
    node = _test_node(window)                 # Traktor -> no_duplicates False
    assert node.playlist.no_duplicates is False
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    node.playlist.track_keys = [keys[0]]
    window._add_tracks(node, [keys[0]], "traktor")
    assert node.playlist.track_keys == [keys[0], keys[0]]


def test_add_drops_duplicates_when_the_playlist_forbids_them(window):
    node = _test_node(window)
    node.playlist.no_duplicates = True
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    node.playlist.track_keys = [keys[0]]
    window._add_tracks(node, [keys[0], keys[1]], "traktor")   # keys[0] is a dup
    assert node.playlist.track_keys == [keys[0], keys[1]]


def test_reorder_playlist_applies_the_new_order(window):
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    node.playlist.track_keys = [keys[0], keys[1]]
    window._on_playlists_selected([node], "traktor")
    table = window.track_tabs.focused_table()
    window._reorder_playlist(table, [keys[1], keys[0]])
    assert node.playlist.track_keys == [keys[1], keys[0]]


def test_remove_positions_removes_one_copy_of_a_duplicate(window):
    node = _test_node(window)
    keys = list(window.libraries.get(TRAKTOR).tracks.keys())
    node.playlist.track_keys = [keys[0], keys[1], keys[0]]
    window._remove_positions(node, [2])       # drop only the 2nd copy of keys[0]
    assert node.playlist.track_keys == [keys[0], keys[1]]


def test_toggling_duplicates_off_dedupes_and_sets_the_flag(window, tmp_path):
    from trackcleaner.model.library import Playlist, Track, TreeNode, VDJ
    k = r"d:\m\x.mp3"
    window.libraries.get(VDJ).tracks[k] = Track(key=k, path=r"D:\m\x.mp3", source=VDJ, title="X")
    pl = Playlist(name="P", source=VDJ, track_keys=[k, k], no_duplicates=False,
                  vdj_file=str(tmp_path / "P.vdjfolder"))
    node = TreeNode("P", "playlist", VDJ, playlist=pl, node_path=("P",))

    window._set_allow_duplicates(node, allow=False)

    assert node.playlist.no_duplicates is True
    assert node.playlist.track_keys == [k]
    window._writer.flush()
    assert 'noDuplicates="yes"' in open(pl.vdj_file, encoding="utf-8").read()


# ------------------- scroll preservation + reload (round 4) -----------------
def _fill_many(window, node, n=200):
    from trackcleaner.model.library import Track, TRAKTOR
    keys = []
    for i in range(n):
        k = f"d:/m/{i}.mp3"
        window.libraries.get(TRAKTOR).tracks[k] = Track(key=k, path=k, source=TRAKTOR, title=f"T{i}")
        keys.append(k)
    node.playlist.track_keys = list(keys)
    return keys


def test_reorder_preserves_scroll_position(window):
    """Reordering a track must keep the list where it was, not snap to the top."""
    node = _test_node(window)
    keys = _fill_many(window, node)
    window._on_playlists_selected([node], "traktor")
    table = window.track_tabs.focused_table()
    QApplication.processEvents()
    vbar = table.verticalScrollBar()
    vbar.setValue(vbar.maximum())               # scroll to the bottom
    target = vbar.value()
    assert target > 0, "fixture must actually be scrollable"

    new = list(keys)
    new[0], new[1] = new[1], new[0]             # a small reorder near the top
    window._reorder_playlist(table, new)
    QApplication.processEvents()                # let the deferred restore fire

    assert vbar.value() == target, "scroll jumped instead of staying put"


def test_focused_tab_not_blank_after_reload(window):
    """A reload must leave the focused tab's rows populated (the bug: the tab
    looked empty until the playlist was re-selected).

    Reproduce it the *real* way: select the playlist through the sidebar tree so
    the tree actually holds a selection. Rebuilding the tree on reload calls
    QTreeWidget.clear(), which -- if that spurious itemSelectionChanged is not
    blocked -- fires playlists_selected([]) and wipes the focused tab's context."""
    node = _test_node(window)
    window.playlist_panel.trees[TRAKTOR].select_node(node)   # real sidebar selection
    table = window.track_tabs.focused_table()
    assert table.visual_keys(), "sanity: rows present before reload"

    window._reload_preserving_view()

    assert not window._reloading
    assert table.nodes, "focused tab lost its playlist context on reload"
    assert table.visual_keys(), "focused tab went blank after reload"


def test_tree_load_does_not_emit_selection_when_it_had_one(window):
    """Rebuilding a tree must not masquerade as the user deselecting a playlist."""
    tree = window.playlist_panel.trees[TRAKTOR]
    tree.select_node(_test_node(window))
    fired = []
    tree.playlists_selected.connect(lambda nodes, src: fired.append(nodes))

    tree.load(window.libraries.get(TRAKTOR).root, set())

    assert fired == [], "load() emitted a spurious selection change"


# ---------------- Phase 3: collection removal / file delete / import --------
def _yes(monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.Yes))


def _traktor_key(window, title):
    return next(k for k, t in window.libraries.get(TRAKTOR).tracks.items() if t.title == title)


def test_remove_with_entry_deletes_entry_and_all_playlist_refs(window, monkeypatch):
    _yes(monkeypatch)
    window._current_source = "traktor"
    key = _traktor_key(window, "Homenaje")
    assert window.libraries.get(TRAKTOR).playlists_containing(key), "fixture references it in a playlist"

    window._remove_with_entry(key)

    assert key not in window.libraries.get(TRAKTOR).tracks, "collection entry gone after reload"
    nml = open(window.settings.paths.nml, encoding="utf-8").read()
    assert 'TITLE="Homenaje"' not in nml
    assert window.libraries.get(TRAKTOR).playlists_containing(key) == [], "no dangling playlist refs"


def test_delete_file_strips_both_programs_and_recycles(window, monkeypatch):
    from trackcleaner.io import recycle as recycle_mod
    _yes(monkeypatch)
    recycled = []
    monkeypatch.setattr(recycle_mod, "send_to_recycle_bin",
                        lambda p: recycled.append(p) or True)
    window._current_source = "traktor"
    key = _traktor_key(window, "Homenaje")
    path = window.libraries.get(TRAKTOR).tracks[key].path

    window._delete_file(key)

    assert recycled == [path], "the file path was sent to the Recycle Bin"
    assert key not in window.libraries.get(TRAKTOR).tracks
    assert 'TITLE="Homenaje"' not in open(window.settings.paths.nml, encoding="utf-8").read()


def test_import_existing_path_just_adds_to_playlist(window, monkeypatch):
    _yes(monkeypatch)
    node = _test_node(window)
    key = _traktor_key(window, "I Believe In Miracles")
    path = window.libraries.get(TRAKTOR).tracks[key].path
    node.playlist.track_keys = []

    window._import_files(node, [path])

    assert key in node.playlist.track_keys


def test_import_new_file_creates_entry_and_adds(window, monkeypatch, tmp_path):
    from trackcleaner.io import probe as probe_mod
    from trackcleaner.model import matching
    from trackcleaner.model.library import Track, TRAKTOR
    _yes(monkeypatch)
    newp = str(tmp_path / "Imported Song.mp3")
    open(newp, "wb").close()
    fake = Track(key=matching.path_key(newp), path=newp, source=TRAKTOR,
                 title="Imported Song", pois=[])
    monkeypatch.setattr(probe_mod, "probe_track", lambda p, s: fake)
    node = _test_node(window)
    node.playlist.track_keys = []

    window._import_files(node, [newp])

    assert 'TITLE="Imported Song"' in open(window.settings.paths.nml, encoding="utf-8").read()
    again = next(n for n in window.libraries.get(TRAKTOR).playlists()
                 if tuple(n.node_path) == tuple(node.node_path))
    assert fake.key in again.playlist.track_keys


# ------------------- collection search (whole-library, grouped) -------------
def test_collection_search_groups_playlist_matches_then_orphan(window):
    from trackcleaner.model.library import Track, TRAKTOR
    node = _test_node(window)
    window._on_playlists_selected([node], "traktor")
    table = window.track_tabs.focused_table()
    homenaje = _traktor_key(window, "Homenaje")
    assert homenaje in node.playlist.track_keys
    orphan = "d:/music/homenaje remix.mp3"
    window.libraries.get(TRAKTOR).tracks[orphan] = Track(
        key=orphan, path=r"D:\Music\Homenaje Remix.mp3", source=TRAKTOR, title="Homenaje Remix")
    assert window.libraries.get(TRAKTOR).playlists_containing(orphan) == [], "must be a true orphan"

    window.search_edit.setText("homenaje")
    window._run_search()

    vk = table.visual_keys()
    assert vk[0] == homenaje, "the in-playlist match comes first"
    assert orphan in vk and vk.index(orphan) > vk.index(homenaje), "orphan after"
    assert table.model().rowCount() == len(vk) + 1, "exactly one separator row between groups"


def test_collection_search_finds_tagless_orphan_by_filename(window):
    from trackcleaner.model.library import Track, TRAKTOR
    node = _test_node(window)
    window._on_playlists_selected([node], "traktor")
    table = window.track_tabs.focused_table()
    orphan = "d:/music/zzq_unique_name.mp3"
    window.libraries.get(TRAKTOR).tracks[orphan] = Track(
        key=orphan, path=r"D:\Music\zzq_unique_name.mp3", source=TRAKTOR)  # no tags at all

    window.search_edit.setText("zzq_unique")
    window._run_search()

    assert orphan in table.visual_keys(), "matched by filename despite no tags"


def test_clearing_search_restores_the_playlist_view(window):
    node = _test_node(window)
    miracles = _traktor_key(window, "I Believe In Miracles")
    node.playlist.track_keys = [miracles]
    window._on_playlists_selected([node], "traktor")
    table = window.track_tabs.focused_table()
    before = table.visual_keys()
    assert before == [miracles]

    window.search_edit.setText("brownout")     # matches Homenaje's artist, not in this playlist
    window._run_search()
    assert table.visual_keys() != before, "search widened beyond the playlist"

    window.search_edit.clear()
    window._run_search()
    assert table.visual_keys() == before, "playlist view restored on clear"


def test_reload_runs_on_done_and_clears_the_guard(window):
    fired = []
    window._reload_preserving_view(on_done=lambda: fired.append(1))
    assert fired == [1]
    assert window._reloading is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
