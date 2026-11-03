"""Session-state capture/restore.

The MainWindow owns the widgets; this module only provides JSON-safe helpers
and the schema documentation. State is persisted inside
``Settings.ui_state`` (see :mod:`trackcleaner.config.paths`).

Persisted keys:
    geometry          hex of QMainWindow.saveGeometry()
    h_splitter        [int]   main horizontal splitter sizes: trees | needs-sync
                              pane | tracks (older sessions saved two)
    sync_pane         bool    the View → Playlists needing sync pane is shown
    sync_pane_include_missing  bool  that pane also lists BLACK playlists
    sync_pane_split   [int]   that pane's playlists | songs-to-sync sizes
    left_splitter     [int]   left column (trees | fields) sizes
    playlist_splitter [int]   hidden | vdj | traktor sizes
    header_state      hex of QHeaderView.saveState() (column order+width+sort)
    hidden_fields     [str]   hidden column ids
    font_pt           int     track-table font point size
    search_scope      str     ALL | Traktor | VDJ
    hidden_playlists  {"vdj":[[names...]], "traktor":[[names...]]}
    tree_view         {"vdj": {"expanded": [[names...]], "selected": [[...]]},
                       "traktor": {...}}   -- sidebar expansion + selection
    tabs              track-area layout; see TrackTabs.capture_layout()
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray


def qbytes_to_hex(b: QByteArray) -> str:
    return bytes(b.toHex()).decode("ascii")


def hex_to_qbytes(s: str) -> QByteArray:
    if not s:
        return QByteArray()
    return QByteArray.fromHex(QByteArray(s.encode("ascii")))


def paths_to_json(paths) -> list[list[str]]:
    """Node paths are sets of tuples in memory and lists of lists on disk --
    neither a set nor a tuple survives ``json.dump``. Sorted so a session file
    doesn't churn between saves that changed nothing."""
    return sorted([list(p) for p in (paths or ())])


def json_to_paths(raw) -> set[tuple]:
    return {tuple(p) for p in (raw or ())}
