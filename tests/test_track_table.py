"""Track table: item-drag model, selection, lock, and visual ordering."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData                                   # noqa: E402
from PySide6.QtWidgets import QAbstractItemView, QApplication          # noqa: E402

from trackcleaner.model.library import Track, VDJ                      # noqa: E402
from trackcleaner.ui import theme                                      # noqa: E402
from trackcleaner.ui.track_table import (TrackTable, TRACK_MIME,       # noqa: E402
                                         SOURCE_MIME)


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    theme.capture_defaults(a)
    return a


def _rows(n):
    return [(Track(key=f"k{i}", path=f"D:/m/{i}.mp3", source=VDJ,
                   title=f"T{i}", artist="A", bpm=float(120 + i)), "black")
            for i in range(n)]


def test_drag_is_enabled_as_item_drag(app):
    t = TrackTable()
    assert t.dragEnabled()
    assert t.dragDropMode() == QAbstractItemView.DragDrop
    assert t.selectionMode() == QAbstractItemView.ExtendedSelection


def test_visual_keys_follow_the_sort(app):
    t = TrackTable()
    t.set_rows(_rows(3))
    assert t.visual_keys() == ["k0", "k1", "k2"]
    # sort by BPM descending: reverse order
    t.sortByColumn(_bpm_col(), _desc())
    assert t.visual_keys() == ["k2", "k1", "k0"]


def test_selected_keys_are_in_visual_order(app):
    t = TrackTable()
    t.set_rows(_rows(4))
    t.selectRow(2)
    t.selectionModel().select(
        t.model().index(0, 0),
        __import__("PySide6.QtCore", fromlist=["QItemSelectionModel"]).QItemSelectionModel.Select
        | __import__("PySide6.QtCore", fromlist=["QItemSelectionModel"]).QItemSelectionModel.Rows)
    assert t.selected_keys() == ["k0", "k2"]


def test_mime_type_constant_is_exposed(app):
    assert TRACK_MIME == "application/x-slipstream-tracks"


def test_context_is_recorded(app):
    t = TrackTable()
    t.set_context("traktor", ["node"])
    assert t.source == "traktor" and t.nodes == ["node"]


def _bpm_col():
    from trackcleaner.ui.fields import ALL_COLUMN_IDS
    return ALL_COLUMN_IDS.index("bpm")


def _desc():
    from PySide6.QtCore import Qt
    return Qt.DescendingOrder


# --------------------------- drop-to-add parsing ---------------------------
# dropEvent's MIME decoding is pulled into the static _parse_drop precisely so
# it can be tested against a real QMimeData without needing a QDropEvent --
# constructing one by hand segfaults this PySide6 build outright (verified).
def _mime(keys, source=None):
    m = QMimeData()
    m.setData(TRACK_MIME, "\n".join(keys).encode("utf-8"))
    if source is not None:
        m.setData(SOURCE_MIME, source.encode("utf-8"))
    return m


def test_parse_drop_returns_keys_and_source(app):
    result = TrackTable._parse_drop(_mime(["k1", "k2"], "traktor"), is_self=False)
    assert result == (["k1", "k2"], "traktor")


def test_parse_drop_self_drag_is_ignored(app):
    """There is no row-reorder feature (see playlist_trees.Section for where
    the real lock/reorder gesture lives) -- a self-drop is simply a no-op."""
    assert TrackTable._parse_drop(_mime(["k1"], "traktor"), is_self=True) is None


def test_parse_drop_empty_keys_is_none(app):
    assert TrackTable._parse_drop(_mime([]), is_self=False) is None


def test_parse_drop_missing_source_mime_defaults_to_empty_string(app):
    assert TrackTable._parse_drop(_mime(["k1"]), is_self=False) == (["k1"], "")


# ------------------------- external file (Explorer) drop -------------------
def test_parse_urls_keeps_only_local_audio_files(app):
    from PySide6.QtCore import QUrl
    m = QMimeData()
    m.setUrls([QUrl.fromLocalFile(r"D:\Music\a.mp3"),
               QUrl.fromLocalFile(r"D:\Music\readme.txt"),
               QUrl.fromLocalFile(r"D:\Music\b.FLAC"),
               QUrl("https://example.com/remote.mp3")])
    got = TrackTable._parse_urls(m)
    # Qt's toLocalFile() normalises separators to forward slashes.
    assert got == ["D:/Music/a.mp3", "D:/Music/b.FLAC"]


def test_parse_urls_without_urls_is_empty(app):
    assert TrackTable._parse_urls(_mime(["k1"])) == []


# ------------------------- separator row (collection search) ---------------
def test_separator_row_is_inert_and_suppresses_sort(app):
    t = TrackTable()
    t.set_rows([(Track(key="k0", path="a", source=VDJ, title="A"), "black"),
                (None, "── other ──"),
                (Track(key="k1", path="b", source=VDJ, title="B"), "black")])
    assert t.model().rowCount() == 3
    assert t.visual_keys() == ["k0", "k1"], "the separator carries no key"
    assert t.isSortingEnabled() is False, "grouped view suppresses column sort"

    # a normal (separator-free) populate restores sorting
    t.set_rows([(Track(key="k2", path="c", source=VDJ, title="C"), "black")])
    assert t.isSortingEnabled() is True
    assert t.visual_keys() == ["k2"]


# ----------------------------- interaction fixes ---------------------------
def test_auto_scroll_is_off(app):
    """Clicking a cell must not re-centre the horizontal scrollbar."""
    assert TrackTable().hasAutoScroll() is False


def test_alt_wheel_emits_font_changed_and_does_not_resize_locally(app):
    """Zoom is global now: the table reports the requested size instead of
    resizing only itself, so split panes stay in lock-step."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QWheelEvent
    t = TrackTable()
    before = t.font_point_size()
    seen = []
    t.font_changed.connect(seen.append)
    ev = QWheelEvent(QPoint(5, 5), t.mapToGlobal(QPoint(5, 5)), QPoint(0, 0),
                     QPoint(0, 120), Qt.NoButton, Qt.AltModifier,
                     Qt.NoScrollPhase, False)
    t.wheelEvent(ev)
    assert seen == [before + 1]
    assert t.font_point_size() == before, "table must not resize itself"


