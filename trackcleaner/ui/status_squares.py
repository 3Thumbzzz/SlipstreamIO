"""Small status icons for playlists and tracks.

A status says two things, and the icon shows both:

    colour  green synced · yellow changed · black absent · red error
    shape   square    relationship to the REFERENCE program
            triangle  self reference -- this copy is the one that changed
            fork      another, non-reference program changed
            X         an error, deliberately not shaped like a sync state

So a yellow triangle is "you changed this", a yellow square is "the reference
changed and you are behind", and a yellow fork is "some other program moved".
Green is always a square, because green only ever means "matches the reference".

The colour names are ids, not literal colours: on a dark background a near-black
"unsynced" square is invisible, so dark mode renders it as a light neutral. The
two tombstone greys move *down* in dark mode for the same reason the unsynced
square moves up -- otherwise "unsynced" and "the other program added this" would
be near-identical shades.

Icons are cached, so the cache key includes the theme; without that a theme
switch would hand back the previous theme's pixmaps.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygon
from PySide6.QtCore import QPoint

from . import theme as theme_mod

# status id -> (colour id, shape). Anything not listed falls back to a square in
# its own colour, which keeps the tombstone markers working unchanged.
SQUARE, TRIANGLE, FORK, CROSS = "square", "triangle", "fork", "cross"

_SHAPES = {
    "green": ("green", SQUARE),          # synced to the reference
    "black": ("black", SQUARE),          # not in the reference yet
    "red": ("red", CROSS),               # error: a track cannot be found
    "yellow_self": ("yellow", TRIANGLE),   # this copy changed
    "yellow_ref": ("yellow", SQUARE),      # the reference changed
    "yellow_other": ("yellow", FORK),      # another program changed
}

_COLORS = {
    theme_mod.LIGHT: {
        "green": "#2ecc71",
        "black": "#3b3b3b",
        "yellow": "#f1c40f",
        "red": "#e74c3c",
        # sync-preview markers on the un-edited copy of a playlist:
        "grey": "#8a8a8a",     # losing: a track a sync will remove from here
        "green2": "#0b6b2e",   # gaining: a track a sync will add here (deep green)
    },
    theme_mod.DARK: {
        "green": "#2ecc71",
        "black": "#d6d6d6",    # light neutral: "empty" but visible on dark
        "yellow": "#f1c40f",
        "red": "#e74c3c",
        "grey": "#6f6f6f",
        "green2": "#1f8f46",   # gaining (deep green, lifted for dark bg)
    },
}

_OUTLINE = {theme_mod.LIGHT: "#222222", theme_mod.DARK: "#6a6a6a"}

# Rows rendered with these statuses are sync-preview markers, greyed/tinted and
# excluded from real membership. "grey" recolours a real member row (losing);
# "green2" is an appended ghost row (gaining).
TOMBSTONE_STATUSES = ("grey", "green2")

_cache: dict[tuple[str, str, int], QIcon] = {}


def color_for(status: str, theme: str | None = None) -> str:
    palette = _COLORS[theme or theme_mod.current()]
    colour = _SHAPES.get(status, (status, SQUARE))[0]
    return palette.get(colour, palette["grey"])


def shape_for(status: str) -> str:
    """The glyph a status draws with. Unknown ids -- the tombstone markers --
    stay squares, so nothing has to be registered to keep working."""
    return _SHAPES.get(status, (status, SQUARE))[1]


def _draw(p: QPainter, shape: str, size: int) -> None:
    """Antialiasing stays off for the square so its edges land on pixels, but on
    for the diagonal shapes, which look ragged without it at 12 px."""
    edge = size - 3
    if shape == TRIANGLE:
        p.setRenderHint(QPainter.Antialiasing, True)
        p.drawPolygon(QPolygon([QPoint(size // 2, 1),
                                QPoint(size - 2, size - 2),
                                QPoint(1, size - 2)]))
    elif shape == FORK:
        # An upside-down tuning fork: two prongs hanging from a crossbar, with
        # the stem rising out of the middle.
        p.setRenderHint(QPainter.Antialiasing, False)
        mid, bar = size // 2, size // 2
        pen = QPen(p.brush().color())
        pen.setWidth(max(2, size // 6))
        p.setPen(pen)
        p.drawLine(mid, 1, mid, bar)                  # stem, upward
        p.drawLine(2, bar, size - 3, bar)             # crossbar
        p.drawLine(2, bar, 2, size - 2)               # left prong, downward
        p.drawLine(size - 3, bar, size - 3, size - 2)  # right prong
    elif shape == CROSS:
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(p.brush().color())
        pen.setWidth(max(2, size // 5))
        p.setPen(pen)
        p.drawLine(2, 2, size - 3, size - 3)
        p.drawLine(size - 3, 2, 2, size - 3)
    else:
        p.setRenderHint(QPainter.Antialiasing, False)
        p.drawRect(1, 1, edge, edge)


def status_pixmap(status: str, size: int = 12, theme: str | None = None) -> QPixmap:
    """The glyph as drawn in ``theme`` (default: the current one). Help draws
    both skins side by side through this, so its legend is the real artwork."""
    theme = theme or theme_mod.current()
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setBrush(QColor(color_for(status, theme)))
    p.setPen(QColor(_OUTLINE[theme]))
    _draw(p, shape_for(status), size)
    p.end()
    return pm


def status_icon(status: str, size: int = 12) -> QIcon:
    theme = theme_mod.current()
    key = (theme, status, size)
    if key in _cache:
        return _cache[key]
    icon = QIcon(status_pixmap(status, size, theme))
    _cache[key] = icon
    return icon


#: Every glyph a **playlist** can show, as ``(status, name, meaning)``. Rendered
#: by Help → Status symbols. "grey" here is the placeholder a sidebar row starts
#: with, not a tombstone: a playlist is never removed row by row.
PLAYLIST_LEGEND = [
    ("green", "Synced",
     "The reference program has this playlist with the same tracks in the same "
     "order, and every track in it is green."),
    ("yellow_self", "This copy changed",
     "This program changed the playlist, or a track in it, since the copies last "
     "agreed. Sync from here to push the change. Also shown on both sides when "
     "there is no history to say who changed it."),
    ("yellow_ref", "Behind the reference",
     "The reference program changed it and this copy has not caught up. Sync "
     "from the reference to bring it up to date."),
    ("yellow_other", "Another program changed it",
     "Neither this copy nor the reference made the change — a third program "
     "did. Only seen with three or more programs on."),
    ("black", "Not in the reference yet",
     "The reference has no playlist at this path. On the reference's own pane: "
     "no other program has it yet. The dark skin draws this as a light square."),
    ("red", "A track cannot be found",
     "At least one track in it is missing — its file is not on disk, or the "
     "playlist names a track the collection has no entry for."),
    ("grey", "Not compared yet",
     "Shown while libraries load, when only one program is switched on, and in "
     "the Hidden section."),
]

#: Every glyph a **track row** can show in the Status column.
TRACK_LEGEND = [
    ("green", "Synced",
     "Cues, beatgrid and tags match the reference program."),
    ("yellow_self", "This copy changed",
     "This program's cues or tags differ, and this program is the one that "
     "changed them."),
    ("yellow_ref", "Behind the reference",
     "The reference program's copy changed; this one is out of date."),
    ("yellow_other", "Another program changed it",
     "A third program changed the track. Three or more programs only."),
    ("black", "Not in the reference yet",
     "The reference program's collection has no entry for this track."),
    ("red", "Cannot be found",
     "The audio file is not on disk at the path this program records, or the "
     "playlist names a track the collection does not hold."),
    ("grey", "Will be removed",
     "Preview: this copy of the playlist holds a track the other copy dropped. "
     "A sync from the other side takes it out."),
    ("green2", "Will be added",
     "Preview row: a track the other copy has and this one does not yet. A "
     "sync from the other side adds it. Not a real member until then."),
]
