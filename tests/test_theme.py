"""Light/dark theming.

The two things most likely to break silently: LIGHT must restore the *exact*
startup look (it is captured, not re-derived), and the status-square icon cache
must be keyed by theme or a switch hands back the previous theme's pixmaps.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QPalette                       # noqa: E402
from PySide6.QtWidgets import QApplication                       # noqa: E402

from trackcleaner.ui import status_squares, theme                # noqa: E402


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    theme.capture_defaults(a)
    yield a
    theme.apply(a, theme.LIGHT)


def _snapshot(app):
    p = app.palette()
    roles = (QPalette.Window, QPalette.WindowText, QPalette.Base,
             QPalette.AlternateBase, QPalette.Text, QPalette.Button,
             QPalette.ButtonText, QPalette.Highlight)
    return theme.style_name(app), tuple(p.color(r).name() for r in roles)


def test_light_restores_the_captured_startup_look(app):
    theme.apply(app, theme.LIGHT)
    before = _snapshot(app)

    theme.apply(app, theme.DARK)
    assert _snapshot(app) != before, "dark must actually change something"

    theme.apply(app, theme.LIGHT)
    assert _snapshot(app) == before, "light must restore style AND palette exactly"
    assert app.styleSheet() == ""


def test_dark_uses_fusion_and_a_dark_window(app):
    # windowsvista ignores QPalette (its menubar/buttons render #000000), so
    # dark mode has to switch style as well.
    theme.apply(app, theme.DARK)
    assert theme.style_name(app).lower() == "fusion"
    assert app.palette().color(QPalette.Window).lightness() < 90
    assert app.palette().color(QPalette.WindowText).lightness() > 160
    theme.apply(app, theme.LIGHT)


def test_style_name_sees_through_the_stylesheet_proxy(app):
    """A non-empty app stylesheet wraps the style in QStyleSheetStyle, whose
    objectName is empty -- capture/restore must not be fooled by it."""
    theme.apply(app, theme.DARK)
    assert app.styleSheet(), "dark mode sets a stylesheet"
    assert app.style().objectName() == "", "the proxy is what makes this subtle"
    assert theme.style_name(app).lower() == "fusion"
    theme.apply(app, theme.LIGHT)


def test_disabled_text_stays_dimmer_than_normal_text(app):
    theme.apply(app, theme.DARK)
    p = app.palette()
    normal = p.color(QPalette.Active, QPalette.WindowText).lightness()
    disabled = p.color(QPalette.Disabled, QPalette.WindowText).lightness()
    assert disabled < normal, "Fusion renders disabled text at full contrast otherwise"
    theme.apply(app, theme.LIGHT)


def test_dark_menubar_actually_renders_dark(app):
    """The reason dark mode switches style at all: under a native Windows style
    a dark palette leaves the menubar painting itself #000000. Render one and
    look at the pixels rather than trusting the palette."""
    from PySide6.QtWidgets import QMainWindow

    theme.apply(app, theme.DARK)
    w = QMainWindow()
    w.menuBar().addMenu("Paths")
    w.resize(400, 200)
    w.show()
    app.processEvents()
    px = QColor(w.grab().toImage().pixel(300, 10))
    w.close()
    theme.apply(app, theme.LIGHT)

    assert px.name() != "#000000", "menubar painted unthemed black"
    assert px.lightness() < 90, f"menubar not dark: {px.name()}"


def test_unknown_theme_falls_back_to_light(app):
    assert theme.apply(app, "chartreuse") == theme.LIGHT
    assert theme.current() == theme.LIGHT


# ------------------------- status squares -------------------------
def test_icon_cache_is_keyed_by_theme(app):
    theme.apply(app, theme.LIGHT)
    light = status_squares.status_icon("black").pixmap(12, 12).toImage()
    theme.apply(app, theme.DARK)
    dark = status_squares.status_icon("black").pixmap(12, 12).toImage()
    theme.apply(app, theme.LIGHT)
    assert light != dark, "a cache keyed only by (status, size) returns stale icons"


def test_unsynced_square_is_visible_against_its_own_background(app):
    """Near-black on dark grey is invisible; that is the whole reason the
    unsynced swatch flips in dark mode."""
    for th, bg in ((theme.LIGHT, "#f0f0f0"), (theme.DARK, "#2b2b2b")):
        swatch = QColor(status_squares.color_for("black", th)).lightness()
        assert abs(swatch - QColor(bg).lightness()) > 60, f"{th} unsynced square"


def test_dark_swatches_do_not_collide(app):
    """The two neutral squares -- unsynced 'black' and losing 'grey' -- must stay
    apart in lightness in dark mode; the gaining 'green2' is distinguished by hue
    (deep green) rather than lightness."""
    black_l = QColor(status_squares.color_for("black", theme.DARK)).lightness()
    grey_l = QColor(status_squares.color_for("grey", theme.DARK)).lightness()
    assert abs(black_l - grey_l) > 25, f"black vs grey too close: {black_l} {grey_l}"
    g = QColor(status_squares.color_for("green2", theme.DARK))
    assert g.green() > g.red() + 40 and g.green() > g.blue() + 40, "green2 must read as green"


def test_every_status_resolves_in_both_themes(app):
    for th in (theme.LIGHT, theme.DARK):
        for status in ("green", "black", "yellow", "red", "grey", "green2"):
            assert QColor(status_squares.color_for(status, th)).isValid()


# ==================== per-pane selection across a theme switch ==============
def test_a_muted_pane_stays_muted_across_a_theme_switch(app):
    """A per-widget palette override outlives an app-wide palette swap, so
    whoever changes the theme has to re-assert it -- otherwise the unfocused
    pane keeps the OLD theme's palette wholesale."""
    from PySide6.QtCore import Qt
    from trackcleaner.ui.track_tabs import TrackTabs
    tabs = TrackTabs(on_clicked=lambda k: None, on_double=lambda k: None,
                     on_context=lambda k, p: None)
    tabs._groups()[0].new_tab()
    tabs.split_from(tabs._groups()[0], Qt.Horizontal)
    muted = [t for t in tabs.all_tables() if not t.pane_active]
    assert muted

    theme.apply(app, theme.DARK)
    tabs.refresh_pane_focus()                 # what MainWindow._set_theme does

    dark_hl = QColor(theme.HIGHLIGHT_MUTED).name()
    for table in tabs.all_tables():
        got = table.palette().color(QPalette.Active, QPalette.Highlight).name()
        if table.pane_active:
            assert got == app.palette().color(
                QPalette.Active, QPalette.Highlight).name(), \
                "the active pane takes the NEW theme's highlight"
        else:
            assert got == dark_hl
    theme.apply(app, theme.LIGHT)


def test_the_tab_marker_and_the_muted_selection_are_the_same_grey(app):
    """They mean the same thing ('not the active group') and must not drift."""
    from trackcleaner.ui import track_tabs
    assert track_tabs._MUTED_ACCENT == theme.HIGHLIGHT_MUTED


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
