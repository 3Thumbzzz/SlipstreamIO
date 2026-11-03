"""Collapsible Hidden / VDJ / Traktor playlist sections.

The subtlety: inside a QSplitter, hiding the inner widget does not give the
space back -- the pane keeps its pixels and just shows blank. Collapsing has to
clamp the section's maximumHeight as well, which is what these tests pin.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                        # noqa: E402

from trackcleaner.ui.playlist_trees import PlaylistPanel, Section  # noqa: E402
from trackcleaner.model.library import TRAKTOR, VDJ                  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app):
    p = PlaylistPanel()
    p.resize(300, 760)
    p.show()
    app.processEvents()
    yield p
    p.close()


def test_sections_start_expanded(panel):
    """One section per registered program, plus Hidden. Asserted against the
    registry rather than a literal pair, so registering a program doesn't need
    an edit here -- that indifference is the point of the adapter layer."""
    from trackcleaner.adapters import ADAPTER_IDS

    assert set(panel.sections) == {"hidden", *ADAPTER_IDS}
    assert panel.collapsed_sections() == []
    assert all(not s.is_collapsed() for s in panel.sections.values())


def test_collapsing_hides_the_tree_and_clamps_the_height(app, panel):
    section = panel.sections["vdj"]
    section.set_collapsed(True)
    app.processEvents()
    assert section.is_collapsed()
    assert not panel.trees[VDJ].isVisible(), "the tree itself must be hidden"
    assert section.maximumHeight() < 60, (
        "without the height clamp the splitter keeps the pane at full size")


def test_collapsing_gives_the_space_to_the_other_sections(app, panel):
    before = panel.splitter.sizes()
    panel.sections["hidden"].set_collapsed(True)
    app.processEvents()
    after = panel.splitter.sizes()
    n = len(panel.section_order())
    assert after[0] < before[0], "collapsed pane must shrink"
    assert sum(after[1:n]) > sum(before[1:n]), \
        "space must be handed over to the sections still expanded"


def test_expanding_restores_the_tree_and_the_height(app, panel):
    section = panel.sections["traktor"]
    before = panel.splitter.sizes()
    section.set_collapsed(True)
    app.processEvents()
    section.set_collapsed(False)
    app.processEvents()
    assert not section.is_collapsed()
    assert panel.trees[TRAKTOR].isVisible()
    assert section.maximumHeight() > 1000
    assert panel.splitter.sizes() == before, "Qt restores the original proportions"


def test_toggle_flips_the_state(app, panel):
    section = panel.sections["vdj"]
    section.toggle()
    assert section.is_collapsed()
    section.toggle()
    assert not section.is_collapsed()


def test_the_button_toggles_and_the_arrow_follows(app, panel):
    from PySide6.QtCore import Qt
    section = panel.sections["vdj"]
    assert section.toggle_btn.arrowType() == Qt.DownArrow
    section.toggle_btn.click()
    app.processEvents()
    assert section.is_collapsed()
    assert section.toggle_btn.arrowType() == Qt.RightArrow
    section.toggle_btn.click()
    app.processEvents()
    assert section.toggle_btn.arrowType() == Qt.DownArrow


def test_setting_the_same_state_twice_is_a_no_op(app, panel):
    section = panel.sections["vdj"]
    fired = []
    section.toggled.connect(lambda: fired.append(1))
    section.set_collapsed(False)             # already expanded
    assert fired == []
    section.set_collapsed(True)
    assert fired == [1]


def test_collapsed_state_round_trips(app, panel):
    panel.set_collapsed_sections(["hidden", "traktor"])
    app.processEvents()
    assert panel.collapsed_sections() == ["hidden", "traktor"]
    assert panel.sections["hidden"].is_collapsed()
    assert panel.sections["traktor"].is_collapsed()
    assert not panel.sections["vdj"].is_collapsed()

    panel.set_collapsed_sections([])         # restoring an empty set expands all
    app.processEvents()
    assert panel.collapsed_sections() == []
    assert panel.trees[VDJ].isVisible() and panel.hidden_tree.isVisible()


def test_every_section_can_be_collapsed_at_once(app, panel):
    n = len(panel.sections)
    panel.set_collapsed_sections(list(panel.sections))
    app.processEvents()
    # Each section shrinks to its header; the trailing filler pane holds the rest
    # of the height (so sum(splitter.sizes()) is still the full height).
    section_total = sum(panel.splitter.sizes()[:n])
    budget = 90 * n
    assert section_total < budget, \
        f"{n} title bars should be compact, got {section_total}px"


def test_all_collapsed_bars_stack_from_the_top(app, panel):
    """The bug: when every section collapses, Qt used to centre the title bars
    in the column. They must stack from the top instead, filler below."""
    panel.setFixedHeight(760)
    panel.splitter.setFixedHeight(760)
    app.processEvents()
    panel.set_collapsed_sections(list(panel.sections))
    app.processEvents()

    bars = [panel.sections[k] for k in panel.section_order()]
    assert bars[0].y() == 0, "top bar must sit at the very top, not float to centre"
    for above, below in zip(bars, bars[1:]):
        assert below.y() == pytest.approx(above.y() + above.height(), abs=6)
    # everything below the last bar is the filler
    assert panel._filler.height() > 760 - 90 * len(bars), \
        "filler must hold the surplus height"


# ----------------- magnet: anchor the top, section below rises ------------
# These address the layout rule by position -- top / middle / bottom-most
# expanded -- rather than by program name, because which program sits at the
# bottom is a registration detail and the rule is not about any of them.
def test_collapsing_a_middle_section_leaves_the_top_fixed(app, panel):
    order = panel.section_order()
    last = len(order) - 1                    # the filler follows the sections
    before = panel.splitter.sizes()

    panel.sections[order[1]].set_collapsed(True)
    app.processEvents()

    after = panel.splitter.sizes()
    assert after[0] == before[0], "the top section must not move"
    assert after[1] < 60, f"{order[1]} collapsed to its header"
    assert after[last] > before[last], \
        "the bottom-most expanded section rose to absorb the space"
    assert sum(after) == sum(before), "total height is conserved"


def test_the_bottom_section_collapses_into_itself(app, panel):
    """Reported: collapsing the bottom section made the one above it grow, which
    dragged the collapsed title bar all the way down to the bottom edge. It has
    to shrink in place instead, leaving the freed space beneath it."""
    order = panel.section_order()
    last = len(order) - 1
    before = panel.splitter.sizes()

    panel.sections[order[-1]].set_collapsed(True)
    app.processEvents()

    after = panel.splitter.sizes()
    assert after[0] == before[0], "the top section stays put"
    assert after[last] < 60, "the bottom section collapsed"
    assert after[last - 1] == before[last - 1], \
        "the section above it must NOT grow into the freed space"
    assert after[-1] > before[-1], "the freed space stays below, in the filler"


def test_the_collapsed_bottom_bar_stays_under_its_neighbour(app, panel):
    """The visible consequence: the title bar sits directly beneath the section
    above it, not floating at the bottom edge of the pane."""
    panel.setFixedHeight(760)
    panel.splitter.setFixedHeight(760)
    app.processEvents()
    order = panel.section_order()
    bottom = panel.sections[order[-1]]
    above = panel.sections[order[-2]]

    bottom.set_collapsed(True)
    app.processEvents()

    assert bottom.y() == pytest.approx(above.y() + above.height(), abs=6)
    assert bottom.y() + bottom.height() < 700, \
        "and well clear of the bottom edge"


def test_a_taller_window_still_grows_the_sections(app, panel):
    """Only the collapsed section's own space is withheld -- genuinely new height
    must still reach the bottom-most expanded section, or growing the window
    would just add blank space."""
    order = panel.section_order()
    panel.sections[order[-1]].set_collapsed(True)
    app.processEvents()
    before = panel.splitter.sizes()

    panel.splitter.setFixedHeight(panel.splitter.height() + 200)
    app.processEvents()

    after = panel.splitter.sizes()
    assert after[len(order) - 2] > before[len(order) - 2], \
        "the bottom-most expanded section takes the new height"


def test_upper_sections_never_grow_on_collapse(app, panel):
    """The bug being fixed: collapsing used to fatten the panes above and push
    the whole stack down. No section above the collapsed one may grow."""
    order = panel.section_order()
    before = panel.splitter.sizes()

    panel.sections[order[-1]].set_collapsed(True)
    app.processEvents()

    after = panel.splitter.sizes()
    for i in range(len(order) - 2):          # every section above the absorber
        assert after[i] <= before[i], f"{order[i]} must not grow"


def test_a_manual_drag_is_remembered_across_a_collapse(app, panel):
    order = panel.section_order()
    sizes = panel.splitter.sizes()
    floor = panel.splitter.widget(1).minimumSizeHint().height()
    delta = min(40, max(0, sizes[1] - floor))
    assert delta > 0, "no slack to drag with"
    sizes[0] += delta                        # drag the first handle down
    sizes[1] -= delta
    panel.splitter.setSizes(sizes)
    panel._remember_weights()                # what splitterMoved would do
    app.processEvents()
    dragged = panel.splitter.sizes()         # actual (Qt normalises a px or two)

    panel.sections[order[1]].set_collapsed(True)
    app.processEvents()
    panel.sections[order[1]].set_collapsed(False)
    app.processEvents()

    assert panel.splitter.sizes() == dragged, "drag sizes must return"


def test_a_bare_section_collapses_too(app):
    """Section is generic -- it must not depend on PlaylistPanel."""
    from PySide6.QtWidgets import QTreeWidget
    inner = QTreeWidget()
    section = Section("Title", inner)
    section.show()
    app.processEvents()
    section.set_collapsed(True)
    app.processEvents()
    assert not inner.isVisible() and section.maximumHeight() < 60
    section.close()


# --------------------- section-reorder lock + drag-to-swap ----------------
def _splitter_order(panel):
    """The sections' actual left-to-right (well, top-to-bottom) widget order
    in the splitter, excluding the trailing filler pane."""
    order = []
    for i in range(panel.splitter.count() - 1):
        widget = panel.splitter.widget(i)
        for key, section in panel.sections.items():
            if section is widget:
                order.append(key)
    return order


def test_sections_start_locked(panel):
    assert panel.sections_locked() is True
    assert all(s.locked for s in panel.sections.values())


def test_toggling_one_sections_lock_flips_all_three(panel):
    panel.sections["vdj"].lock_toggled.emit()
    assert panel.sections_locked() is False
    assert all(not s.locked for s in panel.sections.values())
    panel.sections["traktor"].lock_toggled.emit()
    assert panel.sections_locked() is True
    assert all(s.locked for s in panel.sections.values())


def test_drag_past_threshold_swaps_adjacent_order(panel):
    before = panel.section_order()
    panel._on_section_dragged("vdj", 25)
    after = panel.section_order()
    assert after != before
    assert after.index("vdj") == before.index("vdj") + 1
    # the splitter's real widget order must match the bookkeeping
    assert _splitter_order(panel) == after


def test_drag_below_threshold_does_nothing(panel):
    before = panel.section_order()
    panel._on_section_dragged("vdj", 5)
    panel._on_section_dragged("vdj", -5)
    assert panel.section_order() == before


def test_top_section_cannot_move_further_up(panel):
    top = panel.section_order()[0]
    panel._on_section_dragged(top, -25)
    assert panel.section_order()[0] == top


def test_bottom_section_cannot_move_further_down(panel):
    bottom = panel.section_order()[-1]
    panel._on_section_dragged(bottom, 25)
    assert panel.section_order()[-1] == bottom


def test_swap_preserves_collapsed_state_and_weights(panel):
    panel.sections["vdj"].set_collapsed(True)
    panel._weights["vdj"] = 999          # a distinctive remembered height
    panel._on_section_dragged("vdj", 25)
    assert panel.sections["vdj"].is_collapsed() is True
    assert panel._weights["vdj"] == 999


def test_section_order_round_trips(panel):
    reversed_order = list(reversed(panel.section_order()))
    panel.set_section_order(reversed_order)
    assert panel.section_order() == reversed_order
    assert _splitter_order(panel) == reversed_order


def test_set_section_order_tolerates_unknown_or_missing_keys(panel):
    """A stale saved order (extra/unknown or missing entries) must not crash --
    it degrades to keeping whatever isn't recognised in its prior place."""
    known = set(panel.sections)

    panel.set_section_order(["vdj", "nonexistent"])

    assert set(panel.section_order()) == known
    assert panel.section_order()[0] == "vdj", "what was recognised still moved"


