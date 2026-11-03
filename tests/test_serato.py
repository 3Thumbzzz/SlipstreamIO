"""Serato: a binary database, crate files, and cues inside the audio files.

The adapter contract covers what every program must do. This module covers what
only Serato can get wrong:

* **the record format** -- re-encoding must give back the exact bytes, or every
  field this app ignores is at risk on every write;
* **paths are relative to the database's drive** -- a file on another drive has
  no spelling and must be refused, not written wrong;
* **the crate tree is spelled in file names** (``A%%B.crate``), parents are
  implied, and a rename has to carry every descendant and the sidebar order;
* **cues live in the audio file**, the one place this app writes into one: only
  Serato's two frames may change -- never another frame, never a byte after the
  tag (an ID3v1 tag at the end included) -- the original is restorable, and a
  file that cannot take the write is left exactly as it was;
* **the base64 is Serato's own** -- the quirks are pinned against a frame Serato
  wrote.
"""

import base64
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.adapters import build_adapters                        # noqa: E402
from trackcleaner.config import paths as paths_mod                      # noqa: E402
from trackcleaner.io import recycle, serato_db as sdb, serato_tags as st  # noqa: E402
from trackcleaner.model import matching, status                         # noqa: E402
from trackcleaner.model.cues import Poi                                 # noqa: E402
from trackcleaner.model.library import Track                            # noqa: E402
from fixtures import write_sample                                       # noqa: E402

SERATO = "serato"

#: A Markers2 frame exactly as Serato wrote it (from a real library): a COLOR,
#: two cues, BPMLOCK; payload length 1 mod 3, so no trailing "A"; one full
#: 72-character line; NUL-padded to 470.
SERATO_WRITTEN_TEXT = (
    b"AQFDT0xPUgAAAAAEAP///0NVRQAAAAARAAAAACY/AADMzAAAbi5uLgBDVUUAAAAAEQACAACm\n"
    b"cgAAzMwAAG4ubi4AQlBNTE9DSwAAAAABAA")
SERATO_WRITTEN = b"\x01\x01" + SERATO_WRITTEN_TEXT
SERATO_WRITTEN += b"\x00" * (470 - len(SERATO_WRITTEN))


# ================================ fixtures ==================================
def _record(tag, value):
    payload = value.encode("utf-16-be") if isinstance(value, str) else value
    return tag.encode("latin-1") + struct.pack(">I", len(payload)) + payload


def _track_record(rel, title, **more):
    body = _record("ttyp", "mp3") + _record("pfil", rel) + _record("tsng", title)
    for tag, value in more.items():
        body += _record(tag, value)
    body += _record("bhrt", b"\x01")              # a field this app never reads
    return _record("otrk", body)


