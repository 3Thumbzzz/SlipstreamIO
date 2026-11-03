"""Status squares with more than two programs on.

A square answers "is this in sync?", which needs a second thing to be in sync
*with*. With two programs that was implicit. With three it stops naming anything,
so one program is nominated as the **reference** and every other program's
squares compare against it.

Two properties matter most here and are pinned hardest:

* the two-program case is untouched -- both sides keep their squares;
* **every** loaded program is painted, the reference included. It shows its own
  glyph (a triangle when it changed, a fork when somebody else did) and never a
  square, because a square means "measured against the reference" and it cannot
  be behind itself.

See ``tests/test_status_glyphs.py`` for what the glyphs mean.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                            # noqa: E402

from trackcleaner.adapters import ADAPTER_IDS                         # noqa: E402
from trackcleaner.config.paths import Settings                        # noqa: E402
from trackcleaner.io import backup as backup_mod                      # noqa: E402
from trackcleaner.model import baseline as bl                         # noqa: E402
from trackcleaner.model import status as st                           # noqa: E402
from trackcleaner.model.library import TRAKTOR, VDJ                   # noqa: E402
from trackcleaner.native.slp import SLIPSTREAM                        # noqa: E402
from fixtures import write_sample                                     # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _make_window(tmp_path, monkeypatch, ui_state=None):
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(tmp_path / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(tmp_path / "settings.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(tmp_path / "baseline.csv"))
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


def _painted(window):
    """Which programs currently have a status painter."""
    trees = window.playlist_panel.trees
    by_tree = {tree: aid for aid, tree in trees.items()}
    return {by_tree[tree] for tree, _fn in window._tree_status_fns if tree in by_tree}


# ============================== the default =================================
def test_the_reference_defaults_to_traktor(window):
    """collection.nml is the file most users treat as their master."""
    assert window.reference_program() == TRAKTOR


def test_the_reference_falls_back_when_the_saved_one_is_off(window):
    """Switching off the reference must not leave the session comparing against
    a library that is not loaded -- that would silently drop every square."""
    window._set_reference_program(TRAKTOR)
    window._set_program_enabled(TRAKTOR, False)

    assert window.reference_program() != TRAKTOR
    assert window.reference_program() in window.libraries


def test_an_unknown_saved_reference_is_ignored(app, tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch,
                     ui_state={"reference_program": "serato"})
    try:
        assert w.reference_program() in ADAPTER_IDS
    finally:
        w.close()


# ========================== two programs: unchanged =========================
def test_with_two_programs_both_sides_still_get_squares(window):
    """The promise made when this was introduced: nothing changes for the
    two-program setup. With a single peer, "in sync with the other one" is still
    well defined for the reference too."""
    for aid in ADAPTER_IDS:
        if aid not in (VDJ, TRAKTOR):
            window._set_program_enabled(aid, False)

    assert _painted(window) == {VDJ, TRAKTOR}


# ======================== three programs: the reference =====================
def test_every_other_program_is_compared_against_the_reference(window):
    window._set_reference_program(TRAKTOR)

    painted = _painted(window)
    assert VDJ in painted
    assert SLIPSTREAM in painted


def test_the_reference_is_painted_too(window):
    """It used to be left unpainted once there were several peers. It now shows
    its own glyph -- a triangle when it changed, a fork when somebody else did --
    so it needs a painter like everyone else."""
    window._set_reference_program(TRAKTOR)

    assert TRAKTOR in _painted(window)


def test_every_loaded_program_is_painted_whichever_is_the_reference(window):
    for reference in (TRAKTOR, VDJ):
        window._set_reference_program(reference)

        assert _painted(window) == set(window._enabled), \
            f"with {reference} as the reference"


def test_the_reference_never_reports_itself_as_behind_the_reference(window):
    """A square means "measured against the reference". The reference cannot be
    behind itself, so its only yellows are the triangle and the fork."""
    window._set_reference_program(TRAKTOR)
    tree = window.playlist_panel.trees[TRAKTOR]
    painter = next(fn for t, fn in window._tree_status_fns if t is tree)

    for node in window.libraries[TRAKTOR].playlists():
        assert painter(node) != st.YELLOW_REF, node.name


def test_reports_exist_for_each_non_reference_program(window):
    window._set_reference_program(TRAKTOR)

    for aid in window._enabled:
        if aid != TRAKTOR:
            assert window.reports.get(aid) is not None, aid


# ============================ attribution wiring ============================
def test_the_engine_gets_owners_keyed_by_pair(window):
    """The engine looks attribution up per pair; handing it a flat map would
    make every pair miss, and a miss decides whether a sync overwrites."""
    window._set_reference_program(TRAKTOR)

    owners = window.sync_engine.owners
    assert isinstance(owners, dict)
    for key in owners:
        assert "|" in key, f"{key!r} is not a pair id"
    assert bl.pair_id(VDJ, TRAKTOR) in owners


def test_the_window_resolves_the_baseline_through_the_patched_module(window,
                                                                    tmp_path):
    """Test isolation, asserted rather than assumed.

    ``main_window`` used to do ``from ..config.paths import baseline_path``,
    which binds the function at import time -- so every test's monkeypatch of
    ``paths_mod.baseline_path`` missed, and running the suite read and rewrote
    the developer's REAL sync_baseline.csv. That file is the change-attribution
    history: corrupting it changes what a later sync overwrites or skips on real
    libraries. See "Development conventions" in HANDOFF.md.

    Asserted on the lookup rather than on a written file, because whether the
    diff dirties the baseline at all depends on the fixture's contents.
    """
    from trackcleaner.ui import main_window as mw

    window._recompute_status()

    assert mw.paths_mod.baseline_path() == str(tmp_path / "baseline.csv")
    assert mw.paths_mod.app_dir() != os.path.dirname(str(tmp_path)), \
        "app_dir is a real function, not accidentally patched away"


# ============================== persistence =================================
def test_the_reference_survives_a_restart(app, tmp_path, monkeypatch):
    w1 = _make_window(tmp_path, monkeypatch)
    w1._set_reference_program(VDJ)
    st = json.loads(json.dumps(w1._capture_state()))
    w1.close()
    assert st["reference_program"] == VDJ

    w2 = _make_window(tmp_path, monkeypatch, ui_state=st)
    try:
        assert w2.reference_program() == VDJ
        assert w2._reference_actions[VDJ].isChecked()
    finally:
        w2.close()


# =========================== one program: no diff ===========================
def test_one_program_on_clears_the_squares(window):
    for aid in list(window._enabled):
        if aid != VDJ:
            window._set_program_enabled(aid, False)

    assert not window._diffable()
    assert window.reports == {}
    assert window._tree_status_fns == ()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
