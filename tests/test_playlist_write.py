"""Regression tests for VDJ -> Traktor playlist creation.

Covers the three bugs behind the corrupt "Rocking" output:
  A. _find_target_node must not false-match a top-level playlist to _LOOPS.
  B. create_playlist_node must create a valid node inside $ROOT (no IndexError,
     no insertion outside the tree) and bump the right SUBNODES COUNT.
  C. Adding a playlist of tracks already in the target collection must not
     clobber their existing cues.
"""

import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.config.paths import Paths                       # noqa: E402
from trackcleaner.io import (traktor_read, traktor_write, vdj_read,  # noqa: E402
                             vdj_write)
from trackcleaner.model.cues import Poi                             # noqa: E402
from trackcleaner.model.library import Playlist, Track, TreeNode, TRAKTOR, VDJ  # noqa: E402
from trackcleaner.model import baseline as bl          # noqa: E402
from trackcleaner.sync.engine import SyncEngine, NEW, UPDATE       # noqa: E402
from fixtures import EMPTY_VDJ_XML, SAMPLE_NML, write_sample       # noqa: E402


def _nml():
    return SAMPLE_NML


# ----------------------------- Bug B units -----------------------------
def test_find_subnodes_region_root_no_crash():
    nml = _nml()
    r = traktor_write._find_subnodes_region(nml, ())
    assert r is not None
    assert r[4] == "4"                    # $ROOT SUBNODES COUNT in the sample


def test_find_subnodes_region_is_case_insensitive():
    """Folder lookup ignores case so VDJ's "Breaks" resolves to Traktor's
    "breaks" -- the writer and the engine's lookup must agree on placement."""
    nml = _nml()
    assert traktor_write._find_subnodes_region(nml, ("Breaks",)) is not None
    assert traktor_write._find_subnodes_region(nml, ("breaks",)) is not None
    assert traktor_write._find_subnodes_region(nml, ("NoSuchFolder",)) is None


def test_create_playlist_node_uses_the_existing_folder():
    nml = _nml()
    t = Track(key="k", path=r"D:\Music\Funk\A.mp3", source=VDJ, title="A", artist="Art")
    out = traktor_write.create_playlist_node(nml, ("Breaks",), "Rocking", "uuidX", [t])
    # placed inside the existing lowercase "breaks" folder: its COUNT 1 -> 2,
    # $ROOT untouched, and no case-variant twin folder created
    assert re.search(r'NAME="breaks"[^>]*><SUBNODES COUNT="2">', out)
    assert re.search(r'NAME="\$ROOT"><SUBNODES COUNT="4"', out)
    assert '<NODE TYPE="FOLDER" NAME="Breaks"' not in out
    m = re.search(r'NAME="Rocking"><PLAYLIST ENTRIES="(\d+)"', out)
    assert m and m.group(1) == "1"
    assert re.search(r'NAME="_LOOPS"><PLAYLIST ENTRIES="0"', out)
    assert out.index("Rocking") < out.index("</PLAYLISTS>")


def test_create_playlist_node_creates_a_missing_folder():
    nml = _nml()
    t = Track(key="k", path=r"D:\Music\Funk\A.mp3", source=VDJ, title="A", artist="Art")
    out = traktor_write.create_playlist_node(nml, ("Brand New",), "P", "uuidY", [t])
    assert re.search(r'<NODE TYPE="FOLDER" NAME="Brand New"', out)
    assert re.search(r'NAME="Brand New"><SUBNODES COUNT="1">', out)
    assert re.search(r'NAME="\$ROOT"><SUBNODES COUNT="5"', out)   # gained the folder


