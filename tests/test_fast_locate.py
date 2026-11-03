r"""Finding one entry must not cost a walk of the whole collection.

A single-track sync took ~5 seconds, and none of it was about the track: every
phase is proportional to the collection file. 0.61 s of it went on ``re.sub``
visiting all 82,366 ``<Song>`` blocks to reach one, and 2.38 s on the Traktor
side visiting 83,427 ``<ENTRY>`` blocks in a 132 MB file.

So a small batch locates its blocks by searching for the attribute the writer
would itself emit, and only walks when that cannot be trusted. **The fast route
changes how a block is found, never how it is edited** -- the hit is re-matched
with the same pattern and re-keyed with the same comparison the walk uses, so it
can only ever be wrong by being slow.

Byte-identity is therefore the property under test: for every shape that could
plausibly confuse the search, the located write and the walked write must
produce the same file, character for character.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.io import traktor_write, vdj_write                   # noqa: E402
from trackcleaner.model import matching                                # noqa: E402
from trackcleaner.model.cues import Poi                                # noqa: E402
from trackcleaner.model.library import Track                           # noqa: E402
from fixtures import SAMPLE_NML                                        # noqa: E402


def _track(path, pois=(Poi(1.25, 1, "cue"),), **kw):
    return Track(key=matching.path_key(path), path=path, source="",
                 pois=list(pois), **kw)


def _xml(paths):
    """A database.xml holding one <Song> per path, rendered by the real writer."""
    empty = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<VirtualDJ_Database Version="2023">\n</VirtualDJ_Database>\n')
    out, _ = vdj_write.sync_tracks_into_xml(empty, [_track(p, pois=[])
                                                   for p in paths])
    return out


def _nml(paths):
    empty = ('<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n'
             '<NML VERSION="19">\n<COLLECTION ENTRIES="0"></COLLECTION>\n'
             '<PLAYLISTS><NODE TYPE="FOLDER" NAME="$ROOT"><SUBNODES COUNT="0">'
             '</SUBNODES></NODE></PLAYLISTS>\n</NML>\n')
    out, _ = traktor_write.sync_tracks_into_nml(empty, [_track(p, pois=[])
                                                       for p in paths])
    return out


def _both_ways(module, writer, content, tracks):
    """``(fast, walked)`` -- the same write with the fast locate on and off."""
    fast = writer(content, tracks)
    original = module._FAST_LOCATE_MAX
    module._FAST_LOCATE_MAX = 0                      # forces the walk
    try:
        walked = writer(content, tracks)
    finally:
        module._FAST_LOCATE_MAX = original
    return fast, walked


def _agree(module, writer, content, tracks):
    (fast_out, fast_answer), (walk_out, walk_answer) = _both_ways(
        module, writer, content, tracks)
    assert fast_out == walk_out, "the located write differs from the walked one"
    assert fast_answer == walk_answer
    return fast_out


# ============================== VirtualDJ ==================================
VDJ_CASES = {
    "ordinary": [r"D:\Music\Funk\Track.mp3"],
    "ampersand": [r"D:\Music\Bacao Rhythm & Steel Band.mp3"],
    "entity in the name": [r"D:\Music\Justice - Safe &amp; Sound.mp3"],
    "apostrophe": [r"D:\Music\Cee Walkin'.mp3"],
    "same basename, two folders": [r"D:\Music\A\Track.mp3",
                                   r"D:\Music\B\Track.mp3"],
    "several at once": [r"D:\Music\One.mp3", r"D:\Music\Two.mp3",
                        r"D:\Music\Three.mp3"],
}


@pytest.mark.parametrize("name", list(VDJ_CASES))
def test_a_located_song_write_matches_the_walked_one(name):
    paths = VDJ_CASES[name]
    xml = _xml(paths)

    _agree(vdj_write, vdj_write.sync_tracks_into_xml, xml,
           [_track(p) for p in paths])


def test_a_song_that_is_not_there_is_still_appended():
    xml = _xml([r"D:\Music\Here.mp3"])
    absent = _track(r"D:\Music\Absent.mp3")

    out = _agree(vdj_write, vdj_write.sync_tracks_into_xml, xml, [absent])

    assert out.count("<Song ") == 2


def test_a_miss_gives_up_rather_than_calling_the_track_absent():
    """The bug this guard exists for: a path has more than one legal encoding,
    so failing to find the text does **not** mean the database lacks the track.

    Here the file stores the ampersand as ``&#38;`` while this module writes
    ``&amp;``. The search misses; the walk decodes and finds it. Reporting
    "absent" would append a second ``<Song>`` for a track already held.
    """
    path = r"D:\Music\Rock & Roll.mp3"
    xml = _xml([path]).replace("&amp;", "&#38;")
    key = matching.path_key(path)

    assert vdj_write.locate_songs(xml, {key: path}) is None

    out, created = vdj_write.sync_tracks_into_xml(xml, [_track(path)])
    assert created[key] is False, "found by the walk, not appended"
    assert out.count("<Song ") == 1


def test_two_songs_claiming_one_path_fall_back_to_the_walk():
    """The walk rewrites both blocks; the fast route would rewrite one and call
    it done, so it declines instead."""
    one = _xml([r"D:\Music\Track.mp3"])
    block = re.search(r"<Song\b.*?</Song>", one, re.S).group(0)
    doubled = one.replace("</VirtualDJ_Database>",
                          block + "\n</VirtualDJ_Database>")
    track = _track(r"D:\Music\Track.mp3")

    assert vdj_write.locate_songs(doubled, {track.key: track.path}) is None

    out = _agree(vdj_write, vdj_write.sync_tracks_into_xml, doubled, [track])
    assert out.count("<Poi ") == 2, "the walk rewrites both, so both get the cue"


def test_the_name_appearing_in_a_comment_is_not_mistaken_for_a_song():
    """``FilePath="…"`` is ordinary text inside a ``<Comment>`` and needs no
    escaping, so the search really can land in a body.

    Asserts the outcome, not which guard produced it: the opening-tag check and
    the key check overlap here and either one alone would refuse this document.
    """
    path = r"D:\Music\Track.mp3"
    xml = _xml([r"D:\Music\Other.mp3"]).replace(
        "</Song>", f'  <Comment>see FilePath="{path}"</Comment>\n</Song>', 1)
    track = _track(path)

    assert vdj_write.locate_songs(xml, {track.key: track.path}) is None

    _agree(vdj_write, vdj_write.sync_tracks_into_xml, xml, [track])


def test_a_hit_whose_key_disagrees_is_refused():
    """A last-resort invariant: the block the search landed on must be the block
    the edit pattern reads.

    Contrived to reach -- it needs a tag carrying two ``FilePath`` attributes,
    where the search stops at the first and :data:`_SONG_BLOCK_RE`'s greedy
    prefix reads the last. The check costs one dictionary comparison and is the
    only thing standing between "found the wrong block" and writing one track's
    cues onto another.
    """
    xml = _xml([r"D:\Music\Real.mp3"])
    wanted = _track(r"D:\Music\Real.mp3")
    tampered = xml.replace('<Song FilePath="D:\\Music\\Real.mp3"',
                           '<Song FilePath="D:\\Music\\Real.mp3" '
                           'FilePath="D:\\Music\\Decoy.mp3"', 1)

    assert vdj_write.locate_songs(tampered, {wanted.key: wanted.path}) is None


def test_a_big_batch_uses_the_walk():
    """Past the crossover, locating each block costs more than one pass."""
    paths = [rf"D:\Music\T{i:03d}.mp3" for i in range(vdj_write._FAST_LOCATE_MAX + 1)]
    xml = _xml(paths)

    assert vdj_write.locate_songs(xml, {}) == {}, "the helper itself still works"
    _agree(vdj_write, vdj_write.sync_tracks_into_xml, xml,
           [_track(p) for p in paths])


# =============================== Traktor ===================================
NML_CASES = {
    "ordinary": [r"D:\Music\Funk\Track.mp3"],
    "ampersand": [r"D:\Music\Bacao Rhythm & Steel Band.mp3"],
    "entity in the name": [r"D:\Music\Justice - Safe &amp; Sound.mp3"],
    "apostrophe": [r"D:\Music\Cee Walkin'.mp3"],
    "several at once": [r"D:\Music\One.mp3", r"D:\Music\Two.mp3"],
}


@pytest.mark.parametrize("name", list(NML_CASES))
def test_a_located_entry_write_matches_the_walked_one(name):
    paths = NML_CASES[name]
    nml = _nml(paths)

    _agree(traktor_write, traktor_write.sync_tracks_into_nml, nml,
           [_track(p) for p in paths])


def test_the_same_filename_in_two_folders_picks_the_right_entry():
    """A Traktor needle is only the FILE attribute, so it hits both entries and
    the full-path key is what tells them apart. Getting this wrong writes one
    track's cues onto another track."""
    a, b = r"D:\Music\A\Track.mp3", r"D:\Music\B\Track.mp3"
    nml = _nml([a, b])

    out = _agree(traktor_write, traktor_write.sync_tracks_into_nml, nml,
                 [_track(a, pois=[Poi(9.5, 3, "cue")])])

    entries = re.findall(r"<ENTRY\b.*?</ENTRY>", out, re.S)
    got = {re.search(r'DIR="([^"]*)"', e).group(1): "CUE_V2" in e
           for e in entries if "<LOCATION" in e}
    assert got == {"/:Music/:A/:": True, "/:Music/:B/:": False}


