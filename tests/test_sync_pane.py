"""The View → Playlists needing sync pane.

A worklist of every playlist whose square is not green, per program. Its whole
value rests on agreeing with the sidebar, so the property pinned hardest is
that it lists exactly the sidebar's not-green playlists, with the same glyph --
computed from the very status functions the sidebar was painted with, never a
second diff.

Also pinned: it is off until asked for and costs nothing while off; BLACK ("not
in the reference yet") is opt-in, and an empty list never claims "in sync"
about a library where nothing has synced; hidden playlists stay out; a click
loads the playlist through the sidebar's own selection path; and the choice
survives a restart, including from a session saved before the pane existed.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                                         # noqa: E402
from PySide6.QtTest import QTest                                      # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox               # noqa: E402

from trackcleaner.config.paths import Settings                        # noqa: E402
from trackcleaner.io import backup as backup_mod                      # noqa: E402
from trackcleaner.model import status as st                           # noqa: E402
from trackcleaner.model.library import TRAKTOR, VDJ                   # noqa: E402
from trackcleaner.sync.engine import SyncResult                       # noqa: E402
from trackcleaner.ui.sync_pane import needs_sync                      # noqa: E402
from fixtures import write_sample                                     # noqa: E402

PAIR = {"enabled_programs": [VDJ, TRAKTOR]}


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _make_window(tmp_path, monkeypatch, ui_state=None):
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(tmp_path / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(tmp_path / "settings.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(tmp_path / "baseline.csv"))
    monkeypatch.setattr(paths_mod, "app_dir", lambda: str(tmp_path))
    from trackcleaner.ui.main_window import MainWindow
    settings = Settings()
    settings.paths = write_sample(tmp_path)
    settings.ui_state = dict(PAIR, **(ui_state or {}))
    w = MainWindow(settings)
    w.show()
    return w


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    yield w
    w.close()


def _sidebar(window, include_missing=False):
    """What the sidebar says needs sync: ``{(source, node_path): status}``,
    from the same painter (and overlay) the sidebar squares use."""
    out = {}
    for tree, fn in window._tree_status_fns:
        paint = window._edited_overlay(tree.source, fn)
        for node in tree.visible_playlist_nodes():
            state = paint(node)
            if needs_sync(state, include_missing):
                out[(node.source, tuple(node.node_path))] = state
    return out


def _listed(window):
    return {(n.source, tuple(n.node_path)): s for n, s in window.sync_pane.rows()}


def _note(window):
    tree = window.sync_pane.tree
    return tree.topLevelItem(0).text(0) if tree.topLevelItemCount() else ""


def _synced_pair(window):
    """Carry Traktor's ``breaks/test`` into VirtualDJ and reload, so both
    programs hold it -- the starting point for a playlist that can diverge."""
    node = next(n for n in window.libraries[TRAKTOR].playlists()
                if n.name == "test")
    window.sync_engine.sync_playlist(node, TRAKTOR, "new", target=VDJ)
    window.load_libraries()
    return next(n for n in window.libraries[VDJ].playlists() if n.name == "test")


# ================================ off by default ============================
def test_the_pane_is_off_until_the_view_menu_turns_it_on(window):
    assert not window.sync_pane.isVisible()
    assert not window._sync_pane_action.isChecked()

    window._sync_pane_action.trigger()

    assert window.sync_pane.isVisible()
    assert window._sync_pane_action.isChecked()


def test_while_off_it_is_never_refreshed(window, monkeypatch):
    """Off has to mean free: every repaint of the sidebar would otherwise pay
    for a second pass over every playlist of every program."""
    calls = []
    monkeypatch.setattr(window.sync_pane, "populate",
                        lambda *a, **k: calls.append(a))

    window._recompute_status()

    assert calls == []


def test_turning_it_on_gives_it_a_width(window):
    """A splitter keeps a hidden pane at zero and would show it that way."""
    window._set_sync_pane_visible(True)

    assert window.h_splitter.sizes()[1] > 0
    assert window.h_splitter.sizes()[0] > 0, "the width came from the tracks"


# ============================ agrees with the sidebar =======================
@pytest.mark.parametrize("include_missing", [False, True])
def test_it_lists_exactly_the_sidebars_not_green_playlists(window,
                                                           include_missing):
    """The defining property, before and after a sync and an edit: same
    playlists, same glyph, nothing extra."""
    window._set_sync_pane_visible(True)
    window.sync_pane.include_missing.setChecked(include_missing)
    assert _listed(window) == _sidebar(window, include_missing)

    vdj_test = _synced_pair(window)
    assert _listed(window) == _sidebar(window, include_missing)

    window.membership.edit(vdj_test, [])
    assert _listed(window) == _sidebar(window, include_missing)


def test_an_edited_playlist_is_listed_in_every_program(window):
    """An edit makes every program's copy stale, so the pane lists both -- the
    editor with its triangle, the other copy with its own glyph."""
    window._set_sync_pane_visible(True)
    vdj_test = _synced_pair(window)

    window.membership.edit(vdj_test, [])

    listed = _listed(window)
    assert listed[(VDJ, tuple(vdj_test.node_path))] == st.YELLOW_SELF
    trk = next(k for k in listed if k[0] == TRAKTOR and k[1][-1] == "test")
    assert st.is_changed(listed[trk])


def test_a_green_playlist_is_never_listed(window):
    window._set_sync_pane_visible(True)
    window.sync_pane.include_missing.setChecked(True)
    _synced_pair(window)

    greens = {(tree.source, tuple(n.node_path))
              for tree, fn in window._tree_status_fns
              for n in tree.visible_playlist_nodes() if fn(n) == st.GREEN}

    assert greens, "the fixture should produce at least one synced playlist"
    assert not greens & set(_listed(window))


# ================================ BLACK is opt-in ===========================
def test_missing_from_the_reference_is_opt_in(window):
    """On first load VirtualDJ is empty, so every Traktor playlist is BLACK."""
    window._set_sync_pane_visible(True)
    assert _listed(window) == {}

    window.sync_pane.include_missing.setChecked(True)

    assert _listed(window)
    assert set(_listed(window).values()) == {st.BLACK}


def test_an_empty_list_does_not_claim_everything_is_in_sync(window):
    """Nothing has synced yet; "Every playlist is in sync" would be false."""
    window._set_sync_pane_visible(True)

    assert "in sync" not in _note(window)
    assert "not in the reference" in window.sync_pane.count_label.text()


def test_with_one_program_on_there_is_nothing_to_compare(window):
    window._set_sync_pane_visible(True)
    window._set_program_enabled(VDJ, False)

    assert window.sync_pane.rows() == []
    assert "two programs" in _note(window)


# =================================== hidden =================================
def test_a_hidden_playlist_stays_out(window):
    window._set_sync_pane_visible(True)
    window.sync_pane.include_missing.setChecked(True)
    node = next(n for n, _s in window.sync_pane.rows())

    window._hide_node(node, node.source)

    assert (node.source, tuple(node.node_path)) not in _listed(window)


# ============================== acting on rows ==============================
def _item(window, node):
    return next(item for item in window.sync_pane._items()
                if item.data(0, Qt.UserRole) is node)


def _click(window, node, modifier=Qt.NoModifier, button=Qt.LeftButton):
    """A real mouse click on the row, through the viewport, as a user makes it."""
    tree = window.sync_pane.tree
    pos = tree.visualItemRect(_item(window, node)).center()
    QTest.mouseClick(tree.viewport(), button, modifier, pos)


def _paths(nodes):
    return {tuple(n.node_path) for n in nodes}


def test_clicking_a_row_selects_it_in_the_sidebar_and_loads_it(window):
    """The real user path: the sidebar selection is what loads a tab, so the
    sidebar, the tab and every selection-scoped menu agree."""
    window._set_sync_pane_visible(True)
    window.sync_pane.include_missing.setChecked(True)
    node = window.sync_pane.nodes()[-1]

    _click(window, node)

    tree = window.playlist_panel.tree_for(node.source)
    assert tree.selected_paths() == {tuple(node.node_path)}
    assert _paths(window._table().nodes) == {tuple(node.node_path)}


@pytest.mark.parametrize("modifier", [Qt.ControlModifier, Qt.ShiftModifier])
def test_ctrl_or_shift_selects_several_and_the_sidebar_follows(window, modifier):
    """Like the playlist panes: the sidebar holds the same multi-selection,
    which is what every selection-scoped menu reads."""
    window._set_sync_pane_visible(True)
    window.sync_pane.include_missing.setChecked(True)
    first, *_, last = window.sync_pane.nodes()
    assert first.source == last.source, "the fixture lists one program here"

    _click(window, first)
    _click(window, last, modifier)

    want = (_paths([first, last]) if modifier == Qt.ControlModifier
            else _paths(window.sync_pane.nodes()))
    assert _paths(window.sync_pane.selected_nodes()) == want
    tree = window.playlist_panel.tree_for(first.source)
    assert tree.selected_paths() == want
    assert _paths(window._table().nodes) == want


def test_a_selection_stays_inside_one_program(window):
    """A sync goes from one program, as the sidebar's one-tree-at-a-time
    selection already says. Ctrl-clicking into another group moves there."""
    window._set_sync_pane_visible(True)
    window.sync_pane.include_missing.setChecked(True)
    vdj_test = _synced_pair(window)
    window.membership.edit(vdj_test, [])
    traktor_row = next(n for n in window.sync_pane.nodes() if n.source == TRAKTOR)
    vdj_row = next(n for n in window.sync_pane.nodes() if n.source == VDJ)

    _click(window, traktor_row)
    _click(window, vdj_row, Qt.ControlModifier)

    assert {n.source for n in window.sync_pane.selected_nodes()} == {VDJ}


def test_the_selection_survives_a_refresh_of_the_pane(window):
    window._set_sync_pane_visible(True)
    window.sync_pane.include_missing.setChecked(True)
    first, second = window.sync_pane.nodes()[:2]
    _click(window, first)
    _click(window, second, Qt.ControlModifier)

    window._recompute_status()

    assert _paths(window.sync_pane.selected_nodes()) == _paths([first, second])


@pytest.fixture
def synced(window, monkeypatch):
    """Record which playlists a sync was asked to write, without writing."""
    seen = []
    monkeypatch.setattr(window.sync_engine, "sync_playlist",
                        lambda node, source, mode, target=None:
                        seen.append(node.name) or SyncResult())
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(window, "_confirm_running_targets", lambda targets: True)
    monkeypatch.setattr(type(window), "_reload_preserving_view",
                        lambda self, on_done=None: on_done and on_done())
    return seen


def _menu_callbacks(window, monkeypatch, node):
    """Right-click ``node`` in the pane and hand back the menu's callbacks."""
    from trackcleaner.ui import context_menus
    got = {}

    class _Menu:
        def exec(self, _pos):
            pass

    def build(parent, source, cb):
        got["cb"] = cb
        return _Menu()

    monkeypatch.setattr(context_menus, "playlist_menu", build)
    _click(window, node, button=Qt.RightButton)
    window.sync_pane._on_context(
        window.sync_pane.tree.visualItemRect(_item(window, node)).center())
    return got["cb"]


