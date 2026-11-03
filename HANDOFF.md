# SlipStreamIO — Maintainer Handoff

*A PySide6 desktop app that keeps Traktor, VirtualDJ and SlipStream libraries in
sync, and lets you edit playlists and collections across all of them.*

This is the only document for the codebase. It states how the app works and what
breaks if you change a given piece — read the section that covers what you are
about to touch. Sections are referenced **by name**, never by number, so
inserting one cannot falsify a citation elsewhere.

Snapshot: **74 package modules, 77 test modules + fixtures and `conftest.py`,
1792 tests passing, 5 skipped** (2026-11-03, Python 3.13, PySide6 6.11.1).

---

## Running it

```
python app.py                                        # the GUI
python -m pytest tests/ -q -n auto --dist loadfile   # the suite, ~3 min measured 2026-10-31
```

External dependencies: **PySide6**, and **ffmpeg/ffprobe on PATH** (waveform
peaks, PCM decode for playback, start-time probing, import metadata).

**`--dist loadfile` is not optional.** xdist's default splits individual *tests*
across workers, so tests from different modules interleave inside one process
sharing a single `QApplication`. The GUI modules do not survive that — several
`test_view_preservation` cases fail depending purely on which neighbours they
land beside. `loadfile` keeps each file's tests together. **A whole-suite serial run
currently hangs** (2026-10-31): after ~1,340 tests in one process it stops on
the first `test_theme` case, which passes in 0.3 s on its own, so something the
earlier modules leave on the shared `QApplication` makes a style switch stall.
Until that is found, cross-check serially per module rather than in one
process. xdist can hide ordering and isolation defects. `test_drag_scroll`
reads the **real mouse** via `GetAsyncKeyState`, so run the suite with no mouse
button held or three of its cases fail for environmental reasons.

Tests set `QT_QPA_PLATFORM=offscreen` and run against fixture libraries.
**Never point a test or a manual check at the live `collection.nml` /
`database.xml`** — copy them into a sandbox first.

### Packaging and releases

`pyinstaller SlipStreamIO.spec --noconfirm` (PyInstaller 6.22.3, pinned in
`requirements-dev.txt`) builds for the platform it runs on: a portable
`dist/SlipStreamIO/` folder on Windows (~170 MB), `dist/SlipStreamIO.app` on
macOS. PyInstaller cannot cross-compile, so **pushing a `v*` tag runs
`.github/workflows/release.yml`**, which builds both on GitHub's Windows and
macOS (Apple silicon) runners and publishes a release with the two zips.

- **Where a packaged build keeps its files** (`config/paths.app_dir`): Windows
  beside the exe, as before. macOS in `~/Library/Application Support/SlipStreamIO`,
  because writing into the bundle breaks its signature and a downloaded app runs
  from a read-only translocated copy.
- **Images ship inside the package** (`sys._MEIPASS/Images`); an `Images`
  folder beside the app overrides them (`config/paths.images_dir`).
- **ffmpeg/ffprobe are not bundled.** A Finder-launched macOS app gets a bare
  PATH, so `app._find_mac_tools` appends Homebrew's directories.
- **Every `subprocess.run` passes `creationflags=CREATE_NO_WINDOW`** on
  Windows (0 elsewhere). Run from source the tools share Python's console; a
  windowed build has none, so 1.0.0 opened a console per ffprobe and an
  Overwrite over uncached tracks flashed them non-stop, freezing the desktop.
  `tests/test_packaging.py` fails on any call that omits it.
- **A packaged build has its own state.** Its start-time cache, baseline and
  settings sit beside the exe, separate from a source checkout's, so a first
  sync from the exe probes every track it touches.
- **First-run library guesses use only the programs' standard locations**
  (`main_window._default_paths`): the most recently written
  `Documents/Native Instruments/Traktor*/collection.nml`, and VirtualDJ's
  `Documents/VirtualDJ/database.xml` then `<drive>:\VirtualDJ\database.xml`.
  The list used to end in development-machine paths; `D:\VirtualDJ\traktor.nml`
  (an old Traktor 3 collection) existed on the reported machine, so a freshly
  unpacked 1.0.0/1.0.1 exe took it for Traktor and an Overwrite synced into it.
- **Windows-only features stay Windows-only on macOS:** Recycle Bin
  (`io/recycle.py` refuses, so nothing is deleted), Explorer reveal, the
  "program is running" check (answers "can't tell"), and the drag-scroll mouse
  hook. The macOS build is unsigned; users open it once with right-click → Open.
- What the app writes at runtime (settings, backups, logs, the baseline and
  start-time CSVs, `library.slp`, `userControls.ini`) is in `.gitignore`.
  Tests: `tests/test_packaging.py`.

### Safety posture (binding)

These are properties of the app, not preferences. Breaking one is a defect
whatever else the change achieves.

- **Text-surgical writes.** A collection edit is a regex scoped to a single
  `<ENTRY>` / `<Song>` block. Everything else in the file — formatting,
  attribute order, unrelated blocks, and anything the vendor wrote that this app
  does not understand — survives byte-for-byte.
- **Atomic writes.** `<file>.tmp` + `os.replace`, on a **serialized
  single-worker queue**, so a concurrent reader never sees a half-written file
  and rapid edits cannot interleave.
- **A collection file that does not parse is never persisted.**
- **Never a permanent delete.** Files go to the Recycle Bin, recoverable.
- **Audio files are written in exactly one way: Serato's cue frames.** Only the
  `Serato Markers2` / `Serato BeatGrid` ID3 frames ever change, on a copy that
  is verified (our points landed, every other frame and every byte after the
  tag identical) before it replaces the original, and only after the old
  frames are saved to `backups/serato_tags/` (see **Serato**). Renaming in
  Clean up is the only other thing that touches an audio file.
- **A whole-library backup is taken before any destructive collection change**
  — deletions, imports and Clean up, via `guarded_write`. It covers every
  library file `Paths` names (see **Backups**). A *playlist* sync
  additionally copies the collection it is about to rewrite — **once per run**:
  the window's Sync wraps its loop in `SyncEngine.batch()`, because one copy
  per playlist had left 322 copies of `collection.nml` (40 GB) and 602 of
  `database.xml` (42 GB) beside the originals. A single-track
  sync does not (see **Sync**).
- **VirtualDJ must be closed before `database.xml` is written.** VirtualDJ
  rewrites its database on exit and would clobber the edit; the app checks
  (`io/vdj_process.py`, `LibraryAdapter.clobbers_on_exit`) and warns.
- **`model/cues.py` is load-bearing.** It implements an empirically verified
  timing model (see **Cue timing**). Do not "simplify" it.

---

## Architecture

**[ARCHITECTURE.md](ARCHITECTURE.md) draws this**: the package graph and the
adapter boundary, what happens between launch and a coloured square, what happens
between clicking Sync and bytes on disk, and how the window is composed. The
rules below are the same ones, in prose.

The app reads and writes one library per **registered program**:

| Program | Collection file | Playlists |
|---|---|---|
| **Traktor** | `collection.nml` — one XML file, `<ENTRY>` per track, `<PLAYLISTS>` tree | inside the same `.nml` |
| **VirtualDJ** | `database.xml` — `<Song>` per track | a `Folders/` tree of `.vdjfolder` / `.subfolders` + `order` files |
| **Rekordbox** | `rekordbox.xml` — its XML exchange file, `<TRACK>` per track | a `<PLAYLISTS>` `<NODE>` tree in the same file |
| **Serato DJ** | `_Serato_\database V2` — binary records; **cues inside the audio files** | `Subcrates\*.crate`, one file per crate, tree in the file name |
| **SlipStream** | `library.slp` — this app's own neutral format | inside the same `.slp` |

**Nothing outside `trackcleaner/adapters/` branches on which program it is.** If
you are writing `if source == VDJ` anywhere else, the thing you need is an
adapter method. A few pre-adapter spots still name a program and are known
debt, not precedent: `MembershipController.set_allow_duplicates` checks
`node.source != VDJ` (the context menu beside it already asks
`supports_duplicate_policy`), `MainWindow` defaults `_current_source` to VDJ and
the reference to Traktor, and `io/backup.py` keeps the nml/xml/`Folders/`
snapshot layout and the two legacy backup-name prefixes by name.
`ADAPTER_CLASSES` in `adapters/registry.py` is the single list a
new program is added to; sidebar trees, path dialog rows, watermarks and the
contract test suite all derive from it.

**Every adapter write method returns a *job*** — a zero-arg callable — rather
than performing the write. The caller decides when and on which thread it runs.

**An adapter can be asked to read specific tracks back.** `read_tracks(by_key)`
takes `{key: path}` and returns freshly-parsed `Track`s for just those entries,
locating them rather than walking the document. It is what lets a targeted
refresh keep its colours coming from disk — see **UI behaviour**.

**A track's stored path lives in two places per program, and both move
together.** `repath_tracks_job` rewrites the collection entry *and* every
playlist reference, because Traktor names playlist members by path
(`<PRIMARYKEY>`), VirtualDJ writes the path into each `.vdjfolder`, and
SlipStream keeps it on the playlist `ENTRY`. Rewriting only the collection drops
the track out of every playlist holding it. A playlist may also name a track the
collection has no entry for, so the membership rewrite is not gated on finding
one.

**Positions are canonical.** Each adapter declares only how *its* stored
positions relate to one shared timeline: seconds from the first decoded audio
sample (`to_native_pos` / `from_native_pos` in `adapters/base.py`). A
Traktor↔VirtualDJ conversion is therefore `traktor → native → vdj`, and a third
program needs no knowledge of the first two. `tests/test_adapter_timeline.py`
pins the composed result against the original pairwise correction exactly.

**`.slp` is both SlipStream's own database and the neutral intermediate layer.**
It is NML-inspired in shape but carries plain absolute paths, separate
`CUE`/`BEATGRID` elements, one key notation and canonical positions. It is
parsed and re-rendered with ElementTree rather than by text surgery — safe there
*only* because the app owns every byte of the file. It is authoritative for
SlipStream-owned data only; each vendor database stays authoritative for its own
contents.

---

## Cue timing

Traktor stores each cue and grid marker as a `<CUE_V2>` inside `<ENTRY>`, in
**milliseconds**. VirtualDJ stores the equivalent as a `<Poi>` inside `<Song>`,
in **seconds**. Dividing by 1000 gives cues that are audibly wrong on a large
fraction of a real library — shifted by a consistent ~51.5 ms — while being
exactly right on the rest. Everything below exists to get that split correct.

### The two timelines

Every MP3 begins with encoder padding. A decoder either honours the delay
metadata and skips it, or ignores it and treats the padding as audio. Which one
happened is visible as the audio stream's **`start_time`** at the container
level, readable with `ffprobe`. Files fall into exactly two classes:

| Class | `start_time` | Correction applied |
|---|---|---|
| **A** | > `START_TIME_ZERO_TOL` (0.0005) — typically `0.025057`, also seen `0.023021`, `0.011995` | shift by `CORRECTION_OFFSET_SEC` = **0.051536 s** |
| **B** | ≤ tolerance, i.e. zero | none |
| **unknown** | unreadable or absent from the cache in `cache` mode | none — never a guess |

`classify(start_time)` in `model/cues.py` is that table. **`start_time` only
classifies; it never sizes the correction.** The magnitude is exactly
`0.051536` in every verified instance to six decimal places, across sample
rates, bitrates and encoders. It is also the constant VirtualDJ's own native NML
import applies — a natively imported cue at NML `START=0` is written as
`Pos="-0.051536"`, an unconditional subtraction in VirtualDJ's own pipeline.
After a Class A correction a "Beat Marker" cue lands exactly on its co-located
beatgrid, which is the intended musical alignment.

**Class is not derivable from either database.** Two tracks can have
byte-identical cue timestamps and need opposite treatment; the difference is in
how the audio maps to those numbers, not in the numbers. Do not re-derive the
dead ends — duration ratios, additive duration differences, the LAME/Xing
encoder-delay bytes, `<ALBUM>` presence, every other NML/XML attribute,
Serato/TRAKTOR4 private ID3 tags, audio transient analysis, and comparing stored
XML grid positions against NML ones were each implemented and disproven against
labelled real tracks. `ffprobe`'s `start_time` is the only reliable classifier
and is irreplaceable, which is why it is cached rather than avoided.

### The start-time cache

`StartTimeCache` (`model/cues.py`) is keyed by lowercased resolved path and has
three modes:

| Mode | Behaviour |
|---|---|
| `probe` | ffprobe every file, ignore the CSV. Use after re-encoding files, or when the cache is suspect. |
| `cache` | Read the CSV only, never probe. Misses become unknown — uncorrected and logged. |
| `update` | Read the CSV, probe only misses, rewrite it afterwards. Self-maintaining; the default. |

One probe costs 25–250 ms; a lookup costs microseconds. Over tens of thousands
of files that is the difference between hours and seconds, so:

- **`save()` must actually be called.** It is, from both sync paths and from
  `closeEvent`. It self-guards (`update` mode and `_dirty` only), so calling it
  is free. Remove those calls and every launch silently re-probes every track it
  writes — 28 ms each, for ever rather than once.
- **`prime(paths, workers=8)` probes the run's misses in parallel** through a
  `ThreadPoolExecutor`; ffprobe is a subprocess so the threads wait rather than
  hold the GIL. It is purely an accelerator — `get()` still probes its own
  misses, so no caller has to know it exists. A path that fails to probe stays a
  miss rather than being cached as a wrong value.
