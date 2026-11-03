"""Help window: feature documentation that doubles as a per-feature unit-test
checklist (per the user's request that Help = a way to unit-test
functionality). Pure content — no live test execution; each row states what
to do and what to expect, and points at the automated test when one exists.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QUrl
from PySide6.QtGui import QMovie, QTextDocument
from PySide6.QtWidgets import (QDialog, QLabel, QScrollArea, QTabWidget,
                               QTextBrowser, QVBoxLayout, QWidget)

from ..config import paths as paths_mod
from . import theme as theme_mod
from .status_squares import PLAYLIST_LEGEND, TRACK_LEGEND, status_pixmap

_HTML = """
<h2>SlipStreamIO — Feature Guide &amp; Test Checklist</h2>
<p>Each item below is both a feature description and a manual (or automated)
check you can run to confirm it still works. Automated checks referenced as
<code>tests/test_cues.py</code> run via <code>python -m pytest tests/ -q</code>.</p>

<h3>Reading &amp; display</h3>
<ul>
<li><b>Both libraries load on startup.</b> Check: status bar shows non-zero
    track/playlist counts for VDJ and Traktor after launch.</li>
<li><b>Left column shows Hidden / VDJ / Traktor trees.</b> Check: three
    collapsible sections are visible and scrollable independently.</li>
<li><b>Clicking a playlist shows its tracks.</b> Check: select any leaf
    playlist — the table on the right populates with its tracks in order.</li>
<li><b>Clicking a folder shows all songs recursively.</b> Check: select a
    folder node — the table shows the union of every playlist beneath it.</li>
<li><b>Multi-select (Ctrl/Shift) unions tracks.</b> Check: Ctrl-click two
    playlists — the table shows both, de-duplicated by path.</li>
<li><b>Folder selection auto-selects its playlists.</b> Built into the tree's
    selection resolution (folders expand to descendant playlist nodes).</li>
</ul>

<h3>Track table</h3>
<ul>
<li><b>Columns: Status/Order/Playcount/Title/Artist/Album/BPM/Length/Comment/
    Key/Genre/Bitrate/FilePath.</b> Check: all twelve are present by default.</li>
<li><b>Columns are draggable and resizable.</b> Check: drag a header to
    reorder; drag a header edge to resize.</li>
<li><b>Click-to-sort toggles numeric-then-alpha, ascending/descending.</b>
    Check: click Playcount/BPM/Length/Bitrate headers — sorts numerically;
    click again — reverses; text columns sort case-insensitively.</li>
<li><b>Right-click header = tick menu to hide/show fields.</b> Check:
    right-click any header, untick a field, it disappears from the table and
    its Fields-panel checkbox unchecks in sync.</li>
<li><b>Alt+scroll scales text size.</b> Check: hold Alt and scroll over the
    table — font grows/shrinks (clamped 6–28pt).</li>
<li><b>Scrollbars on both sides + bottom.</b> Qt's standard vertical +
    horizontal scrollbars are always available when content overflows.</li>
<li><b>The Key column shows what each program shows.</b> Open Key on a Traktor
    row, musical notation on a VirtualDJ one — worked out from the analysed key
    rather than from the text in the file, because that text is not dependable:
    in one real collection 87 entries store Open Key, 3 store musical notation
    and 3 store nothing at all beside a key Traktor displays perfectly well. A
    track with no analysed key still shows whatever it has.</li>
</ul>

<h3>Editing tags</h3>
<ul>
<li><b>Right-click → Edit…</b> opens a dialog over the whole selection. Fields
    that differ across it show <code>&lt;various&gt;</code>; leave that alone and
    each track keeps its own value, type over it and every selected track gets
    the new one. <b>Apply writes; Cancel and the window X both discard.</b></li>
<li><b>Click a field on an already-selected track to edit it in place.</b> The
    click that <i>selects</i> a track never opens an editor — that takes a second
    click, so browsing a list never drops you into a text box. Bringing an
    unfocused split pane forward does not count as that first click either.
    Check: click a track (nothing opens), click its Title (it opens).</li>
<li><b>A double-click plays and never edits.</b> A click waits to see whether a
    second one is coming before it starts an edit; the wait is the double-click
    interval, set under <b>Controls → Configure shortcuts…</b>.
    Automated: <code>tests/test_click_to_edit.py</code>.</li>
<li><b>Title, Artist, Album, Genre and Comment</b> are editable. BPM, Key, Length,
    Bitrate and Filepath are analysis output and are read-only — editing BPM
    without moving the beatgrid would desynchronise the cues.</li>
