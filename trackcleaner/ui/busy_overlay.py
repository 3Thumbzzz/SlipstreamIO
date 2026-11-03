"""A centred "busy" overlay that freezes the window during a blocking operation.

Backups and syncs run synchronously on the main thread, so there is no need for
threads: paint the overlay, then run the operation. The event loop is blocked for
the duration, which *is* the freeze -- the overlay just has to be visible before
the blocking call, which ``repaint()`` guarantees.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QWidget

from . import theme as theme_mod


class BusyOverlay(QWidget):
    """Full-window translucent cover with a centred message card."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._message = ""
        self.setVisible(False)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)  # swallow input

    def _cover_parent(self):
        self.setGeometry(self.parent().rect())

    def paintEvent(self, _event):
        dark = theme_mod.current() == theme_mod.DARK
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 150))

        card_w, card_h = 360, 90
        x = (self.width() - card_w) // 2
        y = (self.height() - card_h) // 2
        card = QRect(x, y, card_w, card_h)
        p.setBrush(QColor("#2b2b2b" if dark else "#fafafa"))
        p.setPen(QColor("#555555"))
        p.drawRoundedRect(card, 8, 8)

        p.setPen(QColor("#e8e8e8" if dark else "#222222"))
        f = QFont()
        f.setPointSize(12)
        f.setBold(True)
        p.setFont(f)
        p.drawText(card, Qt.AlignCenter, self._message)

    def run(self, message: str, fn):
        """Show ``message``, run ``fn()`` (blocking), then hide. Returns fn's
        result; always hides even if fn raises."""
        self._message = message
        self._cover_parent()
        self.raise_()
        self.show()
        self.repaint()                  # paint before the blocking call
        try:
            return fn()
        finally:
            self.hide()

    # Keep covering the window if it resizes while somehow left visible.
    def showEvent(self, event):
        self._cover_parent()
        super().showEvent(event)
