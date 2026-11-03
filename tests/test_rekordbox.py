"""Rekordbox's XML: reading it exactly, and writing it without collateral damage.

The adapter contract (``test_adapter_contract.py``) already states what every
program must do. This module is what only ``rekordbox.xml`` can get wrong:

* a ``Location`` is a percent-encoded URL inside an XML attribute -- two layers,
  each undone exactly once, or a name with a literal ``%20`` or ``&amp;`` in it
  stops matching its own file;
* a ``<POSITION_MARK>`` is ours only as a hot cue (``Type="0"``, ``Num`` 0..7);
  memory cues and loops are Rekordbox's and must survive every write;
* the grid is one ``<TEMPO>``, and a tempo-changing grid must not be flattened;
* playlists are a nested tree addressed by path, with ``Count``/``Entries``
  attributes Rekordbox reads back -- an edit that leaves them wrong produces a
  file Rekordbox imports wrongly;
* and the end-to-end property: a track synced in from Traktor reads back as in
  sync, or the square is yellow for ever.
"""

import os
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.adapters import build_adapters                       # noqa: E402
from trackcleaner.io import rekordbox_read, rekordbox_write as rw      # noqa: E402
from trackcleaner.model import matching, status                        # noqa: E402
from trackcleaner.model.cues import Poi                                # noqa: E402
from trackcleaner.model.library import Track                           # noqa: E402
from fixtures import (HOMENAJE, SAFE_AND_SOUND, SAMPLE_REKORDBOX_XML,  # noqa: E402
                      write_sample)

REKORDBOX = rekordbox_read.REKORDBOX


@pytest.fixture
def adapter(tmp_path):
    # write_sample writes an empty rekordbox.xml; the sample replaces it after.
    paths = write_sample(tmp_path)
    with open(paths.rekordbox, "w", encoding="utf-8") as f:
        f.write(SAMPLE_REKORDBOX_XML)
    return build_adapters(paths)[REKORDBOX]


def _key(path):
    return matching.path_key(path)


def _track(path, **kw):
    return Track(key=_key(path), path=path, source="", **kw)


# ================================== reading =================================
def test_the_location_is_decoded_once_into_a_windows_path(adapter):
    lib = adapter.read()

    assert lib.tracks[_key(SAFE_AND_SOUND)].path == SAFE_AND_SOUND
    assert lib.tracks[_key(HOMENAJE)].path == HOMENAJE


def test_the_name_is_entity_decoded_once(adapter):
    assert adapter.read().tracks[_key(SAFE_AND_SOUND)].title == "Safe & Sound"


def test_tags_key_and_counts_are_read(adapter):
    t = adapter.read().tracks[_key(SAFE_AND_SOUND)]

    assert (t.artist, t.album, t.genre, t.comment) == (
        "Justice", "Audio, Video, Disco", "Electro", "peak time")
    assert (t.bpm, t.length, t.bitrate, t.playcount) == (122.0, 303.0, 320, 4)
    assert t.key_value == 15, "Ebm"
    assert adapter.display_key(t) == "Ebm", "Rekordbox spells it with a flat"


def test_hot_cues_and_the_grid_are_ours_memory_cues_and_loops_are_not(adapter):
    """Pad A is cue 1, as Traktor's HOTCUE 0 is: one numbering everywhere."""
    pois = adapter.read().tracks[_key(SAFE_AND_SOUND)].pois

    assert sorted((p.kind, p.num, p.pos) for p in pois) == [
        ("beatgrid", None, 0.212), ("cue", 1, 31.684), ("cue", 3, 94.623)]


def test_a_self_closing_track_reads_with_no_points(adapter):
    assert adapter.read().tracks[_key(HOMENAJE)].pois == []


def test_playlists_resolve_both_kinds_of_member_reference(adapter):
    gigs = next(c for c in adapter.read().root.children if c.name == "Gigs")
    by_name = {c.name: c.playlist.track_keys for c in gigs.children}

    assert by_name["Warmup"] == [_key(HOMENAJE), _key(SAFE_AND_SOUND)]
    assert by_name["By Path"] == [_key(HOMENAJE)]


# ============================ a Location, both ways =========================
@pytest.mark.parametrize("path", [
    SAFE_AND_SOUND,
    r"D:\Music\Rock%20Steady.mp3",                  # a literal "%20" in the name
    r"D:\Music\Justice - Safe &amp; Sound.mp3",     # literal entity text
    r"D:\Music\[2008] Gator Bait ☮ (edit) - it's on, 100% + more.mp3",
    "D:\\Music\\Dogg Master - D\u00e9ja Vu.mp3",
    "D:\\Music\\Dogg Master - De\u0301ja Vu.mp3",   # the other spelling, kept as is
])
def test_a_path_survives_the_round_trip_exactly(path):
    location = rw.path_to_location(path)
    back = rekordbox_read.location_to_path(
        matching.decode_entities(ET.fromstring(
            f"<T Location={rw._attr(location)}/>").get("Location")))

    assert back == path


