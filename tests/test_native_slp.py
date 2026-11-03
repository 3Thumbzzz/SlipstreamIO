"""The .slp format and the SlipStream adapter.

Two things are being pinned here. The first is that the format round-trips: what
goes in comes back out with the same identity, structure and positions, because
everything downstream -- sync through the native layer, SlipStream's own
playlists -- assumes it. The second is that the adapter behaves like every other
adapter, so the pane, the context menus and the write queue need no special case
for it.

Positions in a .slp are canonical seconds, so they are written and read
verbatim: SlipStream's own timeline offset is zero by definition.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.adapters.slipstream import SlipStreamAdapter          # noqa: E402
from trackcleaner.config.paths import Paths                             # noqa: E402
from trackcleaner.model.cues import Poi                                 # noqa: E402
from trackcleaner.model.library import Track, TreeNode                  # noqa: E402
from trackcleaner.native import slp                                     # noqa: E402


# ================================ helpers ===================================
@pytest.fixture
def lib_path(tmp_path):
    return str(tmp_path / "library.slp")


@pytest.fixture
def adapter(lib_path):
    return SlipStreamAdapter(Paths(slp=lib_path))


def _track(path, **kw):
    return Track(key=path.lower(), path=path, source=slp.SLIPSTREAM, **kw)


def _run(job):
    """Adapters return jobs; the queue runs them. Doing it inline keeps these
    tests about the format rather than about threading."""
    assert job is not None, "adapter refused the edit"
    job()


def _names(node):
    return [c.name for c in node.children]


def _find(lib, *node_path):
    node = lib.root
    for name in node_path:
        node = next(c for c in node.children if c.name == name)
    return node


# ============================ an absent library =============================
def test_a_missing_file_reads_as_an_empty_library(adapter, lib_path):
    """It is our own file: not having one yet means "empty", not "broken"."""
    assert not os.path.exists(lib_path)

    lib = adapter.read()

    assert lib.source == slp.SLIPSTREAM
    assert lib.tracks == {}
    assert lib.root.children == []


def test_a_corrupt_file_reads_as_empty_rather_than_raising(adapter, lib_path):
    with open(lib_path, "w", encoding="utf-8") as f:
        f.write("<SlipStream><PLAYLISTS>truncated")

    assert adapter.read().root.children == []


def test_a_location_is_enough_to_count_as_configured(adapter, lib_path):
    """Unlike a vendor library, the file need not exist -- the first write makes
    it. Requiring it to exist would leave SlipStream permanently switched off."""
    assert adapter.is_configured()
    assert not SlipStreamAdapter(Paths(slp="")).is_configured()


# ============================== the collection ==============================
def test_a_track_round_trips_through_the_collection(adapter):
    track = _track(r"D:\Music\Song.mp3", title="Song", artist="Artist",
                   album="Album", genre="House", comment="note",
                   key_musical="Am", bpm=128.0, length=210.5, playcount=3)

    _run(adapter.create_entry_job(track))

    back = adapter.read().tracks[track.key]
    assert (back.path, back.title, back.artist, back.album) == \
        (track.path, "Song", "Artist", "Album")
    assert (back.genre, back.comment, back.key_musical) == ("House", "note", "Am")
    assert back.bpm == pytest.approx(128.0)
    assert back.length == pytest.approx(210.5)
    assert back.playcount == 3


def test_cues_and_beatgrids_stay_separate_and_keep_their_positions(adapter):
    """The whole point of not copying NML: a beatgrid is a beatgrid, not a cue
    with TYPE=4 that might also be a hotcue depending on another attribute."""
    track = _track(r"D:\Music\Song.mp3", pois=[
        Poi(0.5, None, "beatgrid"),
        Poi(12.25, 1, "cue"),
        Poi(90.125, 4, "cue"),
    ])

    _run(adapter.create_entry_job(track))

    back = adapter.read().tracks[track.key]
    grids = [p for p in back.pois if p.kind == "beatgrid"]
    cues = sorted((p for p in back.pois if p.kind == "cue"), key=lambda p: p.pos)
    assert [p.pos for p in grids] == [pytest.approx(0.5)]
    assert [(p.num, p.pos) for p in cues] == \
        [(1, pytest.approx(12.25)), (4, pytest.approx(90.125))]


def test_positions_are_stored_verbatim_because_they_are_already_canonical(adapter):
    """SlipStream's timeline offset is zero by definition -- it *is* the native
    timeline -- so nothing may be shifted on the way in or out."""
    assert adapter.timeline_offset(r"D:\Music\Song.mp3") == 0.0
    assert adapter.to_native_pos(12.25, r"D:\Music\Song.mp3") == pytest.approx(12.25)
    assert adapter.from_native_pos(12.25, r"D:\Music\Song.mp3") == pytest.approx(12.25)


def test_upserting_the_same_path_replaces_rather_than_duplicates(adapter):
    _run(adapter.create_entry_job(_track(r"D:\Music\Song.mp3", title="First")))

    _run(adapter.create_entry_job(_track(r"D:\Music\Song.mp3", title="Second")))

    lib = adapter.read()
    assert len(lib.tracks) == 1
    assert next(iter(lib.tracks.values())).title == "Second"


def test_a_differently_cased_path_is_the_same_track(adapter):
    """Paths are matched on identity, not on the raw string, or a rescan that
    reported a different drive-letter case would silently double the library."""
    _run(adapter.create_entry_job(_track(r"D:\Music\Song.mp3", title="First")))

    _run(adapter.create_entry_job(_track(r"d:\music\song.MP3", title="Second")))

    assert len(adapter.read().tracks) == 1


def test_removing_an_entry(adapter):
    track = _track(r"D:\Music\Song.mp3")
    _run(adapter.create_entry_job(track))

    _run(adapter.remove_entry_job(track.key))

    assert adapter.read().tracks == {}


# =============================== playlists ==================================
def test_creating_a_playlist_at_the_root(adapter):
    tracks = [_track(r"D:\Music\A.mp3"), _track(r"D:\Music\B.mp3")]
    playlist, job = adapter.new_playlist("Set", ("Set",), tracks, None)
    _run(job)

    assert playlist.source == slp.SLIPSTREAM
    node = _find(adapter.read(), "Set")
    assert node.kind == "playlist"
    assert node.playlist.track_keys == [t.key for t in tracks]


def test_a_playlist_references_tracks_by_path(adapter, lib_path):
    """The chosen identity model: an entry names the file rather than pointing at
    a collection row, so nothing has to be renumbered when the collection
    changes."""
    _run(adapter.new_playlist("Set", ("Set",), [_track(r"D:\Music\A.mp3")], None)[1])

    with open(lib_path, encoding="utf-8") as f:
        text = f.read()
    assert r'PATH="D:\Music\A.mp3"' in text
    assert _find(adapter.read(), "Set").playlist.track_keys == [r"d:\music\a.mp3"]


# ============ the playlist that emptied itself (reported bug) ===============
# Symptom: sync a SlipStream playlist to another program, and the SlipStream
# copy comes back EMPTY -- then re-adding one of the tracks it used to hold
# shows the original AND a duplicate. Cause: entries name a path, but a row is
# only drawn for a key that resolves in the collection, so entries with no
# collection record vanished from view while staying in the file.
def test_a_playlist_member_is_added_to_the_collection(adapter):
    """Otherwise the entry has nothing to resolve to and the row is dropped."""
    _run(adapter.new_playlist("Set", ("Set",), [_track(r"D:\Music\A.mp3")], None)[1])

    assert r"d:\music\a.mp3" in adapter.read().tracks


def test_every_member_of_a_playlist_resolves_to_a_track(adapter):
    """The property the view actually depends on: each key in track_keys can be
    looked up. This is what "the playlist is empty" really meant."""
    tracks = [_track(r"D:\Music\A.mp3"), _track(r"D:\Music\B.mp3")]
    _run(adapter.new_playlist("Set", ("Set",), tracks, None)[1])
    _run(adapter.write_membership_job(_find(adapter.read(), "Set"), tracks))

    lib = adapter.read()
    keys = _find(lib, "Set").playlist.track_keys
    assert keys, "membership survived"
    assert all(lib.tracks.get(k) is not None for k in keys), \
        "every member must resolve, or its row is silently dropped"


def test_an_entry_with_no_collection_record_is_still_readable(adapter, lib_path):
    """Repairs libraries written before the collection record was added -- they
    must come back visible rather than staying permanently empty."""
    with open(lib_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<SlipStream VERSION="1"><COLLECTION/><PLAYLISTS>'
                '<PLAYLIST NAME="Set"><ENTRY PATH="D:\\Music\\Orphan.mp3"/>'
                '</PLAYLIST></PLAYLISTS></SlipStream>\n')

    lib = adapter.read()

    key = r"d:\music\orphan.mp3"
    assert _find(lib, "Set").playlist.track_keys == [key]
    assert lib.tracks[key].path == r"D:\Music\Orphan.mp3"
    assert lib.tracks[key].in_database is False, \
        "honest about being known only from a playlist"


def test_re_adding_a_member_does_not_leave_a_second_copy(adapter):
    """The duplicate half of the report: the original was invisible but present,
    so adding it again read as one row becoming two."""
    track = _track(r"D:\Music\A.mp3")
    _run(adapter.new_playlist("Set", ("Set",), [track], None)[1])

    # What the UI does on a re-add: rewrite membership to the keys it holds.
    node = _find(adapter.read(), "Set")
    assert node.playlist.track_keys == [track.key]
    _run(adapter.write_membership_job(node, [track]))

    assert _find(adapter.read(), "Set").playlist.track_keys == [track.key]


def test_adding_a_member_does_not_overwrite_what_we_already_know(adapter):
    """A drag carries whatever the source program had, which may be less than
    SlipStream's own record -- adding to a playlist must not downgrade it."""
    rich = _track(r"D:\Music\A.mp3", title="Real Title", artist="Real Artist",
                  bpm=128.0)
    _run(adapter.create_entry_job(rich))

    bare = _track(r"D:\Music\A.mp3")
    _run(adapter.new_playlist("Set", ("Set",), [bare], None)[1])

    back = adapter.read().tracks[rich.key]
    assert (back.title, back.artist) == ("Real Title", "Real Artist")
    assert back.bpm == pytest.approx(128.0)


