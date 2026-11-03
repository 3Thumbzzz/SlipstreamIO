"""Playlist membership removal (Update mirrors the source) + grey tombstones.

Update makes the target's membership exactly match the source's, so a track
dropped on the source side is dropped on the target too -- including tracks the
*other* program added, which is why the UI greys them as a warning.
"""

import os
import re
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.io import traktor_read, traktor_write, vdj_read, vdj_write  # noqa: E402
from trackcleaner.model import status as st                          # noqa: E402
from trackcleaner.model.baseline import Baseline                     # noqa: E402
from trackcleaner.model.library import (Library, Playlist, Track,     # noqa: E402
                                        TreeNode, TRAKTOR, VDJ, path_ci)
from trackcleaner.sync.engine import SyncEngine, NEW, UPDATE          # noqa: E402
from fixtures import write_sample                                     # noqa: E402

PL_PATH = ("breaks", "test")
PL_KEY = "/".join(path_ci(PL_PATH))


def _seed_playlist_with_whole_collection(paths):
    """The fixture's 'test' playlist holds one track; widen it to the whole
    collection so there is something to remove."""
    tlib = traktor_read.read(paths.nml)
    node = next(n for n in tlib.playlists() if n.name == "test")
    tracks = [tlib.tracks[k] for k in tlib.tracks]
    nml = open(paths.nml, encoding="utf-8").read()
    nml = traktor_write.set_playlist_entries(nml, node.playlist.traktor_uuid, tracks)
    open(paths.nml, "w", encoding="utf-8").write(nml)
    return traktor_read.read(paths.nml), [t.key for t in tracks]


# --------------------------- writer primitive ---------------------------
def test_set_playlist_entries_mirrors_and_fixes_count(tmp_path):
    paths = write_sample(tmp_path)
    tlib, keys = _seed_playlist_with_whole_collection(paths)
    uuid = next(n for n in tlib.playlists() if n.name == "test").playlist.traktor_uuid
    nml = open(paths.nml, encoding="utf-8").read()

    assert traktor_write.playlist_entry_keys(nml, uuid) == keys
    assert re.search(r'<PLAYLIST ENTRIES="%d"[^>]*UUID="%s"' % (len(keys), uuid), nml)

    out = traktor_write.set_playlist_entries(nml, uuid, [tlib.tracks[keys[0]]])
    assert traktor_write.playlist_entry_keys(out, uuid) == [keys[0]]
    assert re.search(r'<PLAYLIST ENTRIES="1"[^>]*UUID="%s"' % uuid, out)
    ET.fromstring(out)                                   # still well-formed
    assert re.search(r'NAME="_LOOPS"><PLAYLIST ENTRIES="0"', out)   # siblings safe


def test_playlist_entry_keys_roundtrips_through_matching(tmp_path):
    paths = write_sample(tmp_path)
    tlib, keys = _seed_playlist_with_whole_collection(paths)
    node = next(n for n in tlib.playlists() if n.name == "test")
    nml = open(paths.nml, encoding="utf-8").read()
    assert traktor_write.playlist_entry_keys(nml, node.playlist.traktor_uuid) \
        == list(node.playlist.track_keys)


# ------------------------------ engine ---------------------------------
def _vdj_mirror_of(paths, tlib, keys, kept):
    """A VDJ library whose copy of the playlist holds only ``kept``."""
    vlib = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    for k in keys:
        t = tlib.tracks[k]
        vlib.tracks[k] = Track(key=k, path=t.path, source=VDJ, title=t.title,
                               artist=t.artist, album=t.album, genre=t.genre,
                               comment=t.comment, pois=list(t.pois))
    node = TreeNode(name="test", kind="playlist", source=VDJ, node_path=PL_PATH,
                    playlist=Playlist(name="test", source=VDJ, track_keys=list(kept)))
    return vlib, node


def test_update_removes_tracks_dropped_on_the_source(tmp_path):
    paths = write_sample(tmp_path)
    tlib, keys = _seed_playlist_with_whole_collection(paths)
    uuid = next(n for n in tlib.playlists() if n.name == "test").playlist.traktor_uuid
    kept = keys[:-1]
    vlib, vnode = _vdj_mirror_of(paths, tlib, keys, kept)

    r = SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=None,
                   owners={}).sync_playlist(vnode, VDJ, UPDATE)

    assert r.playlist_entries_removed == len(keys) - len(kept)
    out = open(paths.nml, encoding="utf-8").read()
    assert traktor_write.playlist_entry_keys(out, uuid) == kept
    ET.fromstring(out)


def test_new_mode_never_removes(tmp_path):
    paths = write_sample(tmp_path)
    tlib, keys = _seed_playlist_with_whole_collection(paths)
    uuid = next(n for n in tlib.playlists() if n.name == "test").playlist.traktor_uuid
    vlib, vnode = _vdj_mirror_of(paths, tlib, keys, keys[:-1])

    r = SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=None,
                   owners={}).sync_playlist(vnode, VDJ, NEW)

    assert r.playlist_entries_removed == 0
    out = open(paths.nml, encoding="utf-8").read()
    assert traktor_write.playlist_entry_keys(out, uuid) == keys