# --------------------------- Bug A + C via engine ----------------------
def _fake_vdj_with_rocking(paths, existing_track):
    """Build a VDJ library whose only playlist is a top-level 'Rocking' with
    one track already in the Traktor collection (existing_track) and one new."""
    vlib = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    new_t = Track(key=r"d:\music\funk\brand new.mp3", path=r"D:\Music\Funk\Brand New.mp3",
                  source=VDJ, title="Brand New", artist="Nobody",
                  pois=[])
    vlib.tracks[new_t.key] = new_t
    vlib.tracks[existing_track.key] = existing_track     # same key as a collection track
    rocking = TreeNode(name="Rocking", kind="playlist", source=VDJ, node_path=("Rocking",),
                       playlist=Playlist(name="Rocking", source=VDJ,
                                        track_keys=[existing_track.key, new_t.key]))
    return vlib, rocking


def test_sync_creates_rocking_without_touching_loops_or_cues(tmp_path):
    paths = write_sample(tmp_path)
    nml_path = tmp_path / "collection.nml"

    tlib = traktor_read.read(paths.nml)
    # pick a track that already exists in the Traktor collection. Give the VDJ
    # copy the SAME cues, so the sync is a genuine no-op for cue data (the
    # realistic "add a playlist of already-synced tracks" case).
    existing_key = next(iter(tlib.tracks))
    existing_cues_before = len(tlib.tracks[existing_key].pois)
    src_existing = Track(key=existing_key, path=tlib.tracks[existing_key].path,
                         source=VDJ, title="x", artist="y",
                         pois=list(tlib.tracks[existing_key].pois))

    vlib, rocking = _fake_vdj_with_rocking(paths, src_existing)
    engine = SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=None)
    result = engine.sync_playlist(rocking, VDJ, UPDATE)

    out = nml_path.read_text(encoding="utf-8")

    # Bug A: a real Rocking node was created (not appended into _LOOPS)
    assert re.search(r'NAME="Rocking"><PLAYLIST ENTRIES="2"', out)
    assert re.search(r'NAME="_LOOPS"><PLAYLIST ENTRIES="0"', out), "_LOOPS must stay empty"
    # $ROOT count bumped
    assert re.search(r'NAME="\$ROOT"><SUBNODES COUNT="5"', out)

    # Bug C: the pre-existing track's cues were NOT overwritten
    tlib2 = traktor_read.read(str(nml_path))
    assert len(tlib2.tracks[existing_key].pois) == existing_cues_before

    # the genuinely-new track was added to the collection (+1 entry)
    assert result.tracks_added == 1
    assert re.search(r'TITLE="Brand New"', out)


def test_update_skips_when_target_provably_newer_without_attribution(tmp_path):
    """With owners=None (no attribution available) the engine falls back to the
    old timestamp rule: skip when the target is provably newer."""
    from datetime import timedelta

    paths = write_sample(tmp_path)
    nml_path = tmp_path / "collection.nml"
    tlib = traktor_read.read(paths.nml)
    existing_key = next(iter(tlib.tracks))
    tgt = tlib.tracks[existing_key]
    cues_before = len(tgt.pois)
    assert tgt.modified is not None
    # source has DIFFERENT cues but an OLDER timestamp -> must be skipped
    src_existing = Track(key=existing_key, path=tgt.path, source=VDJ,
                         title="x", artist="y", pois=[],
                         modified=tgt.modified - timedelta(days=1))
    vlib, rocking = _fake_vdj_with_rocking(paths, src_existing)
    engine = SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=None)   # owners=None
    r = engine.sync_playlist(rocking, VDJ, UPDATE)

    assert r.tracks_skipped >= 1
    tlib2 = traktor_read.read(str(nml_path))
    assert len(tlib2.tracks[existing_key].pois) == cues_before


def _update_with_owner(tmp_path, owner):
    """Run an UPDATE where the source (VDJ) track differs from the target, with
    the given attribution. Returns (result, cues_before, cues_after)."""
    paths = write_sample(tmp_path)
    tlib = traktor_read.read(paths.nml)
    key = next(k for k, t in tlib.tracks.items() if len(t.pois) > 1)
    tgt = tlib.tracks[key]
    cues_before = len(tgt.pois)
    src = Track(key=key, path=tgt.path, source=VDJ, title=tgt.title,
                artist=tgt.artist, album=tgt.album, genre=tgt.genre,
                comment=tgt.comment, pois=[Poi(1.0, None, "beatgrid")])
    vlib, node = _fake_vdj_with_rocking(paths, src)
    # Attribution is per program pair -- a track can be owned by one program
    # against one peer and by that peer against another.
    pair = bl.pair_id(VDJ, TRAKTOR)
    owners = {pair: {} if owner is None else {key: owner}}
    engine = SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=None, owners=owners)
    r = engine.sync_playlist(node, VDJ, UPDATE)
    after = len(traktor_read.read(str(tmp_path / "collection.nml")).tracks[key].pois)
    return r, cues_before, after