# ------------------- drop-to-add: LibraryTree -> PlaylistPanel relay -------
def test_panel_relays_tracks_dropped_from_either_tree(panel):
    received = []
    panel.tracks_dropped.connect(lambda node, keys, src: received.append((node, keys, src)))
    panel.trees[VDJ].tracks_dropped.emit("NODE_A", ["k1"], "traktor")
    panel.trees[TRAKTOR].tracks_dropped.emit("NODE_B", ["k2", "k3"], "vdj")
    assert received == [("NODE_A", ["k1"], "traktor"), ("NODE_B", ["k2", "k3"], "vdj")]


def test_library_tree_accepts_drag_drop_mode(panel):
    from PySide6.QtWidgets import QAbstractItemView
    assert panel.trees[VDJ].dragDropMode() == QAbstractItemView.DragDrop
    assert panel.trees[TRAKTOR].dragDropMode() == QAbstractItemView.DragDrop


# ------------------- highlight_node: mirror the focused tab ----------------
def _mini_root():
    from trackcleaner.model.library import Playlist, TreeNode, TRAKTOR
    pl = TreeNode("List A", "playlist", TRAKTOR, node_path=("List A",),
                  playlist=Playlist("List A", TRAKTOR, track_keys=["k0"]))
    root = TreeNode("Traktor Playlists", "folder", TRAKTOR, children=[pl])
    return root, pl