# --------------------- positional insert (native order) --------------------
class _Node:
    def __init__(self, keys):
        self.node_path = ("PL",)
        self.playlist = type("P", (), {"track_keys": list(keys)})()


def test_native_order_true_when_view_matches_playlist(app):
    t = TrackTable()
    t.set_rows(_rows(3))
    t.set_context(VDJ, [_Node(["k0", "k1", "k2"])])
    assert t._in_native_order() is True


def test_native_order_false_when_sorted_differently(app):
    from PySide6.QtCore import Qt
    from trackcleaner.ui.fields import ALL_COLUMN_IDS
    t = TrackTable()
    t.set_rows(_rows(3))
    t.set_context(VDJ, [_Node(["k0", "k1", "k2"])])
    t.sortByColumn(ALL_COLUMN_IDS.index("bpm"), Qt.DescendingOrder)   # k2,k1,k0
    assert t._in_native_order() is False


def test_native_order_false_for_multi_node_view(app):
    t = TrackTable()
    t.set_rows(_rows(2))
    t.set_context(VDJ, [_Node(["k0"]), _Node(["k1"])])
    assert t._in_native_order() is False


def test_native_order_true_despite_a_duplicate_in_the_playlist(app):
    """The reported bug: a playlist with a repeated track dedupes in the view, so
    an exact visual==track_keys compare wrongly rejected it and every drop
    appended. A subsequence test must still see it as native."""
    t = TrackTable()
    t.set_rows(_rows(2))                                   # displays k0, k1
    t.set_context(VDJ, [_Node(["k0", "k1", "k0"])])       # k0 repeated in storage
    assert t._in_native_order() is True


def test_native_order_true_despite_a_missing_track(app):
    """A track absent from the library is skipped in the view but still sits in
    track_keys; native order must survive that gap too."""
    t = TrackTable()
    t.set_rows(_rows(2))                                   # displays k0, k1
    t.set_context(VDJ, [_Node(["k0", "kGHOST", "k1"])])   # kGHOST not displayed
    assert t._in_native_order() is True


def test_native_order_false_while_filtered(app):
    """A filtered view can't map a visual row to a stored position."""
    t = TrackTable()
    t.set_rows(_rows(3))
    t.set_context(VDJ, [_Node(["k0", "k1", "k2"])])
    t.set_filter("T")                                     # matches every title T0..T2
    assert t._in_native_order() is False


def test_drop_index_below_last_row_appends_at_track_keys_length(app):
    """Dropping past the last visible row appends -- and the index is the length
    of track_keys (which includes the deduped duplicate), not the row count."""
    from PySide6.QtCore import QPoint
    t = TrackTable()
    t.resize(400, 200)
    t.set_rows(_rows(2))                                   # 2 visible rows
    t.set_context(VDJ, [_Node(["k0", "k1", "k0"])])       # len(track_keys) == 3
    assert t._drop_index(QPoint(10, 100000)) == 3         # append at stored length


def test_selected_visual_rows_are_ascending_positions(app):
    from PySide6.QtCore import QItemSelectionModel
    t = TrackTable()
    t.set_rows(_rows(4))
    sm = t.selectionModel()
    for r in (2, 0):
        sm.select(t.model().index(r, 0),
                  QItemSelectionModel.Select | QItemSelectionModel.Rows)
    assert t.selected_visual_rows() == [0, 2]


