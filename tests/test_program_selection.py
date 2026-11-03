"""Choosing which programs a session uses.

The point of this feature is that a program you switch off is genuinely absent:
not read from disk, not in the playlist pane, and not required for the rest of
the app to work. These pin all three, because "it looks gone" and "it is gone"
are different things -- the old code would happily still read and diff it.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox                # noqa: E402

from trackcleaner.adapters import ADAPTER_IDS                          # noqa: E402
from trackcleaner.config.paths import Settings                         # noqa: E402
from trackcleaner.io import backup as backup_mod                       # noqa: E402
from trackcleaner.model.library import TRAKTOR, VDJ                    # noqa: E402
from fixtures import write_sample                                      # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _without(*off):
    """The programs still on after switching ``off`` off, in registration order."""
    return [a for a in ADAPTER_IDS if a not in off]


def _keep_only(window, keep):
    """Switch every program but one off. Written against ``ADAPTER_IDS`` rather
    than a literal pair so registering a new adapter doesn't rewrite these."""
    for aid in _without(keep):
        window._set_program_enabled(aid, False)


def _make_window(tmp_path, monkeypatch, ui_state=None):
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(tmp_path / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(tmp_path / "settings.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(tmp_path / "baseline.json"))
    from trackcleaner.ui.main_window import MainWindow
    settings = Settings()
    settings.paths = write_sample(tmp_path)
    settings.ui_state = ui_state or {}
    w = MainWindow(settings)
    w.show()
    return w


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    yield w
    w.close()


# ============================== the default =================================
def test_every_known_program_is_on_by_default(window):
    assert sorted(window._enabled) == sorted(ADAPTER_IDS)
    assert sorted(window.libraries) == sorted(ADAPTER_IDS)
    for aid in ADAPTER_IDS:
        assert window.playlist_panel.sections[aid].isVisible()


# ============================== switching off ===============================
def test_a_disabled_program_leaves_the_pane(window):
    window._set_program_enabled(TRAKTOR, False)

    assert window._enabled == _without(TRAKTOR)
    assert not window.playlist_panel.sections[TRAKTOR].isVisible()
    assert window.playlist_panel.sections[VDJ].isVisible()


def test_a_disabled_program_is_not_read_at_all(window):
    """Not merely hidden -- switching one off is also what makes a launch
    faster, so it must not be parsed."""
    window._set_program_enabled(TRAKTOR, False)

    assert TRAKTOR not in window.libraries
    assert window.libraries.get(TRAKTOR) is None
    assert VDJ in window.libraries


def test_switching_back_on_reloads_it(window):
    window._set_program_enabled(TRAKTOR, False)
    assert TRAKTOR not in window.libraries

    window._set_program_enabled(TRAKTOR, True)

    assert TRAKTOR in window.libraries
    assert window.playlist_panel.sections[TRAKTOR].isVisible()
    assert any(n.name == "test" for n in window.libraries[TRAKTOR].playlists())


def test_the_last_program_cannot_be_switched_off(window):
    """There would be no pane left, and no way to turn anything back on."""
    _keep_only(window, VDJ)

    window._set_program_enabled(VDJ, False)

    assert window._enabled == [VDJ], "the last one stays on"
    assert window._program_actions[VDJ].isChecked(), "and the tick is restored"


def test_toggling_keeps_registration_order(window):
    """Otherwise the pane reshuffles under you on a toggle."""
    window._set_program_enabled(VDJ, False)
    window._set_program_enabled(VDJ, True)

    assert window._enabled == [a for a in ADAPTER_IDS if a in window._enabled]


# ======================= status with one program on =========================
def test_status_is_cleared_when_only_one_program_is_on(window):
    """A diff compares one program against another. With one on there is
    nothing to be out of sync with, so stale colours must not linger."""
    window._on_playlists_selected(
        [next(n for n in window.libraries[TRAKTOR].playlists())], TRAKTOR)
    assert window._diffable()

    _keep_only(window, VDJ)

    assert not window._diffable()
    assert window.reports == {}
    assert window._tree_status_fns == ()


def test_sync_refuses_with_one_program_and_says_why(window, monkeypatch):
    seen = {}

    def fake_info(_parent, title, text, *a, **k):
        seen["title"], seen["text"] = title, text
        return QMessageBox.Ok

    monkeypatch.setattr(QMessageBox, "information", fake_info)
    _keep_only(window, VDJ)

    window._sync_selected(VDJ, "new")

    assert seen.get("title") == "Sync"
    assert "Programs menu" in seen["text"], "must point at the way to fix it"


# ============================== persistence =================================
def test_the_choice_survives_a_restart(app, tmp_path, monkeypatch):
    w1 = _make_window(tmp_path, monkeypatch)
    w1._set_program_enabled(TRAKTOR, False)
    st = json.loads(json.dumps(w1._capture_state()))
    w1.close()
    assert st["enabled_programs"] == _without(TRAKTOR)

    w2 = _make_window(tmp_path, monkeypatch, ui_state=st)
    try:
        assert w2._enabled == _without(TRAKTOR)
        assert TRAKTOR not in w2.libraries
        assert not w2.playlist_panel.sections[TRAKTOR].isVisible()
        assert not w2._program_actions[TRAKTOR].isChecked()
    finally:
        w2.close()


def test_an_unknown_saved_program_is_ignored(app, tmp_path, monkeypatch):
    """A session saved by a build that knew a program this one doesn't."""
    w = _make_window(tmp_path, monkeypatch,
                     ui_state={"enabled_programs": ["vdj", "djay"]})
    try:
        assert w._enabled == [VDJ]
    finally:
        w.close()


def test_an_empty_saved_selection_falls_back_to_all(app, tmp_path, monkeypatch):
    """Otherwise the window opens with no pane and no way back."""
    w = _make_window(tmp_path, monkeypatch, ui_state={"enabled_programs": []})
    try:
        assert sorted(w._enabled) == sorted(ADAPTER_IDS)
    finally:
        w.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
