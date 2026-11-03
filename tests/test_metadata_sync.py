"""A sync carries the tags it compares.

Editing a tag left the pair **permanently yellow** — at every mode, in both
directions. Two faults compounded, and both are pinned here:

1. the write gate asked `cues_equivalent`, so a metadata-only difference
   short-circuited to "already in sync" *before* the mode branches were reached;
2. `sync_track_into_nml` / `sync_track_into_xml` write only cues and playcount
   onto an **existing** entry — tags are rendered solely when creating one.

And a third, found while proving the first two: `_render_new_song_block` never
wrote Album, so every newly created VirtualDJ entry differed from its source the
instant it was written.

The rule these enforce: **anything COMPARED_FIELDS checks and a sync can write,
a sync must write** — otherwise there is a state the user cannot get out of.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.io import traktor_read, traktor_write                 # noqa: E402
from trackcleaner.io import vdj_read, vdj_write                         # noqa: E402
from trackcleaner.model import status as st                             # noqa: E402
from trackcleaner.sync.engine import (NEW, OVERWRITE, UPDATE,           # noqa: E402
                                      SyncEngine)
from fixtures import write_sample                                       # noqa: E402


@pytest.fixture
def stage(tmp_path):
    """Both libraries loaded, with one playlist already synced across."""
    paths = write_sample(tmp_path)
    trak = traktor_read.read(paths.nml)
    vdj = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    node = next(n for n in trak.playlists() if n.playlist and n.playlist.track_keys)
    key = node.playlist.track_keys[0]
    SyncEngine({"traktor": trak, "vdj": vdj}, paths).sync_playlist(
        node, "traktor", NEW, target="vdj")

    import types
    reloaded = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    return types.SimpleNamespace(
        paths=paths, trak=trak, vdj=reloaded, node=node, key=key,
        engine=lambda: SyncEngine({"traktor": trak, "vdj": reloaded}, paths),
        reload_vdj=lambda: vdj_read.read(paths.xml, paths.vdj_folders_dir()),
        reload_trak=lambda: traktor_read.read(paths.nml))


# ======================= the writers carry tags at all ======================
def test_the_nml_sync_writer_does_not_touch_tags(tmp_path):
    """Documenting the split that caused this: the *sync* writer owns cues and
    playcount, and metadata goes through set_entry_metadata. Stated so nobody
    "simplifies" the engine back into calling only the former."""
    from fixtures import SAMPLE_NML
    lib = traktor_read.read(str(_write(tmp_path, "collection.nml", SAMPLE_NML)))
    track = list(lib.tracks.values())[0]
    edited = type(track)(**{**track.__dict__, "comment": "NEW COMMENT"})

    out, _created = traktor_write.sync_track_into_nml(SAMPLE_NML, edited)

    assert "NEW COMMENT" not in out


def test_set_entry_metadata_is_what_writes_them(tmp_path):
    from fixtures import SAMPLE_NML
    lib = traktor_read.read(str(_write(tmp_path, "collection.nml", SAMPLE_NML)))
    key = list(lib.tracks)[0]

    out, changed = traktor_write.set_entry_metadata(
        SAMPLE_NML, key, {"comment": "NEW COMMENT"})

    assert changed and "NEW COMMENT" in out


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ========================= a new entry is born green ========================
def test_a_freshly_synced_track_matches_its_source(stage):
    """It used to differ on arrival -- yellow with nothing the user could do."""
    fields = stage.engine().syncable_fields("traktor", "vdj")

    assert st.content_matches(stage.trak.tracks[stage.key],
                              stage.vdj.tracks[stage.key], fields)


def test_album_survives_creating_a_vdj_entry(stage):
    """The specific omission: <Tags> was rendered without an Album attribute."""
    assert stage.vdj.tracks[stage.key].album == stage.trak.tracks[stage.key].album
    assert stage.trak.tracks[stage.key].album, "fixture must have an album to test"


def test_a_rendered_song_block_names_every_writable_tag():
    from trackcleaner.model.library import Track
    block = vdj_write._render_new_song_block(
        Track(key="k", source="vdj", path="D:/M/a.mp3", title="T", artist="A",
              album="Alb", genre="G", comment="C"))

    for value in ("T", "A", "Alb", "G", "C"):
        assert value in block, value


# ======================= a tag edit reaches the target ======================
@pytest.mark.parametrize("field, value", [
    ("comment", "EDITED COMMENT"),
    ("title", "Edited Title"),
    ("artist", "Edited Artist"),
    ("album", "Edited Album"),
    ("genre", "Edited Genre"),
])
def test_every_compared_field_syncs(stage, field, value):
    setattr(stage.trak.tracks[stage.key], field, value)

    stage.engine().sync_playlist(stage.node, "traktor", OVERWRITE, target="vdj")

    assert getattr(stage.reload_vdj().tracks[stage.key], field) == value


def test_overwrite_reaches_a_metadata_only_difference(stage):
    """The reported bug. Cue equivalence used to short-circuit to "nothing to
    do" before the mode was even consulted, so Overwrite did nothing."""
    stage.trak.tracks[stage.key].comment = "EDITED"
    assert st.cues_equivalent(stage.trak.tracks[stage.key],
                              stage.vdj.tracks[stage.key]), \
        "the fixture must have equivalent cues, or this proves nothing"

    stage.engine().sync_playlist(stage.node, "traktor", OVERWRITE, target="vdj")

    assert stage.reload_vdj().tracks[stage.key].comment == "EDITED"


