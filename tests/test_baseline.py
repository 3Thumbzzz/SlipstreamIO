"""Change attribution: whichever side's content actually moved gets the yellow.

Timestamps are deliberately NOT consulted -- see model/baseline.py for why
VirtualDJ's LastModified can't answer "who edited the cues".
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.model import baseline as bl                     # noqa: E402
from trackcleaner.model import status as st                       # noqa: E402
from trackcleaner.model.cues import CORRECTION_OFFSET_SEC, Poi    # noqa: E402
from trackcleaner.model.library import (Library, Playlist, Track,  # noqa: E402
                                        TreeNode, TRAKTOR, VDJ)

KEY = "d:\\music\\a.mp3"
# a deliberately misleading clock: VDJ looks newer even though Traktor edited
VDJ_TIME = datetime(2026, 7, 23, 0, 33, 57)
TRK_TIME = datetime(2026, 7, 22, 15, 37, 38)


def _pair(vdj_pois, trk_pois, **kw):
    v = Track(key=KEY, path=r"D:\Music\A.mp3", source=VDJ, title="T", artist="A",
              pois=list(vdj_pois), modified=VDJ_TIME, **kw)
    t = Track(key=KEY, path=r"D:\Music\A.mp3", source=TRAKTOR, title="T", artist="A",
              pois=list(trk_pois), modified=TRK_TIME)
    return v, t


def _libs(v, t):
    return (Library(source=VDJ, tracks={v.key: v}),
            Library(source=TRAKTOR, tracks={t.key: t}))


def _run(v, t, baseline):
    """Attribute the difference, then ask each side what glyph it shows.
    Traktor is the reference throughout."""
    vlib, tlib = _libs(v, t)
    owners = st.attribute_changes([KEY], vlib, tlib, baseline)
    changed = {}
    for key, owner in owners.items():
        st.merge_owner(changed, key, owner, VDJ, TRAKTOR)
    vdj_colour = st.diff_tracks([KEY], vlib, tlib, changed,
                                reference=TRAKTOR).track_status[KEY]
    trk_colour = st.diff_tracks([KEY], tlib, vlib, changed,
                                reference=TRAKTOR).track_status[KEY]
    return owners.get(KEY), vdj_colour, trk_colour


# ------------------------- fingerprinting -------------------------
def test_fingerprint_ignores_float_noise_but_sees_a_real_move():
    a = Track(key=KEY, path="p", source=VDJ, pois=[Poi(10.0, 1, "cue")])
    b = Track(key=KEY, path="p", source=VDJ, pois=[Poi(10.0000001, 1, "cue")])
    c = Track(key=KEY, path="p", source=VDJ, pois=[Poi(10.5, 1, "cue")])
    assert bl.fingerprint(a) == bl.fingerprint(b)
    assert bl.fingerprint(a) != bl.fingerprint(c)


def test_fingerprint_covers_metadata():
    a = Track(key=KEY, path="p", source=VDJ, title="One")
    b = Track(key=KEY, path="p", source=VDJ, title="Two")
    assert bl.fingerprint(a) != bl.fingerprint(b)


# --------------------------- attribution ---------------------------
def test_traktor_edit_is_attributed_to_traktor_despite_newer_vdj_clock():
    """The reported bug: VDJ's LastModified is newer, but Traktor did the edit."""
    b = bl.Baseline()
    cue = [Poi(31.547166, 1, "cue")]
    v, t = _pair(cue, cue)
    owner, vdj_c, trk_c = _run(v, t, b)
    assert owner is None and vdj_c == trk_c == st.GREEN     # seed an in-sync baseline

    # now the Traktor side alone changes
    v2, t2 = _pair(cue, [Poi(31.547166, 1, "cue"), Poi(26.154405, 2, "cue")])
    owner, vdj_c, trk_c = _run(v2, t2, b)
    assert owner == "traktor"
    assert trk_c == st.YELLOW_SELF, "Traktor made the edit"
    # NOT green: VDJ no longer matches the reference, so it says so -- the square
    # means "the reference moved and you are behind it".
    assert vdj_c == st.YELLOW_REF


def test_vdj_edit_is_attributed_to_vdj():
    b = bl.Baseline()
    cue = [Poi(5.0, 1, "cue")]
    _run(*_pair(cue, cue), b)
    owner, vdj_c, trk_c = _run(*_pair([Poi(9.0, 1, "cue")], cue), b)
    assert owner == "vdj"
    assert vdj_c == st.YELLOW_SELF
    # The reference is neither the author nor behind: somebody else moved.
    assert trk_c == st.YELLOW_OTHER


