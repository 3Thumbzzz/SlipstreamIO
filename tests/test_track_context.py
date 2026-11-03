"""The track right-click menu.

Two things: the new "Go to file location", and that a **sync-preview row** must
not offer removal. A deep-green ghost is a track the playlist does not contain
yet, shown so you can see what a sync will bring in -- "Remove from playlist" on
it is meaningless and "Delete file" would act on a file the playlist never
claimed.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                             # noqa: E402

from trackcleaner.io import reveal                                     # noqa: E402
from trackcleaner.ui import context_menus                              # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _CB:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _menu(**overrides):
    cb = dict(sync_track=lambda t: None, edit=lambda: None,
              show_playlists=lambda: None,
              check_integrity=lambda: None, reveal=lambda: None,
              remove_from_playlist=lambda: None, remove_with_entry=lambda: None,
              delete_file=lambda: None, sync_targets=[("traktor", "Traktor")],
              collection_label="database.xml")
    cb.update(overrides)
    return context_menus.track_menu(None, "vdj", _CB(**cb))


def _labels(menu):
    return [("---" if a.isSeparator() else a.text()) for a in menu.actions()]


REMOVALS = ("Remove from playlist", "Remove from playlist + database.xml entry…",
            "Delete file (Recycle Bin)…")


# ========================== go to file location =============================
def test_the_menu_offers_go_to_file_location(app):
    assert "Go to file location" in _labels(_menu())


def test_it_is_disabled_when_the_file_is_not_there(app):
    """Better than opening an Explorer window on nothing and leaving the user to
    work out why it looks wrong."""
    menu = _menu(can_reveal=False)

    act = next(a for a in menu.actions() if a.text() == "Go to file location")
    assert not act.isEnabled()


def test_it_is_enabled_when_the_file_exists(app):
    menu = _menu(can_reveal=True)

    act = next(a for a in menu.actions() if a.text() == "Go to file location")
    assert act.isEnabled()


def test_triggering_it_calls_back(app):
    called = []
    menu = _menu(reveal=lambda: called.append(True))

    next(a for a in menu.actions() if a.text() == "Go to file location").trigger()

    assert called == [True]


# ------------------------------- the launcher -------------------------------
def test_revealing_a_missing_file_reports_failure(tmp_path):
    """It must not launch anything for a path that is not there."""
    assert reveal.reveal_in_file_browser(str(tmp_path / "nope.mp3")) is False


def test_revealing_nothing_is_not_an_error(tmp_path):
    assert reveal.reveal_in_file_browser("") is False


def test_the_command_line_quotes_the_path_not_the_switch():
    """Explorer wants `/select,` unquoted with only the path quoted. Quoting the
    whole token makes it ignore the argument and open Documents.

    Asserted on a path **with a space**, because that is the only case that
    distinguishes the two forms -- a space-free path works either way, which is
    exactly why the first version of this test passed against broken code.
    """
    cmd = reveal.select_command(r"D:\Music\02 Homenaje.mp3")

    assert cmd == 'explorer /select,"D:\\Music\\02 Homenaje.mp3"'
    assert not cmd.startswith('explorer "'), "the switch must not be inside quotes"


def test_a_path_without_spaces_is_built_the_same_way():
    """The fix must not be specific to the case that was failing."""
    cmd = reveal.select_command(r"D:\Music\NoSpaces.mp3")

    assert cmd == 'explorer /select,"D:\\Music\\NoSpaces.mp3"'


def test_it_is_not_passed_as_an_argv_list(tmp_path, monkeypatch):
    """A list would be re-quoted by subprocess.list2cmdline, wrapping the whole
    `/select,<path>` token as soon as the path contains a space -- which is the
    bug this replaced. Popen must receive the composed string."""
    if sys.platform != "win32":
        pytest.skip("Windows-only shell call")
    f = tmp_path / "my song.mp3"                 # a space, deliberately
    f.write_bytes(b"\0")
    seen = []
    monkeypatch.setattr(reveal.subprocess, "Popen", lambda cmd: seen.append(cmd))

    assert reveal.reveal_in_file_browser(str(f)) is True

    assert isinstance(seen[0], str), "an argv list would be re-quoted"
    assert seen[0] == f'explorer /select,"{os.path.normpath(str(f))}"'


# ======================== preview rows offer no removal =====================
def test_a_normal_row_offers_the_removal_entries(app):
    labels = _labels(_menu(is_tombstone=False))

    for entry in REMOVALS:
        assert entry in labels


def test_a_preview_row_offers_none_of_them(app):
    """Not disabled -- absent. The row is not a member, so the operations do not
    belong on its menu at all."""
    labels = _labels(_menu(is_tombstone=True))

    for entry in REMOVALS:
        assert entry not in labels


def test_a_preview_row_keeps_the_harmless_entries(app):
    """It is still a real track: you may want to see where its file is, or which
    playlists hold it."""
    labels = _labels(_menu(is_tombstone=True))

    assert "Playlists…" in labels
    assert "Check file integrity" in labels
    assert "Go to file location" in labels


def test_a_preview_row_menu_has_no_dangling_separator(app):
    """Dropping the last group must not leave the separator that introduced it."""
    labels = _labels(_menu(is_tombstone=True))

    assert labels[-1] != "---"



# =============== a preview row of a track another program added ===============
# Reported: "Sync Track -> VirtualDJ" on such a row in Traktor answered "Track not
# found in source library". Traktor has nothing to sync from; the track is only
# in VirtualDJ. The row now offers to bring it in instead.
def test_with_no_entry_here_sync_track_is_greyed_out(app):
    menu = _menu(source_has_entry=False)
    entry = next(a for a in menu.actions() if a.text().startswith("Sync Track"))

    assert not entry.isEnabled()
    assert "not in this program's collection" in entry.text()


def test_a_track_another_program_added_can_be_brought_in(app):
    brought = []
    menu = _menu(is_tombstone=True, source_has_entry=False,
                 bring_in_from=[("vdj", "VirtualDJ")], bring_in=brought.append)
    entry = next(a for a in menu.actions()
                 if a.text() == "Add to this playlist from VirtualDJ")

    entry.trigger()

    assert brought == ["vdj"]


def test_an_ordinary_row_offers_no_bring_in(app):
    assert not any(label.startswith("Add to this playlist")
                   for label in _labels(_menu()))


def test_bring_in_adds_it_to_the_playlist_and_the_collection(app, tmp_path, monkeypatch):
    """End to end on the sample library: a track only VirtualDJ's copy of a
    playlist holds shows as a preview row in Traktor's; bringing it in creates
    Traktor's entry and makes it a member."""
    from PySide6.QtWidgets import QMessageBox
    from trackcleaner.model.library import TRAKTOR, VDJ
    from trackcleaner.model.status import GAINING
    import test_sync_pane as pane_tests

    window = pane_tests._make_window(tmp_path, monkeypatch)
    try:
        vdj_node = pane_tests._synced_pair(window)
        # A track VirtualDJ alone has, added to its copy after the sync.
        from trackcleaner.model import matching
        from trackcleaner.model.library import Track
        audio = tmp_path / "only in vdj.mp3"
        audio.write_bytes(bytes(64))
        vdj_only = matching.path_key(str(audio))
        window.adapters[VDJ].create_entries_job([Track(
            key=vdj_only, path=str(audio), source=VDJ, title="Only In VDJ",
            artist="Nobody")])()
        window.load_libraries()
        vdj_node = next(n for n in window.libraries[VDJ].playlists()
                        if tuple(n.node_path) == tuple(vdj_node.node_path))
        window.membership.add_tracks(vdj_node, [vdj_only], VDJ)
        window._writer.flush()
        window.load_libraries()

        node = next(n for n in window.libraries[TRAKTOR].playlists()
                    if tuple(n.node_path) == tuple(vdj_node.node_path))
        window.playlist_panel.tree_for(TRAKTOR).select_nodes([node])
        table = window._table()
        row = next(r for r in range(table._proxy.rowCount())
                   if table.visual_key_at(r) == vdj_only)
        assert table.row_status(row) == GAINING

        cb = window._track_callbacks(vdj_only, TRAKTOR, row)
        assert cb.source_has_entry is False
        assert cb.bring_in_from == [(VDJ, window.adapters[VDJ].display_name)]

        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
        cb.bring_in(VDJ)
        window._writer.flush()
        window.load_libraries()

        traktor_lib = window.libraries[TRAKTOR]
        assert traktor_lib.tracks[vdj_only].in_database
        node = next(n for n in traktor_lib.playlists()
                    if tuple(n.node_path) == tuple(vdj_node.node_path))
        assert vdj_only in node.playlist.track_keys
    finally:
        window.close()

if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
