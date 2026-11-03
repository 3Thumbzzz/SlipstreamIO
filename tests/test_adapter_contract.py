"""What every adapter must do, asserted once for all of them.

This module is the payoff of the adapter layer. Each test here is parametrised
over **every registered adapter**, so a new program is correct when it passes
this file -- there is no new test module to write, and no existing program's
tests to re-run and re-read. If you add an adapter and something here fails, the
adapter is wrong; if something here needs a per-program exception, the contract
is wrong and should be narrowed rather than special-cased.

Format-specific behaviour does *not* belong here. NML surgery, `.vdjfolder`
rendering, the Class-A correction and the `.slp` schema each have their own
module. What is shared is the shape of the interface: reads produce a Library,
writes produce jobs, node paths round-trip, and removing a playlist never
touches the collection.
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.adapters import (ADAPTER_CLASSES, ADAPTER_IDS,             # noqa: E402
                                   LibraryAdapter, all_path_fields,
                                   build_adapters, display_name)
from trackcleaner.config.paths import Paths                                  # noqa: E402
from trackcleaner.model.library import Library, Track                        # noqa: E402
from fake_adapter import FAKE, FakeAdapter                                   # noqa: E402
from fixtures import write_sample                                            # noqa: E402

# Every registered program, plus the fake. The fake is the control case: it has
# no file, no format and no vendor, so if the contract passes for it too then the
# contract is about *being an adapter* rather than about XML on disk.
CONTRACT_IDS = ADAPTER_IDS + [FAKE]

# Where the contract's tracks "live". On the drive the temp libraries are on,
# because a program may only be able to name files on its own drive -- Serato
# keeps one database per drive and stores paths relative to its root. None of
# these files exist; only the path's shape matters.
MUSIC = os.path.splitdrive(tempfile.gettempdir())[0] + r"\Music"
music = MUSIC.lower()


# ================================ fixtures ==================================
@pytest.fixture
def paths(tmp_path):
    """Sandboxed libraries for every program. Never the real ones."""
    p = write_sample(tmp_path)
    os.makedirs(p.vdj_folders_dir(), exist_ok=True)
    return p


@pytest.fixture(params=CONTRACT_IDS)
def adapter(request, paths) -> LibraryAdapter:
    if request.param == FAKE:
        return FakeAdapter(paths)
    return build_adapters(paths)[request.param]


def _run(job):
    assert job is not None, "the adapter refused an edit it should support"
    assert callable(job), "writes are queued jobs, not performed inline"
    job()


def _track(path):
    return Track(key=path.lower(), path=path, source="")


def _child(node, name):
    return next((c for c in node.children if c.name.lower() == name.lower()), None)


def _at(lib, *node_path):
    node = lib.root
    for name in node_path:
        node = _child(node, name)
        assert node is not None, f"no node at {node_path}"
    return node


# ============================ registration ==================================
def test_every_adapter_has_a_stable_id_and_a_display_name():
    for cls in ADAPTER_CLASSES:
        assert cls.id, f"{cls.__name__} has no id"
        assert cls.display_name, f"{cls.__name__} has no display_name"


def test_ids_are_unique():
    """Ids key the libraries dict, the trees, the sections and the saved session
    state, so a collision would silently merge two programs."""
    assert len(ADAPTER_IDS) == len(set(ADAPTER_IDS))


def test_display_name_resolves_for_every_id():
    for cls in ADAPTER_CLASSES:
        assert display_name(cls.id) == cls.display_name


def test_display_name_of_an_unknown_program_falls_back_to_its_id():
    """A session saved by a build that knew a program this one doesn't must
    still render something rather than raising."""
    assert display_name("djay") == "djay"


def test_every_adapter_declares_its_own_tab_colours(adapter):
    """Colours live on the adapter rather than in a shared table in theme.py,
    which is what lets a program bring its own without editing shared code."""
    for attr in ("tab_bg", "tab_fg", "tab_bg_inactive"):
        value = getattr(adapter, attr)
        assert isinstance(value, str) and value.startswith("#"), f"{attr}={value!r}"


# =============================== paths ======================================
def test_path_fields_name_real_fields_on_paths(adapter):
    """The Paths dialog builds its rows from these and writes straight back to
    the dataclass, so a typo here would be a row that silently does nothing."""
    for field in adapter.path_fields():
        assert hasattr(Paths(), field.key), f"Paths has no {field.key!r}"
        assert field.label and field.caption and field.file_filter


def test_an_adapter_with_nothing_to_configure_is_always_configured(adapter):
    """Declaring no path is allowed -- a program whose library is not a file the
    user picks has nothing for the dialog to ask about. It must then never
    report itself unconfigured, or it could never be switched on."""
    if not adapter.path_fields():
        assert adapter.is_configured()


def test_the_dialog_sees_every_adapter_path_exactly_once():
    keys = [f.key for f in all_path_fields(Paths())]
    assert sorted(keys) == sorted(set(keys)), "two adapters claim the same path"


def test_an_unconfigured_adapter_says_so():
    """A blank Paths means nothing is set up yet; no *registered* adapter may
    claim it is. (The fake is excluded on purpose -- it has no path to be
    missing, which the previous test states.)"""
    for a in build_adapters(Paths()).values():
        assert not a.is_configured(), f"{a.id} claims to be configured with no path"


def test_the_fake_is_not_registered():
    """It exists for tests only. Shipping it would put a program in the Programs
    menu that cannot open anything."""
    assert FAKE not in ADAPTER_IDS


def test_a_configured_adapter_says_so(adapter):
    assert adapter.is_configured()


# =============================== reading ====================================
def test_read_returns_a_library_tagged_with_the_adapter_id(adapter):
    lib = adapter.read()

    assert isinstance(lib, Library)
    assert lib.source == adapter.id
    assert lib.root is not None


def test_read_calls_the_yield_hook_or_ignores_it_but_never_breaks(adapter):
    """The cooperative reload passes one in to keep audio serviced. An adapter
    that does not need it must still accept it."""
    adapter.read(lambda: None)


# ============================== the timeline ================================
def test_the_timeline_conversions_are_inverse_above_the_offset(adapter):
    """Canonical position = seconds from the first decoded audio sample.
    Below the offset the conversion clamps at zero and is knowingly lossy (see
    test_adapter_timeline.py); above it, a round trip must be exact."""
    path = MUSIC + r"\Song.mp3"
    offset = abs(adapter.timeline_offset(path))

    for pos in (offset + 1.0, offset + 60.0, offset + 3600.0):
        assert adapter.from_native_pos(adapter.to_native_pos(pos, path), path) == \
            pytest.approx(pos)


def test_positions_never_go_negative(adapter):
    path = MUSIC + r"\Song.mp3"
    assert adapter.to_native_pos(0.0, path) >= 0.0
    assert adapter.from_native_pos(0.0, path) >= 0.0


def test_an_adapter_with_no_start_time_cache_still_answers(adapter):
    """``start_times`` is only set once the cache is loaded; every adapter has to
    cope with being asked before that."""
    adapter.start_times = None
    assert isinstance(adapter.timeline_offset(MUSIC + r"\Song.mp3"), float)


# ========================= playlists round-trip =============================
def test_a_new_playlist_comes_back_with_its_tracks(adapter):
    tracks = [_track(MUSIC + r"\A.mp3"), _track(MUSIC + r"\B.mp3")]

    playlist, job = adapter.new_playlist("Contract Set", ("Contract Set",),
                                         tracks, None)
    _run(job)

    assert playlist.source == adapter.id
    node = _at(adapter.read(), "Contract Set")
    assert node.kind == "playlist"
    assert node.playlist.track_keys == [t.key for t in tracks]


def test_a_new_folder_comes_back_as_a_folder(adapter):
    """Where a folder can exist empty. Serato's cannot -- a folder there is a
    crate with crates below it -- so an empty one comes back as an empty list,
    and becomes a folder as soon as something is put inside (next test)."""
    _run(adapter.new_folder_job(("Contract Folder",)))

    node = _at(adapter.read(), "Contract Folder")
    if adapter.holds_empty_folders:
        assert node.kind == "folder"
    else:
        assert node.playlist is not None and node.playlist.track_keys == []


def test_a_folder_holding_something_is_a_folder_everywhere(adapter):
    _run(adapter.new_folder_job(("Gigs",)))
    _run(adapter.new_playlist("Set", ("Gigs", "Set"), [], None)[1])

    assert _at(adapter.read(), "Gigs").kind == "folder"


def test_a_playlist_can_be_created_inside_a_folder(adapter):
    _run(adapter.new_folder_job(("Gigs",)))

    _run(adapter.new_playlist("Set", ("Gigs", "Set"), [], None)[1])

    assert _at(adapter.read(), "Gigs", "Set").kind == "playlist"


def test_membership_is_rewritten_to_exactly_what_was_asked_for(adapter):
    _run(adapter.new_playlist("Set", ("Set",), [_track(MUSIC + r"\A.mp3")], None)[1])
    node = _at(adapter.read(), "Set")

    _run(adapter.write_membership_job(node, [_track(MUSIC + r"\B.mp3"),
                                             _track(MUSIC + r"\C.mp3")]))

    assert _at(adapter.read(), "Set").playlist.track_keys == \
        [music + r"\b.mp3", music + r"\c.mp3"]


# ========================= renaming and moving ==============================
def test_renaming_a_playlist_keeps_its_tracks(adapter):
    _run(adapter.new_playlist("Set", ("Set",), [_track(MUSIC + r"\A.mp3")], None)[1])
    node = _at(adapter.read(), "Set")

    _run(adapter.relocate_playlist_job(node, ("Renamed",), adapter.read()))

    lib = adapter.read()
    assert _child(lib.root, "Set") is None, "the old name must be gone"
    assert _at(lib, "Renamed").playlist.track_keys == [music + r"\a.mp3"]


def test_moving_a_playlist_moves_it_rather_than_copying_it(adapter):
    """The reported bug, pinned for every program: a move that left the original
    behind, so the playlist appeared twice."""
    _run(adapter.new_playlist("Set", ("Set",), [], None)[1])
    _run(adapter.new_folder_job(("Gigs",)))
    node = _at(adapter.read(), "Set")

    _run(adapter.relocate_playlist_job(node, ("Gigs", "Set"), adapter.read()))

    lib = adapter.read()
    assert _child(lib.root, "Set") is None, "left a copy at the old location"
    assert _at(lib, "Gigs", "Set").kind == "playlist"


def test_moving_a_folder_brings_its_contents(adapter):
    _run(adapter.new_folder_job(("Gigs",)))
    _run(adapter.new_playlist("Set", ("Gigs", "Set"), [], None)[1])
    _run(adapter.new_folder_job(("Archive",)))

    _run(adapter.relocate_folder_job(("Gigs",), ("Archive", "Gigs")))

    lib = adapter.read()
    assert _child(lib.root, "Gigs") is None
    assert _at(lib, "Archive", "Gigs", "Set").kind == "playlist"


# ================================ removal ===================================
def test_removing_a_playlist_removes_it(adapter):
    _run(adapter.new_playlist("Set", ("Set",), [], None)[1])
    node = _at(adapter.read(), "Set")

    _run(adapter.remove_job(node))

    assert _child(adapter.read().root, "Set") is None


def test_removing_a_folder_removes_what_is_inside_it(adapter):
    _run(adapter.new_folder_job(("Gigs",)))
    _run(adapter.new_playlist("Set", ("Gigs", "Set"), [], None)[1])
    node = _at(adapter.read(), "Gigs")

    _run(adapter.remove_job(node))

    assert _child(adapter.read().root, "Gigs") is None


def test_removing_a_playlist_never_touches_the_collection(adapter):
    """A binding rule: removing a playlist or folder removes list entries, never
    tracks. Stated once here so no future program can quietly break it.

    The snapshot is taken *after* the playlist exists, not before: an adapter is
    entitled to add a collection record when a playlist starts naming a track
    (SlipStream does, so its entries have something to resolve to). What must
    never happen is the removal taking one away.

    Only real collection entries count. ``lib.tracks`` also carries tracks known
    *only* from a playlist file -- VirtualDJ's ``.vdjfolder``-only tracks, marked
    ``in_database=False`` -- and those legitimately go when the playlist does,
    because they were never in database.xml to begin with.
    """
    def entries():
        return {k for k, t in adapter.read().tracks.items() if t.in_database}

    _run(adapter.new_playlist("Set", ("Set",), [_track(MUSIC + r"\A.mp3")], None)[1])
    node = _at(adapter.read(), "Set")
    before = entries()

    _run(adapter.remove_job(node))

    assert entries() == before


# =========================== reading tracks back ============================
def test_a_written_track_reads_back_with_what_was_written(adapter):
    """What a targeted refresh stands on: after a write, the app asks the file
    what the track now is rather than believing its own copy."""
    track = Track(key=music + r"\a.mp3", path=MUSIC + r"\A.mp3", source="",
                  title="Written Title", artist="Written Artist")
    _run(adapter.create_entry_job(track))

    got = adapter.read_tracks({track.key: track.path})

    assert set(got) == {track.key}
    assert got[track.key].title == "Written Title"
    assert got[track.key].path == track.path


def test_a_track_not_in_the_collection_is_absent_from_the_result(adapter):
    """Absent means absent -- the caller distinguishes "not there" from "there
    and unchanged", and a fabricated empty Track would erase that."""
    _run(adapter.create_entry_job(_track(MUSIC + r"\Here.mp3")))

    assert adapter.read_tracks({music + r"\gone.mp3": MUSIC + r"\Gone.mp3"}) == {}