<li><b>An edit writes to the program you are viewing</b>, and the status glyphs
    then attribute the change to you so a sync carries it across, exactly as a
    cue edit does.</li>
</ul>

<h3>Controls &amp; timing</h3>
<ul>
<li><b>Controls → Configure shortcuts…</b> assigns a key to any menu action, with
    live conflict warnings, and saves to <code>userControls.ini</code> beside the
    settings file. Check: bind something, restart, it is still bound.</li>
<li><b>Two timing sliders</b> in the same dialog: <b>Double-click</b> (how long a
    click waits before it counts as a single one — this also sets the interval
    the whole app uses) and <b>Double-tap</b> (how quickly two Left/Right presses
    must follow each other to expand or collapse a folder). Separate on purpose:
    a comfortable mouse speed is not a comfortable key-repeat speed.</li>
<li><b>Double-click 0 means "follow Windows"</b> in the file. The slider opens on
    whatever your system is currently set to; moving it pins a value.</li>
<li><b>Tab can be assigned</b>, and so can any chord — the field takes the keys
    rather than letting them do their usual job, so Tab is recorded instead of
    moving to the next control. While you are assigning, <b>no shortcut fires</b>,
    so you can safely rebind a key that is already in use.</li>
<li><b>Mouse buttons can be assigned too.</b> Click the field with the button you
    want: <b>middle, back and forward</b> work on their own; <b>left and right
    need a modifier</b> (Ctrl, Alt or Shift), since both do too much elsewhere —
    and a bare left click is how you focus the field in the first place. A bare
    left or right click says so rather than appearing to do nothing.
    Automated: <code>tests/test_shortcut_capture.py</code>.</li>
<li><b>F6 moves focus between split track panes.</b> With only one pane it does
    nothing — splitting stays a deliberate action from the tab menu.</li>
</ul>

<h3>Debug logs</h3>
<ul>
<li><b>Debug → Log playlist sync (Update / Overwrite) and Log track sync.</b>
    Each is a separate toggle, off by default, and each writes a detailed log of
    every matching run into the <b>logs</b> folder beside your settings — one
    file per run, named for the mode and the playlist.</li>
<li><b>They exist because the interesting failures need a real library.</b> The
    test collections have no beatgrid markers with nested elements, no sync
    history and a handful of tracks, so some faults simply cannot happen there.
    A log records what actually happened on yours.</li>
<li><b>What a log contains:</b> the files touched and the backup taken; for every
    track, whether it is in the target collection, whether the cues and each tag
    field match (both values shown side by side), which program the change is
    attributed to, and <b>the decision plus the reason</b> — "skipped" is what
    you can already see, why it skipped is what a log is for. Then what the
    writers actually answered, the playlist membership before and after re-read
    from disk, and a check that the target file still parses.</li>
<li><b>Debug → Log sync performance</b> is not a log of its own: it adds a
    <b>Performance</b> section to whichever of the logs above are already
    switched on. It gives the wall time for the run and for each phase, the
    counts of whole-file collection reads and writes, and <b>document walks</b> —
    passes over a whole collection to find the entries to change — the numbers that say whether a slow sync is one bad track or one
    rewrite too many. <b>The phases add up to the total</b>, so anything slow has
    a name; the backup copy, the parse check and the file write are each timed
    separately. On its own it writes nothing, because there is no file for
    it to be added to.</li>
<li><b>The first sync of a set of tracks is the slow one.</b> Lining cues up
    exactly means measuring where each audio file really starts, which takes
    about 28 ms per file. That measurement is now <b>saved and reused</b>, so
    tracks you have synced before cost nothing to measure again, and the ones
    that do need measuring are done several at a time. If you re-encode a file,
    setting <b>start_time source</b> (in the paths dialog) to <b>probe</b>
    forces fresh readings.</li>
<li><b>Logging never changes a sync.</b> With the toggles off nothing extra runs
    at all; with them on the sync behaves identically and a logging failure is
    swallowed rather than interrupting a write. Check: the same sync gives the
    same result either way (<code>tests/test_synclog.py</code>).</li>
<li><b>Debug → Open log folder</b> shows them in Explorer.</li>
</ul>

<h3>Appearance</h3>
<ul>
<li><b>Each pane shows its program's logo, faintly.</b> A tab of Traktor tracks
    carries the Traktor mark, VirtualDJ the VirtualDJ one, and so on — so a split
    view tells you which program you are looking at without reading the tab.
    Check: open a playlist from each tree and confirm the mark changes.</li>