def test_right_click_inside_the_selection_syncs_all_of_it(window, monkeypatch,
                                                          synced):
    """The point of multi-select: several playlists in one sync."""
    window._set_sync_pane_visible(True)
    window.sync_pane.include_missing.setChecked(True)
    first, second, third = window.sync_pane.nodes()[:3]
    _click(window, first)
    _click(window, third, Qt.ControlModifier)

    _menu_callbacks(window, monkeypatch, third).sync("new", VDJ)

    assert sorted(synced) == sorted([first.name, third.name])
    assert second.name not in synced


def test_right_click_moves_no_selection(window, monkeypatch):
    window._set_sync_pane_visible(True)
    window.sync_pane.include_missing.setChecked(True)
    first, second = window.sync_pane.nodes()[:2]
    _click(window, first)

    _menu_callbacks(window, monkeypatch, second)

    assert _paths(window.sync_pane.selected_nodes()) == _paths([first])


def test_right_click_outside_the_selection_syncs_that_row_alone(
        window, monkeypatch, synced):
    """The sidebar's rule, unchanged -- including when the sidebar's own
    selection still holds playlists from before (a refresh restores it)."""
    window._set_sync_pane_visible(True)
    window.sync_pane.include_missing.setChecked(True)
    first, second, third = window.sync_pane.nodes()[:3]
    _click(window, first)
    _click(window, second, Qt.ControlModifier)

    _menu_callbacks(window, monkeypatch, third).sync("new", VDJ)

    assert synced == [third.name]


