r"""A filename that really contains ``&amp;`` must key the same everywhere.

Web-escaped names get baked into downloads -- 440 files in the reported library
are literally called ``Justice - Safe &amp; Sound (Archi Intro Edit).mp3``, six
characters of entity text and not an ampersand. Both databases store that
correctly as ``&amp;amp;``, so nothing on disk is wrong; what breaks is the app
deciding how many times to decode it.

The rule these tests pin: **decoding happens once, where raw XML text is
captured.** ``matching.path_key`` does not decode, because it is handed values an
XML parser has already decoded. A second decode is invisible on ordinary text and
turns ``&amp;`` into ``&`` -- a file that does not exist -- on these.

Two failures follow from getting the count wrong, and both are here:

* ``Track.path`` stops naming a real file, so the track reads as missing and
  will not play.
* the reader and the NML writer derive different keys from the same entry, so
  ``sync_tracks_into_nml`` cannot find an entry it already holds, reports
  ``created=True`` and appends a duplicate.

``&quot;``/``&lt;``/``&gt;`` are covered too even though Windows forbids the
decoded character in a filename: the entity text itself is legal in a name, so
the app still has to key it consistently.
"""

import os
import re
import sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.io import traktor_read, traktor_write, vdj_read, vdj_write  # noqa: E402
from trackcleaner.model import matching                                       # noqa: E402
from trackcleaner.model.library import Track, TRAKTOR, VDJ                    # noqa: E402

#: Entity text that can legally sit in a Windows filename. The decoded form of
#: the last three cannot, which is a rename problem and not a keying one.
ENTITIES = ["&amp;", "&apos;", "&quot;", "&lt;", "&gt;", "&#39;"]

DIR = r"D:\Music\Beatjunkies"
PLAYLIST_UUID = "f8df219fc1ea42a7834f2fe64556a58d"


def _named(entity: str) -> str:
    """A real-world shaped path whose *filename* holds ``entity`` verbatim."""
    return rf"{DIR}\Justice - Safe {entity} Sound (Archi Edit).mp3"


# ======================= the two sides must agree ==========================
@pytest.mark.parametrize("entity", ENTITIES)
def test_the_nml_reader_and_the_nml_writer_derive_one_key(entity):
    """The defect in its purest form.

    The reader gets ``<LOCATION>`` through ElementTree, which decodes; the writer
    reads the same attributes as raw text. Whichever way the decode count is
    arranged, the two have to land on the same string or no write can find its
    entry.
    """
    path = _named(entity)
    volume, dir_attr, file_name = matching.traktor_location_parts(path)
    location = (f"<LOCATION DIR={quoteattr(dir_attr)} FILE={quoteattr(file_name)} "
                f"VOLUME={quoteattr(volume)}></LOCATION>")

    el = ET.fromstring(location)
    reader_key = matching.path_key(matching.build_windows_path(
        el.get("VOLUME"), el.get("DIR"), el.get("FILE")))
    writer_key = traktor_write._entry_key(location)

    assert reader_key == writer_key
    assert reader_key == path.lower(), "and the key is the real filename"


@pytest.mark.parametrize("entity", ENTITIES)
def test_a_playlist_primarykey_keys_the_same_read_or_written(entity, tmp_path):
    """``traktor_write.playlist_entry_keys`` reads the raw attribute;
    ``traktor_read`` reads the same one through ElementTree. Membership is
    compared across the two, so they have to answer identically.

    Driven through both real functions rather than through ``matching``: the
    decode has to sit at the call site that owns the raw text, and asserting on
    the helper alone would pass with it missing there.
    """
    path = _named(entity)
    nml = _nml_with(path, playlist=True)
    written = tmp_path / "collection.nml"
    written.write_text(nml, encoding="utf-8")

    from_reader = next(traktor_read.read(str(written)).playlists()).playlist.track_keys
    from_writer = traktor_write.playlist_entry_keys(nml, PLAYLIST_UUID)

    assert from_reader == from_writer == [path.lower()]