<li><b>View → Background logo… sets how strongly it shows</b>, 0–100%. It moves
    live as you drag, Cancel puts it back, <b>0 turns it off entirely</b>, and the
    setting is remembered between sessions. Check: set 60%, restart, it is still
    60%. Automated: <code>tests/test_watermarks.py</code>.</li>
<li><b>A program with no artwork simply has none.</b> The logos are ordinary
    files in <code>Images/</code>; if one is missing or replaced with something
    unreadable, that pane draws no watermark rather than failing.</li>
<li><b>Tabs are as wide as their names need</b>, within bounds — short names get
    small tabs, long ones grow to a limit and then elide with "…". When the strip
    fills, tabs narrow before scroll arrows appear, so every tab stays reachable.
    Check: open several tabs with long playlist names and shrink the window.
    Automated: <code>tests/test_tab_sizing.py</code>.</li>
<li><b>Each sidebar section title carries its program's logo</b>, sized to the
    title text. Hidden has no program and no icon.</li>
</ul>

<h3>Status squares</h3>
<ul>
<li><b>Every symbol, drawn, is on the Status symbols tab.</b> Colour says
    <i>what</i> (green synced, yellow changed, black absent, red error); shape
    says <i>who</i> changed it, relative to the reference program chosen in
    Programs → Compare against.</li>
<li><b>Cue equivalence tolerates the verified ±51.536ms Class-A shift</b> so a
    correctly-migrated track reads green, not perpetually "needs update".
    Automated: <code>tests/test_cues.py::test_class_a_forward_correction</code>,
    <code>test_round_trip_class_a_is_stable</code>.</li>
<li><b>View → Playlists needing sync</b> opens a pane beside the sidebar listing
    only the playlists whose square is not green, grouped per program, with the
    same glyph. Click one to open it; right-click for the usual playlist menu
    (Sync, Clean up…). Tick <i>Include playlists missing from the reference</i>
    to list the black ones too. Check: edit a synced playlist's membership —
    it appears in both programs' groups at once.<br>
    Below the list, <b>Songs to sync</b> shows every song of the highlighted
    playlist(s) that is not green, with its glyph and what differs — e.g.
    “cues, genre”, “grid”, “file missing on disk”, “added in Traktor”. Click a
    song to select it in the track table; Shift/Ctrl-click to select several.
    Right-click for the usual track menu (Sync Track, Edit…), which acts on the
    whole selection when the song you clicked is part of it. Drag the divider
    to resize the two halves.</li>
</ul>

<h3>Waveform &amp; playback</h3>
<ul>
<li><b>Single-click loads a waveform only while nothing is playing.</b> During
    playback a single click still highlights the row but leaves the audio and
    the loaded waveform alone, so you can browse without interrupting the track.
    Check: play a track, click others — it keeps playing.</li>
<li><b>Double-click loads and plays</b>, even while another track is playing —
    it is the deliberate "play this now" action. It never also starts an edit,
    however fast the two clicks are.</li>
<li><b>Next / Previous follow the "on end" setting.</b> Next is a manual trigger
    of whatever would have happened at the end of the track: Order walks the
    playlist's stored order, Sort order walks what is on screen, and the Random
    modes pick at random. "Single file" is the exception — it means "don't
    auto-advance", but a Next press still steps through the shown list.
    Check: set Random (playlist) and press Next a few times — it jumps around
    rather than walking down the list.</li>
<li><b>Previous returns to what you actually heard.</b> There is a session play
    history: Previous walks back up it and Next replays forward, which is the
    only meaning "previous" has under shuffle. Take a different turn part-way
    back and the forward path is dropped, like a browser. With nothing behind
    you, Previous falls back to the list's own predecessor.
    Automated: <code>tests/test_next_previous.py</code>.</li>
<li><b>Play button / <code>W</code> key</b> toggles play and pause.</li>
<li><b>8 hotcue buttons, keys <code>1</code>–<code>8</code>.</b> A button is
    coloured when the track has that cue and greyed out when it does not.
    Colours follow Traktor: blue for a normal cue, white/slate for a cue that
    sits on a beatgrid anchor (a "Beat Marker"). Neither collection.nml nor
    database.xml stores a cue colour, so these are derived, not read.</li>
<li><b>The <code>1</code>–<code>8</code> and <code>W</code> keys work from
    anywhere</b> — the playlist trees and the track table included, not only
    when the waveform has focus. Check: click a playlist and press <code>3</code>
    — the cue fires.</li>