- **Priming happens inside `_flush_writes`, not before the decision loop.** The
  plan is the exact set of tracks the run will write, so nothing is probed that
  would not have been; priming earlier measures every *skipped* track too. A
  source and target on the same timeline are compared first and need no
  measurement at all.
- **Cache keys are exact lowercased paths.** Changing `audio_root_override`
  between runs invalidates every key — which shows up as everything going
  unknown in `cache` mode, or everything re-probing in `update` mode.

### What counts as a cue

`pois_from_cue_v2` reads a list of `CUE_V2` attribute dicts:

| Condition | Emits |
|---|---|
| `TYPE="4"` (grid) | a **beatgrid** Poi at `START/1000` |
| `TYPE` 0 or 4 with `HOTCUE` in 0–7 | a **cue** Poi at `START/1000`, `Num = HOTCUE + 1` |
| `TYPE="0"` with `HOTCUE="-1"` | nothing |
| `TYPE` 1 (in), 2 (out), 3 (load), 5 (loop) | nothing, even with a HOTCUE |

**One `CUE_V2` can emit both** — Traktor sometimes puts a real `HOTCUE` directly
on a `TYPE="4"` grid element (the "Beat Marker" pattern) instead of writing a
separate `TYPE="0"` at the same timestamp. Both encodings appear in real
collections, sometimes in the same entry. Collapsing the two `if`s into an
`elif` silently drops cues.

**`Num` is `HOTCUE + 1` verbatim.** Gaps in HOTCUE usage produce gaps in `Num`;
that is intended, not a numbering bug to close up.

**Only `cue` and `beatgrid` round-trip.** `OWNED_POI_TYPES` and
`OWNED_CUE_V2_TYPES` fence off everything else — VirtualDJ automix/remix points,
Traktor fade-in/out, load and loop markers. They are ignored on read so they
cannot masquerade as cues, and preserved on write so a sync never destroys them.

**A `<Poi>` with no `Pos` is at 0.0, not skipped.** VirtualDJ omits the
attribute for a point at the very start. Treating a missing `Pos` as "skip"
makes real cues invisible to the diff, so a track that has gained cues still
compares as in sync.

**A grid marker is always written with `HOTCUE="-1"`.** Because
`pois_from_cue_v2` turns a `TYPE="4"` carrying a pad slot into *both* a beatgrid
and a cue, writing a pure grid as `HOTCUE="0"` resurrects a phantom `Num=1` on
read-back and the track can never diff as in sync. A co-located grid+cue is
written as two elements — a grid with `HOTCUE="-1"` plus a `TYPE="0"` cue —
which reads back as exactly that pair.

---

## Identity and matching

**A track's identity is its path**, lowercased and entity-decoded:
`matching.path_key`. Traktor stores a location as three attributes and
`build_windows_path` reassembles them —

```xml
<LOCATION DIR="/:Music/:Funk/:Johnny Pate/:" FILE="Track.mp3" VOLUME="D:" />
```

→ `D:\Music\Funk\Johnny Pate\Track.mp3`, by pure string manipulation with no
`os.path`, so the result is identical whatever OS runs it. VirtualDJ stores the
Windows path directly. `windows_path_to_traktor_key` and
`traktor_key_to_path_key` are the inverses used by the NML reader and the
playlist writer, so the two agree on identity by construction.

### Decode entities exactly once

**Entity decoding happens once, at the point raw XML text is captured.
`matching.path_key` and `matching.traktor_key_to_path_key` never decode** — they
are handed values an XML parser has already decoded. A raw-text call site
(`traktor_write._entry_key`, `playlist_entry_keys`, `vdj_write`'s three
`FilePath` lookups, `vdj_read._read_vdjfolder`) calls `decode_entities` itself.

Getting the count wrong is invisible on ordinary text and destructive on a name
that really contains entity text. Filenames like
`Justice - Safe &amp; Sound.mp3` are common — web-escaped names baked in by a
download, 440 of them in one real library — and both databases store them
correctly as `&amp;amp;`. Decode twice and the path becomes
`Safe & Sound.mp3`, a file that is not on disk: the track reads as missing,
will not play, and the reader and the NML writer derive different keys, so a
sync cannot find the entry it already holds and appends a duplicate.

**`decode_entities` answers exactly what the parser answers.** It covers the
five predefined names *and* numeric character references (`&#39;`, `&#x27;`);
`saxutils.unescape` does neither `&apos;`/`&quot;` nor the numeric forms, and
any form the two disagree on is a key the writer cannot match. It is **one
pass** — scanning the output again would decode `&amp;#39;`, which *means* the
literal text `&#39;`, all the way to an apostrophe.

**If the same path appears in more than one entry, the last one wins.**

**Unicode normalization is not handled.** An accented character can be stored
composed (NFC, `é` = U+00E9) or decomposed (NFD, `e` + U+0301). They look
identical, but NTFS treats them as different names and `path_key` compares
exactly. Both file formats are UTF-8 with the characters written literally (no
numeric references), and **neither program normalizes**. Both hold both forms,
and VirtualDJ is not even consistent with itself. Surveyed on the real pair
(2026-10-31):

| Symptom | Count |
|---|---|
| Traktor stores NFD, the file exists only as NFC → red X, "File not found" | 159 |
| VirtualDJ `database.xml` has the NFC entry, its `.vdjfolder` lists NFD → playlist row reads as not in the collection | 81 |
| Same file, different form in each program → unmatched pair; a sync would append a duplicate | 83 |
| Stored NFD and the file really is NFD → works today | 65 |
| Missing under either form → genuinely gone | 169 + 44 NFC |

Three things make a blind `normalize("NFC", …)` on `path_key` unsafe:

- **Both forms can exist as separate files.** `Tony! Toni! Toné! - Feels Good …`
  is on disk twice, one name in each form, with different contents, and both
  are in `database.xml`. Folding the keys merges two real files, and "last one
  wins" picks one of them.
- **One program can hold both.** Traktor has two entries for one Korean title,
  an NFD one (missing) and an NFC one (present). Merging them is right, but
  last-one-wins may keep the missing one.
- **NFC is not only about accents.** It also rewrites CJK compatibility
  ideographs (U+F95C → U+6A02), so normalizing a stored path can produce a name
  that never existed. One VirtualDJ path has one.

So the disk is the authority, not a normal form. Keep the stored spelling when
it exists; when it does not, try the other form, and accept it only if exactly
one of the two exists. **Clean up's `unicode_form_path` kind does exactly
that**, rewriting stored paths to the on-disk spelling through
`repath_tracks_job`; see **Features**. Traktor's own relocate does the same
thing to a single track: it rewrites `LOCATION FILE=` and every `PRIMARYKEY` to
the disk's spelling (and drops `INFO FILESIZE`), leaving `TITLE` as it was.

Two things are **still not handled**. The integrity scan and the player compare
the recorded path exactly, so a track reads as missing until Clean up runs; and
`path_key` still compares exactly across programs.

### Text fields carry one canonical line ending

`matching.normalize_text` folds `\r\n` and bare `\r` to `\n`, and **both readers
call it on every text field they produce** (title, artist, album, genre, comment,
comment2), as does `status._norm`.

The two programs spell a line break differently and neither is wrong. Traktor
keeps the comment in an **attribute** and writes the break as the character
references `&#xD;&#xA;`, which survive XML's attribute-value normalisation and
parse back as `\r\n`. VirtualDJ keeps it in a `<Comment>` **element** as a real
CRLF, and XML normalises line ends in element text, so it parses back as `\n`.
Remove the fold and the same comment reads as two different strings,
`metadata_matches` can never say yes, the track is yellow for ever, and Overwrite
rewrites an identical value on every run — the same non-convergence
`status._lists_match` guards against for cues.

Writers hold up the other end: see **Reading and writing collections**.

### Musical keys

Traktor stores Open Key text in `INFO/@KEY` and an unambiguous numeric in
`MUSICAL_KEY VALUE`; VirtualDJ and SlipStream store musical notation. The app
uses the same shape as the cue timeline — a canonical intermediate with each
program rendering its own notation:

- `Track.key_value` carries the canonical number; **both readers fill it and
  both writers render from it.**
- `model/keys.py` converts in both directions; `musical_key()` inverts
  `parse_vdj_key` for all 24 values. Without it the Traktor→VirtualDJ direction
  has nothing to translate *with*, which is why only one direction ever looks
  broken.
- `Track.key_musical` still holds whatever each program literally stores, so it
  can be shown untranslated where that is what is wanted.
- `key` is deliberately **not** in `COMPARED_FIELDS`. VirtualDJ leaves it blank
  on the large majority of songs, so comparing it flags masses of tracks that
  are not really out of sync.

### Playlist paths, and the one name that cannot be represented

Playlist matching is **case-insensitive** (`library.path_ci`) on purpose:
Traktor's `breaks` is VirtualDJ's `Breaks` and they should sync.

But VirtualDJ stores **one file per playlist**, so on Windows two sibling
playlists whose names differ only in case resolve to the same `.vdjfolder`.
Writing both leaves only the last one — silently, with both runs reporting
success.

`Library.colliding_playlist_paths()` returns every case-folded node path held by
more than one playlist, mapped to the nodes spelling it in tree order, so a
caller can name them ("temp", "Temp") rather than say only that something
clashed. Only *siblings* clash: `A/temp` and `B/temp` are separate files.

What the app does with that:

- **The sync engine refuses both twins**, before the backup, so a playlist that
  cannot be written does not even leave a side effect behind. Refusing only the
  loser would preserve exactly the illusion that hides the problem — "the other
  one worked".
- **The sync dialog lists the pairs up front**, before anything is written.
  Renaming one is the only fix and only the user can choose which.
- **The status tree gives this no colour of its own.** Two spellings side by
  side is legal in Traktor, so marking it an error in Traktor's own pane would
  be wrong. Instead, for an ambiguous path *and only then*,
  `playlist_status` / `reference_playlist_status` require the target node's name
  to match **exactly**. The twin the target really holds reads as it always did;
  the other reads BLACK, "not there yet", which is simply true.

Without that exact-name guard, `playlist_index` resolves the tie
first-match-wins and a playlist that does not exist in the target shows another
one's rows and colours.

---

## Reading and writing collections

Both readers parse at **byte level with a regex per `<ENTRY>` / `<Song>` block**
rather than joining the whole file into one string — a 45 MB `"".join()` is a
single un-yieldable stall (see **UI behaviour**). Both take a `yield_fn` hook so
a reload can stay cooperative.

### Text surgery

Every collection write is a regex edit scoped to one entry. Two rules govern the
patterns themselves:

**A `CUE_V2` may contain a nested `<GRID>` child**, written across two lines:

```xml
<CUE_V2 … TYPE="4" HOTCUE="-1"><GRID BPM="129.098221"></GRID>
</CUE_V2>
```

`CUE_V2_RE` is `<CUE_V2\b[^>]*?(?:/>|>.*?</CUE_V2>)` with `re.DOTALL` so it
spans that. A pattern that cannot (the obvious `[^>]*/?>(?:</CUE_V2>)?`) strips
only the opening tag and leaves `<GRID …></GRID></CUE_V2>` orphaned — malformed
XML that Traktor refuses to open. **`tests/fixtures.py` contains a realistic
grid marker for this reason;** adding it made ten existing tests fail
immediately. Do not simplify it back out.

**A writer must never emit a bare carriage return.** `vdj_write._qtext` escapes
element text through `normalize_text` first, and `_qattr` escapes `\n`, `\r` and
`\t` as character references — which is what `saxutils.quoteattr` already does
on the Traktor side. Reverting either one corrupts data rather than merely
mismatching: `escape()` leaves `\r` alone and `atomic_write_text` writes through
a text-mode handle, so the following `\n` becomes `\r\n`, the pair lands as
`\r\r\n`, and the next whole-file rewrite reads that back as two newlines. The
comment gains a blank line on every second sync. A wrapped *title* has the same
shape of defect through a different door: a literal newline in an XML attribute
is legal, but the parser normalises it to a space, so the value comes back
silently altered.

**A rewrite keeps the file's own line ending.** Collections are read with
universal newlines, so content arrives holding `\n` only, and the text-mode
write used to emit the platform's `\r\n`. That was invisible for VirtualDJ
(CRLF already) but Traktor writes `collection.nml` with **LF**, so any NML
write turned all ~1.1 million line ends into CRLF. The XML stayed valid and
Traktor put LF back on its next save, but it was not the text-surgical edit the
safety rules promise. `write_queue._existing_newline` now sniffs the target
and writes the same ending; a new file still gets the platform's. A
one-track relink on a copy of the real NML now changes exactly 4 lines.

### The parse guard

`atomic_write_xml` (`io/write_queue.py`) parses the new content **before**
`os.replace` and raises `MalformedWrite` otherwise, leaving the original
byte-identical. Every collection write here is a regex edit on hand-shaped XML,
so a result that will not parse is always a bug and never worth persisting over
a user's library. `.vdjfolder` writes use the plain `atomic_write_text`.

`tests/test_nml_integrity.py` asserts on the **source** that no collection
writer uses the unguarded call, because a writer that did would pass every
behavioural test right up until it corrupted a real library.

### One read and one write per collection, per run

