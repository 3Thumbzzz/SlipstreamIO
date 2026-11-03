"""Playlist-level status.

Guards the defect where a playlist that had never been synced showed
yellow/green simply because a *different*, already-synced playlist happened to
share its tracks: track colours describe cue state, so they can be green for
reasons that have nothing to do with this playlist existing.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.model.cues import Poi                                    # noqa: E402
from trackcleaner.model.library import (Library, Playlist, Track, TreeNode,  # noqa: E402
                                        TRAKTOR, VDJ)
from trackcleaner.model.status import (BLACK, GREEN, RED, is_changed,      # noqa: E402
                                       diff_tracks)


def _track(key, source, pois=None):
    return Track(key=key, path=key, source=source, pois=list(pois or []))


def _node(name, keys, source=VDJ, folder=()):
    return TreeNode(name=name, kind="playlist", source=source,
                    node_path=tuple(folder) + (name,),
                    playlist=Playlist(name=name, source=source, track_keys=list(keys)))


def _libs(shared_pois):
    """Two libraries whose tracks a/b are already cue-identical (green)."""
    keys = ["a", "b"]
    vdj = Library(source=VDJ, tracks={k: _track(k, VDJ, shared_pois) for k in keys})
    trk = Library(source=TRAKTOR, tracks={k: _track(k, TRAKTOR, shared_pois) for k in keys})
    return vdj, trk


POIS = [Poi(1.0, None, "beatgrid"), Poi(2.0, 1, "cue")]


def test_unsynced_playlist_is_black_even_when_its_tracks_are_green():
    """The reported bug: 'Synced' carried a+b across, so both tracks read
    green; 'Unsynced' shares those tracks but exists only in VirtualDJ."""
    vdj, trk = _libs(POIS)
    synced = _node("Synced", ["a", "b"])
    unsynced = _node("Unsynced", ["a", "b"])
    vdj.root = TreeNode("VDJ", "folder", VDJ, children=[synced, unsynced])
    # only "Synced" exists on the Traktor side
    trk.root = TreeNode("T", "folder", TRAKTOR,
                        children=[_node("Synced", ["a", "b"], TRAKTOR)])

    report = diff_tracks(list(vdj.tracks), vdj, trk)
    assert all(c == GREEN for c in report.track_status.values()), "tracks are in sync"

    idx = trk.playlist_index()
    assert report.playlist_status(synced, idx) == GREEN
    assert report.playlist_status(unsynced, idx) == BLACK, \
        "a playlist absent from the other program must read as unsynced"


def test_present_but_missing_members_is_yellow():
    vdj, trk = _libs(POIS)
    src = _node("P", ["a", "b"])
    vdj.root = TreeNode("VDJ", "folder", VDJ, children=[src])
    trk.root = TreeNode("T", "folder", TRAKTOR,
                        children=[_node("P", ["a"], TRAKTOR)])   # missing "b"
    report = diff_tracks(list(vdj.tracks), vdj, trk)
    assert is_changed(report.playlist_status(src, trk.playlist_index()))


def test_present_with_all_members_and_cues_is_green():
    vdj, trk = _libs(POIS)
    src = _node("P", ["a", "b"])
    vdj.root = TreeNode("VDJ", "folder", VDJ, children=[src])
    trk.root = TreeNode("T", "folder", TRAKTOR, children=[_node("P", ["a", "b"], TRAKTOR)])
    report = diff_tracks(list(vdj.tracks), vdj, trk)
    assert report.playlist_status(src, trk.playlist_index()) == GREEN


def test_present_but_cues_differ_is_yellow():
    vdj = Library(source=VDJ, tracks={"a": _track("a", VDJ, POIS)})
    trk = Library(source=TRAKTOR,
                  tracks={"a": _track("a", TRAKTOR, [Poi(1.0, None, "beatgrid")])})
    src = _node("P", ["a"])
    vdj.root = TreeNode("VDJ", "folder", VDJ, children=[src])
    trk.root = TreeNode("T", "folder", TRAKTOR, children=[_node("P", ["a"], TRAKTOR)])
    report = diff_tracks(list(vdj.tracks), vdj, trk)
    assert is_changed(report.playlist_status(src, trk.playlist_index()))


def test_unfound_track_is_red_when_playlist_exists():
    vdj, trk = _libs(POIS)
    src = _node("P", ["a", "ghost"])          # "ghost" isn't in the source library
    vdj.root = TreeNode("VDJ", "folder", VDJ, children=[src])
    trk.root = TreeNode("T", "folder", TRAKTOR,
                        children=[_node("P", ["a", "ghost"], TRAKTOR)])
    report = diff_tracks(src.playlist.track_keys, vdj, trk)
    assert report.track_status["ghost"] == RED
    assert report.playlist_status(src, trk.playlist_index()) == RED


def test_match_is_case_insensitive_on_the_folder_path():
    """VDJ 'Breaks/P' must match Traktor's 'breaks/P' (same rule the writer
    uses when it places the node)."""
    vdj, trk = _libs(POIS)
    src = _node("P", ["a", "b"], folder=("Breaks",))
    vdj.root = TreeNode("VDJ", "folder", VDJ, children=[src])
    tgt = _node("P", ["a", "b"], TRAKTOR, folder=("breaks",))
    trk.root = TreeNode("T", "folder", TRAKTOR, children=[tgt])
    report = diff_tracks(list(vdj.tracks), vdj, trk)
    assert report.playlist_status(src, trk.playlist_index()) == GREEN


def test_empty_playlist_present_is_green_absent_is_black():
    vdj, trk = _libs(POIS)
    src = _node("Empty", [])
    vdj.root = TreeNode("VDJ", "folder", VDJ, children=[src])
    trk.root = TreeNode("T", "folder", TRAKTOR, children=[])
    report = diff_tracks([], vdj, trk)
    assert report.playlist_status(src, trk.playlist_index()) == BLACK
    trk.root = TreeNode("T", "folder", TRAKTOR, children=[_node("Empty", [], TRAKTOR)])
    assert report.playlist_status(src, trk.playlist_index()) == GREEN


def test_one_differing_track_in_a_long_playlist_is_yellow_on_both_sides():
    r"""The reported "identical playlist shows a yellow triangle", reduced.

    Membership and order matched exactly (517 = 517) and 516 tracks were green;
    a single track's cue sat 12 ms off where the Class-A correction would put
    it, and that is enough -- correctly -- to make the square yellow in both
    panes. Reproduced from the supplied libraries:

        TRAKTOR              VIRTUALDJ
        grid  0.577638       grid  0.526102     -51.536 ms  the correction
        cue5  0.577638       cue5  0.514017     -63.621 ms  12 ms further
        cue1  8.577695       cue1  8.526159     -51.536 ms
        cue2 16.577752       cue2 16.526216     -51.536 ms

    ``cues_equivalent`` already allows the constant shift in either direction,
    so three of the four points match and cue 5 does not. Pinned because the
    obvious "fix" -- widening the tolerance until this reads green -- would hide
    a real difference that only a sync can reconcile.
    """
    same = [Poi(0.577638, None, "beatgrid"), Poi(0.577638, 5, "cue"),
            Poi(8.577695, 1, "cue"), Poi(16.577752, 2, "cue")]
    shifted = [Poi(0.526102, None, "beatgrid"), Poi(0.514017, 5, "cue"),
               Poi(8.526159, 1, "cue"), Poi(16.526216, 2, "cue")]
    keys = [f"t{i}" for i in range(20)]
    trk = Library(source=TRAKTOR,
                  tracks={k: _track(k, TRAKTOR, same) for k in keys})
    vdj = Library(source=VDJ,
                  tracks={k: _track(k, VDJ, same) for k in keys})
    odd = keys[7]
    vdj.tracks[odd] = _track(odd, VDJ, shifted)      # the one that really differs

    t_node = _node("Breaks Nu Funk", keys, TRAKTOR)
    v_node = _node("Breaks Nu Funk", keys, VDJ)
    trk.root = TreeNode("T", "folder", TRAKTOR, children=[t_node])
    vdj.root = TreeNode("V", "folder", VDJ, children=[v_node])

    r_trk = diff_tracks(list(trk.tracks), trk, vdj, reference=VDJ)
    r_vdj = diff_tracks(list(vdj.tracks), vdj, trk, reference=VDJ)

    assert [k for k in keys if r_trk.track_status.get(k) != GREEN] == [odd]
    assert [k for k in keys if r_vdj.track_status.get(k) != GREEN] == [odd]
    assert t_node.playlist.track_keys == v_node.playlist.track_keys, \
        "membership and order are identical -- the yellow is not about those"
    assert r_trk.playlist_status(t_node, vdj.playlist_index()) != GREEN
    assert r_vdj.playlist_status(v_node, trk.playlist_index()) != GREEN


def test_the_class_a_shift_alone_is_still_in_sync():
    """The control for the case above: every point off by exactly the correction
    is what a correctly synced Class-A track looks like, and must read green."""
    C = 0.051536
    nml = [Poi(0.577638, None, "beatgrid"), Poi(0.577638, 5, "cue"),
           Poi(8.577695, 1, "cue")]
    vdj_side = [Poi(p.pos - C, p.num, p.kind) for p in nml]
    trk = Library(source=TRAKTOR, tracks={"a": _track("a", TRAKTOR, nml)})
    vdj = Library(source=VDJ, tracks={"a": _track("a", VDJ, vdj_side)})
    t_node, v_node = _node("P", ["a"], TRAKTOR), _node("P", ["a"], VDJ)
    trk.root = TreeNode("T", "folder", TRAKTOR, children=[t_node])
    vdj.root = TreeNode("V", "folder", VDJ, children=[v_node])

    report = diff_tracks(list(trk.tracks), trk, vdj, reference=VDJ)

    assert report.track_status["a"] == GREEN
    assert report.playlist_status(t_node, vdj.playlist_index()) == GREEN


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
