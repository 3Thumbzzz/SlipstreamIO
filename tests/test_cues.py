"""Verification of the cue-correction math against HANDOFF.md, "Cue timing".

Run: ``python -m pytest tests/ -q``  (or ``python tests/test_cues.py``)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.io import traktor_write      # noqa: E402
from trackcleaner.model import cues, matching  # noqa: E402


def test_class_a_forward_correction():
    # Cut Master DC (Class A): cue 0.140563 -> 0.089027, grid 17.6937 -> 17.642164
    assert abs(cues.corrected_pos_nml_to_vdj(0.140563, "A") - 0.089027) < 1e-6
    assert abs(cues.corrected_pos_nml_to_vdj(17.6937, "A") - 17.642164) < 1e-6


def test_class_b_unchanged():
    assert cues.corrected_pos_nml_to_vdj(0.140563, "B") == 0.140563
    assert cues.corrected_pos_nml_to_vdj(0.140563, None) == 0.140563


def test_round_trip_class_a_is_stable():
    for pos in (0.140563, 17.6937, 105.417007):
        vdj = cues.corrected_pos_nml_to_vdj(pos, "A")
        back = cues.corrected_pos_vdj_to_nml(vdj, "A")
        assert abs(back - pos) < 1e-6


def test_floor_at_zero():
    # a Class-A cue near t=0 can't go negative
    assert cues.corrected_pos_nml_to_vdj(0.01, "A") == 0.0


def test_classify():
    assert cues.classify(None) is None
    assert cues.classify(0.0) == "B"
    assert cues.classify(0.0004) == "B"
    assert cues.classify(0.025057) == "A"
    assert cues.classify(0.011995) == "A"


def test_cue_v2_extraction_beat_marker():
    # A TYPE=4 with a real HOTCUE emits BOTH a beatgrid and a cue.
    pois = cues.pois_from_cue_v2([
        {"TYPE": "4", "START": "15440.166875", "HOTCUE": "0"},
        {"TYPE": "0", "START": "7897.762458", "HOTCUE": "4"},
        {"TYPE": "0", "START": "1000.0", "HOTCUE": "-1"},   # ignored
    ])
    kinds = sorted((p.kind, p.num) for p in pois)
    assert ("beatgrid", None) in kinds
    assert ("cue", 1) in kinds        # HOTCUE 0 -> Num 1
    assert ("cue", 5) in kinds        # HOTCUE 4 -> Num 5
    assert len([p for p in pois if p.kind == "cue"]) == 2


def test_path_matching_apostrophe():
    """``&apos;`` must decode, or every apostrophe filename fails to match.

    Python's ``unescape`` does not decode it by default, which is why
    ``_EXTRA_ENTITIES`` passes it explicitly. Asserted through the writer's
    ``_entry_key``, which is a raw-text call site and therefore owns the one
    decode -- ``path_key`` no longer does it, since it is also handed values an
    XML parser has already decoded (see ``test_entity_paths.py``).
    """
    location = ('<LOCATION DIR="/:Music/:x/:" FILE="Walkin&apos;.mp3" '
                'VOLUME="D:"></LOCATION>')
    assert traktor_write._entry_key(location) == r"d:\music\x\walkin'.mp3"
    assert matching.decode_entities("Walkin&apos;.mp3") == "Walkin'.mp3"


def test_primarykey_roundtrip():
    win = r"D:\Music\Funk\Track.mp3"
    key = matching.windows_path_to_traktor_key(win)
    assert key == "D:/:Music/:Funk/:Track.mp3"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