def _mp3(path, id3_frames=(), v1=True):
    """A small, valid-enough MP3: optional ID3v2 tag, silent MPEG frames, and an
    ID3v1 tag at the end -- the shape 36 of 60 sampled real files have."""
    frame = b"\xff\xfb\x90\x64" + b"\x00" * 413
    with open(path, "wb") as f:
        f.write(frame * 40)
    if id3_frames:
        from mutagen.id3 import ID3
        tags = ID3()
        for fr in id3_frames:
            tags.add(fr)
        tags.save(path, v2_version=4, v1=0)
    if v1:
        # Appended last, and saying something other than the ID3v2 title: a
        # save that regenerated it from the v2 frames would change these bytes.
        with open(path, "ab") as f:
            f.write(b"TAG" + b"Original v1 title".ljust(30, b"\x00") + b"\x00" * 95)
    return path


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """A Serato library on the temp drive: three tracks, nested crates with an
    implied parent, a parent that is also a crate, a sidebar order, and one
    crate member the database does not hold."""
    monkeypatch.setattr(paths_mod, "app_dir", lambda: str(tmp_path))
    paths = write_sample(tmp_path)
    root = sdb.volume_root(paths.serato)
    music = tmp_path / "Music"
    music.mkdir()
    rel = lambda name: sdb.to_stored(str(music / name), root)     # noqa: E731
    db = (_record("vrsn", sdb.DB_VERSION)
          + _track_record(rel("a.mp3"), "Alpha", tart="Artist A", tkey="Ebm",
                          tbpm="120.00", tlen="04:04.45", tbit="320.0kbps")
          + _track_record(rel("b.mp3"), "Bravo")
          + _track_record(rel("c.mp3"), "Charlie"))
    with open(paths.serato, "wb") as f:
        f.write(db)
    sub = os.path.dirname(paths.serato) + "\\Subcrates"

    def crate(name, *members):
        with open(os.path.join(sub, name + ".crate"), "wb") as f:
            f.write(sdb.crate_with_members(None, [rel(m) for m in members]))

    crate("Sets%%Warmup", "b.mp3", "a.mp3")
    crate("Sets%%Late", "c.mp3", "orphan.mp3")
    crate("Gigs", "a.mp3")
    crate("Gigs%%Friday", "b.mp3")
    order = "\n".join(["[begin record]", "[crate]Gigs", "[crate]Gigs%%Friday",
                       "[crate]Sets", "[crate]Sets%%Late", "[crate]Sets%%Warmup",
                       "[end record]", ""]).encode("utf-16-be")
    with open(os.path.join(os.path.dirname(paths.serato), "neworder.pref"), "wb") as f:
        f.write(order)
    adapter = build_adapters(paths)[SERATO]
    return adapter, music


def _key(path):
    return matching.path_key(str(path))


def _child(node, name):
    return next((c for c in node.children if c.name == name), None)


# ============================== the record format ===========================
def test_records_re_encode_to_the_exact_bytes(lib):
    adapter, _music = lib
    data = open(adapter.paths.serato, "rb").read()

    assert sdb.encode(sdb.parse(data)) == data


def test_a_truncated_file_is_refused_before_it_replaces_anything():
    with pytest.raises(ValueError):
        sdb.validate(_record("vrsn", sdb.DB_VERSION)[:-3])


# ================================== reading =================================
def test_paths_are_relative_to_the_database_drive(lib):
    adapter, music = lib
    tracks = adapter.read().tracks

    assert tracks[_key(music / "a.mp3")].path == str(music / "a.mp3")


def test_tags_key_length_and_bitrate_are_read(lib):
    adapter, music = lib
    t = adapter.read().tracks[_key(music / "a.mp3")]

    assert (t.title, t.artist, t.bpm, t.bitrate) == ("Alpha", "Artist A", 120.0, 320)
    assert t.length == pytest.approx(244.45)
    assert t.key_value == 15 and adapter.display_key(t) == "Ebm"


def test_the_crate_tree_follows_the_file_names_and_the_sidebar_order(lib):
    adapter, music = lib
    root = adapter.read().root

    assert [c.name for c in root.children] == ["Gigs", "Sets"], "neworder.pref"
    sets, gigs = _child(root, "Sets"), _child(root, "Gigs")
    assert sets.kind == "folder" and sets.playlist is None, "implied by its children"
    assert gigs.kind == "folder" and gigs.playlist.track_keys == [_key(music / "a.mp3")]
    assert [c.name for c in sets.children] == ["Late", "Warmup"]
    assert _child(sets, "Warmup").playlist.track_keys == [
        _key(music / "b.mp3"), _key(music / "a.mp3")]


def test_a_crate_member_the_database_lacks_is_not_in_the_collection(lib):
    adapter, music = lib
    orphan = adapter.read().tracks[_key(music / "orphan.mp3")]

    assert orphan.in_database is False


# ============================ writing the database ==========================
def test_a_tag_edit_leaves_every_other_record_byte_identical(lib):
    adapter, music = lib
    before = sdb.parse(open(adapter.paths.serato, "rb").read())

    assert adapter.write_metadata_job(_key(music / "b.mp3"), {"genre": "Funk"})()

    after = sdb.parse(open(adapter.paths.serato, "rb").read())
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert changed == [2], "only Bravo's record"
    assert sdb.fields(after[2][1])["bhrt"] == b"\x01", "unread fields kept"
    assert adapter.read().tracks[_key(music / "b.mp3")].genre == "Funk"


