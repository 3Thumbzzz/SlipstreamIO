"""Each adapter converts to and from ONE canonical timeline.

Canonical ("native") position = seconds from the first decoded audio sample.
Traktor <-> VirtualDJ then composes as traktor -> native -> vdj, so a third
program never needs to know the first two exist.

**This file exists to prove that re-expressing the correction did not change
it.** ``model/cues.py`` is load-bearing and its constants are verified against
ear-checked tracks (HANDOFF.md, "Cue timing"), so the tests below assert the
composition is *identical* to the old pairwise functions rather than merely
plausible. Any drift here is a real regression in cue placement, which is
inaudible in a test suite and very audible in a club.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.adapters.traktor import TraktorAdapter                # noqa: E402
from trackcleaner.adapters.vdj import VdjAdapter                        # noqa: E402
from trackcleaner.config.paths import Paths                             # noqa: E402
from trackcleaner.model.cues import (CORRECTION_OFFSET_SEC,             # noqa: E402
                                     corrected_pos_nml_to_vdj,
                                     corrected_pos_vdj_to_nml)

CLASS_A_PATH = r"D:\Music\class_a.mp3"
CLASS_B_PATH = r"D:\Music\class_b.mp3"
UNKNOWN_PATH = r"D:\Music\never_probed.mp3"


class _FakeStartTimes:
    """Stands in for StartTimeCache: Class A has a delay, B is zero, anything
    else is unknown (and must never be shifted)."""

    def get(self, local_path):
        low = (local_path or "").lower()
        if "class_a" in low:
            return 0.025057          # a real observed value; HANDOFF "Cue timing"
        if "class_b" in low:
            return 0.0
        return None


@pytest.fixture
def adapters():
    paths = Paths(nml="x.nml", xml="y.xml", csv="")
    traktor, vdj = TraktorAdapter(paths), VdjAdapter(paths)
    traktor.start_times = vdj.start_times = _FakeStartTimes()
    return traktor, vdj


POSITIONS = [0.0, 0.01, 0.051536, 0.06, 1.0, 45.378570, 3600.5]


# ========================= the offsets themselves ===========================
def test_traktor_is_late_on_a_class_a_file(adapters):
    traktor, _ = adapters
    assert traktor.timeline_offset(CLASS_A_PATH) == -CORRECTION_OFFSET_SEC


def test_traktor_passes_class_b_and_unknown_through(adapters):
    traktor, _ = adapters
    assert traktor.timeline_offset(CLASS_B_PATH) == 0.0
    assert traktor.timeline_offset(UNKNOWN_PATH) == 0.0, \
        "an unprobed file must never be shifted"


def test_virtualdj_stores_canonical_positions(adapters):
    _, vdj = adapters
    for path in (CLASS_A_PATH, CLASS_B_PATH, UNKNOWN_PATH):
        assert vdj.timeline_offset(path) == 0.0


def test_an_adapter_without_a_cache_never_shifts(adapters):
    """Before the start-time cache is loaded the class is unknowable, and
    guessing would move cues on the wrong files."""
    traktor, _ = adapters
    traktor.start_times = None
    assert traktor.timeline_offset(CLASS_A_PATH) == 0.0


# ================== the invariant: composition is unchanged =================
@pytest.mark.parametrize("path", [CLASS_A_PATH, CLASS_B_PATH, UNKNOWN_PATH])
@pytest.mark.parametrize("pos", POSITIONS)
def test_traktor_to_vdj_matches_the_verified_pairwise_correction(adapters, path, pos):
    traktor, vdj = adapters
    category = "A" if "class_a" in path else ("B" if "class_b" in path else None)

    through_native = vdj.from_native_pos(traktor.to_native_pos(pos, path), path)

    assert through_native == corrected_pos_nml_to_vdj(pos, category)


@pytest.mark.parametrize("path", [CLASS_A_PATH, CLASS_B_PATH, UNKNOWN_PATH])
@pytest.mark.parametrize("pos", POSITIONS)
def test_vdj_to_traktor_matches_the_verified_pairwise_correction(adapters, path, pos):
    traktor, vdj = adapters
    category = "A" if "class_a" in path else ("B" if "class_b" in path else None)

    through_native = traktor.from_native_pos(vdj.to_native_pos(pos, path), path)

    assert through_native == corrected_pos_vdj_to_nml(pos, category)


# ============================ shape guarantees ==============================
@pytest.mark.parametrize("pos", [p for p in POSITIONS if p >= CORRECTION_OFFSET_SEC])
def test_a_round_trip_through_native_is_a_no_op_away_from_zero(adapters, pos):
    """Reading a program's own number out and back must not move it, or a
    re-sync would creep the cue by the offset every time."""
    traktor, _ = adapters
    for path in (CLASS_A_PATH, CLASS_B_PATH, UNKNOWN_PATH):
        assert traktor.from_native_pos(
            traktor.to_native_pos(pos, path), path) == pytest.approx(pos, abs=1e-9)


def test_positions_are_floored_at_zero(adapters):
    """The clamp is what the writer already does: a cue at t=0 on a Class A file
    lands on 0.0, never a negative position."""
    traktor, _ = adapters
    assert traktor.to_native_pos(0.0, CLASS_A_PATH) == 0.0
    assert traktor.to_native_pos(0.01, CLASS_A_PATH) == 0.0


def test_the_zero_clamp_is_lossy_and_that_is_expected(adapters):
    """Below the offset the round trip is deliberately NOT reversible: every
    Class-A position under 51.536 ms collapses onto 0.0, so coming back gives
    the offset rather than the original.

    This is pre-existing, documented behaviour, not a side effect of moving the
    correction onto the adapters -- ``status._lists_match`` models the same
    clamp, and its docstring is why: without it a correctly-synced Class-A track
    whose earliest cue sits at 0 could never read as in sync. Pinned here so
    nobody 'fixes' the asymmetry and quietly breaks that.
    """
    traktor, _ = adapters
    for pos in (0.0, 0.01, 0.05):
        assert traktor.to_native_pos(pos, CLASS_A_PATH) == 0.0
    assert traktor.from_native_pos(0.0, CLASS_A_PATH) == CORRECTION_OFFSET_SEC


def test_the_constant_is_still_the_verified_one():
    """HANDOFF.md, "Cue timing": exactly 0.051536 s, to six decimal places, across
    every sample rate, bitrate and encoder tested."""
    assert CORRECTION_OFFSET_SEC == 0.051536


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
