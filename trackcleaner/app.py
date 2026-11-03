"""Application entry point: build the QApplication and MainWindow."""

from __future__ import annotations

import os
import sys

from PySide6.QtWidgets import QApplication

from .config.paths import Settings
from .ui import theme as theme_mod
from .ui.main_window import MainWindow

#: Where Homebrew puts ffmpeg/ffprobe (Apple silicon, then Intel).
_MAC_TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def _find_mac_tools() -> None:
    """Let a macOS app opened from Finder find ffmpeg and ffprobe.

    Finder starts apps with a bare PATH (``/usr/bin:/bin:/usr/sbin:/sbin``)
    that never includes Homebrew, so decoding and the start-time probe would
    fail although ``ffmpeg`` works in Terminal. Only directories that exist and
    are not already listed are added, after what is there.
    """
    if sys.platform != "darwin":
        return
    parts = os.environ.get("PATH", "").split(os.pathsep)
    extra = [d for d in _MAC_TOOL_DIRS if os.path.isdir(d) and d not in parts]
    if extra:
        os.environ["PATH"] = os.pathsep.join([p for p in parts if p] + extra)


def main() -> int:
    _find_mac_tools()
    app = QApplication(sys.argv)
    app.setApplicationName("SlipStreamIO")
    settings = Settings.load()
    # Capture the startup look before anything is themed (that is what LIGHT
    # restores), then theme *before* building the window so widgets are born
    # with the right palette instead of flashing light then dark.
    theme_mod.capture_defaults(app)
    theme_mod.apply(app, (settings.ui_state or {}).get("theme", theme_mod.LIGHT))
    window = MainWindow(settings)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
