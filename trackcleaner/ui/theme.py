"""Light/dark theming for the whole application.

Light mode is not a palette we invent -- it is the style and palette Qt gave us
at startup, captured once by :func:`capture_defaults` and restored verbatim. So
"light" always means exactly what the app looked like before theming existed.

Dark mode cannot be done with a palette alone. The app runs on Qt's
``windowsvista`` style, which paints its own chrome and ignores QPalette: with a
dark palette applied, a rendered menubar and button sample as ``#000000`` while
only the palette-driven tree goes dark. Fusion honours the palette throughout,
so dark mode switches style as well:

    style        menubar   tree      button
    windowsvista #000000   #232323   #000000
    Fusion       #2b2b2b   #232323   #2b2b2b

Order matters: ``setStyle`` resets the palette, so style is always set first.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

from ..adapters.registry import ADAPTER_CLASSES as _ADAPTER_CLASSES

LIGHT = "light"
DARK = "dark"

# Dark shades. The window sits at _WINDOW; panels/buttons one step lighter and
# borders lighter again, which is what visually separates the panes.
_WINDOW = "#2b2b2b"
_BASE = "#232323"          # text/tree/table backgrounds
_ALT_BASE = "#2f2f2f"      # alternating rows
_BUTTON = "#3a3a3a"        # buttons, headers, panel chrome
_BORDER = "#454545"        # splitter handles + frame borders
_TEXT = "#e0e0e0"
_DISABLED = "#7a7a7a"
_HIGHLIGHT = "#2f6ea5"

# Accents that must differ per theme (too dark to read on _WINDOW).
WARNING_FG = {LIGHT: "#b36b00", DARK: "#e0a53c"}
TOMBSTONE_FG = {LIGHT: "#9a9a9a", DARK: "#8d8d8d"}

# The selection colour of a split pane that is NOT the active target. Qt only
# switches to the Inactive palette group when the whole window deactivates, so
# without this both panes paint their selection as if they were the target.
# Deliberately the same grey the tab bar already dims a non-focused group with --
# the two cues mean the same thing and must not drift apart.
HIGHLIGHT_MUTED = "#7a7a7a"
HIGHLIGHT_MUTED_TEXT = {LIGHT: "#ffffff", DARK: "#e0e0e0"}

# ---- waveform panel ----
WAVE_BG = {LIGHT: "#fafafa", DARK: "#1c1c1c"}
# Orange, deliberately: the cue markers are Traktor blue, so a blue envelope
# made the ticks hard to pick out against it.
WAVE_FG = {LIGHT: "#d2691e", DARK: "#ff9640"}      # the envelope itself
WAVE_MID = {LIGHT: "#d0d0d0", DARK: "#3a3a3a"}     # centre line
# Purple, not red: against an orange envelope a red playhead nearly vanishes
# (RGB separation 77 vs purple's 191), and white collides with the grid-cue
# colour in dark mode.
WAVE_PLAYHEAD = {LIGHT: "#8e24aa", DARK: "#e040fb"}
WAVE_TEXT = {LIGHT: "#666666", DARK: "#9a9a9a"}    # "decoding…" / "no track"

# Hotcue colours follow Traktor's convention. Neither collection.nml nor
# database.xml stores a cue colour, but a Traktor "Beat Marker" (CUE_V2 TYPE=4)
# emits a beatgrid Poi alongside its cue Poi, so grid cues stay distinguishable
# from plain ones -- see cues.py::pois_from_cue_v2.
CUE_PLAIN = {LIGHT: "#2f8fd8", DARK: "#2f8fd8"}    # Traktor cue blue
CUE_GRID = {LIGHT: "#4a5560", DARK: "#e8e8e8"}     # Traktor grid white (dark slate on light)
CUE_ABSENT = {LIGHT: "#c8c8c8", DARK: "#4a4a4a"}   # no such cue -> flat, disabled

# Track-tab colours by source (fixed, per the spec -- not theme-derived).
# Each program declares its own on its adapter, so adding one needs no edit
# here; "" is the neutral fallback for a tab holding no program's playlist.
TAB_BG = {"": "#8a8a8a"}
TAB_FG = {"": "#e8e8e8"}
TAB_BG_INACTIVE = {"": "#5f5f5f"}
for _cls in _ADAPTER_CLASSES:
    TAB_BG[_cls.id] = _cls.tab_bg
    TAB_FG[_cls.id] = _cls.tab_fg
    TAB_BG_INACTIVE[_cls.id] = _cls.tab_bg_inactive

# Fusion draws splitter handles and frames from the palette in a way that leaves
# the panes visually merged; these few rules are what the palette cannot express.
_DARK_QSS = f"""
QSplitter::handle {{ background: {_BORDER}; }}
QSplitter::handle:horizontal {{ width: 3px; }}
QSplitter::handle:vertical {{ height: 3px; }}
QTreeWidget, QTreeView, QTableView, QLineEdit, QTextBrowser {{
    border: 1px solid {_BORDER};
}}
QHeaderView::section {{
    background: {_BUTTON};
    color: {_TEXT};
    border: 0px;
    border-right: 1px solid {_BORDER};
    border-bottom: 1px solid {_BORDER};
    padding: 3px;
}}
QTableView {{ gridline-color: {_BORDER}; }}
"""

_default_style: str = ""
_default_palette: QPalette | None = None
_applied_style: str = ""          # what apply() last handed to setStyle
_current: str = LIGHT


def style_name(app) -> str:
    """The name of the app's *real* style.

    A non-empty application stylesheet makes Qt wrap the style in a
    ``QStyleSheetStyle`` whose ``objectName()`` is empty, and PySide6 exposes no
    way to unwrap it -- so fall back to the name we last applied ourselves.
    """
    return app.style().objectName() or _applied_style


def capture_defaults(app) -> None:
    """Remember the startup style + palette, so LIGHT can restore them exactly.

    Idempotent: only the first call is recorded, and it must happen before any
    theme is applied.
    """
    global _default_style, _default_palette, _applied_style
    if _default_palette is None:
        # Called before any theming (app.py guarantees it, and apply() calls it
        # first), so no stylesheet of ours is active and the name is the real one.
        _default_style = app.style().objectName()
        _applied_style = _default_style
        _default_palette = QPalette(app.palette())


def current() -> str:
    """The active theme, for code that draws its own colours."""
    return _current


def _dark_palette() -> QPalette:
    p = QPalette()
    window, base, alt = QColor(_WINDOW), QColor(_BASE), QColor(_ALT_BASE)
    button, text = QColor(_BUTTON), QColor(_TEXT)
    p.setColor(QPalette.Window, window)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, alt)
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, button)
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.BrightText, QColor("#ff6b6b"))
    p.setColor(QPalette.ToolTipBase, QColor(_BUTTON))
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.Highlight, QColor(_HIGHLIGHT))
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.Link, QColor("#5aa9e6"))
    p.setColor(QPalette.PlaceholderText, QColor(_DISABLED))
    # Without these Fusion renders disabled text at near-full contrast.
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(_DISABLED))
    p.setColor(QPalette.Disabled, QPalette.Highlight, QColor("#3f3f3f"))
    return p


def apply(app, theme: str) -> str:
    """Apply ``theme`` app-wide. Returns the theme actually applied."""
    global _current, _applied_style
    capture_defaults(app)                 # no-op if already captured
    theme = DARK if theme == DARK else LIGHT
    if theme == DARK:
        app.setStyle("Fusion")            # resets the palette -- set it after
        _applied_style = "Fusion"
        app.setPalette(_dark_palette())
        app.setStyleSheet(_DARK_QSS)
    else:
        app.setStyleSheet("")             # drop the proxy style first
        if _default_style:
            app.setStyle(_default_style)
            _applied_style = _default_style
        if _default_palette is not None:
            app.setPalette(QPalette(_default_palette))
    _current = theme
    return theme