@pytest.mark.parametrize("entity", ENTITIES)
def test_every_reader_agrees_with_every_other(entity):
    """Traktor entry, VirtualDJ song and a ``.vdjfolder`` row for one file all
    have to produce one key, or a track resolves in one pane and not another."""
    path = _named(entity)
    nml = _nml_with(path)
    xml = _xml_with(path)
    folder = vdj_write.render_vdjfolder([_track(path, VDJ)])

    trk = traktor_read.read(_written(nml, "collection.nml"))
    vdj_song = vdj_read._track_from_song(
        re.search(r"<Song\b.*?</Song>", xml, re.DOTALL).group(0))
    row = re.search(r"<song\b([^>]*)/?>", folder).group(1)
    folder_key = matching.path_key(
        matching.decode_entities(re.search(r'path="([^"]*)"', row).group(1)))

    keys = {next(iter(trk.tracks)), vdj_song.key, folder_key}
    assert keys == {path.lower()}, keys


# ===================== the user-visible consequences =======================
@pytest.mark.parametrize("entity", ENTITIES)
def test_the_parsed_path_still_names_the_real_file(entity):
    """A second decode points ``Track.path`` at a file that is not there, so the
    integrity scan marks it missing and playback fails. 198 of 200 sampled songs
    in the reported library were in that state."""
    path = _named(entity)
    song = re.search(r"<Song\b.*?</Song>", _xml_with(path), re.DOTALL).group(0)

    assert vdj_read._track_from_song(song).path == path


def test_the_writer_finds_an_entry_it_already_holds(tmp_path):
    """The duplicate-entry defect: with the keys disagreeing, a sync reports the
    track as newly created and appends a second ``<ENTRY>`` for a file the
    collection has had all along."""
    path = _named("&amp;")
    nml = _nml_with(path)

    out, created = traktor_write.sync_tracks_into_nml(nml, [_track(path, VDJ)])

    assert created[matching.path_key(path)] is False
    assert out.count("<ENTRY ") == nml.count("<ENTRY ")


def test_the_vdj_writer_finds_its_song():
    """Same property on the other side, where the decode is explicit."""
    path = _named("&amp;")
    xml = _xml_with(path)

    out, created = vdj_write.sync_tracks_into_xml(xml, [_track(path, TRAKTOR)])

    assert created[matching.path_key(path)] is False
    assert out.count("<Song ") == xml.count("<Song ")


# ============================ round trips =================================
@pytest.mark.parametrize("entity", ENTITIES)
def test_a_vdjfolder_round_trips_the_name(entity, tmp_path):
    """Rendered and read back through the real writer and reader."""
    path = _named(entity)
    f = tmp_path / "Temp.vdjfolder"
    f.write_text(vdj_write.render_vdjfolder([_track(path, VDJ)]), encoding="utf-8")

    tracks: dict = {}
    keys, _ = vdj_read._read_vdjfolder(str(f), tracks)

    assert keys == [path.lower()]
    assert tracks[keys[0]].path == path


@pytest.mark.parametrize("entity", ENTITIES)
def test_a_new_nml_entry_round_trips_the_name(entity, tmp_path):
    """``_render_new_entry`` escapes once; the reader decodes once."""
    path = _named(entity)
    nml = _nml_with(path, entries=[])
    out, _ = traktor_write.sync_tracks_into_nml(nml, [_track(path, VDJ)])

    written = tmp_path / "collection.nml"
    written.write_text(out, encoding="utf-8")
    lib = traktor_read.read(str(written))

    assert [t.path for t in lib.tracks.values()] == [path]


