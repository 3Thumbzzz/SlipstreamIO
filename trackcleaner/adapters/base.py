"""The interface every supported DJ program implements.

One adapter owns everything format-specific about one program: how to read its
library, and how to write each kind of change to it. Nothing outside
``adapters/`` should branch on which program it is talking to -- if you find
yourself writing ``if source == VDJ`` somewhere else, the thing you need almost
certainly belongs here instead.

**Every write is text-surgical and returns a job, never performs one.** The
caller queues it on the serialized background writer, so an adapter method is
cheap to call on the main thread and the actual file edit happens off it. A job
is a plain zero-argument callable that must capture everything it needs -- it
runs on a worker thread and must not touch the model or any widget.

Adapters preserve what they do not understand. VirtualDJ's automix/``<Scan>``
points and Traktor's loop/fade/load markers survive every write byte-for-byte;
that is a binding safety rule of this codebase, not an implementation detail.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Optional

from ..model import keys
from ..model.library import Library, Playlist, TreeNode

# A queued write. Zero args, runs on the writer thread.
#
# A job may RETURN something, and some do: ``create_entry_job`` returns whether it
# had to create a brand-new collection entry. That answer comes from the writer
# actually looking at the file, which is why the caller reports it instead of
# guessing from the in-memory library. The write queue ignores return values, so
# a job that returns nothing is equally valid.
Job = Callable[[], object]


@dataclass(frozen=True)
class PathField:
    """One path the program needs, so the Paths dialog can build itself from the
    registered adapters instead of hard-coding two rows."""

    key: str                 # attribute name on Paths
    label: str               # shown in the dialog
    caption: str             # file-chooser caption
    file_filter: str         # e.g. "Traktor collection (*.nml)"
    # False for a file we create rather than find (SlipStream's own .slp), which
    # needs a save-style chooser -- an open dialog cannot name a file that is
    # not there yet.
    must_exist: bool = True


class LibraryAdapter:
    """Base class; subclasses fill in the format-specific parts.

    Subclasses are constructed with the app's ``Paths`` and read whatever they
    need from it. They hold no other state -- the libraries themselves live in
    the window, keyed by :attr:`id`.
    """

    id: str = ""                     # stable key: "traktor", "vdj", …
    display_name: str = ""           # shown in menus and dialogs
    # What this program calls the file its tracks live in, for menu wording like
    # "Remove from playlist + database.xml entry…". Named by the adapter so the
    # menus never have to guess it from the program.
    collection_label: str = "the collection"
    # This program's logo, as a filename inside ``Images/``. Named by the adapter
    # rather than derived from :attr:`id` because the supplied art does not follow
    # the ids ("vdjl.png" names no program), and a logo on the wrong pane is the
    # kind of mistake that ships quietly. Empty means no artwork: the watermark
    # and the sidebar icon both simply do not draw.
    logo_file: str = ""
    # Fixed per-program tab colours, moved off theme.py so a new program brings
    # its own rather than editing a shared table.
    tab_bg: str = "#8a8a8a"
    tab_fg: str = "#e8e8e8"
    tab_bg_inactive: str = "#5f5f5f"
    # True when the program rewrites its database on exit and can clobber our
    # edits, so a destructive write should warn if it is running.
    clobbers_on_exit: bool = False
    # True when a playlist is a FILE on disk, so removing one sends it to the
    # Recycle Bin (and the confirmation should say so).
    playlists_are_files: bool = False
    # True when this program has a per-playlist "allow duplicates" policy. Only
    # VirtualDJ does; the rest always allow repeats, so the menu entry and the
    # ∞ marker would be meaningless.
    supports_duplicate_policy: bool = False
    # False where a folder exists only by holding something. Serato's folders
    # are crates with crates below them, so a new empty folder is an empty crate
    # and reads back as a list until something is put inside it.
    holds_empty_folders: bool = True

    def __init__(self, paths):
        self.paths = paths
        # Set by the window once the cache exists; only adapters whose timeline
        # is offset from the audio need it.
        self.start_times = None
        # See collection_session(). None means "no session": every read and
        # write goes straight to disk, exactly as before.
        self._session: dict | None = None
        # Whole-file touches, for the performance log. Reset per sync run.
        self.collection_reads = 0
        self.collection_writes = 0

    # ------------------------------------------------ batched collection I/O
    # Every collection write in every adapter has the same shape: read the whole
    # file, hand the text to a pure ``str -> (str, changed)`` mutation, write it
    # back atomically. That is fine once. It is not fine 118 times in one sync,
    # which is what a 243-track playlist cost on a real 43 MB database.xml --
    # 292 of the run's 329 seconds, because each rewrite re-reads the file, re-runs
    # the regex over it and re-parses the whole document to validate it.
    #
    # A session keeps the text in memory for the length of a sync so those
    # mutations compose, and writes once at the end. The mutations themselves are
    # untouched: same regexes, same order, same result -- which is why the
    # batched output is byte-identical to the unbatched one.
    def _read_collection_file(self, path: str):
        """Load one collection from disk. Text by default; an adapter whose
        writes work on a parsed document overrides both this and
        :meth:`_write_collection_file` so its session buffers that instead.

        These two are the only place a collection is really touched, so they are
        also where the whole-file counters live -- counted here rather than at
        the call sites so the number stays true whether a session is open or not.
        """
        self.collection_reads += 1
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

    def _write_collection_file(self, path: str, content) -> None:
        self.collection_writes += 1
        from ..io.write_queue import atomic_write_xml
        atomic_write_xml(path, content)

    def _load_collection(self, path: str):
        """This collection's contents: from the session buffer if one is open,
        else from disk."""
        if self._session is not None and path in self._session["value"]:
            return self._session["value"][path]
        content = self._read_collection_file(path)
        if self._session is not None:
            self._session["value"][path] = content
        return content

    def _store_collection(self, path: str, content) -> None:
        """Write it back -- to the session buffer if one is open, else straight
        to disk through the same validating atomic write."""
        if self._session is not None:
            self._session["value"][path] = content
            self._session["dirty"].add(path)
            return
        self._write_collection_file(path, content)

    @contextmanager
    def collection_session(self):
        """Hold this adapter's collection in memory for the block, and write it
        once on the way out.

        The flush is in a ``finally``: an error part-way through a sync leaves
        the work that did succeed on disk, which is what happened before when
        each track wrote as it went. It goes through the same
        :func:`atomic_write_xml`, so the "never replace the file with something
        that does not parse" guard is unchanged -- there is simply one of them
        per run instead of one per track.

        Nesting is a no-op, so a caller can wrap a block that already wraps
        itself (``sync_track`` inside a playlist sync) without writing twice.
        """
        if self._session is not None:
            yield self                      # already inside one; let it own the flush
            return
        self._session = {"value": {}, "dirty": set()}
        try:
            yield self
        finally:
            session, self._session = self._session, None
            for path in sorted(session["dirty"]):
                self._write_collection_file(path, session["value"][path])

    # ------------------------------------------------------------ the key
    @classmethod
    def display_key(cls, track) -> str:
        """This track's musical key **in this program's own notation**.

        Rendered from the canonical value rather than shown as stored, because
        the stored text is not reliable: of 311 entries in a real
        ``collection.nml``, 87 hold Open Key, 3 hold musical notation and 3 hold
        nothing at all beside an analysed ``MUSICAL_KEY``. Traktor itself
        displays from that number, so showing its ``INFO/@KEY`` text put a
        different key on screen from the one the user sees in Traktor.

        Falls back to the stored text, which is all a track with no analysed key
        has.
        """
        return keys.musical_key(getattr(track, "key_value", None)) or             getattr(track, "key_musical", "")

    def session_writes_pending(self) -> int:
        """How many files this adapter's open session would write. Zero outside
        a session -- there is nothing buffered to write."""
        return 0 if self._session is None else len(self._session["dirty"])

    # -------------------------------------------------------- the timeline
    # Canonical ("native") position = seconds from the first decoded audio
    # sample. Each program says only how ITS stored numbers relate to that, so
    # converting between two programs is source -> native -> target and no
    # adapter needs to know another one exists. That is what replaces the old
    # pairwise Traktor<->VirtualDJ correction.
    def timeline_offset(self, stored_path: str) -> float:
        """Seconds to ADD to one of this program's stored positions to get the
        canonical one. Default 0: the program's numbers are already true audio
        time. Per-file, because whether a delay is honoured is a property of the
        audio container, not of the database (see HANDOFF.md, "Cue timing")."""
        return 0.0

    def to_native_pos(self, pos: float, stored_path: str) -> float:
        return max(pos + self.timeline_offset(stored_path), 0.0)

    def from_native_pos(self, pos: float, stored_path: str) -> float:
        return max(pos - self.timeline_offset(stored_path), 0.0)

    # ---------------------------------------------------------------- config
    def path_fields(self) -> list[PathField]:
        raise NotImplementedError

    def is_configured(self) -> bool:
        raise NotImplementedError

    @property
    def collection_path(self) -> str:
        """The file holding this program's track collection.

        What a backup snapshots before a sync writes. Named by the adapter so
        callers never have to map a program id onto ``paths.nml`` /
        ``paths.xml`` themselves.
        """
        return ""

    def is_running(self) -> bool:
        """Whether the program is open right now. Only meaningful when
        :attr:`clobbers_on_exit`; defaults to "can't tell"."""
        return False

    # ------------------------------------------------------------------ read
    def read(self, yield_fn=None) -> Library:
        raise NotImplementedError

    # ------------------------------------------------- playlists and folders
    def new_folder_job(self, node_path: tuple) -> Job:
        """Create an empty folder at ``node_path``."""
        raise NotImplementedError

    def new_playlist(self, name: str, node_path: tuple, tracks: list,
                     copy_of: Optional[TreeNode],
                     no_duplicates: Optional[bool] = None) -> tuple[Playlist, Job]:
        """Build the model ``Playlist`` for a new list plus the job that writes
        it. Returned together because the two share program-specific state --
        a Traktor UUID, a VirtualDJ file path -- that the caller must not invent.

        ``no_duplicates`` overrides the adapter's own default for a new list.
        The sync engine sets it False for playlists it creates, which is the
        opposite of what a hand-made one gets; both are deliberate, so the
        choice has to be the caller's to state. Adapters whose format has no
        such flag ignore it.
        """
        raise NotImplementedError

    def relocate_playlist_job(self, node: TreeNode, new_path: tuple,
                              lib: Library) -> Optional[Job]:
        """Move and/or rename a playlist. ``None`` when it cannot be done (and
        the adapter has said why via :meth:`last_error`)."""
        raise NotImplementedError

    def relocate_folder_job(self, old_path: tuple, new_path: tuple) -> Optional[Job]:
        raise NotImplementedError

    def remove_job(self, node: TreeNode) -> Optional[Job]:
        raise NotImplementedError

    # -------------------------------------------------------- membership
    def write_membership_job(self, node: TreeNode, tracks: list) -> Job:
        """Rewrite ``node``'s playlist to exactly ``tracks``, in order."""
        raise NotImplementedError

    def sanitize_node_name(self, name: str) -> str:
        """Make ``name`` safe for this program to store a playlist under.

        Only matters where a playlist is a file: a name is then a filename, and
        the characters a filesystem refuses are not the ones XML refuses. Formats
        that keep playlists inside a document accept anything, hence the default.
        """
        return name

    def can_write_membership(self, node: TreeNode) -> bool:
        """Whether this node can actually be addressed for a membership write.

        Traktor matches playlists by UUID, so a node that somehow has none
        cannot be written to -- and a sync must create the playlist instead of
        silently writing nowhere. Formats that address playlists by path always
        can, hence the default.
        """
        return True

    # -------------------------------------------------- collection entries
    # The plural forms are the real ones. A write has to walk the collection to
    # find the entry it wants, so doing N of them one at a time is N walks of the
    # whole document -- 46.6 of one 1005-track run's 110.9 seconds. The singular
    # forms below are one-item wrappers, kept so a caller with a single track
    # (the sidebar, a context menu) reads as it always did.
    def create_entries_job(self, tracks) -> Job:
        """Write ``tracks`` into the collection in **one pass**, creating the
        entries that are not there and updating the ones that are.

        Running the job returns ``{key: created}`` -- the writer's own answer per
        track, which is what a sync reports rather than the in-memory guess.
        """
        raise NotImplementedError

    def write_metadata_jobs(self, by_key: dict, paths: dict | None = None) -> Job:
        """Set text metadata on many entries in one pass. ``by_key`` maps a track
        key to its own ``fields`` dict, so one call covers a multi-track edit
        where the tracks differ in what changed. Returns ``{key: changed}``.

        ``paths`` maps a key to the file it lives at. **Optional, and purely an
        accelerator**: a caller that supplies it lets the writer *locate* each
        entry instead of walking the whole collection -- 2.29 s of a backfilling
        sync on a 133.5 MB one. A caller that has no path is not doing anything
        wrong, and formats that hold a parsed tree ignore it entirely."""
        raise NotImplementedError

    def write_playcount_jobs(self, by_key: dict) -> Job:
        """Set the play count on many entries, touching nothing else -- not the
        cues, which is how a sync otherwise carries one. ``by_key`` maps a track
        key to its count. Returns ``{key: changed}``. A program that has not
        implemented it reports every key unchanged."""
        return lambda: {k: False for k in by_key}

    def key_fields(self, key_value: int) -> dict:
        """The attributes that spell canonical key ``key_value`` (0..23) in
        **this program's own notation** -- Traktor "8m", VirtualDJ "A#m".

        Split out from the write so a caller that is already writing metadata can
        fold the key into the same pass. Two passes over a 134 MB collection to
        set a tag and a key on the same track is one pass too many; ~2.1 s of a
        run went on the second one.
        """
        raise NotImplementedError

    def write_key_jobs(self, by_key: dict, paths: dict | None = None) -> Job:
        """Store the canonical musical key on many entries in one pass.
        ``by_key`` maps a track key to its 0..23 value. Returns
        ``{key: changed}``.

        A key write *is* a metadata write with program-specific attributes, so
        this is the metadata pass with :meth:`key_fields` in front of it -- which
        is what lets the engine merge the two into a single walk.
        """
        return self.write_metadata_jobs(
            {k: self.key_fields(v) for k, v in by_key.items()}, paths)

    def create_entry_job(self, track) -> Job:
        """Write ``track`` into the collection, creating the entry if it is not
        there and updating it if it is. Running the job returns **True when a
        new entry had to be created** -- the writer's own answer, which is what
        a sync reports rather than the in-memory guess."""
        job = self.create_entries_job([track])
        return lambda: bool(job()[track.key])

    def remove_entry_job(self, key: str) -> Job:
        raise NotImplementedError

    def read_tracks(self, by_key: dict) -> dict:
        """``{key: Track}`` read fresh from the collection, for these keys only.

        ``by_key`` maps a track key to the path it is believed to live at, which
        is what lets the entry be *located* rather than the document walked. A
        key missing from the result is not in the collection.

        This is what makes a targeted refresh honest. After a write, the app has
        to decide what colour the track is now, and the alternative -- believing
        the in-memory copy -- is the assumption the whole reload existed to
        avoid. Reading the entry back means the square still describes what is
        actually stored, at the cost of about 2 KB instead of the 178 MB a full
        reload re-parses.

        Cheap because the file read is not the expensive part: pulling 133 MB off
        disk takes 0.04 s, while parsing its 83,427 entries takes 11 s. Reading
        two of them takes neither.
        """
        raise NotImplementedError

    def repath_tracks_job(self, by_key: dict) -> Job:
        """Point many entries at a new file path, in **one pass**.

        ``by_key`` maps a track's current key to its new absolute path. Running
        the job returns ``{old_key: relinked}``.

        This is what follows a file being renamed on disk (see
        ``model/cleanup.py``): the audio moved, so every place this program
        records where it lives has to move with it -- the collection entry *and*
        every playlist that names the track. A program that stores membership by
        path rather than by an entry reference has to rewrite both or the track
        vanishes from its playlists.

        The track's identity changes with its path, so a caller re-reads the
        library afterwards rather than trying to patch keys in memory.
        """
        raise NotImplementedError

    #: The text fields a user may edit. BPM and musical key are deliberately not
    #: here: they are each program's own analysis output, and changing the BPM
    #: without moving the beatgrid desynchronises the cues that model/cues.py
    #: exists to keep aligned.
    EDITABLE_FIELDS = ("title", "artist", "album", "genre", "comment")

    def write_metadata_job(self, key: str, fields: dict,
                           path: str = "") -> Job:
        """Set text metadata on one collection entry.

        ``fields`` maps any of :attr:`EDITABLE_FIELDS` to its new value. A field
        **absent from the dict is left alone** -- that is what lets an edit
        across several tracks change only the fields the user typed into and
        leave every ``<various>`` one at each track's own value.

        Like every other write here it is text-surgical: only those attributes
        move, so cues, automix points and analysis data survive untouched.

        **The job returns True only when an entry actually matched and the file
        was rewritten.** Callers must not update their in-memory copy on a False:
        a track the writer could not find has to keep reading as unsynced, or the
        app shows green for something that never reached disk.
        """
        job = self.write_metadata_jobs({key: fields},
                                       {key: path} if path else None)
        return lambda: bool(job()[key])

    def write_key_job(self, key: str, key_value: int, path: str = "") -> Job:
        """Store the canonical musical key (0..23) on one collection entry, in
        **this program's own notation**.

        Separate from :meth:`write_metadata_job` on purpose: the key is analysis
        output, so it is not in :attr:`EDITABLE_FIELDS` and the metadata dialog
        must not be able to reach it -- but a sync does need to carry it, and
        each program spells it differently (Traktor "8m", VirtualDJ "A#m").
        Rendering that spelling is exactly the kind of per-program rule that
        belongs behind this interface.

        Returns True only when an entry matched, like every other write here.
        """
        job = self.write_key_jobs({key: key_value},
                                  {key: path} if path else None)
        return lambda: bool(job()[key])

    # ----------------------------------------------------------------- misc
    def last_error(self) -> str:
        """Why the most recent ``*_job`` returned ``None``. Lets the caller
        report a program-specific reason without knowing what programs exist."""
        return getattr(self, "_last_error", "")

    def _fail(self, message: str) -> None:
        self._last_error = message