def test_an_entry_that_is_not_there_is_still_appended():
    nml = _nml([r"D:\Music\Here.mp3"])

    out = _agree(traktor_write, traktor_write.sync_tracks_into_nml, nml,
                 [_track(r"D:\Music\Absent.mp3")])

    assert 'ENTRIES="2"' in out


def test_a_shared_filename_must_not_pull_in_the_wrong_entry():
    """The needle is only ``FILE=``, so it hits every folder holding that name.
    Here the wanted track is **absent** and a namesake is present: without the
    full-path key check the namesake's entry gets this track's cues.
    """
    present = r"D:\Music\A\Track.mp3"
    wanted = _track(r"D:\Music\B\Track.mp3", pois=[Poi(9.5, 3, "cue")])
    nml = _nml([present])

    assert traktor_write.locate_entries(nml, {wanted.key: wanted.path}) is None

    out, created = traktor_write.sync_tracks_into_nml(nml, [wanted])
    assert created[wanted.key] is True, "appended, not written onto the namesake"
    entry_a = re.search(r'<ENTRY\b(?:(?!</ENTRY>).)*?/:Music/:A/:.*?</ENTRY>',
                        out, re.S).group(0)
    assert "CUE_V2" not in entry_a, "the namesake keeps its own cues"


def test_two_entries_for_one_path_fall_back_to_the_walk():
    """The walk rewrites both; taking the first and stopping would leave the
    second holding stale cues."""
    path = r"D:\Music\Track.mp3"
    one = _nml([path])
    block = re.search(r"<ENTRY\b.*?</ENTRY>", one, re.S).group(0)
    doubled = one.replace("</COLLECTION>", block + "\n</COLLECTION>")
    track = _track(path)

    assert traktor_write.locate_entries(doubled, {track.key: track.path}) is None

    out = _agree(traktor_write, traktor_write.sync_tracks_into_nml,
                 doubled, [track])
    assert out.count("<CUE_V2") == 2, "the walk gives both the cue"


