# SlipStreamIO — how it is wired

Four views of the same program: what the packages are and which way they depend,
what happens between launching and a coloured square, what happens between
clicking Sync and bytes on disk, and how the window is put together.

`HANDOFF.md` is the reference for *rules*; this is the shape they sit in.

> Every arrow here was derived from the source, not from memory. The package
> graph below is what the imports actually say.

---

## 1. Packages, and the one boundary that matters

Arrows point the way imports run — `ui` depends on `sync`, not the reverse.

```mermaid
flowchart TB
    subgraph L5[" "]
        ui["<b>ui/</b><br/>MainWindow · 4 controllers · panes"]
    end
    subgraph L4[" "]
        sync["<b>sync/</b><br/>SyncEngine"]
    end
    subgraph L3[" "]
        adapters["<b>adapters/</b><br/>traktor · vdj · rekordbox · serato · slipstream<br/><i>the boundary</i>"]
    end
    subgraph L2[" "]
        native["<b>native/</b><br/>slp — the neutral format"]
        fmt["<b>io/</b> format readers + writers<br/>traktor_read/write · vdj_read/write<br/>rekordbox_read/write · serato_db · serato_tags"]
        svc["<b>io/</b> general services<br/>write_queue · backup · synclog<br/>probe · recycle · reveal · vdj_process"]
    end
    subgraph L1[" "]
        model["<b>model/</b><br/>library · matching · cues · keys<br/>status · baseline · cleanup"]
        audio["<b>audio/</b><br/>pcm · peaks · player · transport"]
        config["<b>config/</b><br/>paths · controls · state"]
    end

    ui --> sync
    ui --> adapters
    ui --> audio
    ui --> svc
    ui --> model
    ui --> config
    sync --> adapters
    sync --> svc
    sync --> model
    sync --> config
    adapters --> fmt
    adapters --> svc
    adapters --> native
    adapters --> model
    native --> svc
    native --> model
    fmt --> model
    svc --> model
    svc --> config

    linkStyle default stroke:#888
    classDef leaf fill:#2c4c42,stroke:#3f6f5f,color:#d8f5e8
    classDef bound fill:#6a6a6a,stroke:#22d3ee,color:#eaeaea,stroke-width:3px
    classDef fmtc fill:#7f3030,stroke:#b23b3b,color:#fff
    class model,audio,config leaf
    class adapters bound
    class fmt fmtc
```

**`model/`, `audio/` and `config/` import nothing from other packages** (only
siblings within themselves). They are the bottom, and everything is allowed to
depend on them.

**Nothing outside `adapters/` touches a format reader or writer.** That is the
rule the whole design rests on: add a program by adding an adapter, and no
sidebar, menu, diff or sync has to learn about it. If you find yourself writing
`if source == VDJ` outside `adapters/`, the thing you want is an adapter method.
(A few pre-adapter leftovers still do — `io/backup.py`, one check in
`controllers/membership.py`, two defaults in `MainWindow`; `HANDOFF.md` lists
them. They are debt, not precedent.)

**`io/` is two different things**, which is why the rule is not simply "io is
adapters-only":

| kind | modules | who may import |
|---|---|---|
| format readers/writers | `traktor_read`, `traktor_write`, `vdj_read`, `vdj_write`, `rekordbox_read`, `rekordbox_write`, `serato_db`, `serato_tags` | **`adapters/` only** |
| general services | `write_queue`, `backup`, `synclog`, `probe`, `recycle`, `reveal`, `vdj_process` | anyone |

A service knows nothing about any music program's format; a reader is nothing
but that knowledge.

**`ui/controllers/` never imports `main_window`.** Each controller receives the
window at runtime instead, which is what keeps the import graph acyclic while
still letting them reach shared state.

---

## 2. Launch and Refresh — from files to coloured squares