def test_both_sides_changed_is_a_conflict():
    b = bl.Baseline()
    cue = [Poi(5.0, 1, "cue")]
    _run(*_pair(cue, cue), b)
    owner, vdj_c, trk_c = _run(*_pair([Poi(9.0, 1, "cue")], [Poi(7.0, 1, "cue")]), b)
    assert owner == "both"
    assert vdj_c == st.YELLOW_SELF and trk_c == st.YELLOW_SELF


def test_attribution_is_sticky_across_an_unchanged_refresh():
    b = bl.Baseline()
    cue = [Poi(5.0, 1, "cue")]
    _run(*_pair(cue, cue), b)
    changed = _pair(cue, [Poi(7.0, 1, "cue")])
    assert _run(*changed, b)[0] == "traktor"
    # refresh again with nothing moving -> verdict must persist
    assert _run(*changed, b)[0] == "traktor"


def test_returning_to_sync_clears_the_owner():
    b = bl.Baseline()
    cue = [Poi(5.0, 1, "cue")]
    _run(*_pair(cue, cue), b)
    assert _run(*_pair(cue, [Poi(7.0, 1, "cue")]), b)[0] == "traktor"
    owner, vdj_c, trk_c = _run(*_pair(cue, cue), b)
    assert owner is None and vdj_c == trk_c == st.GREEN


def test_no_history_is_unknown_not_a_conflict():
    """Never having compared the two while they agreed is ignorance, not a claim
    that both sides edited it. They were the same verdict once, and that made
    Sync Update decline a first-contact library citing "the other side owns the
    change" -- about tracks the other side had never touched.

    The squares are unchanged: with no evidence either way, both sides still
    have to read as possibly-changed.
    """
    owner, vdj_c, trk_c = _run(*_pair([Poi(5.0, 1, "cue")], [Poi(7.0, 1, "cue")]),
                               bl.Baseline())
    assert owner == bl.UNKNOWN
    assert owner != bl.BOTH
    assert vdj_c == st.YELLOW_SELF and trk_c == st.YELLOW_SELF


def test_both_still_means_both_sides_moved():
    """The real conflict keeps its own verdict."""
    b = bl.Baseline()
    _run(*_pair([Poi(5.0, 1, "cue")], [Poi(5.0, 1, "cue")]), b)   # agree first
    owner, _v, _t = _run(*_pair([Poi(9.0, 1, "cue")], [Poi(3.0, 1, "cue")]), b)

    assert owner == bl.BOTH


def test_class_a_offset_alone_is_never_a_change():
    """The two sides legitimately differ by the correction constant."""
    b = bl.Baseline()
    trk = [Poi(7.865, 1, "cue")]
    vdj = [Poi(7.865 - CORRECTION_OFFSET_SEC, 1, "cue")]
    owner, vdj_c, trk_c = _run(*_pair(vdj, trk), b)
    assert owner is None
    assert vdj_c == trk_c == st.GREEN


# ------------------------------ storage -----------------------------
def test_csv_round_trip(tmp_path):
    path = str(tmp_path / "sync_baseline.csv")
    b = bl.Baseline(path)
    v = b.for_pair(VDJ, TRAKTOR)
    v.set("k1", "aaa", "bbb", "traktor")
    v.set("p1", "ccc", "ddd", "vdj", kind="playlist")
    b.save()

    again = bl.Baseline(path).load().for_pair(VDJ, TRAKTOR)
    row = again.get("k1")
    assert (row.mine_fp, row.theirs_fp, row.owner) == ("aaa", "bbb", "traktor")
    prow = again.get("p1", "playlist")
    assert (prow.mine_fp, prow.theirs_fp, prow.owner) == ("ccc", "ddd", "vdj")
    assert again.get("k1", "playlist") is None      # buckets stay separate


# --------------------------- pair scoping ---------------------------
# Attribution is only meaningful between two programs, so with three loaded
# there is a pair per (program, reference). These pin that the pairs cannot
# contaminate each other -- if they shared fingerprints, each pair's refresh
# would overwrite the last's and every track would read as a conflict forever.
def test_two_pairs_keep_separate_fingerprints(tmp_path):
    b = bl.Baseline(str(tmp_path / "b.csv"))

    b.for_pair(VDJ, TRAKTOR).set("k", "v-fp", "t-fp", "vdj")
    b.for_pair(VDJ, "slipstream").set("k", "v-fp", "s-fp", "both")

    assert b.for_pair(VDJ, TRAKTOR).get("k").theirs_fp == "t-fp"
    assert b.for_pair(VDJ, "slipstream").get("k").theirs_fp == "s-fp"
    assert b.for_pair(VDJ, TRAKTOR).get("k").owner == "vdj"