def test_a_miss_gives_up_rather_than_calling_the_entry_absent():
    """Same rule as the VirtualDJ side: an encoding this module would not have
    written is still the same path, and the walk decodes before comparing."""
    path = r"D:\Music\Rock & Roll.mp3"
    nml = _nml([path]).replace("&amp;", "&#38;")
    track = _track(path)

    assert traktor_write.locate_entries(nml, {track.key: track.path}) is None

    out, created = traktor_write.sync_tracks_into_nml(nml, [track])
    assert created[track.key] is False
    assert out.count("<LOCATION") == 1


def test_a_playlist_entry_block_is_never_mistaken_for_a_collection_one():
    """``<ENTRY><PRIMARYKEY …></ENTRY>`` inside a playlist matches the same
    pattern and carries no LOCATION; it must never be picked as a track."""
    track = _track(r"D:\VirtualDJ\02 Homenaje.mp3")

    out = _agree(traktor_write, traktor_write.sync_tracks_into_nml,
                 SAMPLE_NML, [track])

    assert out.count("<PRIMARYKEY") == SAMPLE_NML.count("<PRIMARYKEY")


def test_a_name_shared_by_too_many_entries_falls_back():
    """Past a handful of candidates, keying each one is no longer cheaper than
    walking."""
    paths = [rf"D:\Music\D{i}\Same.mp3"
             for i in range(traktor_write._MAX_CANDIDATES + 2)]
    nml = _nml(paths)

    assert traktor_write.locate_entries(
        nml, {matching.path_key(paths[0]): paths[0]}) is None
    _agree(traktor_write, traktor_write.sync_tracks_into_nml, nml,
           [_track(paths[0])])


# ========================= the metadata writers too ==========================
# Same property, same guards, different edit. These walk unless a caller hands
# them the paths -- they are keyed by track key alone, which is a *lowercased*
# path and so has no exact-case attribute to search for. Supplying the path is
# what turns a 2.29 s walk of a 133.5 MB collection into a substring search, and
# it is the whole of a backfilling sync's `source write` phase.

def _meta_both_ways(module, writer, content, by_key, paths):
    """``(with paths, without)`` -- located and walked, same edit either way."""
    return writer(content, by_key, paths), writer(content, by_key)


def _meta_agree(module, writer, content, by_key, paths):
    (fast_out, fast_answer), (walk_out, walk_answer) = _meta_both_ways(
        module, writer, content, by_key, paths)
    assert fast_out == walk_out, "the located edit differs from the walked one"
    assert fast_answer == walk_answer
    return fast_out


@pytest.mark.parametrize("name", list(VDJ_CASES))
def test_a_located_song_metadata_write_matches_the_walked_one(name):
    paths = VDJ_CASES[name]
    xml = _xml(paths)
    by_key = {matching.path_key(p): {"genre": "Edited", "comment": "Also edited"}
              for p in paths}

    _meta_agree(vdj_write, vdj_write.set_songs_metadata, xml, by_key,
                {matching.path_key(p): p for p in paths})


