"""Deterministic NML/XML fixtures for the test suite.

The project's ``collection.nml`` is the app's live working target and gets
rewritten by real runs, so tests must never depend on its contents. These
fixtures are a small, realistic, frozen stand-in.
"""

from __future__ import annotations

SAMPLE_NML = """<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<NML VERSION="19"><HEAD COMPANY="www.native-instruments.com" PROGRAM="Traktor"></HEAD>
<MUSICFOLDERS></MUSICFOLDERS>
<COLLECTION ENTRIES="2"><ENTRY MODIFIED_DATE="2025/11/27" MODIFIED_TIME="68950" TITLE="Homenaje" ARTIST="Brownout"><LOCATION DIR="/:VirtualDJ/:" FILE="02 Homenaje.mp3" VOLUME="D:" VOLUMEID="9896ba29"></LOCATION>
<ALBUM TITLE="Homenaje"></ALBUM>
<MODIFICATION_INFO AUTHOR_TYPE="user"></MODIFICATION_INFO>
<INFO BITRATE="256000" GENRE="Latin" KEY="1m" PLAYTIME="220"></INFO>
<TEMPO BPM="129.098221" BPM_QUALITY="100.000000"></TEMPO>
<MUSICAL_KEY VALUE="21"></MUSICAL_KEY>
<CUE_V2 NAME="Beat Marker" DISPL_ORDER="0" TYPE="4" START="15440.166875" LEN="0.000000" REPEATS="-1" HOTCUE="-1"><GRID BPM="129.098221"></GRID>
</CUE_V2>
<CUE_V2 NAME="n.n." DISPL_ORDER="0" TYPE="0" START="7897.762458" LEN="0.000000" REPEATS="-1" HOTCUE="4"></CUE_V2>
<CUE_V2 NAME="n.n." DISPL_ORDER="0" TYPE="0" START="67560.865172" LEN="0.000000" REPEATS="-1" HOTCUE="2"></CUE_V2>
</ENTRY>
<ENTRY MODIFIED_DATE="2025/11/27" MODIFIED_TIME="60780" TITLE="I Believe In Miracles" ARTIST="Jackson Sisters"><LOCATION DIR="/:VirtualDJ/:" FILE="03 - Jackson Sisters - I Believe In Miracles.mp3" VOLUME="D:" VOLUMEID="9896ba29"></LOCATION>
<MODIFICATION_INFO AUTHOR_TYPE="user"></MODIFICATION_INFO>
<INFO BITRATE="320000" GENRE="Funk" KEY="10m" PLAYTIME="290"></INFO>
<TEMPO BPM="107.698547" BPM_QUALITY="100.000000"></TEMPO>
<CUE_V2 NAME="Beat Marker" DISPL_ORDER="0" TYPE="4" START="697.783124" LEN="0.000000" REPEATS="-1" HOTCUE="-1"></CUE_V2>
<CUE_V2 NAME="n.n." DISPL_ORDER="0" TYPE="0" START="34102.745483" LEN="0.000000" REPEATS="-1" HOTCUE="3"></CUE_V2>
<CUE_V2 NAME="MyLoop" DISPL_ORDER="0" TYPE="5" START="50000.000000" LEN="4000.000000" REPEATS="-1" HOTCUE="6"></CUE_V2>
<CUE_V2 NAME="Load" DISPL_ORDER="0" TYPE="3" START="900.000000" LEN="0.000000" REPEATS="-1" HOTCUE="-1"></CUE_V2>
</ENTRY>
</COLLECTION>
<SETS ENTRIES="0"></SETS>
<PLAYLISTS><NODE TYPE="FOLDER" NAME="$ROOT"><SUBNODES COUNT="4"><NODE TYPE="FOLDER" NAME="breaks"><SUBNODES COUNT="1"><NODE TYPE="PLAYLIST" NAME="test"><PLAYLIST ENTRIES="1" TYPE="LIST" UUID="f8df219fc1ea42a7834f2fe64556a58d"><ENTRY><PRIMARYKEY TYPE="TRACK" KEY="D:/:VirtualDJ/:02 Homenaje.mp3"></PRIMARYKEY>
</ENTRY>
</PLAYLIST>
</NODE>
</SUBNODES>
</NODE>
<NODE TYPE="PLAYLIST" NAME="_LOOPS"><PLAYLIST ENTRIES="0" TYPE="LIST" UUID="389ba1f701cf4fa491b68b550da72852"></PLAYLIST>
</NODE>
<NODE TYPE="PLAYLIST" NAME="_RECORDINGS"><PLAYLIST ENTRIES="0" TYPE="LIST" UUID="6cfd0e097eb94483b821dc8f69ae0972"></PLAYLIST>
</NODE>
<NODE TYPE="PLAYLIST" NAME="Preparation"><PLAYLIST ENTRIES="1" TYPE="LIST" UUID="93bcbcd0049a40428d1a7348105df1b0"><ENTRY><PRIMARYKEY TYPE="TRACK" KEY="D:/:VirtualDJ/:02 Homenaje.mp3"></PRIMARYKEY>
</ENTRY>
</PLAYLIST>
</NODE>
</SUBNODES>
</NODE>
</PLAYLISTS>
<INDEXING></INDEXING>
</NML>
"""

