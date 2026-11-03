"""Phase 3 collection-entry removal writers (pure, no Qt)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.io import traktor_write, vdj_write            # noqa: E402
from trackcleaner.model import matching                         # noqa: E402
from fixtures import SAMPLE_NML, DOO_RAG_SONG, DOO_RAG_PATH      # noqa: E402


# --------------------------- Traktor NML ---------------------------------
def _homenaje_key():
    return matching.path_key(r"D:\VirtualDJ\02 Homenaje.mp3")


def test_remove_entry_removes_only_the_matching_collection_entry():
    out, removed = traktor_write.remove_entry_from_nml(SAMPLE_NML, _homenaje_key())
    assert removed is True
    assert 'TITLE="Homenaje"' not in out               # the collection entry is gone
    assert 'TITLE="I Believe In Miracles"' in out       # the other one stays
    assert '<COLLECTION ENTRIES="1">' in out            # count decremented 2 -> 1
    # Playlist <ENTRY><PRIMARYKEY> rows are NOT collection entries (no <LOCATION>),
    # so both playlist references survive -- the UI clears those separately.
    assert out.count('KEY="D:/:VirtualDJ/:02 Homenaje.mp3"') == 2


def test_remove_entry_absent_key_is_a_noop():
    out, removed = traktor_write.remove_entry_from_nml(SAMPLE_NML, "d:/does/not/exist.mp3")
    assert removed is False and out == SAMPLE_NML


# --------------------------- VirtualDJ XML --------------------------------
def _vdj_db(*songs):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<VirtualDJ_Database Version="2023">\n'
            + "\n".join(songs) + "\n</VirtualDJ_Database>\n")


def test_remove_song_removes_only_the_matching_block():
    other = DOO_RAG_SONG.replace("09. Doo Rag.mp3", "99. Other.mp3").replace(
        'Title="Doo Rag"', 'Title="Other"')
    xml = _vdj_db(DOO_RAG_SONG, other)
    out, removed = vdj_write.remove_song_from_xml(xml, matching.path_key(DOO_RAG_PATH))
    assert removed is True
    assert 'Title="Doo Rag"' not in out
    assert 'Title="Other"' in out
    assert out.rstrip().endswith("</VirtualDJ_Database>")


def test_remove_song_absent_is_a_noop():
    xml = _vdj_db(DOO_RAG_SONG)
    out, removed = vdj_write.remove_song_from_xml(xml, "d:/does/not/exist.mp3")
    assert removed is False and out == xml


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