```mermaid
flowchart TB
    files[("collection.nml · 133 MB<br/>database.xml + Folders/ · 44 MB<br/>library.slp")]
    read["<b>adapter.read()</b><br/>enabled programs only"]
    parse["byte-level regex readers<br/>one block at a time"]
    lib["<b>Library</b><br/>tracks + playlist tree"]
    attr["attribute_changes()<br/><i>who moved, per pair</i>"]
    base[("sync_baseline.csv<br/>fingerprints")]
    diff["diff_tracks()"]
    rep["<b>reports</b><br/>{program: track_status}"]
    integ["_scan_integrity_all()<br/>165,794 os.path.exists"]
    apply["_apply_status()"]
    ui2["sidebar squares · row colours"]

    files --> read --> parse --> lib
    lib --> attr
    attr <--> base
    attr --> diff --> rep
    lib --> diff
    rep --> apply
    integ --> apply
    lib --> integ
    apply --> ui2

    classDef cost fill:#7f3030,stroke:#b23b3b,color:#fff
    class parse,integ cost
```

**The parse dominates, not the disk.** Reading 133 MB off disk takes 0.04 s;
turning its 83,427 `<ENTRY>` blocks into objects takes ~11 s. The integrity scan
adds ~7 s of stat calls. A full refresh on that pair is ~30 s, which is why a
track sync no longer does one — see §3.

**It runs cooperatively on the main thread**, yielding every ~128 items, with
the garbage collector off for the duration. A worker thread does not help: the
GIL starves the main-thread audio pull, which feeds a deliberately tiny 20 ms
buffer, and playback crackles for the whole reload.

---

## 3. A sync run — decide, plan, write once

```mermaid
flowchart TB
    start["sync_playlist() / sync_tracks()"]
    decide{"<b>decision loop</b><br/>content_in_sync?<br/>mode? attribution?"}
    skip["skipped, with the reason<br/>target owns · conflict · no history"]
    plan["<b>_Plan</b><br/>cues · tags · keys · source backfill"]
    flush["<b>_flush_writes()</b><br/>one job per KIND, not per track"]
    j1["create_entries_job<br/><i>cues</i>"]
    j2["write_metadata_jobs<br/><i>tags + key, merged</i>"]
    j3["backfill into the <b>source</b>"]
    ad["adapter — returns a job,<br/>never performs one"]
    w{"io writer"}
    loc["locate the entry<br/><i>batch ≤ 32</i>"]
    walk["walk the document"]
    sess["collection_session<br/><i>held in memory</i>"]
    guard["atomic_write_xml<br/><i>parses before os.replace</i>"]
    disk[("disk")]

    start --> decide
    decide -->|no| skip
    decide -->|yes| plan
    plan --> flush
    flush --> j1 & j2 & j3
    j1 & j2 & j3 --> ad --> w
    w --> loc
    w -->|miss, ambiguous,<br/>or large batch| walk
    loc & walk --> sess --> guard --> disk

    classDef guardc fill:#6a6a6a,stroke:#22d3ee,color:#eaeaea,stroke-width:3px
    class guard guardc
```

**The loop only decides.** Writing used to happen inside it, once per track, and
each write walked the whole document to find its one entry — O(tracks × entries),
which was the entire runtime.

**One job per kind, per run.** Not per track: finding a single `<Song>` means
visiting all 82,366 of them. A key write *is* a metadata write with
program-specific spelling, so the two are merged into one pass.

**A small batch skips the walk entirely**, searching for the attribute the
writer itself would emit. It falls back to the walk on anything it will not
vouch for — a name it cannot find, one that appears twice, a key that disagrees —
so it can only ever be wrong by being slow.

**Nothing malformed reaches disk.** `atomic_write_xml` parses the exact string
it is about to store and refuses to replace the file otherwise.

### Positions are converted through a neutral timeline