def test_a_file_on_another_drive_is_refused_not_misspelled(lib):
    adapter, _music = lib
    other = "Q:\\Elsewhere\\x.mp3" if not adapter._root.upper().startswith("Q") \
        else "R:\\Elsewhere\\x.mp3"
    data = open(adapter.paths.serato, "rb").read()

    new, created = sdb.ensure_tracks(data, adapter._root,
                                     [Track(key=other.lower(), path=other, source="")])

    assert created == {other.lower(): False} and new == data


# ================================== crates ==================================
def test_rewriting_members_keeps_the_crate_s_other_records(lib):
    adapter, music = lib
    crate = adapter._crate(("Sets", "Warmup"))
    columns = _record("ovct", _record("tvcn", "song"))
    with open(crate, "rb") as f:
        data = f.read()
    with open(crate, "wb") as f:
        f.write(data[:data.index(b"otrk")] + columns + data[data.index(b"otrk"):])
    node = _child(_child(adapter.read().root, "Sets"), "Warmup")

    adapter.write_membership_job(node, [Track(key=_key(music / "c.mp3"),
                                              path=str(music / "c.mp3"), source="")])()

    out = open(crate, "rb").read()
    assert columns in out
    assert sdb.crate_members(out) == [sdb.to_stored(str(music / "c.mp3"), adapter._root)]


def test_renaming_a_folder_carries_every_descendant_and_the_order(lib):
    adapter, _music = lib

    adapter.relocate_folder_job(("Gigs",), ("Shows",))()

    root = adapter.read().root
    assert _child(root, "Gigs") is None
    assert _child(_child(root, "Shows"), "Friday").kind == "playlist"
    order = sdb.read_order(adapter._order_file)
    assert "Shows%%Friday" in order and not any(o.startswith("Gigs") for o in order)


def test_moving_a_crate_under_another_folder(lib):
    adapter, _music = lib
    node = _child(_child(adapter.read().root, "Sets"), "Late")

    adapter.relocate_playlist_job(node, ("Gigs", "Late"), None)()

    root = adapter.read().root
    assert [c.name for c in _child(root, "Sets").children] == ["Warmup"]
    assert _child(_child(root, "Gigs"), "Late") is not None


def test_removing_a_folder_recycles_its_crates_and_order(lib, monkeypatch):
    adapter, _music = lib
    sent = []
    monkeypatch.setattr(recycle, "send_to_recycle_bin",
                        lambda p: sent.append(os.path.basename(p)) or os.remove(p))

    adapter.remove_job(_child(adapter.read().root, "Sets"))()

    assert sorted(sent) == ["Sets%%Late.crate", "Sets%%Warmup.crate"]
    assert _child(adapter.read().root, "Sets") is None
    assert not any(o.startswith("Sets") for o in sdb.read_order(adapter._order_file))


def test_a_crate_name_cannot_carry_the_level_separator(lib):
    adapter, _music = lib

    assert "%%" not in adapter.sanitize_node_name("100%% Funk")


# =============================== the base64 =================================
def test_a_serato_written_frame_re_encodes_byte_for_byte():
    assert st.encode_markers2(st.decode_markers2(SERATO_WRITTEN)) == SERATO_WRITTEN


@pytest.mark.parametrize("extra,appended", [(0, b""), (1, b"A"), (2, b"A")])
def test_one_A_is_appended_unless_the_payload_is_1_mod_3(extra, appended):
    """Serato's encoder quirk, observed on every Serato-written frame sampled."""
    entries = [("BPMLOCK", b"\x00" * (1 + extra))]
    payload = b"\x01\x01" + b"BPMLOCK\x00" + struct.pack(">I", 1 + extra) + b"\x00" * (1 + extra)
    text = st.encode_markers2(entries)[2:].rstrip(b"\x00").replace(b"\n", b"")

    assert text == base64.b64encode(payload).rstrip(b"=") + (
        b"" if len(payload) % 3 == 1 else b"A")
    assert st.decode_markers2(st.encode_markers2(entries)) == entries


