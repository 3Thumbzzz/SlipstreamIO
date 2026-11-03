"""The Timing settings: how long a double-click and a double-tap have.

The one that matters is the **relationship**, not either number: the deferred
click-to-edit has to wait at least as long as Qt itself waits before calling
something a double-click. If it waited less, a double-click could land after the
editor had already opened — which is the bug the deferral exists to prevent, put
back by a setting.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                              # noqa: E402

from trackcleaner.config import controls as controls_mod                # noqa: E402
from trackcleaner.config.paths import Settings                          # noqa: E402
from trackcleaner.io import backup as backup_mod                        # noqa: E402
from fixtures import write_sample                                       # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _restore_qt_interval(app):
    """apply_timing sets an APPLICATION-wide value; leaking it would silently
    change every later test's double-click behaviour."""
    before = QApplication.doubleClickInterval()
    yield
    QApplication.setDoubleClickInterval(before)


# ============================== the ini file ================================
def test_missing_file_gives_the_timing_defaults(tmp_path):
    assert controls_mod.load_timing(str(tmp_path / "nope.ini")) \
        == controls_mod.TIMING_DEFAULTS


def test_timings_round_trip(tmp_path):
    path = str(tmp_path / "userControls.ini")

    controls_mod.save(controls_mod.defaults(), path,
                      timing={"double_click_ms": 350, "double_tap_ms": 275})

    got = controls_mod.load_timing(path)
    assert got == {"double_click_ms": 350, "double_tap_ms": 275}


def test_saving_without_timings_writes_the_defaults(tmp_path):
    """save(bindings, path) is still called positionally in the older tests, so
    the two-argument form has to keep working."""
    path = str(tmp_path / "userControls.ini")

    controls_mod.save(controls_mod.defaults(), path)

    assert controls_mod.load_timing(path) == controls_mod.TIMING_DEFAULTS


def test_the_shortcuts_survive_a_timing_save(tmp_path):
    """Both sections live in one file; writing one must not drop the other."""
    path = str(tmp_path / "userControls.ini")
    bindings = controls_mod.defaults()
    bindings["app.refresh"] = "F9"

    controls_mod.save(bindings, path, timing={"double_click_ms": 300})

    assert controls_mod.load(path)["app.refresh"] == "F9"


def test_zero_means_follow_the_operating_system(tmp_path):
    """The one value that must NOT be clamped up into range: Windows has its own
    double-click time and other apps honour it."""
    path = tmp_path / "userControls.ini"
    path.write_text("[timing]\ndouble_click_ms = 0\n", encoding="utf-8")

    assert controls_mod.load_timing(str(path))["double_click_ms"] == 0


def test_an_out_of_range_timing_is_clamped(tmp_path):
    path = tmp_path / "userControls.ini"
    path.write_text("[timing]\ndouble_click_ms = 99999\ndouble_tap_ms = 3\n",
                    encoding="utf-8")

    got = controls_mod.load_timing(str(path))

    assert got["double_click_ms"] == controls_mod.TIMING_MAX_MS
    assert got["double_tap_ms"] == controls_mod.TIMING_MIN_MS


def test_a_nonsense_timing_falls_back(tmp_path):
    """The file is meant to be hand-editable, so a typo cannot raise at startup."""
    path = tmp_path / "userControls.ini"
    path.write_text("[timing]\ndouble_tap_ms = quickly\n", encoding="utf-8")

    assert controls_mod.load_timing(str(path))["double_tap_ms"] == \
        controls_mod.TIMING_DEFAULTS["double_tap_ms"]


def test_an_unknown_timing_key_is_ignored(tmp_path):
    path = tmp_path / "userControls.ini"
    path.write_text("[timing]\ntriple_click_ms = 400\n", encoding="utf-8")

    assert "triple_click_ms" not in controls_mod.load_timing(str(path))


def test_an_unreadable_file_falls_back_rather_than_raising(tmp_path):
    path = tmp_path / "userControls.ini"
    path.write_text("not ini at all ][", encoding="utf-8")

    assert controls_mod.load_timing(str(path)) == controls_mod.TIMING_DEFAULTS


def test_only_known_keys_are_written(tmp_path):
    path = str(tmp_path / "userControls.ini")

    controls_mod.save(controls_mod.defaults(), path,
                      timing={"stray_ms": 123, "double_tap_ms": 250})

    assert "stray_ms" not in open(path, encoding="utf-8").read()