def test_creating_a_playlist_inside_a_folder_creates_the_folder(adapter):
    _run(adapter.new_playlist("Set", ("Gigs", "Set"), [], None)[1])

    folder = _find(adapter.read(), "Gigs")
    assert folder.kind == "folder"
    assert _names(folder) == ["Set"]


def test_a_new_folder_nests_to_any_depth(adapter):
    _run(adapter.new_folder_job(("Gigs", "2026", "Summer")))

    assert _find(adapter.read(), "Gigs", "2026", "Summer").kind == "folder"


def test_rewriting_membership_replaces_the_entries(adapter):
    _run(adapter.new_playlist("Set", ("Set",), [_track(r"D:\Music\A.mp3")], None)[1])
    node = _find(adapter.read(), "Set")

    _run(adapter.write_membership_job(node, [_track(r"D:\Music\B.mp3"),
                                             _track(r"D:\Music\C.mp3")]))

    assert _find(adapter.read(), "Set").playlist.track_keys == \
        [r"d:\music\b.mp3", r"d:\music\c.mp3"]


def test_the_same_track_may_appear_twice(adapter):
    """A set can legitimately play a track twice; our own format has no reason
    to forbid what the vendors merely happen to."""
    track = _track(r"D:\Music\A.mp3")
    _run(adapter.new_playlist("Set", ("Set",), [track, track], None)[1])

    node = _find(adapter.read(), "Set")
    assert node.playlist.track_keys == [track.key, track.key]
    assert node.playlist.no_duplicates is False