def test_reading_an_entity_named_track_back_works(adapter):
    """A path whose name really contains ``&amp;``. The locate step rebuilds the
    escaped attribute, so this is the case that would silently return nothing
    and leave the square stale."""
    path = MUSIC + r"\Justice - Safe &amp; Sound.mp3"
    track = _track(path)
    _run(adapter.create_entry_job(track))

    got = adapter.read_tracks({track.key: path})

    assert set(got) == {track.key}
    assert got[track.key].path == path


def test_reading_nothing_back_is_not_a_file_read(adapter):
    assert adapter.read_tracks({}) == {}


def test_supplying_the_path_changes_nothing_observable(adapter):
    """``paths`` is an accelerator, not an instruction.

    It tells a writer where to *look* so it can locate the entry instead of
    walking the collection. What gets written, and what the writer answers, must
    be identical either way -- otherwise it is a second write path, and the fast
    one is the one nobody would check.
    """
    a, b = _track(MUSIC + r"\A.mp3"), _track(MUSIC + r"\B.mp3")
    _run(adapter.create_entry_job(a))
    _run(adapter.create_entry_job(b))

    with_path = adapter.write_metadata_jobs({a.key: {"genre": "Located"}},
                                            {a.key: a.path})()
    without = adapter.write_metadata_jobs({b.key: {"genre": "Located"}})()

    assert with_path == {a.key: True} and without == {b.key: True}
    tracks = adapter.read().tracks
    assert tracks[a.key].genre == tracks[b.key].genre == "Located"