def test_update_writes_when_the_source_side_owns_the_change(tmp_path):
    r, before, after = _update_with_owner(tmp_path, "vdj")
    assert r.tracks_cue_synced >= 1
    assert after != before, "the VDJ-owned edit should have been pushed"


def test_update_skips_when_the_other_side_owns_the_change(tmp_path):
    r, before, after = _update_with_owner(tmp_path, "traktor")
    assert r.tracks_skipped >= 1
    assert after == before, "must not clobber Traktor's own edit"


def test_update_skips_on_conflict(tmp_path):
    r, before, after = _update_with_owner(tmp_path, "both")
    assert r.tracks_skipped >= 1
    assert after == before


def test_update_skips_when_attribution_has_no_history(tmp_path):
    r, before, after = _update_with_owner(tmp_path, None)
    assert r.tracks_skipped >= 1
    assert after == before


# ---------------- attribution is per pair, and fails safe ------------------
def _update_with_owner_map(tmp_path, owners):
    """As _update_with_owner, but the caller supplies the whole owners map --
    so a map that says nothing about THIS pair can be exercised."""
    paths = write_sample(tmp_path)
    tlib = traktor_read.read(paths.nml)
    key = next(k for k, t in tlib.tracks.items() if len(t.pois) > 1)
    tgt = tlib.tracks[key]
    cues_before = len(tgt.pois)
    src = Track(key=key, path=tgt.path, source=VDJ, title=tgt.title,
                artist=tgt.artist, album=tgt.album, genre=tgt.genre,
                comment=tgt.comment, pois=[Poi(1.0, None, "beatgrid")])
    vlib, node = _fake_vdj_with_rocking(paths, src)
    engine = SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths,
                        start_time_cache=None, owners=owners(key))
    r = engine.sync_playlist(node, VDJ, UPDATE)
    after = len(traktor_read.read(str(tmp_path / "collection.nml")).tracks[key].pois)
    return r, cues_before, after


def test_a_pair_with_no_verdicts_skips_rather_than_falling_back(tmp_path):
    """Attribution that ran and found nothing must NOT degrade into the
    timestamp rule. VirtualDJ's LastModified is exactly the signal we decided
    can't answer this, so falling back would overwrite on evidence we know is
    untrustworthy -- silently, and only for pairs that happen to be missing."""
    r, before, after = _update_with_owner_map(tmp_path, lambda key: {})

    assert r.tracks_skipped >= 1
    assert after == before, "an unknown pair must never authorise a write"


def test_one_pairs_verdict_cannot_authorise_another_pairs_write(tmp_path):
    """A track can be owned by one program against one peer and by that peer
    against another. If the maps were shared, SlipStream's verdict would decide
    what gets written to Traktor."""
    other_pair = bl.pair_id(VDJ, "slipstream")

    r, before, after = _update_with_owner_map(
        tmp_path, lambda key: {other_pair: {key: VDJ}})

    assert r.tracks_skipped >= 1
    assert after == before, "the wrong pair's verdict leaked through"


def test_new_mode_does_not_touch_existing_collection_cues(tmp_path):
    paths = write_sample(tmp_path)
    nml_path = tmp_path / "collection.nml"
    tlib = traktor_read.read(paths.nml)
    existing_key = next(iter(tlib.tracks))
    cues_before = len(tlib.tracks[existing_key].pois)
    src_existing = Track(key=existing_key, path=tlib.tracks[existing_key].path,
                         source=VDJ, title="x", artist="y", pois=[])
    vlib, rocking = _fake_vdj_with_rocking(paths, src_existing)
    engine = SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=None)
    engine.sync_playlist(rocking, VDJ, NEW)

    tlib2 = traktor_read.read(str(nml_path))
    assert len(tlib2.tracks[existing_key].pois) == cues_before


