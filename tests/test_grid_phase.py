"""A grid anchor is a phase, not a place.

A cue below zero means "the very start", and flooring it to 0.0 is right. A
grid anchor below zero is where VirtualDJ's analysis put the first downbeat,
and flooring it moves *every* beat line in the track. The 2026-10-31 sync did
exactly that to two tracks of "2025.2 Indigo Blue Diggs":

    Tim aka New T. - SWEET MISERY   VirtualDJ -1.587823 s @ 94 BPM
                                    -> Traktor 51.536 ms, ~half a beat off
    Candido - Jingoism (...Remix)   VirtualDJ -0.004875 s @ 126 BPM
                                    -> Traktor 51.536 ms where 46.661 ms was right

and both then read as "needs sync" forever, since a re-sync floored them again.

The cue conversion itself is untouched -- ``test_adapter_timeline.py`` still pins
it to the verified pairwise functions, floor and all.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.adapters.traktor import TraktorAdapter                # noqa: E402
from trackcleaner.adapters.vdj import VdjAdapter                        # noqa: E402
from trackcleaner.config.paths import Paths                             # noqa: E402
from trackcleaner.model.cues import (CORRECTION_OFFSET_SEC, Poi,        # noqa: E402
                                     bar_seconds, format_cue_v2,
                                     grid_anchor_at_or_after_zero,
                                     pois_from_cue_v2)
from trackcleaner.model.library import TRAKTOR, VDJ, Track              # noqa: E402
from trackcleaner.model.status import cues_equivalent                   # noqa: E402
from trackcleaner.sync.engine import _retimed_pois                      # noqa: E402

CLASS_A = r"D:\Music\class_a.mp3"
CLASS_B = r"D:\Music\class_b.mp3"


class _StartTimes:
    def get(self, path):
        low = (path or "").lower()
        return 0.025057 if "class_a" in low else (0.0 if "class_b" in low else None)


@pytest.fixture
def adapters():
    paths = Paths(nml="x.nml", xml="y.xml", csv="")
    traktor, vdj = TraktorAdapter(paths), VdjAdapter(paths)
    traktor.start_times = vdj.start_times = _StartTimes()
    return traktor, vdj


def _track(source, pois, bpm, path=CLASS_A):
    return Track(key=path.lower(), path=path, source=source, pois=pois, bpm=bpm)


def _grid(track):
    return [p.pos for p in track.pois if p.kind == "beatgrid"]


SWEET_MISERY_VDJ = -1.587823
JINGOISM_VDJ = -0.004875


# ============================ writing a grid ================================
def test_a_negative_anchor_moves_forward_by_whole_bars():
    bar = bar_seconds(94.0)

    assert bar == pytest.approx(4 * 60 / 94.0)
    assert grid_anchor_at_or_after_zero(SWEET_MISERY_VDJ, 94.0) == \
        pytest.approx(SWEET_MISERY_VDJ + bar)
    assert grid_anchor_at_or_after_zero(-2.5 * bar, 94.0) == pytest.approx(0.5 * bar)


def test_an_anchor_that_is_not_negative_is_left_alone():
    assert grid_anchor_at_or_after_zero(0.0, 94.0) == 0.0
    assert grid_anchor_at_or_after_zero(0.046661, 126.0) == 0.046661


def test_with_no_tempo_there_is_nothing_to_step_by():
    """Falls back to the floor, which is all the old code ever did."""
    for bpm in (None, 0, "", "nonsense"):
        assert grid_anchor_at_or_after_zero(-1.0, bpm) == 0.0


def test_sweet_misery_lands_one_bar_later_in_traktor(adapters):
    traktor, vdj = adapters
    src = _track(VDJ, [Poi(SWEET_MISERY_VDJ, None, "beatgrid")], 94.0)

    (written,) = _grid(Track(**{**src.__dict__,
                                "pois": _retimed_pois(src, vdj, traktor)}))

    assert written == pytest.approx(SWEET_MISERY_VDJ + CORRECTION_OFFSET_SEC
                                    + bar_seconds(94.0))
    assert written != pytest.approx(CORRECTION_OFFSET_SEC), "the old floored value"


def test_jingoism_is_not_floored_halfway(adapters):
    """-4.9 ms + 51.5 ms is positive: nothing needed flooring, only the
    halfway floor at canonical time made it 51.5 ms."""
    traktor, vdj = adapters
    src = _track(VDJ, [Poi(JINGOISM_VDJ, None, "beatgrid"),
                       Poi(0.0, 5, "cue")], 126.0)

    out = _retimed_pois(src, vdj, traktor)

    assert [p.pos for p in out if p.kind == "beatgrid"] == \
        [pytest.approx(JINGOISM_VDJ + CORRECTION_OFFSET_SEC)]
    assert [p.pos for p in out if p.kind == "cue"] == \
        [pytest.approx(CORRECTION_OFFSET_SEC)], "cues are converted as before"


def test_traktor_to_vdj_never_writes_a_negative_anchor(adapters):
    """A Class-A Traktor anchor under 51.5 ms is negative on VirtualDJ's
    timeline; it is stepped forward rather than floored there too."""
    traktor, vdj = adapters
    src = _track(TRAKTOR, [Poi(0.020, None, "beatgrid")], 120.0)

    (written,) = [p.pos for p in _retimed_pois(src, traktor, vdj)]

    assert written == pytest.approx(0.020 - CORRECTION_OFFSET_SEC + 2.0)


def test_a_source_with_no_tempo_borrows_the_targets(adapters):
    """5 of the 26 grids the 31 Oct sync floored had a BPM only in Traktor."""
    traktor, vdj = adapters
    src = _track(VDJ, [Poi(SWEET_MISERY_VDJ, None, "beatgrid")], None)

    (written,) = [p.pos for p in _retimed_pois(src, vdj, traktor, 93.999779)]

    assert written == pytest.approx(SWEET_MISERY_VDJ + CORRECTION_OFFSET_SEC
                                    + bar_seconds(93.999779))


def test_a_class_b_grid_converts_one_to_one(adapters):
    traktor, vdj = adapters
    src = _track(VDJ, [Poi(1.25, None, "beatgrid")], 120.0, path=CLASS_B)

    assert [p.pos for p in _retimed_pois(src, vdj, traktor)] == [1.25]


# ============================ comparing grids ===============================
def _pair(vdj_grid, traktor_grid, vdj_bpm=94.0, traktor_bpm=93.999779):
    return (_track(VDJ, [Poi(vdj_grid, None, "beatgrid")], vdj_bpm),
            _track(TRAKTOR, [Poi(traktor_grid, None, "beatgrid")], traktor_bpm))


def test_anchors_whole_bars_apart_are_one_grid():
    written = SWEET_MISERY_VDJ + CORRECTION_OFFSET_SEC + bar_seconds(94.0)

    assert cues_equivalent(*_pair(SWEET_MISERY_VDJ, written))
    assert cues_equivalent(*reversed(_pair(SWEET_MISERY_VDJ, written)))


def test_the_floored_grid_still_reads_as_different():
    """What the 31 Oct sync left in Traktor must stay flagged, or nothing would
    prompt the re-sync that repairs it."""
    assert not cues_equivalent(*_pair(SWEET_MISERY_VDJ, CORRECTION_OFFSET_SEC))
    assert not cues_equivalent(*_pair(JINGOISM_VDJ, CORRECTION_OFFSET_SEC, 126.0,
                                      126.000114))


def test_an_unfloored_class_a_anchor_matches():
    assert cues_equivalent(*_pair(JINGOISM_VDJ, JINGOISM_VDJ + CORRECTION_OFFSET_SEC,
                                  126.0, 126.000114))


def test_a_fraction_of_a_bar_is_a_different_grid():
    bar = bar_seconds(94.0)
    assert not cues_equivalent(*_pair(1.0, 1.0 + bar / 2))
    assert not cues_equivalent(*_pair(1.0, 1.0 + bar / 4)), "one beat is not a bar"


def test_a_bar_in_either_programs_tempo_counts():
    """Tempo was never compared, and the two programs often disagree on it --
    "Freaky Dancin" is 120.0 in VirtualDJ and 120.49 in Traktor. The writer
    steps by the source's bar, so that is the one the comparison must accept."""
    vdj_bar = bar_seconds(120.0)

    assert cues_equivalent(*_pair(-1.249252, -1.249252 + CORRECTION_OFFSET_SEC + vdj_bar,
                                  120.0, 120.48761))
    assert cues_equivalent(*reversed(_pair(-1.249252, -1.249252 + CORRECTION_OFFSET_SEC
                                           + vdj_bar, 120.0, 120.48761)))