def test_a_location_is_spelled_the_way_rekordbox_spells_it():
    """Read off a real export: ``&`` and brackets encoded, parentheses and
    apostrophes left alone."""
    assert rw.path_to_location(r"D:\Music\A & B (it's) [x].mp3") == \
        "file://localhost/D:/Music/A%20%26%20B%20(it's)%20%5Bx%5D.mp3"


# ========================= writing touches only its target ==================
def test_a_tag_edit_leaves_every_other_byte_alone():
    new, done = rw.set_tracks_metadata(SAMPLE_REKORDBOX_XML,
                                       {_key(HOMENAJE): {"genre": "Cumbia"}})

    assert done == {_key(HOMENAJE): True}
    assert new.replace('Genre="Cumbia"', 'Genre="Latin"') == SAMPLE_REKORDBOX_XML


def test_a_cue_write_keeps_rekordbox_s_own_markers():
    """The memory cue and the loop belong to Rekordbox. The hot cues are
    replaced wholesale, so the one the source no longer has is gone."""
    track = _track(SAFE_AND_SOUND, pois=[Poi(40.0, 2, "cue")])

    new, created = rw.sync_tracks(SAMPLE_REKORDBOX_XML, [track])
    block = rw.find_tracks(new, [track.key])[track.key]

    assert created == {track.key: False}
    assert 'Num="-1"' in block, "memory cue dropped"
    assert 'Type="4"' in block and 'End="63.967"' in block, "loop dropped"
    assert 'Start="31.684"' not in block and 'Start="94.623"' not in block
    assert '<POSITION_MARK Name="" Type="0" Start="40.000" Num="1"/>' in block
    ET.fromstring(new)


def test_a_cue_write_touches_no_other_track():
    track = _track(SAFE_AND_SOUND, pois=[Poi(40.0, 2, "cue")])

    new, _ = rw.sync_tracks(SAMPLE_REKORDBOX_XML, [track])

    homenaje = rw.find_tracks(SAMPLE_REKORDBOX_XML, [_key(HOMENAJE)])
    assert rw.find_tracks(new, [_key(HOMENAJE)]) == homenaje
    assert new[new.find("<PLAYLISTS>"):] == \
        SAMPLE_REKORDBOX_XML[SAMPLE_REKORDBOX_XML.find("<PLAYLISTS>"):]


def test_the_grid_anchor_and_tempo_are_replaced_keeping_the_metre():
    track = _track(SAFE_AND_SOUND, bpm=121.5,
                   pois=[Poi(1.75, None, "beatgrid")])

    new, _ = rw.sync_tracks(SAMPLE_REKORDBOX_XML, [track])
    block = rw.find_tracks(new, [track.key])[track.key]

    assert block.count("<TEMPO") == 1
    assert '<TEMPO Inizio="1.750" Bpm="121.50" Metro="4/4" Battito="1"/>' in block


def test_a_tempo_changing_grid_is_never_flattened():
    two = SAMPLE_REKORDBOX_XML.replace(
        '\t\t\t<TEMPO Inizio="0.212" Bpm="122.00" Metro="4/4" Battito="1"/>\n',
        '\t\t\t<TEMPO Inizio="0.212" Bpm="122.00" Metro="4/4" Battito="1"/>\n'
        '\t\t\t<TEMPO Inizio="120.000" Bpm="124.00" Metro="4/4" Battito="1"/>\n')
    track = _track(SAFE_AND_SOUND, bpm=122.0, pois=[Poi(9.0, None, "beatgrid")])

    new, _ = rw.sync_tracks(two, [track])
    block = rw.find_tracks(new, [track.key])[track.key]

    assert 'Inizio="0.212"' in block and 'Inizio="120.000"' in block
    assert 'Inizio="9.000"' not in block


def test_a_self_closing_track_opens_up_to_take_cues():
    track = _track(HOMENAJE, pois=[Poi(12.0, 1, "cue")])

    new, _ = rw.sync_tracks(SAMPLE_REKORDBOX_XML, [track])
    back = rekordbox_read.track_from_element(
        ET.fromstring(rw.find_tracks(new, [track.key])[track.key]))

    assert [(p.num, p.pos) for p in back.pois] == [(1, 12.0)]
    ET.fromstring(new)


def test_a_new_track_is_appended_with_a_fresh_id_and_counted():
    track = _track(r"D:\Music\New & Shiny.mp3", title="New", artist="Me",
                   key_value=10, pois=[Poi(5.0, 1, "cue")])

    new, created = rw.sync_tracks(SAMPLE_REKORDBOX_XML, [track])

    assert created == {track.key: True}
    assert '<COLLECTION Entries="3">' in new
    assert rw.track_ids(new)[track.key] == "10", "max TrackID 9, plus one"
    assert 'Tonality="Bb"' in new, "flats, as Rekordbox writes them"
    root = ET.fromstring(new)
    assert len(root.find("COLLECTION").findall("TRACK")) == 3


