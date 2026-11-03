"""Cue / beatgrid conversion + the verified start_time correction model.

This is the one part of the app that is empirically verified (see HANDOFF.md,
"Cue timing"). The forward direction (Traktor NML -> VirtualDJ XML) is ported
unchanged from ``nml_to_xml_sync.py``. The reverse direction (VDJ -> Traktor)
applies the **inverse** correction so a round-trip is stable.

Model:
    Class A (start_time > 0):  everything shifts -51.536 ms (earlier)  on NML->VDJ
    Class B (start_time == 0): unchanged
The magnitude never scales with start_time; start_time only *classifies*.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

# --- verified constants (HANDOFF.md, "Cue timing") -----------------------
CORRECTION_OFFSET_SEC = 0.051536
START_TIME_ZERO_TOL = 0.0005


# ============================ cue data model ============================
@dataclass(frozen=True)
class Poi:
    """A VirtualDJ point-of-interest / unified in-memory cue.

    ``pos`` is in **seconds**. ``num`` is the VDJ cue number (Traktor HOTCUE+1)
    or ``None`` for a beatgrid anchor. ``kind`` is ``"cue"`` or ``"beatgrid"``.
    """

    pos: float
    num: Optional[int]
    kind: str  # "cue" | "beatgrid"


def ms_to_sec(ms: float) -> float:
    return ms / 1000.0


def sec_to_ms(sec: float) -> float:
    return sec * 1000.0


# ======================= NML CUE_V2 -> unified Poi ======================
def pois_from_cue_v2(cues: list[dict]) -> list[Poi]:
    """Extract unified Pois from a list of CUE_V2 attribute dicts.

    Rules (HANDOFF.md, "Cue timing" -> "What counts as a cue"):
      TYPE=4                          -> beatgrid at START/1000
      TYPE in (0,4) with 0<=HOTCUE<=7 -> cue at START/1000, Num=HOTCUE+1
    A single CUE_V2 can emit BOTH (the "Beat Marker" pattern).
    """
    pois: list[Poi] = []
    for cue in cues:
        try:
            type_id = int(cue.get("TYPE", 0))
            pos_sec = ms_to_sec(float(cue.get("START", 0)))
            hotcue = int(cue.get("HOTCUE", -1))
        except (TypeError, ValueError):
            continue
        if type_id == 4:
            pois.append(Poi(pos_sec, None, "beatgrid"))
        if type_id in (0, 4) and 0 <= hotcue <= 7:
            pois.append(Poi(pos_sec, hotcue + 1, "cue"))
    return pois


# ================= correction application (both directions) =============
def classify(start_time: Optional[float]) -> Optional[str]:
    """'A', 'B', or None (unknown) from an audio stream start_time."""
    if start_time is None:
        return None
    return "B" if start_time <= START_TIME_ZERO_TOL else "A"


#: A grid marker is a downbeat, so moving one by whole bars changes neither
#: where the beats fall nor where the bars start.
BEATS_PER_BAR = 4


def bar_seconds(bpm) -> Optional[float]:
    """One bar at ``bpm``, or None when there is no usable tempo."""
    try:
        bpm = float(bpm)
    except (TypeError, ValueError):
        return None
    return BEATS_PER_BAR * 60.0 / bpm if bpm > 0 else None


def grid_anchor_at_or_after_zero(pos_sec: float, bpm) -> float:
    """A grid anchor moved forward by whole bars until it is not negative.

    **A grid anchor is a phase, not a place.** A cue below zero means "the very
    start" and flooring it is right; flooring a grid anchor moves every beat
    line in the track. VirtualDJ's own analysis puts anchors before zero --
    ``Tim aka New T. - SWEET MISERY`` sits at -1.588 s at 94 BPM -- and the
    floor wrote it into Traktor at 51.5 ms, about half a beat off. One bar
    later is the same grid with a non-negative anchor, which every program can
    store. With no tempo there is nothing to step by, so it falls back to the
    floor.
    """
    if pos_sec >= 0:
        return pos_sec
    bar = bar_seconds(bpm)
    if bar is None:
        return 0.0
    return pos_sec + math.ceil(-pos_sec / bar - 1e-9) * bar


def corrected_pos_nml_to_vdj(pos_sec: float, category: Optional[str]) -> float:
    """Apply the forward correction when writing NML cue data into VDJ.

    Class A shifts everything -CORRECTION_OFFSET_SEC (floored at 0). Class B and
    unknown pass through unchanged.
    """
    if category == "A":
        return max(pos_sec - CORRECTION_OFFSET_SEC, 0.0)
    return max(pos_sec, 0.0)


def corrected_pos_vdj_to_nml(pos_sec: float, category: Optional[str]) -> float:
    """Inverse correction when writing VDJ cue data back into NML.

    Class A adds +CORRECTION_OFFSET_SEC so ``vdj -> nml -> vdj`` is a no-op.
    Class B / unknown pass through.
    """
    if category == "A":
        return max(pos_sec + CORRECTION_OFFSET_SEC, 0.0)
    return max(pos_sec, 0.0)


# ========================= VDJ <Poi> line I/O ===========================
POI_INDENT = "  "
POI_LINE_RE = re.compile(r"[ \t]*<Poi\b[^>]*/>[ \t]*\n?")
POI_ATTR_RE = re.compile(r'<Poi\b([^>]*)/>')

# ---- which stored points this app owns -------------------------------------
# Only cues and beatgrids round-trip between the two apps. Everything else is
# app-specific (VirtualDJ automix/remix points; Traktor fade-in/out, load and
# loop markers) and must be left strictly alone: ignored when reading so it can
# never masquerade as a cue, and preserved when writing so a sync never
# destroys it.
OWNED_POI_TYPES = frozenset({"cue", "beatgrid"})       # VirtualDJ <Poi Type="...">
OWNED_CUE_V2_TYPES = frozenset({0, 4})                 # Traktor <CUE_V2 TYPE="...">


def poi_type_of(attrs: str) -> str:
    """``Type`` of a VirtualDJ <Poi ...>; VDJ omits it on a plain cue."""
    m = re.search(r'Type="([^"]*)"', attrs)
    return m.group(1) if m else "cue"


def is_owned_poi(attrs: str) -> bool:
    return poi_type_of(attrs) in OWNED_POI_TYPES


def is_owned_cue_v2(element: str) -> bool:
    m = re.search(r'TYPE="(-?\d+)"', element)
    try:
        return (int(m.group(1)) if m else 0) in OWNED_CUE_V2_TYPES
    except ValueError:
        return True   # unparseable TYPE: treat as ours (previous behaviour)


def format_poi_line(poi: Poi) -> str:
    pos = max(poi.pos, 0.0)
    if poi.kind == "beatgrid":
        return f'{POI_INDENT}<Poi Pos="{pos:.6f}" Type="beatgrid" />'
    return f'{POI_INDENT}<Poi Pos="{pos:.6f}" Num="{poi.num}" Type="cue" />'


def parse_poi_lines(song_body: str) -> list[Poi]:
    """Read the cue/beatgrid <Poi .../> lines out of a VDJ <Song> body.

    Two things this must get right:

    - **A missing ``Pos`` means position 0.0, not "skip this point".**
      VirtualDJ omits the attribute for a point at the very start, e.g.
      ``<Poi Num="1" Type="cue" />``. Dropping those made real cues invisible to
      the diff, so a track that had gained cues still compared as in-sync.
    - **Only ``cue`` and ``beatgrid`` are ours.** ``automix``/``remix`` points
      are VirtualDJ-specific and must not be read as cues.
    """
    pois: list[Poi] = []
    for m in POI_ATTR_RE.finditer(song_body):
        attrs = m.group(1)
        kind = poi_type_of(attrs)
        if kind not in OWNED_POI_TYPES:
            continue
        pos_m = re.search(r'Pos="([^"]*)"', attrs)
        try:
            pos = float(pos_m.group(1)) if pos_m else 0.0
        except ValueError:
            pos = 0.0
        num_m = re.search(r'Num="([^"]*)"', attrs)
        num = int(num_m.group(1)) if (num_m and kind == "cue") else None
        pois.append(Poi(pos, num, kind))
    return pois


# ==================== NML <CUE_V2> element I/O ==========================
#: A whole CUE_V2 element: self-closing, or open-through-close **including any
#: children and newlines between them**.
#:
#: Traktor writes a grid marker with a nested child, across two lines:
#:
#:     <CUE_V2 … TYPE="4" HOTCUE="-1"><GRID BPM="129.098221"></GRID>
#:     </CUE_V2>
#:
#: The previous pattern (`<CUE_V2\b[^>]*/?>(?:</CUE_V2>)?`) could not span that.
#: Stripping an owned cue therefore removed only the *opening tag* and left
#: `<GRID …></GRID></CUE_V2>` orphaned -- malformed XML that Traktor refuses to
#: open. It went unnoticed because the test fixture had no such marker; every
#: real collection does.
CUE_V2_RE = re.compile(r"<CUE_V2\b[^>]*?(?:/>|>.*?</CUE_V2>)", re.DOTALL)


def format_cue_v2(poi: Poi, bpm=None) -> str:
    """Render a unified Poi as a Traktor CUE_V2 element (position already in
    the NML timeline, i.e. inverse-corrected by the caller).

    A grid marker carries its tempo as a ``<GRID BPM>`` child, as Traktor
    writes its own. Left out, Traktor adds ``<GRID BPM="0.000000">`` on its
    next save -- 818 such markers in the reported collection, against 79 before
    this app wrote grids.

    A grid marker is always written with ``HOTCUE="-1"`` so it occupies no pad
    slot. This keeps the encoding **round-trip stable**: ``pois_from_cue_v2``
    turns a ``TYPE="4"`` with ``0 <= HOTCUE <= 7`` into *both* a beatgrid and a
    cue, so writing a pure grid as ``HOTCUE="0"`` used to resurrect a phantom
    cue ``Num=1`` on read-back and the track could never diff as in-sync.
    A co-located grid+cue is written as two elements (grid HOTCUE=-1 plus a
    TYPE="0" cue), which reads back as exactly that pair.
    """
    start_ms = max(sec_to_ms(poi.pos), 0.0)
    grid = ""
    if poi.kind == "beatgrid":
        hotcue = -1                     # never claim a pad slot
        name = "Beat Marker"
        type_id = 4
        if bar_seconds(bpm) is not None:
            grid = f'<GRID BPM="{float(bpm):.6f}"></GRID>'
    else:
        hotcue = (poi.num - 1) if poi.num is not None else -1
        name = "n.n."
        type_id = 0
    return (f'<CUE_V2 NAME="{name}" DISPL_ORDER="0" TYPE="{type_id}" '
            f'START="{start_ms:.6f}" LEN="0.000000" REPEATS="-1" '
            f'HOTCUE="{hotcue}">{grid}</CUE_V2>')


def playcount_from_infos(infos_attrs: str) -> Optional[str]:
    m = re.search(r'PlayCount="([^"]*)"', infos_attrs)
    return m.group(1) if m else None


def set_playcount(body: str, playcount: Optional[str]) -> str:
    """Update PlayCount in the first <Infos .../> tag, inserting if absent."""
    if playcount is None:
        return body

    def repl(m):
        infos = m.group(0)
        if re.search(r'\bPlayCount="[^"]*"', infos):
            return re.sub(r'\bPlayCount="[^"]*"', f'PlayCount="{playcount}"', infos)
        return re.sub(r'\s*/>\s*$', f' PlayCount="{playcount}" />', infos)

    return re.sub(r'<Infos\b[^>]*/>', repl, body, count=1)


# ===================== start_time cache (ffprobe) =======================
class StartTimeCache:
    """Keyed by lowercased resolved local path. Mirrors the sync script's
    cache/probe/update semantics (HANDOFF.md, "Cue timing")."""

    def __init__(self, csv_path: str, mode: str = "update"):
        self.csv_path = csv_path
        self.mode = mode              # cache | probe | update
        self._cache: dict[str, float] = {}
        self._dirty = False

    def load(self) -> None:
        if self.mode == "probe" or not self.csv_path or not os.path.exists(self.csv_path):
            return
        try:
            with open(self.csv_path, "r", newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    st = (row.get("start_time") or "").strip()
                    p = (row.get("path") or "").strip()
                    if p and st:
                        try:
                            self._cache[p.lower()] = float(st)
                        except ValueError:
                            pass
        except OSError:
            pass

    def save(self) -> None:
        """Write what was probed back to disk.

        Must actually be *called*, which for a long time it was not: the file
        stayed at its 24-byte header while every session re-probed every track it
        wrote -- 28 ms each, 12 seconds of an 865-track run, for ever rather than
        once. A cache that is filled and never flushed is not a cache.
        """
        if self.mode != "update" or not self._dirty or not self.csv_path:
            return
        try:
            with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["start_time", "bucket", "path"])
                for p, st in sorted(self._cache.items()):
                    bucket = "B" if st <= START_TIME_ZERO_TOL else "A"
                    w.writerow([f"{st:.6f}", bucket, p])
        except OSError:
            pass

    #: Probes to run at once in :meth:`prime`. ffprobe is a subprocess, so the
    #: threads spend their time waiting on it rather than holding the GIL.
    PRIME_WORKERS = 8

    def prime(self, paths, workers: int = PRIME_WORKERS) -> int:
        """Probe every one of ``paths`` this cache does not know yet, in
        parallel, and return how many were probed.

        Purely an accelerator: :meth:`get` still probes its own misses, so no
        caller has to know this exists and nothing is probed here that would not
        have been probed one at a time a moment later. A path that fails to probe
        is left a miss rather than cached as a wrong value.
        """
        if self.mode == "cache":
            return 0                      # never probes; nothing to prime
        missing = []
        seen = set()
        for path in paths:
            key = path.lower()
            if key in seen or (self.mode != "probe" and key in self._cache):
                continue
            seen.add(key)
            missing.append(path)
        if not missing:
            return 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for path, st in zip(missing, pool.map(_ffprobe_start_time, missing)):
                if st is not None:
                    self._cache[path.lower()] = st
                    self._dirty = True
        return len(missing)

    def get(self, local_path: str) -> Optional[float]:
        key = local_path.lower()
        if self.mode == "probe":
            return _ffprobe_start_time(local_path)
        if key in self._cache:
            return self._cache[key]
        if self.mode == "cache":
            return None
        st = _ffprobe_start_time(local_path)   # update mode: probe the miss
        if st is not None:
            self._cache[key] = st
            self._dirty = True
        return st

    def __len__(self) -> int:
        return len(self._cache)


def _ffprobe_start_time(path: str) -> Optional[float]:
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
