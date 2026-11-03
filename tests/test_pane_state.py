"""State that belongs to a *pane* being read from the window instead.

Three reported bugs shared one cause: `ViewController.on_tab_focus_changed`
mirrored the focused tab into the sidebar but never moved the window's
`_current_source`. So after switching to a VirtualDJ tab, Left/Right kept walking
the previous program's tree, and the Playlists… popup answered for whatever was
last selected in the sidebar rather than for the tab the track was in.

Plus the selection, which `set_rows` dropped on every refresh, sync and tag edit
because nothing restored it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                              # noqa: E402

from trackcleaner.config.paths import Settings                          # noqa: E402
from trackcleaner.io import backup as backup_mod                        # noqa: E402
from trackcleaner.model import status as st                             # noqa: E402
from trackcleaner.model.library import TRAKTOR, VDJ, Track              # noqa: E402
from fixtures import write_sample                                       # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(tmp_path / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(tmp_path / "settings.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(tmp_path / "baseline.csv"))
    monkeypatch.setattr(paths_mod, "app_dir", lambda: str(tmp_path))
    from trackcleaner.ui.main_window import MainWindow
    settings = Settings()
    settings.paths = write_sample(tmp_path)
    w = MainWindow(settings)
    w.show()
    yield w
    w.close()


def _node_with_tracks(window, source):
    return next((n for n in window.libraries[source].playlists()
                 if n.playlist and n.playlist.track_keys), None)


# ==================== the current source follows the tab ====================
def test_focusing_a_tab_moves_the_current_source(window):
    """The single omission behind both the arrow bug and the popup bug."""
    window._current_source = VDJ
    table = window.track_tabs.focused_table()
    table.set_context(TRAKTOR, [])

    window.views.on_tab_focus_changed()

    assert window._current_source == TRAKTOR


def test_an_empty_tab_does_not_move_it(window):
    """A brand-new tab has no program; adopting "" would leave the arrows with
    no tree to walk at all."""
    window._current_source = TRAKTOR
    window.track_tabs.focused_table().set_context("", [])

    window.views.on_tab_focus_changed()

    assert window._current_source == TRAKTOR


def test_it_moves_even_when_the_tab_shows_several_playlists(window):
    """The node guard is about which *row* to highlight, not which program is
    current -- so it must not skip this."""
    window._current_source = VDJ
    node = _node_with_tracks(window, TRAKTOR)
    window.track_tabs.focused_table().set_context(TRAKTOR, [node, node])

    window.views.on_tab_focus_changed()

    assert window._current_source == TRAKTOR


def test_the_arrows_then_walk_that_tabs_tree(window):
    """The reported symptom: Left/Right kept incrementing the previous
    program's pane."""
    window._current_source = VDJ
    window.track_tabs.focused_table().set_context(TRAKTOR, [])
    window.views.on_tab_focus_changed()

    assert window._arrow_tree() is window.playlist_panel.trees[TRAKTOR]


def test_stepping_starts_from_the_tabs_own_playlist(window):
    """on_tab_focus_changed highlights the tab's node, which also sets the
    tree's current item -- so the first arrow press steps from there rather than
    from wherever the sidebar was last clicked."""
    node = _node_with_tracks(window, TRAKTOR)
    table = window.track_tabs.focused_table()
    table.set_context(TRAKTOR, [node])

    window.views.on_tab_focus_changed()

    tree = window.playlist_panel.trees[TRAKTOR]
    assert tree.currentItem() is not None
    assert tree.currentItem().text(0) == node.name


# ========================= the Playlists… popup =============================
def test_the_popup_lists_every_loaded_program(window):
    from trackcleaner.ui.playlists_window import PlaylistsWindow
    node = _node_with_tracks(window, TRAKTOR)
    key = node.playlist.track_keys[0]

    dlg = PlaylistsWindow(window.libraries, key, "label", TRAKTOR)

    headers = [dlg.list.item(i).text() for i in range(dlg.list.count())
               if not dlg.list.item(i).text().startswith("    ")]
    for source in window.libraries:
        assert any(source.upper() in h for h in headers), source


def test_it_finds_a_traktor_playlist_for_a_traktor_track(window):
    """It used to show VirtualDJ's playlists whatever pane you asked from."""
    from trackcleaner.ui.playlists_window import PlaylistsWindow
    node = _node_with_tracks(window, TRAKTOR)
    key = node.playlist.track_keys[0]

    dlg = PlaylistsWindow(window.libraries, key, "label", TRAKTOR)

    rows = [dlg.list.item(i).text().strip() for i in range(dlg.list.count())]
    assert any(node.name in r for r in rows)