def test_a_full_last_line_still_ends_in_a_newline():
    entries = [("BPMLOCK", b"\x00" * 46)]            # payload 61 -> 81 chars ...
    while True:
        text = st.encode_markers2(entries)[2:].rstrip(b"\x00")
        if len(text.replace(b"\n", b"")) % 72 == 0:
            break
        entries = [("BPMLOCK", entries[0][1] + b"\x00")]

    assert text.endswith(b"\n")


def test_the_converter_style_frame_is_read_too():
    """``=`` kept, an extra NUL encoded, a trailing newline -- a converter's."""
    frame = (b"\x01\x01AQFDVUUAAAAAEQABAABilgAAzMwAAG4ubi4AQlBNTE9DSwAAAAABAAA=\n")

    entries = st.decode_markers2(frame)

    assert [n for n, _b in entries] == ["CUE", "BPMLOCK"]
    assert st._cue(entries[0][1]) == (1, 25.238)


# ============================ cues in the audio file ========================
def _points(path):
    return sorted((p.kind, p.num, round(p.pos, 3)) for p in st.read_points(path))


def test_a_cue_write_changes_only_serato_s_frames(tmp_path):
    from mutagen.id3 import ID3, TIT2, TXXX
    path = _mp3(str(tmp_path / "song.mp3"),
                [TIT2(encoding=3, text=["Keep Me"]),
                 TXXX(encoding=3, desc="MOOD", text=["warm"])])
    with open(path, "rb") as f:
        original = f.read()
    tail = original[st._tag_length(original):]
    track = Track(key=path.lower(), path=path, source="", bpm=101.5,
                  pois=[Poi(12.345, 1, "cue"), Poi(0.432, None, "beatgrid")])

    assert st.write_points(path, track, str(tmp_path / "bk"))

    assert _points(path) == [("beatgrid", None, 0.432), ("cue", 1, 12.345)]
    with open(path, "rb") as f:
        written = f.read()
    assert written[st._tag_length(written):] == tail, "audio + ID3v1 untouched"
    tags = ID3(path)
    assert tags["TIT2"].text == ["Keep Me"] and tags["TXXX:MOOD"].text == ["warm"]


def test_a_file_with_no_tag_at_all_gains_one(tmp_path):
    path = _mp3(str(tmp_path / "bare.mp3"))
    track = Track(key=path.lower(), path=path, source="", pois=[Poi(3.0, 2, "cue")])

    assert st.write_points(path, track, str(tmp_path / "bk"))
    assert _points(path) == [("cue", 2, 3.0)]


def test_writing_the_same_points_again_touches_nothing(tmp_path):
    path = _mp3(str(tmp_path / "song.mp3"))
    track = Track(key=path.lower(), path=path, source="", pois=[Poi(3.0, 2, "cue")])
    st.write_points(path, track, str(tmp_path / "bk"))
    stamp = os.stat(path).st_mtime_ns, open(path, "rb").read()

    assert st.write_points(path, track, str(tmp_path / "bk"))
    assert (os.stat(path).st_mtime_ns, open(path, "rb").read()) == stamp


def test_a_pad_keeps_its_colour_and_name(tmp_path):
    """The shared model has no slot for either, so a moved cue keeps them."""
    from mutagen.id3 import GEOB
    path = _mp3(str(tmp_path / "song.mp3"),
                [GEOB(encoding=0, mime=st._MIME, filename="", desc=st.MARKERS2,
                      data=SERATO_WRITTEN)])
    before = {st._cue(b)[0]: b for n, b in st.decode_markers2(SERATO_WRITTEN) if n == "CUE"}
    track = Track(key=path.lower(), path=path, source="",
                  pois=[Poi(20.0, 1, "cue"), Poi(50.0, 3, "cue")])

    st.write_points(path, track, str(tmp_path / "bk"))

    from mutagen.id3 import ID3
    entries = st.decode_markers2(st._frames(ID3(path))[st.MARKERS2])
    after = {st._cue(b)[0]: b for n, b in entries if n == "CUE"}
    assert after[0][6:] == before[0][6:], "pad A: same colour and name"
    assert [n for n, _b in entries] == ["COLOR", "CUE", "CUE", "BPMLOCK"]


