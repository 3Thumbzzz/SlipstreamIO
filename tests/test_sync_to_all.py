"""Syncing to every active program in one action.

Per-target syncing is correct but tedious: "push this playlist everywhere" cost
one trip through the menu per program, and syncing to one and forgetting the
other is exactly the drift the app exists to prevent.

The fan-out is a loop in the window, not a new mode in the engine --
``sync_playlist`` stays one-source-one-target. So what needs pinning here is the
window's behaviour around that loop: that it visits every program, that one
failing program does not abandon the rest, that the confirmations name all of
them, and that the report says which program got what.

The engine's actual syncing is covered by test_playlist_write.py and
test_sync_through_native.py; these deliberately stub it out to keep the subject
the fan-out rather than file IO.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox                 # noqa: E402

from trackcleaner.config.paths import Settings                          # noqa: E402
from trackcleaner.io import backup as backup_mod                        # noqa: E402
from trackcleaner.model.library import TRAKTOR, VDJ                     # noqa: E402
from trackcleaner.native.slp import SLIPSTREAM                          # noqa: E402
from trackcleaner.sync.engine import OVERWRITE, SyncResult              # noqa: E402
from trackcleaner.ui import context_menus                               # noqa: E402
from fixtures import write_sample                                       # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _CB:
    def __init__(self, **kw):
        self.__dict__.update(kw)


TWO = [(TRAKTOR, "Traktor"), (SLIPSTREAM, "SlipStream")]


# ============================ the track menu ================================
# The playlist menu's shape is covered in test_folders_and_moves.py; these pin
# that the track menu did not diverge from it.
def _track_menu(targets, **overrides):
    cb = dict(sync_track=lambda t: None, edit=lambda: None,
              show_playlists=lambda: None,
              check_integrity=lambda: None, reveal=lambda: None,
              remove_from_playlist=lambda: None,
              remove_with_entry=lambda: None, delete_file=lambda: None,
              sync_targets=targets, collection_label="database.xml")
    cb.update(overrides)
    return context_menus.track_menu(None, VDJ, _CB(**cb))


def _sync_track_action(menu):
    return next(a for a in menu.actions() if a.text().startswith("Sync Track"))


def test_the_track_menu_offers_the_same_fan_out(app):
    menu = _track_menu(TWO)

    entries = [a for a in _sync_track_action(menu).menu().actions()
               if not a.isSeparator()]

    assert [a.text() for a in entries] == \
        [context_menus.ALL_TARGETS_LABEL, "Traktor", "SlipStream"]


def test_the_track_fan_out_is_separated_from_the_targets(app):
    # Each menu is held in a local: letting one fall out of scope mid-chain has
    # shiboken collect the wrapper and the C++ object with it.
    menu = _track_menu(TWO)
    sub = _sync_track_action(menu).menu()

    actions = sub.actions()

    assert actions[0].text() == context_menus.ALL_TARGETS_LABEL
    assert actions[1].isSeparator()


def test_one_target_gets_no_track_fan_out(app):
    menu = _track_menu([(TRAKTOR, "Traktor")])

    act = _sync_track_action(menu)
    assert act.text() == "Sync Track → Traktor"
    assert act.menu() is None, "one target stays a single flat action"


def test_the_track_fan_out_passes_every_target_id(app):
    seen = []
    menu = _track_menu(TWO, sync_track=lambda t: seen.append(t))

    sub = _sync_track_action(menu).menu()
    all_entry = next(a for a in sub.actions()
                     if a.text() == context_menus.ALL_TARGETS_LABEL)
    all_entry.trigger()

    assert seen == [[TRAKTOR, SLIPSTREAM]]


# ============================== the window ==================================
def _make_window(tmp_path, monkeypatch):
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(tmp_path / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(tmp_path / "settings.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(tmp_path / "baseline.csv"))
    from trackcleaner.ui.main_window import MainWindow
    settings = Settings()
    settings.paths = write_sample(tmp_path)
    w = MainWindow(settings)
    w.show()
    return w


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    # The fan-out is the subject; the engine's writing is covered elsewhere.
    # Reloading is stubbed too, so on_done fires without a full re-read.
    monkeypatch.setattr(type(w), "_reload_preserving_view",
                        lambda self, on_done=None: on_done and on_done())
    yield w
    w.close()


@pytest.fixture
def calls(window, monkeypatch):
    """Records (node, source, mode, target) instead of writing anything."""
    seen = []

    def fake(node, source, mode, target=None):
        seen.append((node.name, source, mode, target))
        return SyncResult(playlists_created=1, playlist_entries_added=2)

    monkeypatch.setattr(window.sync_engine, "sync_playlist", fake)
    return seen


@pytest.fixture
def quiet(monkeypatch):
    """Auto-accept every dialog and capture what it said."""
    said = []

    def warn(_parent, title, text, *a, **k):
        said.append((title, text))
        return QMessageBox.Yes

    def info(_parent, title, text, *a, **k):
        said.append((title, text))
        return QMessageBox.Ok

    monkeypatch.setattr(QMessageBox, "warning", warn)
    monkeypatch.setattr(QMessageBox, "information", info)
    return said


def _a_playlist(window, source=TRAKTOR):
    return next(iter(window.libraries[source].playlists()))


# ------------------------------- the fan-out --------------------------------
def test_a_list_of_targets_syncs_to_every_one(window, calls, quiet):
    node = _a_playlist(window)

    window._sync_selected(TRAKTOR, "new", node, [VDJ, SLIPSTREAM])

    assert [t for _n, _s, _m, t in calls] == [VDJ, SLIPSTREAM]


def test_every_selected_playlist_reaches_every_target(window, calls, quiet,
                                                      monkeypatch):
    """The cross-product: N playlists x M programs, none skipped. Sidebar
    selection is stubbed because how the tree tracks a multi-selection is its
    own concern and covered elsewhere; the subject here is the fan-out."""
    nodes = list(window.libraries[TRAKTOR].playlists())[:2]
    assert len(nodes) == 2, "fixture needs two playlists for this to mean anything"
    tree = window.playlist_panel.tree_for(TRAKTOR)
    monkeypatch.setattr(tree, "selected_playlist_nodes", lambda: list(nodes))

    window._sync_selected(TRAKTOR, "new", nodes[0], [VDJ, SLIPSTREAM])

    for target in (VDJ, SLIPSTREAM):
        got = sorted(n for n, _s, _m, t in calls if t == target)
        assert got == sorted(n.name for n in nodes), f"{target} missed a playlist"


def test_a_single_target_still_works_unchanged(window, calls, quiet):
    node = _a_playlist(window)

    window._sync_selected(TRAKTOR, "new", node, VDJ)

    assert [t for _n, _s, _m, t in calls] == [VDJ]


def test_no_target_still_falls_back_to_the_only_other_program(window, calls,
                                                              quiet, monkeypatch):
    """Two-program sessions and the direct API both rely on this."""
    for aid in list(window._enabled):
        if aid not in (TRAKTOR, VDJ):
            window._set_program_enabled(aid, False)
    monkeypatch.setattr(window.sync_engine, "sync_playlist",
                        lambda node, source, mode, target=None:
                        calls.append((node.name, source, mode, target))
                        or SyncResult())

    window._sync_selected(TRAKTOR, "new", _a_playlist(window))

    assert [t for _n, _s, _m, t in calls] == [VDJ]


# --------------------------- failure isolation ------------------------------
def test_one_failing_program_does_not_abandon_the_others(window, quiet,
                                                         monkeypatch):
    """A fan-out that stops half way, silently, is worse than one that finishes
    and reports -- the user would have no idea which programs were written."""
    seen = []

    def flaky(node, source, mode, target=None):
        seen.append(target)
        if target == VDJ:
            raise OSError("database.xml is locked")
        return SyncResult()

    monkeypatch.setattr(window.sync_engine, "sync_playlist", flaky)

    window._sync_selected(TRAKTOR, "new", _a_playlist(window), [VDJ, SLIPSTREAM])

    assert seen == [VDJ, SLIPSTREAM], "the second program must still be synced"
    report = next(t for title, t in quiet if title == "Sync complete")
    assert "locked" in report, "and the failure must be reported, not swallowed"


# ------------------------------ what it says --------------------------------
def test_the_overwrite_confirmation_names_every_program(window, calls, quiet):
    """Overwrite-to-all is the most destructive action in the app; it must not
    say "the other program" when it means two of them."""
    window._sync_selected(TRAKTOR, OVERWRITE, _a_playlist(window),
                          [VDJ, SLIPSTREAM])

    title, text = next((t, x) for t, x in quiet if t == "Confirm overwrite")
    assert "VirtualDJ" in text and "SlipStream" in text


def test_declining_the_overwrite_syncs_nothing(window, calls, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **k: QMessageBox.No)

    window._sync_selected(TRAKTOR, OVERWRITE, _a_playlist(window),
                          [VDJ, SLIPSTREAM])

    assert calls == []


def test_the_report_has_a_block_per_program(window, calls, quiet):
    """One merged total would hide a program that silently did nothing."""
    window._sync_selected(TRAKTOR, "new", _a_playlist(window), [VDJ, SLIPSTREAM])

    report = next(t for title, t in quiet if title == "Sync complete")
    assert "VirtualDJ:" in report
    assert "SlipStream:" in report


# --------------------- the "program is running" warning ---------------------
def test_a_running_target_warns_once_naming_them(window, calls, quiet,
                                                 monkeypatch):
    """One prompt for the whole fan-out: three programs should not mean three
    dialogs to dismiss."""
    monkeypatch.setattr(type(window.adapters[VDJ]), "is_running",
                        lambda self: True)

    window._sync_selected(TRAKTOR, "new", _a_playlist(window), [VDJ, SLIPSTREAM])

    running = [(t, x) for t, x in quiet if "running" in t]
    assert len(running) == 1, "one warning, not one per program"
    assert "VirtualDJ" in running[0][0]


def test_declining_the_running_warning_syncs_nothing(window, calls, monkeypatch):
    monkeypatch.setattr(type(window.adapters[VDJ]), "is_running",
                        lambda self: True)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.No)

    window._sync_selected(TRAKTOR, "new", _a_playlist(window), [VDJ, SLIPSTREAM])

    assert calls == []


# ------------------------------ sync track ----------------------------------
def test_sync_track_fans_out_too(window, quiet, monkeypatch):
    seen = []
    # The plural is what the UI calls: a track sync writes the whole selection
    # in one run per target, so the singular is only the one-item wrapper.
    monkeypatch.setattr(window.sync_engine, "sync_tracks",
                        lambda keys, source, target=None:
                        seen.append(target) or SyncResult())
    key = next(iter(window.libraries[TRAKTOR].tracks))
    window._current_source = TRAKTOR

    window._sync_track(key, [VDJ, SLIPSTREAM])

    assert seen == [VDJ, SLIPSTREAM]


def test_sync_track_reports_which_programs_it_reached(window, quiet, monkeypatch):
    monkeypatch.setattr(window.sync_engine, "sync_tracks",
                        lambda keys, source, target=None: SyncResult())
    key = next(iter(window.libraries[TRAKTOR].tracks))
    window._current_source = TRAKTOR

    window._sync_track(key, [VDJ, SLIPSTREAM])

    text = next(t for title, t in quiet if title == "Sync Track")
    assert "VirtualDJ" in text and "SlipStream" in text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
