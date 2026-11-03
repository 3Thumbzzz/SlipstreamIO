"""Cue round-trip stability, cue deletion, key mapping and NML entry layout.

These guard the defects that kept synced songs permanently yellow:
a pure beatgrid written as HOTCUE="0" resurrected a phantom cue on read-back,
and cue deletions never reached the NML.
"""

import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.config.paths import Paths                        # noqa: E402
from trackcleaner.io import traktor_read, traktor_write            # noqa: E402
from trackcleaner.model import keys                                # noqa: E402
from trackcleaner.model.cues import Poi, format_cue_v2, pois_from_cue_v2  # noqa: E402
from trackcleaner.model.library import Playlist, Track, TreeNode, TRAKTOR, VDJ  # noqa: E402
from trackcleaner.model.status import cues_equivalent, diff_tracks, GREEN  # noqa: E402
from trackcleaner.sync.engine import SyncEngine, UPDATE            # noqa: E402
from fixtures import write_sample                                  # noqa: E402


def _roundtrip(pois):
    """format_cue_v2 -> parse attrs -> pois_from_cue_v2, as the reader does."""
    out = []
    for p in pois:
        xml = format_cue_v2(p)
        out.append(dict(re.findall(r'(\w+)="([^"]*)"', xml)))
    return pois_from_cue_v2(out)


# --------------------------- round-trip stability ---------------------------
def test_beatgrid_only_roundtrip_is_stable():
    """A grid with no cue must NOT come back as grid + phantom cue Num=1."""
    src = [Poi(1.294454, None, "beatgrid")]
    back = _roundtrip(src)
    assert [p.kind for p in back] == ["beatgrid"]
    assert not [p for p in back if p.kind == "cue"]
    a = Track(key="k", path="p", source=VDJ, pois=src)
    b = Track(key="k", path="p", source=TRAKTOR, pois=back)
    assert cues_equivalent(a, b)


def test_grid_plus_cue_roundtrip_is_stable():
    src = [Poi(1.5, None, "beatgrid"), Poi(1.5, 1, "cue"), Poi(30.0, 3, "cue")]
    back = _roundtrip(src)
    assert sorted((p.kind, p.num) for p in back) == sorted((p.kind, p.num) for p in src)
    a = Track(key="k", path="p", source=VDJ, pois=src)
    b = Track(key="k", path="p", source=TRAKTOR, pois=back)
    assert cues_equivalent(a, b)


def test_beatgrid_never_claims_a_pad_slot():
    assert 'HOTCUE="-1"' in format_cue_v2(Poi(1.0, None, "beatgrid"))
    assert 'HOTCUE="2"' in format_cue_v2(Poi(1.0, 3, "cue"))   # Num 3 -> HOTCUE 2


# ------------------------------- key mapping --------------------------------
def test_key_mapping_matches_traktor():
    for text, value, ok in [("Am", 21, "1m"), ("Em", 16, "2m"), ("Dm", 14, "12m"),
                            ("Bm", 23, "3m"), ("Cm", 12, "10m"), ("C", 0, "1d"),
                            ("F", 5, "12d"), ("Bb", 10, "11d")]:
        assert keys.parse_vdj_key(text) == value, text
        assert keys.open_key(value) == ok, text


def test_key_mapping_rejects_unknown():
    for bad in ("", "12m", "xyz", None):
        assert keys.parse_vdj_key(bad) is None


def test_musical_key_inverts_parse_for_every_value():
    """The direction that was missing. Without it Traktor -> VirtualDJ had
    nothing to translate *with*, so the writer passed Open Key text through and
    VirtualDJ got "8d" in a field it reads as a musical key."""
    for value in range(24):
        assert keys.parse_vdj_key(keys.musical_key(value)) == value, value


def test_musical_key_agrees_with_the_open_key_direction():
    for text, value, _ok in [("Am", 21, "1m"), ("Em", 16, "2m"), ("C", 0, "1d"),
                             ("F", 5, "12d")]:
        assert keys.musical_key(value) == text


def test_musical_key_is_empty_for_no_key():
    for bad in (None, -1, 24, 999):
        assert keys.musical_key(bad) == ""


def test_a_traktor_track_lands_in_vdj_as_a_musical_key():
    """The bug as reported: Traktor rows arriving in VirtualDJ with "8d"."""
    from trackcleaner.io import vdj_write
    t = Track(key="k", path=r"D:\Music\A.mp3", source=TRAKTOR, title="A",
              key_musical="8d", key_value=22)          # Traktor's own notation

    block = vdj_write._render_new_song_block(t)
    folder = vdj_write.render_vdjfolder([t])

    assert 'Key="A#m"' in block and '"8d"' not in block
    assert 'key="A#m"' in folder and '"8d"' not in folder


def test_a_track_with_no_analysed_key_keeps_whatever_it_has():
    """No canonical value is not a licence to blank the field."""
    from trackcleaner.io import vdj_write
    t = Track(key="k", path=r"D:\Music\A.mp3", source=VDJ, title="A",
              key_musical="Gm", key_value=None)

    assert 'Key="Gm"' in vdj_write._render_new_song_block(t)