def test_a_tempo_changing_grid_is_never_flattened(tmp_path):
    from mutagen.id3 import GEOB
    grid = (b"\x01\x00" + struct.pack(">I", 2) + struct.pack(">f", 0.5)
            + struct.pack(">I", 64) + struct.pack(">f", 30.0)
            + struct.pack(">f", 124.0) + b"\x07")
    path = _mp3(str(tmp_path / "song.mp3"),
                [GEOB(encoding=0, mime=st._MIME, filename="", desc=st.BEATGRID, data=grid)])
    track = Track(key=path.lower(), path=path, source="", bpm=120.0,
                  pois=[Poi(9.0, None, "beatgrid"), Poi(4.0, 1, "cue")])

    st.write_points(path, track, str(tmp_path / "bk"))

    from mutagen.id3 import ID3
    assert st._frames(ID3(path))[st.BEATGRID] == grid


def test_an_unsupported_container_is_left_alone(tmp_path):
    path = str(tmp_path / "song.flac")
    with open(path, "wb") as f:
        f.write(b"fLaC" + b"\x00" * 64)
    track = Track(key=path.lower(), path=path, source="", pois=[Poi(1.0, 1, "cue")])

    assert st.read_points(path) is None
    assert st.write_points(path, track, str(tmp_path / "bk")) is False
    assert open(path, "rb").read() == b"fLaC" + b"\x00" * 64


def test_the_previous_frames_are_backed_up_and_restorable(tmp_path):
    from mutagen.id3 import GEOB
    path = _mp3(str(tmp_path / "song.mp3"),
                [GEOB(encoding=0, mime=st._MIME, filename="", desc=st.MARKERS2,
                      data=SERATO_WRITTEN)])
    original = _points(path)
    backups = str(tmp_path / "bk")
    st.write_points(path, Track(key=path.lower(), path=path, source="",
                                pois=[Poi(99.0, 5, "cue")]), backups)
    assert _points(path) != original

    restored = st.restore(os.path.join(backups, os.listdir(backups)[0]))

    assert restored == 1 and _points(path) == original


# ================================ the cache =================================
def test_the_cache_reads_a_file_once_until_it_changes(tmp_path, monkeypatch):
    path = _mp3(str(tmp_path / "song.mp3"))
    reads = []
    real = st.read_points
    monkeypatch.setattr(st, "read_points", lambda p: reads.append(p) or real(p))
    cache = st.CueCache(str(tmp_path / "cache.json"))

    cache.points(path)
    cache.points(path)
    cache.save()
    st.CueCache(str(tmp_path / "cache.json")).points(path)
    assert len(reads) == 1, "unchanged file, saved cache: one read"

    os.utime(path, ns=(os.stat(path).st_atime_ns, os.stat(path).st_mtime_ns + 10**9))
    st.CueCache(str(tmp_path / "cache.json")).points(path)
    assert len(reads) == 2, "a changed file is read again"


# ================================= end to end ===============================
def test_a_track_synced_in_reads_back_in_sync(lib):
    """Convergence: what the adapter writes must compare equal on read-back,
    database fields and file cues alike, or the square stays yellow for ever."""
    adapter, music = lib
    path = _mp3(str(music / "new.mp3"))
    source = Track(key=_key(path), path=path, source="traktor", title="New One",
                   artist="Someone", bpm=96.0,
                   pois=[Poi(1.25, None, "beatgrid"), Poi(30.5, 1, "cue"),
                         Poi(62.0, 4, "cue")])

    created = adapter.create_entries_job([source])()

    assert created == {source.key: True}
    back = adapter.read_tracks({source.key: path})[source.key]
    assert status.cues_equivalent(source, back)
    assert (back.title, back.artist) == ("New One", "Someone")
    assert status.cues_equivalent(source, adapter.read().tracks[source.key]), \
        "and through the cache on a full read"