<li><b>Pressing a hotcue while playing</b> jumps there and keeps playing.
    <b>Pressing one while stopped</b> jumps there and plays only while you hold
    the button or key down — on release the playhead snaps back to the cue.
    Check: <code>tests/test_transport.py</code> covers every branch including
    overlapping holds.</li>
<li><b>Time readout</b> shows <code>mm:ss:ms / total</code>; click it to switch
    to time remaining, shown as <code>-mm:ss:ms</code>. <b>Title - Artist</b> of
    the loaded track sits to its left, and the <b>BPM</b> to its right.</li>
<li><b>Tempo fader</b> (vertical slider right of the waveform): centre is 0%,
    top is -35% (slower), bottom is +35% (faster). It changes speed and pitch
    together, like a turntable, and the BPM readout tracks it. Double-click the
    fader to reset it to 0%. It stays put when you load another track and is
    remembered between sessions.</li>
<li><b>Click or drag on the waveform to seek.</b> A red playhead marks the
    current position and the cue positions are ticked in their own colours.</li>
<li><b>Transport keys stand down while you are typing.</b> Digits and "w" in
    the Search box filter tracks; they do not trigger cues or playback.</li>
<li><b>Cues are auditioned exactly as that program stores them.</b> A track
    opened from the VDJ tree plays VDJ's cue positions, from Traktor plays
    Traktor's — which for Class-A tracks differ by the documented 51.536 ms.</li>
</ul>

<h3>Search, refresh, hide</h3>
<ul>
<li><b>Search bar + scope (Traktor/VDJ/ALL).</b> Check: type in Search while a
    VDJ playlist is shown and scope=Traktor — filter is inactive (status bar
    explains why); switch scope to VDJ or ALL — filter applies.</li>
<li><b>Refresh re-diffs both libraries.</b> Check: click ⟳ Refresh — all
    status squares recompute without reloading from disk.</li>
<li><b>Your selection and scroll position survive it</b>, in every pane — a
    refresh, a sync or a tag edit rebuilds the rows, and the highlighted tracks
    come back. Held by track, not by row number, because a sync consumes the
    preview rows and reorders what is left; a track that is genuinely gone is
    simply dropped from the selection.</li>
<li><b>Each playlist section collapses to its title bar.</b> Click the arrow
    beside Hidden, VDJ Playlists or Traktor Playlists — the section shrinks and
    hands its space to the others. The state is remembered between sessions.</li>
<li><b>Drag a playlist/folder into Hidden to hide it; right-click Hidden entry
    → Unhide.</b> Check: drag a playlist onto the Hidden panel, it disappears
    from its software tree and appears under Hidden; right-click it there and
    choose Unhide to restore it.</li>
</ul>

<h3>Context menus</h3>
<ul>
<li><b>Playlist/folder right-click:</b> New playlist, New folder, Duplicate
    playlist, Rename, Select all playlists, Sync → other software (New Only /
    Update / Overwrite…), Hide.</li>