def test_update_reaches_it_too(stage):
    stage.trak.tracks[stage.key].comment = "EDITED"

    stage.engine().sync_playlist(stage.node, "traktor", UPDATE, target="vdj")

    assert stage.reload_vdj().tracks[stage.key].comment == "EDITED"


def test_new_only_leaves_an_existing_entry_alone(stage):
    """"New only" means new tracks; it must not rewrite one already there."""
    stage.trak.tracks[stage.key].comment = "EDITED"

    stage.engine().sync_playlist(stage.node, "traktor", NEW, target="vdj")

    assert stage.reload_vdj().tracks[stage.key].comment != "EDITED"


def test_the_track_goes_green(stage):
    """The user-visible point of all of this."""
    stage.trak.tracks[stage.key].comment = "EDITED"
    engine = stage.engine()
    fields = engine.syncable_fields("traktor", "vdj")

    engine.sync_playlist(stage.node, "traktor", OVERWRITE, target="vdj")

    assert st.content_matches(stage.trak.tracks[stage.key],
                              stage.reload_vdj().tracks[stage.key], fields)


def test_the_in_memory_copy_is_updated_too(stage):
    """Or the row stays yellow until the next reload even though the file on
    disk already matches."""
    stage.trak.tracks[stage.key].comment = "EDITED"

    stage.engine().sync_playlist(stage.node, "traktor", OVERWRITE, target="vdj")

    assert stage.vdj.tracks[stage.key].comment == "EDITED"


def test_it_works_the_other_way_round(stage):
    """VirtualDJ -> Traktor is the direction that was reported."""
    stage.vdj.tracks[stage.key].comment = "FROM VDJ"
    vdj_node = next((n for n in stage.vdj.playlists()
                     if n.playlist and stage.key in n.playlist.track_keys), None)
    if vdj_node is None:
        pytest.skip("fixture has no VirtualDJ playlist holding this track")

    stage.engine().sync_playlist(vdj_node, "vdj", OVERWRITE, target="traktor")

    assert stage.reload_trak().tracks[stage.key].comment == "FROM VDJ"


# ===================== what a sync cannot write, it skips ===================
def test_syncable_fields_are_what_both_programs_can_store(stage):
    fields = stage.engine().syncable_fields("traktor", "vdj")

    assert set(fields) == {"title", "artist", "album", "genre", "comment"}
    assert "comment2" not in fields, "VirtualDJ has nowhere to put one"


def test_a_comment2_only_difference_does_not_churn(stage):
    """comment2 is Traktor-only. Comparing it while being unable to write it
    would rewrite the target on every single sync and never converge, so the
    write gate uses the fields it can actually carry."""
    stage.trak.tracks[stage.key].comment2 = "TRAKTOR ONLY"
    before = os.path.getmtime(stage.paths.xml), \
        open(stage.paths.xml, encoding="utf-8").read()

    stage.engine().sync_playlist(stage.node, "traktor", OVERWRITE, target="vdj")

    assert open(stage.paths.xml, encoding="utf-8").read() == before[1]


def test_a_comment2_only_difference_still_reads_as_unsynced(stage):
    """The known residue, stated rather than hidden: the status model compares
    comment2, so such a track shows yellow even though nothing can be done."""
    stage.trak.tracks[stage.key].comment2 = "TRAKTOR ONLY"

    assert not st.content_matches(stage.trak.tracks[stage.key],
                                  stage.vdj.tracks[stage.key])


# ====================== what a sync must not disturb ========================
def test_unowned_markers_survive_a_tag_write(tmp_path):
    """The binding rule: VirtualDJ automix/<Scan> points and Traktor loop, fade
    and load markers are not ours and survive byte-for-byte."""
    from fixtures import SAMPLE_NML
    nml = SAMPLE_NML.replace(
        "</ENTRY>",
        '<LOOP_INFO NAME="keepme"></LOOP_INFO></ENTRY>', 1)
    lib = traktor_read.read(str(_write(tmp_path, "c.nml", nml)))
    key = list(lib.tracks)[0]

    out, changed = traktor_write.set_entry_metadata(nml, key, {"comment": "X"})

    assert changed
    assert '<LOOP_INFO NAME="keepme">' in out


def test_a_tag_write_leaves_other_tags_alone(tmp_path):
    from fixtures import SAMPLE_NML
    lib = traktor_read.read(str(_write(tmp_path, "c.nml", SAMPLE_NML)))
    key = list(lib.tracks)[0]
    before = lib.tracks[key]

    out, _ = traktor_write.set_entry_metadata(SAMPLE_NML, key, {"comment": "X"})
    after = traktor_read.read(str(_write(tmp_path, "after.nml", out))).tracks[key]

    assert after.title == before.title and after.artist == before.artist
    assert after.album == before.album and after.genre == before.genre
    assert after.comment == "X"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
