"""Reading and preserving stored points.

Guards two defects:
  * a <Poi> with no Pos attribute was dropped, so cues added in VirtualDJ were
    invisible to the diff and the song stayed green;
  * automix/remix/loop/load/fade points were read as cues (or not at all) and
    then deleted by the writers.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.io import traktor_read, traktor_write, vdj_read, vdj_write  # noqa: E402
from trackcleaner.model.cues import Poi, parse_poi_lines                      # noqa: E402
from trackcleaner.model.library import Library, Track, TRAKTOR, VDJ           # noqa: E402
from trackcleaner.model.status import GREEN, cues_equivalent, diff_tracks, is_changed  # noqa: E402
from fixtures import DOO_RAG_SONG, SAMPLE_NML, write_sample                   # noqa: E402


# ------------------------- reading points correctly -------------------------
def test_pos_less_cue_is_parsed_at_zero():
    pois = parse_poi_lines(DOO_RAG_SONG)
    cues = [p for p in pois if p.kind == "cue"]
    grids = [p for p in pois if p.kind == "beatgrid"]
    assert len(grids) == 1
    assert len(cues) == 3, "cues written without a Pos attribute must still parse"
    assert sorted(c.num for c in cues) == [1, 2, 3]
    assert all(c.pos == 0.0 for c in cues), "omitted Pos means position 0"


def test_automix_point_is_not_a_cue():
    pois = parse_poi_lines(DOO_RAG_SONG)
    assert all(p.kind in ("cue", "beatgrid") for p in pois)
    assert len(pois) == 4          # 1 grid + 3 cues; the automix point is ignored


def test_remix_and_unknown_types_ignored():
    body = ('<Poi Pos="1.0" Type="beatgrid" />'
            '<Poi Pos="2.0" Type="remix" />'
            '<Poi Pos="3.0" Type="somethingnew" />')
    assert [p.kind for p in parse_poi_lines(body)] == ["beatgrid"]


def test_added_cues_are_detected_as_a_difference():
    """The reported symptom: 3 cues added in VDJ must not read as in-sync."""
    vdj_track = vdj_read._track_from_song(DOO_RAG_SONG)
    nml_track = Track(key=vdj_track.key, path=vdj_track.path, source=TRAKTOR,
                      pois=[Poi(1.294454, None, "beatgrid")])
    assert not cues_equivalent(vdj_track, nml_track)
    src = Library(source=VDJ, tracks={vdj_track.key: vdj_track})
    tgt = Library(source=TRAKTOR, tracks={nml_track.key: nml_track})
    assert is_changed(diff_tracks([vdj_track.key], src, tgt).track_status[vdj_track.key])


# --------------------- writers must preserve foreign points -----------------
def test_vdj_write_preserves_automix_point():
    track = vdj_read._track_from_song(DOO_RAG_SONG)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<VirtualDJ_Database Version="2023">\n'
           + DOO_RAG_SONG + "\n</VirtualDJ_Database>\n")
    # rewrite with a different cue set
    track = Track(**{**track.__dict__,
                     "pois": [Poi(1.5, None, "beatgrid"), Poi(9.0, 1, "cue")]})
    out, created = vdj_write.sync_track_into_xml(xml, track)
    assert not created
    assert 'Type="automix"' in out, "automix point must survive a cue rewrite"
    assert out.count('Type="cue"') == 1        # old 3 cues replaced by 1
    assert out.count('Type="beatgrid"') == 1
    assert 'Pos="9.000000"' in out


def test_nml_write_preserves_loop_and_load_points():
    key = r"d:\virtualdj\03 - jackson sisters - i believe in miracles.mp3"
    track = Track(key=key,
                  path=r"D:\VirtualDJ\03 - Jackson Sisters - I Believe In Miracles.mp3",
                  source=VDJ, title="x", artist="y",
                  pois=[Poi(1.0, None, "beatgrid"), Poi(5.0, 2, "cue")])
    out, created = traktor_write.sync_track_into_nml(SAMPLE_NML, track)
    assert not created
    entry = re.search(r"<ENTRY[^>]*Jackson Sisters.*?</ENTRY>", out, re.DOTALL).group(0)
    assert 'TYPE="5"' in entry, "loop marker must survive a cue rewrite"
    assert 'TYPE="3"' in entry, "load marker must survive a cue rewrite"
    assert entry.count('TYPE="0"') == 1        # our single cue
    assert entry.count('TYPE="4"') == 1        # our single beatgrid


def test_foreign_points_survive_a_full_playlist_sync(tmp_path):
    """End-to-end: loops/load points still present after sync_playlist."""
    from trackcleaner.model.library import Playlist, TreeNode
    from trackcleaner.sync.engine import SyncEngine, UPDATE

    paths = write_sample(tmp_path)
    tlib = traktor_read.read(paths.nml)
    key = next(k for k in tlib.tracks if "jackson" in k)
    # Same track, same tags as the Traktor entry -- only the cues differ, so the
    # status law sees a pure cue change (metadata is compared too now).
    _t = tlib.tracks[key]
    src = Track(key=key, path=_t.path, source=VDJ, title=_t.title, artist=_t.artist,
                album=_t.album, genre=_t.genre, comment=_t.comment,
                pois=[Poi(1.0, None, "beatgrid")])
    vlib = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    vlib.tracks[key] = src
    node = TreeNode(name="P", kind="playlist", source=VDJ, node_path=("P",),
                    playlist=Playlist(name="P", source=VDJ, track_keys=[key]))
    SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=None).sync_playlist(node, VDJ, UPDATE)

    out = (tmp_path / "collection.nml").read_text(encoding="utf-8")
    entry = re.search(r"<ENTRY[^>]*Jackson Sisters.*?</ENTRY>", out, re.DOTALL).group(0)
    assert 'TYPE="5"' in entry and 'TYPE="3"' in entry
    # and the track now reads as in sync
    tlib2 = traktor_read.read(str(paths.nml))
    assert diff_tracks([key], vlib, tlib2).track_status[key] == GREEN


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