# ------------------- idempotency / folder mirroring ------------------------
def _vdj_playlist_at(paths, folder_path, name="Breaks Exotic"):
    """A VDJ library with one playlist at ``folder_path + (name,)`` whose tracks
    already exist (with identical cues) in the Traktor collection."""
    tlib = traktor_read.read(paths.nml)
    vlib = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    keys = []
    for k in list(tlib.tracks)[:2]:
        vlib.tracks[k] = Track(key=k, path=tlib.tracks[k].path, source=VDJ,
                               title="t", artist="a", pois=list(tlib.tracks[k].pois))
        keys.append(k)
    node = TreeNode(name=name, kind="playlist", source=VDJ,
                    node_path=tuple(folder_path) + (name,),
                    playlist=Playlist(name=name, source=VDJ, track_keys=keys))
    return vlib, tlib, node


def _sync(paths, folder_path, name="Breaks Exotic"):
    vlib, tlib, node = _vdj_playlist_at(paths, folder_path, name)
    eng = SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=None)
    found = eng._find_target_node(node, tlib)
    eng.sync_playlist(node, VDJ, UPDATE)
    return found


def test_repeated_sync_does_not_duplicate_the_playlist(tmp_path):
    """The reported bug: each Sync Update appended another copy of the node."""
    paths = write_sample(tmp_path)
    nml_path = tmp_path / "collection.nml"
    for run in range(3):
        found = _sync(paths, ("Breaks",))
        out = nml_path.read_text(encoding="utf-8")
        assert out.count('NAME="Breaks Exotic"') == 1, f"duplicate created on run {run+1}"
        if run > 0:
            assert found is not None, "the existing node must be found on re-sync"


def test_missing_folder_chain_is_created(tmp_path):
    paths = write_sample(tmp_path)
    _sync(paths, ("NewFolder",))
    out = (tmp_path / "collection.nml").read_text(encoding="utf-8")
    assert re.search(r'<NODE TYPE="FOLDER" NAME="NewFolder"', out)
    # playlist sits inside it, and the folder's own COUNT is 1
    assert re.search(r'NAME="NewFolder"><SUBNODES COUNT="1">', out)
    # $ROOT gained exactly one child
    assert re.search(r'NAME="\$ROOT"><SUBNODES COUNT="5"', out)
    tlib = traktor_read.read(str(paths.nml))
    assert [p.node_path for p in tlib.playlists() if p.name == "Breaks Exotic"] \
        == [("NewFolder", "Breaks Exotic")]


def test_nested_folder_chain_is_created(tmp_path):
    paths = write_sample(tmp_path)
    _sync(paths, ("Outer", "Inner"))
    out = (tmp_path / "collection.nml").read_text(encoding="utf-8")
    assert re.search(r'<NODE TYPE="FOLDER" NAME="Outer"', out)
    assert re.search(r'<NODE TYPE="FOLDER" NAME="Inner"', out)
    tlib = traktor_read.read(str(paths.nml))
    assert [p.node_path for p in tlib.playlists() if p.name == "Breaks Exotic"] \
        == [("Outer", "Inner", "Breaks Exotic")]
    # and it is still idempotent through the nested chain
    _sync(paths, ("Outer", "Inner"))
    out2 = (tmp_path / "collection.nml").read_text(encoding="utf-8")
    assert out2.count('NAME="Breaks Exotic"') == 1