def test_a_path_for_a_track_that_is_not_there_still_answers_false(adapter):
    """A hint that leads nowhere must not become a claim that something was
    written."""
    absent = music + r"\gone.mp3"

    written = adapter.write_metadata_jobs({absent: {"genre": "X"}},
                                          {absent: MUSIC + r"\Gone.mp3"})()

    assert written == {absent: False}


# ============================ repathing tracks ==============================
def test_a_repathed_track_is_findable_at_its_new_path(adapter):
    """What Clean up needs after it renames a file on disk: every program that
    records where the audio lives has to be moved with it."""
    old = _track(MUSIC + r"\Safe &amp; Sound.mp3")
    new_path = MUSIC + r"\Safe & Sound.mp3"
    _run(adapter.create_entry_job(old))

    _run(adapter.repath_tracks_job({old.key: new_path}))

    tracks = adapter.read().tracks
    assert old.key not in tracks, "the old path is gone, not duplicated"
    assert tracks[new_path.lower()].path == new_path


def test_a_repathed_track_stays_in_its_playlists(adapter):
    """The rule a collection-only rewrite breaks. Traktor names playlist members
    by path and VirtualDJ writes the path into each ``.vdjfolder``, so a track
    whose file moved drops out of every list unless both are rewritten."""
    old = _track(MUSIC + r"\Safe &amp; Sound.mp3")
    new_path = MUSIC + r"\Safe & Sound.mp3"
    _run(adapter.new_playlist("Set", ("Set",), [old], None)[1])

    _run(adapter.repath_tracks_job({old.key: new_path}))

    node = _at(adapter.read(), "Set")
    assert node.playlist.track_keys == [new_path.lower()]


