"""A comment with a line break in it has to be able to reach "in sync".

Traktor and VirtualDJ spell a line break differently and neither is wrong.
Traktor keeps the comment in an *attribute* and writes the break as the
character references ``&#xD;&#xA;``, which are protected from XML's
attribute-value normalisation and parse back as ``\\r\\n``. VirtualDJ keeps it
in a ``<Comment>`` *element* as a real CRLF, and XML normalises line ends in
element text, so it parses back as ``\\n``.

Nothing folded those together, so:

1. ``metadata_matches`` could never say yes. The track stayed yellow, the
   playlist stayed yellow, and Overwrite rewrote an identical value on every
   run -- 92 of 92 comment "differences" in one reported playlist were this.

2. The ``\\r`` was written into ``database.xml`` raw, and ``atomic_write_text``
   uses a text-mode handle that turns the following ``\\n`` into ``\\r\\n``. So
   ``\\r\\n`` landed as ``\\r\\r\\n``; the next whole-file rewrite read that back
   as two newlines and stored ``\\r\\n\\r\\n``. **The comment gained a blank line
   every second sync** -- 511 of them in the reported library.

The tests here are the two properties that were missing: the comparison
converges, and the bytes are stable from the first write.
"""

import os
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from trackcleaner.io import vdj_read, vdj_write, traktor_read           # noqa: E402
from trackcleaner.io.write_queue import atomic_write_text               # noqa: E402
from trackcleaner.model import matching, status                         # noqa: E402
from trackcleaner.model.library import TRAKTOR, VDJ, Track              # noqa: E402

#: Exactly the shape found in the user's collection.nml: the break is written
#: as character references, so it survives attribute normalisation as CRLF.
NML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<NML VERSION="19"><COLLECTION ENTRIES="1">\n'
    '<ENTRY TITLE="Together" ARTIST="Ray Barretto">'
    '<LOCATION DIR="/:m/:" FILE="a.flac" VOLUME="D:" VOLUMEID="x"></LOCATION>'
    '<INFO GENRE="Latin" COMMENT="CD1 - El Barrio.&#xD;&#xA;Box set of four."'
    '></INFO>'
    '</ENTRY>\n'
    '</COLLECTION></NML>\n'
)

#: And the shape found in database.xml: a real CRLF inside the element.
XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<VirtualDJ_Database Version="2025">\n'
    ' <Song FilePath="D:\\m\\a.flac" FileSize="1">\n'
    '  <Tags Author="Ray Barretto" Title="Together" Genre="Latin" />\n'
    '  <Comment>CD1 - El Barrio.\r\nBox set of four.</Comment>\n'
    ' </Song>\n'
    '</VirtualDJ_Database>\n'
)

KEY = "d:\\m\\a.flac"


def _write(path, text):
    """Bytes exactly as given -- these fixtures carry meaningful CRs."""
    with open(path, "wb") as f:
        f.write(text.encode("utf-8"))


# ===================== the two sides agree on one string ====================
def test_the_readers_agree_on_a_multi_line_comment(tmp_path):
    """The whole bug in one assertion: the same comment, read from each
    program's own file, has to come back as the same Python string."""
    nml, xml = tmp_path / "collection.nml", tmp_path / "database.xml"
    _write(nml, NML)
    _write(xml, XML)

    t = list(traktor_read.read(str(nml)).tracks.values())[0]
    v = list(vdj_read._read_database(str(xml)).values())[0]

    assert t.comment == v.comment == "CD1 - El Barrio.\nBox set of four."
    assert "\r" not in t.comment, "CRLF folds to LF on the way in"


def test_a_crlf_comment_reads_as_in_sync():
    """What the user saw: yellow fork on one side, yellow triangle on the other,
    on a track whose comment was already identical."""
    a = Track(key=KEY, path=KEY, source=TRAKTOR, comment="A.\r\nB")
    b = Track(key=KEY, path=KEY, source=VDJ, comment="A.\nB")

    assert status.metadata_matches(a, b)
    assert status.content_matches(a, b)


def test_a_real_difference_is_still_a_difference():
    """The fold must not paper over an actual edit -- including the blank line
    the old writer inserted, which is exactly what the next Overwrite repairs."""
    a = Track(key=KEY, path=KEY, source=TRAKTOR, comment="A.\nB")
    grown = Track(key=KEY, path=KEY, source=VDJ, comment="A.\n\nB")
    other = Track(key=KEY, path=KEY, source=VDJ, comment="A.\nC")

    assert not status.metadata_matches(a, grown)
    assert not status.metadata_matches(a, other)


def test_normalize_text_handles_every_flavour():
    assert matching.normalize_text("a\r\nb") == "a\nb"
    assert matching.normalize_text("a\rb") == "a\nb"      # classic Mac
    assert matching.normalize_text("a\nb") == "a\nb"
    assert matching.normalize_text(None) == ""