def test_existing_folder_is_reused_case_insensitively(tmp_path):
    """VDJ 'Breaks' must land in Traktor's existing lowercase 'breaks' folder,
    not spawn a near-duplicate twin."""
    paths = write_sample(tmp_path)
    _sync(paths, ("Breaks",))
    out = (tmp_path / "collection.nml").read_text(encoding="utf-8")
    assert out.count('<NODE TYPE="FOLDER" NAME="breaks"') == 1
    assert '<NODE TYPE="FOLDER" NAME="Breaks"' not in out, "no case-variant twin"
    assert re.search(r'NAME="breaks"[^>]*><SUBNODES COUNT="2">', out)  # test + new


def test_preexisting_duplicates_are_left_alone(tmp_path):
    """We stop making new duplicates but never delete ones already there."""
    paths = write_sample(tmp_path)
    _sync(paths, ("Breaks",))
    nml_path = tmp_path / "collection.nml"
    out = nml_path.read_text(encoding="utf-8")
    # forge a second copy by hand
    node_xml = re.search(r'<NODE TYPE="PLAYLIST" NAME="Breaks Exotic">.*?</NODE>',
                         out, re.DOTALL).group(0)
    nml_path.write_text(out.replace(node_xml, node_xml + node_xml, 1), encoding="utf-8")
    assert nml_path.read_text(encoding="utf-8").count('NAME="Breaks Exotic"') == 2

    _sync(paths, ("Breaks",))
    assert nml_path.read_text(encoding="utf-8").count('NAME="Breaks Exotic"') == 2


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))


# ============ reporting: Traktor -> VDJ playlist work is counted ============
# "Tracks added" counts COLLECTION additions and is structurally 0 when the VDJ
# database is a superset of the Traktor collection -- which made a successful
# sync look like a no-op. The playlist work must be reported separately.
def _traktor_playlist_to_vdj(tmp_path):
    """A Traktor playlist whose tracks all already exist on the VDJ side."""
    from trackcleaner.model.library import TRAKTOR
    paths = write_sample(tmp_path)
    tlib = traktor_read.read(paths.nml)
    vlib = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    keys = list(tlib.tracks)
    # mirror every Traktor track into the VDJ collection, cues and all
    for k in keys:
        t = tlib.tracks[k]
        vlib.tracks[k] = Track(key=k, path=t.path, source=VDJ, title=t.title,
                               artist=t.artist, album=t.album, genre=t.genre,
                               comment=t.comment, pois=list(t.pois))
    node = TreeNode(name="FromTraktor", kind="playlist", source=TRAKTOR,
                    node_path=("FromTraktor",),
                    playlist=Playlist(name="FromTraktor", source=TRAKTOR,
                                      track_keys=keys))
    return paths, tlib, vlib, node, keys


def test_traktor_to_vdj_reports_playlist_work_even_when_nothing_is_added(tmp_path):
    from trackcleaner.model.library import TRAKTOR
    paths, tlib, vlib, node, keys = _traktor_playlist_to_vdj(tmp_path)
    engine = SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=None, owners={})
    r = engine.sync_playlist(node, TRAKTOR, UPDATE)

    # the reported symptom: nothing to add to the collection...
    assert r.tracks_added == 0
    # ...but the playlist was genuinely created with every track
    assert r.playlists_created == 1
    assert r.playlist_entries_added == len(keys)

    dest = vdj_write.vdjfolder_path_for(paths.vdj_folders_dir(), node.node_path)
    assert os.path.exists(dest)
    assert len(re.findall(r"<song ", open(dest, encoding="utf-8").read())) == len(keys)


def test_second_sync_reports_no_new_playlist(tmp_path):
    from trackcleaner.model.library import TRAKTOR
    paths, tlib, vlib, node, keys = _traktor_playlist_to_vdj(tmp_path)
    SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=None,
               owners={}).sync_playlist(node, TRAKTOR, UPDATE)
    # re-read so the VDJ library now knows about the playlist we just wrote
    vlib2 = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    for k in keys:
        vlib2.tracks[k] = vlib.tracks[k]
    r = SyncEngine({VDJ: vlib2, TRAKTOR: tlib}, paths, start_time_cache=None,
                   owners={}).sync_playlist(node, TRAKTOR, UPDATE)
    assert r.playlists_created == 0
    assert r.playlist_entries_added == 0