# ============================ moving and renaming ===========================
def test_renaming_a_playlist_keeps_its_entries(adapter):
    _run(adapter.new_playlist("Set", ("Set",), [_track(r"D:\Music\A.mp3")], None)[1])
    node = _find(adapter.read(), "Set")

    _run(adapter.relocate_playlist_job(node, ("Renamed",), adapter.read()))

    lib = adapter.read()
    assert _names(lib.root) == ["Renamed"]
    assert _find(lib, "Renamed").playlist.track_keys == [r"d:\music\a.mp3"]


def test_moving_a_playlist_into_a_folder_does_not_leave_a_copy_behind(adapter):
    """The bug this mirrors on the vendor side: a move that was really a copy."""
    _run(adapter.new_playlist("Set", ("Set",), [], None)[1])
    _run(adapter.new_folder_job(("Gigs",)))
    node = _find(adapter.read(), "Set")

    _run(adapter.relocate_playlist_job(node, ("Gigs", "Set"), adapter.read()))

    lib = adapter.read()
    assert _names(lib.root) == ["Gigs"]
    assert _names(_find(lib, "Gigs")) == ["Set"]


def test_moving_a_folder_takes_its_contents(adapter):
    _run(adapter.new_playlist("Set", ("Gigs", "Set"), [], None)[1])
    _run(adapter.new_folder_job(("Archive",)))

    _run(adapter.relocate_folder_job(("Gigs",), ("Archive", "Gigs")))

    lib = adapter.read()
    assert _names(lib.root) == ["Archive"]
    assert _names(_find(lib, "Archive", "Gigs")) == ["Set"]