`LibraryAdapter.collection_session()` holds the collection in memory for the
duration of a run and writes once on the way out. Adapters reach files only
through `_load_collection` / `_store_collection`, so outside a session
everything behaves exactly as before and the metadata dialog and sidebar are
unchanged. Nesting is a no-op, which is what lets `sync_track` open a session
inside a caller that already has one. The flush is in a `finally`, so an
interrupted run still lands the work that succeeded.

Every mutation in every adapter is a pure `str -> (str, changed)` (or, for
SlipStream, a document edit), so they compose untouched: same regexes, same
order, **byte-identical output**. `tests/test_batched_writes.py` asserts exactly
that by running the same sync with sessions suppressed and comparing files.

Without the session, each track's write would do a full read-modify-write for
cues and a second for tags, and a backfill adds a third — each one re-reading
the file, re-running the regex and re-parsing the whole document to validate it.
On a real pair that is 118 reads and 118 writes of a 43 MB `database.xml` where
one and one will do.

### One walk per write kind, per run

Batching the I/O is not enough on its own: a write still has to **find** its
entry, and doing that per track means walking every entry in the document, which
is O(tracks × entries). On a 1005-track run that is 5,050,750 `_entry_matches`
calls, 46.6 s inside `re.Pattern.sub`, and 0.02 s of actual decisions.

So the writers take **many** tracks and walk once:

- **`_entry_key(body)` asks *what* an entry is**, not whether it is the one
  wanted, so a batch is one dict lookup per entry. Its patterns are compiled
  once at module level.
- `sync_tracks_into_nml` / `sync_tracks_into_xml` / `set_entries_metadata` /
  `set_songs_metadata` each do one `sub`, then append every missing entry
  together and bump `ENTRIES` once by the count.
- **The singular functions stay as one-item wrappers.** That is what keeps every
  pre-existing writer test exercising the shared edit — see the note under
  **Development conventions** about what the batching tests can and cannot
  catch.
- The engine's decision loop collects into a `_Plan` and one batched job per
  kind per file runs afterwards (`_flush_writes`). Memory is updated from the
  returned `{key: outcome}` map, only where the writer says the write landed.
- `sync_track` and `ensure_track_in_target` go through `_write_one`, the
  one-item door onto the same path, so there is no second write path.

### Locating one entry without walking the document

A walk is right for a large batch and absurd for one track: **nothing about a
single-track sync is proportional to one track**, and finding its block cost
0.61 s on a 44 MB `database.xml` (82,366 songs) and 2.38 s on a 132 MB
`collection.nml` (83,427 entries).

So for a batch of `_FAST_LOCATE_MAX` (32) or fewer, `vdj_write.locate_songs` /
`traktor_write.locate_entries` rebuild the attribute the writer would itself emit —
`FilePath=` for VirtualDJ, `FILE=` for Traktor — and find it by substring search.
Measured crossover is ~48 tracks; on a real database it located 3,000 of 3,000
sampled tracks.

**It changes only how a block is found, never how it is edited.** The hit is
re-matched with the same pattern and re-keyed with the same comparison the walk
uses, then handed to the same edit function, so the output is byte-identical —
which is what `tests/test_fast_locate.py` asserts for every shape that could
confuse the search.

Four things make it give up and walk instead, and **each one is a real defect if
removed**:

- **A miss does not prove the track is absent.** The same path has more than one
  legal encoding — a file storing `&#38;` where this app writes `&amp;` — and
  the walk decodes before comparing. Reporting "absent" here appends a duplicate
  entry for a track the collection already holds.
- **A filename is not unique.** Traktor's needle is only the `FILE=` attribute,
  so it hits every folder holding that name; the full-path key is what tells
  them apart. Skip it and one track's cues land on its namesake.
- **Two blocks may claim one path.** The walk rewrites both, so taking the first
  and stopping would leave the second holding stale cues.
- **The hit must sit in an opening tag.** `FilePath="…"` is ordinary text inside
  a `<Comment>` and needs no escaping.

**The metadata writers take the path as an optional argument** —
`set_entries_metadata(content, by_key, paths=None)`, and the same through
`write_metadata_jobs`. They are keyed by track key alone, which is a *lowercased*
path and so gives nothing to search for; the path is what lets them locate. It is
**purely an accelerator**: omit it, hand over a wrong one, or supply only part of
a batch, and the walk runs exactly as before. All-or-nothing on a batch is
deliberate — locating half and walking for the rest is two passes, which is worse
than the one it was avoiding.

This is what the `source write (backfill)` phase pays for. A backfill is the only
write the engine makes to the *source*, so on a Traktor→VirtualDJ sync it lands
on the 133.5 MB `collection.nml`. Measured: **3.29 s → 1.13 s**, taking a
backfilling track sync from 8.57 s to 6.46 s. What is left is reading the file
and rebuilding a 133.5 MB string, both inherent to a whole-file text format.

Two callers supply paths today: `SyncEngine._flush_writes` (from the plan, asking
the target library for its own spelling first, since it may store a different
case) and `_apply_metadata`, so the Edit… dialog gets it too.

**A key write is a metadata write.** It only spells the value differently per
program (Traktor `8m` plus a matching `key_value`; VirtualDJ and SlipStream
`A#m`). `LibraryAdapter.key_fields(key_value) -> dict` is that per-program rule
and `write_key_jobs` runs it through `write_metadata_jobs`, so the engine merges
tags and key into **one** call per file. Two entry points means two full walks of
a 134 MB file to set two attributes on one entry.

### Rendering a VirtualDJ playlist row

A `.vdjfolder` `<song …/>` row is VirtualDJ's cached view of the song (size,
length, BPM, key, artist, title, remix). **A row already in the playlist is
kept exactly** — `render_vdjfolder(tracks, nodup, existing)` reuses it and only
renumbers `idx`; `write_membership_job` passes the file's current text. Only a
row new to the playlist is built, from the target's own entry where it has one
(`engine._target_view`), with the file's real `size`. Until 2026-11-03 every
membership write rebuilt every row from the Track it was handed, with a
hard-coded `size="0"`, and a sync handed over the *source's* copy, so a
Traktor → VirtualDJ sync gave VirtualDJ's playlists Traktor's key, BPM and
rounded length and dropped `remix` (2,877 of 2,882 rows in "BTTB-ALL"). 459
playlist files written since August still carry zeroed rows. Tests:
`tests/test_vdjfolder_rows.py`.

### database.xml must look like VirtualDJ wrote it

On 2026-11-03 VirtualDJ said **"Database D: is corrupt"** about a
`database.xml` this app had edited (valid XML), and its repair rewrote the
whole file. Against VirtualDJ's own file ours broke every convention it keeps,
all of them this app's doing: 596 empty attributes (`Key=""`), 530
`FileSize="0"`, 73,038 songs closing at column 0 after a line holding one
space (the cue writer), whitespace runs up to 862,932 characters (removals
leaving their line behind), empty `<Comment></Comment>`, a 3-space comment
indent — and **three C: songs written into the D: database**, which
VirtualDJ's repair re-rooted onto `D:\…`. VirtualDJ keeps **one database per
drive**; that is the one rule its repair had to correct rather than reformat,
and it arrived in the same write as most of the empty attributes, so either
may have been the trigger.

Now: every writer passes the song through `vdj_write._tidy_song` (children one
per line at two spaces, ` </Song>` at one, no empty attribute, no
`FileSize="0"`, no empty comment; a block holding stray text is left alone);
`remove_song_from_xml` takes the song's whole line; a new song records its
real size or none; and `VdjAdapter.accepts_path` holds a drive's own database
(`X:\VirtualDJ\database.xml`) to that drive's songs — the engine skips the
entry (`SKIP_OTHER_DRIVE`) and still writes the playlist row. Tests:
`tests/test_vdj_database_layout.py`.

### Rendering a VirtualDJ `<Song>`

The `<Poi>` set is rebuilt wholesale from the source: every existing
`<Poi … />` line is stripped and new lines appended in source order, so stale
Pois are dropped automatically. Positions are formatted to six decimal places
and **floored at 0.0** — a correction can never produce a negative `Pos`, which
matters for cues near the very start of a Class A track. Beatgrid lines carry no
`Num`; cue lines carry `Num` and no name.

**The floor is for cues only. A grid anchor is a phase, not a place.** Flooring
it moves every beat line in the track. VirtualDJ's own analysis routinely puts
the anchor before zero (`Tim aka New T. - SWEET MISERY`: −1.588 s at 94 BPM),
and the 2026-10-31 sync floored 38 of them into Traktor — 26 by up to half a
beat. The floor also ran *halfway*, at canonical time, so even an anchor whose
Traktor value was positive was lost: Jingoism's −4.9 ms became 51.5 ms where
46.7 ms was right. Now `engine._retimed_pois` converts a grid in one step
(source offset − target offset, no intermediate floor) and
`cues.grid_anchor_at_or_after_zero` steps an anchor still below zero forward
by **whole bars** (4 beats) at the source's BPM, else the target copy's, else
it floors. `status._grids_in_phase` treats single-marker grids whose anchors
are a whole number of bars apart (at either side's BPM, ± the Class A shift) as
one grid; tempo is not compared, as it never was. Cue conversion is untouched,
and `tests/test_adapter_timeline.py` still pins it to the verified functions.
Traktor grid markers are written with a `<GRID BPM>` child taken from the
entry's `TEMPO`; without one Traktor adds `<GRID BPM="0.000000">` on save.
Tests: `tests/test_grid_phase.py`.

Repair state (2026-11-03): the **380 VirtualDJ grids floored to 0.0** on the
way in from Traktor were rewritten in place (grid `<Poi>` line only, by the
bar rule above; backup `backups/2026_11_03_111035`). The 38 Traktor grids are
fixed by a VirtualDJ→Traktor sync of those tracks.

**Play count has its own writer** (`write_playcount_jobs`, via
`vdj_write.set_songs_playcount` / `traktor_write.set_entries_playcount`). A cue
sync still carries it, but a count-only change must not re-render VirtualDJ's
Pois: `format_poi_line` floors every position, negative grids included.
`_set_playcount_nml` used to append a **second `<INFO>`** to an entry whose
INFO had no PLAYCOUNT, because Traktor writes `<INFO …></INFO>` and only a
self-closing tag was handled; it now sets the attribute on the one there.

**A playlist sync prefers the exact spelling** (`SyncEngine._find_target_node`).
Traktor held `Diggs/temp` (511) beside `Diggs/Temp` (42) and VirtualDJ only
`Temp`; the first case-insensitive match mirrored 42 tracks over the 511.

**Why playlists stay unsynced after an Update** (audit of 2026-11-03, 98 of
116 left after the repair): 644 tracks are VirtualDJ **playlist references
with no `<Song>` entry** to files missing on disk, against a Traktor entry.
Nothing can be written into an entry that does not exist, so their blank
fields and missing cues read as a difference forever; they need relinking (342
have the same filename in one other place, 141 the other Unicode spelling),
removing, or a database entry. Next come Update's own refusals (Traktor owns
the change, conflict, no history), which only Overwrite resolves. Tests:
`tests/test_playcount_and_lookup.py`.

Relinked 2026-11-03 (backup `backups/2026_11_03_121416`): 341 tracks whose file
had one other home on disk and 120 in the other Unicode spelling, in both
programs; 74 stale entries were merged into an entry already holding the new
path (none carried cues). The missing side's entry was then created by a track
sync for those and for the 141 whose file is gone, so all 602 compare equal
(the 141 stay red). 116 playlists needing sync became 53. The 79 tracks with
several copies on disk were left alone.

**Tag text compares in one Unicode spelling** (`status._norm` takes NFC, and
`SyncEngine.carried_fields` uses `_norm`, so a field called equal is never
rewritten). 60 of the relinked tracks differed only by `é` vs `e`+U+0301 in a
title or artist. Paths are still never normalised.

`cues.set_playcount` copies the NML's `<INFO PLAYCOUNT="N">` into the matched
Song's first `<Infos … />` as `PlayCount="N"` — replacing the value in place if
the attribute exists, inserting it before the closing `/>` if not, touching no
other attribute, and leaving the XML value alone when the NML has no playcount.
It is a pure metadata copy and does not interact with classification or timing.

### Backups

`io/backup.py` `snapshot()` copies `collection.nml`, `database.xml` and the
VirtualDJ `Folders/` tree into a timestamped folder under `backups/`, keeping
the newest 20 (`_KEEP`). It runs at launch, on Ctrl-S, on the autosave timer
(default off), and inside `guarded_write` before any destructive collection
change. Restore is available from the menu (`revert`, which replaces `Folders/`
wholesale).

**Every other library file `Paths` names is copied too** — `library.slp`,
`rekordbox.xml`, and whatever a new program adds as a `Paths` field — under
`files/<field>/` in the snapshot, by iterating the dataclass rather than naming
programs. `revert` puts each back wherever `Paths` names it *now*. The
start-time cache (`csv`) is excluded: it is rebuilt by probing, not restored.
Older snapshots keep their layout and still restore.

### Rekordbox

Rekordbox's live library is `master.db`, an encrypted SQLite database Pioneer
does not document. **This app never touches it.** It reads and writes
`rekordbox.xml`, the exchange format Rekordbox itself exports (File → Export
Collection in xml format) and imports (the "rekordbox xml" view). The round trip
is manual on Rekordbox's side — export before a Refresh here, import after a
sync — which is the trade the user chose over writing an undocumented database.

- **Location** is a `file://localhost/D:/…` URL inside an XML attribute: two
  layers, each undone exactly once (entities by the parser, then percent
  escapes in `location_to_path`). The writer encodes the way Rekordbox does —
  `&` as `%26`, brackets encoded, `( ) ' , ! + ~ @ $ ; =` literal — read off a
  real 21,855-track export.
