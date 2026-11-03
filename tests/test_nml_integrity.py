"""The collection file must survive every write, and never claim more than it has.

All of this came from logs taken on a real 133 MB Traktor collection, because the
fixtures could not express it. Two of the three defects here were **invisible to
the entire suite** until `tests/fixtures.py` gained a single realistic element.

The rules:

* a write that would store malformed XML is **refused**, not persisted;
* a `<CUE_V2>` is stripped **whole**, children and all;
* a blank source field means *no information*, never "make it empty".
"""

import os
import re
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.io import traktor_read, traktor_write                 # noqa: E402
from trackcleaner.io.write_queue import (MalformedWrite,                # noqa: E402
                                         atomic_write_text,
                                         atomic_write_xml)
from trackcleaner.model.cues import CUE_V2_RE, is_owned_cue_v2          # noqa: E402
from trackcleaner.model.library import Track                            # noqa: E402
from fixtures import SAMPLE_NML                                         # noqa: E402

#: The shape that caused it: Traktor writes a grid marker with a nested child,
#: split across two lines.
GRID_CUE = ('<CUE_V2 NAME="Beat Marker" DISPL_ORDER="0" TYPE="4" '
            'START="19738.899231" LEN="0.000000" REPEATS="-1" HOTCUE="-1">'
            '<GRID BPM="110.000000"></GRID>\n</CUE_V2>')


def _nml(tmp_path, text=None):
    path = tmp_path / "collection.nml"
    path.write_text(text if text is not None else SAMPLE_NML, encoding="utf-8")
    return path


# ===================== the fixture now has the real shape ===================
def test_the_fixture_contains_a_nested_grid_cue():
    """Guarding the guard. The fixture had **zero** `<GRID>` elements, which is
    the only reason a corruption bug that hits every real collection could pass
    a full suite. Take this element out and ten tests stop being meaningful."""
    assert "<GRID " in SAMPLE_NML
    assert re.search(r"<CUE_V2\b[^>]*>\s*<GRID\b", SAMPLE_NML)


# ========================= stripping a whole element ========================
def test_a_cue_with_a_child_is_matched_whole():
    body = f'<MUSICAL_KEY VALUE="1"></MUSICAL_KEY>\n{GRID_CUE}\n<LOUDNESS></LOUDNESS>'

    stripped = CUE_V2_RE.sub(
        lambda m: "" if is_owned_cue_v2(m.group(0)) else m.group(0), body)

    assert "<GRID" not in stripped, "the child was left behind"
    assert "</CUE_V2>" not in stripped, "an orphan closing tag was left behind"
    assert "MUSICAL_KEY" in stripped and "LOUDNESS" in stripped


def test_the_old_pattern_would_have_orphaned_it():
    """Stated so the fix cannot be quietly reverted to something simpler."""
    old = re.compile(r"<CUE_V2\b[^>]*/?>(?:</CUE_V2>)?")

    stripped = old.sub("", GRID_CUE)

    assert "<GRID" in stripped and "</CUE_V2>" in stripped


def test_a_self_closing_cue_still_matches():
    assert CUE_V2_RE.findall('<CUE_V2 TYPE="0" START="1.0" />')


def test_several_cues_do_not_swallow_each_other():
    """A greedy match across the block would eat everything between the first
    `<CUE_V2>` and the last `</CUE_V2>`, including the elements in between."""
    body = (f'{GRID_CUE}\n'
            '<CUE_V2 NAME="n.n." TYPE="0" START="1.0" HOTCUE="1"></CUE_V2>\n'
            '<CUE_V2 NAME="n.n." TYPE="0" START="2.0" HOTCUE="2"></CUE_V2>')

    assert len(CUE_V2_RE.findall(body)) == 3


def test_syncing_a_track_leaves_the_nml_parseable(tmp_path):
    """End to end, the reported corruption: rewriting an entry that has a
    nested-GRID marker used to produce a file Traktor refused to open."""
    lib = traktor_read.read(str(_nml(tmp_path)))
    key = list(lib.tracks)[0]
    edited = Track(**{**lib.tracks[key].__dict__})

    out, _created = traktor_write.sync_track_into_nml(SAMPLE_NML, edited)

    ET.fromstring(out)                       # raises if malformed
    assert "</CUE_V2>\n</CUE_V2>" not in out


def test_traktor_only_markers_survive_a_cue_write(tmp_path):
    """The binding rule: loop, fade and load markers are not ours."""
    nml = SAMPLE_NML.replace("</ENTRY>",
                             '<CUE_V2 NAME="MyLoop" TYPE="5" START="5.0" '
                             'LEN="4.0" HOTCUE="6"></CUE_V2></ENTRY>', 1)
    lib = traktor_read.read(str(_nml(tmp_path, nml)))
    key = list(lib.tracks)[0]

    out, _ = traktor_write.sync_track_into_nml(nml, lib.tracks[key])

    assert 'NAME="MyLoop"' in out
    ET.fromstring(out)


# ==================== refusing to store malformed XML =======================
def test_a_malformed_write_is_refused(tmp_path):
    path = tmp_path / "c.nml"
    path.write_text("<NML></NML>", encoding="utf-8")

    with pytest.raises(MalformedWrite):
        atomic_write_xml(str(path), "<NML><ENTRY></CUE_V2></NML>")


def test_the_original_file_is_untouched_by_a_refusal(tmp_path):
    """The whole point: the damage never reaches the user's library."""
    path = tmp_path / "c.nml"
    path.write_text(SAMPLE_NML, encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(MalformedWrite):
        atomic_write_xml(str(path), "<NML><ENTRY></CUE_V2></NML>")

    assert path.read_bytes() == before


def test_no_temp_file_is_left_behind(tmp_path):
    path = tmp_path / "c.nml"
    path.write_text(SAMPLE_NML, encoding="utf-8")

    with pytest.raises(MalformedWrite):
        atomic_write_xml(str(path), "<broken>")

    assert not (tmp_path / "c.nml.tmp").exists()


def test_well_formed_content_writes_normally(tmp_path):
    path = tmp_path / "c.nml"

    atomic_write_xml(str(path), SAMPLE_NML)

    assert traktor_read.read(str(path)).tracks


def test_the_plain_writer_still_takes_anything(tmp_path):
    """`.vdjfolder` and friends are not this XML, and must not be gated."""
    path = tmp_path / "plain.txt"

    atomic_write_text(str(path), "not xml at all <<<")

    assert path.read_text(encoding="utf-8") == "not xml at all <<<"


def test_the_collection_writers_go_through_the_guard():
    """Asserted on the source, because a writer that quietly used the unguarded
    call would pass every behavioural test right up until it corrupted a real
    library -- which is exactly what happened."""
    import inspect
    from trackcleaner.adapters import traktor as t_adapter
    from trackcleaner.adapters import vdj as v_adapter

    for module, opener in ((t_adapter, "nml_path"), (v_adapter, "path")):
        src = inspect.getsource(module)
        for line in src.splitlines():
            if "atomic_write_text(" in line and opener in line:
                pytest.fail(f"{module.__name__} writes its collection unguarded:"
                            f"\n{line.strip()}")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
