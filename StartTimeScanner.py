#!/usr/bin/env python3
"""
scan_start_time.py

Standalone diagnostic to test the "start_time" classification theory
against your library, WITHOUT touching database.xml or doing any audio
analysis. It only reads each MP3's container-level start_time via ffprobe
(the encoder-delay / gapless-padding offset the decoder reports) and
buckets every file accordingly.

THEORY BEING TESTED
-------------------
Across a labeled sample, start_time cleanly split the two behaviors:
  - start_time > 0 (~0.025057s):  GROUP A -- "beatgrid too late"
        (XML cues are correct, beatgrids need shifting earlier)
  - start_time == 0 (0.000000s):  GROUP B -- "cues too early"
        (XML beatgrid is correct, cues need shifting)

This script does NOT apply any correction. It just reports which bucket
each file falls into so you can spot-check whether the split matches what
you hear in VDJ. If it holds across a big sample, start_time becomes the
automatic classifier and we wire it into the sync script.

REQUIREMENTS
------------
- ffprobe (part of ffmpeg) must be installed and on PATH.
- Point ROOT at your music library (or a subfolder to test a batch).

OUTPUT
------
- Console summary: count per bucket.
- start_time_report.csv: one row per file with its start_time, detected
  bucket, and any unusual start_time value (not ~0.0 and not ~0.025) so
  outliers that break the theory are easy to find.
"""

import csv
import json
import os
import subprocess
import sys

# ============================= CONFIG =============================
# Folder to scan (recurses into subfolders). Change to a subfolder to
# test a smaller batch first.
ROOT = r"D:\Music"

# File extensions to scan.
EXTENSIONS = (".mp3",)

# Tolerance for grouping start_time values.
ZERO_TOL = 0.0005            # start_time <= this  -> treated as 0.000000
DELAY_CENTER = 0.025057      # the observed nonzero value
DELAY_TOL = 0.003            # +/- window counted as "the ~0.025 delay"

OUTPUT_CSV = "start_time_report.csv"
# ================================================================


def get_start_time(path: str):
    """Return the stream start_time (float seconds) via ffprobe, or None."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", "-select_streams", "a:0", path,
            ],
            capture_output=True, timeout=30,
        ).stdout
        data = json.loads(out)
        streams = data.get("streams", [])
        if not streams:
            return None
        st = streams[0].get("start_time")
        return float(st) if st is not None else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, OSError):
        return None


def classify(start_time):
    """Return (bucket_label, action, is_outlier)."""
    if start_time is None:
        return "UNKNOWN", "could not read start_time", True
    if start_time <= ZERO_TOL:
        return "B_cues_early", "grid correct; shift cues to align to beatgrid", False
    if abs(start_time - DELAY_CENTER) <= DELAY_TOL:
        return "A_grid_late", "cues correct; shift beatgrids earlier to nearest cue", False
    # nonzero but not the expected ~0.025 value -> flag it
    return "A_grid_late?", f"nonzero start_time {start_time:.6f} (unexpected value)", True


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else ROOT
    if not os.path.isdir(root):
        print(f"Error: '{root}' is not a folder. Edit ROOT or pass a path argument.")
        return

    print(f"Scanning '{root}' for {', '.join(EXTENSIONS)} ...")

    # First pass: gather the full file list so we have a known total for
    # the progress display.
    all_files = []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if name.lower().endswith(EXTENSIONS):
                all_files.append(os.path.join(dirpath, name))

    total_files = len(all_files)
    print(f"Found {total_files} files. Reading start_time...\n")

    rows = []

    for i, full in enumerate(all_files, 1):
        st = get_start_time(full)
        bucket, action, outlier = classify(st)
        rows.append({
            "start_time": f"{st:.6f}" if st is not None else "",
            "bucket": bucket,
            "outlier": "YES" if outlier else "",
            "action": action,
            "path": full,
        })

        # live progress on one rewritten line
        pct = i / total_files * 100 if total_files else 100.0
        print(f"\r  {pct:5.1f}%  {i}/{total_files} files", end="", flush=True)

    print()  # newline after the progress line
    counts = {}
    for r in rows:
        counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["start_time", "bucket", "outlier", "action", "path"])
        w.writeheader()
        # outliers first so they're easy to eyeball
        rows.sort(key=lambda r: (r["outlier"] != "YES", r["bucket"]))
        w.writerows(rows)

    total = len(rows)
    print(f"\nScanned {total} files.")
    for bucket in sorted(counts):
        print(f"  {bucket:16} {counts[bucket]}")
    n_outliers = sum(1 for r in rows if r["outlier"] == "YES")
    print(f"\n{n_outliers} outliers (unexpected/unreadable start_time) -- listed first in the CSV.")
    print(f"Full per-file report: {OUTPUT_CSV}")
    print("\nSpot-check a handful from each bucket in VDJ. If the split holds,")
    print("start_time becomes the automatic classifier for the sync script.")


if __name__ == "__main__":
    main()