def test_drop_index_is_minus_one_when_not_native(app):
    from PySide6.QtCore import QPoint, Qt
    from trackcleaner.ui.fields import ALL_COLUMN_IDS
    t = TrackTable()
    t.resize(400, 200)
    t.set_rows(_rows(3))
    t.set_context(VDJ, [_Node(["k0", "k1", "k2"])])
    t.sortByColumn(ALL_COLUMN_IDS.index("bpm"), Qt.DescendingOrder)
    assert t._drop_index(QPoint(10, 5)) == -1


# ============================ model reset hygiene ============================
def _capture_qt_messages(fn):
    """Run ``fn`` and return everything Qt logged.

    Scoped to one call rather than installed suite-wide: a global handler that
    failed on any Qt warning would be a much bigger change with unrelated
    fallout. Restored in a finally, or a failure here would swallow Qt output
    for the rest of the session.
    """
    from PySide6.QtCore import qInstallMessageHandler
    seen = []

    def handler(_mode, _ctx, message):
        seen.append(message)

    previous = qInstallMessageHandler(handler)
    try:
        fn()
    finally:
        qInstallMessageHandler(previous)
    return seen


def test_set_rows_does_not_nest_model_resets(app):
    """QStandardItemModel.clear() performs its own begin/endResetModel, so
    wrapping it in another pair nested two resets -- four Qt warnings on every
    single playlist switch, and an outer reset whose end was unbalanced."""
    t = TrackTable()
    t.set_rows(_rows(5))

    messages = _capture_qt_messages(lambda: t.set_rows(_rows(3)))

    offenders = [m for m in messages if "ResetModel" in m]
    assert not offenders, "nested model reset: " + " | ".join(offenders)


def test_switching_to_a_shorter_playlist_leaves_no_stale_rows(app):
    t = TrackTable()
    t.set_rows(_rows(6))
    t.set_rows(_rows(2))

    assert t._model.rowCount() == 2
    assert t._proxy.rowCount() == 2


def test_headers_survive_a_rebuild(app):
    from PySide6.QtCore import Qt
    from trackcleaner.ui.fields import ALL_COLUMN_IDS, LABEL_BY_ID
    t = TrackTable()
    t.set_rows(_rows(4))
    t.set_rows(_rows(1))

    assert t._model.columnCount() == len(ALL_COLUMN_IDS)
    assert t._model.headerData(0, Qt.Horizontal) == LABEL_BY_ID[ALL_COLUMN_IDS[0]]


def test_separator_rows_and_spans_survive_a_rebuild(app):
    """The search view's grouped results depend on these."""
    t = TrackTable()
    rows = _rows(2) + [(None, "-- separator --")] + _rows(2)
    t.set_rows(rows)

    assert t._model.rowCount() == 5
    sep_row = 2
    assert t.columnSpan(sep_row, 0) == t._model.columnCount()


# ============================== Alt+scroll zoom ==============================
def test_zoom_step_reads_whichever_axis_carries_the_delta(app):
    """The bug: with Alt held, Qt on Windows delivers the wheel as a HORIZONTAL
    event, so the delta is in x() and y() is 0. Reading y() alone made `0 > 0`
    false every time -- the font could only ever shrink."""
    from PySide6.QtCore import QPoint
    assert TrackTable._zoom_step(QPoint(0, 120)) == 1
    assert TrackTable._zoom_step(QPoint(0, -120)) == -1
    assert TrackTable._zoom_step(QPoint(120, 0)) == 1, "the Alt case"
    assert TrackTable._zoom_step(QPoint(-120, 0)) == -1


def test_zoom_step_ignores_an_empty_delta(app):
    """Otherwise a stray zero-delta event silently shrinks the font."""
    from PySide6.QtCore import QPoint
    assert TrackTable._zoom_step(QPoint(0, 0)) == 0


def test_font_size_scales_columns_proportionally(app):
    t = TrackTable()
    t.set_rows(_rows(3))
    t.set_font_point_size(10)
    header = t.horizontalHeader()
    header.resizeSection(1, 100)
    header.resizeSection(2, 200)

    t.set_font_point_size(20)                       # double

    assert header.sectionSize(1) == pytest.approx(200, abs=2)
    assert header.sectionSize(2) == pytest.approx(400, abs=2)


def test_font_size_change_scales_row_height(app):
    t = TrackTable()
    t.set_rows(_rows(3))
    t.set_font_point_size(9)
    small = t.verticalHeader().defaultSectionSize()

    t.set_font_point_size(18)

    assert t.verticalHeader().defaultSectionSize() > small


def test_setting_the_same_font_size_leaves_columns_alone(app):
    """Every new tab is initialised at the current size; that must not rescale
    the columns it inherited."""
    t = TrackTable()
    t.set_rows(_rows(3))
    t.set_font_point_size(12)
    t.horizontalHeader().resizeSection(1, 137)

    t.set_font_point_size(12)

    assert t.horizontalHeader().sectionSize(1) == 137


def test_font_size_is_clamped(app):
    t = TrackTable()
    t.set_font_point_size(999)
    assert t.font_point_size() == 28
    t.set_font_point_size(1)
    assert t.font_point_size() == 6


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