EMPTY_VDJ_XML = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<VirtualDJ_Database Version="2023">\n</VirtualDJ_Database>\n')

# The reported case verbatim: a beatgrid plus three cues written WITHOUT a Pos
# attribute (VirtualDJ omits it at position 0), alongside an automix point that
# belongs to VirtualDJ alone.
DOO_RAG_SONG = (
    '<Song FilePath="D:\\Music\\Funk\\Galactic\\Galactic - Coolin&apos; Off\\'
    '09. Doo Rag.mp3" FileSize="15226124">\n'
    '  <Tags Author="Galactic" Title="Doo Rag" Genre="Jazz+Funk" '
    'Album="Coolin&apos; Off" TrackNumber="9" Bpm="0.631579" Flag="1" />\n'
    '  <Infos SongLength="375.986667" LastModified="1784675550" '
    'FirstSeen="1681543524" PlayCount="1" Bitrate="320" Cover="1" />\n'
    '  <Scan Version="801" Bpm="0.629841" AltBpm="0.475850" Volume="0.702552" '
    'Key="Dm" Flag="32768" />\n'
    '  <Poi Pos="1.242918" Type="beatgrid" />\n'
    '  <Poi Num="1" Type="cue" />\n'
    '  <Poi Num="2" Type="cue" />\n'
    '  <Poi Num="3" Type="cue" />\n'
    '  <Poi Pos="120.5" Point="intro" Type="automix" />\n'
    '</Song>'
)

DOO_RAG_PATH = r"D:\Music\Funk\Galactic\Galactic - Coolin' Off\09. Doo Rag.mp3"

# $ROOT children in SAMPLE_NML: breaks(folder), _LOOPS, _RECORDINGS, Preparation
ROOT_SUBNODES_COUNT = 4

# What Rekordbox exports for an empty library. Written by write_sample so every
# window test sees Rekordbox present but holding nothing, which keeps the status
# of the existing fixture pair exactly as it was.
EMPTY_REKORDBOX_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<DJ_PLAYLISTS Version="1.0.0">\n'
    '\t<PRODUCT Name="rekordbox" Version="6.8.5" Company="AlphaTheta"/>\n'
    '\t<COLLECTION Entries="0">\n'
    '\t</COLLECTION>\n'
    '\t<PLAYLISTS>\n'
    '\t\t<NODE Type="0" Name="ROOT" Count="0">\n'
    '\t\t</NODE>\n'
    '\t</PLAYLISTS>\n'
    '</DJ_PLAYLISTS>\n')

