"""Assignable keyboard shortcuts and their userControls.ini.

Action **ids** are the contract: they are what ends up in the user's file, so
labels may be reworded freely and ids may not. The registry names what exists,
the window says what each id does, and the file records the user's choice —
these check the seams between those three.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                             # noqa: E402

from trackcleaner.config import controls as controls_mod               # noqa: E402
from trackcleaner.config.paths import Settings                         # noqa: E402
from trackcleaner.io import backup as backup_mod                       # noqa: E402
from fixtures import write_sample                                      # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# ============================== the registry ================================
def test_every_action_id_is_unique():
    """Ids key the ini file; a duplicate would silently overwrite a binding."""
    ids = [a.id for a in controls_mod.ACTIONS]
    assert len(ids) == len(set(ids))


def test_every_action_has_a_label_and_a_group():
    for action in controls_mod.ACTIONS:
        assert action.label, action.id
        assert action.group, action.id


def test_the_defaults_do_not_conflict_with_each_other():
    """Shipping a clash would mean one shortcut silently never fires."""
    assert controls_mod.conflicts(controls_mod.defaults()) == {}


def test_no_default_collides_with_a_bare_transport_key(app):
    """The transport filter owns the bare digits and "w" application-wide. It
    used to claim them regardless of modifiers, so Ctrl+W toggled playback and
    the shortcut bound to it silently never fired."""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from trackcleaner.ui.main_window import route_transport_key

    class _Panel:
        def __init__(self):
            self.played = 0
            self.cues = []

        def toggle_play(self):
            self.played += 1

        def cue_pressed(self, n):
            self.cues.append(n)

        def cue_released(self, n):
            pass

    panel = _Panel()
    for key in (Qt.Key_W, Qt.Key_1):
        event = QKeyEvent(QEvent.KeyPress, key, Qt.ControlModifier)
        assert route_transport_key(event, panel) is False, key
    assert panel.played == 0 and panel.cues == []

    # The bare key still works -- the guard must not disable the transport.
    assert route_transport_key(
        QKeyEvent(QEvent.KeyPress, Qt.Key_W, Qt.NoModifier), panel) is True
    assert panel.played == 1


def test_conflicts_finds_a_double_binding():
    clashes = controls_mod.conflicts({"a": "Ctrl+X", "b": "Ctrl+X", "c": "Ctrl+Y"})

    assert clashes == {"Ctrl+X": ["a", "b"]}


def test_unbound_actions_are_not_a_conflict():
    """Several actions with no key are not clashing with each other."""
    assert controls_mod.conflicts({"a": "", "b": "", "c": ""}) == {}


# ============================== the ini file ================================
def test_missing_file_gives_the_defaults(tmp_path):
    got = controls_mod.load(str(tmp_path / "nope.ini"))

    assert got == controls_mod.defaults()


def test_a_binding_round_trips(tmp_path):
    path = str(tmp_path / "userControls.ini")
    bindings = controls_mod.defaults()
    bindings["track.edit"] = "Ctrl+Shift+E"

    assert controls_mod.save(bindings, path)

    assert controls_mod.load(path)["track.edit"] == "Ctrl+Shift+E"


def test_an_unbound_action_round_trips_as_unbound(tmp_path):
    """Clearing a key must survive a restart, or unbinding would never stick."""
    path = str(tmp_path / "userControls.ini")
    bindings = controls_mod.defaults()
    bindings["app.refresh"] = ""

    controls_mod.save(bindings, path)

    assert controls_mod.load(path)["app.refresh"] == ""


def test_an_unknown_id_in_the_file_is_ignored(tmp_path):
    """A file written by a build that knew more actions must not break this one."""
    path = tmp_path / "userControls.ini"
    path.write_text("[shortcuts]\ntrack.edit = F4\nfuture.thing = Ctrl+Q\n",
                    encoding="utf-8")

    got = controls_mod.load(str(path))

    assert got["track.edit"] == "F4"
    assert "future.thing" not in got


def test_an_unreadable_file_falls_back_rather_than_raising(tmp_path):
    path = tmp_path / "userControls.ini"
    path.write_text("this is not ini at all ][", encoding="utf-8")

    assert controls_mod.load(str(path)) == controls_mod.defaults()


def test_only_known_ids_are_written(tmp_path):
    path = str(tmp_path / "userControls.ini")
    bindings = dict(controls_mod.defaults())
    bindings["stray.id"] = "Ctrl+Z"

    controls_mod.save(bindings, path)

    assert "stray.id" not in open(path, encoding="utf-8").read()


def test_the_path_is_resolved_through_the_module(tmp_path, monkeypatch):
    """Test isolation: a from-import of app_dir would bind the original and
    write the developer's real userControls.ini -- the rule under "Development
    conventions" in HANDOFF.md."""
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(paths_mod, "app_dir", lambda: str(tmp_path))

    assert controls_mod.controls_path() == str(tmp_path / "userControls.ini")


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