- **Ours on a track**: hot cues (`<POSITION_MARK Type="0">`, `Num` 0..7, read as
  cue `Num + 1`, the numbering Traktor's reader uses) and the grid anchor (the
  single `<TEMPO>`'s `Inizio`). Memory cues (`Num="-1"`), loops, fades and load
  points are Rekordbox's: ignored on read, carried through on write. A cue on a
  pad beyond H has nowhere to go and is left out. **A grid with several
  `<TEMPO>` elements is never rewritten** — one anchor cannot describe a
  tempo-changing grid. None of the real export's tracks has more than one.
- **Keys** are written in Rekordbox's own spelling, **flats** (`Ebm`, `Bb`),
  from the canonical value; `display_key` renders the same.
- **Playlists** have no stable identifier, so they are addressed by path like
  Traktor's folders, and edited by splicing the `<NODE>` spans `scan_nodes`
  finds. `Count` (child nodes) and `Entries` (members) are kept true on every
  edit — Rekordbox reads them. A playlist keeps its own `KeyType`: TrackID
  references follow the entry on a repath, Location references are rewritten.
  A new member the collection lacks gets an entry first, since a TrackID
  reference needs one.
- **The timeline is uncalibrated.** `timeline_offset` returns 0: stored seconds
  are taken as true audio time until measured. Whether Rekordbox honours an
  MP3's encoder delay is exactly what the Traktor work had to *measure* (see
  **Cue timing**); expect a Class-A-style split until it has been. Calibrate
  with a handful of tracks where one cue sits on a sharp transient in every
  program.

Verified against a copy of the real export: rewriting 500 random tracks' own
points read back identical, the other 21,355 tracks and the whole playlist
section stayed byte-identical, and the file still parsed.

### Serato

Three stores the adapter ties together (`adapters/serato.py`):

- **`database V2`** (`io/serato_db.py`): a flat run of records — 4-byte tag,
  big-endian u32 length, payload; `t`/`p` tags are UTF-16BE. Tracks are `otrk`
  records holding `pfil` (path), `tsng`, `tart`, `talb`, `tgen`, `tcom`, `tbpm`,
  `tkey` and ~30 fields this app ignores. **A record not being edited is
  re-emitted byte-for-byte**, verified on the real 21,474-record database.
- **Crates** are `Subcrates\A%%B%%C.crate`, the tree spelled in the file name;
  a parent with no file of its own is still a folder. `neworder.pref` (UTF-16BE
  `[crate]A%%B` lines) is the sidebar order, kept in step on create, rename,
  move and remove. **Serato has no empty folders**: a new folder is an empty
  crate and reads back as an empty list until something is put inside it
  (`LibraryAdapter.holds_empty_folders = False`, which narrows the one contract
  test that assumed otherwise).
- **Paths are relative to the root of the database's drive** (`Music/x.mp3` in
  `D:\Music\_Serato_` is `D:\Music\x.mp3`). A file on another drive has no
  spelling there — Serato keeps one `_Serato_` per drive — and is refused, not
  written wrong. This is why the adapter contract's fake music folder lives on
  the temp drive.

**Cues live in the audio files** (`io/serato_tags.py`), the user's explicit
choice (2026-10-31): `Serato Markers2` (base64 of entries; a `CUE` is pad index,
u32 milliseconds, colour, name) and `Serato BeatGrid` (float32 position, beat
count or BPM, one footer byte). Pad A is cue 1, as everywhere. Rules, each
pinned in `tests/test_serato.py` and checked on copies of 150 real files:

- **Only those two frames change.** The edit is made on a copy beside the
  original and swapped in with `os.replace` only after verifying the points
  landed and every other frame is unchanged. For an MP3 only the leading ID3v2
  tag comes from mutagen; **every byte after it comes from the original file**,
  because mutagen regenerates a trailing ID3v1 tag on save — 36 of 60 sampled
  real files carry one, and the first version of this writer refused all 36.
- **The previous frames are saved first**, one JSON line per write in
  `backups/serato_tags/serato_tags_<date>.jsonl`; `serato_tags.restore()`
  replays the oldest entry per file through the same verified path.
- **The base64 is Serato's own**: `=` stripped, one `A` appended unless the
  payload is 1 mod 3, a newline after every full 72-character line, NUL-padded
  to 470. Established by re-encoding every Serato-written frame in a real
  library byte-for-byte. A converter's style (`=` kept, an extra NUL encoded,
  trailing newline) is read too.
- **A pad keeps its colour and name** when its position moves; the shared model
  has no slot for either. `COLOR`, `BPMLOCK`, `LOOP`, `FLIP` are carried
  through. **A grid with several markers is never flattened.**
- **The legacy `Serato Markers_` frame is left untouched.** Serato reads
  `Markers2` when present; verify on a few tracks in Serato before trusting a
  large sync.
- **Only ID3 containers are written**: MP3, AIFF, WAV (97% of the real
  library). FLAC, M4A and Ogg read as having no cues, so a track in one with
  cues elsewhere stays yellow.

**Reading cues means opening every audio file** — 141 s for 21,473 on the real
library the first time. `serato_tags.CueCache` (`serato_cues.json` beside the
app) keeps each file's points against its size and modification time, so a
later load re-reads only changed files: 2.4 s. `read_tracks` bypasses the cache,
so a targeted refresh describes the file as it is now.

**The timeline is uncalibrated**, as Rekordbox's: `timeline_offset` is 0. The
real library's Serato cues equal Traktor's to the millisecond on 17,681 pads —
a converter copied them unchanged — which says what that converter believed,
not what Serato does.

Separately, `make_backup` copies **one** collection beside itself before a
*playlist* sync rewrites it. A **track sync takes no such copy**: it touches one
entry, and a 44 MB copy per single-track sync grew the user's VirtualDJ folder
without bound. The trade is that a track sync is recoverable to the session's
starting state rather than to the instant before that one write.

---

## Sync

Three modes (`sync/engine.py`): **New** (`NEW`) adds missing tracks and touches
nothing else; **Update** (`UPDATE`) writes only what the source is judged to own;
**Overwrite** (`OVERWRITE`) writes regardless of attribution. `SyncEngine` names
no program and no file format — every write goes through the target's adapter and
every position converts source → native → target.

### What is compared, what is written

- `COMPARED_FIELDS` = title, artist, album, genre, comment, comment2 — what the
  diff calls a metadata difference.
- `EDITABLE_FIELDS` (per adapter) = title, artist, album, genre, comment — what
  a program can actually be made to store.
- `SyncEngine.syncable_fields(source, target)` is the intersection of the pair's
  editable fields with `COMPARED_FIELDS`, and is what the **write gate** uses.

**Anything compared but not writable is a state with no way out.** `comment2` is
Traktor-only and VirtualDJ has nowhere to put one, so a track differing only
there reads as unsynced — but `syncable_fields` keeps the gate from rewriting it
on every run trying to converge. Widening `COMPARED_FIELDS` without widening
`EDITABLE_FIELDS` reintroduces permanent yellow; dropping `comment2` from
`COMPARED_FIELDS` is the one-line change if it ever bites.

The write gate is `content_matches` — **cues *and* metadata**. Gating on cue
equivalence alone short-circuits a metadata-only difference to "already in sync"
before the mode branches are ever reached, Overwrite included.

### Blank means "no information", not "make it empty"

`SyncEngine.carried_fields` returns both directions from one comparison, so the
gate and the write cannot disagree:

- A populated source field goes to the target.
- A field **blank on the source and populated on the target is dropped**, never
  written over the target. VirtualDJ carries no comment for most tracks, so
  without this a sync from it turns Traktor's `COMMENT="Jazz"` into `COMMENT=""`.
- A field **blank on the source is backfilled from the target** — the only write
  this engine makes to the *source* library. It needs no attribution guard
  because it can only add a value where there was none. It runs in Update and
  Overwrite only.

Backfill is what stops the two rules above from deadlocking. Without it the
blank-never-overwrites rule makes `content_in_sync` answer "nothing to do" while
`metadata_matches` still counts the one-sided blank as a difference: the engine
will not act and the square cannot clear. `model/status.py` is not involved —
green is reached by the two sides genuinely converging, not by lowering the bar.

`carried_key()` follows the same shape and **only ever fills a blank side**. Two
programs that both hold a key are left alone even when they disagree: the app
cannot settle which analysis is right, and the spellings differ harmlessly, so
"converging" a track VirtualDJ spells `Ebm` would rewrite it to `D#m`, the same
note. The key is analysis output, so it stays out of `EDITABLE_FIELDS` and the
metadata dialog cannot reach it — but a sync can.

### Attribution

`model/baseline.py` fingerprints each side and compares it against **its own**
previous fingerprint; Traktor's and VirtualDJ's "last modified" clocks are
untrustworthy, so a timestamp is never the signal. Fingerprints are scoped
**per program pair** (`Baseline.for_pair(a, b)`) — attribution only means
something relative to something else — and a pre-pair CSV is read as the
VirtualDJ↔Traktor pair so upgrading keeps its history.

`attribute()` distinguishes three answers, and the distinction matters:

| Verdict | Meaning |
|---|---|
| one side | that side owns the change; the other is behind |
| `BOTH` | a genuine conflict — both moved since they last agreed |
| `UNKNOWN` | **no history at all** — the two have never been diffed |

Returning `BOTH` for the no-history case makes Update decline everything on a
library the app has not seen before, citing "the other side owns the change"
about tracks the other side never touched — and because the verdict is then
*stored*, every later refresh finds "nothing moved", keeps it, and it sticks for
ever. Update still declines `UNKNOWN` (the guarantee is worth more than the
convenience), but `SyncResult.skipped_by_reason` and the summary dialog name
which of the three applied. Glyphs are unaffected: `merge_owner` folds `unknown`
exactly like `both`, since with no evidence either way both sides must read as
possibly changed.

**A pair with no verdicts skips rather than falling back to timestamps.**

**An edit records its own verdict as it happens.** `_apply_metadata` calls
`baseline.record_edit` and pushes the result into the live engine's `owners`.
Without that, a track that was in sync before the edit is *absent* from the
owners map — `attribute_changes` omits in-sync keys — so `pair_owners.get(key)`
answers `None` and Update skips exactly the tracks you just edited, while
Overwrite works because it bypasses the guard. The editor's own fingerprint is
deliberately left stale so the next real diff still sees that side moved, and
the guard still bites in the other direction: syncing *into* the program holding
the edit finds `owner != source` and skips.

### Memory never claims more than disk

Every `_edit`-style job returns **whether it rewrote the file**, and
`_apply_written` (called per planned track from `_flush_writes`) mutates the
in-memory copies only for what was really written, recording the rest on `SyncResult.unwritten_tags`. A job that reports
nothing makes "no matching entry" indistinguishable from success: two in-memory
tracks then agree, `content_matches` says green, and a Refresh re-diffs *memory*
so the lie survives it. Pinned for every adapter — the `FakeAdapter` included —
in `tests/test_adapter_contract.py`.

### One run per selection, not one per track

`sync_tracks(keys, source, target)` is the real entry point for a track sync;
`sync_track` is its one-item wrapper, the same pattern the writers use. A run
opens a collection session per program and flushes it on the way out, and that
flush re-parses and rewrites the whole file — so looping the singular over a
selection pays it once per track. Measured on the real pair, 10 tracks:
**24.48 s looped against 6.60 s planned together.**

The track menu follows the rule Edit and Remove already follow — **the whole
selection when the clicked row is part of it, else that row alone**, since
right-click deliberately does not move the selection. `_selected_track_keys` is
the single definition; `_edit_targets` resolves it against a library. Sync Track
had its own copy that ignored the selection entirely, which is why choosing it on
a multi-selection wrote only the row that was clicked.

### What a run refuses

- **A playlist whose path collides with a sibling's under case folding**, both
  twins, before the backup. See **Identity and matching**.
- **A field the target cannot store** — it is excluded from the write gate by
  `syncable_fields`, so the track is left alone rather than rewritten for ever.
- **An Update whose attribution says the other side owns the change**, or says
  nothing at all.

---

## Status

Shape says **who** changed; colour says **what**. Everything is computed against
the **reference program** chosen in `Programs → Compare against`.

| Glyph | Meaning |
|---|---|
| green **square** | synced — matches the reference |
| yellow **triangle** | this copy changed (self reference) |
| yellow **square** | the reference changed; this copy is behind it |
| yellow **fork** (inverted tuning fork) | another, non-reference program changed |
| black **square** | not in the reference yet |
| red **X** | error — a track could not be found |

**Green means "matches the reference", and nothing else.** A copy that differs
is never green, whoever made the change. Returning green on the un-edited side
lets a playlist show "synced" while its own rows display deep-green incoming
tracks. Every program is painted, the reference included; it shows a triangle or
a fork but never a square, since it cannot be behind itself.

`changed_glyph(viewer, reference, changed_by)` is the **single** rule deciding
which yellow anything shows, used by the diff *and* by the optimistic overlay so
they cannot disagree. `changed_by` must be aggregated across **every** pair by
the caller: one pair only names its own two members, so it can never report that
a third program is the one that moved.

**A yellow playlist square means a track in it is not green** — membership and
order can match perfectly and one differing track is enough. That is correct, and
it is also invisible: a single yellow row among 517 is not findable by scrolling,
which is exactly how a correct square came to be reported as a bug. The status
bar therefore says how many (`ViewController.out_of_sync_count`), and clicking
the status column sorts them together.