<li><b>New playlist / New folder</b> appear on a folder (created inside it), on
    a playlist (created alongside it, in the same folder), and on <b>empty
    space</b> in a tree (created at that software's root). The new row goes
    straight into edit mode in the sidebar — type the name and press Enter;
    Escape cancels and nothing is written. A name already in use is
    auto-numbered “Name (2)”, and characters Windows forbids in a filename
    become “_” on the VDJ side. It is created in <em>that</em> software only;
    use Sync to push it to the other one.</li>
<li><b>Duplicate playlist</b> makes a sibling copy holding the same tracks in
    the same order, with the row pre-filled with the free name so you can press
    Enter or type your own. The copy is independent — editing it leaves the
    original alone. Offered on playlists only.</li>
<li><b>Rename</b> edits the row in place the same way, and works on folders as
    well as playlists; a folder takes its whole contents along. Note the other
    software still holds the old-named copy until you sync, so its square may
    show divergence until then.</li>
<li><b>Remove playlist / Remove folder</b> asks first, then deletes just that
    entry — a folder takes its contents with it. <b>No track is deleted</b>: the
    collection and the audio files are untouched, and the same track stays in
    every other playlist that holds it. On VirtualDJ the <code>.vdjfolder</code>
    file (or <code>.subfolders</code> directory) goes to the Recycle Bin; a
    Traktor removal is an edit inside <code>collection.nml</code>, so recover it
    with File → Revert to backup.</li>
<li><b>Dragging near the top or bottom entry of a panel scrolls it</b>, faster
    the longer you hold there; move back to the middle to stop, and it starts
    slow again next time. Works in both sidebar trees and the track table, for
    tracks, playlists, folders and files dragged in from Explorer. With the
    button still held, the <b>scroll wheel</b> also scrolls whichever panel you
    are over.</li>
<li><b>Drag a playlist or folder onto another folder to move it</b> — contents
    and all — or onto empty space to move it to the top level. Dropping a
    folder inside itself is refused, and dragging between the VDJ and Traktor
    trees does nothing (that is what Sync is for). Dragging onto the
    <b>Hidden</b> panel still hides instead.</li>
<li><b>Track right-click:</b> Sync Track, Playlists… — every playlist containing
    this track, in <b>every loaded program</b>, grouped under a heading per
    program with the track's own listed first. Double-click one to jump to it
    and snap the sidebar highlight; the row knows which program it came from, so
    a Traktor row opens the Traktor tree.</li>
</ul>

<h3>Sync engine (the write path)</h3>
<ul>
<li><b>Sync New Only:</b> adds tracks present in source but missing from
    target's playlist; syncs cues only for those new tracks.</li>
<li><b>Sync Update:</b> adds new tracks AND pushes cues and tags for shared
    tracks only when the source is the side that changed them; the other
    program's edits are left alone. A change you make <i>in this app</i> counts
    immediately — it used to need a Refresh first, and without one Update
    silently skipped exactly the tracks you had just edited.</li>
<li><b>When Update declines, it now says which of three things happened</b> —
    the other program owns the change, both sides changed it, or <b>the two were
    never compared while they agreed</b>, so nothing can be deduced about who
    moved. That last one is common the first time you use the app on an existing
    pair of libraries: run <b>Overwrite</b> once to establish a baseline, after
    which Update can tell the sides apart. Every skip used to be reported as
    "other side owns the change", which was untrue for two of the three and read
    as the app contradicting its own squares.</li>
<li><b>A blank field never overwrites a filled one.</b> VirtualDJ carries no
    comment for most tracks, so a sync from it was erasing comments that only
    Traktor had. A blank value now means "nothing to say" rather than "make it
    empty" — in every mode, Overwrite included. To clear a field, clear it in
    each program.</li>
<li><b>The blank side gets filled instead.</b> A blank that never overwrites left
    tracks permanently yellow: the engine would not act, but the comparison still
    counted the one-sided blank as a difference, so no amount of syncing cleared
    the square. A blank is missing data, so the value is now copied into whichever
    side lacks it — <i>including back into the library you synced from</i>. Nothing
    is ever erased, and the two genuinely converge, so green is earned rather than
    assumed. Update and Overwrite only: New Only means "add missing tracks, touch
    nothing else".</li>
<li><b>The musical key is translated, not copied.</b> Traktor stores Open Key
    ("8d"), VirtualDJ stores notation ("A#m"). Passing the text straight across
    put a value VirtualDJ cannot read into its Key field, so the key goes through
    a canonical number and each program is handed its own notation.</li>
<li><b>A sync fills an empty Key, and only an empty one.</b> VirtualDJ has no key
    at all for most tracks — 60,120 of 81,107 in one real library — so a sync now
    carries the analysed key into whichever side lacks it, in that side's own
    notation. A key both programs already hold is <i>never</i> touched, even when
    the two disagree: neither reading is ours to overrule, and the spellings
    differ harmlessly (VirtualDJ writes "Ebm" where we would write "D#m", which
    is the same note). Nothing can be lost, because this only ever adds.</li>
<li><b>A sync writes each collection file once, not once per track.</b> Every
    change is applied to one in-memory copy and saved at the end of the run. It
    is the same file either way — same edits, same order — but a 243-track
    playlist used to rewrite a 43 MB <code>database.xml</code> 118 times, which
    was 292 of that run's 329 seconds. If the app is killed mid-sync you lose
    that run's collection changes rather than a half-written file, and the backup
    is still taken before anything starts.</li>
<li><b>And it reads each collection once too.</b> A write has to <i>find</i> its
    entry, and finding one means looking at every entry in the file — so writing
    a thousand tracks one at a time meant a thousand passes over the whole
    collection. Now all of a run's changes are applied in a single pass, one per
    kind of change: cues, tags, key. On the reported 1005-track sync that is
    three passes instead of nine hundred and ninety-eight, and the run went from
    68 seconds to 4.</li>
<li><b>The same applies to editing tags across a selection.</b> Editing fifty
    tracks is one pass, not fifty.</li>
<li><b>A write that would corrupt the collection is refused.</b> The new content
    is checked before it replaces your file, so a bug in a write can no longer
    leave you with a <code>collection.nml</code> Traktor will not open. The
    original is left untouched and the sync reports the error.</li>
<li><b>Sync Overwrite:</b> forces target membership + cues to match source,
    after an explicit confirmation dialog.</li>
<li><b>A sync carries tags, not just cues.</b> Title, Artist, Album, Genre and
    Comment travel with the sync, so a tag you edit in one program can be pushed
    to the other and the track goes green. Before this they were <i>compared</i>
    but never <i>written</i>, so an edited comment stayed yellow forever whatever
    mode you used — the state had no way out.
    Check: edit a comment, sync, the track goes green.
    Automated: <code>tests/test_metadata_sync.py</code>.</li>
<li><b>A track the writer cannot find stays yellow.</b> If a sync cannot locate a
    track's entry in the target's collection file, its tags are not written —
    and the app says so rather than colouring the row green. Green always means
    "this matches on disk", never "we tried".</li>
<li><b>A track that differs only in Traktor's second comment field</b>
    (<code>COMMENT2</code>) will read as unsynced and cannot be fixed: VirtualDJ
    has nowhere to store one. It is deliberately <i>not</i> rewritten on every
    sync, which would never converge. If a track is stubbornly yellow with
    everything else matching, this is the thing to look at.</li>
<li><b>Every write backs up the target first</b> as
    <code>database_backup_YYYY_MM_DD_HHMMSS.xml</code> or
    <code>nml_backup_YYYY_MM_DD_HHMMSS.xml</code> next to the original.</li>
<li><b>Missing tracks get added to the target collection</b> from available
    metadata; once added + cue-synced they read green.</li>
<li><b>VirtualDJ-closed warning:</b> a Sync while VirtualDJ.exe is detected
    running prompts to confirm before writing, since VDJ rewrites database.xml
    on its own exit.</li>
<li><b>Verification performed during development:</b> a sandboxed round-trip
    using the bundled <code>collection.nml</code> / <code>test.vdjfolder</code>
    sample confirmed New/Update/Overwrite all behave as specified, backups are
    created every write, and apostrophe-bearing paths (<code>&amp;apos;</code>)
    survive the round trip untouched.</li>
</ul>

<h3>Paths &amp; persistence</h3>
<ul>
<li><b>Serato DJ</b>: point File → Configure paths… at <code>_Serato_\\database V2</code> and tick Programs → Serato DJ, with Serato closed. Crates sync like any playlist. <b>Cues are written into the audio files' own tags</b> (MP3, AIFF, WAV), only Serato's two frames, with the old ones saved under <code>backups/serato_tags/</code> first. The first load reads every file's cues and takes a couple of minutes; later loads only re-read files that changed. Cue timing is not calibrated yet.</li>
<li><b>Rekordbox works through rekordbox.xml</b>, never its own database. In
    Rekordbox, export the collection as XML (File → Export Collection in xml
    format), point File → Configure paths… at that file and tick Programs →
    Rekordbox. After a sync, import the file back in Rekordbox's "rekordbox
    xml" view. Its cue timing is not calibrated yet, so check a few cues by ear
    before trusting a large sync.</li>
<li><b>View menu → Light mode / Dark mode.</b> Check: switch to Dark — the
    window turns dark grey with lighter greys separating the panels, and the
    status squares stay legible; switch back to Light for the original look.
    The choice is remembered between sessions.</li>
<li><b>Paths menu → Configure paths…</b> lets you set database.xml,
    collection.nml, start_time_report.csv and the start_time source mode
    (cache/probe/update).</li>
<li><b>start_time_report.csv auto-creates</b> in the app directory if missing
    on first run.</li>
<li><b>Session state persists</b>: window geometry, splitter sizes, column
    order/width/sort, hidden fields, text scale, search scope, and the hidden
    playlist set — all restored on next launch.</li>
</ul>

<h3>Known limitations (carried over from the original CLI — see
HANDOFF.md)</h3>
<ul>
<li>An accented filename recorded in the other Unicode spelling (é as one
    character vs. e + accent mark) reads as a red/unfound track. Clean up…
    relinks it to the name actually on disk, without renaming the file.</li>
<li>The −51.536 ms correction is a fixed, ear-verified constant for this
    library's MP3 encoder-delay pattern — it is not re-derived per file.</li>
<li><b>Update and Overwrite mirror membership.</b> The target playlist is made
    to match the source exactly — including order — so a track dropped on the
    source side is dropped on the target, and a track the <i>other</i> program
    added to that playlist is deleted. The grey rows warn you which tracks
    those are before you sync.</li>
</ul>
"""


#: Controls that are **not** reassignable, because they are gestures rather than
#: commands -- there is nowhere in the Controls dialog to put "drag sideways".
#: Anything that CAN be rebound lives in config/controls.py instead; keeping the
#: two lists apart is what stops Help promising a key the dialog has changed.
FIXED_CONTROLS = [
    ("Playback", [
        ("1 – 8", "Jump to hotcue 1-8. Hold to preview: playback returns to "
                  "where it was when you let go."),
        ("W", "Play / pause."),
    ]),
    ("Navigation", [
        ("Left / Right", "Step to the previous / next playlist in the sidebar — "
                         "from anywhere, whether you last clicked a playlist, a "
                         "track or the waveform."),
        ("Up / Down", "Move down the track list — again from anywhere. The two "
                      "axes are split, so an arrow always means the same thing. "
                      "With nothing selected, either lands on the top track."),
        ("Double-tap Right", "On a folder, expand it. A single tap steps past it "
                             "instead, a moment later — the pause is the app "
                             "waiting to see whether a second tap is coming. How "
                             "long it waits is set under Controls."),
        ("Double-tap Left", "On a folder, collapse it."),
        ("Click a track", "Load its waveform. Declined while something is "
                          "playing, so browsing never cuts the audio."),
        ("Click a field on a selected track", "Edit it in place. The click that "
                                              "selects a track never opens an "
                                              "editor — it takes a second one."),
        ("Double-click a track", "Load it and start playing. Never starts an "
                                 "edit, however fast the two clicks are."),
        ("Alt + mouse wheel", "Scale the track table's font, rows and columns."),
    ]),
    ("Dragging", [
        ("Track → playlist", "Add it. Across programs you are asked once, and "
                             "any track missing from the target's collection is "
                             "created there first."),
        ("Track → another application", "Carries the file itself, so players "
                                        "and Explorer accept the drop."),
        ("File from Explorer → playlist", "Import it. A blue line shows exactly "
                                          "where it will land."),
        ("Playlist → Hidden", "Hide it. Unhide from the Hidden section's menu."),
        ("Mouse wheel mid-drag", "Scrolls the list you are hovering, with the "
                                 "button still held."),
        ("Hover a list edge mid-drag", "Scrolls, accelerating the longer you "
                                       "hold there."),
    ]),
]

#: Tutorial topics. ``gif`` names a file in ``Images/tutorial/``; when it is not
#: there the row shows a placeholder, so animations can be added one at a time
#: without touching this structure.
TUTORIAL_TOPICS = [
    ("Stepping through playlists",
     "Left and Right walk the sidebar. On a folder, tap Right twice quickly to "
     "open it.", "navigate.gif"),
    ("Positional drag and drop",
     "Drag a track — or a file from Explorer — and the blue line shows the exact "
     "position it will take in the playlist.", "drop-line.gif"),
    ("Editing metadata",
     "Right-click → Edit… for one track or a whole selection; or click a field "
     "on an already-selected row to edit it in place.", "edit-metadata.gif"),
    ("Syncing to everything at once",
     "Sync selected → All programs pushes a playlist to every active program in "
     "one action.", "sync-all.gif"),
]


def tutorial_dir() -> str:
    """Where the tutorial animations live. Not packaged as a resource, so they
    can be dropped in and replaced without a rebuild."""
    return paths_mod.image_path("tutorial")


def _controls_html() -> str:
    """The fixed controls, rendered from :data:`FIXED_CONTROLS` rather than
    written out twice."""
    parts = ["<h2>Built-in controls</h2>",
             "<p>These are gestures rather than commands, so the keys and "
             "buttons themselves cannot be reassigned. Everything that <i>can</i> "
             "be rebound — and how long the double-click and double-tap gestures "
             "have to complete — is under <b>Controls → Configure shortcuts…</b>, "
             "which writes your choices to <code>userControls.ini</code>.</p>"]
    for group, rows in FIXED_CONTROLS:
        parts.append(f"<h3>{group}</h3><table cellpadding='4'>")
        for keys, what in rows:
            parts.append(f"<tr><td valign='top'><b>{keys}</b></td>"
                         f"<td>{what}</td></tr>")
        parts.append("</table>")
    return "".join(parts)


class _TutorialPage(QWidget):
    """One scrollable column of topics, each with room for an animation.

    Built as real widgets rather than HTML because ``QTextBrowser`` will not
    animate a GIF -- and an animated demonstration is the whole point of the
    section, so the structure has to support one from the start.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._movies = []                 # held: a QMovie stops if collected
        body = QWidget()
        col = QVBoxLayout(body)
        col.addWidget(QLabel(
            "<p>A short demonstration of each gesture. More will be added.</p>"))
        for title, text, gif in TUTORIAL_TOPICS:
            heading = QLabel(f"<b>{title}</b>")
            col.addWidget(heading)
            blurb = QLabel(text)
            blurb.setWordWrap(True)
            col.addWidget(blurb)
            col.addWidget(self._animation(gif))
        col.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(scroll)

    def _animation(self, gif: str) -> QLabel:
        label = QLabel()
        path = os.path.join(tutorial_dir(), gif)
        if os.path.exists(path):
            movie = QMovie(path)
            self._movies.append(movie)
            label.setMovie(movie)
            movie.start()
        else:
            label.setText(f"<i>(animation to come — drop "
                          f"<code>{gif}</code> into Images/tutorial/)</i>")
            label.setStyleSheet("color: #888; padding: 4px 0 12px 0;")
        return label


_SYMBOL_SIZE = 16


def _symbols_html() -> str:
    """The legend, one table per kind of row. Images are referenced as
    ``status-<theme>:<id>`` and registered by :func:`_symbols_page`."""
    def table(rows):
        out = ["<table cellpadding='5' cellspacing='0'>",
               "<tr><th>Light</th><th>Dark</th><th align='left'>Meaning</th></tr>"]
        for status, name, meaning in rows:
            out.append(
                f"<tr><td align='center' valign='top'>"
                f"<img src='status-{theme_mod.LIGHT}:{status}'></td>"
                f"<td align='center' valign='top' bgcolor='#2b2b2b'>"
                f"<img src='status-{theme_mod.DARK}:{status}'></td>"
                f"<td valign='top'><b>{name}</b> — {meaning}</td></tr>")
        out.append("</table>")
        return "".join(out)

    return "".join([
        "<h2>Status symbols</h2>",
        "<p><b>Colour says what, shape says who.</b> Every status is measured "
        "against the <b>reference program</b> (Programs → Compare against).</p>",
        "<ul><li><b>Square</b> — this copy's relationship to the reference "
        "(synced, behind it, or not in it).</li>"
        "<li><b>Triangle</b> — this copy is the one that changed.</li>"
        "<li><b>Fork</b> (upside-down tuning fork) — some other program "
        "changed it.</li>"
        "<li><b>X</b> — an error, deliberately not shaped like a sync state."
        "</li></ul>",
        "<h3>Playlists</h3>",
        "<p>In the sidebar and the Needs sync pane.</p>",
        table(PLAYLIST_LEGEND),
        "<p><b>∞</b> after the square — a VirtualDJ playlist that allows the "
        "same track more than once.</p>",
        "<p><b>A yellow playlist can have identical tracks in identical order.</b> "
        "One track whose cues or tags differ is enough. The status bar counts "
        "them, and clicking the Status column header groups them together.</p>",
        "<h3>Tracks</h3>",
        "<p>In the Status column of a track list.</p>",
        table(TRACK_LEGEND),
    ])


def _symbols_page() -> QTextBrowser:
    """Help → Status symbols, drawn with the app's own painter in both skins,
    so the legend cannot drift from the artwork."""
    page = QTextBrowser()
    doc = page.document()
    for theme in (theme_mod.LIGHT, theme_mod.DARK):
        for status, _name, _meaning in PLAYLIST_LEGEND + TRACK_LEGEND:
            doc.addResource(
                QTextDocument.ImageResource, QUrl(f"status-{theme}:{status}"),
                status_pixmap(status, _SYMBOL_SIZE, theme).toImage())
    page.setHtml(_symbols_html())
    return page


class HelpWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Help — Feature Guide & Test Checklist")
        self.resize(760, 640)

        guide = QTextBrowser()
        guide.setOpenExternalLinks(True)
        guide.setHtml(_HTML)

        controls = QTextBrowser()
        controls.setHtml(_controls_html())

        self.tabs = QTabWidget()
        self.tabs.addTab(guide, "Feature guide")
        self.tabs.addTab(_symbols_page(), "Status symbols")
        self.tabs.addTab(controls, "Controls")
        self.tabs.addTab(_TutorialPage(), "Tutorial")

        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs)
