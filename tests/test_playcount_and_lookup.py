"""Three defects found auditing the playlists that stayed unsynced.

1. **A sync could land in the wrong playlist.** Traktor holds ``Diggs/temp``
   (511 tracks) beside ``Diggs/Temp`` (42); VirtualDJ has only ``Temp``. The
   membership write took the first case-insensitive match and mirrored
   VirtualDJ's 42 into the 511-track ``temp``, removing 469 tracks from it.

2. **A play count on a Traktor INFO without one grew a second INFO.** Traktor
   writes ``<INFO …></INFO>``, not a self-closing tag, so the setter fell
   through to "append a new element".

3. **There was no way to write only a play count.** It travelled with a cue
   write, which re-renders VirtualDJ's Pois and floors a negative grid anchor.
"""

import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.io import traktor_write, vdj_write                    # noqa: E402
from trackcleaner.model.library import (Library, Playlist, TreeNode,     # noqa: E402
                                        TRAKTOR, VDJ)
from trackcleaner.sync.engine import SyncEngine                         # noqa: E402


# ======================= the exact spelling wins ===========================
def _node(source, path, keys):
    return TreeNode(name=path[-1], kind="playlist", source=source,
                    playlist=Playlist(name=path[-1], source=source, track_keys=keys),
                    node_path=path)


def _library(source, *nodes):
    root = TreeNode(name="root", kind="folder", source=source, children=list(nodes))
    return Library(source=source, root=root)


def test_the_exact_spelling_wins_over_an_earlier_case_twin():
    temp_lower = _node(TRAKTOR, ("Diggs", "temp"), ["a"] * 511)
    temp_upper = _node(TRAKTOR, ("Diggs", "Temp"), ["b"] * 42)
    target = _library(TRAKTOR, temp_lower, temp_upper)
    source = _node(VDJ, ("Diggs", "Temp"), ["b"] * 42)

    assert SyncEngine._find_target_node(None, source, target) is temp_upper


def test_with_no_exact_spelling_the_first_case_insensitive_match_is_used():
    only = _node(TRAKTOR, ("Diggs", "temp"), ["a"])
    target = _library(TRAKTOR, only)

    assert SyncEngine._find_target_node(None, _node(VDJ, ("Diggs", "TEMP"), []),
                                        target) is only


# ===================== Traktor's play count, in place ======================
def test_a_playcount_goes_onto_the_existing_info():
    body = '<INFO BITRATE="320000" GENRE="Funk"></INFO>'

    out = traktor_write._set_playcount_nml(body, 7)

    assert out == '<INFO BITRATE="320000" GENRE="Funk" PLAYCOUNT="7"></INFO>'
    assert out.count("<INFO") == 1


def test_an_existing_playcount_is_replaced():
    body = '<INFO BITRATE="1" PLAYCOUNT="2" PLAYTIME="3"></INFO>'

    assert traktor_write._set_playcount_nml(body, 9) == \
        '<INFO BITRATE="1" PLAYCOUNT="9" PLAYTIME="3"></INFO>'


def test_an_entry_with_no_info_gains_one():
    assert traktor_write._set_playcount_nml("<TEMPO></TEMPO>", 1) == \
        '<TEMPO></TEMPO><INFO PLAYCOUNT="1"></INFO>'


NML = """<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<NML VERSION="20"><COLLECTION ENTRIES="2">
<ENTRY MODIFIED_DATE="2020/1/1" MODIFIED_TIME="1" TITLE="A"><LOCATION DIR="/:Music/:" FILE="a.mp3" VOLUME="D:" VOLUMEID="x"></LOCATION>
<INFO BITRATE="320000" PLAYCOUNT="2"></INFO>
<CUE_V2 NAME="Beat Marker" DISPL_ORDER="0" TYPE="4" START="1.000000" LEN="0.000000" REPEATS="-1" HOTCUE="-1"><GRID BPM="120.000000"></GRID>
</CUE_V2>
</ENTRY>
<ENTRY MODIFIED_DATE="2020/1/1" MODIFIED_TIME="1" TITLE="B"><LOCATION DIR="/:Music/:" FILE="b.mp3" VOLUME="D:" VOLUMEID="x"></LOCATION>
<INFO BITRATE="320000"></INFO>
</ENTRY>
</COLLECTION></NML>
"""


def test_set_entries_playcount_changes_the_count_and_nothing_else():
    a, b = r"d:\music\a.mp3", r"d:\music\b.mp3"

    out, changed = traktor_write.set_entries_playcount(NML, {a: 5, b: 3})

    assert changed == {a: True, b: True}
    assert out == (NML.replace('PLAYCOUNT="2"', 'PLAYCOUNT="5"')
                   .replace('<INFO BITRATE="320000"></INFO>\n</ENTRY>\n</COLLECTION>',
                            '<INFO BITRATE="320000" PLAYCOUNT="3"></INFO>\n</ENTRY>\n</COLLECTION>'))
    ET.fromstring(out)


def test_an_unchanged_count_reports_unchanged():
    _out, changed = traktor_write.set_entries_playcount(NML, {r"d:\music\a.mp3": 2})

    assert changed == {r"d:\music\a.mp3": False}


# ==================== VirtualDJ's play count, in place =====================
XML = """<?xml version="1.0" encoding="UTF-8"?>
<VirtualDJ_Database Version="8">
 <Song FilePath="D:\\Music\\a.mp3" FileSize="1">
  <Tags Author="A" Title="T" />
  <Infos SongLength="200" PlayCount="1" />
  <Poi Pos="-1.587823" Type="beatgrid" />
  <Poi Num="5" Type="cue" />
 </Song>
 <Song FilePath="D:\\Music\\stub.mp3" Flag="16">
 </Song>
</VirtualDJ_Database>
"""


def test_set_songs_playcount_leaves_a_negative_grid_alone():
    out, changed = vdj_write.set_songs_playcount(XML, {r"d:\music\a.mp3": 4})

    assert changed == {r"d:\music\a.mp3": True}
    assert out == XML.replace('PlayCount="1"', 'PlayCount="4"')
    assert '<Poi Pos="-1.587823" Type="beatgrid" />' in out


def test_a_song_with_no_infos_has_nowhere_to_keep_a_count():
    out, changed = vdj_write.set_songs_playcount(XML, {r"d:\music\stub.mp3": 4})

    assert changed == {r"d:\music\stub.mp3": False}
    assert out == XML


# ============== one Unicode spelling of a tag is not a difference ============
def test_tag_text_in_either_unicode_spelling_is_equal():
    """``Tony! Toni! Toné!`` composed and decomposed: identical on screen, and a
    permanent "differs" for 60 of the tracks relinked on 2026-11-03."""
    import unicodedata
    from trackcleaner.model.library import Track
    from trackcleaner.model.status import metadata_matches
    nfc = "Tony! Toni! Toné!"
    nfd = unicodedata.normalize("NFD", nfc)
    a = Track(key="k", path="k", source=VDJ, artist=nfc)
    b = Track(key="k", path="k", source=TRAKTOR, artist=nfd)

    assert nfc != nfd
    assert metadata_matches(a, b)
    assert SyncEngine.carried_fields(a, b, ("artist",)) == ({}, {}), \
        "a field the status model calls equal must never be rewritten"


def test_a_real_difference_is_still_carried():
    from trackcleaner.model.library import Track
    a = Track(key="k", path="k", source=VDJ, artist="Toné")
    b = Track(key="k", path="k", source=TRAKTOR, artist="Tone")

    assert SyncEngine.carried_fields(a, b, ("artist",)) == ({"artist": "Toné"}, {})