# The shapes a real export holds, each one a way a writer could go wrong:
# a track with a grid, two hot cues, a memory cue (Num=-1) and a loop (Type=4)
# that are Rekordbox's alone; a self-closing track with no children at all;
# an ampersand percent-encoded in a Location and entity-encoded in a Name; and
# a folder holding a TrackID playlist beside a Location-keyed one.
SAMPLE_REKORDBOX_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<DJ_PLAYLISTS Version="1.0.0">\n'
    '\t<PRODUCT Name="rekordbox" Version="6.8.5" Company="AlphaTheta"/>\n'
    '\t<COLLECTION Entries="2">\n'
    '\t\t<TRACK TrackID="7" Name="Safe &amp; Sound" Artist="Justice" '
    'Album="Audio, Video, Disco" Genre="Electro" Kind="MP3 File" Size="8123456" '
    'TotalTime="303" AverageBpm="122.00" DateAdded="2024-05-01" BitRate="320" '
    'Comments="peak time" PlayCount="4" '
    'Location="file://localhost/D:/Music/Justice/Justice%20-%20Safe%20%26%20Sound.mp3" '
    'Tonality="Ebm">\n'
    '\t\t\t<TEMPO Inizio="0.212" Bpm="122.00" Metro="4/4" Battito="1"/>\n'
    '\t\t\t<POSITION_MARK Name="" Type="0" Start="31.684" Num="0"/>\n'
    '\t\t\t<POSITION_MARK Name="drop" Type="0" Start="94.623" Num="2" '
    'Red="230" Green="40" Blue="40"/>\n'
    '\t\t\t<POSITION_MARK Name="" Type="0" Start="15.950" Num="-1"/>\n'
    '\t\t\t<POSITION_MARK Name="" Type="4" Start="62.000" End="63.967" Num="1"/>\n'
    '\t\t</TRACK>\n'
    '\t\t<TRACK TrackID="9" Name="Homenaje" Artist="Bosq" Genre="Latin" '
    'TotalTime="245" AverageBpm="104.00" '
    'Location="file://localhost/D:/VirtualDJ/02%20Homenaje.mp3"/>\n'
    '\t</COLLECTION>\n'
    '\t<PLAYLISTS>\n'
    '\t\t<NODE Type="0" Name="ROOT" Count="1">\n'
    '\t\t\t<NODE Name="Gigs" Type="0" Count="2">\n'
    '\t\t\t\t<NODE Name="Warmup" Type="1" KeyType="0" Entries="2">\n'
    '\t\t\t\t\t<TRACK Key="9"/>\n'
    '\t\t\t\t\t<TRACK Key="7"/>\n'
    '\t\t\t\t</NODE>\n'
    '\t\t\t\t<NODE Name="By Path" Type="1" KeyType="1" Entries="1">\n'
    '\t\t\t\t\t<TRACK Key="file://localhost/D:/VirtualDJ/02%20Homenaje.mp3"/>\n'
    '\t\t\t\t</NODE>\n'
    '\t\t\t</NODE>\n'
    '\t\t</NODE>\n'
    '\t</PLAYLISTS>\n'
    '</DJ_PLAYLISTS>\n')

#: Serato's database with no tracks: just the version record, as a fresh
#: install writes it. UTF-16BE text behind a tag and a big-endian length.
_SERATO_VERSION = "2.0/Serato Scratch LIVE Database".encode("utf-16-be")
EMPTY_SERATO_DB = b"vrsn" + len(_SERATO_VERSION).to_bytes(4, "big") + _SERATO_VERSION

SAFE_AND_SOUND = r"D:\Music\Justice\Justice - Safe & Sound.mp3"
HOMENAJE = r"D:\VirtualDJ\02 Homenaje.mp3"


def write_sample(tmp_path):
    """Materialise the fixture pair and return a Paths pointing at them.

    ``slp`` is pointed inside tmp_path too. It is not written here -- SlipStream
    creates its library on first write -- but naming it keeps ``_default_paths``
    from defaulting it to the app directory, which under test is the repo root.
    """
    from trackcleaner.config.paths import Paths

    nml = tmp_path / "collection.nml"
    xml = tmp_path / "database.xml"
    rekordbox = tmp_path / "rekordbox.xml"
    nml.write_text(SAMPLE_NML, encoding="utf-8")
    xml.write_text(EMPTY_VDJ_XML, encoding="utf-8")
    rekordbox.write_text(EMPTY_REKORDBOX_XML, encoding="utf-8")
    serato = tmp_path / "_Serato_" / "database V2"
    # exist_ok: restart tests materialise the sample twice in one directory.
    (tmp_path / "_Serato_" / "Subcrates").mkdir(parents=True, exist_ok=True)
    serato.write_bytes(EMPTY_SERATO_DB)
    return Paths(nml=str(nml), xml=str(xml), slp=str(tmp_path / "library.slp"),
                 rekordbox=str(rekordbox), serato=str(serato), csv="")