def test_relocating_something_that_is_not_there_changes_nothing(adapter,
                                                                lib_path):
    _run(adapter.new_playlist("Set", ("Set",), [], None)[1])
    before = open(lib_path, encoding="utf-8").read()

    _run(adapter.relocate_folder_job(("Ghost",), ("Elsewhere",)))

    assert open(lib_path, encoding="utf-8").read() == before


# ================================ removal ===================================
def test_removing_a_playlist(adapter):
    _run(adapter.new_playlist("Set", ("Set",), [], None)[1])
    node = _find(adapter.read(), "Set")

    _run(adapter.remove_job(node))

    assert adapter.read().root.children == []


def test_removing_a_folder_removes_what_is_in_it(adapter):
    _run(adapter.new_playlist("Set", ("Gigs", "Set"), [], None)[1])
    node = _find(adapter.read(), "Gigs")

    _run(adapter.remove_job(node))

    assert adapter.read().root.children == []


def test_removing_a_playlist_leaves_the_collection_alone(adapter):
    """Removing a playlist must never remove tracks -- the same rule the vendor
    adapters follow."""
    track = _track(r"D:\Music\A.mp3")
    _run(adapter.create_entry_job(track))
    _run(adapter.new_playlist("Set", ("Set",), [track], None)[1])
    node = _find(adapter.read(), "Set")

    _run(adapter.remove_job(node))

    assert list(adapter.read().tracks) == [track.key]


# ============================ jobs, not writes ==============================
def test_asking_for_an_edit_does_not_perform_it(adapter, lib_path):
    """Every adapter method returns a job for the queue. Writing on the calling
    thread would put file IO on the UI thread and skip the serialization that
    stops two edits interleaving."""
    job = adapter.new_folder_job(("Gigs",))

    assert not os.path.exists(lib_path), "nothing written until the job runs"
    job()
    assert os.path.exists(lib_path)


def test_a_job_reads_the_file_at_run_time_not_at_build_time(adapter):
    """Jobs are queued and run later, possibly after another job has already
    changed the file. Capturing the document when the job was built would make
    the second write silently drop the first."""
    first = adapter.new_folder_job(("First",))
    second = adapter.new_folder_job(("Second",))

    first()
    second()

    assert sorted(_names(adapter.read().root)) == ["First", "Second"]


# ========================= it is a program like any other ===================
def test_it_is_registered_alongside_the_vendor_programs():
    from trackcleaner.adapters import ADAPTER_IDS, display_name

    assert slp.SLIPSTREAM in ADAPTER_IDS
    assert display_name(slp.SLIPSTREAM) == "SlipStream"


def test_its_path_is_offered_in_the_paths_dialog():
    from trackcleaner.adapters import all_path_fields

    field = next(f for f in all_path_fields(Paths()) if f.key == "slp")
    assert not field.must_exist, "the file is created, not found"


def test_the_slp_path_persists_across_a_restart(tmp_path, monkeypatch):
    """It is a dataclass field like any other; the loader must not carry a
    hard-coded list of the paths it knows about."""
    from trackcleaner.config import paths as paths_mod
    from trackcleaner.config.paths import Settings

    monkeypatch.setattr(paths_mod, "settings_path",
                        lambda: str(tmp_path / "settings.json"))
    s = Settings()
    s.paths = Paths(nml="n.nml", xml="x.xml", slp=r"D:\lib.slp", csv="c.csv")
    s.save()

    assert Settings.load().paths.slp == r"D:\lib.slp"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
