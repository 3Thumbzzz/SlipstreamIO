"""Main application window.

Menu bar (File · View · Skin · Help), a waveform panel on top, then a
Refresh/Search row above the playlist trees (left) and a tabbed, splittable
track area (right). Hosts the diff/status, sync engine, whole-state backups
(on open / Ctrl-S / autosave, with a busy overlay), file-integrity checks, and
on-end auto-advance. Playlist-editing writes (remove/delete/reorder/import)
land in later phases; the P1 context-menu entries for those are present but
disabled.
"""

from __future__ import annotations

import gc
import os
import random
from datetime import datetime

from PySide6.QtCore import QEvent, QEventLoop, Qt, QTimer
from PySide6.QtGui import (QAction, QActionGroup, QGuiApplication, QKeyEvent,
                           QKeySequence, QMouseEvent, QShortcut)
from PySide6.QtWidgets import (QAbstractSlider, QAbstractSpinBox,
                               QApplication, QComboBox, QDialog,
                               QDialogButtonBox, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPushButton, QSlider, QSplitter, QToolButton,
                               QVBoxLayout, QWidget)

from ..adapters import build_adapters
# Imported as a MODULE, not by name: tests redirect app_dir/baseline_path to a
# tmp dir with monkeypatch, and a from-import binds the original function here at
# load time so the redirect silently misses. That is not a test-tidiness point --
# it means a test run reads and rewrites the real sync_baseline.csv, which is the
# user's change-attribution history and decides what a sync overwrites.
from ..config import controls as controls_mod
from ..config import paths as paths_mod
from ..config.paths import Settings, ensure_start_time_csv
from ..model import baseline as bl_mod
from ..model.baseline import Baseline
from ..config import state as state_mod
# No format reader or writer here on purpose: reading and writing a program's
# files goes through its adapter, and this was the only import in the tree that
# said otherwise (it was unused). See ARCHITECTURE.md.
from ..io import backup, reveal, synclog
from ..io.vdj_process import is_vdj_running as _vdj_running
from ..io.write_queue import WriteQueue
from ..model import cleanup
from ..model import matching
from ..model import status as status_mod
from ..model.cues import StartTimeCache
from ..model.library import Library, Track, TreeNode, VDJ, TRAKTOR, path_ci
from ..sync.engine import (NEW, OVERWRITE, SKIP_CONFLICT,
                           SKIP_TARGET_OWNS, SKIP_UNKNOWN,
                           SyncEngine, UPDATE)
from . import context_menus
from .cleanup_dialog import CleanupDialog
from .controls_dialog import ControlsDialog, mouse_event_binding
from .metadata_dialog import MetadataDialog
from . import drag_scroll
from .controllers.collection import CollectionController
from .controllers.membership import MembershipController
from .controllers.structure import StructureController
from .controllers.views import ViewController
from . import waveform_panel as wf
from .busy_overlay import BusyOverlay
from .fields import ALL_COLUMN_IDS, LABEL_BY_ID, STATUS_COL
from .help_window import HelpWindow
from .tap_detect import DoubleTapDetector
from .paths_dialog import PathsDialog
from .playlist_trees import PlaylistPanel
from .playlists_window import PlaylistsWindow
from .sync_pane import SyncPane, needs_sync
from .track_tabs import TrackTabs
from .waveform_panel import WaveformPanel
from . import theme as theme_mod
from . import watermark

_MODE_LABELS = {NEW: "Sync New Only", UPDATE: "Sync Update", OVERWRITE: "Sync Overwrite"}


def _default_paths(settings: Settings):
    p = settings.paths
    if not p.slp:
        # Ours, so it just lives beside the app; nothing to go looking for.
        p.slp = os.path.join(paths_mod.app_dir(), "library.slp")
    if not p.xml:
        cand = r"D:\VirtualDJ\database.xml"
        if os.path.exists(cand):
            p.xml = cand
    if not p.nml:
        for cand in (os.path.join(paths_mod.app_dir(), "collection.nml"),
                     r"D:\VirtualDJ\traktor.nml",
                     r"C:\Users\Tententen\Documents\Native Instruments\Traktor 3.5.1\collection.nml"):
            if os.path.exists(cand):
                p.nml = cand
                break


def _mtime(path: str):
    """File modification time, or None when the path is missing/unreadable."""
    try:
        return os.path.getmtime(path) if path else None
    except OSError:
        return None


def _mtime_str(path: str) -> str:
    ts = _mtime(path)
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "missing"


def transport_keys_blocked() -> bool:
    """True while a text-entry widget has focus.

    Digits and "w" are ordinary things to type in the Search box, so the
    transport keys (1-8, W) must stand down rather than firing hotcues and
    playback at whoever is filtering the track list.
    """
    return isinstance(QApplication.focusWidget(), (QLineEdit, QComboBox))


def route_transport_key(event, panel) -> bool:
    """Send 1-8 and W to ``panel``. Returns True when the event is consumed.

    This has to run in an application-wide event filter, not a window
    ``keyPressEvent``: both selection views run type-ahead search and eat
    printable keys first -- a QTreeWidget swallows digits *and* "w", and the
    track table swallows "w" -- so the window never sees them. Consuming the
    event here also stops type-ahead jumping the selection to a matching row.
    """
    # Check the event *class* before its type: an application-wide filter sees
    # every event Qt produces, and PySide6 raises on .type() for the internal
    # ones whose value falls outside the QEvent.Type enum.
    if not isinstance(event, QKeyEvent):
        return False
    etype = event.type()
    if etype not in (QEvent.KeyPress, QEvent.KeyRelease):
        return False
    # Auto-repeat would re-trigger a hotcue continuously and desynchronise the
    # press/release bookkeeping behind hold-to-preview.
    if event.isAutoRepeat() or transport_keys_blocked():
        return False
    if QApplication.activeModalWidget() is not None:
        return False                                 # a dialog owns the keyboard
    # The transport keys are the BARE digits and "w". Without this the filter
    # claimed Ctrl+W and Ctrl+1..8 too -- swallowing them application-wide, so
    # any shortcut using one would silently never fire while secretly toggling
    # playback or firing a hotcue instead.
    if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
        return False

    key = event.key()
    if Qt.Key_1 <= key <= Qt.Key_8:
        cue = key - Qt.Key_0
        if etype == QEvent.KeyPress:
            panel.cue_pressed(cue)
        else:
            panel.cue_released(cue)
        return True
    if key == Qt.Key_W:
        if etype == QEvent.KeyPress:
            panel.toggle_play()
        return True                                  # also eat the release
    return False


#: Widgets the arrow keys belong to rather than to navigation: text entry, where
#: they move a caret, and a combo box, where they change the value.
#: ``QLineEdit`` covers both the search box and an open cell editor.
#:
#: **Sliders are deliberately not here.** Qt hands focus to the tempo fader as
#: soon as the window is shown, so including ``QAbstractSlider`` meant the arrows
#: stopped navigating anything at all until something else was clicked -- while
#: the fader's own arrow-key nudging is undocumented and is not how anyone uses
#: it. The arrows navigate universally; that was the point of routing them.
ARROW_OWNING_WIDGETS = (QLineEdit, QComboBox)


def arrow_keys_blocked() -> bool:
    """True while some widget legitimately owns the arrow keys."""
    return isinstance(QApplication.focusWidget(), ARROW_OWNING_WIDGETS)


def route_arrow_key(event, window) -> bool:
    """Left/Right walk the sidebar, Up/Down walk the track list -- from anywhere.

    Application-wide for the same reason the transport keys are: whichever view
    has focus handles arrows itself first, so the window never sees them. That is
    exactly the reported bug -- clicking a playlist puts focus in the tree, and
    Left/Right then did nothing at all while Up/Down moved the *playlist*
    selection instead of the track list.

    Consuming them is the point, not a side effect: it is what stops the tree
    navigating on Up/Down.
    """
    if not isinstance(event, QKeyEvent):
        return False
    if event.type() != QEvent.KeyPress:
        return False
    # Auto-repeat would fire the double-tap gesture continuously from one held
    # key -- the same reason the transport keys ignore it.
    if event.isAutoRepeat() or arrow_keys_blocked():
        return False
    if QApplication.activeModalWidget() is not None:
        return False                                 # a dialog owns the keyboard
    if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
        return False                                 # Ctrl+Left is Previous track
    key = event.key()
    if key in (Qt.Key_Left, Qt.Key_Right):
        window.step_playlist(1 if key == Qt.Key_Right else -1)
        return True
    if key in (Qt.Key_Up, Qt.Key_Down):
        window.step_track_row(1 if key == Qt.Key_Down else -1)
        return True
    return False


def route_mouse_binding(event, bindings) -> bool:
    """Fire a bound mouse button. ``bindings`` maps (modifiers, button) -> handler.

    **Modifiers are matched exactly.** A bare left click must never fire a
    ``Ctrl+Mouse:Left`` binding -- that is the trap the transport filter fell
    into when it claimed the bare digits regardless of modifiers and silently ate
    Ctrl+W.
    """
    if not bindings or not isinstance(event, QMouseEvent):
        return False
    if event.type() != QEvent.MouseButtonPress:
        return False
    if QApplication.activeModalWidget() is not None:
        return False
    parsed = mouse_event_binding(event)
    handler = bindings.get(parsed) if parsed is not None else None
    if handler is None:
        return False
    handler()
    return True


#: The Debug menu's toggles: (id, menu label, tooltip). Ids are what persist in
#: ``ui_state`` and what name the log files, so they may not be reworded; the
#: labels may.
DEBUG_LOG_KINDS = (
    ("sync_update", "Log playlist sync — Update",
     "Write a detailed log of every Update sync to the logs folder"),
    ("sync_overwrite", "Log playlist sync — Overwrite",
     "Write a detailed log of every Overwrite sync to the logs folder"),
    ("track_sync", "Log track sync",
     "Write a detailed log of every single-track sync to the logs folder"),
    ("performance", "Log sync performance",
     "Add timings and whole-file read/write counts to whichever logs are on"),
    ("parse_checks", "Verify collection parses — slow",
     "Re-parse the whole collection before and after a sync to prove the run "
     "did not break it. Adds ~2 s per run on a large library"),
)

#: Not a kind of its own: it adds a section to the logs the other toggles
#: produce. On its own it writes nothing, because there is no file to add it to.
PERF_LOG_KIND = "performance"

#: Also a modifier rather than a kind. Off by default because two whole-document
#: parses cost 2.3 s of a 5 s single-track sync -- see ``SyncLog.want_parse_checks``.
PARSE_CHECK_KIND = "parse_checks"

#: Which toggle covers a playlist sync of each mode. NEW is deliberately absent:
#: the reported faults are in Update and Overwrite, and a toggle nobody asked for
#: is one more thing to leave switched on by accident.
_MODE_LOG_KIND = {UPDATE: "sync_update", OVERWRITE: "sync_overwrite"}


