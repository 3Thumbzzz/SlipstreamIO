"""One walk of the collection per run, not one per track.

Batching the *I/O* was not enough. A write still had to **find** its entry, and
finding it meant walking every entry in the document: `_ENTRY_RE.sub` over the
whole file, asking "is this the one?" of each. That is O(tracks x entries), and a
profile of one 1005-track run put **46.6 of its 110.9 seconds inside
`re.Pattern.sub`**, with `_entry_matches` called 5,050,750 times.

So the writers take many tracks and walk once. The property that makes that safe
is the one tested hardest here: **the batched result is byte-for-byte what
writing the same tracks one at a time produces.** Each track only ever touches
its own entry, so applying them together cannot change the outcome — and if that
ever stops being true, these fail.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.io import traktor_read, traktor_write, vdj_read, vdj_write  # noqa: E402
from trackcleaner.model.cues import Poi                                  # noqa: E402
from trackcleaner.model.library import Track, TRAKTOR, VDJ               # noqa: E402
from fixtures import write_sample                                        # noqa: E402


@pytest.fixture
def sample(tmp_path):
    paths = write_sample(tmp_path)
    return (paths,
            open(paths.nml, encoding="utf-8").read(),
            open(paths.xml, encoding="utf-8").read())


def _traktor_tracks(paths):
    return list(traktor_read.read(paths.nml).tracks.values())


def _edited(tracks, n=0):
    """The same tracks with a different cue set, so a write really changes
    something."""
    out = []
    for i, t in enumerate(tracks):
        out.append(Track(**{**t.__dict__,
                            "pois": [Poi(1.0 + i + n, None, "beatgrid"),
                                     Poi(9.0 + i + n, 1, "cue")]}))
    return out


# ==================== the batched file is the unbatched file ================
def test_traktor_cues_batched_are_identical_to_one_at_a_time(sample):
    paths, nml, _ = sample
    tracks = _edited(_traktor_tracks(paths))
    assert len(tracks) >= 2, "one track cannot tell the two apart"

    one_at_a_time = nml
    for t in tracks:
        one_at_a_time, _ = traktor_write.sync_track_into_nml(one_at_a_time, t)
    all_at_once, created = traktor_write.sync_tracks_into_nml(nml, tracks)

    assert all_at_once == one_at_a_time
    assert created == {t.key: False for t in tracks}


def test_traktor_metadata_batched_is_identical(sample):
    paths, nml, _ = sample
    tracks = _traktor_tracks(paths)
    by_key = {t.key: {"genre": f"G{i}", "comment": f"C{i}"}
              for i, t in enumerate(tracks)}

    one_at_a_time = nml
    for key, fields in by_key.items():
        one_at_a_time, _ = traktor_write.set_entry_metadata(
            one_at_a_time, key, fields)
    all_at_once, changed = traktor_write.set_entries_metadata(nml, by_key)

    assert all_at_once == one_at_a_time
    assert changed == {k: True for k in by_key}


def test_virtualdj_cues_batched_are_identical(sample):
    """The sample database starts with no <Song> blocks, so this covers both
    halves at once: appending the entries, then editing the entries."""
    paths, _, xml = sample
    tracks = _edited(_traktor_tracks(paths))
    assert len(tracks) >= 2, "one track cannot tell the two apart"

    one_at_a_time = xml
    for t in tracks:
        one_at_a_time, _ = vdj_write.sync_track_into_xml(one_at_a_time, t)
    all_at_once, created = vdj_write.sync_tracks_into_xml(xml, tracks)
    assert all_at_once == one_at_a_time, "appending differs"
    assert created == {t.key: True for t in tracks}

    again = _edited(tracks, n=100)
    one_at_a_time_2 = all_at_once
    for t in again:
        one_at_a_time_2, _ = vdj_write.sync_track_into_xml(one_at_a_time_2, t)
    all_at_once_2, created_2 = vdj_write.sync_tracks_into_xml(all_at_once, again)
    assert all_at_once_2 == one_at_a_time_2, "editing differs"
    assert created_2 == {t.key: False for t in again}


def test_virtualdj_metadata_batched_is_identical(sample):
    paths, _, xml = sample
    songs = _traktor_tracks(paths)
    xml, _ = vdj_write.sync_tracks_into_xml(xml, songs)   # give it some <Song>s
    by_key = {t.key: {"genre": f"G{i}", "comment": f"C{i}"}
              for i, t in enumerate(songs)}

    one_at_a_time = xml
    for key, fields in by_key.items():
        one_at_a_time, _ = vdj_write.set_song_metadata(one_at_a_time, key, fields)
    all_at_once, _ = vdj_write.set_songs_metadata(xml, by_key)

    assert all_at_once == one_at_a_time


# ============================ appending new entries =========================
def _new(path, title="New"):
    return Track(key=path.lower(), path=path, source=VDJ, title=title,
                 pois=[Poi(1.0, None, "beatgrid")])


def test_missing_entries_are_appended_once_each(sample):
    _paths, nml, _ = sample
    before = int(re.search(r'ENTRIES="(\d+)"', nml).group(1))
    tracks = [_new(r"D:\Music\A.mp3"), _new(r"D:\Music\B.mp3")]

    out, created = traktor_write.sync_tracks_into_nml(nml, tracks)

    assert created == {t.key: True for t in tracks}
    assert int(re.search(r'ENTRIES="(\d+)"', out).group(1)) == before + 2
    for t in tracks:
        assert out.count(f'FILE="{os.path.basename(t.path)}"') == 1


def test_a_track_named_twice_is_appended_once(sample):
    """A playlist may hold the same path twice. One entry results, carrying the
    last one's data -- the same end state as today, where the second write
    simply overwrote the first."""
    _paths, nml, _ = sample
    before = int(re.search(r'ENTRIES="(\d+)"', nml).group(1))
    twice = [_new(r"D:\Music\Dup.mp3", "First"), _new(r"D:\Music\Dup.mp3", "Last")]

    out, created = traktor_write.sync_tracks_into_nml(nml, twice)

    assert int(re.search(r'ENTRIES="(\d+)"', out).group(1)) == before + 1
    assert out.count('FILE="Dup.mp3"') == 1
    assert 'TITLE="Last"' in out and 'TITLE="First"' not in out
    assert created == {twice[0].key: True}


def test_the_entries_count_is_bumped_only_by_what_landed(sample):
    """A batch of one new and one existing track adds one, not two."""
    paths, nml, _ = sample
    before = int(re.search(r'ENTRIES="(\d+)"', nml).group(1))
    existing = _edited(_traktor_tracks(paths))[:1]

    out, created = traktor_write.sync_tracks_into_nml(
        nml, existing + [_new(r"D:\Music\Solo.mp3")])

    assert sum(created.values()) == 1
    assert int(re.search(r'ENTRIES="(\d+)"', out).group(1)) == before + 1


# ======================= what the writers must not touch ====================
def test_a_batch_that_matches_nothing_leaves_the_file_alone(sample):
    _paths, nml, _ = sample
    out, changed = traktor_write.set_entries_metadata(
        nml, {"d:/nowhere/at/all.mp3": {"genre": "X"}})
    assert out == nml
    assert changed == {"d:/nowhere/at/all.mp3": False}


def test_traktor_only_markers_survive_a_batched_write(sample):
    """The binding rule: what we do not understand is carried through
    byte-for-byte. Loop/fade/load markers are Traktor's, not ours."""
    from trackcleaner.model.cues import CUE_V2_RE, is_owned_cue_v2
    paths, nml, _ = sample
    # A loop and a load marker, which are Traktor's own and not ours to touch.
    theirs = ('<CUE_V2 NAME="Loop" DISPL_ORDER="0" TYPE="5" START="1000.0" '
              'LEN="4000.0" REPEATS="-1" HOTCUE="3"></CUE_V2>\n'
              '<CUE_V2 NAME="Fade In" DISPL_ORDER="0" TYPE="1" START="2000.0" '
              'LEN="0.0" REPEATS="-1" HOTCUE="-1"></CUE_V2>\n')
    tracks = _traktor_tracks(paths)
    marked = nml.replace("</ENTRY>", theirs + "</ENTRY>", 1)
    kept = [m.group(0) for m in CUE_V2_RE.finditer(marked)
            if not is_owned_cue_v2(m.group(0))]
    assert kept, "the fixture has to carry something we do not own"

    out, _ = traktor_write.sync_tracks_into_nml(marked, _edited(tracks))

    for marker in kept:
        assert marker in out, marker


def test_playlist_entries_are_never_matched(sample):
    """``<ENTRY><PRIMARYKEY…></ENTRY>`` blocks inside playlists carry no
    LOCATION, so they have no key and a batch can never land on one."""
    _paths, nml, _ = sample
    for block in re.findall(r"<ENTRY\b[^>]*>(?!.*?<LOCATION).*?</ENTRY>", nml,
                            re.DOTALL):
        assert traktor_write._entry_key(block) == ""


def test_an_entry_key_is_the_same_answer_the_old_test_asked(sample):
    """`_entry_matches` is still right -- it is just asked once per entry now
    instead of once per entry per track."""
    paths, nml, _ = sample
    for track in _traktor_tracks(paths):
        block = next(b for b in re.findall(r"<ENTRY\b.*?</ENTRY>", nml, re.DOTALL)
                     if traktor_write._entry_key(b) == track.key)
        assert traktor_write._entry_matches(block, track.key)
        assert not traktor_write._entry_matches(block, "d:/nope.mp3")
