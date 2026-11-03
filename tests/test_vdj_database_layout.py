"""database.xml must look like VirtualDJ wrote it.

Reported: VirtualDJ said "Database D: is corrupt" about a database.xml this app
had edited, and its own repair emptied it. The file was valid XML, but against
VirtualDJ's own it broke every convention VirtualDJ keeps -- all of them this
app's doing: 596 empty attributes, 530 FileSize="0", 73,038 songs closing at
column 0 after a line holding one space, whitespace runs of up to 862,932
characters left by removals, empty comments, and three C: songs written into
the D: database. Every writer now leaves a song in VirtualDJ's own layout, and
a drive's database is only given that drive's songs.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.io import vdj_write                                    # noqa: E402
from trackcleaner.model import matching                                  # noqa: E402
from trackcleaner.model.cues import Poi                                  # noqa: E402
from trackcleaner.model.library import Track, TRAKTOR                    # noqa: E402

DB = ('<?xml version="1.0" encoding="UTF-8"?>\n'
      '<VirtualDJ_Database Version="2023">\n'
      ' <Song FilePath="D:\\Music\\a.mp3" FileSize="123">\n'
      '  <Tags Author="A" Title="T" />\n'
      '  <Infos SongLength="200.0" PlayCount="1" />\n'
      '  <Poi Pos="1.000000" Type="beatgrid" />\n'
      '  <Poi Pos="2.000000" Num="1" Type="cue" />\n'
      ' </Song>\n'
      ' <Song FilePath="D:\\Music\\b.mp3" FileSize="456">\n'
      '  <Tags Author="B" Title="U" />\n'
      ' </Song>\n'
      '</VirtualDJ_Database>\n')
A = r"D:\Music\a.mp3"


def _t(path, **kw):
    return Track(key=matching.path_key(path), path=path, source=TRAKTOR, **kw)


def _vdj_conventions(text):
    """What VirtualDJ's own database never has."""
    return {
        "empty attributes": len(re.findall(r' \w+=""', text)),
        'FileSize="0"': text.count('FileSize="0"'),
        "column-0 </Song>": text.count("\n</Song>"),
        "whitespace-only lines": len(re.findall(r"\n[ \t]*\n", text)),
        "empty comments": text.count("<Comment></Comment>"),
        "child indent not 2": len(re.findall(r"\n(?! {2}<)[ \t]*<(?:Tags|Infos|Scan|Poi|Comment)\b", text)),
    }


CLEAN = {k: 0 for k in _vdj_conventions("")}


def test_rewriting_cues_keeps_virtualdjs_layout():
    out, _ = vdj_write.sync_tracks_into_xml(DB, [_t(A, pois=[Poi(3.0, 2, "cue")], playcount=4)])

    assert _vdj_conventions(out) == CLEAN
    assert ' </Song>\n <Song FilePath="D:\\Music\\b.mp3"' in out
    assert '  <Poi Pos="3.000000" Num="2" Type="cue" />\n </Song>' in out


def test_a_new_song_has_no_empty_fields_and_no_unknown_size():
    out, created = vdj_write.sync_tracks_into_xml(
        DB, [_t(r"D:\Music\gone.mp3", title="New", artist="", album="", genre="")])

    assert created == {matching.path_key(r"D:\Music\gone.mp3"): True}
    assert _vdj_conventions(out) == CLEAN
    block = re.search(r' <Song FilePath="D:\\Music\\gone.mp3".*?</Song>', out, re.S).group(0)
    assert "FileSize" not in block, "an unknown size is left out, as VirtualDJ does"
    assert 'Title="New"' in block and "Author" not in block and "Comment" not in block


def test_a_new_song_records_the_real_size(tmp_path):
    f = tmp_path / "x.mp3"
    f.write_bytes(bytes(321))
    out, _ = vdj_write.sync_tracks_into_xml(DB, [_t(str(f), title="X")])

    assert 'FileSize="321"' in out


def test_clearing_a_field_removes_the_attribute():
    out, _ = vdj_write.set_songs_metadata(DB, {matching.path_key(A): {"title": "", "comment": "hi"}})

    assert _vdj_conventions(out) == CLEAN
    block = re.search(r' <Song FilePath="D:\\Music\\a.mp3".*?</Song>', out, re.S).group(0)
    assert "Title=" not in block and "  <Comment>hi</Comment>\n" in block


def test_removing_a_song_leaves_no_trace_of_its_line():
    out, removed = vdj_write.remove_song_from_xml(DB, matching.path_key(A))

    assert removed
    assert out == DB.replace(DB[DB.index(' <Song FilePath="D:\\Music\\a.mp3"'):
                                DB.index(' <Song FilePath="D:\\Music\\b.mp3"')], "")


def test_text_outside_a_child_is_not_guessed_at():
    odd = "<Song FilePath=\"D:\\x.mp3\">", "\n  stray text\n  <Tags Title=\"\" />\n", "</Song>"
    assert vdj_write._tidy_song(*odd) == "".join(odd)


# ===================== one VirtualDJ database per drive =======================
def _adapter(xml_path):
    from trackcleaner.adapters.vdj import VdjAdapter
    from trackcleaner.config.paths import Paths
    return VdjAdapter(Paths(xml=xml_path))


def test_a_drive_database_holds_only_that_drives_songs():
    a = _adapter(r"D:\VirtualDJ\database.xml")

    assert a.accepts_path(r"D:\Music\a.mp3")
    assert not a.accepts_path(r"C:\Users\x\Desktop\a.mp3")
    assert not a.accepts_path(r"E:\a.mp3")


def test_any_other_database_location_holds_any_path(tmp_path):
    """The home database (or one elsewhere) is filed by settings this app does
    not read, so it is not second-guessed."""
    a = _adapter(str(tmp_path / "Documents" / "VirtualDJ" / "database.xml"))

    assert a.accepts_path(r"F:\video.mp4")
    assert a.accepts_path(r"D:\Music\a.mp3")


def test_other_programs_hold_any_path():
    from trackcleaner.adapters.traktor import TraktorAdapter
    from trackcleaner.config.paths import Paths
    assert TraktorAdapter(Paths(nml=r"C:\x.nml")).accepts_path(r"Z:\anything.mp3")