def test_the_sidebar_keeps_a_multi_selection_across_a_reload(window):
    """``select_paths`` restores the sidebar after every refresh and sync. A
    plain setCurrentItem is ClearAndSelect, which kept only the last playlist
    -- so syncing several from the pane, then syncing again, did one."""
    tree = window.playlist_panel.tree_for(TRAKTOR)
    nodes = tree.visible_playlist_nodes()[:3]
    tree.select_nodes(nodes)
    snap = window.playlist_panel.snapshot_view()

    window.load_libraries()
    window.playlist_panel.restore_view(snap)

    assert tree.selected_paths() == _paths(nodes)


def test_right_click_offers_the_ordinary_playlist_menu(window, monkeypatch):
    """No second set of actions: the row's program gets its own playlist menu,
    built for the row's node. The builder is stubbed at the module the window
    calls through, so the real wiring runs and no menu blocks the test."""
    from trackcleaner.ui import context_menus
    window._set_sync_pane_visible(True)
    window.sync_pane.include_missing.setChecked(True)
    built = []

    class _Menu:
        def exec(self, _pos):
            pass

    monkeypatch.setattr(context_menus, "playlist_menu",
                        lambda parent, source, cb: built.append(source) or _Menu())
    tree = window.sync_pane.tree
    item = tree.topLevelItem(0).child(0)

    window.sync_pane._on_context(tree.visualItemRect(item).center())

    node = window.sync_pane.nodes()[0]
    assert built == [node.source]


