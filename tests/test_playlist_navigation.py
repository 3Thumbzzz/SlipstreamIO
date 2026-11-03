"""Left/Right arrow navigation through the playlist pane.

The arrows step the sidebar one visible row at a time. On a **folder** the step
waits out the double-tap window: a second tap of the same arrow inside it expands
(Right) or collapses (Left) instead, and the cursor never leaves the folder.

That wait is the fix for a gesture that could not work as built — the first tap
stepped immediately, so the second landed on whatever row it had moved to and
expanded that. Rows that are not folders still step with no delay.

The timing rule itself is a plain function of (key, timestamp), so the window is
stated outright here rather than slept through.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                                          # noqa: E402
from PySide6.QtWidgets import QApplication                             # noqa: E402

from trackcleaner.config.paths import Settings                         # noqa: E402
from trackcleaner.io import backup as backup_mod                       # noqa: E402
from trackcleaner.model.library import TRAKTOR, VDJ                    # noqa: E402
from trackcleaner.ui.tap_detect import DOUBLE_TAP_MS, DoubleTapDetector  # noqa: E402
from fixtures import write_sample                                      # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# ============================== the timing rule =============================
def test_two_quick_presses_of_the_same_key_are_a_double_tap():
    d = DoubleTapDetector()

    assert d.tap("right", 1000.0) is False
    assert d.tap("right", 1000.0 + DOUBLE_TAP_MS - 1) is True


def test_a_slow_second_press_is_two_singles():
    d = DoubleTapDetector()

    assert d.tap("right", 1000.0) is False
    assert d.tap("right", 1000.0 + DOUBLE_TAP_MS + 1) is False


def test_the_boundary_counts_as_a_double_tap():
    """Stated explicitly so the comparison cannot drift between <= and <."""
    d = DoubleTapDetector()
    d.tap("right", 1000.0)

    assert d.tap("right", 1000.0 + DOUBLE_TAP_MS) is True


def test_alternating_keys_never_double_tap():
    """Stepping quickly back and forth must not expand folders at random."""
    d = DoubleTapDetector()

    assert d.tap("right", 1000.0) is False
    assert d.tap("left", 1010.0) is False
    assert d.tap("right", 1020.0) is False


def test_three_fast_presses_are_one_double_then_a_single():
    """A completed gesture consumes its history, so taps do not overlap into a
    second double the user never made."""
    d = DoubleTapDetector()

    assert d.tap("right", 1000.0) is False
    assert d.tap("right", 1050.0) is True
    assert d.tap("right", 1100.0) is False


def test_reset_breaks_a_part_formed_gesture():
    """Focus leaving and coming back must not pair a tap from before with one
    from after."""
    d = DoubleTapDetector()
    d.tap("right", 1000.0)

    d.reset()

    assert d.tap("right", 1050.0) is False


# ============================ stepping the tree =============================
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


def _tree(window):
    return window.playlist_panel.trees[TRAKTOR]


def _current_name(tree):
    item = tree.currentItem()
    return item.text(0) if item is not None else None


def _folder(tree):
    return next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                if tree.topLevelItem(i).childCount())


def _leaf(tree):
    leaf = next((tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                 if not tree.topLevelItem(i).childCount()), None)
    assert leaf is not None, "fixture needs a top-level playlist"
    return leaf


def _elapse(window):
    """Let a pending folder step fire, without sleeping."""
    window._flush_pending_step()


# ---- a leaf steps at once -------------------------------------------------
def test_stepping_forward_moves_down_one_visible_row(window):
    tree = _tree(window)
    tree.setCurrentItem(_leaf(tree))
    first = _current_name(tree)

    window.step_playlist(1)

    assert _current_name(tree) != first


def test_a_leaf_steps_with_no_wait(window):
    """Only folders have a gesture to wait for; making every press pay the delay
    would make the arrows feel broken."""
    tree = _tree(window)
    tree.setCurrentItem(_leaf(tree))
    before = _current_name(tree)

    window.step_playlist(1)

    assert _current_name(tree) != before
    assert window._pending_step is None


def test_stepping_back_returns_to_where_it_started(window):
    tree = _tree(window)
    tree.setCurrentItem(_leaf(tree))
    first = _current_name(tree)

    window.step_playlist(1)
    window.step_playlist(-1)

    assert _current_name(tree) == first


def test_stepping_stops_at_the_top(window):
    """Wrapping the sidebar would jump the view across the whole library on a
    stray keypress, which is a worse outcome than doing nothing."""
    tree = _tree(window)
    tree.setCurrentItem(tree.topLevelItem(0))
    first = _current_name(tree)

    window.step_playlist(-1)
    _elapse(window)

    assert _current_name(tree) == first


def test_a_collapsed_folders_children_are_skipped(window):
    """itemBelow walks what is on screen, so nothing has to track expansion."""
    tree = _tree(window)
    folder = _folder(tree)
    folder.setExpanded(False)
    tree.setCurrentItem(folder)

    window.step_playlist(1)
    _elapse(window)

    assert _current_name(tree) != folder.child(0).text(0)


def test_stepping_before_anything_is_selected_starts_at_the_top(window):
    tree = _tree(window)
    tree.setCurrentItem(None)

    window.step_playlist(1)
    _elapse(window)

    assert tree.currentItem() is not None


# ---- a folder waits, and the second tap expands ---------------------------
def test_a_first_tap_on_a_folder_does_not_move_yet(window):
    """The bug, stated as a test: the cursor moving here is what made the second
    tap land somewhere else."""
    tree = _tree(window)
    folder = _folder(tree)
    folder.setExpanded(False)
    tree.setCurrentItem(folder)

    window.step_playlist(1)

    assert _current_name(tree) == folder.text(0)
    assert not folder.isExpanded()


def test_a_second_tap_expands_without_ever_moving(window):
    tree = _tree(window)
    folder = _folder(tree)
    folder.setExpanded(False)
    tree.setCurrentItem(folder)

    window.step_playlist(1)
    window.step_playlist(1)

    assert folder.isExpanded()
    assert _current_name(tree) == folder.text(0), "expanding must not also step"


def test_a_second_tap_left_collapses(window):
    tree = _tree(window)
    folder = _folder(tree)
    folder.setExpanded(True)
    tree.setCurrentItem(folder)

    window.step_playlist(-1)
    window.step_playlist(-1)

    assert not folder.isExpanded()
    assert _current_name(tree) == folder.text(0)


def test_one_tap_alone_steps_when_the_window_elapses(window):
    """The wait is not a cancellation: a single tap on a folder still navigates,
    just a moment later."""
    tree = _tree(window)
    folder = _folder(tree)
    folder.setExpanded(False)
    tree.setCurrentItem(folder)

    window.step_playlist(1)
    _elapse(window)

    assert _current_name(tree) != folder.text(0)
    assert not folder.isExpanded()


def test_the_opposite_arrow_cancels_a_pending_step(window):
    """You changed direction, so the tap being cancelled never happened -- it
    must not also fire once the timer runs out."""
    tree = _tree(window)
    folder = _folder(tree)
    folder.setExpanded(False)
    tree.setCurrentItem(folder)

    window.step_playlist(1)
    window.step_playlist(-1)

    assert not folder.isExpanded(), "opposite arrows are not a double tap"


def test_the_pending_step_is_dropped_not_deferred(window):
    """Belt and braces on the above: the abandoned direction must not still be
    sitting in the timer waiting to fire."""
    tree = _tree(window)
    folder = _folder(tree)
    tree.setCurrentItem(folder)

    window.step_playlist(1)
    window.step_playlist(-1)

    assert window._pending_step in (None, -1)


# ---- which tree the arrows walk -------------------------------------------
def test_the_arrows_walk_the_focused_tree(window):
    """Clicking a VirtualDJ playlist and pressing Right has to walk the VirtualDJ
    tree, not whichever program the focused tab happens to be showing."""
    window._current_source = TRAKTOR
    vdj_tree = window.playlist_panel.trees[VDJ]
    vdj_tree.setFocus()
    from PySide6.QtWidgets import QApplication as _QA
    if _QA.focusWidget() is not vdj_tree:
        pytest.skip("offscreen platform will not move focus")

    assert window._arrow_tree() is vdj_tree


def test_without_sidebar_focus_it_walks_the_current_source(window):
    window._current_source = TRAKTOR

    assert window._arrow_tree() is _tree(window)