def test_a_bar_in_neither_tempo_is_a_different_grid():
    assert not cues_equivalent(*_pair(1.0, 1.0 + bar_seconds(110.0), 94.0, 96.0))


def test_a_multi_marker_grid_compares_exactly():
    bar = bar_seconds(120.0)
    a = _track(VDJ, [Poi(1.0, None, "beatgrid"), Poi(60.0, None, "beatgrid")], 120.0)
    b = _track(TRAKTOR, [Poi(1.0 + bar, None, "beatgrid"),
                         Poi(60.0 + bar, None, "beatgrid")], 120.0)
    assert not cues_equivalent(a, b)


def test_cues_get_no_bar_tolerance():
    bar = bar_seconds(120.0)
    a = _track(VDJ, [Poi(1.0, 1, "cue")], 120.0)
    b = _track(TRAKTOR, [Poi(1.0 + bar, 1, "cue")], 120.0)
    assert not cues_equivalent(a, b)


# ======================== Traktor's marker format ===========================
def test_a_grid_marker_carries_its_tempo():
    xml = format_cue_v2(Poi(1.0, None, "beatgrid"), 93.999779)

    assert '<GRID BPM="93.999779"></GRID></CUE_V2>' in xml


def test_with_no_tempo_the_marker_has_no_grid_child():
    assert "<GRID" not in format_cue_v2(Poi(1.0, None, "beatgrid"))
    assert "<GRID" not in format_cue_v2(Poi(1.0, 2, "cue"), 120.0)


