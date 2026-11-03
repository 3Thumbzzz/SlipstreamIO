"""Cue positions travel source -> native -> target through the engine.

`test_adapter_timeline.py` pins the *composition* against the original pairwise
correction. This pins that the engine actually performs it: that a real
start-time cache reaches the adapters, that the shift lands in the written file
with the right sign, and that a Class-B file is left alone.

The engine used to hold the pairwise rule itself (`corrected_pos_nml_to_vdj` /
`_vdj_to_nml` chosen from a `direction` string). Every existing engine test
passes `start_time_cache=None`, which makes the correction a no-op -- so the one
thing that would catch a sign error or a dropped cache had no coverage on this
side. Hence this module.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.io import traktor_read, vdj_read                   # noqa: E402
from trackcleaner.model import matching                              # noqa: E402
from trackcleaner.model.cues import (CORRECTION_OFFSET_SEC, Poi,     # noqa: E402
                                     StartTimeCache)
from trackcleaner.model.library import (Playlist, Track, TreeNode,   # noqa: E402
                                        TRAKTOR, VDJ)
from trackcleaner.sync.engine import OVERWRITE, SyncEngine           # noqa: E402
from fixtures import write_sample                                    # noqa: E402

# Comfortably clear of the zero-clamp, so a round trip is exact and the sign of
# the shift is unambiguous.
CUE_POS = 30.0


def _class_a_cache(tmp_path, local_path):
    """A cache that calls one file Class A -- a container whose first sample is
    delayed, which is what the correction exists for."""
    csv_path = tmp_path / "start_times.csv"
    csv_path.write_text(
        "start_time,bucket,path\n0.100000,A,%s\n" % local_path.lower(),
        encoding="utf-8")
    cache = StartTimeCache(str(csv_path), "cache")
    cache.load()
    return cache


def _class_b_cache(tmp_path, local_path):
    csv_path = tmp_path / "start_times.csv"
    csv_path.write_text(
        "start_time,bucket,path\n0.000000,B,%s\n" % local_path.lower(),
        encoding="utf-8")
    cache = StartTimeCache(str(csv_path), "cache")
    cache.load()
    return cache


@pytest.fixture
def libs(tmp_path):
    """A VirtualDJ library holding one track with a single cue at CUE_POS, and
    the Traktor library it will be synced into."""
    paths = write_sample(tmp_path)
    tlib = traktor_read.read(paths.nml)
    key = next(iter(tlib.tracks))
    stored = tlib.tracks[key].path

    vlib = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    vlib.tracks[key] = Track(key=key, path=stored, source=VDJ,
                             pois=[Poi(CUE_POS, 1, "cue")])
    node = TreeNode(name="P", kind="playlist", source=VDJ, node_path=("P",),
                    playlist=Playlist(name="P", source=VDJ, track_keys=[key]))
    return paths, vlib, tlib, node, key, stored


def _synced_cue(paths, key):
    after = traktor_read.read(paths.nml).tracks[key]
    cues = [p for p in after.pois if p.kind == "cue"]
    assert len(cues) == 1, f"expected one cue, got {[p.kind for p in after.pois]}"
    return cues[0].pos


# ============================ the shift happens =============================
def test_a_class_a_file_is_shifted_by_the_verified_offset(tmp_path, libs):
    """VirtualDJ -> Traktor on a Class-A file moves the cue by the documented
    amount. Which way is asserted below; this pins the magnitude."""
    paths, vlib, tlib, node, key, stored = libs
    cache = _class_a_cache(tmp_path, matching.resolve_local_path(stored))

    SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=cache).sync_playlist(
        node, VDJ, OVERWRITE)

    assert abs(_synced_cue(paths, key) - CUE_POS) == \
        pytest.approx(CORRECTION_OFFSET_SEC, abs=1e-6)


def test_the_shift_has_the_same_sign_as_the_pairwise_rule(tmp_path, libs):
    """A sign error here would be silent: every cue would land one offset the
    wrong way, which reads as a 103 ms error rather than as nothing working."""
    from trackcleaner.model.cues import corrected_pos_vdj_to_nml

    paths, vlib, tlib, node, key, stored = libs
    cache = _class_a_cache(tmp_path, matching.resolve_local_path(stored))

    SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=cache).sync_playlist(
        node, VDJ, OVERWRITE)

    assert _synced_cue(paths, key) == \
        pytest.approx(corrected_pos_vdj_to_nml(CUE_POS, "A"), abs=1e-6)


def test_a_class_b_file_is_not_shifted(tmp_path, libs):
    """Class B means the container honours no delay, so the two programs already
    agree and touching the position would introduce an error."""
    paths, vlib, tlib, node, key, stored = libs
    cache = _class_b_cache(tmp_path, matching.resolve_local_path(stored))

    SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=cache).sync_playlist(
        node, VDJ, OVERWRITE)

    assert _synced_cue(paths, key) == pytest.approx(CUE_POS, abs=1e-6)


def test_an_unknown_file_is_never_shifted(tmp_path, libs):
    """HANDOFF.md, "Cue timing": no start time known means no correction, because
    guessing wrong is worse than doing nothing."""
    paths, vlib, tlib, node, key, _ = libs
    empty = StartTimeCache(str(tmp_path / "nothing.csv"), "cache")
    empty.load()

    SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=empty).sync_playlist(
        node, VDJ, OVERWRITE)

    assert _synced_cue(paths, key) == pytest.approx(CUE_POS, abs=1e-6)


# ====================== the cache actually reaches them =====================
def test_the_engine_hands_its_cache_to_the_adapters(tmp_path, libs):
    """The conversion is the adapters' job now, and they can only do it if the
    cache gets to them. Without this the correction would silently become a
    no-op -- which looks exactly like a Class-B library."""
    paths, vlib, tlib, _, _, stored = libs
    cache = _class_a_cache(tmp_path, matching.resolve_local_path(stored))

    engine = SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, start_time_cache=cache)

    assert all(a.start_times is cache for a in engine.adapters.values())
    assert engine.adapter_for(TRAKTOR).timeline_offset(stored) != 0.0, \
        "Traktor's offset is what encodes the Class-A correction"


def test_adapters_passed_in_are_used_rather_than_rebuilt(tmp_path, libs):
    """The window builds adapters once and hands them over; rebuilding would
    quietly drop whatever state they already hold."""
    from trackcleaner.adapters import build_adapters

    paths, vlib, tlib, _, _, _ = libs
    mine = build_adapters(paths)

    engine = SyncEngine({VDJ: vlib, TRAKTOR: tlib}, paths, adapters=mine)

    assert engine.adapters is mine


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
