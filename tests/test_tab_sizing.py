"""Tab widths follow their text, within bounds.

The width maths is pure so it can be checked without Qt's layout pass, which is
where a size-hint bug is otherwise almost impossible to pin down: the visible
symptom (a clipped name, a scroll arrow) is several layers from the cause.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                              # noqa: E402

from trackcleaner.ui import track_tabs                                  # noqa: E402
from trackcleaner.ui.track_tabs import (PLUS_TAB_W, TAB_MAX_W, TAB_MIN_W,  # noqa: E402
                                        _PLUS, _SourceTabBar,
                                        fitted_widths, natural_width)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# ============================== natural width ===============================
def test_a_longer_name_wants_a_wider_tab():
    assert natural_width(300, 12) > natural_width(100, 12)


def test_the_chrome_is_counted():
    """The bar paints its own padding and "×"; Qt's default hint knows about
    neither, which is what was clipping long names."""
    assert natural_width(100, 12) >= 100 + 12


def test_a_tiny_name_still_gets_a_usable_tab():
    assert natural_width(1, 12) == TAB_MIN_W


def test_a_huge_name_is_capped():
    """One long playlist name must not swallow the whole strip."""
    assert natural_width(5000, 12) == TAB_MAX_W


# ============================== fitting =====================================
def test_tabs_that_fit_are_left_alone():
    assert fitted_widths([120, 120], 400) == [120, 120]


def test_an_overfull_strip_shrinks_rather_than_scrolls():
    """Scroll arrows hide tabs outright; narrowing keeps every one reachable."""
    got = fitted_widths([200, 200, 200], 300)

    assert sum(got) <= 300
    assert len(got) == 3


def test_shrinking_stops_at_the_minimum():
    """Past this there is nothing left to give, and Qt's arrows are honest."""
    got = fitted_widths([200, 200, 200], 10)

    assert got == [TAB_MIN_W] * 3


def test_an_unlaid_out_bar_does_not_collapse_every_tab():
    """Width is 0 before the first layout; treating that as "no room" would
    return minimums and then never grow back."""
    assert fitted_widths([120, 120], 0) == [120, 120]


def test_no_tabs_is_not_a_division_by_zero():
    assert fitted_widths([], 300) == []


# ======================= the bar, as actually laid out ======================
# These ask tabRect (where the tab really ended up) rather than tabSizeHint
# (what it asked for). Qt's default `expanding` stretched every tab to fill the
# bar, which overrode the hint entirely -- identical widths whatever the text
# said -- and a hint-only test could not see it.
def test_a_wider_name_really_gets_a_wider_tab(app):
    bar = _SourceTabBar()
    bar.resize(900, 28)
    bar.addTab("x")
    bar.addTab("a considerably longer playlist name")
    bar.show()

    assert bar.tabRect(1).width() > bar.tabRect(0).width()


def test_a_roomy_bar_does_not_stretch_tabs_to_fill(app):
    """The symptom of `expanding`: leftover space shared out equally, so a
    one-word tab is as wide as a sentence."""
    bar = _SourceTabBar()
    bar.resize(900, 28)
    bar.addTab("House")
    bar.addTab("Peak Time Bangers 2024")
    bar.show()

    assert sum(bar.tabRect(i).width() for i in range(2)) < 900


def test_the_plus_tab_really_stays_narrow(app):
    bar = _SourceTabBar()
    bar.resize(900, 28)
    bar.addTab("House")
    bar.addTab("+")
    bar.setTabData(1, _PLUS)
    bar.show()

    assert bar.tabRect(1).width() == PLUS_TAB_W


def test_crowding_the_bar_really_narrows_the_tabs(app):
    bar = _SourceTabBar()
    bar.resize(900, 28)
    for _ in range(3):
        bar.addTab("a reasonably long playlist name")
    bar.show()
    roomy = bar.tabRect(0).width()

    bar.resize(300, 28)

    assert bar.tabRect(0).width() < roomy


# ============================== the bar =====================================
def test_a_wider_name_gets_a_wider_tab(app):
    bar = _SourceTabBar()
    bar.resize(1000, 26)
    bar.addTab("x")
    bar.addTab("a considerably longer playlist name")

    assert bar.tabSizeHint(1).width() > bar.tabSizeHint(0).width()


def test_the_plus_tab_stays_narrow(app):
    """It holds one glyph; sizing it to text would give it a full-width tab."""
    bar = _SourceTabBar()
    bar.resize(1000, 26)
    bar.addTab("+")
    bar.setTabData(0, _PLUS)

    assert bar.tabSizeHint(0).width() == PLUS_TAB_W


def test_the_plus_tab_is_not_shrunk_with_the_others(app):
    bar = _SourceTabBar()
    bar.resize(120, 26)
    for name in ("a very long playlist name here", "another long one"):
        bar.addTab(name)
    bar.addTab("+")
    bar.setTabData(2, _PLUS)

    assert bar.tabSizeHint(2).width() == PLUS_TAB_W


def test_a_crowded_bar_narrows_its_tabs(app):
    bar = _SourceTabBar()
    bar.resize(1000, 26)
    for _ in range(3):
        bar.addTab("a reasonably long playlist name")
    roomy = bar.tabSizeHint(0).width()

    bar.resize(200, 26)

    assert bar.tabSizeHint(0).width() < roomy


def test_no_tab_is_narrower_than_the_minimum(app):
    bar = _SourceTabBar()
    bar.resize(40, 26)
    for _ in range(6):
        bar.addTab("a reasonably long playlist name")

    assert all(bar.tabSizeHint(i).width() >= TAB_MIN_W for i in range(bar.count()))


def test_an_out_of_range_index_answers_rather_than_raising(app):
    """Qt asks about indices during teardown; raising there is a crash with no
    usable traceback. Its own answer for an unknown tab is an invalid QSize, so
    the override passes that through rather than inventing a width."""
    bar = _SourceTabBar()
    bar.addTab("x")

    assert not bar.tabSizeHint(99).isValid()
    assert not bar.tabSizeHint(-1).isValid()


def test_the_minimum_hint_never_exceeds_the_size_hint(app):
    """Qt uses both; a minimum above the hint makes layout undefined."""
    bar = _SourceTabBar()
    bar.resize(1000, 26)
    bar.addTab("short")
    bar.addTab("+")
    bar.setTabData(1, _PLUS)

    for i in range(bar.count()):
        assert bar.minimumTabSizeHint(i).width() <= bar.tabSizeHint(i).width()


def test_painting_a_crowded_bar_does_not_raise(app):
    """The paint path elides against the tab rect; a zero/negative width there
    is the case that would throw."""
    from PySide6.QtGui import QPixmap
    bar = _SourceTabBar()
    bar.resize(60, 26)
    for _ in range(5):
        bar.addTab("a reasonably long playlist name")
    bar.show()

    bar.render(QPixmap(bar.size()))


def test_the_bounds_are_ordered():
    """A guard on the constants themselves: min above max would make every
    clamp return the wrong end."""
    assert PLUS_TAB_W < TAB_MIN_W < TAB_MAX_W
    assert track_tabs.TAB_PAD_LEFT > 0 and track_tabs.TAB_PAD_RIGHT > 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
