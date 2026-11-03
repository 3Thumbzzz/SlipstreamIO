"""The Help window's Controls and Tutorial sections.

The point of these is drift: Help documents the keys that *cannot* be
reassigned, so the one real failure mode is Help and the Controls dialog both
claiming the same chord — the user presses it, gets the assignable action, and
the documented gesture appears broken. The rest guard the tutorial structure,
which exists specifically so GIFs can be dropped in later without a rewrite.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel                      # noqa: E402

from trackcleaner.config import controls as controls_mod               # noqa: E402
from trackcleaner.ui import help_window as help_mod                     # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# ============================ the fixed controls =============================
def test_no_fixed_control_is_also_assignable():
    """The drift that matters. If "W" were bound in the Controls dialog too,
    one of the two would silently never fire and Help would be lying."""
    assignable = {seq for seq in controls_mod.defaults().values() if seq}
    documented = {keys for _, rows in help_mod.FIXED_CONTROLS for keys, _ in rows}

    assert documented & assignable == set()


def test_every_documented_control_says_what_it_does():
    for group, rows in help_mod.FIXED_CONTROLS:
        assert rows, f"{group} documents nothing"
        for keys, what in rows:
            assert keys.strip(), group
            assert what.strip(), keys


def test_the_transport_keys_are_documented():
    """These are the ones a user cannot discover from a menu -- nothing in the
    UI says the digits fire hotcues."""
    documented = " ".join(keys for _, rows in help_mod.FIXED_CONTROLS
                          for keys, _ in rows)

    assert "1 – 8" in documented or "1 - 8" in documented
    assert "W" in documented


def test_the_double_tap_gesture_is_documented():
    """The headline behaviour of the arrow keys, and completely invisible
    otherwise."""
    text = " ".join(f"{keys} {what}" for _, rows in help_mod.FIXED_CONTROLS
                    for keys, what in rows)

    assert "Double-tap" in text


def test_the_docs_do_not_quote_a_configurable_timing():
    """The gesture windows moved into the Controls dialog. A hard number in Help
    would go stale the moment the slider is touched, and Help is the one place a
    user checks when a gesture stops working."""
    text = " ".join(f"{keys} {what}" for _, rows in help_mod.FIXED_CONTROLS
                    for keys, what in rows) + help_mod._controls_html()

    assert "200 ms" not in text
    assert "Controls" in text        # …and it says where the number now lives


def test_the_rendered_controls_html_lists_every_row():
    html = help_mod._controls_html()

    for group, rows in help_mod.FIXED_CONTROLS:
        assert group in html
        for keys, _ in rows:
            assert keys in html


def test_the_controls_html_points_at_the_dialog_for_the_rest():
    """Otherwise the section reads as "these are the controls", full stop."""
    html = help_mod._controls_html()

    assert "userControls.ini" in html


# ============================== the tutorial =================================
def test_every_topic_names_a_gif():
    for title, text, gif in help_mod.TUTORIAL_TOPICS:
        assert title.strip()
        assert text.strip()
        assert gif.endswith(".gif"), title


def test_gif_names_are_unique():
    """Two topics sharing a filename would show the same animation twice."""
    names = [gif for _, _, gif in help_mod.TUTORIAL_TOPICS]

    assert len(names) == len(set(names))


def test_the_tutorial_directory_is_under_images():
    got = help_mod.tutorial_dir()

    assert os.path.basename(got) == "tutorial"
    assert os.path.basename(os.path.dirname(got)) == "Images"
    assert os.path.isdir(os.path.dirname(got)), "Images/ should already exist"


def test_a_missing_gif_shows_a_placeholder_rather_than_a_blank(app, monkeypatch,
                                                               tmp_path):
    """The GIFs do not exist yet, so this is the shipping state: the section has
    to read as "coming", not as an empty page."""
    monkeypatch.setattr(help_mod, "tutorial_dir", lambda: str(tmp_path))
    page = help_mod._TutorialPage()

    texts = [w.text() for w in page.findChildren(QLabel) if w.text()]

    for _, _, gif in help_mod.TUTORIAL_TOPICS:
        assert any(gif in t for t in texts), gif


def test_a_present_gif_is_loaded_as_a_movie(app, monkeypatch, tmp_path):
    """And this is the state after they are dropped in -- proving the structure
    really does accept them without a rewrite."""
    gif = help_mod.TUTORIAL_TOPICS[0][2]
    # Smallest thing Qt will accept as a GIF; content does not matter, only that
    # the file exists and the branch takes.
    (tmp_path / gif).write_bytes(
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
        b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D"
        b"\x01\x00;")
    monkeypatch.setattr(help_mod, "tutorial_dir", lambda: str(tmp_path))

    page = help_mod._TutorialPage()

    assert len(page._movies) == 1
    assert os.path.basename(page._movies[0].fileName()) == gif


def test_the_movies_are_retained(app, monkeypatch, tmp_path):
    """A QMovie that is only a local stops playing the moment it is collected --
    the animation would appear as a single frozen frame."""
    monkeypatch.setattr(help_mod, "tutorial_dir", lambda: str(tmp_path))

    assert hasattr(help_mod._TutorialPage(), "_movies")


# ============================== the window ===================================
def test_the_window_has_its_four_sections(app):
    w = help_mod.HelpWindow()

    tabs = [w.tabs.tabText(i) for i in range(w.tabs.count())]

    assert tabs == ["Feature guide", "Status symbols", "Controls", "Tutorial"]
    w.close()


def test_the_feature_guide_is_still_first(app):
    """It was the whole window before this change; demoting it would move
    everything the user already knows where to find."""
    w = help_mod.HelpWindow()

    assert "Feature Guide" in w.tabs.widget(0).toHtml()
    w.close()



# ============================ the status symbols =============================
def _statuses_the_app_draws():
    """Every status id the app paints: the diff's six, the placeholder grey a
    sidebar row starts with, and the two sync-preview row markers."""
    from trackcleaner.model import status as st
    from trackcleaner.ui import status_squares
    return ({st.GREEN, st.BLACK, st.RED, *st.YELLOWS, "grey"}
            | set(status_squares.TOMBSTONE_STATUSES))


def test_every_status_the_app_draws_is_in_the_legend():
    """The drift that matters: a glyph on screen that Help never explains."""
    from trackcleaner.ui import status_squares
    legend = {s for s, _n, _m in status_squares.PLAYLIST_LEGEND
              + status_squares.TRACK_LEGEND}

    assert _statuses_the_app_draws() <= legend


def test_every_legend_row_names_and_explains_its_symbol():
    from trackcleaner.ui import status_squares
    for status, name, meaning in (status_squares.PLAYLIST_LEGEND
                                  + status_squares.TRACK_LEGEND):
        assert name.strip() and meaning.strip(), status


def test_the_symbols_tab_draws_every_glyph_in_both_skins(app):
    """Drawn by the app's own painter, so the legend cannot show a symbol the
    panes do not. A missing resource renders as a broken image."""
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QTextDocument
    from trackcleaner.ui import status_squares, theme as theme_mod
    help_window = help_mod.HelpWindow()
    titles = [help_window.tabs.tabText(i) for i in range(help_window.tabs.count())]
    page = help_window.tabs.widget(titles.index("Status symbols"))

    for theme in (theme_mod.LIGHT, theme_mod.DARK):
        for status, _n, _m in (status_squares.PLAYLIST_LEGEND
                               + status_squares.TRACK_LEGEND):
            image = page.document().resource(QTextDocument.ImageResource,
                                             QUrl(f"status-{theme}:{status}"))
            assert image is not None and not image.isNull(), (theme, status)


def test_the_dark_skin_draws_the_unsynced_square_light():
    """What a dark-skin user sees as a white square is the black status --
    the legend shows both skins so that is not a mystery."""
    from PySide6.QtGui import QColor
    from trackcleaner.ui import status_squares, theme as theme_mod
    light = QColor(status_squares.color_for("black", theme_mod.LIGHT)).lightness()
    dark = QColor(status_squares.color_for("black", theme_mod.DARK)).lightness()

    assert dark > 160 > light


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