```mermaid
flowchart LR
    t["Traktor<br/>stored ms"] -->|to_native_pos| n(("<b>canonical</b><br/>seconds from the first<br/>decoded audio sample"))
    n -->|from_native_pos| v["VirtualDJ<br/>stored s"]
    v -->|to_native_pos| n
    n -->|from_native_pos| t
    n <-->|both ways| s["SlipStream<br/>.slp"]
```

Each adapter declares only how *its* numbers relate to that one timeline, so a
third program needs no knowledge of the first two. The Class-A correction
(`0.051536 s`, applied only when `ffprobe` reports a non-zero container
`start_time`) lives in the Traktor adapter, because that is whose numbers it
describes.

---

## 4. How the window is put together

```mermaid
flowchart TB
    mw["<b>MainWindow</b><br/><i>composition root</i><br/>menus · libraries · reports · sync"]
    subgraph ctl["ui/controllers/ — receive the window, never import it"]
        c1["<b>structure</b><br/>new · rename · move<br/>remove playlists/folders"]
        c2["<b>views</b><br/>tabs · rows · scroll<br/>sidebar↔tab link"]
        c3["<b>membership</b><br/>add · remove · reorder<br/>duplicate policy"]
        c4["<b>collection</b><br/>delete · import<br/>guarded_write"]
    end
    subgraph panes["panes"]
        tree["playlist_trees<br/><i>one per enabled program</i>"]
        syncp["sync_pane<br/><i>not-green worklist, off by default</i>"]
        tabs["track_tabs → track_table"]
        wave["waveform_panel"]
    end
    wq["<b>WriteQueue</b><br/>serialized · atomic · background"]
    eng["SyncEngine"]
    aud["audio/player · peaks → ffmpeg"]

    mw --> c1 & c2 & c3 & c4
    mw --> eng
    mw --> panes
    c1 & c3 & c4 -->|edits| wq
    c2 --> tabs
    tree & tabs & wave --> mw
    syncp -->|click selects in| tree
    wave --> aud
    eng -->|inline| adapters2["adapters"]
    wq --> adapters2

    classDef q fill:#2c4c42,stroke:#3f6f5f,color:#d8f5e8
    class wq q
```

**Edits are queued; sync writes are not.** A membership or metadata edit mutates
the in-memory model, refreshes every view of it, and writes on the background
queue — no reload, no interrupted playback, no frozen UI. A sync calls the
adapters' jobs inline, because the busy overlay and the refresh afterwards both
assume the writes have landed.

**The needs-sync pane is a view of the sidebar, not a second diff.** The window
feeds it the same status functions the squares were painted with, and a click
selects the playlist in its sidebar tree, so the pane can never disagree with
the sidebar or act on a playlist by a different path.

**The delegators on `MainWindow` are its public API.** The suite is bolted to its
private surface across hundreds of call sites, and cross-controller calls route
through the window too, so every call site reads the same. Deleting one silently
breaks tests it is not obviously connected to.

---

## The rules, in one place

| | |
|---|---|
| **Adapters own program identity** | nothing outside `adapters/` branches on which program it is |
| **Writes return jobs** | the caller decides when and on which thread |
| **Text-surgical** | a collection edit is a regex scoped to one entry; the rest of the file survives byte-for-byte |
| **Never store what will not parse** | `atomic_write_xml` checks before `os.replace` |
| **Memory never claims more than disk** | a write reports whether it landed; only then is the in-memory copy updated |
| **Decode entities exactly once** | at the point raw XML text is captured — never in `matching.path_key` |
| **Positions are canonical** | `source → native → target`, never a pairwise special case |
| **Never a permanent delete** | files go to the Recycle Bin |
| **Audio files: Serato's frames only** | verified on a copy, swapped in, old frames backed up first |
| **Snapshot before destroying** | `guarded_write` backs up every library file `Paths` names first |
| **Close VirtualDJ before writing** | it rewrites its database on exit and would clobber the edit |

Each is stated with its consequences in `HANDOFF.md`.
