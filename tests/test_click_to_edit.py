"""Click-to-edit, driven by real mouse events.

**These deliver actual clicks with QTest rather than calling
``should_edit_on_click``**, following `test_key_routing.py`'s reason for doing
the same with keys: the whole feature is *when* the check runs relative to Qt's
own handling, and a direct call chooses that ordering for itself.

That is not hypothetical. The shipped version tested the live selection from the
``clicked`` signal — which Qt emits on **release**, after the press has already
selected the row — so the "row was already selected" condition was true for every
click and the first click on a row opened an editor. The direct-call tests
agreed with it, because they set the selection by hand.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                                           # noqa: E402
from PySide6.QtTest import QTest                                        # noqa: E402
from PySide6.QtWidgets import QApplication                              # noqa: E402

from trackcleaner.model.library import Track                            # noqa: E402
from trackcleaner.model import status as stmod                          # noqa: E402
from trackcleaner.ui.fields import ALL_COLUMN_IDS                       # noqa: E402
from trackcleaner.ui.track_table import TrackTable                      # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _tracks(n=4):
    return [Track(key=f"k{i}", source="traktor", path=f"D:/Music/{i}.mp3",
                  title=f"Title {i}", artist=f"Artist {i}", album="Album",
                  genre="House") for i in range(n)]


@pytest.fixture
def table(app):
    t = TrackTable()
    t.set_rows([(tr, stmod.GREEN) for tr in _tracks()])
    t.resize(900, 400)
    t.show()
    # Deterministic: the tests are about the wait, not about how long it is.
    t.set_double_click_ms(80)
    yield t
    t.close()


def _cell(table, row, col_id):
    return table._proxy.index(row, ALL_COLUMN_IDS.index(col_id))


def _click(table, row, col_id, double=False):
    """A real click at the centre of a cell."""
    pos = table.visualRect(_cell(table, row, col_id)).center()
    if double:
        QTest.mouseDClick(table.viewport(), Qt.LeftButton, Qt.NoModifier, pos)
    else:
        QTest.mouseClick(table.viewport(), Qt.LeftButton, Qt.NoModifier, pos)


def _settle(table):
    """Let the deferral elapse."""
    QTest.qWait(table.double_click_ms() + 60)


# ===================== which click starts an edit =========================
def test_the_first_click_on_a_row_selects_without_editing(table):
    """The reported bug, stated as a test: clicking a track must not immediately
    put its Title into an editor."""
    _click(table, 1, "title")

    assert 1 in table.selected_visual_rows()
    _settle(table)
    assert table.state() != TrackTable.EditingState


def test_a_second_click_on_the_same_field_edits(table):
    _click(table, 1, "title")
    _click(table, 1, "title")

    _settle(table)
    assert table.state() == TrackTable.EditingState


def test_clicking_a_different_field_of_a_selected_row_edits(table):
    """The row is what has to be already selected, not the cell -- otherwise you
    would need two clicks per field."""
    _click(table, 1, "title")
    _click(table, 1, "artist")

    _settle(table)
    assert table.state() == TrackTable.EditingState


def test_moving_to_another_row_selects_without_editing(table):
    """Second click, but on a different row: that is navigation."""
    _click(table, 1, "title")
    _click(table, 3, "title")

    _settle(table)
    assert table.state() != TrackTable.EditingState
    assert 3 in table.selected_visual_rows()


def test_a_non_editable_column_never_edits(table):
    _click(table, 1, "bpm")
    _click(table, 1, "bpm")

    _settle(table)
    assert table.state() != TrackTable.EditingState


def test_an_unfocused_pane_takes_two_clicks_to_edit(table):
    """The click that brings a split pane forward must not open an editor."""
    table.set_pane_active(False)

    _click(table, 1, "title")
    _click(table, 1, "title")

    _settle(table)
    # The first click only activated the pane, so this is the "first" real click.
    assert table.state() != TrackTable.EditingState


# ===================== the double-click window ============================
def test_a_double_click_on_a_selected_field_does_not_edit(table):
    """The second requirement: a double-click means play, so the edit that the
    first click would have started has to be called off."""
    _click(table, 1, "title")           # select
    _click(table, 1, "title", double=True)

    _settle(table)
    assert table.state() != TrackTable.EditingState


def test_a_double_click_still_reports_a_double_click(table):
    """Cancelling the edit must not swallow the gesture itself."""
    seen = []
    table.track_double_clicked.connect(seen.append)
    _click(table, 1, "title")

    _click(table, 1, "title", double=True)

    assert seen == ["k1"]


def test_the_edit_waits_rather_than_opening_at_once(table):
    """If it opened immediately there would be no window in which to cancel it,
    which is the whole mechanism."""
    _click(table, 1, "title")

    _click(table, 1, "title")

    assert table.has_pending_edit()
    assert table.state() != TrackTable.EditingState


def test_the_wait_is_the_configured_interval(table):
    table.set_double_click_ms(400)

    assert table.double_click_ms() == 400


def test_an_unset_interval_follows_qt(table):
    """So the deferral and Qt's own double-click detection cannot disagree by
    default -- a shorter wait would let a double-click land after the editor
    opened."""
    table._double_click_ms = None

    assert table.double_click_ms() == QApplication.doubleClickInterval()


# ===================== cancelling a pending edit ==========================
def test_repopulating_cancels_a_pending_edit(table):
    """A refresh replaces every row; the pending one names a cell that is gone."""
    _click(table, 1, "title")
    _click(table, 1, "title")

    table.set_rows([(tr, stmod.GREEN) for tr in _tracks(2)])

    _settle(table)
    assert not table.has_pending_edit()
    assert table.state() != TrackTable.EditingState


def test_losing_focus_cancels_a_pending_edit(table):
    """The focus event is posted rather than provoked with ``clearFocus``: the
    offscreen platform has no window manager, so no widget here ever holds focus
    and clearing it is a no-op. This is the same event Qt would deliver."""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QFocusEvent
    _click(table, 1, "title")
    _click(table, 1, "title")
    assert table.has_pending_edit()

    QApplication.sendEvent(table, QFocusEvent(QEvent.FocusOut))

    assert not table.has_pending_edit()


def test_clicking_elsewhere_cancels_a_pending_edit(table):
    _click(table, 1, "title")
    _click(table, 1, "title")
    assert table.has_pending_edit()

    _click(table, 3, "title")

    _settle(table)
    assert table.state() != TrackTable.EditingState


def test_cancelling_twice_is_harmless(table):
    table.cancel_pending_edit()
    table.cancel_pending_edit()

    assert not table.has_pending_edit()


def test_a_pending_edit_on_a_vanished_row_opens_nothing(table):
    """Belt and braces on the persistent index: even if the cancel were missed,
    a stale index must not open an editor on whatever slid into its place."""
    _click(table, 1, "title")
    _click(table, 1, "title")
    table.set_rows([(tr, stmod.GREEN) for tr in _tracks(2)])

    table._open_pending_edit()          # as the timer would have

    assert table.state() != TrackTable.EditingState


# ===================== the click still does its old job ===================
def test_a_single_click_still_reports_the_track(table):
    """Loading the waveform must stay immediate -- only the edit is deferred."""
    seen = []
    table.track_clicked.connect(seen.append)

    _click(table, 2, "title")

    assert seen == ["k2"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
