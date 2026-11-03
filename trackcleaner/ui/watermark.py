"""Faint program logos behind the track panes.

**Composited over the rows, not behind them.** The table draws alternating row
colours, and both of those brushes are opaque -- anything painted underneath is
covered wherever a row exists, which is precisely the area a watermark is meant
to occupy. Painting on top at a low opacity is what actually produces the effect;
at the default it reads as a tint rather than a picture, and the slider exists
because the right number depends on the theme.

Pixmaps are cached per (file, size, opacity) and the opacity is baked into the
alpha, so a repaint -- which happens on every scroll -- is a single blit.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter, QPixmap

from ..config import paths as paths_mod

#: "Very faintly" -- the user's phrase. Their "80-90%" is read as transparency,
#: so this is what is left of it.
DEFAULT_OPACITY = 12

#: Fraction of the pane's shorter side the logo spans. Large enough to read as a
#: watermark, small enough that it never crowds the first column.
MAX_FRACTION = 0.6

#: Below this the pane is a sliver (a collapsed section, a mid-drag resize) and
#: scaling art into it is wasted work.
MIN_PANE_PX = 40

_cache: dict = {}


def clear_cache() -> None:
    _cache.clear()


def watermark_pixmap(logo_file: str, size: QSize, opacity: int):
    """The logo scaled and faded for a pane of ``size``, or ``None``.

    ``None`` covers every "draw nothing" case -- no artwork named, the file
    absent, opacity 0, a pane too small to matter -- so the caller has one check
    rather than four. A missing image is deliberately not an error: artwork is
    loose files, and a program with none should simply have no watermark.
    """
    opacity = max(0, min(100, int(opacity)))
    if not logo_file or opacity == 0:
        return None
    width, height = size.width(), size.height()
    if width < MIN_PANE_PX or height < MIN_PANE_PX:
        return None

    key = (logo_file, width, height, opacity)
    if key in _cache:
        return _cache[key]

    path = paths_mod.image_path(logo_file)
    if not os.path.exists(path):
        _cache[key] = None
        return None
    source = QPixmap(path)
    if source.isNull():
        _cache[key] = None
        return None

    side = int(min(width, height) * MAX_FRACTION)
    scaled = source.scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    faded = QPixmap(scaled.size())
    faded.fill(Qt.transparent)
    painter = QPainter(faded)
    painter.setOpacity(opacity / 100.0)
    painter.drawPixmap(0, 0, scaled)
    painter.end()

    _cache[key] = faded
    return faded


def draw_watermark(painter: QPainter, rect, pixmap) -> bool:
    """Centre ``pixmap`` in ``rect``. False when there was nothing to draw, so
    the caller can skip the rest without repeating the None check."""
    if pixmap is None or pixmap.isNull():
        return False
    x = rect.x() + (rect.width() - pixmap.width()) // 2
    y = rect.y() + (rect.height() - pixmap.height()) // 2
    painter.drawPixmap(x, y, pixmap)
    return True