# ============================== across a restart ============================
def test_the_pane_and_its_filter_survive_a_restart(app, tmp_path, monkeypatch):
    first = _make_window(tmp_path, monkeypatch)
    first._set_sync_pane_visible(True)
    first.sync_pane.include_missing.setChecked(True)
    saved = first._capture_state()
    first.close()

    second = _make_window(tmp_path, monkeypatch, ui_state=saved)
    try:
        assert second._sync_pane_on and second._sync_pane_action.isChecked()
        assert second.sync_pane.include_missing.isChecked()
        assert second.sync_pane.rows(), "populated on restore, not just shown"
    finally:
        second.close()


def test_a_session_saved_before_the_pane_existed_still_restores(
        app, tmp_path, monkeypatch):
    """Two splitter sizes, from before the pane: Qt leaves a short list
    undefined, so the trees and tracks must keep their widths."""
    w = _make_window(tmp_path, monkeypatch, ui_state={"h_splitter": [300, 900]})
    try:
        sizes = w.h_splitter.sizes()
        assert len(sizes) == 3 and sizes[1] == 0
        assert not w.sync_pane.isVisible()
    finally:
        w.close()



# ============================== songs to sync ================================
def _songs(window):
    return {(key, status) for key, status, _why in window.sync_pane.song_rows()}


