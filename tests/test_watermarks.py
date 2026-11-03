"""Program logos: the pane watermark and the sidebar section icon.

Two things are actually load-bearing here. **Which logo belongs to which
program** -- the supplied files do not follow the adapter ids, so a mapping
mistake would ship a Traktor logo over VirtualDJ's tracks and look deliberate.
And **a missing image must be nothing to draw, never an exception**: the artwork
is loose files, so any of it can be absent at runtime.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize                                        # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget                     # noqa: E402

from trackcleaner.adapters import registry                              # noqa: E402
from trackcleaner.config import paths as paths_mod                      # noqa: E402
from trackcleaner.ui import watermark                                   # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _clear_cache():
    watermark.clear_cache()
    yield
    watermark.clear_cache()


# ============================== the mapping =================================
#: Programs registered before their artwork exists. "No logo" is a supported
#: state (the pane and the section simply draw none), but it has to be a known
#: one: a program missing from here still fails the test below.
_AWAITING_ARTWORK = {"rekordbox", "serato"}


def test_every_program_names_a_logo():
    for cls in registry.ADAPTER_CLASSES:
        if cls.id in _AWAITING_ARTWORK:
            assert not cls.logo_file, f"{cls.id} has artwork now; drop it from the list"
            continue
        assert cls.logo_file, cls.id


def test_every_named_logo_is_actually_there():
    """Named-but-absent is the failure this catches: the adapter would claim
    artwork and every pane would silently draw nothing."""
    for cls in registry.ADAPTER_CLASSES:
        if cls.logo_file:
            assert os.path.exists(paths_mod.image_path(cls.logo_file)), cls.logo_file


def test_no_two_programs_share_a_logo():
    files = [cls.logo_file for cls in registry.ADAPTER_CLASSES if cls.logo_file]

    assert len(files) == len(set(files))


def _mean_opaque_colour(path):
    """Average colour of the non-transparent pixels -- what the eye reads as
    "the logo's colour". Sampled rather than trusted, because "vdjl.png" names
    no program and a filename is not evidence."""
    from PySide6.QtGui import QImage
    image = QImage(path).convertToFormat(QImage.Format_ARGB32)
    total = [0, 0, 0]
    count = 0
    for y in range(0, image.height(), 4):
        for x in range(0, image.width(), 4):
            pixel = image.pixelColor(x, y)
            if pixel.alpha() > 200:
                total[0] += pixel.red()
                total[1] += pixel.green()
                total[2] += pixel.blue()
                count += 1
    if not count:
        return None
    return tuple(c // count for c in total)


@pytest.mark.parametrize("adapter_id, dominant", [
    ("vdj", "red"),          # VirtualDJ's branding
    ("slipstream", "blue"),  # purple: blue channel over red over green
    ("traktor", "grey"),
])
def test_each_logo_looks_like_its_program(app, adapter_id, dominant):
    """Guards the id -> file mapping by what the image *is*, so renaming a file
    into the wrong slot fails here rather than in front of the user."""
    path = paths_mod.image_path(registry.logo_file(adapter_id))
    red, green, blue = _mean_opaque_colour(path)

    if dominant == "red":
        assert red > green + 40 and red > blue + 40, (red, green, blue)
    elif dominant == "blue":
        assert blue > green + 40, (red, green, blue)
    else:
        assert abs(red - blue) < 30 and abs(red - green) < 30, (red, green, blue)


def test_an_unknown_program_has_no_logo():
    """"hidden" is a real sidebar section with no program behind it."""
    assert registry.logo_file("hidden") == ""
    assert registry.logo_file("") == ""


# ============================== the pixmap ==================================
def test_a_named_logo_scales_into_the_pane(app):
    got = watermark.watermark_pixmap("traktor.png", QSize(800, 600), 12)

    assert got is not None
    assert max(got.width(), got.height()) <= int(600 * watermark.MAX_FRACTION) + 1


def test_it_fits_the_shorter_side(app):
    """A tall narrow pane must not get a logo wider than itself."""
    got = watermark.watermark_pixmap("traktor.png", QSize(200, 900), 12)

    assert got.width() <= 200


def test_zero_opacity_draws_nothing(app):
    assert watermark.watermark_pixmap("traktor.png", QSize(800, 600), 0) is None


def test_no_logo_named_draws_nothing(app):
    assert watermark.watermark_pixmap("", QSize(800, 600), 12) is None


def test_a_missing_file_draws_nothing_rather_than_raising(app):
    assert watermark.watermark_pixmap("not-a-file.png", QSize(800, 600), 12) is None


def test_a_pane_too_small_draws_nothing(app):
    """A collapsed pane or a mid-drag resize: scaling art into it is wasted."""
    assert watermark.watermark_pixmap("traktor.png", QSize(10, 600), 12) is None


def test_opacity_is_clamped_rather_than_trusted(app):
    """Session state is hand-editable JSON, so 500 has to mean something."""
    assert watermark.watermark_pixmap("traktor.png", QSize(400, 400), 500) is not None
    assert watermark.watermark_pixmap("traktor.png", QSize(400, 400), -20) is None


def test_the_same_request_is_cached(app):
    first = watermark.watermark_pixmap("traktor.png", QSize(400, 400), 12)
    second = watermark.watermark_pixmap("traktor.png", QSize(400, 400), 12)

    assert first is second


def test_a_different_opacity_is_a_different_pixmap(app):
    """Opacity is baked into the alpha, so it has to be part of the key or the
    slider would appear to do nothing."""
    first = watermark.watermark_pixmap("traktor.png", QSize(400, 400), 12)
    second = watermark.watermark_pixmap("traktor.png", QSize(400, 400), 60)

    assert first is not second


def test_a_missing_file_is_cached_too(app, monkeypatch):
    """Otherwise every repaint -- and there is one per scroll -- stats the disk."""
    watermark.watermark_pixmap("not-a-file.png", QSize(400, 400), 12)
    calls = []
    real = os.path.exists
    monkeypatch.setattr(os.path, "exists",
                        lambda p: calls.append(p) or real(p))

    watermark.watermark_pixmap("not-a-file.png", QSize(400, 400), 12)

    assert calls == []


def test_the_fade_is_applied(app):
    """A pixmap at 12% must not come back fully opaque -- that would be the
    logo painted flat over the tracks."""
    faded = watermark.watermark_pixmap("traktor.png", QSize(400, 400), 12)
    image = faded.toImage()

    alphas = [image.pixelColor(x, y).alpha()
              for y in range(0, image.height(), 8)
              for x in range(0, image.width(), 8)]
    assert max(alphas) < 200, max(alphas)


def test_drawing_reports_when_there_was_nothing_to_draw(app):
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QPainter, QPixmap
    canvas = QPixmap(50, 50)
    painter = QPainter(canvas)

    assert watermark.draw_watermark(painter, QRect(0, 0, 50, 50), None) is False
    painter.end()


# ============================== the table ===================================
def test_the_table_follows_its_source(app):
    from trackcleaner.ui.track_table import TrackTable
    table = TrackTable()
    table.resize(600, 400)

    table.set_context("traktor", [])

    assert table.current_watermark() is not None


def test_switching_source_switches_the_logo(app):
    """set_context is the single place this is decided, so the six callers that
    set a source cannot forget to update it."""
    from trackcleaner.ui.track_table import TrackTable
    table = TrackTable()
    table.resize(600, 400)

    table.set_context("traktor", [])
    first = table.current_watermark()
    table.set_context("vdj", [])

    assert table.current_watermark() is not first


def test_a_sourceless_table_draws_nothing(app):
    """A brand-new empty tab has no program yet."""
    from trackcleaner.ui.track_table import TrackTable
    table = TrackTable()
    table.resize(600, 400)

    assert table.current_watermark() is None


def test_opacity_zero_turns_the_table_watermark_off(app):
    from trackcleaner.ui.track_table import TrackTable
    table = TrackTable()
    table.resize(600, 400)
    table.set_context("traktor", [])

    table.set_watermark_opacity(0)

    assert table.current_watermark() is None


def test_painting_with_a_watermark_does_not_raise(app):
    """paintEvent draws over the rows with a second QPainter; an unbalanced
    begin/end there is a hard Qt warning, not a Python error, so exercise it."""
    from PySide6.QtGui import QPixmap
    from trackcleaner.ui.track_table import TrackTable
    table = TrackTable()
    table.resize(600, 400)
    table.set_context("traktor", [])
    table.show()

    table.viewport().render(QPixmap(table.viewport().size()))


# ============================== the sidebar =================================
def test_a_program_section_shows_its_logo(app):
    from trackcleaner.ui.playlist_trees import Section
    section = Section("Traktor Playlists", QWidget(), logo_file="traktor.png")

    assert not section.logo.pixmap().isNull()
    assert not section.logo.isHidden()


def test_the_logo_tracks_the_label_height(app):
    """Sized to the text, so Alt-scroll zoom does not leave a giant icon beside
    a small title."""
    from trackcleaner.ui.playlist_trees import Section
    section = Section("Traktor Playlists", QWidget(), logo_file="traktor.png")

    assert section.logo.pixmap().height() <= section.label.sizeHint().height()


def test_the_hidden_section_has_no_logo(app):
    """It is not a program, and giving it one would imply it were."""
    from trackcleaner.ui.playlist_trees import Section
    section = Section("Hidden", QWidget())

    assert section.logo.pixmap().isNull()
    assert section.logo.isHidden()


def test_a_missing_section_logo_is_not_fatal(app):
    from trackcleaner.ui.playlist_trees import Section
    section = Section("Nope", QWidget(), logo_file="not-a-file.png")

    assert section.logo.pixmap().isNull()


# ============================== the setting =================================
def _make_window(tmp_path, monkeypatch, ui_state=None):
    from trackcleaner.config.paths import Settings
    from trackcleaner.io import backup as backup_mod
    from fixtures import write_sample
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(tmp_path / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(tmp_path / "settings.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(tmp_path / "baseline.csv"))
    monkeypatch.setattr(paths_mod, "app_dir", lambda: str(tmp_path))
    from trackcleaner.ui.main_window import MainWindow
    settings = Settings()
    settings.paths = write_sample(tmp_path)
    settings.ui_state = ui_state or {}
    return MainWindow(settings)


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    w.show()
    yield w
    w.close()


def test_the_default_is_faint(window):
    """"Very faintly" was the requirement; a default anywhere near opaque would
    put a logo on top of every track."""
    assert 0 < window.watermark_opacity() <= 25


def test_the_setting_reaches_every_pane(window):
    window.set_watermark_opacity(40)

    assert all(t.watermark_opacity() == 40 for t in window.track_tabs.all_tables())


def test_it_reaches_panes_in_other_tabs(window):
    """It is a preference, not a property of whichever tab is showing."""
    window.track_tabs.new_tab()
    window.set_watermark_opacity(33)

    tables = window.track_tabs.all_tables()
    assert len(tables) > 1
    assert all(t.watermark_opacity() == 33 for t in tables)


def test_a_new_tab_inherits_the_current_setting(window):
    window.set_watermark_opacity(45)

    window.track_tabs.new_tab()

    assert window.track_tabs.all_tables()[-1].watermark_opacity() == 45


def test_the_setting_is_captured_for_next_session(window):
    window.set_watermark_opacity(37)

    assert window._capture_state()["watermark_opacity"] == 37


def test_a_saved_setting_is_restored(app, tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch, {"watermark_opacity": 55})

    assert w.watermark_opacity() == 55
    w.close()


def test_a_saved_setting_reaches_the_restored_tables(app, tmp_path, monkeypatch):
    """Read during __init__ rather than in _restore_state, so a pane's first
    paint is already right instead of flashing the default."""
    w = _make_window(tmp_path, monkeypatch, {"watermark_opacity": 55})
    w.show()

    assert all(t.watermark_opacity() == 55 for t in w.track_tabs.all_tables())
    w.close()


def test_a_nonsense_saved_value_does_not_break_startup(app, tmp_path, monkeypatch):
    """ui_state is a hand-editable JSON blob; this runs inside __init__, where
    an exception means the window never opens at all."""
    w = _make_window(tmp_path, monkeypatch, {"watermark_opacity": "loud"})

    assert w.watermark_opacity() == watermark.DEFAULT_OPACITY
    w.close()


def test_an_out_of_range_saved_value_is_clamped(app, tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch, {"watermark_opacity": 900})

    assert w.watermark_opacity() == 100
    w.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