`tests/test_playlist_status.py` pins the reported shape — identical membership
and order, one track whose cue sits 12 ms off where the Class-A correction would
put it — so the tempting "fix" of widening the tolerance until it reads green
fails loudly. A track that is off by *exactly* the correction is in sync and has
its own test beside it.

**A field that is compared but cannot converge is a defect, not a display
quirk.** Anything the diff counts as a difference must be reachable by a write
in at least one direction, or the square is permanently yellow and every sync
rewrites it. This is the invariant behind `syncable_fields`, `carried_fields`
and `normalize_text` alike.

**Divergence markers land on the un-edited copy.** `playlist_divergence` derives
them purely from the last-agreed membership snapshot: **grey = a track this copy
will lose**, **deep-green = a track it will gain**. The edited side always shows
clean. Showing "removed" rows on the side that made the edit is the inverse and
reads as a bug to anyone using it.

**Reference rows show no per-row preview markers once three or more programs are
loaded** — whose change they meant would be a guess. The reference's status
*glyph* is still shown. At two programs everything is as it was.

---

## Diagnostics

The fixtures cannot express several real failure modes — no `<CUE_V2>` with a
nested `<GRID>`, no attribution history, a handful of tracks — so a class of
defect is invisible to the suite and can only be caught by recording what
happened on a real collection.

**Debug → the sync-log toggles** (`io/synclog.py`) write **one file per
run** into `<app_dir>/logs/`, named
`sync_update_2026-07-31_0142-33_Zero-To-Break-Chill.log`: the timestamp carries
seconds so two runs a moment apart cannot collide, and sorts chronologically as
plain text. A run is the unit you would send to someone or diff against another
run.

What a log records:

- the per-track write decision **and the branch that produced it** — "skipped" is
  what the user can already see; *which* branch skipped it is the answer
- the attribution verdict, reported as **`ever_agreed`** — whether the baseline
  actually holds fingerprints for both sides, which is what attribution reasons
  from. Reporting `has_verdict` instead reads as "the owner is known", which is
  true from the first diff onward and therefore says nothing.
- a field-by-field metadata comparison, with `<blank>` shown for an empty value
  so blank is visible rather than looking like a missing column
- what the writers actually answered
- playlist membership before and after, **re-read from disk through the
  adapter** — the in-memory library is not reloaded until the window does it, so
  asking it reports nonsense like "1005 missing" about a playlist that was just
  written in full
- a parse check of the target file before and after — **only with
  `Debug → Verify collection parses`, which is off by default.** Two
  whole-document `ET.parse` calls cost 1.13 s each on a 44 MB `database.xml`,
  which was 2.3 s of a 5 s single-track sync. `atomic_write_xml` already parses
  the exact string it stores and refuses malformed output, so the *after* check
  re-proves a guarantee that already holds; the *before* check answers "was the
  file already broken when this run started", which is what you switch it on
  for.

Three rules this module lives by:

**`NullLog` is a null object, not a flag.** The engine has no `if logging`
branches, so a sync behaves identically either way — pinned in
`tests/test_synclog.py`. A missing method on the null sink would crash every
ordinary sync.

**Logging never changes an outcome.** Every event call is wrapped and failures
are swallowed. A diagnostic that breaks the thing it is diagnosing is worse than
no diagnostic.

**A diagnostic must not be the cost it is measuring.** Reloading the baseline CSV
per track inside `_ever_agreed` costs 68 s on a 165-track run that writes
nothing. `_begin_run` clears the cache and `_baseline_store` loads it once per
**run** — per run, not per session, so an edit between two syncs is still seen.
`baseline_loads` is asserted in `tests/test_synclog.py` because this is a
performance contract nothing else would catch.

### Building and writing a log

**Each *occurrence* of a track reserves its own outcome line.** `_plan_track`
appends to `plan.slots.setdefault(key, [])` — a **list** per key — and
`_flush_writes` fills every slot for a key, not just the last. Storing one index
per track key means a playlist holding the same track twice keeps only the second
reservation; the first stays at the `_RESERVED` sentinel. 329 playlists in one
real library hold a repeated track, so this is common rather than exotic.

**The text is built in full before the file is opened**, then stored through
`atomic_write_text` and encoded with `backslashreplace`. Opening the real path
first truncates it, so any failure after the `open` destroys the file and leaves
0 bytes.

**A line nobody filled is dropped**, which is what `fill` documents. Rendering it
instead puts `<object object at 0x…>` in the middle of an otherwise good log —
trading a lost file for garbage in a kept one.

**A log that could not be written is named in the sync report.** `_finish_log`
records the failure on `self._log_failures` and `_logged_note` drains it into a
"Debug log could NOT be written (the sync itself completed)" block. Swallowing
the exception and returning `""` leaves the app reporting "Sync complete" beside
an empty file.

**The folder-sync loop catches `Exception`, not `OSError`.** Catching only
`OSError` lets one bad playlist abandon every playlist after it.

### Timing

`Debug → Log sync performance` adds a Performance section to whichever logs are
already on: wall time per phase, per-track totals worst-first, and counts of
whole-file collection reads and writes. Those counters live on the adapters
(`collection_reads` / `collection_writes`, incremented in the two methods that
really touch a file), so the number stays true however the writes are arranged.
`Timings.walks` counts passes over a whole document — one per write kind per run
is the contract, one per track is the regression it exists to catch.

**The phases add up to the total, exactly.** `Timings.stop()` freezes the total
in `_end_run` and the leftover is charged to a `decision loop` phase read off the
same instant. Rendering `elapsed()` at log-write time instead charges the log's
own after-work — including the re-read that checks the collection still parses,
about 6.5 s on a 52 MB library — to a sync that had already finished. The backup,
the parse guard and the session flush are phases of their own
(`collections.close()` is called explicitly inside one rather than left to the
`with`), so the next unphased cost shows up as a suspiciously large
`decision loop` rather than as a total nobody can account for.

`tests/test_probe_and_timing.py` counts calls to `_ffprobe_start_time`, which is
the only thing that would notice the number of measurements going back up.

---

## UI behaviour

These are guarantees the UI is expected to hold. Each has a mechanism that is
easy to undo by accident.

### A refresh or sync keeps audio playing

The parse+diff runs **cooperatively on the main thread**, yielding to the event
loop every ~128 items via
`QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)` — which services
audio and paint while excluding user input, so there is no re-entrancy. Moving
it to a worker thread does *not* work: CPython's GIL means the parsing thread
still starves the main-thread audio pull (`readData` runs on the main thread,
feeding a deliberately tiny **20 ms** buffer). Lowering `sys.setswitchinterval`
hands the interpreter back more often but slows the parse and *lengthens* the
crackle.

Three things keep the residual stalls under the buffer:

- `gc.disable()` for the duration of the reload — building ~80 k objects
  otherwise triggers ~100 ms cyclic-GC pauses.
- **byte-level regex readers** that decode each `<Song>` / `<ENTRY>` block
  individually, instead of one 45 MB `"".join()`.
- `setResizeContentsPrecision(24)` instead of `resizeColumnsToContents()`
  scanning 1000 rows, and `yield_fn` hooks through the status-diff loops.

Measured on a real 81 k-track library: average main-thread work-gap ~3 ms, p99
under 10 ms. The trade-off is that the UI is briefly non-interactive during a
refresh, but never silent. See `ui/main_window.py` `_reload_preserving_view` /
`_reload_yield`, and the `yield_fn` parameters threaded through `io/*_read.py`,
`model/status.py`, `ui/playlist_trees.py`.

A refresh also runs the **file-integrity scan over every loaded collection**
before installing the diff, so a track whose file has gone never comes back
green. A track whose audio will not load is recorded in `_missing_keys` like any
other integrity finding and shows the red X everywhere until the next check.

### A refresh is a last resort, not the way to reflect an edit

`_reload_preserving_view` throws away the app's whole picture of every library
and rebuilds it from disk. On a real pair — 133.5 MB `collection.nml`, 44.3 MB
`database.xml`, 165,794 tracks — that is about **30 seconds**:

| | |
|---|---|
| re-parse `collection.nml` | 10.96 s |
| re-parse `database.xml` + `Folders/` | 5.39 s |
| two full status diffs | 1.97 s |
| integrity scan — 165,794 `os.path.exists` | 6.90 s |
| plus attribution and two `baseline.save()` per pair | |

It is unconditionally correct, which is why it was the default: nothing can
drift, because the picture *is* the files, freshly read. It is also the entire
reason a one-track sync took 30–40 seconds while the sync itself took 3.3.

**An operation that touched a known set of tracks calls `_restatus_tracks`
instead.** That asks each program to `read_tracks` just those entries, re-diffs
only those keys against the reference, maintains their ownership verdicts, and
repaints. Measured on the same libraries: **29.97 s → 4.47 s**, painting the
identical colour.

The rule it preserves is the one the reload existed to defend: **the colour
comes from disk.** The entries are read *back* rather than the in-memory copy
believed, so a write that did not land cannot show as green. Four things follow
from that and are each pinned by a test:

- **both sides are re-read**, not just the target — a sync backfills into the
  source too;
- **a created entry** is read back like any other, so "absent from the result"
  means absent from the collection;
- **a verdict that no longer applies is removed**, not merely overwritten —
  `attribute_changes` omits keys that agree, so an update that only ever added
  would leave the old owner standing for ever;
- **a read that fails falls back to the full reload**, because a targeted
  refresh that cannot complete must not leave a stale colour behind.

What it does **not** do is notice edits made outside this app. Neither did the
reload, except by accident — nothing watches the files. Refresh remains the
answer for that, and still re-reads everything.

Today only the track sync uses it. Playlist and folder sync still reload.

### Selection, scroll and tab contents survive a refresh

- **Selection is restored by key, not by row.** `set_rows` resets the model, so
  nothing survives on its own. `populate_table` captures `selected_keys()` and
  reselects on the same deferred tick the scroll restore uses. A sync consumes
  preview rows and reorders what is left, so the row a track sat on means
  something else afterwards; a key that is gone is dropped, never approximated
  onto whatever took its place.
- **Scroll position is remembered as the top visible *track*, not a pixel
  offset** — a sync invalidates the offset by consuming preview rows.
- **The scroll restore is deferred to the next event-loop tick**
  (`QTimer.singleShot(0)`). Restoring synchronously right after `set_rows`'s
  model reset is unreliable because the view has not recomputed its range, which
  shows up as a reorder snapping the list to the top.
- **`LibraryTree.load()` blocks signals.** A reload rebuilds the sidebar, and
  `QTreeWidget.clear()` fires `itemSelectionChanged` → `_on_selection` emits
  `playlists_selected([])` → `set_context(source, [])` wipes the focused tab's
  `nodes` → `_refresh_all_tabs` then skips it (`if not table.nodes`) and the tab
  goes blank.
- **A multi-selection is restored whole.** `select_paths` and `select_nodes`
  move the current row with `QItemSelectionModel.NoUpdate`. A plain
  `setCurrentItem` is ClearAndSelect, so the restore after every refresh and
  sync used to keep only the last selected playlist; a second sync of "the
  selection" then did one. Pinned in `tests/test_sync_pane.py`.

### Pane state comes from the pane

`ViewController.on_tab_focus_changed` sets `win._current_source` — before the
"exactly one node" guard, which is about which row to highlight, not which
program is current. `_on_track_context` takes its source from
`self._table().source`, the pane that drew the row, rather than from the window
at all.

Anything that asks the *window* which program is current gets a stale answer if
`_current_source` is not moved: Left/Right keep walking the previous program's
tree, the Playlists… popup lists one program's playlists only, and a right-click
menu can answer for the wrong program.

### The track table

- **The default view is the playlist's own order.** `setSortingEnabled` leaves
  the proxy sorting on column 0 *descending*, which orders every table by status
  and floats preview rows above real members. Sorting stays off until a header is
  clicked (`_user_sorted`), and a chosen sort survives a repopulate.
- **Native-order detection is a subsequence test with a key-based insert
  index.** An exact `visual_keys() == track_keys` compare fails on any playlist
  holding a duplicate or an unresolved track, because the row builder dedupes
  and drops those — and a failed compare means positional insert silently
  degrades to append. Preview rows are not in `track_keys` either, which is the
  other reason the test has to be a subsequence rather than an equality.
- **Every drop handler tests `TRACK_MIME` first**, before `text/uri-list`. The
  drag carries both so players and Explorer accept it; flip the order and an
  internal drag between playlists silently becomes a file re-import. Pinned by
  `tests/test_track_drag.py`.
- **Sync-preview rows offer no removal.** A deep-green "gaining" row is a track
  the playlist does not contain yet, so Remove/Delete are absent from its menu
  entirely rather than disabled. They are not editable either.