def test_the_tracks_own_program_is_listed_first(window):
    from trackcleaner.ui.playlists_window import PlaylistsWindow

    dlg = PlaylistsWindow(window.libraries, "nope", "label", TRAKTOR)

    assert dlg.list.item(0).text().startswith(TRAKTOR.upper())


def test_a_headers_row_cannot_be_navigated_to(window):
    """Double-clicking "TRAKTOR — 2" must not try to open a program."""
    from trackcleaner.ui.playlists_window import PlaylistsWindow
    from PySide6.QtCore import Qt
    dlg = PlaylistsWindow(window.libraries, "nope", "label", TRAKTOR)

    assert not (dlg.list.item(0).flags() & Qt.ItemIsSelectable)


def test_a_track_in_nothing_says_so(window):
    from trackcleaner.ui.playlists_window import PlaylistsWindow

    dlg = PlaylistsWindow(window.libraries, "not-a-key", "label", TRAKTOR)

    text = " ".join(dlg.list.item(i).text() for i in range(dlg.list.count()))
    assert "not in any playlist" in text


def test_navigating_reports_the_row_s_own_program(window):
    """The row carries its source, so a Traktor row navigates the Traktor tree
    even though the popup lists several programs."""
    from trackcleaner.ui.playlists_window import PlaylistsWindow
    node = _node_with_tracks(window, TRAKTOR)
    key = node.playlist.track_keys[0]
    dlg = PlaylistsWindow(window.libraries, key, "label", TRAKTOR)
    seen = []
    dlg.navigate.connect(lambda n, s: seen.append(s))

    row = next(dlg.list.item(i) for i in range(dlg.list.count())
               if dlg.list.item(i).text().startswith("    "))
    dlg._on_activate(row)

    assert seen == [TRAKTOR]


# ===================== the selection survives a repopulate ==================
def _stock(window, count=5):
    table = window.track_tabs.focused_table()
    table.set_rows([(Track(key=f"k{i}", source=TRAKTOR, path=f"D:/M/{i}.mp3",
                           title=f"T{i}"), st.GREEN) for i in range(count)])
    return table


def test_the_selection_comes_back_after_a_repopulate(window):
    table = _stock(window)
    table.select_visual_row(2)
    keys = table.selected_keys()

    _stock(window)                       # the model reset a refresh performs
    table.select_keys(keys)

    assert table.selected_keys() == keys


def test_it_is_restored_by_key_not_by_row(window):
    """A sync consumes preview rows and reorders what is left, so the row a
    track sat on means something else afterwards."""
    table = _stock(window, 5)
    table.select_visual_row(3)           # k3
    keys = table.selected_keys()

    table.set_rows([(Track(key=f"k{i}", source=TRAKTOR, path=f"D:/M/{i}.mp3",
                           title=f"T{i}"), st.GREEN) for i in (1, 3, 4)])
    table.select_keys(keys)

    assert table.selected_keys() == ["k3"]


def test_a_track_that_is_gone_is_dropped(window):
    table = _stock(window, 3)

    assert table.select_keys(["k99"]) == 0
    assert table.selected_keys() == []


def test_a_multi_row_selection_survives(window):
    table = _stock(window)
    table.select_keys(["k1", "k3"])
    keys = table.selected_keys()

    _stock(window)
    table.select_keys(keys)

    assert table.selected_keys() == ["k1", "k3"]


def test_restoring_sets_a_current_row(window):
    """Or Up/Down would start from the top again despite a visible highlight."""
    table = _stock(window)

    table.select_keys(["k2"])

    assert table.currentIndex().isValid()


def test_populate_restores_the_selection(window, app):
    """End to end through the controller, which is where a refresh goes."""
    node = _node_with_tracks(window, TRAKTOR)
    table = window.track_tabs.focused_table()
    window.views.populate_table(table, [node], TRAKTOR,
                                include_tombstones=False, preserve_scroll=False)
    if not table.rowCount():
        pytest.skip("fixture playlist is empty")
    table.select_visual_row(0)
    keys = table.selected_keys()

    window.views.populate_table(table, [node], TRAKTOR,
                                include_tombstones=False, preserve_scroll=True)

    assert table.selected_keys() == keys


def test_populating_an_empty_selection_is_not_an_error(window, app):
    node = _node_with_tracks(window, TRAKTOR)
    table = window.track_tabs.focused_table()

    window.views.populate_table(table, [node], TRAKTOR,
                                include_tombstones=False, preserve_scroll=True)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