def test_a_pair_reads_the_same_whichever_way_round_it_is_asked_for(tmp_path):
    """(vdj, traktor) and (traktor, vdj) are one comparison. Two histories would
    mean the answer depended on which sidebar row you happened to click."""
    b = bl.Baseline(str(tmp_path / "b.csv"))

    b.for_pair(VDJ, TRAKTOR).set("k", "mine-v", "theirs-t", "vdj")

    flipped = b.for_pair(TRAKTOR, VDJ).get("k")
    assert flipped.mine_fp == "theirs-t", "the flip must swap the sides"
    assert flipped.theirs_fp == "mine-v"
    assert len(b.pairs()) == 1, "and must not have made a second pair"


def test_members_are_per_pair_too(tmp_path):
    b = bl.Baseline(str(tmp_path / "b.csv"))

    b.for_pair(VDJ, TRAKTOR).set_members("pl", ["a", "b"])

    assert b.for_pair(VDJ, TRAKTOR).get_members("pl") == ["a", "b"]
    assert b.for_pair(VDJ, "slipstream").get_members("pl") == []
    assert not b.for_pair(VDJ, "slipstream").has_members("pl")


def test_pairs_survive_a_round_trip(tmp_path):
    path = str(tmp_path / "b.csv")
    b = bl.Baseline(path)
    b.for_pair(VDJ, TRAKTOR).set("k", "v", "t", "vdj")
    b.for_pair(VDJ, "slipstream").set("k", "v", "s", "both")
    b.save()

    again = bl.Baseline(path).load()

    assert again.for_pair(VDJ, TRAKTOR).get("k").owner == "vdj"
    assert again.for_pair(VDJ, "slipstream").get("k").theirs_fp == "s"


def test_a_baseline_written_before_pairs_existed_is_still_read(tmp_path):
    """Upgrading must not silently discard the user's attribution history --
    that would turn every track yellow once, and "both" (conflict) at that, so
    an Update sync would refuse to write until they resolved each one by hand.
    """
    path = tmp_path / "sync_baseline.csv"
    path.write_text(
        "kind,key,vdj_fp,traktor_fp,owner,members\n"
        "track,k1,vvv,ttt,traktor,\n"
        "playlist,p1,ccc,ddd,vdj,a|b\n",
        encoding="utf-8")

    view = bl.Baseline(str(path)).load().for_pair(VDJ, TRAKTOR)

    row = view.get("k1")
    assert (row.mine_fp, row.theirs_fp, row.owner) == ("vvv", "ttt", "traktor")
    assert view.get_members("p1") == ["a", "b"]


def test_the_legacy_rows_land_in_the_vdj_traktor_pair_specifically(tmp_path):
    """Not just "some pair" -- reading them into the wrong one would look like
    history was kept while actually starting from scratch."""
    path = tmp_path / "sync_baseline.csv"
    path.write_text("kind,key,vdj_fp,traktor_fp,owner,members\n"
                    "track,k1,vvv,ttt,traktor,\n", encoding="utf-8")

    b = bl.Baseline(str(path)).load()

    assert b.pairs() == [bl.pair_id(VDJ, TRAKTOR)]
    assert b.for_pair(VDJ, "slipstream").get("k1") is None


# ----------------------------- playlists ----------------------------
def _pl(name, keys, source):
    return TreeNode(name=name, kind="playlist", source=source, node_path=(name,),
                    playlist=Playlist(name=name, source=source, track_keys=list(keys)))


def test_playlist_reorder_is_attributed(tmp_path):
    b = bl.Baseline()
    vlib = Library(source=VDJ, root=TreeNode("r", "folder", VDJ,
                                             children=[_pl("PL", ["a", "b"], VDJ)]))
    tlib = Library(source=TRAKTOR, root=TreeNode("r", "folder", TRAKTOR,
                                                 children=[_pl("PL", ["a", "b"], TRAKTOR)]))
    assert st.attribute_playlists(vlib, tlib, b) == {}      # in sync

    # Traktor reorders
    tlib2 = Library(source=TRAKTOR, root=TreeNode("r", "folder", TRAKTOR,
                                                  children=[_pl("PL", ["b", "a"], TRAKTOR)]))
    owners = st.attribute_playlists(vlib, tlib2, b)
    assert owners[("pl",)] == "traktor"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