def test_a_track_can_be_relinked_to_its_other_unicode_spelling(adapter):
    """Clean up's relink-only kind: ``Déja`` recorded with a decomposed accent
    (e + U+0301) while the file on disk has the composed one (U+00E9). The two
    look identical and differ only in code points, so a writer that compared
    loosely -- or normalized on the way out -- would either miss the entry or
    write back the spelling it was asked to replace. Collection and playlist
    both, since both record the path."""
    old = _track(MUSIC + "\\Dogg Master - De\u0301ja Vu.mp3")
    new_path = MUSIC + "\\Dogg Master - D\u00e9ja Vu.mp3"
    _run(adapter.create_entry_job(old))
    _run(adapter.new_playlist("Set", ("Set",), [old], None)[1])

    _run(adapter.repath_tracks_job({old.key: new_path}))

    lib = adapter.read()
    assert old.key not in lib.tracks, "the decomposed spelling is gone"
    assert lib.tracks[new_path.lower()].path == new_path
    assert _at(lib, "Set").playlist.track_keys == [new_path.lower()]


def test_relinking_a_playlist_only_reference_reports_it_relinked(adapter):
    """A playlist may name a track the collection holds no entry for --
    VirtualDJ does, for every file it could not find when the playlist was
    built. Moving that reference is the work asked for, so it reports True;
    reporting False made Clean up say "0 relinked" about a playlist it had just
    rewritten."""
    old = _track(MUSIC + "\\El Carriqui\u0301.mp3")
    new_path = MUSIC + "\\El Carriqu\u00ed.mp3"
    _run(adapter.new_playlist("Set", ("Set",), [old], None)[1])
    if old.key in {k for k, t in adapter.read().tracks.items() if t.in_database}:
        pytest.skip("this program creates an entry for every playlist member")

    done = adapter.repath_tracks_job({old.key: new_path})()

    assert done == {old.key: True}
    assert _at(adapter.read(), "Set").playlist.track_keys == [new_path.lower()]


