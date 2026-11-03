"""The status law: yellow marks the newer side of a real difference.

See ``trackcleaner/model/status.py`` (THE LAW) for the rules under test.
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.model import status as st                      # noqa: E402
from trackcleaner.model.cues import Poi                          # noqa: E402
from trackcleaner.model.library import (Library, Playlist, Track,  # noqa: E402
                                        TreeNode, TRAKTOR, VDJ)

T0 = datetime(2026, 7, 22, 12, 0, 0)
LATER = T0 + timedelta(hours=1)


def _track(key="k", *, pois=(), modified=T0, source=VDJ, **kw):
    return Track(key=key, path=r"D:\Music\A.mp3", source=source,
                 pois=list(pois), modified=modified, **kw)


def _libs(src_track, tgt_track):
    """Libraries labelled with each track's OWN software, so ``source.source``
    (which drives attribution) is meaningful."""
    s = Library(source=src_track.source, tracks={src_track.key: src_track})
    t = Library(source=tgt_track.source, tracks={tgt_track.key: tgt_track})
    return s, t


def _status(src_track, tgt_track, changed_by=None, reference=TRAKTOR):
    """``changed_by`` is {key: set(programs)} -- the cross-program tally, not a
    single owner id."""
    s, t = _libs(src_track, tgt_track)
    return st.diff_tracks([src_track.key], s, t, changed_by,
                          reference=reference).track_status[src_track.key]


# ------------------------------ track law ------------------------------
def test_the_glyph_says_which_side_changed():
    """Attribution, not timestamps, decides the shape. Both sides are yellow --
    they differ -- but each is told something different about why."""
    vdj_side = _track(pois=[Poi(10.0, 1, "cue")], source=VDJ)
    trk_side = _track(pois=[Poi(20.0, 1, "cue")], source=TRAKTOR)

    # Traktor (the reference) changed it.
    changed = {"k": {"traktor"}}
    assert _status(trk_side, vdj_side, changed) == st.YELLOW_SELF   # I changed it
    assert _status(vdj_side, trk_side, changed) == st.YELLOW_REF    # I am behind

    # VirtualDJ changed it: the reference is neither author nor behind, so it
    # gets the fork -- "somebody else moved".
    changed = {"k": {"vdj"}}
    assert _status(vdj_side, trk_side, changed) == st.YELLOW_SELF
    assert _status(trk_side, vdj_side, changed) == st.YELLOW_OTHER


def test_both_owner_lights_up_both_sides():
    a = _track(pois=[Poi(10.0, 1, "cue")], source=VDJ)
    b = _track(pois=[Poi(20.0, 1, "cue")], source=TRAKTOR)
    changed = {"k": {"vdj", "traktor"}}
    assert _status(a, b, changed) == st.YELLOW_SELF
    assert _status(b, a, changed) == st.YELLOW_SELF


def test_no_attribution_history_flags_both_sides():
    a = _track(pois=[Poi(10.0, 1, "cue")], source=VDJ)
    b = _track(pois=[Poi(20.0, 1, "cue")], source=TRAKTOR)
    assert _status(a, b, {}) == st.YELLOW_SELF
    assert _status(b, a, {}) == st.YELLOW_SELF


def test_cue_diff_equal_timestamps_flags_both_sides():
    a = _track(pois=[Poi(10.0, 1, "cue")], modified=T0)
    b = _track(pois=[Poi(20.0, 1, "cue")], modified=T0)
    assert st.is_changed(_status(a, b))
    assert st.is_changed(_status(b, a))


def test_cue_diff_missing_timestamp_flags_both_sides():
    a = _track(pois=[Poi(10.0, 1, "cue")], modified=None)
    b = _track(pois=[Poi(20.0, 1, "cue")], modified=T0)
    assert st.is_changed(_status(a, b))
    assert st.is_changed(_status(b, a))


def test_class_a_offset_still_counts_as_synced():
    """Traktor 7.865 vs VDJ 7.8135 is the same musical position."""
    a = _track(pois=[Poi(7.865, 1, "cue")], modified=LATER)
    b = _track(pois=[Poi(7.8135, 1, "cue")], modified=T0)
    assert _status(a, b) == st.GREEN


# ------------------- the zero-clamp (Osibisa - Fire) -------------------
# The writer floors at zero, so a Traktor cue at 0.0 lands on VDJ 0.0 instead of
# -0.051536. That one clamped point used to break the uniform-shift test and
# left the track permanently yellow, unfixable even by Overwrite.
_FIRE_TRAKTOR = [0.0, 13.159, 29.359, 64.728, 75.85]
_FIRE_VDJ = [0.0, 13.107464, 29.307464, 64.676464, 75.798464]


def _cues(positions):
    return [Poi(p, i + 1, "cue") for i, p in enumerate(positions)]


def test_zero_clamped_cue_reads_as_synced_both_directions():
    trk = _track(pois=_cues(_FIRE_TRAKTOR), source=TRAKTOR)
    vdj = _track(pois=_cues(_FIRE_VDJ), source=VDJ)
    assert st.cues_equivalent(trk, vdj)
    assert st.cues_equivalent(vdj, trk)      # must be symmetric
    assert _status(trk, vdj) == st.GREEN
    assert _status(vdj, trk) == st.GREEN


def test_zero_clamped_beatgrid_reads_as_synced():
    trk = _track(pois=[Poi(0.0, None, "beatgrid"), Poi(13.159, None, "beatgrid")],
                 source=TRAKTOR)
    vdj = _track(pois=[Poi(0.0, None, "beatgrid"), Poi(13.107464, None, "beatgrid")],
                 source=VDJ)
    assert st.cues_equivalent(trk, vdj)
    assert st.cues_equivalent(vdj, trk)


def test_clamped_round_trip_is_stable():
    """vdj 0.0 -> nml max(0+C,0)=0.051536 -> back to vdj 0.0."""
    trk = _track(pois=_cues([0.051536, 13.159]), source=TRAKTOR)
    vdj = _track(pois=_cues([0.0, 13.107464]), source=VDJ)
    assert st.cues_equivalent(trk, vdj)
    assert st.cues_equivalent(vdj, trk)


def test_a_genuinely_moved_cue_is_still_a_difference():
    """The clamp tolerance must not swallow a real edit."""
    moved = list(_FIRE_TRAKTOR)
    moved[2] += 0.5
    trk = _track(pois=_cues(moved), source=TRAKTOR)
    vdj = _track(pois=_cues(_FIRE_VDJ), source=VDJ)
    assert not st.cues_equivalent(trk, vdj)
    assert not st.cues_equivalent(vdj, trk)


def test_cue_count_mismatch_is_still_a_difference():
    trk = _track(pois=_cues(_FIRE_TRAKTOR), source=TRAKTOR)
    vdj = _track(pois=_cues(_FIRE_VDJ[:-1]), source=VDJ)
    assert not st.cues_equivalent(trk, vdj)


def test_metadata_only_difference_is_a_difference():
    a = _track(album="Real Album", source=VDJ)
    b = _track(album="Other Album", source=TRAKTOR)
    assert not st.content_matches(a, b)
    assert _status(a, b, {"k": {"vdj"}}) == st.YELLOW_SELF
    # The reference is not the author and is not behind: somebody else moved.
    assert _status(b, a, {"k": {"vdj"}}) == st.YELLOW_OTHER


def test_blank_vs_populated_is_a_difference():
    a = _track(album="Homenaje", modified=LATER)
    b = _track(album="", modified=T0)
    assert not st.metadata_matches(a, b)
    assert st.is_changed(_status(a, b))


def test_key_is_excluded_from_comparison():
    a = _track(key_musical="12m", modified=LATER)
    b = _track(key_musical="Am", modified=T0)
    assert st.metadata_matches(a, b)
    assert _status(a, b) == st.GREEN


def test_metadata_compare_is_trimmed_and_case_insensitive():
    assert st.metadata_matches(_track(title=" Homenaje "), _track(title="homenaje"))


def test_missing_in_target_is_black_missing_in_source_is_red():
    src = _track()
    s = Library(source=VDJ, tracks={src.key: src})
    empty = Library(source=TRAKTOR, tracks={})
    assert st.diff_tracks([src.key], s, empty).track_status[src.key] == st.BLACK
    assert st.diff_tracks(["ghost"], s, empty).track_status["ghost"] == st.RED


def test_newer_side_helper():
    assert st.newer_side(_track(modified=LATER), _track(modified=T0)) == "a"
    assert st.newer_side(_track(modified=T0), _track(modified=LATER)) == "b"
    assert st.newer_side(_track(modified=T0), _track(modified=T0)) is None
    assert st.newer_side(_track(modified=None), _track(modified=T0)) is None


# ----------------------------- playlist law ----------------------------
def _node(name, keys, source=VDJ, vdj_file=""):
    return TreeNode(name=name, kind="playlist", source=source, node_path=(name,),
                    playlist=Playlist(name=name, source=source,
                                      track_keys=list(keys), vdj_file=vdj_file))


def _report(colours):
    return st.Report(track_status=dict(colours))


def test_playlist_absent_in_target_is_black():
    src = _node("PL", ["a"])
    assert _report({"a": st.GREEN}).playlist_status(src, {}) == st.BLACK


def test_playlist_all_green_same_order_is_green():
    src = _node("PL", ["a", "b"])
    tgt = _node("PL", ["a", "b"], source=TRAKTOR)
    idx = {("pl",): tgt}
    assert _report({"a": st.GREEN, "b": st.GREEN}).playlist_status(src, idx) == st.GREEN


def test_playlist_order_change_is_shaped_by_who_changed_it():
    """Reordering is a change like any other; the glyph says whose. Note the
    reference case: it used to come back GREEN, which is what let a playlist
    read as synced while its rows showed incoming tracks."""
    src = _node("PL", ["a", "b"])            # VDJ side
    tgt = _node("PL", ["b", "a"], source=TRAKTOR)
    idx = {("pl",): tgt}
    rep = _report({"a": st.GREEN, "b": st.GREEN})
    ref = TRAKTOR

    assert rep.playlist_status(src, idx, {"vdj"}, ref) == st.YELLOW_SELF
    assert rep.playlist_status(src, idx, {"traktor"}, ref) == st.YELLOW_REF
    assert rep.playlist_status(src, idx, {"slipstream"}, ref) == st.YELLOW_OTHER
    # Both moved: your own edit is the more useful thing to be told.
    assert rep.playlist_status(src, idx, {"vdj", "traktor"}, ref) == st.YELLOW_SELF
    # No history is not evidence of agreement; it falls back to the long-standing
    # "both sides light up" rule rather than to green.
    assert rep.playlist_status(src, idx, None, ref) == st.YELLOW_SELF


def test_playlist_membership_change_is_shaped_by_who_changed_it():
    src = _node("PL", ["a", "b"])
    tgt = _node("PL", ["a"], source=TRAKTOR)
    idx = {("pl",): tgt}
    rep = _report({"a": st.GREEN, "b": st.GREEN})

    assert rep.playlist_status(src, idx, {"vdj"}, TRAKTOR) == st.YELLOW_SELF
    assert rep.playlist_status(src, idx, {"traktor"}, TRAKTOR) == st.YELLOW_REF


def test_a_playlist_that_differs_is_never_green():
    """The reported bug, stated directly: whatever attribution says -- including
    nothing at all -- a copy that does not match the reference must not claim to
    be synced."""
    src = _node("PL", ["a", "b"])
    tgt = _node("PL", ["a"], source=TRAKTOR)
    idx = {("pl",): tgt}
    rep = _report({"a": st.GREEN, "b": st.GREEN})

    for changed_by in (None, set(), {"vdj"}, {"traktor"}, {"slipstream"},
                       {"vdj", "traktor"}):
        assert rep.playlist_status(src, idx, changed_by, TRAKTOR) != st.GREEN


def test_playlist_same_structure_but_track_yellow_is_yellow():
    src = _node("PL", ["a"])
    tgt = _node("PL", ["a"], source=TRAKTOR)
    idx = {("pl",): tgt}
    got = _report({"a": st.YELLOW_SELF}).playlist_status(src, idx, {"vdj"}, TRAKTOR)
    assert st.is_changed(got)


def test_playlist_red_track_wins():
    src = _node("PL", ["a"])
    tgt = _node("PL", ["a"], source=TRAKTOR)
    idx = {("pl",): tgt}
    assert _report({"a": st.RED}).playlist_status(src, idx) == st.RED


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
