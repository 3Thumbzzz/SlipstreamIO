#!/usr/bin/env python3
"""
nml_to_xml_sync.py

Reads cue point / beatgrid data from a Traktor NML file and syncs it into
an existing VirtualDJ-style database.xml file, applying an automatic
per-file timing correction and copying the NML play count across.

PATH MATCHING
-------------
For each NML <ENTRY>, a Windows-style path is built from <LOCATION> and
matched (case-insensitive, XML entities incl. &apos;/&quot; decoded)
against a <Song FilePath="..."> in the XML. No match -> that Song block
is left byte-for-byte untouched.

POI REWRITE
-----------
On a match, every <Poi .../> in that Song block is replaced from the NML
entry's <CUE_V2> points:
    TYPE="4"                       -> <Poi Pos="..." Type="beatgrid" />
    TYPE="0" or "4" w/ HOTCUE 0..7 -> <Poi Pos="..." Num="HOTCUE+1" Type="cue" />
A single CUE_V2 can yield BOTH a beatgrid and a cue (a "Beat Marker":
grid element carrying a real HOTCUE). Other TYPEs / HOTCUE=-1 are ignored.

TIMING CORRECTION (automatic, via audio start_time)
---------------------------------------------------
The correction magnitude is a fixed constant, CORRECTION_OFFSET_SEC
(51.536ms), confirmed across many tracks. WHICH set of points it applies
to is decided per file by the audio stream's container start_time
(encoder-delay offset), read via ffprobe:

  start_time > 0  (e.g. 0.025057, 0.023021, 0.011995)  -> CATEGORY A
        Beatgrid is late; cues are correct.
        ACTION: shift every beatgrid cues EARLIER by the constant. 
        (Grid then lands on its co-located cue.)

  start_time == 0 (0.000000)                            -> CATEGORY B
        Beatgrid is correct; cues are early.
        ACTION: cue untouched. Beatgrids
        untouched.

The start_time VALUE only classifies (A vs B); it does NOT scale the
magnitude -- the shift is always the same constant. Files whose
start_time can't be read are left uncorrected and logged.

Requires local access to the actual .mp3 files (ffprobe reads them). Set
AUDIO_ROOT_OVERRIDE if this machine's paths differ from the NML's.

PLAY COUNT
----------
The NML <INFO PLAYCOUNT="..."> value is written into the matched XML
<Infos PlayCount="..."> attribute (updated in place if present, inserted
if absent). All other <Infos> attributes are left untouched.

LOGGING
-------
LOG_FILENAME reports each matched file's category, and lists NML/XML
entries that failed to match a counterpart (both directions) so path
mismatches are easy to spot.
"""

import csv
import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from xml.sax.saxutils import unescape

# ============================= CONFIG =============================
NML_FILENAME = r"C:\Users\Tententen\Documents\Native Instruments\Traktor 3.5.1\collection.nml"
XML_FILENAME = r"D:\VirtualDJ\database.xml"
MAKE_BACKUP = True
POI_INDENT = "  "
LOG_FILENAME = "sync_log.txt"

# Fixed correction magnitude (seconds). Same for both categories.
CORRECTION_OFFSET_SEC = 0.051536

# start_time <= this is treated as 0 (Category B). Anything above is
# Category A regardless of its exact value.
START_TIME_ZERO_TOL = 0.0005

# If the NML's D:\... paths aren't openable as-is on this machine, set
# this to the folder that replaces the drive-letter root. None = use the
# path exactly as built.
AUDIO_ROOT_OVERRIDE = None

# ---- start_time source (the ffprobe call is the main bottleneck) -------
# The audio start_time only needs reading once per file; ffprobe is slow
# (~tens to hundreds of ms each), a CSV lookup is instant. Modes:
#   "cache"  : read start_time ONLY from START_TIME_CSV. Files not in the
#              CSV are treated as unreadable (logged, left uncorrected).
#              Fastest. Use once the CSV covers your library.
#   "probe"  : ffprobe every file, ignore the CSV. Slowest, no caching.
#   "update" : use the CSV where present; ffprobe only files missing from
#              it, then rewrite the CSV with the new values. Best of both
#              -- first run populates the cache, later runs are instant.
START_TIME_SOURCE = "cache"
START_TIME_CSV = "start_time_report.csv"
# ------------------------------------------------------------------------
# ================================================================