def test_every_bound_default_has_something_to_do(window):
    """A registry entry with a default key but no handler would look assigned
    and do nothing."""
    handlers = window._action_handlers()

    for action in controls_mod.ACTIONS:
        if action.default:
            assert action.id in handlers, f"{action.id} is bound but unhandled"


def test_every_handler_names_a_real_action(window):
    """The other direction: a handler for an id the registry does not list can
    never be bound, so it is dead code."""
    for action_id in window._action_handlers():
        assert action_id in controls_mod.ACTION_BY_ID, action_id


def test_shortcuts_are_created_for_bound_actions(window):
    window.apply_controls({"app.refresh": "F5", "tab.new": "Ctrl+T"})

    assert len(window._shortcuts) == 2


def test_an_unbound_action_gets_no_shortcut(window):
    window.apply_controls({"app.refresh": "", "tab.new": "Ctrl+T"})

    assert len(window._shortcuts) == 1


def test_reapplying_replaces_rather_than_accumulates(window):
    """Leaving the old ones attached would keep a chord the user just cleared
    working -- "it un-assigned but still fires"."""
    window.apply_controls({"app.refresh": "F5"})
    window.apply_controls({"app.refresh": "F6"})

    assert len(window._shortcuts) == 1
    assert window._shortcuts[0].key().toString() == "F6"


def test_clearing_every_binding_leaves_no_shortcuts(window):
    window.apply_controls({a.id: "" for a in controls_mod.ACTIONS})

    assert window._shortcuts == []


def test_an_id_with_no_handler_is_skipped_not_crashed(window):
    """The registry may name something before it is implemented."""
    window.apply_controls({"not.a.real.action": "Ctrl+Q"})

    assert window._shortcuts == []


def test_triggering_a_shortcut_runs_its_handler(window, monkeypatch):
    called = []
    monkeypatch.setattr(type(window), "refresh", lambda self: called.append(True))
    window.apply_controls({"app.refresh": "F5"})

    window._shortcuts[0].activated.emit()

    assert called == [True]


# ============================== the dialog ==================================
def test_the_dialog_reports_what_was_captured(app):
    from trackcleaner.ui.controls_dialog import ControlsDialog
    d = ControlsDialog({"app.refresh": "F5"})

    assert d.bindings()["app.refresh"] == "F5"


def test_clearing_in_the_dialog_unbinds(app):
    from trackcleaner.ui.controls_dialog import ControlsDialog
    d = ControlsDialog({"app.refresh": "F5"})

    d._edits["app.refresh"].clear()

    assert d.bindings()["app.refresh"] == ""


def test_the_dialog_warns_about_a_conflict(app):
    from trackcleaner.ui.controls_dialog import ControlsDialog
    d = ControlsDialog(controls_mod.defaults())

    d._edits["app.refresh"].set_binding("Ctrl+T")            # tab.new

    assert "Conflicting" in d._conflict_label.text()


def test_resetting_restores_the_defaults(app):
    from trackcleaner.ui.controls_dialog import ControlsDialog
    d = ControlsDialog(controls_mod.defaults())
    d._edits["app.refresh"].set_binding("Ctrl+Alt+R")

    d._reset()

    assert d.bindings()["app.refresh"] == \
        controls_mod.ACTION_BY_ID["app.refresh"].default


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