def test_repathing_reports_per_track_and_returns_a_job(adapter):
    """Like every other write: a job, and the writer's own answer -- so a caller
    never claims a relink that did not land."""
    known = _track(MUSIC + r"\A.mp3")
    _run(adapter.create_entry_job(known))
    job = adapter.repath_tracks_job({known.key: MUSIC + r"\B.mp3",
                                     music + r"\never.mp3": MUSIC + r"\C.mp3"})

    assert callable(job), "writes are queued jobs, not performed inline"
    done = job()

    assert done[known.key] is True
    assert done[music + r"\never.mp3"] is False


def test_repathing_nothing_is_a_no_op(adapter):
    _run(adapter.create_entry_job(_track(MUSIC + r"\A.mp3")))
    before = set(adapter.read().tracks)

    adapter.repath_tracks_job({})()

    assert set(adapter.read().tracks) == before


# ============================ editing metadata ==============================
def test_a_text_field_can_be_changed(adapter):
    track = _track(MUSIC + r"\A.mp3")
    _run(adapter.create_entry_job(track))

    _run(adapter.write_metadata_job(track.key, {"title": "Edited Title"}))

    assert adapter.read().tracks[track.key].title == "Edited Title"


def test_a_field_left_out_of_the_edit_is_left_alone(adapter):
    """The whole basis of a multi-track edit: a `<various>` field the user did
    not touch is simply absent from the dict, and every track keeps its own."""
    track = Track(key=music + r"\a.mp3", path=MUSIC + r"\A.mp3", source="",
                  title="Keep Me", artist="Original")
    _run(adapter.create_entry_job(track))

    _run(adapter.write_metadata_job(track.key, {"artist": "Changed"}))

    back = adapter.read().tracks[track.key]
    assert back.artist == "Changed"
    assert back.title == "Keep Me", "an untouched field must not be blanked"