def test_vdjfolder_only_track_is_created_and_counted(tmp_path):
    """A VDJ track known only from a .vdjfolder has no <Song> block, so it must
    be created (and counted) rather than treated as already in the collection."""
    from trackcleaner.model.library import TRAKTOR
    paths = write_sample(tmp_path)
    tlib = traktor_read.read(paths.nml)
    vlib = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    key = next(iter(tlib.tracks))
    t = tlib.tracks[key]
    # present in memory, but flagged as absent from database.xml
    vlib.tracks[key] = Track(key=key, path=t.path, source=VDJ, title=t.title,
                             artist=t.artist, pois=[], in_database=False)
    node = TreeNode(name="P", kind="playlist", source=TRAKTOR, node_path=("P",),
                    playlist=Playlist(name="P", source=TRAKTOR, track_keys=[key]))
    r = SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=None,
                   owners={}).sync_playlist(node, TRAKTOR, NEW)

    assert r.tracks_added == 1, "must create the missing <Song> block"
    xml = open(paths.xml, encoding="utf-8").read()
    assert 'FilePath="%s"' % t.path.replace("&", "&amp;") in xml


# ------------------- ensure_track_in_target (cross-program drag-add) -------
def test_ensure_track_in_target_creates_a_missing_vdj_entry(tmp_path):
    """Traktor -> VDJ: a track that exists in the Traktor collection but has no
    <Song> block in database.xml gets one created, cue-corrected the same way
    a normal sync would."""
    from trackcleaner.model.library import TRAKTOR
    paths = write_sample(tmp_path)
    tlib = traktor_read.read(paths.nml)
    vlib = vdj_read.read(paths.xml, paths.vdj_folders_dir())    # empty database.xml
    key = next(iter(tlib.tracks))
    assert key not in vlib.tracks

    engine = SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=None)
    created = engine.ensure_track_in_target(key, TRAKTOR)

    assert created is True
    assert key in vlib.tracks, "in-memory library updated so the UI sees it immediately"
    xml = open(paths.xml, encoding="utf-8").read()
    assert "<Song " in xml and 'FilePath=' in xml


def test_ensure_track_in_target_is_a_no_op_when_already_present(tmp_path):
    from trackcleaner.model.library import TRAKTOR
    paths = write_sample(tmp_path)
    tlib = traktor_read.read(paths.nml)
    vlib = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    key = next(iter(tlib.tracks))
    engine = SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=None)

    assert engine.ensure_track_in_target(key, TRAKTOR) is True    # first call creates it
    before = open(paths.xml, encoding="utf-8").read()
    assert engine.ensure_track_in_target(key, TRAKTOR) is False   # already there now
    after = open(paths.xml, encoding="utf-8").read()
    assert before == after, "a no-op must not touch the file"


def test_ensure_track_in_target_creates_a_missing_traktor_entry(tmp_path):
    """VDJ -> Traktor: the reverse direction, same guarantee."""
    paths = write_sample(tmp_path)
    tlib = traktor_read.read(paths.nml)
    vlib = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    new_key = r"d:\music\funk\brand new.mp3"
    vlib.tracks[new_key] = Track(key=new_key, path=r"D:\Music\Funk\Brand New.mp3",
                                 source=VDJ, title="Brand New", artist="Nobody", pois=[])
    engine = SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=None)

    created = engine.ensure_track_in_target(new_key, VDJ)

    assert created is True
    assert new_key in tlib.tracks
    nml = open(paths.nml, encoding="utf-8").read()
    assert 'TITLE="Brand New"' in nml


def test_ensure_track_in_target_missing_from_source_is_a_safe_no_op(tmp_path):
    from trackcleaner.model.library import TRAKTOR
    paths = write_sample(tmp_path)
    tlib = traktor_read.read(paths.nml)
    vlib = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    engine = SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=None)
    assert engine.ensure_track_in_target("no-such-key", TRAKTOR) is False