@pytest.mark.parametrize("stored,decoded", [
    ("Walkin&#39;.mp3", "Walkin'.mp3"),          # decimal character reference
    ("Walkin&#x27;.mp3", "Walkin'.mp3"),         # hex
    ("Rock &#38; Roll.mp3", "Rock & Roll.mp3"),
    ("A &lt;B&gt; &quot;C&quot;.mp3", 'A <B> "C".mp3'),
    ("&amp;#39; is text.mp3", "&#39; is text.mp3"),   # one pass, not two
])
def test_decode_entities_answers_exactly_what_the_parser_answers(stored, decoded):
    """The raw-text call sites decode with this; the readers decode with
    ElementTree. Any form the two disagree on is a key the writer cannot match.

    The last case is why it is a single pass: ``&amp;#39;`` *means* the literal
    text ``&#39;``. Scanning the output again would take it all the way to an
    apostrophe.
    """
    through_parser = ET.fromstring(f'<a v="{stored}"/>').get("v")

    assert matching.decode_entities(stored) == through_parser == decoded


def test_an_ordinary_ampersand_is_unaffected(tmp_path):
    """The behaviour that must not break: a plain ``&`` in a name is stored as
    ``&amp;`` and still reads back as ``&``."""
    path = rf"{DIR}\Bacao Rhythm & Steel Band - Jungle Fever.mp3"
    out, _ = traktor_write.sync_tracks_into_nml(_nml_with(path, entries=[]),
                                               [_track(path, VDJ)])
    written = tmp_path / "collection.nml"
    written.write_text(out, encoding="utf-8")

    lib = traktor_read.read(str(written))
    assert [t.path for t in lib.tracks.values()] == [path]
    assert "&amp;amp;" not in out, "one ampersand, escaped once"


# ================================ helpers =================================
def _track(path: str, source: str) -> Track:
    return Track(key=matching.path_key(path), path=path, source=source,
                 title="Safe Sound", artist="Justice", pois=[])


def _nml_with(path: str, entries=None, playlist: bool = False) -> str:
    """A minimal collection holding ``path`` as its one entry (or none), and
    optionally a one-track playlist referencing it."""
    volume, dir_attr, file_name = matching.traktor_location_parts(path)
    entry = (f'<ENTRY TITLE="Safe Sound" ARTIST="Justice">'
             f'<LOCATION DIR={quoteattr(dir_attr)} FILE={quoteattr(file_name)} '
             f'VOLUME={quoteattr(volume)} VOLUMEID=""></LOCATION>'
             f'<INFO BITRATE="320000" PLAYTIME="200"></INFO>'
             f'</ENTRY>\n')
    body = "" if entries == [] else entry
    if playlist:
        stored = matching.windows_path_to_traktor_key(path)
        nodes = (f'<NODE TYPE="PLAYLIST" NAME="Temp"><PLAYLIST ENTRIES="1" '
                 f'TYPE="LIST" UUID="{PLAYLIST_UUID}">'
                 f'<ENTRY><PRIMARYKEY TYPE="TRACK" KEY={quoteattr(stored)}>'
                 f'</PRIMARYKEY></ENTRY></PLAYLIST></NODE>')
        subnodes = f'<SUBNODES COUNT="1">{nodes}</SUBNODES>'
    else:
        subnodes = '<SUBNODES COUNT="0"></SUBNODES>'
    return ('<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n'
            '<NML VERSION="19">\n'
            f'<COLLECTION ENTRIES="{0 if entries == [] else 1}">{body}</COLLECTION>\n'
            f'<PLAYLISTS><NODE TYPE="FOLDER" NAME="$ROOT">{subnodes}'
            '</NODE></PLAYLISTS>\n</NML>\n')


def _xml_with(path: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<VirtualDJ_Database Version="2023">\n'
            f'<Song FilePath={vdj_write._qattr(path)} FileSize="1">\n'
            f' <Tags Author="Justice" Title="Safe Sound" />\n'
            f' <Infos SongLength="200.0" />\n'
            f'</Song>\n</VirtualDJ_Database>\n')


def _written(content: str, name: str) -> str:
    """Materialise ``content`` for a reader that only takes a path."""
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