# ======================= and the bytes stop drifting ========================
def test_a_written_comment_is_byte_stable(tmp_path):
    """Write, read, write again. Before the fix generation 1 stored ``\\r\\r\\n``
    and generation 2 stored ``\\r\\n\\r\\n`` -- one blank line gained per pair of
    syncs, permanently, in the user's own library."""
    path = tmp_path / "database.xml"
    _write(path, XML)
    value = "CD1 - El Barrio.\r\nBox set of four."      # as Traktor hands it over

    seen = []
    for _ in range(3):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        content, changed = vdj_write.set_songs_metadata(
            content, {KEY: {"comment": value}})
        atomic_write_text(str(path), content)
        seen.append(path.read_bytes())

    assert seen[0] == seen[1] == seen[2], "the file must stop changing"
    assert bytes([13, 13, 10]) not in seen[0], "no bare CR written"
    assert bytes([13, 10, 13, 10]) not in seen[0], "no blank line gained"
    assert changed[KEY]


def test_a_written_comment_round_trips_through_the_reader(tmp_path):
    path = tmp_path / "database.xml"
    _write(path, XML)
    value = "one\ntwo\nthree"

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    content, _ = vdj_write.set_songs_metadata(content, {KEY: {"comment": value}})
    atomic_write_text(str(path), content)

    back = list(vdj_read._read_database(str(path)).values())[0]
    assert back.comment == value


@pytest.mark.parametrize("field,attr", [("title", "Title"), ("artist", "Author")])
def test_a_line_break_in_an_attribute_is_escaped(tmp_path, field, attr):
    """A literal newline in an XML attribute is legal but the parser normalises
    it to a space, so a wrapped title came back silently altered and could never
    match its source again. Traktor's writer already escapes; VirtualDJ's did
    not."""
    path = tmp_path / "database.xml"
    _write(path, XML)
    value = "Wrapped\ntitle"

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    content, _ = vdj_write.set_songs_metadata(content, {KEY: {field: value}})
    atomic_write_text(str(path), content)

    raw = path.read_text(encoding="utf-8")
    assert "&#10;" in raw, "escaped as a character reference, not left literal"
    tags = ET.fromstring(raw).find("Song").find("Tags")
    assert tags.get(attr) == value
    assert getattr(list(vdj_read._read_database(str(path)).values())[0],
                   field) == value


def test_a_new_entry_carries_a_multi_line_comment(tmp_path):
    """The create path renders its own block, so it needs the same guard."""
    track = Track(key=KEY, path="D:\\m\\a.flac", source=TRAKTOR,
                  title="T", artist="A", comment="one\ntwo")
    path = tmp_path / "database.xml"
    _write(path, XML.replace(
        ' <Song FilePath="D:\\m\\a.flac" FileSize="1">\n'
        '  <Tags Author="Ray Barretto" Title="Together" Genre="Latin" />\n'
        '  <Comment>CD1 - El Barrio.\r\nBox set of four.</Comment>\n'
        ' </Song>\n', ""))

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    content, added = vdj_write.sync_track_into_xml(content, track)
    atomic_write_text(str(path), content)

    assert added
    assert bytes([13, 13, 10]) not in path.read_bytes()
    assert list(vdj_read._read_database(str(path)).values())[0].comment == "one\ntwo"


# ========================= the Traktor side is safe =========================
def test_traktor_writes_a_line_break_as_character_references(tmp_path):
    """``quoteattr`` escapes it, so the value survives attribute normalisation
    and a backfilled comment reads back as what was written."""
    from trackcleaner.io import traktor_write

    content, changed = traktor_write.set_entries_metadata(
        NML, {KEY: {"comment": "one\ntwo"}})
    assert changed[KEY]
    assert "&#10;" in content, "a literal newline here would become a space"

    path = tmp_path / "collection.nml"
    atomic_write_text(str(path), content)
    back = list(traktor_read.read(str(path)).tracks.values())[0]
    assert back.comment == "one\ntwo"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ======================= the file's own line ending ========================
# Text is read with universal newlines, so it comes back holding ``\n`` only;
# writing it through a plain text handle made every one ``\r\n``. Traktor's
# collection.nml is LF, and one relinked attribute rewrote all 1.1 million of
# its line ends. A write now keeps whatever the file already uses.

@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
def test_a_rewrite_keeps_the_files_line_ending(tmp_path, ending):
    path = tmp_path / "collection.nml"
    path.write_bytes(b"<NML>" + ending + b"<A/>" + ending + b"</NML>" + ending)

    atomic_write_text(str(path), "<NML>\n<A/>\n<B/>\n</NML>\n")

    assert path.read_bytes() == ending.join(
        [b"<NML>", b"<A/>", b"<B/>", b"</NML>", b""])


def test_a_new_file_gets_the_platform_line_ending(tmp_path):
    """Unchanged from before: VirtualDJ's new ``.vdjfolder`` files stay CRLF on
    Windows, like the ones VirtualDJ writes itself."""
    path = tmp_path / "new.vdjfolder"

    atomic_write_text(str(path), "<VirtualFolder>\n</VirtualFolder>\n")

    assert path.read_bytes() == os.linesep.encode().join(
        [b"<VirtualFolder>", b"</VirtualFolder>", b""])