- **A gaining row is brought in, not synced from.** When this program has no
  collection entry for the track, Sync Track is greyed out ("not in this
  program's collection") — it used to answer "Track not found in source
  library", reported on a FLAC only VirtualDJ's "2021 Walrus Diggs" held. The
  row offers "Add to this playlist from <program>" instead
  (`MainWindow._bring_track_in`), which reuses the cross-program drop
  (`membership.add_tracks` with that program as the drag source): confirm,
  create the entry from it, append. `_sync_track` also leaves out selected
  tracks with no entry here and says so, rather than syncing an empty
  playlist-only reference. Tests: `tests/test_track_context.py`.
- **The Key column renders `LibraryAdapter.display_key()`**, resolved through
  `adapters.adapter_class(track.source)` in `ui/fields.py`. `TraktorAdapter`
  overrides it to render from `Track.key_value`, falling back to the stored text
  for a track with no analysed key — because Traktor itself displays from the
  analysed number, and a handful of entries in any real collection hold musical
  notation or nothing at all in `INFO/@KEY`.

### Click-to-edit

Clicking a text column (Title/Artist/Album/Genre/Comment) opens an in-place
editor **only when the pane already had focus and the row was already selected**.
The click that focuses a pane, or that selects a track, never opens an editor.

**Both "already" states are captured in `mousePressEvent`, before `super()`
runs.** `clicked` is emitted on *release*, by which point Qt has already moved
the selection onto the row you hit — so asking the live selection there is true
for every click and the first click on any row opens an editor.

The edit is **deferred by the double-click interval** and cancelled if a second
click lands, so a double-click plays without also opening an editor. It is
cancelled too by a click elsewhere, by focus loss and by a repopulate; the
pending index is a `QPersistentModelIndex` so a refresh mid-wait cannot open an
editor on whatever row slid into that position. Qt emits `clicked` **again** on
the release after a double-click, so the trailing one is explicitly skipped.

**`editTriggers` is `NoEditTriggers`.** Qt's defaults
(`DoubleClicked | EditKeyPressed | AnyKeyPressed`) are a second, independent way
into editing from inside Qt's own press handling, whatever the deliberate path
does. `edit(index)` is a slot and ignores the setting, so the deliberate path
still works.

### Keyboard

- **The arrows are routed application-wide and split by axis**: Left/Right step
  the sidebar's playlists, Up/Down move the track list, from anywhere whatever
  has focus. Handling them in whichever view has focus leaves Left/Right dead
  after a sidebar click (focus is in the tree, which never handles them) while
  `QTreeWidget` moves the *playlist* selection on Up/Down. **Consuming the events
  is the mechanism, not a side effect** — it is what stops the tree navigating.
  With nothing selected in the table, either vertical arrow lands on the top row.
- **`ARROW_OWNING_WIDGETS` is text entry only** (`QLineEdit`, `QComboBox`).
  Adding sliders breaks everything: Qt hands focus to the tempo fader as soon as
  the window is shown, so the arrows navigate nothing until something else is
  clicked.
- **A double tap on a folder** expands (Right) or collapses (Left) it. On a
  folder the step **waits out the double-tap window** rather than moving at once;
  other rows step with no delay. Stepping first makes the gesture unreachable by
  construction — the second tap lands on whatever row the first moved to. The
  opposite arrow cancels a pending step rather than performing it. One detector
  on the window, not one per pane, since the arrows are application-wide.
- **F6 moves focus between split track panes**, and does nothing when unsplit.
- **The transport filter ignores Ctrl/Alt/Meta.** Claiming the bare digits and
  "w" regardless of modifiers swallows Ctrl+W and Ctrl+1–8 application-wide, so
  any shortcut bound to those never fires while quietly toggling playback.

### Assignable shortcuts

`Controls → Configure shortcuts…`, persisted to `userControls.ini` beside the
settings JSON (`config/controls.py`, `ui/controls_dialog.py`).

- **Stable action *ids* are the contract.** Labels may be reworded; ids may not.
  Unknown ids from a future build are ignored, as `enabled_programs` already
  does.
- **Conflicts are surfaced live** — two actions on one chord means one silently
  never fires.
- **The capture field is `ShortcutEdit`, not `QKeySequenceEdit`.** Tab never
  reaches a `QKeySequenceEdit`: `QWidget.event` turns Tab into a focus change
  before any subclass sees it, so the override takes key events in `event()`
  rather than `keyPressEvent`. And `QKeySequence` has no representation for a
  mouse button at all.
- **Mouse buttons are bindable**, stored as `Mouse:Middle` / `Ctrl+Mouse:Left` in
  the same string field so `conflicts()` covers them with no special case.
  Middle/Back/Forward bind bare; **Left and Right require a modifier** and warn
  if clicked without one — both do too much elsewhere, and a bare left click is
  how you focus the field you would be binding it in. `parse_mouse_binding`
  refuses a bare `Mouse:Left` even from a hand-edited file.
- `QShortcut` cannot fire on a mouse button, so these dispatch from the same
  application-wide filter as the transport keys, **matching modifiers exactly**.
- **Assigning freezes the app's shortcuts** by accepting
  `QEvent.ShortcutOverride` — Qt's own "these keys are mine" mechanism, so
  nothing has to be disabled and restored by hand and no path can leak an enabled
  shortcut.
- **Gesture timings live in a `[timing]` section of the same file**, with two
  sliders: the mouse **double-click** and the keyboard **double-tap**. Separate
  on purpose — a comfortable mouse speed is not a comfortable key-repeat speed.
  The double-click slider sets `QApplication.setDoubleClickInterval`, not just
  this app's tables, so the deferred click-to-edit and Qt itself cannot disagree
  about how long a double-click is. **`double_click_ms = 0` means follow the
  OS**; overriding Windows' own double-click time at startup would be a change
  nobody asked for.

### Appearance

- **Watermarks are composited over the rows, not behind them.** The table's
  alternating row colours are opaque, so anything painted underneath survives
  only in the strip below the last row. Pixmaps cache per (file, size, opacity)
  with the alpha baked in, because a repaint happens on every scroll.
- **Each adapter names its own logo file** (`LibraryAdapter.logo_file`). The
  supplied art does not follow the adapter ids — `vdjl.png` names no program — so
  the mismatch is plain data rather than a lookup table with a special case. The
  mapping is pinned by **sampling each image's mean opaque colour**, not by
  trusting filenames: a logo on the wrong pane would ship quietly.
- **Tabs size to their own text**, clamped 72–220 px and eliding beyond. A
  crowded strip narrows toward the minimum *before* Qt shows scroll arrows, which
  hide tabs outright. Qt's own size hint cannot be used — this bar paints its
  text and "×" itself, with padding the default hint knows nothing about.
- **Sidebar section heights are learned only once the pane is on screen.**
  Before `show()` Qt reports placeholder heights (76 px, under a section's
  91 px minimum), and adopting those as the remembered weights left every
  section but the last at its minimum — visible as soon as five programs were
  registered, even with two switched on. When remembered heights do not fit,
  `_relayout` shrinks the expanded sections in proportion rather than taking
  the whole shortfall from the bottom one.
- **A missing or unreadable image is nothing to draw, never an exception**,
  everywhere. The artwork is loose files in `Images/` and any of it can be absent
  at runtime.
- `ui/status_squares.py` draws square / triangle / fork / X from the status id;
  **unknown ids fall back to a square in their own colour**, so a new marker
  works without being registered. `PLAYLIST_LEGEND` and `TRACK_LEGEND` there
  are the Help → Status symbols tab, drawn by `status_pixmap` in both skins so
  the legend is the real artwork. A new status needs a legend row:
  `tests/test_help_controls.py` fails on any drawn status Help does not explain.

---

## Development conventions

**Resolve redirectable paths through their module, never a from-import.**
`from ..config.paths import baseline_path` binds the function object at import
time, so a test's `monkeypatch.setattr(paths_mod, "baseline_path", …)` patches a
name nothing looks at. Use `from ..config import paths as paths_mod` and call
`paths_mod.baseline_path()`.

This is not a style preference. `sync_baseline.csv` is the change-attribution
history — it decides whether an Update sync overwrites a track or skips it — and
a bound-around redirect means every windowed test reads and rewrites the
developer's real one. The tests look isolated and are not.
`tests/test_reference_program.py` pins the lookup itself.

**A `MainWindow` test fixture must redirect `settings_path`, `baseline_path`,
`backups_dir` and `app_dir`.** `close()` calls `settings.save()`, which otherwise
rewrites the developer's real `trackcleaner_settings.json` with pytest temp
paths on every run.

**The suite never asks the machine what is running.** `tests/conftest.py`
answers "no DJ program is open" for every test, through
`io/vdj_process.is_any_running` — so callers must reach it through the module,
not a from-import. With VirtualDJ open, a dozen sync tests had their sync
cancelled by the confirmation dialog. A test about that warning patches
`is_running` itself. (The same session found the real check had never matched:
it looked for `"VirtualDJ.exe"` case-sensitively while `tasklist` reported
`virtualdj.exe`, so writing `database.xml` never warned. It now compares
case-insensitively.)

**MIME-decode logic lives in static, `QMimeData`-testable seams.**
Constructing a `QDropEvent` by hand segfaults this PySide6 build, so
`TrackTable._parse_drop` and `TrackTable._parse_urls` are tested directly and
drop *wiring* is covered separately. A synthetic `QMouseEvent` is fine — the
tab-close and right-click tests use one.

**Reproduce a UI bug through the real user path.** Driving a pane by calling
`_on_playlists_selected` directly leaves the sidebar tree with no selection,
which is precisely the condition several tab and selection defects depend on.
Select through the tree.

**Fixtures must carry the shapes real files have.** `tests/fixtures.py` holds a
`<CUE_V2>` with a nested `<GRID>` child because every real collection has one and
a self-contained single-line cue exercises nothing. Note also that **`SAMPLE_NML`
carries real paths from the machine it was captured on** — a test asking "does
this track's file exist" against the fixture is testing that developer's disk,
not the code. Arrange the filesystem state you mean, in both directions.

**A test that compares two paths through the same code cannot catch a bug in
that code.** The byte-identity test "N single writes == one batched write"
compares two arrangements of the *same* edit, so breaking the entry edit leaves
it green — it is `tests/test_cue_roundtrip.py`'s cue-deletion test that fails
instead. This is why the singular writer functions are kept as one-item wrappers:
the batching tests cover the batching layer (grouping, appending, counting) and
the pre-existing writer tests cover the edit. Both are needed.

**Adding a program? Start with `tests/test_adapter_contract.py`.** It is
parametrised over every entry in `ADAPTER_CLASSES`, so it states once, for all
programs, what an adapter must do: reads produce a `Library` tagged with the
adapter id; node paths round-trip through create/rename/move/remove; membership
is rewritten to exactly what was asked for; removing a playlist never touches the
collection; every write returns a job rather than performing one; and memory
never claims more than disk. A new program is correct when that file passes.
Format-specific behaviour goes in its own module (`test_native_slp.py`,
`test_adapter_timeline.py`, the existing NML/`.vdjfolder` writer tests).

**Layout and section tests assert by position** (top / middle / bottom-most
expanded) and against `ADAPTER_IDS`, never against a literal
`{"vdj", "traktor"}` — which program sits where is a registration detail.

**`MainWindow`'s one-line delegators are its public API, not clutter.** The suite
is bolted to its *private* surface (`window._create_node`, `window._move_nodes`,
`window._hidden`, …) across hundreds of call sites, and cross-controller calls
route through the window too (`win._clear_tabs_for(…)`) so every call site is
uniform. Deleting one silently breaks tests it is not obviously connected to.

**Controllers receive the window at runtime and must never import
`main_window`** — that is what keeps the import graph acyclic.

---

## Features

**Sync and diff.** Bidirectional cue/beatgrid + metadata sync with the verified
timing correction and play-count carry-over; change attribution so a re-sync only
pushes what actually changed; status glyphs computed against a selectable
reference program.

**Shell.** Splittable tabbed track area (one or two groups, per-tab program
colour, drag-drop, a persistent "+" tab, a per-tab close "×"). Sidebar with a
collapsible Hidden section plus **one tree per enabled program**, hide/unhide,
drag-reorder of sections, per-playlist status squares. Light/dark theming,
movable/hideable columns, session state persistence.

**Needs-sync pane.** View → Playlists needing sync shows a pane between the
sidebar and the tracks (`ui/sync_pane.py`) listing only the playlists whose
square is not green, grouped per program, as flat `Folder / Playlist` rows.
Three rules keep it honest, each pinned in `tests/test_sync_pane.py`:

- **It computes no status.** `_refresh_sync_pane` reuses `_tree_status_fns`
  through `_edited_overlay`, the same functions the sidebar squares come from,
  and is called from `_reapply_status_icons` and `_repaint_tree`. A second diff
  could disagree with the sidebar; this cannot.
- **It owns no actions.** Its selection (Shift/Ctrl, like the sidebar) is
  mirrored into that program's sidebar tree with `select_nodes`, one
  `playlists_selected` for the lot, so the playlists load through the real
  selection path. A right-click first sets the sidebar selection to the pane's,
  silently, then opens the ordinary `playlist_menu`, whose "whole selection
  when the clicked row is in it, else that row" rule makes several rows sync
  together. Sync, Clean up, Hide and the rest need no copy.
- **A selection belongs to one program**, as in the sidebar: a sync goes from
  one program, so Ctrl-clicking into another group moves the selection there.
  Right-click never moves it. It survives a refresh by `(source, node_path)`.
- **BLACK is opt-in** ("Include playlists missing from the reference"), and an
  empty list says "No playlist has diverged" with a count of the missing ones,
  never "in sync", when nothing has synced yet. Hidden playlists stay out.
- **Songs to sync**, below the list in a splitter (`sync_pane_split` in
  ui_state): every not-green track of the highlighted playlists, once each, in
  table order. `_refresh_sync_pane_songs` takes the rows from `_playlist_rows`,
  the track table's own builder, so the glyphs (preview rows included) are the
  table's. The "what differs" text is `_song_difference`: red is a missing
  file, the preview glyphs say which program added or removed the track, and a
  yellow names its differences via `status.what_differs`, which shares
  `_kind_equivalent` with `cues_equivalent` so it cannot name a difference the
  colour is not about. Songs select like any track list (Shift/Ctrl,
  right-click moves nothing; kept by key across a refresh), and the selection
  is mirrored into the track table (`_on_sync_pane_songs` → `select_keys`).
  Right-click opens the ordinary track menu (`_on_sync_pane_song_context` →
  `_on_track_context`); since the table holds the same selection,
  `_selected_track_keys` gives every action the whole selection when the
  clicked song is in it, with no second implementation.

While off it is never refreshed, so it costs nothing. `h_splitter` now holds
three panes; a two-size list from an older session is slotted around a
zero-width pane.

**Programs menu.** Tick which programs this session uses. A program switched off
is genuinely absent — not parsed from disk, not in the sidebar — so switching one
off also speeds up a launch. The choice persists in `ui_state`; at least one has
to stay on, or there would be no pane to turn anything back on from.

**Playback.** Waveform panel with hold-to-preview, hotcues, tempo fader, on-end
behaviour, pull-based `QAudioSink`. **Next/Previous follow the "on end" mode**,
so Next manually triggers whatever would have happened at the end of the track,
including a random pick under shuffle. Single-file mode is the exception: it
means "do not auto-advance", but a button press has to do something, so it steps
through the shown list. A **session play history** backs Previous, browser-style:
back walks up it, Next replays forward, and taking a different turn part-way back
drops the forward tail. Under shuffle there is no "previous track in the list",
only the one you actually heard. Not persisted — it only means anything within a
listening session. A replay moves the position instead of appending, or stepping
back and forth traps you between two tracks. `WaveformPanel.click_load` returns
whether it actually loaded, so a click it declined during playback is not
recorded as heard.

**Playlist editing.** Remove from playlist, remove duplicates, drag-to-add (same-
and cross-program, one confirmation for cross-program), positional insert with a
drop-line indicator, in-place drag reorder. All **live and non-blocking**: mutate
the in-memory model, refresh every view of that playlist, write the file on the
background queue — no library reload, no playback interruption, no UI freeze.

**Duplicates.** VirtualDJ's per-playlist `<VirtualFolder noDuplicates="yes|no">`
is parsed, preserved on write, and toggleable via "Allow duplicate tracks" (VDJ
only; Traktor always allows). A dup-allowing playlist shows every occurrence as
its own row and an **∞** marker after the sidebar square.

**Collection operations.** Removing playlists/folders acts on the whole sidebar
selection when the right-clicked row is part of it, else on that row alone — the
same rule Sync uses, since right-click deliberately does not change the
selection. One confirmation naming everything that will go. "Remove from playlist
+ collection entry" strips a track from all of the current program's playlists
and deletes its `<ENTRY>`/`<Song>`. "Delete file (Recycle Bin)" removes it from
**every loaded** program and sends the audio to the Recycle Bin. "Import new
files" accepts a drag from Explorer onto a track table (positional) or a sidebar
playlist, or a drag of the waveform title; ffprobe reads
title/artist/album/genre/bpm/length. All three gate through one confirm →
VDJ-closed check → backup → atomic write → reload helper (`guarded_write`).

**Clean up.** `Clean up…` on the playlist context menu scans the selection (the
same scoping rule Sync and Remove use) for fixable defects and reports them
**grouped by kind**, each group with its own checkbox — "fix the ampersands,
leave the percent signs" is a real answer. The scan writes nothing, so it needs
no confirmation; applying goes through `guarded_write`.

Two of the three kinds are "the filename carries escaped text where a
character was meant": `xml_entity_filename` (`Safe &amp; Sound.mp3`) and
`percent_escape_filename` (`Rock%20Steady.mp3`). Adding a kind is one
`Detector` in `model/cleanup.py`; its `propose(path, exists)` gets the
filesystem check the scan was handed.

Applying **renames the audio file, then relinks every loaded program** through
`repath_tracks_job` — and only for files that really moved, so a failed rename
leaves that track untouched rather than pointing at a name that is not there. A
finding is reported but **not offered** when the decoded name is illegal on
Windows (`&quot;` decodes to `"`), when the file is not on disk, or when two
tracks would land on one file. Those are shown greyed with the reason, because
"6 I cannot fix, and why" is the useful answer.

**A percent-escape that decodes to a control character is not an escape** —
`BIXIGA 70 7pol 100%13` is "100 percent 13", and requiring the result to be
printable is what keeps that album out of the report.

The third kind, `unicode_form_path`, **renames nothing** (`Detector.relinks_only`).
A library records an accented name in one Unicode spelling while the file on
disk has the other (see **Identity and matching**), so the file is right and
only the record is wrong. `rename_files` skips these, `relink_map` hands them
straight to the same `repath_tracks_job`, and the dialog's third column names
the code points that differ (`e + U+0301 → U+00E9`), because both name columns
show identical text. Rules that are each pinned in `tests/test_cleanup.py`:

- **Only a recorded path that is not on disk is considered.** A spelling that
  exists is the file, whatever form it is in.
- **The disk decides the direction**: composed→decomposed is relinked as readily
  as the reverse.
- **Both other spellings on disk** (a path mixing forms) is ambiguous and
  blocked.
- **`block_existing_entries` refuses a relink that would duplicate an entry,
  per program.** A program whose *collection* already holds the on-disk name
  beside the stale one should lose the stale entry, and that is the user's
  call. That program sits the relink out (`Finding.skipped`, applied through
  `skipped_keys` in `_apply_cleanup`; the dialog names it) while every other
  program is still relinked. Only when every program holding the stale entry
  also holds the on-disk one is the finding blocked outright. The reported
  case: "l0tso x odie - seńorita" was missing in Traktor (decomposed record,
  composed file) while VirtualDJ held both, the real entry with cues plus a
  bare decomposed stub its playlists pointed at. Blocking the whole finding
  left Traktor unfixable. Playlist-only references are deliberately excluded,
  since relinking those is exactly what reunites VirtualDJ's `.vdjfolder` rows
  with their `database.xml` entry.
- **An ASCII path is never stat-ed**, so the scan costs nothing on most of a
  library.

On the real pair (2026-10-31) it finds 157 fixable Traktor entries and 158
VirtualDJ playlist references, with one of each blocked for a duplicate.

**Search.** Typing in the search box widens the range from the open playlist to
the whole collection of the focused tab and repopulates it, **grouped**: tracks
already in the focused playlist first → a separator row → every other match,
orphans included. Debounced, with a cached per-track search blob (includes the
path, so tag-less orphans match by filename), a result cap and a persistent clear
"✕".

**Metadata editing.** "Edit…" opens a dialog over the whole selection; fields
that differ show `<various>`, and leaving that placeholder alone leaves each
track's own value untouched. **Apply writes; Cancel and the window X both
discard.** Click-to-edit covers the same five text columns in place. An edit
writes to **only the program being viewed** — the diff then attributes it to you
and a sync propagates it, exactly how cue edits behave. It is deliberately *not*
gated through `guarded_write`: that snapshots the whole library, which is right
for a deletion and absurd per keystroke, and a metadata edit is reversible by
editing back.

**Help** has four tabs: the feature guide/test checklist, **Status symbols**
(every playlist and track glyph, drawn in both skins, with its meaning), the
**fixed controls**
(rendered from `FIXED_CONTROLS`, so Help and the Controls dialog cannot both
claim a chord), and a **Tutorial** built from `QLabel` + `QMovie` so GIFs dropped
into `Images/tutorial/` animate. Until one is there, each topic shows a
placeholder naming the file it wants.

---

## Where to take it next

- **Route sync writes through `WriteQueue`.** `SyncEngine` currently calls the
  adapters' jobs directly because the busy overlay and the reload afterwards both
  assume the writes have landed. Queueing them would make sync async — a real
  improvement, but it changes when the reload sees results.
- **Background the Phase-3 collection writes.** Remove/delete/import do a
  synchronous write + reload behind the confirmation; on a 45 MB `database.xml`
  that is a visible pause. Route them through the cooperative reload the
  membership edits already use.
- **Extend `_restatus_tracks` to the playlist sync and to metadata edits.** The
  track sync already avoids the ~30 s reload this way (see **UI behaviour**); a
  playlist sync pays it in full. More has to be right in memory there —
  playlists created, entries added and removed, divergence markers — which is
  why it was left for a second step.
- **Launch and Refresh still cost ~30 s** on a 165,794-track pair, broken down
  under **UI behaviour**. No single win: it is spread across two parses, two
  diffs and the integrity scan. A streaming parse or a persistent index is the
  real answer.
- **Undo/redo** for edits and deletes, and a **backups browser/restore** UX — the
  data is already snapshotted, there is just no in-app way to browse it beyond
  "Revert to backup…".
- **Search polish:** match highlighting, sortable results (sorting is suppressed
  while the grouped separator is shown), column-scoped queries, an "orphans only"
  filter.
- **Import depth:** cover art and musical-key detection, and a progress indicator
  for bulk imports.
- **Finish decoupling the controllers.** Each still takes the whole window and
  reaches through it (`self.win._writer`, `win.libraries[source]`) — a deliberate
  choice that made every extraction a mechanical move so the existing suite could
  stand as the specification. Passing explicit dependencies instead is what makes
  them testable without a real `MainWindow`.
- **Calibrate Rekordbox's timeline.** `RekordboxAdapter.timeline_offset` is 0
  until measured — see **Rekordbox**.
- **Calibrate Serato's timeline**, the same way — see **Serato**.
- **Serato cues in FLAC, M4A and Ogg** (3% of the real library): Vorbis
  comments and MP4 atoms hold the same frames base64-wrapped.
- **A Serato tag-restore entry in the UI.** `serato_tags.restore()` exists and
  is tested; there is no menu for it yet.
- **Extract what is left of `main_window.py`** (~2,950 lines): sync, backups,
  file integrity, playback/auto-advance, search and state persistence still sit
  beside the composition root and the delegators.
- **Cross-platform gaps.** `io/recycle.py` (SHFileOperationW),
  `io/vdj_process.py` (tasklist) and the mid-drag wheel pump in
  `ui/drag_scroll.py` (PeekMessage/GetAsyncKeyState) are Windows-only. All
  degrade to no-ops elsewhere, but a Mac/Linux port needs real implementations.
- **Streaming parser.** The reload is smooth but still re-reads the whole
  library; a truly incremental parse or a persistent index would shrink the
  window further and cut peak memory.
- **NFC/NFD paths beyond Clean up.** Clean up repairs the records; the
  integrity scan and the player could also try the other spelling when the
  recorded one is missing, under the same disk-checked rule, so a track is not
  red before anyone runs Clean up. See **Identity and matching**.

---

## Codebase map

### Entry points and reference implementations
| File | Purpose |
|---|---|
| `app.py` | Launcher — `python app.py` → `trackcleaner.app.main()`. |
| `trackcleaner/app.py` | Builds the `QApplication`, applies theme, shows `MainWindow`. |
| `trackcleaner/__init__.py` | Package docstring + `__version__`. |
| `SlipStreamIO.spec`, `.github/workflows/release.yml` | The PyInstaller build and the tag-triggered release that runs it on Windows and macOS runners (see **Packaging and releases**). `requirements.txt` pins the runtime, `requirements-dev.txt` adds tests and the build. |
| `nml_to_xml_sync.py` | The one-shot CLI this app grew out of. Kept as the reference sync implementation — `model/matching.py`, `model/cues.py`, `io/traktor_read.py` and `io/vdj_write.py` are ports of its rules, and five module docstrings still cite it. Its docstring and the CSV bucket labels both scripts write (`A_grid_late` / `B_cues_early`) name a superseded model; treat them as opaque class identifiers. `StartTimeCache.save()` writes plain `A`/`B` and never reads the column back. |
| `StartTimeScanner.py` | Standalone read-only ffprobe survey. Buckets a music folder by container `start_time` and writes `start_time_report.csv` — the **same file** `StartTimeCache` reads, so one overnight scan pre-builds the cache. It flags any positive-but-unexpected `start_time` as an outlier, deliberately stricter than the sync, which treats *any* positive value as Class A. An outlier row is an invitation to spot-check that file, not an error. |

### `trackcleaner/model/` — pure data and logic (no Qt)
| File | Purpose |
|---|---|
| `library.py` | `Track`, `Playlist`, `TreeNode`, `Library`; `path_ci`, `playlists_containing`, `playlist_index`, `find_playlist`, `colliding_playlist_paths`. |
| `matching.py` | Path building/decoding and cross-database track identity (`path_key`, Traktor↔Windows conversion, `decode_entities`, `normalize_text`). |
| `cleanup.py` | Clean up's detectors and findings: what is wrong with a track, what the fix would be, and why it cannot be applied. Rename kinds plus the relink-only `unicode_form_path`; `block_existing_entries`, `relink_map`, `spelling_difference`. Qt-free. |
| `keys.py` | Musical-key conversion between VDJ notation and Traktor Open Key, both directions. |
| `cues.py` | Cue/beatgrid parsing and formatting, the Class-A correction, the ffprobe start-time cache. **Load-bearing — don't simplify.** |
| `status.py` | The diff engine: track/playlist status glyphs, cue equivalence, change attribution, `playlist_divergence`, `changed_glyph`. |
| `baseline.py` | Persistent fingerprint store, scoped per program pair via `Baseline.for_pair(a, b)`. |

### `trackcleaner/adapters/` — one module per supported program
| File | Purpose |
|---|---|
| `base.py` | `LibraryAdapter` (the interface), `PathField`, the `Job` alias, the canonical-timeline pair `to_native_pos`/`from_native_pos`, `collection_session`, `display_key`, `key_fields`, `read_tracks`, `repath_tracks_job`; the metadata/key writes take an optional `paths` so they can locate rather than walk. |
| `registry.py` | `ADAPTER_CLASSES` — the one list a new program is added to. `build_adapters`, `display_name`, `all_path_fields`, `logo_file`. |
| `traktor.py` | Traktor, wrapping `io/traktor_read.py` + `io/traktor_write.py`. Declares the Class-A timeline offset. |
| `vdj.py` | VirtualDJ, wrapping `io/vdj_read.py` + `io/vdj_write.py`. `clobbers_on_exit = True`. |
| `rekordbox.py` | Rekordbox through `rekordbox.xml`, wrapping `io/rekordbox_read.py` + `io/rekordbox_write.py`. Path-addressed playlists; timeline offset 0, **uncalibrated**. |
| `serato.py` | Serato DJ: `database V2` + crate files via `io/serato_db.py`, cues in the audio files via `io/serato_tags.py`, the cue cache. `clobbers_on_exit`, `holds_empty_folders = False`; timeline **uncalibrated**. |
| `slipstream.py` | SlipStream's own library, modelled as just another program so its playlists get the whole sidebar for free. Writes the whole document, safe only because this app owns every byte of it. |

### `trackcleaner/native/`
| File | Purpose |
|---|---|
| `slp.py` | The neutral `.slp` format: plain absolute paths, separate `CUE`/`BEATGRID` elements, one key notation, canonical positions. ElementTree rather than text surgery. |

### `trackcleaner/io/` — per-format readers and writers
Called **only** from `adapters/`; nothing else should import these directly.

| File | Purpose |
|---|---|
| `traktor_read.py` | `collection.nml` → `Library` (byte-level regex per `<ENTRY>`, `yield_fn` hook). |
| `traktor_write.py` | Text-surgical NML writes: cue/playcount sync, playlist membership, `remove_entry_from_nml`, `repath_entries_in_nml`, node/folder creation, `_entry_key` for batched walks. |
| `vdj_read.py` | `database.xml` + the `Folders/` tree → `Library`; parses `noDuplicates`; `yield_fn` hooks. |
| `vdj_write.py` | Text-surgical XML writes: cue sync, `.vdjfolder` render/write, `remove_song_from_xml`, `repath_songs_in_xml`/`repath_songs_in_vdjfolder`, `_qattr`/`_qtext` escaping. |
| `backup.py` | Snapshot backups (nml + xml + `Folders/`, plus every other library file in `Paths` under `files/`) + restore; `make_backup` single-file copy for playlist sync. |
| `rekordbox_read.py` | `rekordbox.xml` → `Library`: Location URLs decoded once, hot cues + the `<TEMPO>` grid anchor as Pois, TrackID- and Location-keyed playlists. |
| `serato_db.py` | Serato's record format (`parse`/`encode`/`validate`), the database (drive-relative paths, byte-preserving edits), crate files and `neworder.pref`. |
| `serato_tags.py` | Serato's cue/grid frames in audio files: Markers2/BeatGrid codecs in Serato's exact style, verified copy-and-swap writes, frame backups + `restore`, `CueCache`. **The only module that writes into audio files.** |
| `rekordbox_write.py` | Text-surgical `rekordbox.xml` writes: cues/grid/playcount, tags, entries, repath, and the `<NODE>` tree edited by spliced spans (`scan_nodes`) with `Count`/`Entries` kept true. |
| `write_queue.py` | `WriteQueue` (serialized atomic background writes), `atomic_write_text`, `atomic_write_xml`, `BackgroundTask`. |
| `synclog.py` | Per-run diagnostic logs, `NullLog`, `Timings`. |
| `probe.py` | ffprobe-based `probe_track(path, source)` for import metadata; `AUDIO_EXTS`. |
| `vdj_process.py` | Is VirtualDJ running? (`tasklist`; Windows-only, "can't tell" elsewhere). |
| `recycle.py` | `send_to_recycle_bin(path)` via `SHFileOperationW` (Windows; no-op elsewhere, never a hard delete). |
| `reveal.py` | `reveal_in_file_browser(path)` via `explorer /select,`. The switch and path are **one argv token** — split or quoted as a whole, Explorer ignores it and opens Documents. |

### `trackcleaner/sync/`
| File | Purpose |
|---|---|
| `engine.py` | `SyncEngine`: New/Update/Overwrite playlist sync and `sync_tracks` (the plural; `sync_track` is its one-item wrapper) across programs; creates missing collection entries and playlist nodes; `_Plan` collect-then-flush; `_name_collision` refusals. Names no program and no file format. |

### `trackcleaner/audio/`
| File | Purpose |
|---|---|
| `pcm.py` | Decode audio to raw PCM (via ffmpeg) for the pull-based sink. |
| `peaks.py` | Decode audio to a compact min/max waveform envelope. |
| `player.py` | Buffered playback: a pull `QAudioSink` fed from an in-memory `_PcmSource` (20 ms buffer, on-the-fly tempo/seek). |
| `transport.py` | Qt-free play/hotcue/hold-to-preview transport rules. |

### `trackcleaner/config/`
| File | Purpose |
|---|---|
| `paths.py` | `Paths`/`Settings` (persisted JSON), `settings_path`, `baseline_path`, `app_dir`, `images_dir`/`image_path`. |
| `controls.py` | The assignable-shortcut registry (`Action`, `ACTIONS`, `defaults`, `conflicts`) and its `userControls.ini`. |
| `state.py` | JSON-safe session-state capture/restore helpers for the window. |

### `trackcleaner/ui/` — the PySide6 front end
| File | Purpose |
|---|---|
| `main_window.py` | The application shell (~2,950 lines): composition root, menus, signal wiring, library load/reload + status diff, sync, backups, integrity, search, playback/auto-advance, state persistence, plus thin delegators to the controllers. |
| `controllers/structure.py` | `StructureController` — sidebar tree ops: new/duplicate/rename/move/remove playlists and folders, the shared relocate jobs (rename and move are the same operation on VDJ), path-keyed cache migration. |
| `controllers/views.py` | `ViewController` — tabs and their contents: row building incl. sync-preview ghosts, table population + scroll restore, node re-resolution by `node_path`, the sidebar↔tab link, lazy session tab restore. |
| `controllers/membership.py` | `MembershipController` — add/remove/reorder within a playlist, duplicate policy, cross-program drag confirmation. |
| `controllers/collection.py` | `CollectionController` — the destructive/import ops and `guarded_write` (confirm → VDJ-closed check → backup → write). |
| `track_table.py` | `TrackTable` — the sortable track view: rows/columns, drag-out, drop-in, positional insert/reorder, separator rows, font zoom, per-pane selection colour. |
| `track_tabs.py` | The splittable tabbed area: tab groups, the "+" tab, per-tab close "×", split/unsplit, drop-to-tab, focus tracking. |
| `playlist_trees.py` | Sidebar `LibraryTree` + `HiddenTree` + collapsible `PlaylistPanel`; drops, hover highlight, section reorder. One tree per **enabled** adapter, built from the registry. |
| `drag_scroll.py` | Drag auto-scroll (rows/sec ramp) + the mid-drag wheel pump, which peeks `WM_MOUSEWHEEL` off the queue because `DoDragDrop` never dispatches it. |
| `waveform_panel.py` | Waveform display + transport UI + the draggable title label (import gesture). |
| `status_squares.py` | Cached status icons, `status_pixmap` for a given skin, and the playlist/track legends Help renders. |
| `theme.py` | Light/dark theming for the whole app. |
| `context_menus.py` | Builders for the playlist and track right-click menus. |
| `fields.py` | The track-table column registry (shared by table, header menu, fields panel). |
| `fields_panel.py` | Left-column checkbox list mirroring visible/hidden columns. |
| `busy_overlay.py` | Centred "busy" overlay for the remaining synchronous blocking ops. |
| `help_window.py` | Feature guide/test checklist, the Status symbols legend (both skins), the fixed controls (from `FIXED_CONTROLS`), and the Tutorial tab. |
| `controls_dialog.py` | `ShortcutEdit`, the two timing sliders, live conflict warning, Reset to defaults. |
| `cleanup_dialog.py` | Clean up's results: one section per problem kind, its own checkbox, blocked findings greyed with the reason. |
| `metadata_dialog.py` | The Edit… dialog: `<various>` for fields that differ across a selection, writes only on Apply. |
| `tap_detect.py` | The double-tap rule, as a plain object so it is testable without synthesising Qt events. |
| `watermark.py` | Cached, faded program logos for the track panes. |
| `paths_dialog.py` | Configure-paths dialog. Rows are built from the registered adapters' `path_fields()`. |
| `sync_pane.py` | `SyncPane`, the View → Playlists needing sync worklist: not-green playlists per program, fed by the window from the sidebar's own status functions. |
| `playlists_window.py` | "Playlists…" popup: every playlist containing a given track, in every loaded program, grouped per program with the track's own first. Each row carries its source. |

### `tests/`
77 modules plus `fixtures.py`, `conftest.py` (`SAMPLE_NML`, `EMPTY_VDJ_XML`, `DOO_RAG_SONG`,
`write_sample`) and `fake_adapter.py`. Coverage spans the model and diff
(`test_status`, `test_status_glyphs`, `test_playlist_status`, `test_baseline`,
`test_cues`, `test_cue_roundtrip`, `test_grid_phase`, `test_playcount_and_lookup`, `test_packaging`, `test_vdjfolder_rows`, `test_vdj_database_layout`, `test_poi_parsing`, `test_key_routing`,
`test_line_endings`, `test_name_collisions`, `test_entity_paths`, `test_cleanup`),
IO and writers
(`test_playlist_write`, `test_playlist_removal`, `test_collection_removal`,
`test_batched_writers`, `test_batched_writes`, `test_fast_locate`,
`test_nml_integrity`,
`test_write_queue`, `test_backup`, `test_probe`, `test_recycle`), sync
(`test_metadata_sync`, `test_sync_attribution`, `test_sync_through_native`, `test_rekordbox`, `test_serato`,
`test_sync_to_all`, `test_sync_selection`, `test_synclog`,
`test_probe_and_timing`, `test_track_sync_refresh`), the adapter
contract (`test_adapter_contract`, `test_adapter_timeline`, `test_native_slp`),
audio (`test_player`, `test_pcm`, `test_peaks`, `test_transport`,
`test_waveform_panel`, `test_autoadvance`, `test_next_previous`,
`test_playback_controls`) and the UI (`test_track_table`, `test_track_tabs`,
`test_track_drag`, `test_track_context`, `test_click_to_edit`,
`test_metadata_edit`, `test_playlist_sections`, `test_playlist_navigation`,
`test_playlist_creation`, `test_folders_and_moves`, `test_membership_editing`,
`test_view_preservation`, `test_refresh_behaviour`, `test_pane_state`,
`test_arrow_routing`, `test_controls_hotkeys`, `test_shortcut_capture`,
`test_gesture_timing`, `test_help_controls`, `test_program_selection`,
`test_reference_program`, `test_session_state`, `test_tab_sizing`,
`test_theme`, `test_watermarks`, `test_duplicates_and_reorder`, `test_sync_pane`,
`test_drag_scroll`).

---

## Glossary

| Term | Meaning |
|---|---|
| **NML** | Traktor's XML collection format (`collection.nml`). Cue timestamps in milliseconds. |
| **`CUE_V2`** | An NML element holding one stored point — cue, grid marker, loop, load point — distinguished by `TYPE`. |
| **HOTCUE** | Traktor's performance-pad slot (0–7); `-1` means stored but not on a pad. Maps to VirtualDJ `Num` as HOTCUE + 1. |
| **Beat Marker** | A grid anchor that is simultaneously a hot cue — either one `TYPE=4` element carrying a real HOTCUE, or a `TYPE=4` + `TYPE=0` pair at the same timestamp. |
| **Poi** | "Point of interest" — VirtualDJ's per-track stored point (`Type="cue"` or `"beatgrid"`), position in seconds. |
| **`start_time`** | Container-level audio stream field (via ffprobe) expressing the decoder-honoured encoder-delay offset. The sole reliable Class A/B discriminator. |
| **Class A / Class B** | The two file populations: `start_time > 0` (shift by 51.536 ms) versus `start_time == 0` (no shift). |
| **The constant** | `0.051536` s — the universal correction magnitude, identical to the offset VirtualDJ's own native NML import applies. |
| **Encoder delay** | Silence samples prepended to MP3 audio by the encoder; whether decoders skip it determines which timeline a timestamp refers to. |
| **Canonical position** | Seconds from the first decoded audio sample. The timeline every adapter converts to and from, so a position means the same physical instant whichever program it came from. |
| **Reference program** | The program every other one's status is computed against, chosen in `Programs → Compare against`. |
| **Job** | A zero-arg callable returned by an adapter write method instead of performing the write, so the caller decides when and where it runs. |
| **`path_key`** | A track's identity: its Windows path, entity-decoded and lowercased. |
| **`path_ci`** | A playlist's identity: its node path as a case-folded tuple. |