# ------------------------------ entry layout --------------------------------
def test_new_entry_is_multiline_in_order():
    t = Track(key="k", path=r"D:\Music\Funk\A.mp3", source=VDJ, title="A Title",
              artist="An Artist", album="An Album", genre="Funk", bpm=120.0,
              key_musical="Am", length=200.0, playcount=3,
              pois=[Poi(1.0, None, "beatgrid"), Poi(2.0, 1, "cue")])
    out = traktor_write._render_new_entry(t)
    lines = out.strip().split("\n")
    starts = [ln.split(">")[0].split(" ")[0] for ln in lines]
    assert starts == ["<ENTRY", "<ALBUM", "<MODIFICATION_INFO", "<INFO", "<TEMPO",
                      "<MUSICAL_KEY", "<CUE_V2", "<CUE_V2", "</ENTRY"]
    assert "MODIFIED_DATE=" in lines[0] and "MODIFIED_TIME=" in lines[0]
    assert "<LOCATION" in lines[0]                 # LOCATION rides the ENTRY line
    assert 'KEY="1m"' in out                       # Open Key notation
    assert 'VALUE="21"' in out                     # MUSICAL_KEY
    assert "LOUDNESS" not in out                   # no fabricated analysis data


def test_new_entry_omits_unknown_optional_elements():
    t = Track(key="k", path=r"D:\Music\Funk\B.mp3", source=VDJ, title="B",
              artist="Artist")                     # no album, no key, no bpm
    out = traktor_write._render_new_entry(t)
    assert "<ALBUM" not in out
    assert "<MUSICAL_KEY" not in out
    assert "<TEMPO" not in out
    assert "<MODIFICATION_INFO" in out


# ------------------------------ cue deletion --------------------------------
def test_cue_deletion_propagates_and_goes_green(tmp_path):
    """Source drops a cue -> the NML entry loses it too, and the track diffs
    GREEN in both directions."""
    paths = write_sample(tmp_path)
    nml_path = tmp_path / "collection.nml"
    tlib = traktor_read.read(paths.nml)

    # a collection track that currently has several cues
    key = next(k for k, t in tlib.tracks.items() if len(t.pois) > 2)
    tgt = tlib.tracks[key]
    grid = next(p for p in tgt.pois if p.kind == "beatgrid")

    # VDJ side keeps ONLY the beatgrid (all cues removed). Tags mirror the
    # Traktor entry so the status law sees a pure cue change.
    src = Track(key=key, path=tgt.path, source=VDJ, title=tgt.title, artist=tgt.artist,
                album=tgt.album, genre=tgt.genre, comment=tgt.comment,
                pois=[Poi(grid.pos, None, "beatgrid")])

    from trackcleaner.io import vdj_read
    vlib = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    vlib.tracks[key] = src
    node = TreeNode(name="P", kind="playlist", source=VDJ, node_path=("P",),
                    playlist=Playlist(name="P", source=VDJ, track_keys=[key]))

    engine = SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=None)
    engine.sync_playlist(node, VDJ, UPDATE)

    tlib2 = traktor_read.read(str(nml_path))
    after = tlib2.tracks[key]
    assert [p.kind for p in after.pois] == ["beatgrid"], "cues must be deleted"

    # green in both directions
    assert diff_tracks([key], vlib, tlib2).track_status[key] == GREEN
    assert diff_tracks([key], tlib2, vlib).track_status[key] == GREEN


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))


# --------------------------- the Key column ---------------------------------
# Reported as "in the traktor playlists, some of the keys are not displayed as
# open key, but in traktor all keys metadata are open keys". Verified on a real
# collection.nml: of 311 entries, 87 hold Open Key in INFO/@KEY, 3 hold musical
# notation, and 3 hold nothing beside an analysed MUSICAL_KEY. Traktor displays
# from that number every time, so showing the stored text put a different key on
# screen from the one the user sees in Traktor.
def _shown(track):
    from trackcleaner.ui.fields import FIELD_BY_ID
    return FIELD_BY_ID["key"].getter(track)


def test_a_traktor_row_shows_open_key_whatever_is_stored():
    stored_openkey = Track(key="a", path="a", source=TRAKTOR,
                           key_musical="8m", key_value=22)
    stored_musical = Track(key="b", path="b", source=TRAKTOR,
                           key_musical="A#m", key_value=22)   # the 3 odd entries
    stored_nothing = Track(key="c", path="c", source=TRAKTOR,
                           key_musical="", key_value=22)      # analysed, untagged

    assert _shown(stored_openkey) == "8m"
    assert _shown(stored_musical) == "8m", "was showing A#m, Traktor shows 8m"
    assert _shown(stored_nothing) == "8m", "was showing nothing at all"


def test_a_virtualdj_row_shows_musical_notation():
    assert _shown(Track(key="a", path="a", source=VDJ, key_value=22)) == "A#m"


def test_a_track_with_no_analysed_key_still_shows_what_it_has():
    """Including VirtualDJ's flat spellings, which are not ours to normalise."""
    t = Track(key="a", path="a", source=VDJ, key_musical="Ebm", key_value=None)
    assert _shown(t) == "Ebm"


def test_an_unknown_program_falls_back_rather_than_failing():
    t = Track(key="a", path="a", source="something-else", key_musical="Am",
              key_value=None)
    assert _shown(t) == "Am"