class _CB:
    """Lightweight callback bag for the context-menu builders."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        # One adapter per known program. Everything format-specific goes through
        # these, so no UI or controller code branches on which program it is.
        # Built first: the per-program dicts below key off it.
        self.adapters = build_adapters(settings.paths)
        # One entry per program, keyed by adapter id. A program that is switched
        # off for the session simply has no entry, so callers use .get().
        self.libraries: dict[str, Library] = {}
        self.reports: dict[str, status_mod.Report] = {}
        self._current_source = VDJ
        self._current_nodes: list[TreeNode] = []
        # Playlists edited since the last full diff, as (source, path_ci(path)).
        # Their sidebar square is overlaid yellow ("needs update") without a
        # re-diff, until the next Refresh/Sync reconciles and clears this.
        # {playlist path_ci: the program that edited it}. Keyed by path, not
        # by (program, path): an edit makes EVERY program's copy stale, and
        # each one's glyph depends on who the editor was.
        self._edited_paths: dict = {}
        # The reference the last diff actually used. The overlay needs it before
        # any diff has run, so it falls back to reference_program().
        self._reference_in_use: str = ""
        # What an in-place "New …" edit is building. The commit signal only
        # carries (source, parent, name), so the kind -- and the playlist a
        # Duplicate is copying -- are remembered here between begin and commit.
        self._pending_create: dict = {"kind": "playlist", "copy_of": None}
        self.start_time_cache: StartTimeCache | None = None
        # hidden node paths per source, restored from state
        st = settings.ui_state or {}
        hp = st.get("hidden_playlists", {})
        self._hidden = {aid: {tuple(p) for p in hp.get(aid, [])}
                        for aid in self.adapters}
        # Which programs this session uses. Unknown ids from an older session
        # are dropped; an empty result falls back to all, because a window with
        # no programs at all has nothing to show and no way back.
        saved = [a for a in (st.get("enabled_programs") or []) if a in self.adapters]
        self._enabled = saved or list(self.adapters)
        # Which program everything else's squares are compared against. Defaults
        # to Traktor, whose collection.nml is the one most users treat as their
        # master; validated against what is actually loaded by
        # reference_program(), so a stale or switched-off choice can't leave the
        # session diffing against nothing.
        ref = st.get("reference_program")
        self._reference = ref if ref in self.adapters else TRAKTOR
        self._reference_actions = {}
        # How strongly each pane shows its program's logo. Read here rather than
        # in _restore_state because tables created during __init__ need it before
        # their first paint, or the watermark would flash in at the default.
        self._watermark_opacity = self._clean_opacity(
            st.get("watermark_opacity", watermark.DEFAULT_OPACITY))
        # Gesture timings. Defaults stand in until the file is read further down;
        # tables built during __init__ ask for them before that happens.
        self._timing = dict(controls_mod.TIMING_DEFAULTS)
        # What has actually been played this session, and where Previous/Next
        # currently sit in it. Not persisted: "the track before this one" only
        # means anything within a listening session.
        self._play_history: list[str] = []
        self._history_pos = -1
        # The Left/Right folder gesture. One detector for the whole window rather
        # than one per pane: the arrows are routed application-wide, so several
        # detectors would each see a different part of the same gesture.
        self._taps = DoubleTapDetector()
        self._pending_step: int | None = None
        self._step_timer = QTimer(self)
        self._step_timer.setSingleShot(True)
        self._step_timer.timeout.connect(self._flush_pending_step)
        self._mouse_bindings: dict = {}
        # Which diagnostic logs are switched on. Persisted because the machine
        # gathering them gets restarted between runs.
        self._debug_logs = {k for k in (st.get("debug_logs") or ())
                            if k in {kind for kind, _l, _h in DEBUG_LOG_KINDS}}
        #: Logs this run could not write, and why. Drained by _logged_note.
        self._log_failures: list[str] = []

        # Controllers carved out of this class. Constructed before _build_ui so
        # its signal wiring can connect straight to them. See
        # ui/controllers/__init__.py for the back-reference rationale.
        self.collection = CollectionController(self)
        self.membership = MembershipController(self)
        self.structure = StructureController(self)
        self.views = ViewController(self)

        self.setWindowTitle("SlipStreamIO — Traktor ⇄ VirtualDJ")
        self.resize(1280, 760)
        self._font_pt = 9
        self._hidden_fields: set[str] = set()   # global column visibility
        # {program: {track keys whose file is missing}} -- the red overlay.
        # One entry per registered program, not a hardcoded pair.
        self._missing_keys = {aid: set() for aid in self.adapters}
        self._track_changed_by: dict = {}
        self._build_ui()
        # Apply the session's program choice before the first load, so a
        # switched-off program is never read or shown even briefly.
        self.playlist_panel.set_enabled_programs(self._enabled)
        _default_paths(self.settings)
        ensure_start_time_csv(self.settings)
        self.load_libraries()
        # Assignable shortcuts. Loaded before the UI is wired so a binding is
        # live from the first keypress, not after the first dialog visit.
        self._controls = controls_mod.load()
        self._shortcuts = []
        self.apply_controls()
        self.apply_timing(controls_mod.load_timing())
        self._restore_state()
        # A fresh snapshot every launch, so each session starts from a safe point.
        self._save_backups(quiet=True)

    # -------------------- UI construction --------------------
    def _build_ui(self):
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(4, 4, 4, 4)

        # Waveform on top; the Refresh/Search row sits below it, directly above
        # the playlist trees and track table.
        self.waveform_panel = WaveformPanel()
        self.v_splitter = QSplitter(Qt.Vertical)
        self.v_splitter.addWidget(self.waveform_panel)

        self.h_splitter = QSplitter(Qt.Horizontal)
        # The playlist trees now own the whole left column (the metadata field
        # checkboxes moved into the View → Columns menu).
        self.playlist_panel = PlaylistPanel(self.adapters)
        self.h_splitter.addWidget(self.playlist_panel)
        # The "needs sync" worklist, beside the trees it is derived from. Off
        # until View turns it on; while off it is never refreshed, so it costs
        # nothing.
        self.sync_pane = SyncPane()
        self.sync_pane.setVisible(False)
        self._sync_pane_on = False
        self.h_splitter.addWidget(self.sync_pane)

        self.track_tabs = TrackTabs(
            on_clicked=self._on_track_clicked,
            on_double=self._on_track_double_clicked,
            on_context=self._on_track_context,
            on_created=self._init_table,
            on_column_toggled=self._on_header_column_toggled,
            on_tracks_dropped=self._on_tracks_dropped_on_table,
            on_font_changed=self._on_font_changed,
            on_tracks_reordered=self._reorder_playlist,
            on_files_dropped=self._on_files_dropped_on_table,
        )
        self.h_splitter.addWidget(self.track_tabs)
        self.h_splitter.setSizes([340, 0, 940])

        lower = QWidget()
        lower_lay = QVBoxLayout(lower)
        lower_lay.setContentsMargins(0, 0, 0, 0)
        lower_lay.setSpacing(4)
        lower_lay.addLayout(self._build_topbar())
        lower_lay.addWidget(self.h_splitter, 1)
        self.v_splitter.addWidget(lower)
        self.v_splitter.setSizes([150, 610])
        self.v_splitter.setStretchFactor(1, 1)
        outer.addWidget(self.v_splitter, 1)

        self.setCentralWidget(central)
        self.overlay = BusyOverlay(self)
        self._writer = WriteQueue(self)   # serialized, atomic background file writes
        # Guards the cooperative main-thread reload (parse+diff yields to the
        # event loop so audio keeps playing; input is excluded so it can't nest).
        self._reloading = False
        self._build_menu()
        QApplication.instance().installEventFilter(self)

        # wiring
        self.playlist_panel.playlists_selected.connect(self._on_playlists_selected)
        self.playlist_panel.node_context_requested.connect(self._on_node_context)
        self.playlist_panel.blank_context_requested.connect(self._on_blank_context)
        self.playlist_panel.new_playlist_committed.connect(self._create_node)
        self.playlist_panel.rename_committed.connect(self._rename_node)
        self.playlist_panel.node_move_requested.connect(self._move_nodes)
        self.playlist_panel.hide_requested.connect(self._hide_selected)
        self.playlist_panel.unhide_requested.connect(self._unhide)
        self.playlist_panel.tracks_dropped.connect(self._on_tracks_dropped_on_sidebar)
        self.playlist_panel.files_dropped.connect(self._on_files_dropped_on_sidebar)
        self.sync_pane.selection_changed.connect(self._on_sync_pane_selection)
        self.sync_pane.node_context_requested.connect(self._on_sync_pane_context)
        self.sync_pane.include_missing_changed.connect(
            lambda _on: self._refresh_sync_pane())
        self.sync_pane.songs_selected.connect(self._on_sync_pane_songs)
        self.sync_pane.song_context_requested.connect(self._on_sync_pane_song_context)
        self.track_tabs.focused_changed.connect(self._on_tab_focus_changed)
        self.waveform_panel.track_finished.connect(self._auto_advance)
        self.waveform_panel.track_unreadable.connect(self._on_track_unreadable)
        self.waveform_panel.next_requested.connect(lambda: self._step_track(1))
        self.waveform_panel.previous_requested.connect(lambda: self._step_track(-1))
        self.waveform_panel.title_context_requested.connect(
            self._on_playback_title_context)
        # Search widens to the focused tab's whole library; debounce so a big
        # (81k-track) scan doesn't run on every keystroke, and cache a per-track
        # search blob per library (cleared on reload).
        self._search_cache: dict = {}
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._run_search)
        self.search_edit.textChanged.connect(lambda *_: self._search_timer.start())
        self.search_scope.currentTextChanged.connect(lambda *_: self._search_timer.start())

        # autosave backups (0 = off), armed from the persisted setting
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(lambda: self._save_backups(quiet=True))
        self._apply_autosave_interval(self.settings.backup_autosave_minutes)

        self.status_label = QLabel("")
        self.statusBar().addPermanentWidget(self.status_label)

    def _build_topbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        self.refresh_btn = QPushButton("⟳ Refresh")
        self.refresh_btn.setToolTip("Re-diff both libraries to update all status squares")
        self.refresh_btn.clicked.connect(self.refresh)
        bar.addWidget(self.refresh_btn)

        bar.addWidget(QLabel("  Search:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("filter tracks…")
        self.search_edit.setClearButtonEnabled(True)
        bar.addWidget(self.search_edit, 1)
        self.search_scope = QComboBox()
        self.search_scope.addItems(
            ["ALL"] + [a.display_name for a in self.adapters.values()])
        bar.addWidget(self.search_scope)
        self.search_clear_btn = QToolButton()
        self.search_clear_btn.setText("✕")
        self.search_clear_btn.setToolTip("Clear search")
        self.search_clear_btn.clicked.connect(self.search_edit.clear)
        bar.addWidget(self.search_clear_btn)

        return bar

    def _build_menu(self):
        file_menu = self.menuBar().addMenu("File")
        save = QAction("Save backups", self)
        save.setShortcut("Ctrl+S")
        save.triggered.connect(lambda: self._save_backups())
        file_menu.addAction(save)
        file_menu.addAction("Revert to backup…", self._revert_backup)
        file_menu.addAction("Autosave interval…", self._configure_autosave)
        file_menu.addSeparator()
        file_menu.addAction("Configure paths…", self._show_paths_dialog)

        view_menu = self.menuBar().addMenu("View")
        cols = view_menu.addMenu("Columns")
        self._column_actions = {}
        for col_id in ALL_COLUMN_IDS:
            if col_id == STATUS_COL:
                continue                     # status column is always shown
            a = QAction(LABEL_BY_ID[col_id], self, checkable=True)
            a.setChecked(True)
            a.triggered.connect(lambda checked, cid=col_id: self._set_column_visible(cid, checked))
            cols.addAction(a)
            self._column_actions[col_id] = a

        view_menu.addSeparator()
        self._sync_pane_action = QAction("Playlists needing sync", self,
                                         checkable=True)
        self._sync_pane_action.setToolTip(
            "A separate pane listing only the playlists that are not in sync, "
            "per program")
        self._sync_pane_action.triggered.connect(self._set_sync_pane_visible)
        view_menu.addAction(self._sync_pane_action)
        act = QAction("Background logo…", self)
        act.setToolTip("How strongly each program's logo shows behind its tracks")
        act.triggered.connect(self._configure_watermark)
        view_menu.addAction(act)

        prog_menu = self.menuBar().addMenu("Programs")
        prog_menu.setToolTipsVisible(True)
        self._program_actions = {}
        for aid, adapter in self.adapters.items():
            a = QAction(adapter.display_name, self, checkable=True)
            a.setChecked(aid in self._enabled)
            a.setToolTip(f"Show {adapter.display_name} in this session")
            a.triggered.connect(
                lambda checked, i=aid: self._set_program_enabled(i, checked))
            prog_menu.addAction(a)
            self._program_actions[aid] = a

        # Which program the status squares compare everything against. Exclusive:
        # a square has to answer one question, not several.
        prog_menu.addSeparator()
        ref_menu = prog_menu.addMenu("Compare against")
        ref_menu.setToolTipsVisible(True)
        self._reference_group = QActionGroup(self)
        self._reference_group.setExclusive(True)
        for aid, adapter in self.adapters.items():
            a = QAction(adapter.display_name, self, checkable=True)
            a.setChecked(aid == self._reference)
            a.setToolTip(f"Colour every other program's playlists by whether "
                         f"they match {adapter.display_name}")
            a.triggered.connect(lambda _=False, i=aid: self._set_reference_program(i))
            self._reference_group.addAction(a)
            ref_menu.addAction(a)
            self._reference_actions[aid] = a

        skin_menu = self.menuBar().addMenu("Skin")
        self._theme_group = QActionGroup(self)
        self._theme_group.setExclusive(True)
        self._theme_actions = {}
        for label, key in (("Light mode", theme_mod.LIGHT), ("Dark mode", theme_mod.DARK)):
            a = QAction(label, self, checkable=True)
            a.setChecked(theme_mod.current() == key)
            a.triggered.connect(lambda _=False, k=key: self._set_theme(k))
            self._theme_group.addAction(a)
            skin_menu.addAction(a)
            self._theme_actions[key] = a

        controls_menu = self.menuBar().addMenu("Controls")
        act = QAction("Configure shortcuts…", self)
        act.triggered.connect(self._show_controls_dialog)
        controls_menu.addAction(act)

        # Diagnostics. Off by default and purely additive: a ticked box swaps the
        # engine's log sink and changes nothing else about what a sync does.
        debug_menu = self.menuBar().addMenu("Debug")
        debug_menu.setToolTipsVisible(True)
        self._debug_actions = {}
        for kind, label, hint in DEBUG_LOG_KINDS:
            act = QAction(label, self, checkable=True)
            act.setChecked(kind in self._debug_logs)
            act.setToolTip(hint)
            act.triggered.connect(
                lambda checked, k=kind: self._set_debug_log(k, checked))
            debug_menu.addAction(act)
            self._debug_actions[kind] = act
        debug_menu.addSeparator()
        debug_menu.addAction("Open log folder", self._open_logs_dir)

        help_menu = self.menuBar().addMenu("Help")
        act = QAction("Feature Guide & Test Checklist…", self)
        act.triggered.connect(self._show_help)
        help_menu.addAction(act)

    # -------------------- theme --------------------
    def _set_theme(self, key: str):
        """Switch theme and rebuild only what carries baked-in colour: the
        status-square icons. Deliberately avoids ``_recompute_status``, which
        re-diffs both libraries -- a repaint must not cost a full diff."""
        applied = theme_mod.apply(QGuiApplication.instance(), key)
        act = self._theme_actions.get(applied)
        if act is not None:
            act.setChecked(True)
        self._reapply_status_icons()
        # A per-widget palette override survives an app-wide palette swap, so the
        # unfocused pane would keep the OLD theme's muted grey (and the focused
        # one a stale highlight) until the next focus change.
        self.track_tabs.refresh_pane_focus()
        self.waveform_panel.apply_theme()
        self._on_playlists_selected(self._current_nodes, self._current_source)

    def _reapply_status_icons(self):
        """Re-draw the tree squares using the status functions from the last
        diff, so no re-diff is needed. Just-edited playlists are overlaid yellow
        (see ``_edited_paths``)."""
        for tree, fn in getattr(self, "_tree_status_fns", ()):
            tree.apply_playlist_status(self._edited_overlay(tree.source, fn),
                                       dup_fn=self._dup_flag)
        self._refresh_sync_pane()

    # -------------------- needs-sync pane --------------------
    def _set_sync_pane_visible(self, on: bool) -> None:
        on = bool(on)
        self._sync_pane_on = on
        self._sync_pane_action.setChecked(on)
        self.sync_pane.setVisible(on)
        if on:
            # A splitter remembers a hidden pane as zero wide, and would show it
            # that way. Take its first width from the track area, never the trees.
            sizes = self.h_splitter.sizes()
            if len(sizes) == 3 and sizes[1] == 0:
                width = min(280, max(sizes[2] // 3, 160))
                self.h_splitter.setSizes([sizes[0], width,
                                          max(sizes[2] - width, 0)])
            self._refresh_sync_pane()

    def _refresh_sync_pane(self) -> None:
        """Rebuild the pane from the status functions the sidebar was just
        painted with, so a row can never show a different glyph than its
        sidebar square. Skipped while the pane is off."""
        if not getattr(self, "_sync_pane_on", False):
            return
        fns = dict(getattr(self, "_tree_status_fns", ()))
        if not fns:
            self.sync_pane.populate(
                [], note="Turn on at least two programs to compare playlists.")
            return
        include_missing = self.sync_pane.include_missing.isChecked()
        groups, missing = [], 0
        for aid in self._enabled:
            tree = self.playlist_panel.trees.get(aid)
            if tree is None or tree not in fns:
                continue
            status_fn = self._edited_overlay(aid, fns[tree])
            rows = []
            for node in tree.visible_playlist_nodes():
                state = status_fn(node)
                if needs_sync(state, include_missing):
                    rows.append((node, state, self._dup_flag(node)))
                elif state == status_mod.BLACK:
                    missing += 1
            groups.append((f"{self.adapters[aid].display_name} Playlists", rows))
        self.sync_pane.populate(groups, missing=missing)
        self._refresh_sync_pane_songs()

    def _refresh_sync_pane_songs(self) -> None:
        """The pane's song list: every track of its highlighted playlists that
        is not green, with what differs.

        Built from the very rows the track table draws (``_playlist_rows``,
        sync-preview rows included), so a song's glyph here is its row's glyph
        there. Each track once, in table order."""
        if not getattr(self, "_sync_pane_on", False):
            return
        nodes = self.sync_pane.selected_nodes()
        if not nodes:
            self.sync_pane.populate_songs([], "")
            return
        source = nodes[0].source
        rows, _ghosts = self._playlist_rows(nodes, source, include_tombstones=True)
        out, seen = [], set()
        for track, state in rows:
            if state == status_mod.GREEN or track.key in seen:
                continue
            seen.add(track.key)
            out.append((track.key, track.title, track.artist, state,
                        self._song_difference(track, state, source)))
        self.sync_pane.populate_songs(out, source)

    def _song_difference(self, track, state: str, source: str) -> str:
        """A few words on why ``track`` is not green in ``source``.

        A copy is measured against the reference and the reference against
        every peer -- the same comparisons that coloured it."""
        reference = self._reference_in_use or self.reference_program()
        peers = [a for a, lib in self.libraries.items()
                 if a != source and lib is not None]
        against = [reference] if source != reference else peers
        names = ", ".join(self.adapters[a].display_name for a in against
                          if a in self.adapters) or "the other programs"
        if state == status_mod.RED:
            return "file missing on disk"
        if state == status_mod.LOSING:
            return f"removed in {names}"
        if state == status_mod.GAINING:
            return f"added in {names}"
        if state == status_mod.BLACK:
            return f"not in {names}"
        copies = [self.libraries[a].tracks.get(track.key) for a in against
                  if self.libraries.get(a) is not None]
        return ", ".join(status_mod.what_differs(track, copies)) or "differs"

    def _on_sync_pane_songs(self, keys: list, source: str) -> None:
        """Mirror the pane's song selection into the track table, which already
        shows the pane's playlists (their selection is mirrored into the
        sidebar). The table's selection is what every track action reads, so
        this is all a multi-selection needs."""
        table = self._table()
        if table is not None and table.select_keys(keys):
            table.scroll_to_key(keys[0])

    def _on_sync_pane_song_context(self, key: str, global_pos, selected: list) -> None:
        """The ordinary track menu, for the pane's selection when the clicked
        song is part of it, else that song alone -- the track list's own rule,
        which holds because the table carries the same selection."""
        table = self._table()
        if table is None:
            return
        if key in selected:
            table.select_keys(selected)
        row = next((r for r in range(table.model().rowCount())
                    if table.visual_key_at(r) == key), -1)
        if row >= 0:
            self._on_track_context(key, row, global_pos)

    def _on_sync_pane_selection(self, nodes: list, source: str) -> None:
        """Mirror the pane's selection into that program's sidebar tree -- the
        real user path, so the tab shows what is selected and every
        selection-scoped menu (Sync, Clean up, Hide…) acts on all of it."""
        tree = self.playlist_panel.trees.get(source)
        if tree is not None:
            tree.select_nodes(nodes)
        self._refresh_sync_pane_songs()

    def _on_sync_pane_context(self, node, global_pos, selected: list) -> None:
        """The ordinary playlist menu, scoped to the pane's selection.

        The sidebar may hold a different selection by now -- a refresh restores
        the one from before a sync, rows that synced included -- so it is set
        to the pane's, silently, before the menu reads it. The menu then applies
        its usual rule: the whole selection when the clicked row is in it, else
        that row alone."""
        tree = self.playlist_panel.trees.get(node.source)
        if tree is not None:
            tree.select_paths({tuple(n.node_path) for n in selected})
        self._on_node_context(node, global_pos)

    def _dup_flag(self, node) -> bool:
        """True for a playlist that permits duplicate tracks (∞ indicator).

        Only meaningful where the program HAS a duplicate policy -- everywhere
        else repeats are always allowed, so the marker would be on every row and
        mean nothing."""
        adapter = self.adapters.get(node.source)
        return bool(adapter is not None and adapter.supports_duplicate_policy
                    and node.playlist is not None
                    and not node.playlist.no_duplicates)

    def _repaint_tree(self, source: str) -> None:
        """Repaint one tree's status squares + ∞ flags from the last diff, with
        the edited-yellow overlay -- cheap icon redraw, no re-diff."""
        tree = self.playlist_panel.tree_for(source)
        for t, fn in getattr(self, "_tree_status_fns", ()):
            if t is tree:
                tree.apply_playlist_status(self._edited_overlay(source, fn),
                                           dup_fn=self._dup_flag)
                # An edit makes every program's copy stale (see
                # _edited_overlay), so the whole pane is rebuilt, not one group.
                self._refresh_sync_pane()
                return

    def _edited_overlay(self, source: str, base_fn):
        """Wrap a playlist status fn so a just-edited playlist shows its glyph
        immediately, in **every** program, without a re-diff.

        ``_edited_paths`` maps a playlist path to the program that edited it, so
        the same rule the real diff uses picks each copy's shape: the editor gets
        the triangle, and everyone else the square or the fork depending on
        whether the editor was the reference. Keyed by path rather than by
        (program, path) precisely so the other copies react too -- editing one
        program's playlist is what makes the others stale.
        """
        def fn(node):
            editor = self._edited_paths.get(path_ci(node.node_path))
            if editor is not None:
                return status_mod.changed_glyph(
                    source, self._reference_in_use or self.reference_program(),
                    {editor})
            return base_fn(node)
        return fn

    # -------------------- loading --------------------
    def _read_libraries(self, yield_fn=None) -> dict:
        """Parse each **enabled** program's library from disk, via its adapter.
        A program switched off for the session is not read at all, so turning one
        off is also what makes a launch faster. ``yield_fn`` (optional) is called
        periodically through the parse so the caller can keep audio/UI serviced.
        """
        return {aid: self.adapters[aid].read(yield_fn) for aid in self._enabled}

    def _install_libraries(self, libraries: dict, yield_fn=None):
        """Adopt freshly-parsed libraries: rebuild the trees and rewire the
        caches/engine. Main thread only (touches widgets)."""
        self.libraries = libraries
        self._search_cache = {}          # blobs are stale once the library changes
        self.playlist_panel.load(self.libraries, self._hidden, yield_fn)
        p = self.settings.paths
        ensure_start_time_csv(self.settings)
        self.start_time_cache = StartTimeCache(p.csv, self.settings.start_time_source)
        self.start_time_cache.load()
        # Only now can an adapter answer timeline questions: the class of a file
        # lives in the audio container, not in either database.
        for adapter in self.adapters.values():
            adapter.start_times = self.start_time_cache
        # Cue positions now convert source -> native -> target through the
        # adapters; playlist *structure* is still pairwise (see engine docstring).
        self.sync_engine = SyncEngine(self.libraries, p, self.start_time_cache,
                                      adapters=self.adapters)

    def _lib_counts_msg(self) -> str:
        parts = []
        for aid, lib in self.libraries.items():
            parts.append(f"{self.adapters[aid].display_name}: "
                         f"{len(lib.tracks)} tracks, "
                         f"{sum(1 for _ in lib.playlists())} playlists")
        return "  |  ".join(parts)

    def load_libraries(self):
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.statusBar().showMessage("Loading libraries…")
            QGuiApplication.processEvents()
            self._install_libraries(self._read_libraries())
            self._scan_integrity_all()   # before the diff, so no stale greens
            self._recompute_status()
            self.statusBar().showMessage(self._lib_counts_msg(), 8000)
        finally:
            QGuiApplication.restoreOverrideCursor()

    @staticmethod
    def _status_artifacts(libraries: dict, reference: str, yield_fn=None) -> dict:
        """Diff every loaded program against ``reference``.

        A square answers "is this in sync with the reference", which is the one
        question that stays well-defined however many programs are on -- "the
        other one" stops naming anything once there are three. The reference
        itself gets no report: it is the baseline, so there is nothing
        single-valued for it to be out of sync *with*.

        Reads/writes only the baseline file, one pair-scoped section per program.
        ``yield_fn`` (optional) is called through the heavy loops so a caller can
        keep audio/UI serviced. Returns everything the squares/rows need; the
        caller installs it.
        """
        # Which side actually changed is worked out ONCE per pair, in absolute
        # program-id terms, so both directional reports agree. VirtualDJ's
        # LastModified can't be trusted for this (see model/baseline.py), so each
        # side is compared against its own previously-seen fingerprint instead.
        baseline = Baseline(paths_mod.baseline_path()).load()
        ref_lib = libraries.get(reference)
        art = {"reference": reference, "owners": {}, "playlist_owners": {},
               "divergence": {}, "reports": {}, "indices": {},
               "track_changed_by": {}, "playlist_changed_by": {}}
        if ref_lib is None:
            return art
        art["indices"][reference] = ref_lib.playlist_index()
        others = [aid for aid, lib in libraries.items()
                  if aid != reference and lib is not None]
        # With a single peer, "you will lose this track" is unambiguous for the
        # reference too, so it keeps its own row markers. With more, whose change
        # it meant would be a guess.
        symmetric = len(others) == 1

        # ---- pass 1: attribution, and the cross-program tally --------------
        # Who changed what has to be known for EVERY pair before any report is
        # built: a copy shows the fork when a program it is not paired with is
        # the one that moved, which no single pair can report.
        tracks_by, lists_by = art["track_changed_by"], art["playlist_changed_by"]
        for aid in others:
            lib = libraries[aid]
            pair = bl_mod.pair_id(aid, reference)
            shared = [k for k in lib.tracks if k in ref_lib.tracks]
            owners = status_mod.attribute_changes(shared, lib, ref_lib,
                                                  baseline, yield_fn)
            art["owners"][pair] = owners
            pl_owners = status_mod.attribute_playlists(lib, ref_lib, baseline,
                                                       yield_fn)
            art["playlist_owners"][pair] = pl_owners
            baseline.save()
            for key, owner in owners.items():
                status_mod.merge_owner(tracks_by, key, owner, aid, reference)
            for path, owner in pl_owners.items():
                status_mod.merge_owner(lists_by, path, owner, aid, reference)

            divergence = status_mod.playlist_divergence(lib, ref_lib, baseline,
                                                        yield_fn)
            baseline.save()
            for path, sides in divergence.items():
                keep = (aid, reference) if symmetric else (aid,)
                for side in keep:
                    if side in sides:
                        art["divergence"].setdefault(path, {})[side] = sides[side]
            art["indices"][aid] = lib.playlist_index()

        # ---- pass 2: reports, now that the tally is complete ---------------
        for aid in others:
            lib = libraries[aid]
            art["reports"][aid] = status_mod.diff_tracks(
                list(lib.tracks), lib, ref_lib, tracks_by, yield_fn, reference)
        # The reference gets one too, against all its peers at once -- it needs a
        # glyph of its own (triangle when it changed, fork when somebody else
        # did), so it can no longer be left unpainted.
        if others:
            art["reports"][reference] = status_mod.diff_tracks_against(
                list(ref_lib.tracks), ref_lib, [libraries[a] for a in others],
                tracks_by, yield_fn, reference)
        return art

    def _apply_status(self, art: dict) -> None:
        """Install a status-artifacts dict (see :meth:`_status_artifacts`) and
        repaint the sidebar squares. Main thread only."""
        self._owners = art["owners"]
        self._divergence = art["divergence"]
        # Kept so a targeted re-status can fold new verdicts into the same
        # tally rather than rebuild it from every pair.
        self._track_changed_by = art["track_changed_by"]
        if getattr(self, "sync_engine", None) is not None:
            self.sync_engine.owners = art["owners"]
        self.reports = dict(art["reports"])
        indices = art["indices"]
        reference = art["reference"]
        self._reference_in_use = reference
        lists_by = art["playlist_changed_by"]

        # One painter per program. Every non-reference copy is measured against
        # the reference, so a square always answers the same question however
        # many programs are on; the reference is measured against all of them.
        def painter(source: str):
            report = self.reports.get(source)
            if report is None:
                return None
            # Its own library's, not the target's: a path spelled two ways here
            # is unwritable whoever it is being compared against.
            lib = self.libraries.get(source)
            clashes = lib.colliding_playlist_paths() if lib else {}
            if source == reference:
                peers = [indices[a] for a in self.reports
                         if a != reference and a in indices]
                if not peers:
                    return None
                return lambda n: report.reference_playlist_status(
                    n, peers, lists_by.get(path_ci(n.node_path)), reference,
                    clashes)
            index = indices.get(reference)
            if index is None:
                return None
            return lambda n: report.playlist_status(
                n, index, lists_by.get(path_ci(n.node_path)), reference,
                clashes)

        # Kept so a theme switch can redraw the squares without re-diffing.
        fns = ((tree, painter(aid))
               for aid, tree in self.playlist_panel.trees.items()
               if aid in self.reports)
        self._tree_status_fns = tuple((t, fn) for t, fn in fns if fn is not None)
        self._reapply_status_icons()

    def _set_program_enabled(self, adapter_id: str, on: bool) -> None:
        """Turn a program on or off for this session.

        Off means: not read from disk, and absent from the playlist pane. The
        adapter and its tree stay in place, so turning it back on is just another
        load rather than a rebuild.
        """
        enabled = [a for a in self.adapters if a in self._enabled]
        if on and adapter_id not in enabled:
            enabled.append(adapter_id)
        elif not on:
            enabled = [a for a in enabled if a != adapter_id]
        if not enabled:
            # Nothing left to show, and no pane to turn anything back on from.
            self._program_actions[adapter_id].setChecked(True)
            self.statusBar().showMessage(
                "At least one program has to stay on.", 5000)
            return
        # Keep registration order, so the pane doesn't reshuffle on a toggle.
        self._enabled = [a for a in self.adapters if a in enabled]
        self.playlist_panel.set_enabled_programs(self._enabled)
        self.load_libraries()

    def _sync_targets(self, source: str) -> list:
        """``[(adapter_id, display_name), …]`` -- every loaded program a sync
        from ``source`` could go to, in registration order. The context menu
        shapes itself from this rather than assuming there is exactly one."""
        return [(aid, self.adapters[aid].display_name) for aid in self._enabled
                if aid != source and self.libraries.get(aid) is not None]

    def reference_program(self) -> str:
        """The program every other one's squares are compared against.

        Falls back to the first loaded program if the saved choice is off or
        unknown, so a session can never end up diffing against nothing.
        """
        loaded = [a for a in self._enabled if self.libraries.get(a) is not None]
        if self._reference in loaded:
            return self._reference
        return loaded[0] if loaded else ""

    def _set_reference_program(self, adapter_id: str) -> None:
        self._reference = adapter_id
        for aid, act in self._reference_actions.items():
            act.setChecked(aid == adapter_id)
        self._recompute_status()

    def _diffable(self) -> bool:
        """A status diff compares programs against the reference, so it needs at
        least two loaded. With one on there is nothing to be out of sync with."""
        return len([a for a in self._enabled
                    if self.libraries.get(a) is not None]) >= 2

    def _recompute_status(self):
        if not self._diffable():
            # Clear rather than leave stale colours from when more were on.
            self.reports = {}
            self._owners = {}
            self._divergence = {}
            self._track_changed_by = {}
            self._tree_status_fns = ()
            self._edited_paths.clear()
            self._reapply_status_icons()
            return
        # A full diff reconciles disk state, so the optimistic edited-yellow
        # overlay is no longer needed -- the real status takes over.
        self._edited_paths.clear()
        loaded = {a: lib for a, lib in self.libraries.items() if lib is not None}
        self._apply_status(
            self._status_artifacts(loaded, self.reference_program()))

    def _restatus_tracks(self, programs, keys) -> None:
        """Re-read these tracks from each program's collection and recolour them.

        The targeted alternative to :meth:`_reload_preserving_view`, for an
        operation that touched a known handful of tracks. A full reload re-parses
        both collections (178 MB, ~16 s), re-diffs all 165,794 tracks (~2 s) and
        stats every audio file (~7 s) -- none of which is about the track that
        changed. This does the same job for the keys that moved.

        **The colours still come from disk.** Each program is asked to read those
        entries back (``LibraryAdapter.read_tracks``) rather than being taken at
        its word, so a write that did not land cannot show as green. That is the
        property the full reload was defending, kept at 2 KB instead of 178 MB.

        What it deliberately does not do is notice edits made *outside* this app.
        Neither did the reload, except by accident -- nothing watches the files.
        Refresh remains the answer for that.
        """
        keys = [k for k in dict.fromkeys(keys) if k]
        if not keys or not self._diffable():
            return
        reference = self.reference_program()
        for source in programs:
            lib = self.libraries.get(source)
            adapter = self.adapters.get(source)
            if lib is None or adapter is None:
                continue
            wanted = {k: lib.tracks[k].path for k in keys
                      if k in lib.tracks and lib.tracks[k].path}
            try:
                fresh = adapter.read_tracks(wanted)
            except Exception:                            # noqa: BLE001
                # A targeted read that fails must not leave a wrong colour
                # behind, so fall back to the thing that is always right.
                self._reload_preserving_view()
                return
            for key, track in fresh.items():
                lib.tracks[key] = track

        ref_lib = self.libraries.get(reference)
        if ref_lib is None:
            return
        others = [a for a, lib in self.libraries.items()
                  if a != reference and lib is not None and a in self.reports]

        # ---- ownership, per pair, before any report is rebuilt -------------
        # Same order the full diff uses, and for the same reason: a copy shows
        # the fork when a program it is not paired with is the one that moved,
        # which no single pair can report. The tally for these keys is cleared
        # first, or a verdict that has since gone away would linger in it.
        baseline = Baseline(paths_mod.baseline_path()).load()
        for key in keys:
            self._track_changed_by.pop(key, None)
        for aid in others:
            lib = self.libraries[aid]
            shared = [k for k in keys if k in lib.tracks and k in ref_lib.tracks]
            if not shared:
                continue
            owners = status_mod.attribute_changes(shared, lib, ref_lib, baseline)
            pair = bl_mod.pair_id(aid, reference)
            for key in shared:
                owner = owners.get(key)
                if owner is None:
                    self._owners.get(pair, {}).pop(key, None)
                else:
                    self._owners.setdefault(pair, {})[key] = owner
                status_mod.merge_owner(self._track_changed_by, key, owner,
                                       aid, reference)
        baseline.save()
        if self.sync_engine is not None:
            self.sync_engine.owners = self._owners

        # ---- reports, now that the tally is complete ----------------------
        for aid in others:
            lib = self.libraries[aid]
            report = self.reports.get(aid)
            shared = [k for k in keys if k in lib.tracks]
            if report is None or not shared:
                continue
            report.track_status.update(status_mod.diff_tracks(
                shared, lib, ref_lib, self._track_changed_by,
                reference=reference).track_status)
        # The reference is painted too -- it carries a glyph of its own -- and
        # is measured against every peer at once rather than against one.
        ref_report = self.reports.get(reference)
        ref_shared = [k for k in keys if k in ref_lib.tracks]
        if ref_report is not None and others and ref_shared:
            ref_report.track_status.update(status_mod.diff_tracks_against(
                ref_shared, ref_lib, [self.libraries[a] for a in others],
                self._track_changed_by, reference=reference).track_status)

        for key in keys:
            self._missing_keys_drop(key)
        self._reapply_status_icons()
        self._refresh_all_tabs()

    def _missing_keys_drop(self, key: str) -> None:
        """A synced track is no longer whatever a previous integrity scan said:
        its file was read to sync it."""
        for bucket in self._missing_keys.values():
            bucket.discard(key)

    # ---- tabs / rows / selection — ViewController ----
    # Bodies live in controllers/views.py; these keep the names the tests and the
    # rest of the window already call.
    def _nodes_by_paths(self, paths, source: str) -> list:
        return self.views.nodes_by_paths(paths, source)

    def _resolve_nodes(self, nodes: list, source: str) -> list:
        return self.views.resolve_nodes(nodes, source)

    def _refresh_all_tabs(self):
        self.views.refresh_all_tabs()

    def _reload_preserving_view(self, on_done=None):
        """Re-read both libraries and re-diff, keeping the tree expansion, the
        selected playlist and every open tab where they were.

        The parse + diff runs **cooperatively on the main thread**: it yields to
        the event loop (excluding user input) every few hundred items, so the
        main-thread audio pull keeps being serviced and playback never crackles,
        while a nested refresh/edit can't be triggered mid-reload. Each tab
        refreshes by its OWN context, so a sync of one playlist does not drag the
        focused tab onto it. ``on_done`` fires after the apply.
        """
        if self._reloading:
            return                       # ignore a reload requested mid-reload
        self._reloading = True
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        self.statusBar().showMessage("Refreshing libraries…")
        yield_fn = self._reload_yield
        # Building ~80k objects otherwise triggers cyclic-GC pauses (~100ms) that
        # no yield can interrupt -- disable GC for the reload so audio never
        # stutters; re-enable (and reclaim) afterwards.
        gc_was_on = gc.isenabled()
        gc.disable()
        try:
            snap = self.playlist_panel.snapshot_view()
            libraries = self._read_libraries(yield_fn)
            loaded = {a: lib for a, lib in libraries.items() if lib is not None}
            art = None
            if len(loaded) >= 2:
                reference = (self._reference if self._reference in loaded
                             else next(iter(loaded)))
                art = self._status_artifacts(loaded, reference, yield_fn)
            self._install_libraries(libraries, yield_fn)
            self._edited_paths.clear()   # a full diff reconciles the optimistic yellow
            # Verify the files exist before the diff is installed, so a refresh
            # never shows a green track whose file has gone.
            self._scan_integrity_all(yield_fn)
            if art is not None:
                self._apply_status(art)
            else:
                self._recompute_status()   # one program on: clears the squares
            self.playlist_panel.restore_view(snap)
            self._current_nodes = self._resolve_nodes(self._current_nodes, self._current_source)
            self._refresh_all_tabs()
            self._clear_search_silently()  # tabs show their playlists after a reload
            self.statusBar().showMessage(self._lib_counts_msg(), 6000)
        finally:
            self._reloading = False
            if gc_was_on:
                gc.enable()
            QGuiApplication.restoreOverrideCursor()
        if on_done:
            on_done()

    @staticmethod
    def _reload_yield():
        """Service audio/paint/timer events during a cooperative reload WITHOUT
        delivering user input (so no nested refresh/edit re-entrancy)."""
        QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)

    def refresh(self):
        """Re-read database.xml and collection.nml from disk and re-diff their
        cues, so the squares reflect what is actually on disk (either app may
        have changed the files since load). Runs in the background; playback
        (if any) keeps going."""
        # Show what was actually on disk: Traktor only writes collection.nml
        # when it saves/exits, so a cue edit made in a running Traktor is not
        # visible here until then -- this makes that obvious.
        self._reload_preserving_view(on_done=lambda: self.statusBar().showMessage(
            f"Refreshed — on disk:  collection.nml {_mtime_str(self.settings.paths.nml)}"
            f"   |   database.xml {_mtime_str(self.settings.paths.xml)}", 12000))

    # -------------------- waveform / playback --------------------
    def _track_for_key(self, key: str):
        lib = self.libraries.get(self._current_source)
        track = lib.tracks.get(key) if lib else None
        if track is None:
            # A tombstone row: some other program owns this track. Any of them
            # will do -- the waveform only needs the file path and its cues, and
            # a ghost row exists precisely because this program has no copy.
            for aid, other in self.libraries.items():
                if aid == self._current_source or other is None:
                    continue
                track = other.tracks.get(key)
                if track is not None:
                    break
        return track

    def _on_track_clicked(self, key: str):
        # Declines while playing -- see WaveformPanel.click_load. Only a load
        # that actually happened belongs in the history, which is why click_load
        # reports whether it stood down.
        track = self._track_for_key(key)
        if self.waveform_panel.click_load(track):
            self._record_play(track)

    def _on_track_double_clicked(self, key: str):
        track = self._track_for_key(key)
        self._record_play(track)
        self.waveform_panel.load_track(track, play=True)

    def _typing_in_a_text_field(self) -> bool:
        return transport_keys_blocked()

    def eventFilter(self, obj, event):
        """Route the transport keys and the arrows before any focused view can
        eat them, fire mouse bindings, and hand the wheel to whatever panel a
        drag is currently over."""
        if route_transport_key(event, self.waveform_panel):
            return True
        if route_arrow_key(event, self):
            return True
        if route_mouse_binding(event, getattr(self, "_mouse_bindings", None)):
            return True
        # Only while a drag is in flight, so Alt+scroll font zoom and ordinary
        # scrolling are untouched. The target is whichever view last received a
        # dragMoveEvent, which also covers drags started in Explorer.
        if event.type() == QEvent.Wheel:
            target = drag_scroll.drag_target()
            if target is not None and drag_scroll.wheel_scroll(
                    target, event.angleDelta().y()):
                return True
        return super().eventFilter(obj, event)

    # -------------------- tables / columns --------------------
    def _table(self):
        return self.views.table()

    def _init_table(self, table):
        self.views.init_table(table)

    def _on_font_changed(self, pt: int):
        self.views.on_font_changed(pt)

    def _set_column_visible(self, col_id: str, visible: bool):
        self.views.set_column_visible(col_id, visible)

    def _on_header_column_toggled(self, col_id: str, visible: bool):
        self.views.on_header_column_toggled(col_id, visible)

    def _status_for(self, source: str, report, key: str, default: str) -> str:
        return self.views.status_for(source, report, key, default)

    # -------------------- selection --------------------
    def _playlist_rows(self, nodes: list, source: str, *, include_tombstones: bool):
        return self.views.playlist_rows(nodes, source,
                                        include_tombstones=include_tombstones)

    def _populate_table(self, table, nodes: list, source: str, *,
                        include_tombstones: bool, preserve_scroll: bool):
        return self.views.populate_table(table, nodes, source,
                                         include_tombstones=include_tombstones,
                                         preserve_scroll=preserve_scroll)

    def _on_playlists_selected(self, nodes: list, source: str):
        self.views.on_playlists_selected(nodes, source)

    def _refresh_playlist_everywhere(self, node):
        self.views.refresh_playlist_everywhere(node)

    def _on_tab_focus_changed(self):
        self.views.on_tab_focus_changed()

    _SEARCH_CAP = 500

    def _clear_search_silently(self):
        """Reset the search box without triggering a re-search (used when a
        navigation/reload is about to repopulate the tab itself)."""
        self._search_timer.stop()
        self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(False)

    def _search_blobs(self, source: str) -> dict:
        """``{key: lowercased searchable text}`` for a whole library, cached (built
        once, cleared on reload). Includes the path so tag-less orphans still
        match by filename."""
        cached = self._search_cache.get(source)
        if cached is not None:
            return cached
        lib = self.libraries[source]
        blobs = {}
        for key, t in (lib.tracks.items() if lib else ()):
            blobs[key] = " ".join(filter(None, (
                t.title, t.artist, t.album, t.genre, t.comment,
                t.key_musical, t.path))).lower()
        self._search_cache[source] = blobs
        return blobs

    def _run_search(self):
        """Search the whole software library of the focused tab and repopulate
        that tab with the matches -- those already in the focused playlist first,
        then a separator, then every other match (orphans included). Empty query
        restores the tab's playlist view."""
        table = self._table()
        if table is None:
            return
        text = self.search_edit.text().strip().lower()
        scope = self.search_scope.currentText()
        # Map the label back to an adapter id explicitly. It used to rely on
        # display_name.lower() == id, which holds for "Traktor" but not for
        # "VirtualDJ" -> "vdj".
        by_name = {a.display_name: aid for aid, a in self.adapters.items()}
        source = by_name.get(scope) or (table.source or self._current_source)
        if not text:
            if table.nodes:
                self._populate_table(table, list(table.nodes),
                                     table.source or source,
                                     include_tombstones=True, preserve_scroll=False)
            else:
                table.set_rows([])
            self.status_label.setText("")
            return
        lib = self.libraries.get(source)
        if lib is None:
            return
        focus_keys: set = set()
        if len(table.nodes) == 1 and table.nodes[0].playlist is not None:
            focus_keys = set(table.nodes[0].playlist.track_keys)

        in_focus, others = [], []
        for key, blob in self._search_blobs(source).items():
            if text in blob:
                (in_focus if key in focus_keys else others).append(key)

        def sort_key(k):
            t = lib.tracks.get(k)
            return ((t.artist or "").lower(), (t.title or "").lower()) if t else ("", "")
        in_focus.sort(key=sort_key)
        others.sort(key=sort_key)
        shown_others = others[:max(0, self._SEARCH_CAP - len(in_focus))]

        report = self.reports.get(source)
        rows = [(lib.tracks[k], self._status_for(source, report, k, "black")) for k in in_focus]
        if in_focus and shown_others:
            rows.append((None, "──  Other collection tracks (not in this playlist)  ──"))
        rows += [(lib.tracks[k], self._status_for(source, report, k, "black")) for k in shown_others]
        table.set_rows(rows)

        more = (f"  (showing {len(shown_others)} of {len(others)})"
                if len(others) > len(shown_others) else "")
        self.status_label.setText(
            f"{source.upper()} search “{self.search_edit.text()}”: "
            f"{len(in_focus)} in this playlist · {len(others)} elsewhere{more}")

    # -------------------- context menus --------------------
    def _on_node_context(self, node, global_pos):
        source = node.source if node is not None else self._current_source
        tree = self.playlist_panel.tree_for(source)
        # Duplicate policy is a VDJ-only, per-playlist flag (Traktor always
        # allows repeats); None hides the menu entry.
        dup_allowed = (not node.playlist.no_duplicates) if (
            self.adapters[source].supports_duplicate_policy
            and node is not None and node.playlist is not None) else None
        cb = _CB(
            select_all=tree.select_all_playlists,
            sync=lambda mode, target: self._sync_selected(source, mode, node, target),
            sync_targets=self._sync_targets(source),
            hide=lambda: self._hide_node(node, source),
            unhide=lambda: None,
            open_in_new_tab=lambda: self._open_in_new_tab(node, source),
            check_integrity=lambda: self._check_integrity_playlist(node, source),
            clean_up=lambda: self._clean_up(source, node),
            remove_duplicates=lambda: self._remove_duplicates(node),
            duplicates_allowed=dup_allowed,
            toggle_duplicates=lambda checked: self._set_allow_duplicates(node, checked),
            new_playlist=lambda: self._begin_new_node(source, node, "playlist"),
            new_folder=lambda: self._begin_new_node(source, node, "folder"),
            # Playlists only: duplicating a folder would mean a recursive copy
            # of its whole subtree, minting a fresh UUID per playlist inside.
            can_duplicate=(node is not None and node.kind == "playlist"),
            duplicate=lambda: self._begin_duplicate(source, node),
            rename_label=("Rename folder" if (node is not None and node.is_folder())
                          else "Rename playlist"),
            rename=lambda: self._begin_rename(source, node),
            remove_label=("Remove folder" if (node is not None and node.is_folder())
                          else "Remove playlist"),
            remove=lambda: self._remove_node(source, node),
        )
        context_menus.playlist_menu(self, source, cb).exec(global_pos)

    def _on_blank_context(self, source: str, global_pos):
        """Right-click on empty space in a library tree: the only things to do
        there create at that software's root."""
        cb = _CB(new_playlist=lambda: self._begin_new_node(source, None, "playlist"),
                 new_folder=lambda: self._begin_new_node(source, None, "folder"))
        context_menus.blank_menu(self, source, cb).exec(global_pos)

    def _on_playback_title_context(self, global_pos):
        """Right-click on the loaded track's name in the playback pane.

        The same menu the track list gives, built from the same bag -- a track is
        a track wherever you clicked it, and two bags would drift the moment one
        gained an entry.
        """
        track = self.waveform_panel._track
        if track is None:
            return
        source = self._current_source
        # Not a list row, so there is no preview-row case to consider: what is
        # loaded is a real track by definition.
        context_menus.track_menu(
            self, source, self._track_callbacks(track.key, source, row=-1)
        ).exec(global_pos)

    def _track_callbacks(self, track_key, source, row):
        """The track menu's callback bag. One builder for both callers."""
        table = self._table()
        # Asked of the table, which drew the row, rather than recomputed from the
        # report -- deriving it twice is how a row and its square came apart.
        ghost = bool(row >= 0 and table is not None
                     and table.is_tombstone_row(row))
        gaining = ghost and table.row_status(row) == status_mod.GAINING
        path = self._local_path_for(track_key, source)
        cb = self._build_track_cb(track_key, source, row, ghost, path)
        lib = self.libraries.get(source)
        own = lib.tracks.get(track_key) if lib is not None else None
        cb.source_has_entry = own is not None and own.in_database
        if gaining:
            cb.bring_in_from = [
                (aid, self.adapters[aid].display_name)
                for aid, other in self.libraries.items()
                if aid != source and other is not None
                and getattr(other.tracks.get(track_key), "in_database", False)]
            cb.bring_in = lambda holder: self._bring_track_in(track_key, source, holder)
        return cb

    def _bring_track_in(self, track_key: str, source: str, holder: str) -> None:
        """Add a preview row's track -- one ``holder``'s copy of the playlist
        has and this one lacks -- to this program's playlist(s), creating its
        collection entry from ``holder`` first.

        The cross-program drop does exactly that (confirm, stage the entry,
        append), so it is reused rather than copied. Of the open playlists, the
        ones whose ``holder`` copy contains the track are the ones it goes into;
        the whole selection comes along when the clicked row is part of it."""
        table = self._table()
        nodes = [n for n in (getattr(table, "nodes", None) or [])
                 if n.source == source and n.playlist is not None]
        holder_lib = self.libraries.get(holder)
        if not nodes or holder_lib is None:
            return
        index = holder_lib.playlist_index()
        keys = [k for k in self._selected_track_keys(track_key)
                if getattr(holder_lib.tracks.get(k), "in_database", False)]
        added = 0
        for node in nodes:
            theirs = index.get(path_ci(node.node_path))
            wanted = [k for k in keys
                      if k not in node.playlist.track_keys
                      and (theirs is None or theirs.playlist is None
                           or k in theirs.playlist.track_keys)]
            if wanted:
                self.membership.add_tracks(node, wanted, holder)
                added += 1
        if not added:
            self.statusBar().showMessage(
                "Nothing to add: the open playlist already has that track.", 4000)

    def _on_track_context(self, track_key, row, global_pos):
        # From the table that drew the row, not the window: a right-click is
        # answered by the pane it happened in, whatever the window-level "current
        # source" happens to say.
        table = self._table()
        source = getattr(table, "source", "") or self._current_source
        context_menus.track_menu(
            self, source, self._track_callbacks(track_key, source, row)
        ).exec(global_pos)

    def _build_track_cb(self, track_key, source, row, ghost, path):
        cb = _CB(
            sync_track=lambda target: self._sync_track(track_key, target),
            sync_targets=self._sync_targets(source),
            collection_label=self.adapters[source].collection_label
            if source in self.adapters else "",
            edit=lambda: self._edit_track_metadata(track_key, source),
            show_playlists=lambda: self._show_track_playlists(track_key, source),
            check_integrity=lambda: self._check_integrity_tracks(source, [track_key]),
            reveal=lambda: self._reveal_track(track_key, source),
            can_reveal=bool(path),
            is_tombstone=ghost,
            remove_from_playlist=lambda: self._remove_from_playlist_by_row(track_key, row),
            remove_with_entry=lambda: self._remove_with_entry(track_key),
            delete_file=lambda: self._delete_file(track_key),
        )
        return cb

    def _on_track_unreadable(self, track_key: str) -> None:
        """A track whose audio would not load is marked red wherever it appears.

        Recorded in ``_missing_keys``, the same store the integrity check uses,
        so it renders through the existing path and clears on the next check or
        refresh rather than lingering forever.
        """
        source = self._current_source
        if not track_key or source not in self.libraries:
            return
        bucket = self._missing_keys.setdefault(source, set())
        if track_key in bucket:
            return                                   # already flagged
        bucket.add(track_key)
        self._repaint_tree(source)
        self._refresh_all_tabs()
        self.statusBar().showMessage(
            "That track's audio could not be read — marked as an error.", 6000)

    # -------------------- editing track metadata --------------------
    def _selected_track_keys(self, track_key: str) -> list:
        """The tracks a track-menu action acts on: the whole selection when the
        clicked track is part of it, else just that one -- right-click
        deliberately does not change the selection, so the clicked row has to
        speak for itself when it is outside it.

        **One definition, used by every action that follows the rule.** Edit had
        it and Sync Track did not, which is why syncing a multi-selection only
        ever wrote the row that was clicked.
        """
        table = self._table()
        keys = list(table.selected_keys()) if table is not None else []
        return keys if track_key in keys else [track_key]

    def _edit_targets(self, track_key: str, source: str) -> list:
        """:meth:`_selected_track_keys` resolved against ``source``'s library."""
        lib = self.libraries.get(source)
        if lib is None:
            return []
        return [lib.tracks[k] for k in self._selected_track_keys(track_key)
                if k in lib.tracks]

    def _edit_track_metadata(self, track_key: str, source: str) -> None:
        tracks = self._edit_targets(track_key, source)
        if not tracks:
            return
        adapter = self.adapters.get(source)
        fields = adapter.EDITABLE_FIELDS if adapter else ()
        dialog = MetadataDialog(tracks, fields, self)
        if dialog.exec() != QDialog.Accepted:
            return                       # Cancel and the X both land here
        self._apply_metadata(tracks, source, dialog.result_fields())

    # -------------------- assignable shortcuts --------------------
    def _action_handlers(self) -> dict:
        """``{action id: callable}`` for everything a key can be bound to.

        The ids live in ``config/controls.py`` and never change; this is the only
        place that says what each one *does*. An id with no handler is simply not
        bound, so the registry can name something before it is implemented.
        """
        tabs = self.track_tabs
        return {
            "tab.next": lambda: tabs.step_tab(1),
            "tab.previous": lambda: tabs.step_tab(-1),
            "tab.new": tabs.new_tab,
            "tab.close": tabs.close_current_tab,
            "tab.split": tabs.split_focused,
            "tab.unsplit": tabs.unsplit,
            "pane.focus_other": tabs.focus_other_group,
            "play.next": lambda: self._step_track(1),
            "play.previous": lambda: self._step_track(-1),
            "track.edit": self._edit_current_track,
            "track.reveal": lambda: self._with_current_track(self._reveal_track),
            "track.playlists": lambda: self._with_current_track(
                lambda key, src: self._show_track_playlists(key, src)),
            "track.integrity": lambda: self._with_current_track(
                lambda key, src: self._check_integrity_tracks(src, [key])),
            "track.remove": lambda: self._with_current_track(
                lambda key, _src: self._remove_from_playlist_by_row(
                    key, self._current_row_for(key))),
            "track.remove_entry": lambda: self._with_current_track(
                lambda key, _src: self._remove_with_entry(key)),
            "track.delete_file": lambda: self._with_current_track(
                lambda key, _src: self._delete_file(key)),
            "playlist.open_tab": lambda: self._with_current_node(self._open_in_new_tab),
            "playlist.select_all": self._select_all_playlists,
            "playlist.remove_duplicates": lambda: self._with_current_node(
                lambda node, _src: self._remove_duplicates(node)),
            "playlist.integrity": lambda: self._with_current_node(
                lambda node, src: self._check_integrity_playlist(node, src)),
            "playlist.new_playlist": lambda: self._with_current_node(
                lambda node, src: self._begin_new_node(src, node, "playlist")),
            "playlist.new_folder": lambda: self._with_current_node(
                lambda node, src: self._begin_new_node(src, node, "folder")),
            "playlist.duplicate": lambda: self._with_current_node(
                lambda node, src: self._begin_duplicate(src, node)),
            "playlist.rename": lambda: self._with_current_node(
                lambda node, src: self._begin_rename(src, node)),
            "playlist.remove": lambda: self._with_current_node(
                lambda node, src: self._remove_node(src, node)),
            "playlist.hide": lambda: self._with_current_node(
                lambda node, src: self._hide_node(node, src)),
            "app.refresh": self.refresh,
            "app.backup": lambda: self._save_backups(),
        }

    def _with_current_track(self, fn):
        """Run ``fn(track_key, source)`` for the focused table's current track."""
        table = self._table()
        if table is None:
            return
        rows = table.selected_visual_rows()
        if not rows:
            return
        key = table.visual_key_at(rows[0])
        if key:
            fn(key, table.source or self._current_source)

    def _current_row_for(self, key: str) -> int:
        table = self._table()
        if table is None:
            return -1
        for row in range(table._proxy.rowCount()):
            if table.visual_key_at(row) == key:
                return row
        return -1

    def _edit_current_track(self) -> None:
        self._with_current_track(
            lambda key, src: self._edit_track_metadata(key, src))

    def _with_current_node(self, fn):
        """Run ``fn(node, source)`` for the sidebar's current playlist/folder."""
        source = self._current_source or next(iter(self._enabled), "")
        tree = self.playlist_panel.trees.get(source)
        if tree is None:
            return
        nodes = tree.selected_top_nodes()
        if nodes:
            fn(nodes[0], source)

    def _select_all_playlists(self) -> None:
        source = self._current_source or next(iter(self._enabled), "")
        tree = self.playlist_panel.trees.get(source)
        if tree is not None:
            tree.select_all_playlists()

    def apply_controls(self, bindings: dict | None = None) -> None:
        """(Re)build the QShortcuts from the saved bindings.

        Old shortcuts are destroyed first: leaving them attached would keep a
        chord the user has just cleared working, which is the sort of thing that
        only shows up as "it un-assigned but still fires".
        """
        if bindings is not None:
            self._controls = dict(bindings)
        for sc in getattr(self, "_shortcuts", []):
            sc.setParent(None)
        self._shortcuts = []
        # QShortcut cannot fire on a mouse button, so those are dispatched from
        # the application-wide event filter instead, keyed by exactly the
        # (modifiers, button) they were bound with.
        self._mouse_bindings = {}
        handlers = self._action_handlers()
        for action_id, seq in self._controls.items():
            handler = handlers.get(action_id)
            if not seq or handler is None:
                continue
            if controls_mod.is_mouse_binding(seq):
                parsed = controls_mod.parse_mouse_binding(seq)
                if parsed is not None:        # unknown button / bare left-right
                    self._mouse_bindings[parsed] = handler
                continue
            shortcut = QShortcut(QKeySequence(seq), self)
            shortcut.setContext(Qt.ApplicationShortcut)
            shortcut.activated.connect(handler)
            self._shortcuts.append(shortcut)

    def apply_timing(self, timing: dict | None = None) -> None:
        """Apply the gesture timings to Qt and to every pane.

        The mouse interval is set on **QApplication**, not just on our tables:
        the deferred click-to-edit has to wait at least as long as Qt itself
        waits before calling something a double-click, or a double-click could
        land after the editor had already opened. Setting one from the other is
        what keeps them from disagreeing.

        ``double_click_ms = 0`` means follow the OS, so Qt's own value is left
        alone and the tables read it back.
        """
        if timing is not None:
            self._timing = dict(timing)
        ms = int(self._timing.get("double_click_ms") or 0)
        if ms:
            QApplication.setDoubleClickInterval(ms)
        effective = ms or QApplication.doubleClickInterval()
        # One detector, on the window: the arrows are routed application-wide.
        self._taps.window_ms = max(1, int(
            self._timing.get("double_tap_ms")
            or controls_mod.TIMING_DEFAULTS["double_tap_ms"]))
        for table in self.track_tabs.all_tables():
            table.set_double_click_ms(effective)

    def double_tap_ms(self) -> int:
        return self._taps.window_ms

    def _show_controls_dialog(self) -> None:
        dialog = ControlsDialog(self._controls, self._timing, self)
        if dialog.exec() != QDialog.Accepted:
            return
        bindings = dialog.bindings()
        timing = dialog.timing()
        controls_mod.save(bindings, timing=timing)
        self.apply_controls(bindings)
        self.apply_timing(timing)
        self.statusBar().showMessage("Shortcuts saved to userControls.ini", 5000)

    # -------------------- arrow-key navigation --------------------
    def _arrow_tree(self):
        """The sidebar tree Left/Right walks.

        Whichever tree has focus, else the current source's. Clicking a VirtualDJ
        playlist and pressing Right has to walk the VirtualDJ tree, not whichever
        program the focused tab happens to be showing.
        """
        focused = QApplication.focusWidget()
        for tree in self.playlist_panel.trees.values():
            if focused is tree:
                return tree
        source = self._current_source or next(iter(self._enabled), "")
        return self.playlist_panel.trees.get(source)

    def step_playlist(self, delta: int) -> None:
        """Left/Right walk the playlist pane, from anywhere in the window.

        On a **folder** the step waits out the double-tap window: a second tap of
        the same arrow inside it expands (Right) or collapses (Left) instead, and
        the cursor never leaves the folder. Stepping first and jumping back was
        the alternative; it is what the shipped version effectively did, and the
        gesture could not work at all because the second tap landed on whatever
        row the first had moved to.

        Rows that are not folders step immediately -- there is no gesture to wait
        for, and every arrow press should not feel laggy.
        """
        tree = self._arrow_tree()
        if tree is None:
            return
        key = Qt.Key_Right if delta > 0 else Qt.Key_Left
        if self._taps.tap(key) and self._pending_step is not None:
            # Second tap of the same arrow, inside the window: the step never
            # happens, so the cursor is still on the folder.
            self._cancel_pending_step()
            tree.set_current_expanded(delta > 0)
            return
        # A different arrow abandons the pending step rather than performing it:
        # you changed your mind, so that tap never happened.
        self._cancel_pending_step()
        if not tree.current_is_folder():
            tree.step_row(delta)
            return
        self._pending_step = delta
        self._step_timer.start(self._taps.window_ms)

    def _cancel_pending_step(self) -> None:
        self._step_timer.stop()
        self._pending_step = None

    def _flush_pending_step(self) -> None:
        """The double-tap window elapsed with no second tap, so it was a single
        one after all: step, as it would have immediately off a folder."""
        delta, self._pending_step = self._pending_step, None
        tree = self._arrow_tree()
        if delta is not None and tree is not None:
            tree.step_row(delta)

    def step_track_row(self, delta: int) -> None:
        """Up/Down move the track list's selection, from anywhere in the window.

        Deliberately *not* the sidebar: the arrows are split by axis, so each one
        always means the same thing wherever focus happens to be. With nothing
        selected either key lands on the **top row** -- you are entering the list,
        and that is where a list starts.
        """
        table = self._table()
        if table is None:
            return
        rows = table.rowCount()
        if not rows:
            return
        selected = table.selected_visual_rows()
        if not selected:
            row = 0
        else:
            # From the far edge of a multi-row selection, so Down continues past
            # the block rather than collapsing back to its start.
            anchor = max(selected) if delta > 0 else min(selected)
            row = max(0, min(rows - 1, anchor + delta))
        table.select_visual_row(row)

    def _on_cell_edited(self, table, track_key: str, field: str, value: str) -> None:
        """One cell edited in place. Routed to the same write as the dialog.

        Acts on that one track only, never the selection: you clicked a specific
        cell, and quietly rewriting five other tracks' tags because they happened
        to be selected would be a surprise with no undo.
        """
        source = getattr(table, "source", "") or self._current_source
        lib = self.libraries.get(source)
        track = lib.tracks.get(track_key) if lib else None
        if track is None or getattr(track, field, "") == value:
            return                                   # nothing actually changed
        self._apply_metadata([track], source, {field: value})

    def _apply_metadata(self, tracks: list, source: str, fields: dict) -> None:
        """Write ``fields`` to each track, in memory and on the queue.

        Deliberately not routed through ``guarded_write``: that snapshots the
        whole library, which is right before a deletion but absurd per tag edit.
        An edit is undone by editing back, and the session-open backup stands.
        """
        if not fields or not tracks:
            return
        adapter = self.adapters.get(source)
        if adapter is None:
            return
        for track in tracks:
            for field, value in fields.items():
                setattr(track, field, value)
        # One job for the whole selection. A write has to walk the collection to
        # find its entry, so one per track meant fifty walks of a 43 MB
        # database.xml for a fifty-track edit.
        self._writer.submit(
            adapter.write_metadata_jobs({t.key: fields for t in tracks},
                                        {t.key: t.path for t in tracks}))
        # The edit makes this program differ from the others, exactly as a
        # membership edit does, so the same optimistic marking applies.
        for node in self.libraries[source].playlists():
            if any(t.key in node.playlist.track_keys for t in tracks):
                self._edited_paths[path_ci(node.node_path)] = source
        self._record_edit_attribution([t.key for t in tracks], source)
        self._reapply_status_icons()
        self._refresh_all_tabs()
        self.statusBar().showMessage(
            f"Updated {len(tracks)} track(s): {', '.join(sorted(fields))}", 5000)

    def _record_edit_attribution(self, keys: list, source: str) -> None:
        """Tell the baseline that ``source`` just edited these tracks.

        Sync **Update** asks attribution "does the source own this change?" and
        skips when the answer is no. Attribution is only ever computed by a full
        diff, and a track that was in sync beforehand is absent from the owners
        map -- so a freshly edited track answered "nobody", and Update skipped
        exactly the tracks the user had just changed. Overwrite ignores the guard
        entirely, which is why only it appeared to work.

        Recorded here rather than by re-running the diff: the app already knows
        who edited what, and a full re-diff per keystroke-edit would cost seconds
        on a large library.
        """
        peers = [aid for aid in self.libraries
                 if aid != source and self.libraries.get(aid) is not None]
        if not keys or not peers:
            return
        store = Baseline(paths_mod.baseline_path()).load()
        for key in keys:
            bl_mod.record_edit(store, key, source, peers, self.libraries)
        store.save()
        # Push it into the live engine too: self._owners is what a sync in this
        # session reads, and it is only rebuilt by a full recompute.
        for peer in peers:
            pair = bl_mod.pair_id(source, peer)
            self._owners.setdefault(pair, {}).update({k: source for k in keys})
        if getattr(self, "sync_engine", None) is not None:
            self.sync_engine.owners = self._owners

    def _local_path_for(self, track_key: str, source: str) -> str:
        """The on-disk path for a track, or "" when it is unknown or gone. The
        stored path is a program's own notation, so it goes through
        ``resolve_local_path`` before anything touches the filesystem."""
        lib = self.libraries.get(source)
        track = lib.tracks.get(track_key) if lib else None
        if track is None or not track.path:
            return ""
        local = matching.resolve_local_path(track.path)
        return local if local and os.path.exists(local) else ""

    def _reveal_track(self, track_key: str, source: str) -> None:
        path = self._local_path_for(track_key, source)
        if not path or not reveal.reveal_in_file_browser(path):
            self.statusBar().showMessage(
                "Could not open the file's location.", 5000)

    def _reorder_playlist(self, table, new_keys: list[str]):
        self.membership.reorder(table, new_keys)

    def _open_in_new_tab(self, node, source):
        self.track_tabs.new_tab()
        again = [n for n in (self.libraries[source]).playlists()
                 if tuple(n.node_path) == tuple(node.node_path)]
        if again:
            self._on_playlists_selected(again, source)

    def _show_track_playlists(self, track_key, source: str = ""):
        """Where this track lives, across every loaded program.

        ``source`` names the pane it was asked from, which only decides the
        ordering -- the popup lists them all, because "which playlists am I in"
        is a question about the whole library set.
        """
        source = source or self._current_source
        track = next((lib.tracks[track_key] for lib in self.libraries.values()
                      if track_key in lib.tracks), None)
        label = f"{track.artist} — {track.title}" if track else track_key
        dlg = PlaylistsWindow(self.libraries, track_key, label, source, self)
        dlg.navigate.connect(self._navigate_to)
        dlg.exec()

    def _navigate_to(self, node, source):
        self.playlist_panel.tree_for(source).select_node(node)

    # ---- sidebar structure: new / duplicate / rename / move / remove ----
    # Bodies live in controllers/structure.py; these keep the names the tests
    # and the rest of the window already call.
    def _resolve_node_name(self, source: str, parent, name: str, node=None) -> str:
        return self.structure.resolve_name(source, parent, name, node)

    def _parent_for(self, source: str, node):
        return self.structure.parent_for(source, node)

    def _begin_new_node(self, source: str, node, kind: str):
        self.structure.begin_new(source, node, kind)

    def _begin_duplicate(self, source: str, node):
        self.structure.begin_duplicate(source, node)

    def _begin_rename(self, source: str, node):
        self.structure.begin_rename(source, node)

    def _create_node(self, source: str, parent_node, name: str):
        self.structure.create(source, parent_node, name)

    def _build_folder(self, source: str, name: str, node_path: tuple) -> TreeNode:
        return self.structure.build_folder(source, name, node_path)

    def _build_playlist(self, source: str, name: str, node_path: tuple,
                        copy_of, lib) -> TreeNode:
        return self.structure.build_playlist(source, name, node_path, copy_of, lib)

    def _rename_node(self, source: str, node, name: str):
        self.structure.rename(source, node, name)

    def _relocate_playlist_job(self, source: str, node, new_path: tuple, lib):
        return self.structure.relocate_playlist_job(source, node, new_path, lib)

    def _relocate_folder_job(self, source: str, old_path: tuple, new_path: tuple):
        return self.structure.relocate_folder_job(source, old_path, new_path)

    def _migrate_paths(self, source: str, old_path: tuple, new_path: tuple) -> None:
        self.structure.migrate_paths(source, old_path, new_path)

    def _move_nodes(self, source: str, nodes: list, target):
        self.structure.move(source, nodes, target)

    def _remove_node(self, source: str, node):
        self.structure.remove(source, node)

    def _remove_job(self, source: str, node):
        return self.structure.remove_job(source, node)

    def _forget_paths(self, source: str, path: tuple) -> None:
        self.structure.forget_paths(source, path)

    def _clear_tabs_for(self, nodes: list) -> None:
        self.views.clear_tabs_for(nodes)

    def _rebuild_trees_preserving_view(self):
        self.views.rebuild_trees_preserving_view()

    def _relabel_tabs_for(self, node):
        self.views.relabel_tabs_for(node)

    # -------------------- hide / unhide --------------------
    def _hide_selected(self):
        tree = self.playlist_panel.active_tree()
        if tree is None:
            return
        source = tree.source
        for node in tree.selected_top_nodes():
            self._hidden[source].add(node.node_path)
        self._reload_trees()

    def _hide_node(self, node, source: str):
        """Hide the right-clicked playlist (right-click no longer selects, so the
        context menu can't rely on the tree selection)."""
        if node is None:
            return
        self._hidden[source].add(node.node_path)
        self._reload_trees()

    def _unhide(self, payload):
        for source, node_path in payload:
            self._hidden.get(source, set()).discard(tuple(node_path))
        self._reload_trees()

    def _reload_trees(self):
        self.playlist_panel.load(self.libraries, self._hidden)
        self._recompute_status()

    # -------------------- sync (write engine) --------------------
    def _resolve_targets(self, source: str, target) -> list:
        """Normalise a sync target into a list of adapter ids.

        The menus hand back either one id or -- for the "All programs" entry --
        the whole list, so every caller downstream can just loop. ``None`` still
        means "the only other program", which is what a two-program session and
        the direct API both rely on.
        """
        if target is None:
            fallback = self.sync_engine.default_target(source)
            return [fallback] if fallback else []
        if isinstance(target, str):
            return [target]
        return [t for t in target if t]

    def _sync_selected(self, source: str, mode: str, node=None, target=None):
        targets = self._resolve_targets(source, target)
        if not targets:
            QMessageBox.information(
                self, "Sync",
                "Syncing copies from one program into another, so there has to "
                "be another one on for this session — see the Programs menu.")
            return
        tree = self.playlist_panel.tree_for(source)
        selected = tree.selected_playlist_nodes()
        # Right-click no longer changes the selection, so a context-menu sync
        # targets the right-clicked node -- unless it's part of the current
        # multi-selection, in which case the whole selection is synced.
        if node is not None and not any(
                tuple(n.node_path) == tuple(node.node_path) for n in selected):
            nodes = list(node.iter_playlists())
        else:
            nodes = selected
        if not nodes:
            QMessageBox.information(self, "Sync", "No playlist selected.")
            return
        target_names = self.collection.program_list(targets)
        if not self._confirm_no_name_collisions(source, nodes):
            return
        if mode == OVERWRITE:
            names = ", ".join(n.name for n in nodes[:5]) + (" …" if len(nodes) > 5 else "")
            reply = QMessageBox.warning(
                self, "Confirm overwrite",
                f"This will OVERWRITE {target_names}'s copy of: {names}\n\n"
                f"Playlist membership and cues will be forced to match "
                f"{source.upper()}. This cannot be undone (a backup is made, "
                f"but existing changes to these playlists in {target_names} "
                f"are lost).\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply != QMessageBox.Yes:
                return
        if not self._confirm_running_targets(targets):
            return

        # Per target, so the report can say which program did what. One merged
        # total would hide a program that silently did nothing.
        stats = {t: {"added": 0, "synced": 0, "skipped": 0, "created": 0,
                     "entries": 0, "removed": 0, "errors": [],
                     "skip_reasons": {}} for t in targets}
        logged: list[str] = []

        def do_sync():
            # One backup per target collection for the whole run, not one per
            # playlist (see SyncEngine.batch).
            with self.sync_engine.batch():
                _sync_all()

        def _sync_all():
            for target in targets:
                s = stats[target]
                for node in nodes:
                    # One file per playlist per target, so a fan-out produces
                    # logs you can attribute rather than one interleaved blob.
                    log = self._start_log(_MODE_LOG_KIND.get(mode, ""),
                                          f"{node.name}-to-{target}")
                    try:
                        r = self.sync_engine.sync_playlist(node, source, mode, target)
                    except Exception as e:       # noqa: BLE001
                        # One program failing must not abandon the others: a
                        # fan-out that stops half way, silently, is worse than
                        # one that finishes and reports. Not just OSError --
                        # a MalformedWrite is a ValueError, and catching only
                        # OSError let one bad playlist take the rest of the
                        # folder down with it.
                        s["errors"].append(f"{node.name}: {type(e).__name__}: {e}")
                        continue
                    finally:
                        # What the run probed, kept. Without this the file stays
                        # at its header and the next launch re-probes every
                        # track it writes -- 28 ms each, every session.
                        self._save_start_times()
                        written = self._finish_log(log)
                        if written:
                            logged.append(written)
                    s["added"] += r.tracks_added
                    s["synced"] += r.tracks_cue_synced
                    s["skipped"] += r.tracks_skipped
                    for why, count in r.skipped_by_reason.items():
                        s["skip_reasons"][why] = (
                            s["skip_reasons"].get(why, 0) + count)
                    s["created"] += r.playlists_created
                    s["entries"] += r.playlist_entries_added
                    s["removed"] += r.playlist_entries_removed
                    s["errors"] += r.errors

        self.overlay.run(
            f"Syncing playlists — {source.upper()} → {target_names}", do_sync)
        msg = self._sync_report(source, mode, nodes, targets, stats)
        msg += self._logged_note(logged)
        # Re-diff in the background (keeps playback going), then report.
        self._reload_preserving_view(
            on_done=lambda: QMessageBox.information(self, "Sync complete", msg))

    def _confirm_no_name_collisions(self, source: str, nodes: list) -> bool:
        """Name the playlists that cannot both be written, before anything is.

        Two sibling playlists whose names differ only in capitalisation are one
        path to everything downstream, and VirtualDJ keeps one *file* per
        playlist -- so the second write lands on the first one's file. That
        already cost a reported 511-track playlist, with both runs saying they
        succeeded. The engine refuses them either way; asking here means the run
        can be cancelled and a name fixed instead of reading about it afterwards.
        """
        lib = self.libraries.get(source)
        if lib is None:
            return True
        collisions = lib.colliding_playlist_paths()
        if not collisions:
            return True
        chosen = {path_ci(n.node_path) for n in nodes}
        hit = [ns for path, ns in collisions.items() if path in chosen]
        if not hit:
            return True
        listed = "\n".join(
            "    " + ", ".join(f'"{n.name}"' for n in ns)
            + "  in  " + ("/".join(ns[0].node_path[:-1]) or "the top level")
            for ns in hit[:6])
        more = f"\n    …and {len(hit) - 6} more" if len(hit) > 6 else ""
        return QMessageBox.warning(
            self, "Playlist names clash",
            f"These differ only in capitalisation:\n\n{listed}{more}\n\n"
            f"The target keeps one file per playlist, so writing both would "
            f"leave only whichever went last — the other one's tracks would be "
            f"gone. They will be SKIPPED and reported.\n\n"
            f"Rename one of each pair in {source.upper()} to sync them.\n\n"
            f"Continue with the rest?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def _confirm_running_targets(self, targets: list) -> bool:
        """Warn once about every target that rewrites its database on exit and is
        open right now. Asked of the adapters, not of their names: whether a
        program clobbers our writes is a fact about that program.

        One prompt naming all of them, rather than one prompt each -- a fan-out
        to three programs should not mean three dialogs to dismiss.
        """
        running = [t for t in targets
                   if self.adapters[t].clobbers_on_exit and self.adapters[t].is_running()]
        if not running:
            return True
        names = self.collection.program_list(running)
        verb = "appears" if len(running) == 1 else "appear"
        return QMessageBox.warning(
            self, f"{names} running",
            f"{names} {verb} to be open. It rewrites its database on exit and "
            f"can overwrite this sync's changes. Close it before continuing."
            f"\n\nContinue anyway?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    #: How each Update skip reads. One total reported as "the other side owns the
    #: change" was wrong for two of these three, and saying that next to a square
    #: reading "you changed this" is what made the app look self-contradictory.
    _SKIP_WORDING = {
        SKIP_TARGET_OWNS: "the other program owns the change",
        SKIP_CONFLICT: "changed on both sides — use Overwrite to force",
        SKIP_UNKNOWN: ("never compared before — run Overwrite once to establish "
                       "a baseline"),
        "": "the target looks newer",
    }

    @classmethod
    def _skip_lines(cls, s: dict) -> str:
        """One line per reason, or nothing at all when none were skipped."""
        if not s["skipped"]:
            return ""
        reasons = s.get("skip_reasons") or {}
        if not reasons:
            return f"\n  Skipped: {s['skipped']}"
        return "\n" + "\n".join(
            f"  Skipped ({cls._SKIP_WORDING.get(why, why)}): {count}"
            for why, count in sorted(reasons.items(), key=lambda kv: -kv[1]))

    def _sync_report(self, source: str, mode: str, nodes: list, targets: list,
                     stats: dict) -> str:
        """One block per target, so a fan-out says what each program actually
        got rather than a single total nobody can attribute."""
        lines = [f"{_MODE_LABELS[mode]}: {source.upper()} → "
                 f"{self.collection.program_list(targets)}",
                 f"Playlists: {len(nodes)}"]
        errors = []
        for target in targets:
            s = stats[target]
            lines.append(
                f"\n{self.adapters[target].display_name}: "
                f"{s['created']} playlist(s) created\n"
                f"  Playlist entries added: {s['entries']}   removed: {s['removed']}\n"
                f"  Collection tracks added: {s['added']}   Cues synced: {s['synced']}"
                + self._skip_lines(s))
            errors += [f"{self.adapters[target].display_name}: {e}"
                       for e in s["errors"]]
        msg = "\n".join(lines)
        if errors:
            msg += "\n\nErrors:\n" + "\n".join(errors[:10])
        return msg

    def _sync_track(self, track_key: str, target=None):
        """``target`` is one program id, or a list of them for "All programs".

        Acts on the **whole selection** when the clicked track is part of it --
        the same rule Edit and Remove follow. One call per target rather than one
        per track: the engine writes each collection once per run, so a 50-track
        selection looped one at a time would rewrite both files fifty times.
        """
        source = self._current_source
        targets = self._resolve_targets(source, target)
        if not targets:
            QMessageBox.information(
                self, "Sync Track",
                "There has to be another program on for this session — see the "
                "Programs menu.")
            return
        if not self._confirm_running_targets(targets):
            return
        keys = self._selected_track_keys(track_key)
        # A preview row of a track another program added has no entry here to
        # sync from; the engine would answer "Track not found in source
        # library". Say what is going on instead, and sync the rest.
        lib = self.libraries.get(source)
        absent = [k for k in keys
                  if not getattr(lib.tracks.get(k) if lib else None, "in_database", False)]
        keys = [k for k in keys if k not in absent]
        name = self.adapters[source].display_name if source in self.adapters else source
        absent_note = (f"\n\n{len(absent)} selected track(s) are not in {name}'s "
                       f"collection, so there was nothing to sync from. Right-click "
                       f"one and use \"Add to this playlist from …\" to bring it in."
                       if absent else "")
        if not keys:
            QMessageBox.information(self, "Sync Track", absent_note.strip())
            return
        errors = []
        logged: list[str] = []
        track = self._track_for_key(track_key)
        label = getattr(track, "title", "") or track_key
        if len(keys) > 1:
            label = f"{len(keys)}-tracks"
        for t in targets:
            log = self._start_log("track_sync", f"{label}-to-{t}")
            try:
                r = self.sync_engine.sync_tracks(keys, source, t)
            except OSError as e:                 # one failure, the rest still run
                errors.append(f"{self.adapters[t].display_name}: {e}")
                continue
            finally:
                self._save_start_times()
                written = self._finish_log(log)
                if written:
                    logged.append(written)
            errors += [f"{self.adapters[t].display_name}: {e}" for e in r.errors]
        names = self.collection.program_list(targets)
        note = self._logged_note(logged) + absent_note
        # Only these tracks changed, so only these are re-read and recoloured.
        # A full reload here re-parsed 178 MB, re-diffed 165,794 tracks and
        # stat-ed every audio file before the dialog could appear -- about 30
        # seconds of work, none of it about what was synced.
        self._restatus_tracks({source, *targets}, keys)
        if errors:
            QMessageBox.warning(self, "Sync Track", "\n".join(errors) + note)
        else:
            what = "Track cues" if len(keys) == 1 else f"{len(keys)} tracks' cues"
            QMessageBox.information(
                self, "Sync Track", f"{what} synced to {names}.{note}")

    def _logged_note(self, logged: list) -> str:
        """Tell the user a log was written, and where. Silent when none were --
        a run with logging off must read exactly as it did before.

        A log that could *not* be written is named too, with its reason. The
        sync itself succeeded in that case, so this is a note rather than an
        error -- but a silently missing log is a bug report the user cannot
        file. Drained as it is rendered, so each report covers its own run.
        """
        failed, self._log_failures = self._log_failures, []
        note = ""
        if logged:
            note = ("\n\nDebug log written:\n"
                    + "\n".join(logged[:6])
                    + (f"\n…and {len(logged) - 6} more" if len(logged) > 6 else ""))
        if failed:
            note += ("\n\nDebug log could NOT be written (the sync itself "
                     "completed):\n" + "\n".join(failed[:6])
                     + (f"\n…and {len(failed) - 6} more" if len(failed) > 6 else ""))
        return note

    # -------------------- backups --------------------
    def _save_backups(self, quiet: bool = False):
        dest = self.overlay.run("Writing backup",
                                lambda: backup.snapshot(self.settings.paths))
        if quiet:
            return
        if dest:
            self.statusBar().showMessage(f"Backup saved: {os.path.basename(dest)}", 5000)
        else:
            self.statusBar().showMessage("Nothing to back up.", 4000)

    def _revert_backup(self):
        items = backup.list_backups()
        if not items:
            QMessageBox.information(self, "Revert", "No backups found.")
            return
        labels = [ts for ts, _ in items]
        choice, ok = QInputDialog.getItem(
            self, "Revert to backup",
            "Restore which snapshot? This overwrites the live collection files.",
            labels, 0, False)
        if not ok:
            return
        backup_dir = dict(zip(labels, [d for _, d in items]))[choice]
        if QMessageBox.warning(
                self, "Confirm revert",
                f"Restore backup {choice} over the current collection.nml, "
                f"database.xml and the VirtualDJ Folders tree?\n\nThis replaces "
                f"the live files.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.overlay.run("Reverting backup",
                         lambda: backup.revert(backup_dir, self.settings.paths))
        self._reload_preserving_view()
        self.statusBar().showMessage(f"Reverted to {choice}", 6000)

    def _configure_autosave(self):
        minutes, ok = QInputDialog.getInt(
            self, "Autosave interval",
            "Backup automatically every N minutes (0 = off):",
            self.settings.backup_autosave_minutes, 0, 1440, 1)
        if ok:
            self.settings.backup_autosave_minutes = minutes
            self._apply_autosave_interval(minutes)

    def _apply_autosave_interval(self, minutes: int):
        if minutes and minutes > 0:
            self._autosave_timer.start(minutes * 60_000)
        else:
            self._autosave_timer.stop()

    # -------------------- clean up --------------------
    def _clean_up_targets(self, source: str, node) -> tuple:
        """``(tracks, label)`` for a Clean up run.

        The whole sidebar selection when the right-clicked node is part of it,
        else that node alone -- the same rule Sync and Remove use, since
        right-click deliberately does not change the selection. A folder
        contributes every playlist beneath it.
        """
        lib = self.libraries.get(source)
        if lib is None:
            return [], ""
        tree = self.playlist_panel.tree_for(source)
        nodes = tree.selected_playlist_nodes() if tree is not None else []
        if node is not None and not any(
                tuple(n.node_path) == tuple(node.node_path) for n in nodes):
            # A right-clicked folder means every playlist under it, which is
            # what the tree's own selection would have resolved to.
            nodes = list(node.iter_playlists())

        keys, seen = [], set()
        for playlist_node in nodes:
            if playlist_node.playlist is None:
                continue
            for key in playlist_node.playlist.track_keys:
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        tracks = [lib.tracks[k] for k in keys if k in lib.tracks]
        name = (f"“{nodes[0].name}”" if len(nodes) == 1
                else f"{len(nodes)} playlists")
        return tracks, f"{name} ({len(tracks)} tracks)"

    def _clean_up(self, source: str, node) -> None:
        """Scan for fixable defects, then apply whichever kinds the user picks.

        The scan writes nothing, so it runs without a confirmation. Applying
        renames real audio files, so it goes through ``guarded_write`` first --
        confirm, warn about any program that would clobber the result on exit,
        and snapshot the whole library.
        """
        tracks, label = self._clean_up_targets(source, node)
        if not tracks:
            return
        # A relink is applied to every loaded program, so every one of them is
        # asked whether it already holds the on-disk name as a separate entry.
        collections = {
            self.adapters[aid].display_name:
                {k for k, t in lib.tracks.items() if t.in_database}
            for aid, lib in self.libraries.items() if lib is not None}
        findings = cleanup.block_existing_entries(cleanup.scan(tracks),
                                                  collections)
        dialog = CleanupDialog(findings, label, self)
        if dialog.exec() != QDialog.Accepted:
            return
        chosen = dialog.chosen_findings()
        if not chosen:
            return
        self._apply_cleanup(chosen)

    def _apply_cleanup(self, findings: list) -> None:
        relinks = cleanup.relink_map(findings)
        count = len(findings) - len(relinks)
        work = []
        if count:
            work.append(f"Rename {count} file{'' if count == 1 else 's'} on disk")
        if relinks:
            work.append(f"relink {len(relinks)} track"
                        f"{'' if len(relinks) == 1 else 's'} to the name "
                        "already on disk")
        if not self.collection.guarded_write(
                "Clean up",
                f"{' and '.join(work).capitalize()}, and update every loaded "
                "program to match?\n\n"
                "Files are renamed, never deleted, and a full library backup "
                "is taken first.",
                writes_vdj=True):
            return

        moved, failures = cleanup.rename_files(findings)
        renamed = len(moved)
        # Their files are already right, so they join the relink map directly.
        moved.update(relinks)
        # Only tracks whose file really moved are relinked, so a rename that
        # failed leaves that track pointing at a file that is still there.
        relinked, errors = {}, [f"{f.current_name}: {why}" for f, why in failures]
        for source, adapter in self.adapters.items():
            if not moved:
                break
            # A program already holding the on-disk entry sits a relink out
            # rather than gaining a duplicate (``block_existing_entries``).
            skipped = cleanup.skipped_keys(findings, adapter.display_name)
            mapping = {k: v for k, v in moved.items() if k not in skipped}
            if not mapping:
                continue
            try:
                relinked[source] = sum(1 for ok in adapter.repath_tracks_job(
                    mapping)().values() if ok)
            except Exception as exc:                       # noqa: BLE001
                errors.append(f"{adapter.display_name}: {type(exc).__name__}: {exc}")

        self._report_cleanup(renamed, relinked, errors, respelled=len(relinks))
        if moved:
            self._reload_preserving_view()

    def _report_cleanup(self, renamed: int, relinked: dict, errors: list,
                        respelled: int = 0) -> None:
        lines = []
        if renamed or not respelled:
            lines.append(f"Renamed {renamed} file{'' if renamed == 1 else 's'}.")
        if respelled:
            lines.append(f"Relinked {respelled} track"
                         f"{'' if respelled == 1 else 's'} to the spelling on "
                         "disk.")
        for source, count in sorted(relinked.items()):
            name = self.adapters[source].display_name
            lines.append(f"  {name}: {count} relinked")
        if errors:
            lines.append("")
            lines.append(f"{len(errors)} could not be done:")
            lines.extend(f"  {e}" for e in errors[:10])
            if len(errors) > 10:
                lines.append(f"  … and {len(errors) - 10} more")
        QMessageBox.information(self, "Clean up", "\n".join(lines))

    # -------------------- file integrity --------------------
    def _check_integrity_playlist(self, node, source):
        keys = list(node.playlist.track_keys) if node and node.playlist else []
        self._check_integrity_tracks(source, keys)

    def _scan_integrity(self, source, keys) -> tuple:
        """Check whether each track's file is on disk. Silent: it only updates
        ``_missing_keys`` and returns ``(missing_file, dangling)``.

        Split out of the reported check so a refresh can run it over everything
        without a summary dialog, and without re-diffing per program.
        """
        lib = self.libraries.get(source)
        if lib is None:
            return [], []
        missing_file, dangling = [], []
        for key in keys:
            track = lib.tracks.get(key)
            if track is None or not track.in_database:
                dangling.append(key)                 # referenced but no collection entry
                continue
            if not (track.path and os.path.exists(track.path)):
                missing_file.append(key)

        # Missing files render red everywhere until re-checked or found again.
        bucket = self._missing_keys.setdefault(source, set())
        for key in keys:
            bucket.discard(key)                      # clear stale verdicts first
        bucket.update(missing_file)
        return missing_file, dangling

    def _scan_integrity_all(self, yield_fn=None) -> None:
        """Check every loaded program's whole collection.

        Run as part of a refresh, before the diff is installed, so the red marks
        are correct the moment the refresh finishes rather than appearing later.
        ``yield_fn`` keeps audio and the UI alive through what is thousands of
        stat calls on a big library.
        """
        for source, lib in list(self.libraries.items()):
            if lib is None:
                continue
            self._scan_integrity(source, list(lib.tracks))
            if yield_fn is not None:
                yield_fn()

    def _check_integrity_tracks(self, source, keys):
        lib = self.libraries[source]
        missing_file, dangling = self._scan_integrity(source, keys)
        self._recompute_status()
        self._on_playlists_selected(self._current_nodes, self._current_source)

        ok = len(keys) - len(missing_file) - len(dangling)
        lines = [f"Checked {len(keys)} track(s):",
                 f"  OK: {ok}",
                 f"  File missing on disk: {len(missing_file)}",
                 f"  Not in the {source.upper()} collection (dangling): {len(dangling)}"]
        for key in (missing_file + dangling)[:20]:
            t = lib.tracks.get(key)
            lines.append(f"    • {t.path if t else key}")
        QMessageBox.information(self, "File integrity", "\n".join(lines))

    # ---- membership editing (Phase 2) — MembershipController ----
    # Bodies live in controllers/membership.py; these keep the names the tests
    # and the rest of the window already call.
    def _edit_membership(self, node, new_keys: list[str]):
        self.membership.edit(node, new_keys)

    def _enqueue_membership_write(self, node, keys: list[str]):
        self.membership.enqueue_write(node, keys)

    def _set_allow_duplicates(self, node, allow: bool):
        self.membership.set_allow_duplicates(node, allow)

    def _remove_from_playlist(self, node, keys: list[str]):
        self.membership.remove_keys(node, keys)

    def _remove_positions(self, node, positions):
        self.membership.remove_positions(node, positions)

    def _remove_from_playlist_by_row(self, track_key: str, row: int):
        self.membership.remove_by_row(track_key, row)

    # ---- Phase 3: collection removal / file delete / import (CollectionController)
    # Bodies live in controllers/collection.py; these keep the names the tests
    # and the rest of the window already call.
    def _guarded_write(self, title: str, msg: str, writes_vdj: bool) -> bool:
        return self.collection.guarded_write(title, msg, writes_vdj)

    def _enqueue_entry_removal(self, source: str, key: str):
        self.collection.enqueue_entry_removal(source, key)

    def _enqueue_entry_creation(self, source: str, track: Track):
        self.collection.enqueue_entry_creation(source, track)

    def _strip_from_all_playlists(self, lib, key: str):
        self.collection.strip_from_all_playlists(lib, key)

    def _track_label(self, track, fallback: str) -> str:
        return self.collection.track_label(track, fallback)

    def _remove_with_entry(self, track_key: str):
        self.collection.remove_with_entry(track_key)

    def _delete_file(self, track_key: str):
        self.collection.delete_file(track_key)

    def _import_files(self, node, paths: list[str], at_index: int = -1):
        self.collection.import_files(node, paths, at_index)

    def _on_files_dropped_on_table(self, table, paths: list[str], at_index: int = -1):
        self.collection.on_files_dropped_on_table(table, paths, at_index)

    def _on_files_dropped_on_sidebar(self, node, paths: list[str]):
        self.collection.on_files_dropped_on_sidebar(node, paths)

    def _remove_duplicates(self, node):
        self.membership.remove_duplicates(node)

    def _add_tracks(self, node, keys: list[str], drag_source: str, at_index: int = -1):
        self.membership.add_tracks(node, keys, drag_source, at_index)

    def _on_tracks_dropped_on_table(self, table, keys: list[str], drag_source: str,
                                    at_index: int = -1):
        self.membership.on_tracks_dropped_on_table(table, keys, drag_source, at_index)

    def _on_tracks_dropped_on_sidebar(self, node, keys: list[str], drag_source: str):
        self.membership.on_tracks_dropped_on_sidebar(node, keys, drag_source)

    # -------------------- play history --------------------
    # Browser-style: a list of what was played plus a position in it. Previous
    # walks back up it and Next replays forward, which is the only reading of
    # "previous" that means anything under shuffle -- the mode's own rule would
    # just hand you another random track.
    def _record_play(self, track) -> None:
        """Note that ``track`` became the loaded one.

        Replays from the history deliberately do NOT come through here -- they
        move the position instead of appending, or stepping back and forward
        would record each replay and trap you between two tracks.
        """
        if track is None:
            return
        del self._play_history[self._history_pos + 1:]    # drop the forward tail
        if self._play_history and self._play_history[-1] == track.key:
            self._history_pos = len(self._play_history) - 1
            return                                        # reloading the same one
        self._play_history.append(track.key)
        self._history_pos = len(self._play_history) - 1

    def _history_step(self, delta: int):
        """The track ``delta`` places along the history, or None at either end."""
        pos = self._history_pos + delta
        if not (0 <= pos < len(self._play_history)):
            return None
        track = self._track_for_key(self._play_history[pos])
        if track is None:
            return None
        self._history_pos = pos
        return track

    # -------------------- auto-advance --------------------
    def _auto_advance(self):
        mode = self.waveform_panel.on_end_mode()
        nxt = self._next_track(mode)
        if nxt is not None:
            self._record_play(nxt)
            self.waveform_panel.load_track(nxt, play=True)

    def _next_track(self, mode):
        """Resolve the successor of the playing track for the given on-end mode,
        or None (stop). Uses the focused tab's context."""
        if mode == wf.ON_END_SINGLE:
            return None
        table = self._table()
        cur = self.waveform_panel._track
        if table is None or cur is None:
            return None
        source = table.source or self._current_source
        lib = self.libraries[source]

        def track_of(key):
            return lib.tracks.get(key)

        if mode == wf.ON_END_SORT:
            keys = table.visual_keys()
            return self._after(keys, cur.key, track_of)
        if mode == wf.ON_END_ORDER:
            keys = []
            for n in table.nodes:
                keys.extend(n.playlist.track_keys)
            return self._after(keys, cur.key, track_of)
        if mode == wf.ON_END_RANDOM_PLAYLIST:
            keys = [k for n in table.nodes for k in n.playlist.track_keys]
            return self._random_other(keys, cur.key, track_of)
        if mode == wf.ON_END_RANDOM_SUBFOLDER:
            keys = self._subfolder_keys(source, table.nodes)
            return self._random_other(keys, cur.key, track_of)
        if mode == wf.ON_END_RANDOM_LIBRARY:
            return self._random_other(list(lib.tracks.keys()), cur.key, track_of)
        return None

    def _step_track(self, delta: int) -> None:
        """Play the track ``delta`` places from the current one.

        **Follows the "on end" mode**, so Next is a manual trigger of whatever
        would have happened at the end of the track -- including a random pick
        under the shuffle modes. Single file is the exception: it means "do not
        auto-advance", but a button press has to do something, so it steps
        through what the tab is showing.

        Backwards prefers the **play history**, because under shuffle there is no
        such thing as the previous track in the list -- only the one you actually
        heard. Falling back to the mode's own predecessor covers a fresh session
        with nothing behind you.

        Playback only starts if it was already playing -- pressing Next while
        paused should move the selection, not start the music.
        """
        playing = self.waveform_panel.player.is_playing()
        replay = self._history_step(delta)
        if replay is not None:
            # Already moved the position; recording it would append a duplicate.
            self.waveform_panel.load_track(replay, play=playing)
            return
        mode = self.waveform_panel.on_end_mode()
        nxt = (self._next_track_for_step(mode) if delta >= 0
               else self._previous_track(mode))
        if nxt is None:
            return
        self._record_play(nxt)
        self.waveform_panel.load_track(nxt, play=playing)

    def _next_track_for_step(self, mode):
        """Next's successor. Identical to auto-advance except under Single file,
        where auto-advance stops and an explicit Next still moves."""
        if mode == wf.ON_END_SINGLE:
            return self._ordered_step(1)
        return self._next_track(mode)

    def _previous_track(self, mode):
        """The predecessor for the ordered modes.

        The random modes have no meaningful predecessor -- that is exactly what
        the history exists for -- so with no history behind you they step back
        through the shown order rather than drawing another random track, which
        would make Previous indistinguishable from Next.
        """
        if mode == wf.ON_END_ORDER:
            table = self._table()
            keys = ([k for n in table.nodes for k in n.playlist.track_keys]
                    if table is not None else [])
            return self._ordered_step(-1, keys)
        return self._ordered_step(-1)

    def _ordered_step(self, delta: int, keys=None):
        """``delta`` places through ``keys`` (default: what the tab shows)."""
        table = self._table()
        if table is None:
            return None
        source = table.source or self._current_source
        lib = self.libraries.get(source)
        if lib is None:
            return None
        cur = self.waveform_panel._track
        # Preview rows are excluded: stepping onto one would play a track this
        # playlist does not actually contain.
        if keys is None:
            keys = table.real_visual_keys()
        return self._step(keys, cur.key if cur else "", delta, lib.tracks.get)

    @staticmethod
    def _step(keys, cur_key, delta, track_of):
        """The track ``delta`` places from ``cur_key``, wrapping at both ends and
        skipping any key that does not resolve to a track.

        With nothing playing, a forward step starts at the top and a backward
        step at the bottom -- so Previous on a fresh list plays its last track
        rather than doing nothing.
        """
        usable = [k for k in keys if track_of(k) is not None]
        if not usable:
            return None
        if cur_key in usable:
            return track_of(usable[(usable.index(cur_key) + delta) % len(usable)])
        return track_of(usable[0] if delta >= 0 else usable[-1])

    @staticmethod
    def _after(keys, cur_key, track_of):
        if cur_key in keys:
            i = keys.index(cur_key)
            for k in keys[i + 1:] + keys[:i + 1]:     # wrap around
                if k != cur_key and track_of(k) is not None:
                    return track_of(k)
        for k in keys:
            if track_of(k) is not None:
                return track_of(k)
        return None

    @staticmethod
    def _random_other(keys, cur_key, track_of):
        pool = [k for k in keys if k != cur_key and track_of(k) is not None]
        return track_of(random.choice(pool)) if pool else None

    def _subfolder_keys(self, source, nodes):
        """All track keys in the folder that contains the current playlist."""
        if not nodes:
            return []
        parent_path = tuple(nodes[0].node_path[:-1])
        lib = self.libraries[source]
        keys = []
        for pl in lib.playlists():
            if tuple(pl.node_path[:-1]) == parent_path and pl.playlist:
                keys.extend(pl.playlist.track_keys)
        return keys

    # -------------------- paths / help --------------------
    def _show_paths_dialog(self):
        dlg = PathsDialog(self.settings, self)
        if dlg.exec():
            self._reload_preserving_view()

    # -------------------- diagnostics --------------------
    def _set_debug_log(self, kind: str, on: bool) -> None:
        if on:
            self._debug_logs.add(kind)
        else:
            self._debug_logs.discard(kind)
        where = synclog.logs_dir()
        self.statusBar().showMessage(
            f"Logging {kind} → {where}" if on else f"Stopped logging {kind}", 6000)

    def debug_logging(self, kind: str) -> bool:
        return kind in self._debug_logs

    def _open_logs_dir(self) -> None:
        path = synclog.logs_dir()
        os.makedirs(path, exist_ok=True)
        if not reveal.open_folder(path):
            QMessageBox.information(self, "Logs", f"Logs are written to:\n{path}")

    def _start_log(self, kind: str, label: str):
        """Attach a log to the engine for one run, or leave the null sink.

        Returned so the caller can write it afterwards; ``None`` means this kind
        is switched off, which every call site treats as "do nothing extra".
        """
        if kind not in self._debug_logs or self.sync_engine is None:
            return None
        try:
            log = synclog.SyncLog(kind, label)
            log.want_timings = PERF_LOG_KIND in self._debug_logs
            log.want_parse_checks = PARSE_CHECK_KIND in self._debug_logs
            self.sync_engine.log = log
            return log
        except Exception:                        # noqa: BLE001
            return None                          # diagnostics never break a sync

    def _finish_log(self, log) -> str:
        """Write it and detach. Never raises: a log that cannot be saved must not
        turn a completed sync into a failed one.

        A failure is *recorded* rather than discarded. Swallowing it silently is
        how two playlists in a reported folder sync ended up with 0-byte logs
        while the app said "Sync complete" -- the user could see something was
        wrong but had nothing to act on.
        """
        if log is None:
            return ""
        try:
            written = log.write()
        except Exception as exc:                 # noqa: BLE001
            self._log_failures.append(f"{log.label}: {type(exc).__name__}: {exc}")
            return ""
        finally:
            if self.sync_engine is not None:
                self.sync_engine.log = synclog.NullLog()
        if not written and getattr(log, "error", ""):
            self._log_failures.append(f"{log.label}: {log.error}")
        return written

    def _show_help(self):
        HelpWindow(self).exec()

    # -------------------- background logo --------------------
    @staticmethod
    def _clean_opacity(value) -> int:
        """A percentage, whatever the saved state actually holds. Session state
        is a hand-editable JSON blob, so a string or a nonsense number has to
        land on something drawable rather than raise during __init__."""
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return watermark.DEFAULT_OPACITY

    def set_watermark_opacity(self, percent: int) -> None:
        """Apply to every pane, including ones in other tabs -- the setting is a
        preference, not a property of the visible view."""
        self._watermark_opacity = self._clean_opacity(percent)
        for table in self.track_tabs.all_tables():
            table.set_watermark_opacity(self._watermark_opacity)

    def watermark_opacity(self) -> int:
        return self._watermark_opacity

    def _configure_watermark(self):
        """Live while dragging, restored on Cancel: the only way to choose a
        watermark strength is to see it against your own tracks."""
        before = self._watermark_opacity
        dlg = QDialog(self)
        dlg.setWindowTitle("Background logo")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("How strongly each program's logo shows behind "
                             "its tracks.\nZero turns it off."))
        row = QHBoxLayout()
        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(before)
        readout = QLabel(f"{before}%")
        readout.setMinimumWidth(40)
        row.addWidget(slider)
        row.addWidget(readout)
        lay.addLayout(row)

        def on_change(value):
            readout.setText(f"{value}%")
            self.set_watermark_opacity(value)

        slider.valueChanged.connect(on_change)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        if not dlg.exec():
            self.set_watermark_opacity(before)

    # -------------------- state persistence --------------------
    def _capture_state(self) -> dict:
        primary = self.track_tabs.all_tables()[0] if self.track_tabs.all_tables() else None
        return {
            "geometry": state_mod.qbytes_to_hex(self.saveGeometry()),
            "h_splitter": self.h_splitter.sizes(),
            "v_splitter": self.v_splitter.sizes(),
            "volume": self.waveform_panel.volume_value(),
            "tempo": self.waveform_panel.tempo_value(),
            "on_end": self.waveform_panel.on_end_mode(),
            "time_remaining": self.waveform_panel.show_remaining(),
            "playlist_splitter": self.playlist_panel.splitter.sizes(),
            "collapsed_sections": self.playlist_panel.collapsed_sections(),
            "header_state": primary.save_header_state() if primary else "",
            "hidden_fields": sorted(self._hidden_fields),
            "font_pt": primary.font_point_size() if primary else 9,
            "sections_locked": self.playlist_panel.sections_locked(),
            "section_order": self.playlist_panel.section_order(),
            "search_scope": self.search_scope.currentText(),
            "debug_logs": sorted(self._debug_logs),
            "watermark_opacity": self._watermark_opacity,
            "theme": theme_mod.current(),
            "enabled_programs": list(self._enabled),
            "reference_program": self._reference,
            "hidden_playlists": {aid: [list(p) for p in paths]
                                 for aid, paths in self._hidden.items()},
            # Which folders were open and what was selected, per tree.
            "tree_view": {
                source: {"expanded": state_mod.paths_to_json(data.get("expanded")),
                         "selected": state_mod.paths_to_json(data.get("selected"))}
                for source, data in self.playlist_panel.snapshot_view().items()
            },
            "tabs": self.track_tabs.capture_layout(),
            "sync_pane": self._sync_pane_on,
            "sync_pane_include_missing": self.sync_pane.include_missing.isChecked(),
            "sync_pane_split": self.sync_pane.splitter.sizes(),
        }

    def _restore_state(self):
        st = self.settings.ui_state or {}
        if not st:
            return
        if st.get("geometry"):
            self.restoreGeometry(state_mod.hex_to_qbytes(st["geometry"]))
        if st.get("h_splitter"):
            sizes = list(st["h_splitter"])
            # Sessions saved before the needs-sync pane existed hold two sizes
            # (trees, tracks); Qt leaves a short list undefined, so the new pane
            # is slotted in at zero rather than handed to setSizes as is.
            if len(sizes) == 2:
                sizes = [sizes[0], 0, sizes[1]]
            if len(sizes) == self.h_splitter.count():
                self.h_splitter.setSizes(sizes)
        self.sync_pane.include_missing.setChecked(
            bool(st.get("sync_pane_include_missing")))
        split = st.get("sync_pane_split")
        if isinstance(split, list) and len(split) == 2 and any(split):
            self.sync_pane.splitter.setSizes(split)
        if st.get("sync_pane"):
            self._set_sync_pane_visible(True)
        if st.get("v_splitter"):
            self.v_splitter.setSizes(st["v_splitter"])
        if st.get("volume") is not None:
            self.waveform_panel.set_volume_value(st["volume"])
        if st.get("tempo") is not None:
            self.waveform_panel.set_tempo_value(st["tempo"])
        if st.get("on_end"):
            self.waveform_panel.set_on_end_mode(st["on_end"])
        if st.get("time_remaining"):
            self.waveform_panel.set_show_remaining(True)
        if st.get("collapsed_sections"):
            self.playlist_panel.set_collapsed_sections(st["collapsed_sections"])
        if st.get("section_order"):
            self.playlist_panel.set_section_order(st["section_order"])
        # Sizes go LAST: the two calls above each fire a _relayout() that would
        # otherwise overwrite them, and set_section_order rewrites the order the
        # saved size list is positionally keyed to.
        if st.get("playlist_splitter"):
            sizes = list(st["playlist_splitter"])
            self.playlist_panel.restore_sizes(sizes)
            # __init__ runs before show(), so the splitter has no real geometry
            # yet and rescales whatever it is given. Re-apply once the layout is
            # real -- same deferral the scroll restore uses.
            QTimer.singleShot(0, lambda: self.playlist_panel.restore_sizes(sizes))
        if "sections_locked" in st:
            self.playlist_panel.set_sections_locked(bool(st["sections_locked"]))
        if "hidden_fields" in st:
            self._hidden_fields = set(st["hidden_fields"])
            for cid, act in self._column_actions.items():
                act.setChecked(cid not in self._hidden_fields)
        if st.get("font_pt"):
            self._font_pt = st["font_pt"]
        for t in self.track_tabs.all_tables():
            t.set_font_point_size(self._font_pt)
            t.set_hidden_fields(set(self._hidden_fields))
            t.set_watermark_opacity(self._watermark_opacity)
            if st.get("header_state"):
                t.restore_header_state(st["header_state"])
        if st.get("search_scope"):
            self.search_scope.setCurrentText(st["search_scope"])
        # app.py normally themes before the window is built (no light->dark
        # flash); this covers a MainWindow constructed without that step.
        if st.get("theme") and st["theme"] != theme_mod.current():
            self._set_theme(st["theme"])
        # Both of these need the libraries, which __init__ loads before calling
        # us, so the saved node_paths can be resolved to real nodes.
        if st.get("tree_view"):
            self.playlist_panel.restore_view({
                source: {"expanded": state_mod.json_to_paths(data.get("expanded")),
                         "selected": state_mod.json_to_paths(data.get("selected"))}
                for source, data in st["tree_view"].items()
            })
        if st.get("tabs"):
            self._restore_tabs(st["tabs"])

    def _restore_tabs(self, spec: dict) -> None:
        self.views.restore_tabs(spec)

    def _fill_pending(self, table) -> bool:
        return self.views.fill_pending(table)

    def _save_start_times(self) -> None:
        """Flush the audio-timing cache. Cheap and self-guarding: it writes only
        in ``update`` mode and only when something new was probed."""
        if self.start_time_cache is not None:
            self.start_time_cache.save()

    def closeEvent(self, event):
        self._save_start_times()           # a probe from any path, not just sync
        self._writer.flush()               # let any queued playlist writes finish
        self.settings.ui_state = self._capture_state()
        self.settings.save()
        super().closeEvent(event)