def test_the_grid_child_reads_back_as_one_grid_and_no_cue():
    xml = format_cue_v2(Poi(1.0, None, "beatgrid"), 120.0)
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', xml.split("<GRID")[0]))

    assert pois_from_cue_v2([attrs]) == [Poi(1.0, None, "beatgrid")]


def test_an_existing_entry_takes_the_tempo_from_its_own_TEMPO():
    """So the marker agrees with the BPM Traktor shows, not with the source's
    rounding of it."""
    from trackcleaner.io import traktor_write
    body = ('<INFO></INFO>\n<TEMPO BPM="93.999779" BPM_QUALITY="100.000000"></TEMPO>\n')
    track = _track(VDJ, [Poi(1.0, None, "beatgrid")], 94.0)

    entry = traktor_write._entry_with_cues("<ENTRY>", body, "</ENTRY>", track)

    assert '<GRID BPM="93.999779">' in entry


# ====================== what_differs, for the sync pane =====================
def test_what_differs_names_the_fields_and_cues():
    from trackcleaner.model.status import what_differs
    a = Track(key="k", path=CLASS_A, source=VDJ, genre="Funk", title="X",
              pois=[Poi(1.0, 1, "cue")], bpm=120.0)
    b = Track(key="k", path=CLASS_A, source=TRAKTOR, genre="", title="x",
              pois=[Poi(2.0, 1, "cue")], bpm=120.0)

    assert what_differs(a, [b]) == ["cues", "genre"]


def test_what_differs_agrees_with_cues_equivalent():
    """A class-A shift and a grid a bar along are the same, as in the status."""
    from trackcleaner.model.status import what_differs
    a = Track(key="k", path=CLASS_A, source=VDJ, bpm=94.0,
              pois=[Poi(SWEET_MISERY_VDJ, None, "beatgrid"), Poi(1.0, 1, "cue")])
    b = Track(key="k", path=CLASS_A, source=TRAKTOR, bpm=94.0,
              pois=[Poi(SWEET_MISERY_VDJ + CORRECTION_OFFSET_SEC + bar_seconds(94.0),
                        None, "beatgrid"),
                    Poi(1.0 + CORRECTION_OFFSET_SEC, 1, "cue")])

    assert cues_equivalent(a, b)
    assert what_differs(a, [b]) == []
    assert what_differs(a, [None]) == []