def test_traktor_to_vdj_update_removes_from_the_vdjfolder(tmp_path):
    paths = write_sample(tmp_path)
    tlib, keys = _seed_playlist_with_whole_collection(paths)
    tnode = next(n for n in tlib.playlists() if n.name == "test")
    vlib, _ = _vdj_mirror_of(paths, tlib, keys, keys)

    # a .vdjfolder that currently holds everything
    dest = vdj_write.vdjfolder_path_for(paths.vdj_folders_dir(), tnode.node_path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    vdj_write.write_vdjfolder(dest, [vlib.tracks[k] for k in keys])
    vlib2 = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    for k in keys:
        vlib2.tracks[k] = vlib.tracks[k]

    tnode.playlist.track_keys = keys[:-1]          # Traktor dropped the last one
    r = SyncEngine({VDJ: vlib2, TRAKTOR: tlib}, paths, start_time_cache=None,
                   owners={}).sync_playlist(tnode, TRAKTOR, UPDATE)

    assert r.playlist_entries_removed == 1
    assert len(re.findall(r"<song ", open(dest, encoding="utf-8").read())) == len(keys) - 1


# --------------------------- divergence shades --------------------------
def _pair(baseline):
    """The baseline is scoped per program pair; these tests are all about
    the VirtualDJ<->Traktor one."""
    return baseline.for_pair(VDJ, TRAKTOR)


def _libs_with(vdj_keys, traktor_keys):
    def lib(source, keys):
        node = TreeNode(name="PL", kind="playlist", source=source, node_path=("PL",),
                        playlist=Playlist(name="PL", source=source, track_keys=list(keys)))
        return Library(source=source,
                       tracks={k: Track(key=k, path=k, source=source) for k in keys},
                       root=TreeNode("r", "folder", source, children=[node]))
    return lib(VDJ, vdj_keys), lib(TRAKTOR, traktor_keys)


def test_divergence_marks_losing_and_gaining_on_the_stale_side():
    b = Baseline()
    v, t = _libs_with(["a", "b", "x"], ["a", "b", "x"])          # agreed state
    st.attribute_playlists(v, t, b)
    assert _pair(b).get_members("pl") == ["a", "b", "x"]

    # VDJ drops x; Traktor gains y. The marker lands on the *un-edited* copy:
    v2, t2 = _libs_with(["a", "b"], ["a", "b", "x", "y"])
    st.attribute_playlists(v2, t2, b)
    dv = st.playlist_divergence(v2, t2, b)[("pl",)]
    # Traktor still holds x (VDJ removed it) -> Traktor will LOSE x on sync.
    assert dv[TRAKTOR]["losing"] == ["x"]
    assert dv[TRAKTOR]["gaining"] == []
    # VDJ lacks y (Traktor added it) -> VDJ will GAIN y on sync.
    assert dv[VDJ]["gaining"] == ["y"]
    assert dv[VDJ]["losing"] == []


def test_snapshot_is_sticky_until_the_sides_agree():
    b = Baseline()
    v, t = _libs_with(["a", "b", "x"], ["a", "b", "x"])
    st.attribute_playlists(v, t, b)

    v2, t2 = _libs_with(["a", "b"], ["a", "b", "x"])   # VDJ dropped x; Traktor still has it
    for _ in range(3):                       # repeated refreshes must not forget
        st.attribute_playlists(v2, t2, b)
        assert st.playlist_divergence(v2, t2, b)[("pl",)][TRAKTOR]["losing"] == ["x"]
    assert _pair(b).get_members("pl") == ["a", "b", "x"], "snapshot must not move"

    # once both sides agree again the snapshot advances and the marker clears
    v3, t3 = _libs_with(["a", "b"], ["a", "b"])
    st.attribute_playlists(v3, t3, b)
    assert _pair(b).get_members("pl") == ["a", "b"]
    dv = st.playlist_divergence(v3, t3, b)[("pl",)]
    assert dv[VDJ] == {"losing": [], "gaining": []}
    assert dv[TRAKTOR] == {"losing": [], "gaining": []}


def test_order_only_change_still_advances_the_snapshot():
    b = Baseline()
    v, t = _libs_with(["a", "b"], ["b", "a"])
    st.attribute_playlists(v, t, b)
    assert set(_pair(b).get_members("pl")) == {"a", "b"}


def test_members_survive_a_csv_round_trip(tmp_path):
    path = str(tmp_path / "b.csv")
    b = Baseline(path)
    view = _pair(b)
    view.set_members("pl", ["a", "b", "x"])
    view.set("pl", "fp1", "fp2", "vdj", kind="playlist")   # must not clobber members
    b.save()
    assert _pair(Baseline(path).load()).get_members("pl") == ["a", "b", "x"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