def _table_not_green(window, nodes, source):
    rows, _ghosts = window._playlist_rows(nodes, source, include_tombstones=True)
    return {(t.key, s) for t, s in rows if s != st.GREEN}


def test_the_songs_are_the_highlighted_playlists_not_green_rows(window):
    """Exactly the track table's own rows and glyphs, so the two can never
    disagree about a song."""
    window._set_sync_pane_visible(True)
    window.sync_pane.include_missing.setChecked(True)
    node = window.sync_pane.nodes()[-1]

    _click(window, node)

    expected = _table_not_green(window, [node], node.source)
    assert expected, "the sample needs a playlist with something to sync"
    assert _songs(window) == expected
    assert all(why for _k, _s, why in window.sync_pane.song_rows())


def test_several_playlists_list_their_songs_once_each(window):
    window._set_sync_pane_visible(True)
    window.sync_pane.include_missing.setChecked(True)
    nodes = [n for n in window.sync_pane.nodes()
             if n.source == window.sync_pane.nodes()[-1].source]
    _click(window, nodes[0])
    for node in nodes[1:]:
        _click(window, node, Qt.ControlModifier)

    keys = [k for k, _s, _w in window.sync_pane.song_rows()]
    assert len(keys) == len(set(keys))
    assert set(_songs(window)) == _table_not_green(window, nodes, nodes[0].source)


def test_with_nothing_highlighted_it_says_what_to_do(window):
    window._set_sync_pane_visible(True)
    assert window.sync_pane.song_rows() == []
    assert "Select a playlist" in window.sync_pane.songs.topLevelItem(0).text(0)


def test_clicking_a_song_selects_it_in_the_track_table(window):
    window._set_sync_pane_visible(True)
    window.sync_pane.include_missing.setChecked(True)
    _click(window, window.sync_pane.nodes()[-1])
    key = window.sync_pane.song_rows()[0][0]
    songs = window.sync_pane.songs
    item = songs.topLevelItem(0)

    QTest.mouseClick(songs.viewport(), Qt.LeftButton, Qt.NoModifier,
                     songs.visualItemRect(item).center())

    assert window._table().selected_keys() == [key]


def test_a_missing_file_says_so(window):
    window._set_sync_pane_visible(True)
    window.sync_pane.include_missing.setChecked(True)
    node = window.sync_pane.nodes()[-1]
    key = node.playlist.track_keys[0]
    window._missing_keys.setdefault(node.source, set()).add(key)

    _click(window, node)

    assert (key, st.RED, "file missing on disk") in window.sync_pane.song_rows()


def test_the_split_survives_a_restart(app, tmp_path, monkeypatch):
    first = _make_window(tmp_path, monkeypatch)
    first._set_sync_pane_visible(True)
    first.sync_pane.splitter.setSizes([120, 300])
    saved = first._capture_state()
    first.close()

    second = _make_window(tmp_path, monkeypatch, ui_state=saved)
    try:
        a, b = second.sync_pane.splitter.sizes()
        assert b > a, "the songs section kept the larger share"
    finally:
        second.close()


# ========================= songs: selected like a track list ==================
SONGS = [(f"d:\\m\\{i}.mp3", f"Song {i}", "Artist", st.YELLOW_SELF, "cues")
         for i in range(4)]


@pytest.fixture
def pane(app):
    from trackcleaner.ui.sync_pane import SyncPane
    p = SyncPane()
    p.resize(400, 600)
    p.show()
    p.populate_songs(SONGS, VDJ)
    yield p
    p.close()


def _song_click(pane, index, modifier=Qt.NoModifier, button=Qt.LeftButton):
    songs = pane.songs
    pos = songs.visualItemRect(songs.topLevelItem(index)).center()
    QTest.mouseClick(songs.viewport(), button, modifier, pos)


