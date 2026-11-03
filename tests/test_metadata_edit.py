"""Editing a track's text metadata.

The multi-track rule carries the design: a field the selection disagrees on shows
`<various>`, and leaving it alone must leave every track at its **own** value.
The mechanism is that the dialog returns only the fields that changed, and the
adapters treat an absent field as "do not touch" — so the two halves are tested
separately and then together.

Writing tags at all is new: the writers previously rewrote only cues and
playcount on an existing entry, and set title/artist solely when creating one.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog                     # noqa: E402

from trackcleaner.config.paths import Settings                          # noqa: E402
from trackcleaner.io import backup as backup_mod                        # noqa: E402
from trackcleaner.io import traktor_read, traktor_write, vdj_read, vdj_write  # noqa: E402
from trackcleaner.model.cues import Poi                                 # noqa: E402
from trackcleaner.model.library import TRAKTOR, Track, VDJ, path_ci     # noqa: E402
from trackcleaner.ui.metadata_dialog import (VARIOUS, MetadataDialog,   # noqa: E402
                                             changed_fields, common_values)
from fixtures import DOO_RAG_SONG, EMPTY_VDJ_XML, SAMPLE_NML, write_sample  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _t(**kw):
    base = dict(key="k", path=r"D:\Music\A.mp3", source=VDJ, title="", artist="",
                album="", genre="", comment="")
    base.update(kw)
    return Track(**base)


FIELDS = ("title", "artist", "album", "genre", "comment")


# ============================ the <various> rule ============================
def test_a_shared_value_is_shown_as_itself():
    tracks = [_t(title="Same"), _t(title="Same")]

    assert common_values(tracks, ["title"])["title"] == "Same"


def test_a_differing_value_shows_various():
    tracks = [_t(title="One"), _t(title="Two")]

    assert common_values(tracks, ["title"])["title"] == VARIOUS


def test_one_track_never_shows_various():
    assert common_values([_t(title="Only")], ["title"])["title"] == "Only"


def test_a_field_left_as_various_is_not_written():
    """The point of the placeholder: those tracks keep their own values."""
    original = {"title": VARIOUS, "artist": "Shared"}

    assert changed_fields(original, {"title": VARIOUS, "artist": "Shared"}) == {}


def test_typing_over_various_writes_that_field_to_everything():
    original = {"title": VARIOUS, "artist": "Shared"}

    assert changed_fields(original, {"title": "Unified", "artist": "Shared"}) \
        == {"title": "Unified"}


def test_retyping_the_same_text_writes_nothing():
    assert changed_fields({"title": "Same"}, {"title": "Same"}) == {}


def test_clearing_a_field_is_a_change():
    """Blanking a tag is a real edit, not "left alone" -- otherwise a field could
    never be emptied."""
    assert changed_fields({"comment": "old"}, {"comment": ""}) == {"comment": ""}


# ============================== the dialog ==================================
def test_the_dialog_shows_various_for_a_differing_field(app):
    d = MetadataDialog([_t(title="One"), _t(title="Two")], FIELDS)

    assert d.edited_values()["title"] == VARIOUS


def test_applying_without_typing_writes_nothing(app):
    d = MetadataDialog([_t(title="One"), _t(title="Two")], FIELDS)

    assert d.result_fields() == {}


def test_typing_then_applying_returns_only_that_field(app):
    d = MetadataDialog([_t(title="One", artist="A"), _t(title="Two", artist="A")],
                       FIELDS)

    d._edits["title"].setText("Unified")

    assert d.result_fields() == {"title": "Unified"}


def test_cancel_is_a_reject(app):
    """Apply is the only thing that writes; Cancel and the window's X must be
    the same action, or closing the window would look like an escape while
    behaving like a commit."""
    d = MetadataDialog([_t()], FIELDS)
    d._edits["title"].setText("Typed")

    d.reject()

    assert d.result() == QDialog.Rejected


# =========================== the writers, per format ========================
def test_traktor_sets_every_text_field(tmp_path):
    paths = write_sample(tmp_path)
    lib = traktor_read.read(paths.nml)
    key = next(iter(lib.tracks))

    out, changed = traktor_write.set_entry_metadata(SAMPLE_NML, key, {
        "title": "New & Title", "artist": "A", "album": "Alb",
        "genre": "G", "comment": 'has "quotes"'})

    assert changed
    p = tmp_path / "edited.nml"
    p.write_text(out, encoding="utf-8")
    got = traktor_read.read(str(p)).tracks[key]
    assert (got.title, got.artist, got.album, got.genre, got.comment) == \
        ("New & Title", "A", "Alb", "G", 'has "quotes"')


def test_traktor_leaves_the_cues_alone(tmp_path):
    """Text surgery: the cue block beside the tags must survive untouched."""
    paths = write_sample(tmp_path)
    lib = traktor_read.read(paths.nml)
    key = next(k for k, t in lib.tracks.items() if t.pois)
    before = len(lib.tracks[key].pois)

    out, _ = traktor_write.set_entry_metadata(SAMPLE_NML, key, {"title": "X"})

    p = tmp_path / "edited.nml"
    p.write_text(out, encoding="utf-8")
    assert len(traktor_read.read(str(p)).tracks[key].pois) == before


def test_traktor_reports_no_change_for_an_unknown_track():
    assert traktor_write.set_entry_metadata(SAMPLE_NML, "d:/nope.mp3",
                                            {"title": "x"}) == (SAMPLE_NML, False)


def _vdj_xml():
    return EMPTY_VDJ_XML.replace("</VirtualDJ_Database>",
                                 DOO_RAG_SONG + "</VirtualDJ_Database>")


def test_vdj_sets_every_text_field(tmp_path):
    xml = _vdj_xml()
    p = tmp_path / "database.xml"
    p.write_text(xml, encoding="utf-8")
    folders = str(tmp_path / "Folders")
    key = next(iter(vdj_read.read(str(p), folders).tracks))

    out, changed = vdj_write.set_song_metadata(xml, key, {
        "title": "T <new>", "artist": "A & B", "album": "Alb",
        "genre": "G", "comment": "note & <stuff>"})

    assert changed
    p.write_text(out, encoding="utf-8")
    got = vdj_read.read(str(p), folders).tracks[key]
    assert (got.title, got.artist, got.album, got.genre, got.comment) == \
        ("T <new>", "A & B", "Alb", "G", "note & <stuff>")


def test_vdj_leaves_the_pois_alone(tmp_path):
    xml = _vdj_xml()
    p = tmp_path / "database.xml"
    p.write_text(xml, encoding="utf-8")
    folders = str(tmp_path / "Folders")
    lib = vdj_read.read(str(p), folders)
    key = next(iter(lib.tracks))
    before = len(lib.tracks[key].pois)

    out, _ = vdj_write.set_song_metadata(xml, key, {"title": "X"})

    p.write_text(out, encoding="utf-8")
    assert len(vdj_read.read(str(p), folders).tracks[key].pois) == before


def test_vdj_reports_no_change_for_an_unknown_track():
    xml = _vdj_xml()
    assert vdj_write.set_song_metadata(xml, "d:/nope.mp3", {"title": "x"}) \
        == (xml, False)


# ============================ through the window ============================
def _make_window(tmp_path, monkeypatch):
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(tmp_path / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(tmp_path / "settings.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(tmp_path / "baseline.csv"))
    from trackcleaner.ui.main_window import MainWindow
    settings = Settings()
    settings.paths = write_sample(tmp_path)
    w = MainWindow(settings)
    w.show()
    return w


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    yield w
    w.close()


def test_applying_updates_the_in_memory_track(window):
    lib = window.libraries[TRAKTOR]
    track = lib.tracks[next(iter(lib.tracks))]

    window._apply_metadata([track], TRAKTOR, {"title": "Renamed"})
    window._writer.flush()

    assert track.title == "Renamed"


def test_applying_reaches_the_file(window, tmp_path):
    lib = window.libraries[TRAKTOR]
    key = next(iter(lib.tracks))

    window._apply_metadata([lib.tracks[key]], TRAKTOR, {"artist": "Written"})
    window._writer.flush()

    assert traktor_read.read(window.settings.paths.nml).tracks[key].artist \
        == "Written"


def test_an_empty_edit_writes_nothing(window):
    lib = window.libraries[TRAKTOR]
    track = lib.tracks[next(iter(lib.tracks))]
    before = track.title

    window._apply_metadata([track], TRAKTOR, {})
    window._writer.flush()

    assert track.title == before


def test_an_edit_marks_the_playlists_holding_the_track(window):
    """An edit makes this program differ from the others, exactly as a membership
    edit does -- so the same optimistic marking has to apply, or the sidebar
    would still claim everything is in sync."""
    lib = window.libraries[TRAKTOR]
    node = next(n for n in lib.playlists() if n.playlist.track_keys)
    key = node.playlist.track_keys[0]

    window._apply_metadata([lib.tracks[key]], TRAKTOR, {"title": "Marked"})
    window._writer.flush()

    assert window._edited_paths.get(path_ci(node.node_path)) == TRAKTOR


def test_the_edit_targets_are_the_selection_when_the_track_is_in_it(window):
    """Right-click does not change the selection, so an edit acts on the whole
    selection when the clicked row is part of it -- the rule Sync and Remove use."""
    lib = window.libraries[TRAKTOR]
    keys = list(lib.tracks)[:2]
    table = window._table()
    table.selected_keys = lambda: list(keys)

    got = window._edit_targets(keys[0], TRAKTOR)

    assert sorted(t.key for t in got) == sorted(keys)


def test_a_track_outside_the_selection_is_edited_alone(window):
    lib = window.libraries[TRAKTOR]
    keys = list(lib.tracks)
    table = window._table()
    table.selected_keys = lambda: [keys[0]]

    got = window._edit_targets(keys[1], TRAKTOR)

    assert [t.key for t in got] == [keys[1]]


# =========================== editing in the table ===========================
def _table_with_rows(app, statuses=None):
    from trackcleaner.model import status as stmod
    from trackcleaner.ui.track_table import TrackTable
    tracks = [_t(key=f"k{i}", title=f"T{i}") for i in range(3)]
    statuses = statuses or [stmod.GREEN] * len(tracks)
    t = TrackTable()
    t.set_rows(list(zip(tracks, statuses)))
    return t


def _index(table, row, col_id):
    from trackcleaner.ui.fields import ALL_COLUMN_IDS
    return table._proxy.index(row, ALL_COLUMN_IDS.index(col_id))


def test_a_tag_cell_is_editable(app):
    table = _table_with_rows(app)
    src = table._proxy.mapToSource(_index(table, 0, "title"))

    assert table._model.item(src.row(), src.column()).isEditable()


def test_an_analysis_cell_is_not_editable(app):
    """BPM and the rest are analysis output; editing BPM without moving the
    beatgrid would desynchronise the cues."""
    table = _table_with_rows(app)
    for col in ("bpm", "length", "filepath", "status"):
        src = table._proxy.mapToSource(_index(table, 0, col))
        assert not table._model.item(src.row(), src.column()).isEditable(), col


def test_a_preview_row_is_not_editable(app):
    from trackcleaner.model import status as stmod
    table = _table_with_rows(app, [stmod.GREEN, stmod.GREEN, "green2"])
    src = table._proxy.mapToSource(_index(table, 2, "title"))

    assert not table._model.item(src.row(), src.column()).isEditable()


def test_the_click_that_focuses_a_pane_does_not_start_an_edit(app):
    """The guard you asked for: bringing an unfocused pane forward must not open
    an editor on whatever tag happened to be under the cursor."""
    table = _table_with_rows(app)
    table.selectRow(0)
    table._was_active = False           # what mousePressEvent records first
    table._was_selected = {0}

    assert not table.should_edit_on_click(_index(table, 0, "title"))


def test_a_click_on_a_focused_pane_starts_an_edit(app):
    table = _table_with_rows(app)
    table.selectRow(0)
    table._was_active = True
    table._was_selected = {0}           # …and second, before Qt reselects

    assert table.should_edit_on_click(_index(table, 0, "title"))


def test_clicking_an_unselected_row_is_navigation_not_editing(app):
    """Selecting a different track is the common case; it must not also open an
    editor on the cell you happened to land on."""
    table = _table_with_rows(app)
    table.selectRow(0)
    table._was_active = True
    table._was_selected = {0}

    assert not table.should_edit_on_click(_index(table, 2, "title"))


def test_committing_a_cell_reports_key_field_and_value(app):
    table = _table_with_rows(app)
    seen = []
    table.metadata_edited.connect(lambda k, f, v: seen.append((k, f, v)))
    src = table._proxy.mapToSource(_index(table, 0, "artist"))

    table._model.item(src.row(), src.column()).setText("Typed")

    assert seen == [("k0", "artist", "Typed")]


def test_repopulating_is_not_mistaken_for_a_page_of_edits(app):
    """set_rows rebuilds every item; without the guard each one would look like a
    user edit and queue a write per cell."""
    table = _table_with_rows(app)
    seen = []
    table.metadata_edited.connect(lambda k, f, v: seen.append((k, f, v)))

    table.set_rows([(_t(key="k0", title="Fresh"), "green")])

    assert seen == []


def test_a_cell_edit_writes_only_that_track(window):
    """Never the selection: you clicked one cell, and rewriting the other
    selected tracks' tags would be a surprise with no undo."""
    lib = window.libraries[TRAKTOR]
    keys = list(lib.tracks)[:2]
    table = window._table()
    table.selected_keys = lambda: list(keys)
    table.source = TRAKTOR
    before = lib.tracks[keys[1]].title

    window._on_cell_edited(table, keys[0], "title", "Only This One")
    window._writer.flush()

    assert lib.tracks[keys[0]].title == "Only This One"
    assert lib.tracks[keys[1]].title == before


def test_a_cell_edit_that_changes_nothing_writes_nothing(window):
    lib = window.libraries[TRAKTOR]
    key = next(iter(lib.tracks))
    table = window._table()
    table.source = TRAKTOR
    current = lib.tracks[key].title
    marked = dict(window._edited_paths)

    window._on_cell_edited(table, key, "title", current)

    assert window._edited_paths == marked, "a no-op edit must not mark anything"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