# ============================== the dialog ==================================
def test_the_dialog_reports_both_timings(app):
    from trackcleaner.ui.controls_dialog import ControlsDialog
    d = ControlsDialog(controls_mod.defaults(),
                       {"double_click_ms": 320, "double_tap_ms": 260})

    assert d.timing() == {"double_click_ms": 320, "double_tap_ms": 260}


def test_a_zero_double_click_opens_on_the_system_value(app):
    """A slider cannot render "follow the OS", so it shows what that resolves to
    right now; moving it is what pins a value."""
    from trackcleaner.ui.controls_dialog import ControlsDialog
    QApplication.setDoubleClickInterval(450)
    d = ControlsDialog(controls_mod.defaults(), {"double_click_ms": 0})

    assert d.timing()["double_click_ms"] == 450


def test_resetting_restores_the_timing_defaults(app):
    from trackcleaner.ui.controls_dialog import ControlsDialog
    d = ControlsDialog(controls_mod.defaults(),
                       {"double_click_ms": 900, "double_tap_ms": 900})

    d._reset()

    assert d.timing()["double_tap_ms"] == \
        controls_mod.TIMING_DEFAULTS["double_tap_ms"]


def test_the_dialog_still_works_with_no_timings_given(app):
    from trackcleaner.ui.controls_dialog import ControlsDialog
    d = ControlsDialog(controls_mod.defaults())

    assert set(d.timing()) == set(controls_mod.TIMING_DEFAULTS)


def test_the_sliders_stay_within_the_offered_bounds(app):
    from trackcleaner.ui.controls_dialog import ControlsDialog
    d = ControlsDialog(controls_mod.defaults())

    for slider in (d._double_click.slider, d._double_tap.slider):
        assert slider.minimum() == controls_mod.TIMING_MIN_MS
        assert slider.maximum() == controls_mod.TIMING_MAX_MS


# ============================== the window ==================================
def _make_window(tmp_path, monkeypatch):
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(tmp_path / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(tmp_path / "settings.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(tmp_path / "baseline.csv"))
    monkeypatch.setattr(paths_mod, "app_dir", lambda: str(tmp_path))
    from trackcleaner.ui.main_window import MainWindow
    settings = Settings()
    settings.paths = write_sample(tmp_path)
    w = MainWindow(settings)
    w.show()
    return w


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    yield w
    w.close()


def test_applying_a_double_click_sets_qts_own_interval(window):
    """Not just our tables: Qt decides what counts as a double-click, and the
    two must not be able to disagree."""
    window.apply_timing({"double_click_ms": 640, "double_tap_ms": 200})

    assert QApplication.doubleClickInterval() == 640


def test_the_panes_wait_exactly_that_long(window):
    window.apply_timing({"double_click_ms": 640, "double_tap_ms": 200})

    assert all(t.double_click_ms() == 640
               for t in window.track_tabs.all_tables())


def test_zero_leaves_qts_interval_alone(window):
    QApplication.setDoubleClickInterval(475)

    window.apply_timing({"double_click_ms": 0, "double_tap_ms": 200})

    assert QApplication.doubleClickInterval() == 475
    assert all(t.double_click_ms() == 475
               for t in window.track_tabs.all_tables())


def test_the_double_tap_window_lives_on_the_window(window):
    """One detector for the whole app, not one per pane: the arrows are routed
    application-wide, so several detectors would each see a different part of
    the same gesture."""
    window.apply_timing({"double_click_ms": 0, "double_tap_ms": 275})

    assert window.double_tap_ms() == 275


def test_the_two_gestures_are_independent(window):
    """Separate sliders, as asked: a slow mouse must not force a sluggish folder
    expand."""
    window.apply_timing({"double_click_ms": 900, "double_tap_ms": 150})

    assert window.track_tabs.all_tables()[0].double_click_ms() == 900
    assert window.double_tap_ms() == 150


def test_a_new_tab_inherits_the_click_timing(window):
    window.apply_timing({"double_click_ms": 700, "double_tap_ms": 300})

    window.track_tabs.new_tab()

    assert window.track_tabs.all_tables()[-1].double_click_ms() == 700


def test_the_double_tap_change_actually_changes_the_gesture(window):
    """Through the detector, not just the stored number -- the setting is only
    real if the timing rule uses it."""
    from PySide6.QtCore import Qt
    window.apply_timing({"double_click_ms": 0, "double_tap_ms": 500})

    window._taps.tap(Qt.Key_Right, now_ms=1000.0)

    assert window._taps.tap(Qt.Key_Right, now_ms=1400.0)   # 400ms: inside 500


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
