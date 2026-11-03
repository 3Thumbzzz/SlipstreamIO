"""A VirtualDJ playlist row is VirtualDJ's own view of the song.

Reported: after an Overwrite from Traktor, VirtualDJ's playlists showed
Traktor's keys, BPMs and rounded lengths, every row carried ``size="0"``, and
``remix`` was gone -- 2,877 of 2,882 rows in "BTTB-ALL". Every membership write
rebuilt every row from the ``Track`` it was handed, and the sync handed over
Traktor's copy. Now a row already in the playlist is kept exactly, a new row
is drawn from VirtualDJ's own entry where there is one, and a new row carries
the file's real size.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.io import vdj_write                                   # noqa: E402
from trackcleaner.model import matching                                 # noqa: E402
from trackcleaner.model.library import Library, Track, TRAKTOR, VDJ     # noqa: E402
from trackcleaner.sync.engine import _target_view                       # noqa: E402

A = r"D:\Music\a.mp3"
B = r"D:\Music\b.mp3"
EXISTING = ('<?xml version="1.0" encoding="UTF-8"?>\r\n'
            '<VirtualFolder noDuplicates="no">\r\n'
            '\t<song path="D:\\Music\\a.mp3" size="6262337" songlength="156.0" bpm="131.904" '
            'key="Dm" artist="Mr. Carmack" title="Blouses" remix="Flip" idx="0" />\r\n'
            '\t<song path="D:\\Music\\b.mp3" size="8978692" songlength="216.8" bpm="103.001" '
            'key="G" artist="Lauryn Hill" title="Doo Wop" idx="1" />\r\n'
            '</VirtualFolder>\r\n')


def _t(path, **kw):
    return Track(key=matching.path_key(path), path=path, source=TRAKTOR, **kw)


def _rows(text):
    return [line.strip() for line in text.splitlines() if line.strip().startswith("<song")]


def test_a_row_already_in_the_playlist_is_kept_exactly():
    traktor_copy = [_t(B, bpm=103.0, length=217.0, title="Doo Wop", artist="Lauryn Hill"),
                    _t(A, bpm=131.901, length=157.0, title="Blouses", artist="Mr. Carmack")]

    out = vdj_write.render_vdjfolder(traktor_copy, False, EXISTING)

    before = _rows(EXISTING)
    assert _rows(out) == [before[1].replace('idx="1"', 'idx="0"'),
                          before[0].replace('idx="0"', 'idx="1"')], \
        "reordered, renumbered, and otherwise VirtualDJ's own rows"


def test_a_repeated_track_reuses_its_rows_in_order():
    twice = EXISTING.replace("</VirtualFolder>",
                             '\t<song path="D:\\Music\\a.mp3" size="1" title="second" idx="2" />\r\n'
                             "</VirtualFolder>")
    out = vdj_write.render_vdjfolder([_t(A), _t(A)], False, twice)

    rows = _rows(out)
    assert 'title="Blouses"' in rows[0] and 'title="second"' in rows[1]


def test_a_new_row_gets_the_files_real_size(tmp_path):
    audio = tmp_path / "new.mp3"
    audio.write_bytes(bytes(1234))

    out = vdj_write.render_vdjfolder([_t(str(audio), title="New")], False, EXISTING)

    assert 'size="1234"' in _rows(out)[0]


def test_a_new_row_for_a_missing_file_says_size_zero():
    out = vdj_write.render_vdjfolder([_t(r"Z:\gone.mp3")], False, None)

    assert 'size="0"' in _rows(out)[0]


def test_the_adapter_keeps_the_rows_of_the_file_it_rewrites(tmp_path):
    from trackcleaner.adapters.vdj import VdjAdapter
    from trackcleaner.config.paths import Paths
    from trackcleaner.model.library import Playlist, TreeNode
    f = tmp_path / "Folders" / "p.vdjfolder"
    f.parent.mkdir()
    f.write_text(EXISTING, encoding="utf-8", newline="")
    (tmp_path / "database.xml").write_text("<VirtualDJ_Database/>")
    adapter = VdjAdapter(Paths(xml=str(tmp_path / "database.xml")))
    node = TreeNode(name="p", kind="playlist", source=VDJ, node_path=("p",),
                    playlist=Playlist(name="p", source=VDJ, vdj_file=str(f),
                                      track_keys=[matching.path_key(A), matching.path_key(B)]))

    adapter.write_membership_job(node, [_t(A, title="Traktor's"), _t(B)])()

    text = f.read_text(encoding="utf-8")
    assert 'key="Dm"' in text and 'remix="Flip"' in text and 'size="6262337"' in text
    assert "Traktor's" not in text


def test_a_sync_draws_new_rows_from_the_targets_own_entry():
    vdj_own = Track(key=matching.path_key(A), path=A, source=VDJ, bpm=131.904,
                    key_musical="Dm", in_database=True)
    target = Library(source=VDJ, tracks={vdj_own.key: vdj_own})
    traktor = _t(A, bpm=131.9)
    elsewhere = _t(B)

    assert _target_view(target, traktor) is vdj_own
    assert _target_view(target, elsewhere) is elsewhere, "not in the target: the source's"