def test_highlight_node_selects_without_emitting_playlists_selected(app):
    from PySide6.QtCore import Qt
    from trackcleaner.ui.playlist_trees import LibraryTree
    from trackcleaner.model.library import TRAKTOR
    tree = LibraryTree(TRAKTOR)
    root, pl = _mini_root()
    tree.load(root)
    fired = []
    tree.playlists_selected.connect(lambda nodes, src: fired.append((nodes, src)))

    tree.highlight_node(pl)

    assert fired == [], "highlighting must not reload the shown playlist"
    current = tree.currentItem()
    assert current is not None and current.data(0, Qt.UserRole) is pl
    assert current.isSelected()


def test_right_click_does_not_change_selection_or_load(app):
    """A right-click must not select (which would fire playlists_selected and
    reload the focused tab) -- the context menu targets the node under the
    cursor instead."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from trackcleaner.ui.playlist_trees import LibraryTree
    from trackcleaner.model.library import TRAKTOR
    tree = LibraryTree(TRAKTOR)
    root, pl = _mini_root()
    tree.load(root)
    fired = []
    tree.playlists_selected.connect(lambda nodes, src: fired.append(1))

    ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(5, 5),
                     Qt.RightButton, Qt.RightButton, Qt.NoModifier)
    tree.mousePressEvent(ev)

    assert fired == [], "right-click must not emit playlists_selected"
    assert tree.selectedItems() == [], "right-click must not select anything"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