@pytest.mark.parametrize("modifier", [Qt.ControlModifier, Qt.ShiftModifier])
def test_songs_multi_select_with_ctrl_or_shift(pane, modifier):
    seen = []
    pane.songs_selected.connect(lambda keys, source: seen.append((keys, source)))

    _song_click(pane, 0)
    _song_click(pane, 2, modifier)

    expected = ([SONGS[0][0], SONGS[2][0]] if modifier == Qt.ControlModifier
                else [SONGS[0][0], SONGS[1][0], SONGS[2][0]])
    assert pane.selected_song_keys() == expected
    assert seen[-1] == (expected, VDJ)


def test_right_click_on_a_song_moves_no_selection(pane):
    menus = []
    pane.song_context_requested.connect(lambda k, pos, sel: menus.append((k, sel)))
    _song_click(pane, 0)
    _song_click(pane, 1, Qt.ControlModifier)

    _song_click(pane, 3, button=Qt.RightButton)
    songs = pane.songs
    pane._on_song_context(songs.visualItemRect(songs.topLevelItem(3)).center())

    assert pane.selected_song_keys() == [SONGS[0][0], SONGS[1][0]]
    assert menus[-1] == (SONGS[3][0], [SONGS[0][0], SONGS[1][0]])


def test_the_song_selection_survives_a_refresh(pane):
    _song_click(pane, 1)
    _song_click(pane, 3, Qt.ControlModifier)

    pane.populate_songs(SONGS[1:], VDJ)          # song 0 synced away

    assert pane.selected_song_keys() == [SONGS[1][0], SONGS[3][0]]


def _two_songs(window):
    """Highlight every listed Traktor playlist: two not-green songs in the
    sample library."""
    window._set_sync_pane_visible(True)
    window.sync_pane.include_missing.setChecked(True)
    # The sample's playlists hold one track each; give "test" a second one
    # from Traktor's collection, which VirtualDJ does not have either.
    test = next(n for n in window.libraries[TRAKTOR].playlists() if n.name == "test")
    other = next(k for k, t in window.libraries[TRAKTOR].tracks.items()
                 if t.in_database and k not in test.playlist.track_keys)
    window.membership.add_tracks(test, [other], TRAKTOR)
    window._writer.flush()
    window.load_libraries()
    node = next(n for n in window.sync_pane.nodes()
                if n.source == TRAKTOR and n.name == "test")
    _click(window, node)
    keys = [k for k, _s, _w in window.sync_pane.song_rows()]
    assert len(keys) >= 2, "the sample needs two songs to select"
    return keys


def test_a_song_multi_selection_is_the_track_tables(window):
    keys = _two_songs(window)
    songs = window.sync_pane.songs
    for i in range(len(keys)):
        QTest.mouseClick(songs.viewport(), Qt.LeftButton,
                         Qt.ControlModifier if i else Qt.NoModifier,
                         songs.visualItemRect(songs.topLevelItem(i)).center())

    assert sorted(window._table().selected_keys()) == sorted(keys)


def test_right_click_acts_on_the_whole_song_selection(window, monkeypatch):
    keys = _two_songs(window)
    window.sync_pane.songs.selectAll()
    opened = []
    monkeypatch.setattr(window, "_on_track_context",
                        lambda key, row, pos: opened.append(
                            (key, window._selected_track_keys(key))))

    window._on_sync_pane_song_context(keys[1], None, list(keys))

    assert opened and opened[0][0] == keys[1]
    assert sorted(opened[0][1]) == sorted(keys)


def test_right_click_outside_the_song_selection_acts_on_that_song(window, monkeypatch):
    keys = _two_songs(window)
    opened = []
    monkeypatch.setattr(window, "_on_track_context",
                        lambda key, row, pos: opened.append(
                            window._selected_track_keys(key)))
    window._on_sync_pane_songs([keys[0]], TRAKTOR)

    window._on_sync_pane_song_context(keys[1], None, [keys[0]])

    assert opened == [[keys[1]]]

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