def test_removing_a_track_takes_its_line_and_decrements_the_count():
    new, removed = rw.remove_track(SAMPLE_REKORDBOX_XML, _key(HOMENAJE))

    assert removed and '<COLLECTION Entries="1">' in new
    assert "Homenaje" not in new[:new.find("<PLAYLISTS>")]
    ET.fromstring(new)


# ================================== playlists ===============================
def _node_attrs(content, *path):
    node = rw.find_node(rw.scan_nodes(content), path)
    return content[node.start:node.open_end]


def test_a_new_playlist_counts_into_its_parent():
    new = rw.create_playlist(SAMPLE_REKORDBOX_XML, ("Gigs", "Late"), ["7"])

    assert 'Count="3"' in _node_attrs(new, "Gigs")
    assert 'Entries="1"' in _node_attrs(new, "Gigs", "Late")
    ET.fromstring(new)


def test_a_new_folder_chain_counts_at_every_level():
    new, changed = rw.ensure_folders(SAMPLE_REKORDBOX_XML, ("A", "B"))

    assert changed
    assert 'Count="2"' in _node_attrs(new, ), "ROOT now holds Gigs and A"
    assert 'Count="1"' in _node_attrs(new, "A")
    assert 'Count="0"' in _node_attrs(new, "A", "B")
    ET.fromstring(new)


def test_membership_keeps_the_playlist_s_own_key_type():
    """A Location-keyed playlist must stay Location-keyed: Rekordbox reads the
    Key by the playlist's KeyType."""
    new, _ = rw.set_playlist(SAMPLE_REKORDBOX_XML, ("Gigs", "By Path"),
                             [("7", SAFE_AND_SOUND), ("9", HOMENAJE)])

    node = rw.find_node(rw.scan_nodes(new), ("Gigs", "By Path"))
    refs = [k for _s, _e, k in rw._refs(new, node)]
    assert refs == [rw.path_to_location(SAFE_AND_SOUND),
                    rw.path_to_location(HOMENAJE)]
    assert 'Entries="2"' in new[node.start:node.open_end]


def test_moving_and_removing_keep_every_count_true():
    moved, ok = rw.move_node(SAMPLE_REKORDBOX_XML, ("Gigs", "Warmup"), ())
    assert ok
    assert 'Count="2"' in _node_attrs(moved, ), "ROOT: Gigs and Warmup"
    assert 'Count="1"' in _node_attrs(moved, "Gigs")
    gone, ok = rw.remove_node(moved, ("Gigs",))
    assert ok and 'Count="1"' in _node_attrs(gone, )
    for content in (moved, gone):
        ET.fromstring(content)


def test_a_moved_node_is_reindented_to_its_new_depth():
    moved, _ = rw.move_node(SAMPLE_REKORDBOX_XML, ("Gigs", "Warmup"), ())

    assert '\n\t\t\t<NODE Name="Warmup"' in moved
    assert '\n\t\t\t\t<TRACK Key="9"/>' in moved


def test_a_folder_cannot_move_into_itself():
    _new, ok = rw.move_node(SAMPLE_REKORDBOX_XML, ("Gigs",), ("Gigs", "Warmup"))

    assert not ok


def test_repathing_moves_location_keyed_references_too():
    target = r"D:\Music\Homenaje (fixed).mp3"

    new, done = rw.repath_tracks(SAMPLE_REKORDBOX_XML, {_key(HOMENAJE): target})

    assert done == {_key(HOMENAJE): True}
    assert rw.path_to_location(HOMENAJE) not in new
    assert new.count(rw.path_to_location(target)) == 2, "entry and By Path"
    ET.fromstring(new)


# ================================= end to end ===============================
def test_a_track_synced_in_from_traktor_reads_back_in_sync(tmp_path):
    """The convergence property: whatever is written must compare equal when
    read back, or the square is yellow for ever and every sync rewrites it."""
    from trackcleaner.sync.engine import SyncEngine
    paths = write_sample(tmp_path)
    adapters = build_adapters(paths)
    libs = {a: adapters[a].read() for a in ("traktor", REKORDBOX)}
    engine = SyncEngine(libs, paths, owners=None,
                        adapters={a: adapters[a] for a in libs})
    node = next(n for n in libs["traktor"].playlists() if n.name == "test")

    engine.sync_playlist(node, "traktor", "overwrite", target=REKORDBOX)

    back = adapters[REKORDBOX].read()
    fields = engine.syncable_fields("traktor", REKORDBOX)
    for key in node.playlist.track_keys:
        src, got = libs["traktor"].tracks[key], back.tracks[key]
        assert got.pois, "the cues reached the file"
        assert status.content_matches(src, got, fields), key
    synced = back.find_playlist(node.node_path)
    assert synced.playlist.track_keys == node.playlist.track_keys
    ET.parse(paths.rekordbox)