@pytest.mark.parametrize("name", list(NML_CASES))
def test_a_located_entry_metadata_write_matches_the_walked_one(name):
    paths = NML_CASES[name]
    nml = _nml(paths)
    by_key = {matching.path_key(p): {"genre": "Edited", "comment": "Also edited"}
              for p in paths}

    _meta_agree(traktor_write, traktor_write.set_entries_metadata, nml, by_key,
                {matching.path_key(p): p for p in paths})


def test_metadata_for_a_track_that_is_not_there_reports_the_same_either_way():
    """A key nothing matches must answer False, not be quietly located."""
    xml = _xml([r"D:\Music\Here.mp3"])
    absent = matching.path_key(r"D:\Music\Absent.mp3")

    out = _meta_agree(vdj_write, vdj_write.set_songs_metadata, xml,
                      {absent: {"genre": "X"}},
                      {absent: r"D:\Music\Absent.mp3"})

    assert out == xml, "nothing matched, so nothing was written"


def test_a_metadata_path_that_is_wrong_still_writes_the_right_entry():
    """The path is a hint for *finding*, never for choosing. Point it at another
    file and the locate must refuse rather than edit that one."""
    nml = _nml([r"D:\Music\Real.mp3", r"D:\Music\Decoy.mp3"])
    key = matching.path_key(r"D:\Music\Real.mp3")

    out = _meta_agree(traktor_write, traktor_write.set_entries_metadata, nml,
                      {key: {"genre": "Edited"}},
                      {key: r"D:\Music\Decoy.mp3"})

    decoy = re.search(r'<ENTRY\b(?:(?!</ENTRY>).)*?Decoy\.mp3.*?</ENTRY>',
                      out, re.S).group(0)
    assert "Edited" not in decoy, "the decoy must not have been edited"


def test_a_partial_set_of_paths_falls_back_to_the_walk():
    """All or nothing: locating half a batch and walking for the rest would be
    two passes, which is worse than the one it was trying to avoid."""
    paths = [r"D:\Music\One.mp3", r"D:\Music\Two.mp3"]
    xml = _xml(paths)
    by_key = {matching.path_key(p): {"genre": "Edited"} for p in paths}

    _meta_agree(vdj_write, vdj_write.set_songs_metadata, xml, by_key,
                {matching.path_key(paths[0]): paths[0]})       # only one path


def test_an_nml_partial_set_of_paths_falls_back_to_the_walk():
    """All or nothing, on the Traktor side too."""
    paths = [r"D:\Music\One.mp3", r"D:\Music\Two.mp3"]
    nml = _nml(paths)
    by_key = {matching.path_key(p): {"genre": "Edited"} for p in paths}

    _meta_agree(traktor_write, traktor_write.set_entries_metadata, nml, by_key,
                {matching.path_key(paths[0]): paths[0]})       # only one path


@pytest.mark.parametrize("module,writer,build,over", [
    (vdj_write, vdj_write.set_songs_metadata, _xml, False),
    (vdj_write, vdj_write.set_songs_metadata, _xml, True),
    (traktor_write, traktor_write.set_entries_metadata, _nml, False),
    (traktor_write, traktor_write.set_entries_metadata, _nml, True),
])
def test_only_a_small_batch_is_worth_locating(module, writer, build, over,
                                              monkeypatch):
    """A performance contract, pinned on the call.

    Past the crossover, looking each entry up costs more than one pass of the
    document, so a large batch must go back to walking. Nothing observable
    changes either way -- which is exactly why it needs asserting here rather
    than being left to the byte-identity tests.
    """
    count = module._FAST_LOCATE_MAX + (1 if over else 0)
    paths = [rf"D:\Music\T{i:03d}.mp3" for i in range(count)]
    content = build(paths)
    by_key = {matching.path_key(p): {"genre": "Edited"} for p in paths}
    locator = "locate_songs" if module is vdj_write else "locate_entries"
    calls = []
    real = getattr(module, locator)
    monkeypatch.setattr(module, locator,
                        lambda c, bk: calls.append(len(bk)) or real(c, bk))

    writer(content, by_key, {matching.path_key(p): p for p in paths})

    assert calls == ([] if over else [count])


def test_metadata_without_paths_is_exactly_what_it_always_was():
    """The argument is optional, and a caller that omits it is not doing
    anything wrong -- most of the app has no path to hand."""
    paths = [r"D:\Music\One.mp3"]
    nml = _nml(paths)
    by_key = {matching.path_key(paths[0]): {"genre": "Edited"}}

    assert (traktor_write.set_entries_metadata(nml, by_key)
            == traktor_write.set_entries_metadata(nml, by_key, None))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