def test_editing_metadata_leaves_the_cues_alone(adapter):
    """Text surgery: a tag edit must not disturb the cue data beside it."""
    from trackcleaner.model.cues import Poi

    track = Track(key=music + r"\a.mp3", path=MUSIC + r"\A.mp3", source="",
                  title="T", pois=[Poi(12.5, 1, "cue"), Poi(0.5, None, "beatgrid")])
    _run(adapter.create_entry_job(track))
    before = len(adapter.read().tracks[track.key].pois)

    _run(adapter.write_metadata_job(track.key, {"title": "Renamed"}))

    assert len(adapter.read().tracks[track.key].pois) == before


def test_editing_a_track_that_is_not_there_is_harmless(adapter):
    _run(adapter.write_metadata_job("d:/nope.mp3", {"title": "x"}))


def test_a_metadata_write_reports_whether_it_happened(adapter):
    """The caller has to be able to tell "written" from "no such entry".

    Without this the sync engine updated its in-memory copy as though the write
    had landed, so two in-memory tracks agreed, the row went **green**, and the
    change had never reached the file -- a lie that survived a refresh, because a
    refresh re-diffs memory rather than re-reading the file.
    """
    track = Track(key=music + r"\a.mp3", path=MUSIC + r"\A.mp3", source="",
                  title="T")
    _run(adapter.create_entry_job(track))

    assert adapter.write_metadata_job(track.key, {"title": "Renamed"})() is True
    assert adapter.write_metadata_job("d:/nope.mp3", {"title": "x"})() is False


def test_the_editable_fields_are_declared(adapter):
    """Named on the adapter so the dialog and the in-place editor build from one
    list, and BPM/key cannot creep in by accident."""
    assert "title" in adapter.EDITABLE_FIELDS
    assert "bpm" not in adapter.EDITABLE_FIELDS
    assert "key_musical" not in adapter.EDITABLE_FIELDS


# ============================ jobs, not writes ==============================
def test_building_a_job_does_not_perform_it(adapter):
    """Every write returns a zero-arg callable for the serialized queue. Doing
    the IO at build time would put it on the UI thread and skip the
    serialization that stops two edits interleaving."""
    job = adapter.new_folder_job(("Deferred",))

    assert callable(job)
    assert _child(adapter.read().root, "Deferred") is None, \
        "the library changed before the job ran"

    job()

    assert _child(adapter.read().root, "Deferred") is not None


def test_a_refused_edit_returns_none_with_a_reason(adapter):
    """Adapters report *why* through last_error so the caller can show a
    program-specific message without knowing which programs exist."""
    job = adapter.relocate_folder_job(("Definitely Not There",), ("Elsewhere",))

    if job is None:
        assert adapter.last_error(), "refused without saying why"
    else:
        job()      # tolerating it is fine; corrupting the library is not
        assert _child(adapter.read().root, "Elsewhere") is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