def ms_to_sec(ms: float) -> float:
    return ms / 1000.0


def build_windows_path(location_elem) -> str:
    volume = location_elem.get("VOLUME", "")
    dir_attr = location_elem.get("DIR", "")
    file_name = location_elem.get("FILE", "")
    dir_path = dir_attr.replace("/:", "\\").strip("\\")
    if volume:
        rest = "\\".join(p for p in (dir_path, file_name) if p)
        return volume + "\\" + rest if rest else volume
    return "\\".join(p for p in (dir_path, file_name) if p)


def resolve_local_path(windows_path: str) -> str:
    if AUDIO_ROOT_OVERRIDE:
        rest = re.sub(r"^[A-Za-z]:\\", "", windows_path).replace("\\", os.sep)
        return os.path.join(AUDIO_ROOT_OVERRIDE, rest)
    return windows_path


def get_start_time(path: str):
    """Audio stream start_time (float sec) via ffprobe, or None."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "a:0", path],
            capture_output=True, timeout=30,
        ).stdout
        streams = json.loads(out).get("streams", [])
        if not streams:
            return None
        st = streams[0].get("start_time")
        return float(st) if st is not None else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, OSError):
        return None


# ---- start_time cache (keyed by lowercased resolved local path) --------
_start_time_cache = {}       # path.lower() -> float
_cache_dirty = False         # True if we probed anything new to persist


def load_start_time_csv(csv_path: str):
    """Populate _start_time_cache from a start_time_report.csv (as written
    by scan_start_time.py or a prior run). Missing/unreadable file -> empty
    cache. Rows with a blank start_time are skipped."""
    if not os.path.exists(csv_path):
        return
    try:
        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                st = row.get("start_time", "").strip()
                p = row.get("path", "").strip()
                if p and st:
                    try:
                        _start_time_cache[p.lower()] = float(st)
                    except ValueError:
                        pass
    except OSError:
        pass


def save_start_time_csv(csv_path: str):
    """Rewrite the CSV from the current cache (used in 'update' mode)."""
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["start_time", "bucket", "path"])
            for p, st in sorted(_start_time_cache.items()):
                bucket = "B_cues_early" if st <= START_TIME_ZERO_TOL else "A_grid_late"
                w.writerow([f"{st:.6f}", bucket, p])
    except OSError:
        pass


def resolve_start_time(local_path: str):
    """
    Return the start_time for a file honoring START_TIME_SOURCE:
      cache  -> cache only (None if absent)
      probe  -> always ffprobe
      update -> cache if present, else ffprobe + remember it
    """
    global _cache_dirty
    key = local_path.lower()

    if START_TIME_SOURCE == "probe":
        return get_start_time(local_path)

    if key in _start_time_cache:
        return _start_time_cache[key]

    if START_TIME_SOURCE == "cache":
        return None  # not in cache and we're not allowed to probe

    # "update": probe the miss and cache it
    st = get_start_time(local_path)
    if st is not None:
        _start_time_cache[key] = st
        _cache_dirty = True
    return st


def classify(start_time):
    """Return 'A', 'B', or None (unknown) from start_time."""
    if start_time is None:
        return None
    if start_time <= START_TIME_ZERO_TOL:
        return "B"
    return "A"


def extract_nml_entries(nml_content: str):
    """
    path.lower() -> {"windows_path", "playcount", "pois": [(pos, num_or_None, type)]}
    """
    entry_pattern = re.compile(r"<ENTRY\b[^>]*>(.*?)</ENTRY>", re.DOTALL | re.IGNORECASE)
    entries = {}
    for entry_body in entry_pattern.findall(nml_content):
        full_entry = "<ENTRY>" + entry_body + "</ENTRY>"
        try:
            root = ET.fromstring(full_entry)
        except ET.ParseError:
            continue
        location = root.find("LOCATION")
        if location is None or not location.get("FILE"):
            continue
        path = build_windows_path(location)

        info = root.find("INFO")
        playcount = None
        if info is not None and info.get("PLAYCOUNT") is not None:
            playcount = info.get("PLAYCOUNT")

        pois = []
        for cue in root.findall(".//CUE_V2"):
            type_id = int(cue.get("TYPE", 0))
            pos_sec = ms_to_sec(float(cue.get("START", 0)))
            hotcue = int(cue.get("HOTCUE", -1))
            if type_id == 4:
                pois.append((pos_sec, None, "beatgrid"))
            if type_id in (0, 4) and 0 <= hotcue <= 7:
                pois.append((pos_sec, hotcue + 1, "cue"))

        entries[path.lower()] = {"windows_path": path, "playcount": playcount, "pois": pois}
    return entries


def format_poi_line(pos_sec: float, num, poi_type: str) -> str:
    pos_sec = max(pos_sec, 0.0)
    if poi_type == "beatgrid":
        return f'{POI_INDENT}<Poi Pos="{pos_sec:.6f}" Type="beatgrid" />'
    return f'{POI_INDENT}<Poi Pos="{pos_sec:.6f}" Num="{num}" Type="cue" />'


def set_playcount(body: str, playcount: str) -> str:
    """Update PlayCount in the <Infos .../> tag, inserting if absent."""
    if playcount is None:
        return body

    def repl(m):
        infos = m.group(0)
        if re.search(r'\bPlayCount="[^"]*"', infos):
            return re.sub(r'\bPlayCount="[^"]*"', f'PlayCount="{playcount}"', infos)
        # insert right before the closing /> of the Infos tag
        return re.sub(r'\s*/>\s*$', f' PlayCount="{playcount}" />', infos)

    return re.sub(r'<Infos\b[^>]*/>', repl, body, count=1)


SONG_BLOCK_RE = re.compile(
    r'(<Song\b[^>]*FilePath="([^"]*)"[^>]*>)(.*?)(</Song>)', re.DOTALL
)
POI_LINE_RE = re.compile(r"[ \t]*<Poi\b[^>]*/>[ \t]*\n?")


def sync_xml(xml_content: str, nml_entries: dict, log_lines: list):
    stats = {"matched": 0, "A": 0, "B": 0, "unknown": 0, "playcount_set": 0}
    unmatched_xml_paths = []
    matched_keys = set()

    # Count how many Song blocks we'll actually process (those whose path
    # matches an NML entry) so the progress readout has a real total.
    total_to_process = 0
    for mm in SONG_BLOCK_RE.finditer(xml_content):
        fp = unescape(mm.group(2), {"&apos;": "'", "&quot;": '"'}).lower()
        if fp in nml_entries:
            total_to_process += 1
    progress = {"done": 0}

    def replace_song(m):
        open_tag, filepath, body, close_tag = m.groups()
        decoded_path = unescape(filepath, {"&apos;": "'", "&quot;": '"'})
        key = decoded_path.lower()
        if key not in nml_entries:
            unmatched_xml_paths.append(decoded_path)
            return m.group(0)

        progress["done"] += 1
        pct = progress["done"] / total_to_process * 100 if total_to_process else 100.0
        print(f"\r  {pct:5.1f}%  {progress['done']}/{total_to_process} files",
              end="", flush=True)

        stats["matched"] += 1
        matched_keys.add(key)
        entry = nml_entries[key]
        pois = entry["pois"]

        local_path = resolve_local_path(entry["windows_path"])
        start_time = resolve_start_time(local_path)
        cat = classify(start_time)

        off = 0.0
        if cat == "A":
            stats["A"] += 1
            off = CORRECTION_OFFSET_SEC        # Class A: everything moves earlier
        elif cat == "B":
            stats["B"] += 1
            off = 0.0                          # Class B: left untouched
        else:
            stats["unknown"] += 1

        st_str = f"{start_time:.6f}" if start_time is not None else "unreadable"
        log_lines.append(
            f"  [{cat or '?'}] start_time={st_str}  {entry['windows_path']}"
        )

        new_body = POI_LINE_RE.sub("", body)
        if not new_body.endswith("\n"):
            new_body += "\n"

        # Model:
        #   Class A (start_time>0):  beatgrids AND cues shift -OFF (earlier).
        #   Class B (start_time==0): nothing shifts (raw NML->XML is correct).
        # Verified vs Cut Master DC (A): cue 0.140563 -> 0.089027,
        #   17.693700 -> 17.642164, etc.
        lines = [format_poi_line(pos - off, num, t) for pos, num, t in pois]
        if lines:
            new_body += "\n".join(lines) + "\n"

        new_body = set_playcount(new_body, entry["playcount"])
        if entry["playcount"] is not None:
            stats["playcount_set"] += 1

        return open_tag + new_body + close_tag

    new_content = SONG_BLOCK_RE.sub(replace_song, xml_content)
    if total_to_process:
        print()  # newline after the in-place progress line
    unmatched_nml = [e["windows_path"] for k, e in nml_entries.items() if k not in matched_keys]
    return new_content, stats, unmatched_xml_paths, unmatched_nml


def main():
    if not os.path.exists(NML_FILENAME):
        print(f"Error: '{NML_FILENAME}' not found!")
        return
    if not os.path.exists(XML_FILENAME):
        print(f"Error: '{XML_FILENAME}' not found!")
        return

    with open(NML_FILENAME, "r", encoding="utf-8", errors="ignore") as f:
        nml_content = f.read()
    with open(XML_FILENAME, "r", encoding="utf-8", errors="ignore") as f:
        xml_content = f.read()

    print(f"Parsing '{NML_FILENAME}'...")
    nml_entries = extract_nml_entries(nml_content)
    print(f"Found {len(nml_entries)} NML entries with a resolvable file path.")

    if START_TIME_SOURCE in ("cache", "update"):
        load_start_time_csv(START_TIME_CSV)
        print(f"start_time source: '{START_TIME_SOURCE}' "
              f"({len(_start_time_cache)} entries loaded from '{START_TIME_CSV}').")
    else:
        print(f"start_time source: 'probe' (ffprobe every file).")

    print(f"Syncing into '{XML_FILENAME}' (reading audio start_time per file)...")
    log_lines = [
        f"nml_to_xml_sync.py run log -- {datetime.now().isoformat(timespec='seconds')}",
        f"CORRECTION_OFFSET_SEC = {CORRECTION_OFFSET_SEC}",
        "",
        "Per matched file (category from audio start_time):",
    ]
    new_xml, stats, unmatched_xml, unmatched_nml = sync_xml(xml_content, nml_entries, log_lines)

    print(f"Matched {stats['matched']} entries: "
          f"A(All-shift)={stats['A']}  B(Non-shift)={stats['B']}  unknown={stats['unknown']}")
    print(f"PlayCount written for {stats['playcount_set']} entries.")
    print(f"Unmatched: {len(unmatched_xml)} XML songs, {len(unmatched_nml)} NML entries.")

    log_lines += ["", "=" * 80,
                  f"NML entries with NO matching Song in XML -- {len(unmatched_nml)}:", "=" * 80]
    log_lines += [f"  {p}" for p in unmatched_nml]
    log_lines += ["", "=" * 80,
                  f"XML songs with NO matching NML entry -- {len(unmatched_xml)}:", "=" * 80]
    log_lines += [f"  {p}" for p in unmatched_xml]
    with open(LOG_FILENAME, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))
    print(f"Log written to '{LOG_FILENAME}'.")

    if START_TIME_SOURCE == "update" and _cache_dirty:
        save_start_time_csv(START_TIME_CSV)
        print(f"start_time cache updated -> '{START_TIME_CSV}' "
              f"({len(_start_time_cache)} entries).")

    if MAKE_BACKUP:
        shutil.copyfile(XML_FILENAME, XML_FILENAME + ".bak")
        print(f"Backup saved to '{XML_FILENAME}.bak'.")

    with open(XML_FILENAME, "w", encoding="utf-8") as f:
        f.write(new_xml)
    print("Done.")


if __name__ == "__main__":
    main()