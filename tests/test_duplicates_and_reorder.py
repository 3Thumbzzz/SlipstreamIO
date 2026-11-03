"""VDJ duplicate policy (noDuplicates round-trip) and the in-place reorder math.

These are pure -- no QApplication needed: ``render_vdjfolder`` / ``_read_vdjfolder``
are plain functions and ``TrackTable._reorder_keys`` is a staticmethod.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.io import vdj_read, vdj_write                   # noqa: E402
from trackcleaner.model.library import Track, VDJ                 # noqa: E402
from trackcleaner.ui.track_table import TrackTable                # noqa: E402


# ------------------------------ noDuplicates -------------------------------
def test_render_emits_the_flag():
    assert 'noDuplicates="no"' in vdj_write.render_vdjfolder([], no_duplicates=False)
    assert 'noDuplicates="yes"' in vdj_write.render_vdjfolder([], no_duplicates=True)


def test_read_parses_the_flag(tmp_path):
    for flag, expected in (("no", False), ("yes", True)):
        p = tmp_path / f"pl_{flag}.vdjfolder"
        p.write_text(f'<?xml version="1.0"?>\n<VirtualFolder noDuplicates="{flag}">\n'
                     f'<song path="D:\\a.mp3" />\n</VirtualFolder>', encoding="utf-8")
        keys, nodup = vdj_read._read_vdjfolder(str(p), {})
        assert nodup is expected and len(keys) == 1


def test_read_defaults_missing_flag_to_disallow(tmp_path):
    p = tmp_path / "pl.vdjfolder"
    p.write_text('<VirtualFolder>\n<song path="D:\\a.mp3" />\n</VirtualFolder>', encoding="utf-8")
    _, nodup = vdj_read._read_vdjfolder(str(p), {})
    assert nodup is True


def test_render_read_round_trip(tmp_path):
    t = Track(key="d:/a.mp3", path=r"D:\a.mp3", source=VDJ, title="A")
    for nodup in (True, False):
        p = tmp_path / "rt.vdjfolder"
        p.write_text(vdj_write.render_vdjfolder([t], nodup), encoding="utf-8")
        _, got = vdj_read._read_vdjfolder(str(p), {})
        assert got is nodup


# ------------------------------ reorder math -------------------------------
def test_reorder_single_row_forward():
    assert TrackTable._reorder_keys(["a", "b", "c", "d"], [0], 2) == ["b", "a", "c", "d"]


def test_reorder_single_row_backward():
    assert TrackTable._reorder_keys(["a", "b", "c", "d"], [3], 1) == ["a", "d", "b", "c"]


def test_reorder_multiple_rows_to_end():
    assert TrackTable._reorder_keys(["a", "b", "c", "d"], [0, 1], 4) == ["c", "d", "a", "b"]


def test_reorder_respects_duplicate_positions():
    # move the SECOND 'a' (row 2) to the front -- position-based, not key-based
    assert TrackTable._reorder_keys(["a", "b", "a", "c"], [2], 0) == ["a", "a", "b", "c"]


def test_reorder_noop_when_selection_empty():
    assert TrackTable._reorder_keys(["a", "b"], [], 0) == ["a", "b"]


# --------------------- cooperative-reload yield hooks -----------------------
def test_traktor_read_yield_fn_is_called_and_result_unchanged(tmp_path):
    from trackcleaner.io import traktor_read
    from fixtures import SAMPLE_NML
    p = tmp_path / "collection.nml"
    p.write_text(SAMPLE_NML, encoding="utf-8")
    calls = []
    lib_plain = traktor_read.read(str(p))
    lib_yield = traktor_read.read(str(p), yield_fn=lambda: calls.append(1))
    assert calls, "yield_fn was never called"
    assert set(lib_yield.tracks) == set(lib_plain.tracks), "yield_fn changed the parse"


def test_diff_tracks_yield_fn_is_called_and_result_unchanged():
    from trackcleaner.model import status as st
    from trackcleaner.model.library import Library, Track, TRAKTOR, VDJ
    keys = [f"k{i}" for i in range(5)]
    src = Library(source=VDJ, tracks={k: Track(key=k, path=k, source=VDJ) for k in keys})
    tgt = Library(source=TRAKTOR, tracks={k: Track(key=k, path=k, source=TRAKTOR) for k in keys})
    calls = []
    r_plain = st.diff_tracks(keys, src, tgt)
    r_yield = st.diff_tracks(keys, src, tgt, yield_fn=lambda: calls.append(1))
    assert calls, "yield_fn was never called"
    assert r_yield.track_status == r_plain.track_status


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
