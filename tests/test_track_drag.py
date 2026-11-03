"""Dragging tracks in and out of the track table.

Two reported defects:

* dragging a file in from Explorer showed **no drop line**, so the insert point
  was real (``dropEvent`` already computed it) but invisible -- it read as
  "files always append";
* dragging a track **out** into another application showed the "no entry"
  cursor, because the drag offered only our private MIME types and nothing an
  outside program can read.

Note the non-``QDropEvent`` convention (HANDOFF.md, "Development conventions"):
constructing one by hand
segfaults this PySide6 build, so the logic is exercised through the static
``QMimeData`` seams and the table's own public methods.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QUrl                             # noqa: E402
from PySide6.QtWidgets import QApplication                             # noqa: E402

from trackcleaner.model import status as st                            # noqa: E402
from trackcleaner.model.library import (Playlist, Track, TreeNode,     # noqa: E402
                                        VDJ)
from trackcleaner.ui.track_table import (SOURCE_MIME, TRACK_MIME,      # noqa: E402
                                         TrackTable)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _track(key, path):
    return Track(key=key, path=path, source=VDJ, title=key)


@pytest.fixture
def real_files(tmp_path):
    """Two tracks whose files actually exist, and one that does not."""
    made = []
    for name in ("a.mp3", "b.mp3"):
        p = tmp_path / name
        p.write_bytes(b"\0")
        made.append(_track(name, str(p)))
    made.append(_track("gone.mp3", str(tmp_path / "gone.mp3")))
    return made


@pytest.fixture
def table(app, real_files):
    t = TrackTable()
    node = TreeNode(name="PL", kind="playlist", source=VDJ, node_path=("PL",),
                    playlist=Playlist(name="PL", source=VDJ,
                                      track_keys=[x.key for x in real_files]))
    t.set_rows([(x, st.GREEN) for x in real_files])
    t.set_context(VDJ, [node])
    return t


# ============================== dragging out ================================
def test_the_outgoing_drag_offers_the_files_themselves(table, real_files):
    """Without text/uri-list every other application refuses the drop, which is
    what showed the "no entry" cursor."""
    table.selectAll()

    urls = table._selected_urls([x.key for x in real_files])

    assert urls, "no file URLs offered"
    assert all(isinstance(u, QUrl) and u.isLocalFile() for u in urls)


def test_a_track_whose_file_is_missing_is_not_offered(table, real_files):
    """Handing out a path that does not resolve makes the receiving application
    report the error, which looks like our bug rather than a missing file."""
    urls = table._selected_urls([x.key for x in real_files])

    names = [os.path.basename(u.toLocalFile()) for u in urls]
    assert "gone.mp3" not in names
    assert sorted(names) == ["a.mp3", "b.mp3"]


def test_unknown_keys_are_skipped_rather_than_raising(table):
    assert table._selected_urls(["not-a-key"]) == []


# ------------- the internal path must not become a file import -------------
def test_an_internal_drag_is_still_recognised_as_a_track_drag():
    """The outgoing drag now carries BOTH our private format and file URLs. Every
    drop handler tests TRACK_MIME first, so a same-app drag must still take the
    track path -- if that ordering ever flips, dragging between two playlists
    silently becomes a re-import of the files."""
    mime = QMimeData()
    mime.setData(TRACK_MIME, b"k1\nk2")
    mime.setData(SOURCE_MIME, VDJ.encode())
    mime.setUrls([QUrl.fromLocalFile(r"D:\Music\A.mp3")])

    assert mime.hasFormat(TRACK_MIME), "the internal format must win"
    keys, source = TrackTable._parse_drop(mime, is_self=False)
    assert keys == ["k1", "k2"]
    assert source == VDJ


def test_a_plain_file_drop_still_parses_as_files():
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(r"D:\Music\A.mp3")])

    assert not mime.hasFormat(TRACK_MIME)
    # QUrl.toLocalFile normalises the separators; compare on that footing.
    got = [p.replace("\\", "/") for p in TrackTable._parse_urls(mime)]
    assert got == ["D:/Music/A.mp3"]


# ============================== dragging in =================================
def test_the_drop_line_is_available_for_file_drops(table):
    """A file drop lands at the drop position exactly like an internal drag, so
    it gets the same indicator. The line is only meaningful in native order --
    which this table is in."""
    assert table._in_native_order()

    table._show_drop_line(table.viewport().rect().topLeft())

    # isHidden(), not isVisible(): the table is never shown in an offscreen test,
    # and every child of an unshown widget reports not-visible regardless of its
    # own state. Same trap as the sidebar sections (see playlist_trees).
    assert not table._drop_line.isHidden()


def test_a_sorted_view_still_shows_no_drop_line(table):
    """Positional insert is undefined once rows no longer follow stored order,
    so the indicator must not promise one."""
    from PySide6.QtCore import Qt
    from trackcleaner.ui.fields import ALL_COLUMN_IDS
    table.sortByColumn(ALL_COLUMN_IDS.index("title"), Qt.DescendingOrder)

    table._show_drop_line(table.viewport().rect().topLeft())

    assert table._drop_line.isHidden()


# ---------------- preview rows do not break positional insert ---------------
def test_native_order_survives_sync_preview_rows(app, real_files):
    """A "gaining" ghost is not in track_keys, and the subsequence test used to
    fail on it -- disabling the drop line and positional insert on exactly the
    playlists that are out of sync."""
    t = TrackTable()
    node = TreeNode(name="PL", kind="playlist", source=VDJ, node_path=("PL",),
                    playlist=Playlist(name="PL", source=VDJ,
                                      track_keys=[real_files[0].key,
                                                  real_files[1].key]))
    ghost = _track("incoming.mp3", r"D:\Music\Incoming.mp3")
    t.set_rows([(real_files[0], st.GREEN), (real_files[1], st.GREEN),
                (ghost, "green2")])
    t.set_context(VDJ, [node])

    assert t._in_native_order(), "the ghost row must not disable positional insert"
    assert t.real_visual_keys() == [real_files[0].key, real_files[1].key]
    assert t.is_tombstone_row(2)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
