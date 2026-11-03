"""The arrows, routed application-wide and split by axis.

Left/Right walk the **playlist sidebar**; Up/Down walk the **track list** — from
anywhere in the window, whatever has focus. That is the fix for two things at
once: clicking a playlist put focus in the tree, where Left/Right were never
handled and did nothing, while `QTreeWidget` handled Up/Down itself and moved the
*playlist* selection instead of the track list.

Consuming the events is the mechanism, not a side effect: it is what stops the
tree navigating on Up/Down.

Also covers dispatching a mouse button, which `QShortcut` cannot do.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt                          # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent                        # noqa: E402
from PySide6.QtWidgets import (QApplication, QComboBox, QLineEdit,      # noqa: E402
                               QSlider, QSpinBox)

from trackcleaner.config.paths import Settings                          # noqa: E402
from trackcleaner.io import backup as backup_mod                        # noqa: E402
from trackcleaner.model import status as st                             # noqa: E402
from trackcleaner.model.library import TRAKTOR, Track                   # noqa: E402
from trackcleaner.ui import main_window as mw_mod                       # noqa: E402
from trackcleaner.ui.main_window import (arrow_keys_blocked,            # noqa: E402
                                         route_arrow_key,
                                         route_mouse_binding)
from fixtures import write_sample                                       # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _make_window(tmp_path, monkeypatch):
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
    return w


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    w._current_source = TRAKTOR
    yield w
    w.close()


@pytest.fixture
def stocked(window):
    """A window whose focused table holds five rows."""
    table = window.track_tabs.focused_table()
    table.set_rows([(Track(key=f"k{i}", source=TRAKTOR, path=f"D:/M/{i}.mp3",
                           title=f"T{i}"), st.GREEN) for i in range(5)])
    return window


def _key(key, modifiers=Qt.NoModifier, etype=QEvent.KeyPress):
    return QKeyEvent(etype, key, modifiers)


# ============================ which axis goes where =========================
def test_right_walks_the_sidebar(window):
    seen = []
    window.step_playlist = lambda delta: seen.append(delta)

    assert route_arrow_key(_key(Qt.Key_Right), window) is True
    assert seen == [1]


def test_left_walks_the_sidebar_backwards(window):
    seen = []
    window.step_playlist = lambda delta: seen.append(delta)

    route_arrow_key(_key(Qt.Key_Left), window)

    assert seen == [-1]


def test_down_walks_the_track_list(window):
    seen = []
    window.step_track_row = lambda delta: seen.append(delta)

    assert route_arrow_key(_key(Qt.Key_Down), window) is True
    assert seen == [1]


def test_up_walks_the_track_list_backwards(window):
    seen = []
    window.step_track_row = lambda delta: seen.append(delta)

    route_arrow_key(_key(Qt.Key_Up), window)

    assert seen == [-1]


def test_up_and_down_never_touch_the_sidebar(window):
    """The requirement, stated directly: the playlist pane no longer navigates
    on Up/Down."""
    seen = []
    window.step_playlist = lambda delta: seen.append(delta)
    window.step_track_row = lambda delta: None

    route_arrow_key(_key(Qt.Key_Up), window)
    route_arrow_key(_key(Qt.Key_Down), window)

    assert seen == []


def test_the_arrows_are_consumed(window):
    """Returning True is what stops the focused tree handling them itself --
    without it the sidebar would still move on Up/Down."""
    window.step_playlist = lambda delta: None
    window.step_track_row = lambda delta: None

    for key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
        assert route_arrow_key(_key(key), window) is True, key


def test_other_keys_are_left_alone(window):
    assert route_arrow_key(_key(Qt.Key_A), window) is False
    assert route_arrow_key(_key(Qt.Key_Home), window) is False


def test_a_key_release_does_not_step_a_second_time(window):
    """Every press produces a release; acting on both would double every step."""
    seen = []
    window.step_playlist = lambda delta: seen.append(delta)

    route_arrow_key(_key(Qt.Key_Right, etype=QEvent.KeyRelease), window)

    assert seen == []


def test_a_non_key_event_is_ignored(window):
    """The filter sees every event Qt produces, including internal ones whose
    type() raises in PySide6 -- so the class check has to come first."""
    assert route_arrow_key(QEvent(QEvent.User), window) is False


# ============================== modifiers ===================================
def test_ctrl_left_is_left_for_previous_track(window):
    """Ctrl+Left is bound to Previous track. The arrow router claiming it
    regardless of modifiers is exactly the trap the transport filter fell into
    with Ctrl+W."""
    seen = []
    window.step_playlist = lambda delta: seen.append(delta)

    assert route_arrow_key(_key(Qt.Key_Left, Qt.ControlModifier), window) is False
    assert seen == []


def test_shift_arrow_still_navigates(window):
    """Shift is not claimed by anything here, and Shift+Down is how a list is
    extended -- leaving it to the router keeps one behaviour, not two."""
    seen = []
    window.step_track_row = lambda delta: seen.append(delta)

    route_arrow_key(_key(Qt.Key_Down, Qt.ShiftModifier), window)

    assert seen == [1]


# ============================== standing down ===============================
@pytest.mark.parametrize("factory", [QLineEdit, QComboBox])
def test_text_entry_keeps_its_arrows(app, factory):
    """Where an arrow moves a caret or changes a value it is that widget's input.
    Asserted against the type list rather than by moving focus: the offscreen
    platform has no window manager, and patching QApplication.focusWidget leaks
    into every later test in the process -- which is how this file first failed."""
    assert isinstance(factory(), mw_mod.ARROW_OWNING_WIDGETS)


@pytest.mark.parametrize("factory", [QSlider, QSpinBox])
def test_a_slider_does_not_swallow_the_arrows(app, factory):
    """Qt gives the tempo fader focus as soon as the window is shown. With
    sliders in the list the arrows navigated nothing at all until something else
    was clicked -- which is the opposite of routing them application-wide."""
    assert not isinstance(factory(), mw_mod.ARROW_OWNING_WIDGETS)


def test_a_plain_widget_does_not_block_the_arrows(app):
    """The other direction: the track table and the sidebar must not be in the
    list, or the arrows would never route at all."""
    from trackcleaner.ui.track_table import TrackTable
    from trackcleaner.ui.playlist_trees import LibraryTree

    assert not isinstance(TrackTable(), mw_mod.ARROW_OWNING_WIDGETS)
    assert not isinstance(LibraryTree(TRAKTOR), mw_mod.ARROW_OWNING_WIDGETS)


def test_the_arrows_survive_the_tempo_fader_having_focus(app, window):
    """End to end on the above, through the real widget: this is the state the
    window is actually in after it opens."""
    window.waveform_panel.tempo.setFocus()
    seen = []
    window.step_playlist = lambda delta: seen.append(delta)

    route_arrow_key(_key(Qt.Key_Right), window)

    assert seen == [1]


def test_arrows_run_when_nothing_owns_them(app):
    """No focus at all is not a reason to stand down."""
    if QApplication.focusWidget() is not None:
        pytest.skip("another widget still holds focus in this process")

    assert arrow_keys_blocked() is False


def test_auto_repeat_is_ignored(window):
    """A held arrow would otherwise fire the double-tap gesture continuously."""
    seen = []
    window.step_playlist = lambda delta: seen.append(delta)
    event = QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.NoModifier, autorep=True)

    assert route_arrow_key(event, window) is False
    assert seen == []


# ========================== moving the track selection ======================
def test_up_with_nothing_selected_lands_on_the_top_row(stocked):
    """Asked for directly: entering the list starts at the top."""
    table = stocked.track_tabs.focused_table()
    table.clearSelection()

    stocked.step_track_row(-1)

    assert table.selected_visual_rows() == [0]


def test_down_with_nothing_selected_also_lands_on_the_top(stocked):
    """Same reasoning -- there is nothing above to move from."""
    table = stocked.track_tabs.focused_table()
    table.clearSelection()

    stocked.step_track_row(1)

    assert table.selected_visual_rows() == [0]


def test_down_moves_one_row(stocked):
    table = stocked.track_tabs.focused_table()
    table.select_visual_row(1)

    stocked.step_track_row(1)

    assert table.selected_visual_rows() == [2]


def test_up_moves_one_row(stocked):
    table = stocked.track_tabs.focused_table()
    table.select_visual_row(3)

    stocked.step_track_row(-1)

    assert table.selected_visual_rows() == [2]


def test_it_stops_at_the_bottom(stocked):
    """Wrapping a track list on a held key would jump the view to the far end."""
    table = stocked.track_tabs.focused_table()
    table.select_visual_row(4)

    stocked.step_track_row(1)

    assert table.selected_visual_rows() == [4]


def test_it_stops_at_the_top(stocked):
    table = stocked.track_tabs.focused_table()
    table.select_visual_row(0)

    stocked.step_track_row(-1)

    assert table.selected_visual_rows() == [0]


def test_an_empty_table_is_not_an_error(window):
    window.track_tabs.focused_table().set_rows([])

    window.step_track_row(1)          # must not raise


def test_down_continues_past_a_multi_row_selection(stocked):
    """From the far edge of the block, so Down does not collapse back to its
    start and appear stuck."""
    table = stocked.track_tabs.focused_table()
    from PySide6.QtCore import QItemSelectionModel
    table.select_visual_row(1)
    flags = QItemSelectionModel.Select | QItemSelectionModel.Rows
    table.selectionModel().select(table._proxy.index(2, 0), flags)
    assert table.selected_visual_rows() == [1, 2]

    stocked.step_track_row(1)

    assert table.selected_visual_rows() == [3]


# ============================== mouse dispatch ==============================
def _mouse(button, modifiers=Qt.NoModifier, etype=QEvent.MouseButtonPress):
    return QMouseEvent(etype, QPointF(1, 1), QPointF(1, 1), button, button,
                       modifiers)


def test_a_bound_mouse_button_fires_its_handler(app):
    fired = []
    bindings = {((), "Middle"): lambda: fired.append(True)}

    assert route_mouse_binding(_mouse(Qt.MiddleButton), bindings) is True
    assert fired == [True]


def test_an_unbound_button_is_left_alone(app):
    bindings = {((), "Middle"): lambda: None}

    assert route_mouse_binding(_mouse(Qt.BackButton), bindings) is False


def test_a_bare_click_does_not_fire_a_modified_binding(app):
    """The important one: ordinary clicking must not trigger bound actions."""
    fired = []
    bindings = {(("Ctrl",), "Left"): lambda: fired.append(True)}

    assert route_mouse_binding(_mouse(Qt.LeftButton), bindings) is False
    assert fired == []


def test_the_modified_click_does_fire_it(app):
    fired = []
    bindings = {(("Ctrl",), "Left"): lambda: fired.append(True)}

    route_mouse_binding(_mouse(Qt.LeftButton, Qt.ControlModifier), bindings)

    assert fired == [True]


def test_the_wrong_modifier_does_not_fire(app):
    fired = []
    bindings = {(("Ctrl",), "Left"): lambda: fired.append(True)}

    route_mouse_binding(_mouse(Qt.LeftButton, Qt.AltModifier), bindings)

    assert fired == []


def test_a_release_does_not_fire(app):
    fired = []
    bindings = {((), "Middle"): lambda: fired.append(True)}

    route_mouse_binding(
        _mouse(Qt.MiddleButton, etype=QEvent.MouseButtonRelease), bindings)

    assert fired == []


def test_no_bindings_is_not_an_error(app):
    assert route_mouse_binding(_mouse(Qt.MiddleButton), None) is False
    assert route_mouse_binding(_mouse(Qt.MiddleButton), {}) is False


def test_a_non_mouse_event_is_ignored(app):
    assert route_mouse_binding(_key(Qt.Key_A), {((), "Middle"): lambda: None}) \
        is False


def test_a_mouse_binding_reaches_the_dispatch_map(window):
    """apply_controls has to route these away from QShortcut, which cannot fire
    on a mouse button at all."""
    window.apply_controls({"app.refresh": "Ctrl+Mouse:Middle"})

    assert (("Ctrl",), "Middle") in window._mouse_bindings
    assert window._shortcuts == [], "a mouse binding is not a QShortcut"


def test_a_bare_left_binding_is_never_armed(window):
    """Even from a hand-edited ini."""
    window.apply_controls({"app.refresh": "Mouse:Left"})

    assert window._mouse_bindings == {}


def test_key_and_mouse_bindings_coexist(window):
    window.apply_controls({"app.refresh": "F5", "tab.new": "Mouse:Middle"})

    assert len(window._shortcuts) == 1
    assert len(window._mouse_bindings) == 1


# ============================== pane focus ==================================
def test_focusing_the_other_pane_does_nothing_when_unsplit(window):
    """Splitting is a deliberate action from the tab menu; a focus key that
    rearranged the layout would be surprising to lean on."""
    assert window.track_tabs.focus_other_group() is False


def test_it_moves_focus_when_split(window):
    tabs = window.track_tabs
    before = tabs.focused_table()
    tabs.split_focused()

    assert tabs.focus_other_group() is True
    assert tabs.focused_table() is not before


def test_it_comes_back(window):
    tabs = window.track_tabs
    tabs.split_focused()
    first = tabs.focused_table()

    tabs.focus_other_group()
    tabs.focus_other_group()

    assert tabs.focused_table() is first


def test_the_action_is_bound_by_default(window):
    from trackcleaner.config import controls as controls_mod

    assert controls_mod.ACTION_BY_ID["pane.focus_other"].default == "F6"
    assert "pane.focus_other" in window._action_handlers()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
