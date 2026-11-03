"""Tabbed, splittable track area: tab groups, the "+" tab, and split/unsplit."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                                     # noqa: E402
from PySide6.QtWidgets import QApplication                        # noqa: E402

from trackcleaner.ui import theme                                 # noqa: E402
from trackcleaner.ui.track_tabs import TrackTabs, _PLUS           # noqa: E402


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    theme.capture_defaults(a)
    return a


def _tabs(app):
    return TrackTabs(on_clicked=lambda k: None, on_double=lambda k: None,
                     on_context=lambda k, p: None)


def test_starts_with_one_group_one_tab_plus_a_plus_tab(app):
    t = _tabs(app)
    assert len(t._groups()) == 1
    g = t._groups()[0]
    assert g.real_count() == 1
    assert g.count() == 2                         # one real tab + the "+"
    assert g.tabBar().tabData(g._plus_index()) == _PLUS
    assert t.focused_table() is not None


def test_clicking_the_plus_tab_adds_a_real_tab(app):
    t = _tabs(app)
    g = t._groups()[0]
    before = g.real_count()
    g._on_tab_clicked(g._plus_index())
    assert g.real_count() == before + 1
    # the "+" is never the shown page
    assert g.tabBar().tabData(g.currentIndex()) != _PLUS


def test_split_moves_the_tab_into_a_new_group(app):
    t = _tabs(app)
    g0 = t._groups()[0]
    moved = g0.current_table()
    t.split_from(g0, Qt.Horizontal)
    groups = t._groups()
    assert len(groups) == 2
    # the tab jumped over to the second group...
    assert moved in groups[1].tables()
    assert moved not in groups[0].tables()
    # ...and the emptied first group has a fresh empty tab (never bare)
    assert groups[0].real_count() == 1
    assert t.splitter.orientation() == Qt.Horizontal


def test_split_horizontal_stacks(app):
    t = _tabs(app)
    t.split_from(t._groups()[0], Qt.Vertical)
    assert t.splitter.orientation() == Qt.Vertical


def test_each_group_has_its_own_plus(app):
    t = _tabs(app)
    t.split_from(t._groups()[0], Qt.Horizontal)
    for g in t._groups():
        assert g.tabBar().tabData(g._plus_index()) == _PLUS
        before = g.real_count()
        g._on_tab_clicked(g._plus_index())
        assert g.real_count() == before + 1


def test_all_tables_and_focus_span_groups(app):
    t = _tabs(app)
    t.split_from(t._groups()[0], Qt.Horizontal)
    assert len(t.all_tables()) == 2
    second = t._groups()[1].current_table()
    second.focused.emit()
    assert t.focused_table() is second


def test_only_two_groups(app):
    t = _tabs(app)
    t.split_from(t._groups()[0], Qt.Horizontal)
    t.split_from(t._groups()[0], Qt.Horizontal)   # ignored -- already split
    assert len(t._groups()) == 2


def test_unsplit_merges_back_to_one_group(app):
    t = _tabs(app)
    g0 = t._groups()[0]
    g0.new_tab()                                  # two tabs so the merge is visible
    t.split_from(g0, Qt.Horizontal)
    total = sum(g.real_count() for g in t._groups())
    t.unsplit()
    assert len(t._groups()) == 1
    assert t._groups()[0].real_count() == total


def test_closing_the_last_tab_leaves_a_fresh_one(app):
    t = _tabs(app)
    g = t._groups()[0]
    t.close_tab(g, g.currentIndex())
    assert len(t._groups()) == 1
    assert g.real_count() == 1                    # never zero tabs overall


def test_closing_a_group_empty_collapses_the_split(app):
    t = _tabs(app)
    t.split_from(t._groups()[0], Qt.Horizontal)
    second = t._groups()[1]
    # close the only tab in the second group -> the split collapses
    t.close_tab(second, second.currentIndex())
    assert len(t._groups()) == 1


def test_single_group_is_marked_focused(app):
    t = _tabs(app)
    assert t._groups()[0].tabBar().group_focused is True


def test_focused_group_flag_tracks_the_active_group(app):
    t = _tabs(app)
    t.split_from(t._groups()[0], Qt.Horizontal)
    a, b = t._groups()
    # the moved tab is in B, which is now focused
    assert a.tabBar().group_focused is False
    assert b.tabBar().group_focused is True
    # focusing a table in A swaps the flags
    a.current_table().focused.emit()
    assert a.tabBar().group_focused is True
    assert b.tabBar().group_focused is False


def test_switching_tabs_does_not_swap_their_program_colour(app):
    """The reported bug: clicking a tab used to stamp the newly-focused tab's
    source onto the tab you left (blue<->red swap). Sources must stay put."""
    t = _tabs(app)
    g = t._groups()[0]
    # tab 0 exists (empty); give it a Traktor source, add a VDJ tab
    g.tabBar().setTabData(0, "traktor")
    g.tables()[0].source = "traktor"
    vdj = g.new_tab()
    vdj.source = "vdj"
    g.tabBar().setTabData(g.tables().index(vdj), "vdj")
    tr_idx, vdj_idx = 0, g.tables().index(vdj)

    # Reproduce the real click ordering: tabBarClicked fires while the *old* tab
    # is still current (Qt updates currentIndex only afterwards).
    g.setCurrentIndex(tr_idx)
    g._on_tab_clicked(vdj_idx)                    # click VDJ while Traktor current
    assert g.tabBar().tabData(tr_idx) == "traktor", "left tab kept its colour"
    assert g.tabBar().tabData(vdj_idx) == "vdj"
    # and back
    g.setCurrentIndex(vdj_idx)
    g._on_tab_clicked(tr_idx)
    assert g.tabBar().tabData(tr_idx) == "traktor"
    assert g.tabBar().tabData(vdj_idx) == "vdj"


def test_cross_group_focus_keeps_each_tab_colour(app):
    t = _tabs(app)
    a = t._groups()[0]
    a.tables()[0].source = "traktor"
    a.tabBar().setTabData(0, "traktor")
    t.split_from(a, Qt.Horizontal)               # traktor tab jumps to group B
    a, b = t._groups()
    b.tables()[0].source = "traktor"             # moved tab is traktor
    # give A's fresh tab a VDJ identity
    a.tables()[0].source = "vdj"
    a.tabBar().setTabData(0, "vdj")

    a.current_table().focused.emit()             # focus A
    b.current_table().focused.emit()             # focus B
    a.current_table().focused.emit()             # focus A again
    assert a.tabBar().tabData(0) == "vdj"
    assert b.tabBar().tabData(0) == "traktor"


def test_clicking_the_current_tab_of_the_other_group_focuses_it(app):
    """A single-tab non-focused group: clicking its (already-current) tab must
    still focus that group, even though currentChanged does not fire."""
    t = _tabs(app)
    t.split_from(t._groups()[0], Qt.Horizontal)
    a, b = t._groups()
    assert b.tabBar().group_focused is True       # B holds the moved tab
    a._on_tab_clicked(a.currentIndex())           # click A's current tab
    assert a.tabBar().group_focused is True
    assert b.tabBar().group_focused is False
    assert t.focused_table() is a.current_table()


def test_label_current_sets_text_and_source(app):
    t = _tabs(app)
    t.label_current("Breaks", "traktor")
    g = t._groups()[0]
    i = g.currentIndex()
    assert g.tabText(i) == "Breaks"
    assert g.tabBar().tabData(i) == "traktor"


def test_on_tracks_dropped_relays_with_the_receiving_table(app):
    """main_window needs to know WHICH table received a drop (to read its
    .nodes for the single-playlist gate) -- confirm the callback is handed the
    actual receiving TrackTable, not just the keys/source."""
    received = []
    t = TrackTabs(on_clicked=lambda k: None, on_double=lambda k: None,
                 on_context=lambda k, p: None,
                 on_tracks_dropped=lambda tbl, keys, src, at: received.append((tbl, keys, src, at)))
    table = t.focused_table()
    table.tracks_dropped.emit(["k1", "k2"], "vdj", -1)
    assert received == [(table, ["k1", "k2"], "vdj", -1)]


def test_close_rect_is_inside_the_tab_and_skips_the_plus(app):
    t = _tabs(app)
    g = t._groups()[0]
    g.resize(400, 30)
    app.processEvents()
    bar = g.tabBar()
    real = 0                                    # the one real tab
    close = bar._close_rect(real)
    assert bar.tabRect(real).contains(close.center()), "× must sit within its tab"
    # the "+" tab has no close box that mousePress would honour
    assert bar.tabData(g._plus_index()) == _PLUS


def test_left_click_on_the_x_closes_the_tab_without_selecting(app):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    t = _tabs(app)
    g = t._groups()[0]
    g.new_tab()                                 # now two real tabs
    g.resize(600, 30)
    app.processEvents()
    before = g.real_count()
    bar = g.tabBar()
    center = bar._close_rect(0).center()
    ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(center),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    bar.mousePressEvent(ev)
    assert g.real_count() == before - 1, "clicking × should close one tab"


# ========================= unfocused pane selection =========================
# Qt only swaps to the Inactive palette group when the whole WINDOW deactivates,
# never when a sibling pane takes focus -- so without set_pane_active both split
# panes paint their selection as if they were the active target.
def _highlight(table):
    from PySide6.QtGui import QPalette
    return table.palette().color(QPalette.Active, QPalette.Highlight).name()


def _muted():
    return theme.HIGHLIGHT_MUTED.lower()


def test_an_unsplit_pane_uses_the_theme_highlight(app):
    from PySide6.QtGui import QPalette
    t = _tabs(app)
    table = t.focused_table()
    assert table.pane_active is True
    assert _highlight(table) == \
        app.palette().color(QPalette.Active, QPalette.Highlight).name()


def test_set_pane_active_false_mutes_the_selection(app):
    t = _tabs(app)
    table = t.focused_table()
    active = _highlight(table)

    table.set_pane_active(False)

    assert table.pane_active is False
    assert _highlight(table) == _muted()
    assert _highlight(table) != active


def test_the_muted_colour_holds_when_the_window_is_not_frontmost(app):
    """Set on the Inactive group too, or the grey would revert to blue the
    moment the window loses focus."""
    from PySide6.QtGui import QPalette
    t = _tabs(app)
    table = t.focused_table()
    table.set_pane_active(False)
    assert table.palette().color(QPalette.Inactive,
                                 QPalette.Highlight).name() == _muted()


def test_only_the_focused_group_is_active_after_a_split(app):
    t = _tabs(app)
    t._groups()[0].new_tab()                      # two tabs, so a split can move one
    t.split_from(t._groups()[0], Qt.Horizontal)
    assert t.is_split()

    focused = t.focused_table()
    others = [tb for tb in t.all_tables() if tb is not focused]

    assert focused.pane_active is True
    assert _highlight(focused) != _muted(), "the active pane keeps the theme colour"
    assert others and all(tb.pane_active is False for tb in others)
    assert all(_highlight(tb) == _muted() for tb in others)


def test_unsplitting_restores_the_survivor(app):
    t = _tabs(app)
    t._groups()[0].new_tab()
    t.split_from(t._groups()[0], Qt.Horizontal)
    t.unsplit()

    assert not t.is_split()
    assert all(tb.pane_active is True for tb in t.all_tables())
    assert all(_highlight(tb) != _muted() for tb in t.all_tables())


def test_focus_moving_between_panes_swaps_which_is_muted(app):
    t = _tabs(app)
    t._groups()[0].new_tab()
    t.split_from(t._groups()[0], Qt.Horizontal)
    first, second = t._groups()[0], t._groups()[1]

    t._set_focused(first.current_table())
    assert first.current_table().pane_active is True
    assert second.current_table().pane_active is False

    t._set_focused(second.current_table())
    assert first.current_table().pane_active is False
    assert second.current_table().pane_active is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
