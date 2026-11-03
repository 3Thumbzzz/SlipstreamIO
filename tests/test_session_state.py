"""Session restore: sidebar expansion/selection and the whole tab layout.

Tab contents are restored lazily -- the focused tab fills at startup, the rest
the first time they are shown -- so launch time does not grow with the number of
tabs left open on a big library.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                                          # noqa: E402
from PySide6.QtWidgets import QApplication                             # noqa: E402

from trackcleaner.config import state as state_mod                     # noqa: E402
from trackcleaner.config.paths import Settings                         # noqa: E402
from trackcleaner.io import backup as backup_mod                       # noqa: E402
from trackcleaner.model.library import TRAKTOR, VDJ                    # noqa: E402
from fixtures import write_sample                                      # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _make_window(tmp_path, monkeypatch, ui_state=None):
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(tmp_path / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(tmp_path / "settings.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(tmp_path / "baseline.json"))
    from trackcleaner.ui.main_window import MainWindow
    settings = Settings()
    settings.paths = write_sample(tmp_path)
    # Three programs unless a test says otherwise. The size tests drag height
    # between sidebar sections, and six sections at their 91 px minimum fill
    # the whole sidebar of a 760 px window -- there is no height to move.
    settings.ui_state = {"enabled_programs": ["vdj", "traktor", "slipstream"],
                         **(ui_state or {})}
    w = MainWindow(settings)
    w.show()
    return w


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    yield w
    w.close()


def _node(window, source, name):
    lib = window.libraries.get(VDJ) if source == VDJ else window.libraries.get(TRAKTOR)
    return next(n for n in lib.playlists() if n.name == name)


# ============================ the JSON helpers ==============================
def test_paths_round_trip():
    paths = {("a", "b"), ("c",)}
    raw = state_mod.paths_to_json(paths)
    assert raw == [["a", "b"], ["c"]]          # sorted, so saves don't churn
    assert json.loads(json.dumps(raw)) == raw
    assert state_mod.json_to_paths(raw) == paths


def test_paths_helpers_tolerate_empty():
    assert state_mod.paths_to_json(None) == []
    assert state_mod.json_to_paths(None) == set()


# ============================== tab layout ==================================
def test_capture_layout_of_the_default_single_tab(window):
    spec = window.track_tabs.capture_layout()

    assert spec["orientation"] == "h"
    assert len(spec["groups"]) == 1
    assert len(spec["groups"][0]["tabs"]) == 1
    assert json.loads(json.dumps(spec)) == spec, "must be JSON-safe"


def test_capture_layout_records_each_tabs_playlist(window):
    node = _node(window, TRAKTOR, "test")
    window._on_playlists_selected([node], TRAKTOR)

    spec = window.track_tabs.capture_layout()
    tab = spec["groups"][0]["tabs"][0]

    assert tab["source"] == TRAKTOR
    assert tab["node_paths"] == [["breaks", "test"]]
    assert tab["label"] == "test"


def test_layout_round_trips_through_json(window):
    window._on_playlists_selected([_node(window, TRAKTOR, "test")], TRAKTOR)
    window.track_tabs.new_tab()
    window._on_playlists_selected([_node(window, TRAKTOR, "_LOOPS")], TRAKTOR)
    spec = json.loads(json.dumps(window.track_tabs.capture_layout()))

    restored = window.track_tabs.restore_layout(spec)

    assert len(restored) == 2
    assert [s for _t, s, _p in restored] == [TRAKTOR, TRAKTOR]
    assert [p for _t, _s, p in restored] == [[("breaks", "test")], [("_LOOPS",)]]
    again = window.track_tabs.capture_layout()
    assert len(again["groups"][0]["tabs"]) == 2
    assert again["groups"][0]["current"] == spec["groups"][0]["current"]


@pytest.mark.parametrize("orientation,flag", [(Qt.Horizontal, "h"), (Qt.Vertical, "v")])
def test_split_layout_round_trips(window, orientation, flag):
    window._on_playlists_selected([_node(window, TRAKTOR, "test")], TRAKTOR)
    window.track_tabs.new_tab()
    group = window.track_tabs._groups()[0]
    window.track_tabs.split_from(group, orientation)
    assert window.track_tabs.is_split()
    spec = json.loads(json.dumps(window.track_tabs.capture_layout()))
    assert spec["orientation"] == flag

    window.track_tabs.restore_layout(spec)

    assert window.track_tabs.is_split()
    assert len(window.track_tabs._groups()) == 2
    assert window.track_tabs.capture_layout()["orientation"] == flag


def test_restore_layout_keeps_the_focused_tab(window):
    window._on_playlists_selected([_node(window, TRAKTOR, "test")], TRAKTOR)
    window.track_tabs.new_tab()
    window._on_playlists_selected([_node(window, TRAKTOR, "_LOOPS")], TRAKTOR)
    spec = window.track_tabs.capture_layout()
    assert spec["focused"] == [0, 1]

    window.track_tabs.restore_layout(spec)

    assert window.track_tabs.capture_layout()["focused"] == [0, 1]


def test_restore_layout_ignores_an_empty_or_broken_spec(window):
    before = len(window.track_tabs.all_tables())
    assert window.track_tabs.restore_layout({}) == []
    assert window.track_tabs.restore_layout({"groups": []}) == []
    assert window.track_tabs.restore_layout({"groups": [{"tabs": []}]}) == []
    assert len(window.track_tabs.all_tables()) == before, "never left with no tabs"


# ============================= capture / restore ============================
def test_capture_state_is_json_safe_and_complete(window):
    window._on_playlists_selected([_node(window, TRAKTOR, "test")], TRAKTOR)
    st = window._capture_state()

    assert "tree_view" in st and "tabs" in st
    assert json.loads(json.dumps(st)) == st, "a set or tuple would break the save"


def test_expanded_and_selected_folders_survive_a_restart(app, tmp_path, monkeypatch):
    w1 = _make_window(tmp_path, monkeypatch)
    tree = w1.playlist_panel.tree_for(TRAKTOR)
    tree.select_node(_node(w1, TRAKTOR, "test"))     # expands "breaks"
    assert ("breaks",) in tree.expanded_paths()
    st = w1._capture_state()
    w1.close()

    w2 = _make_window(tmp_path, monkeypatch, ui_state=json.loads(json.dumps(st)))
    tree2 = w2.playlist_panel.tree_for(TRAKTOR)
    try:
        assert ("breaks",) in tree2.expanded_paths()
        assert ("breaks", "test") in tree2.selected_paths()
    finally:
        w2.close()


def test_tabs_survive_a_restart(app, tmp_path, monkeypatch):
    w1 = _make_window(tmp_path, monkeypatch)
    w1._on_playlists_selected([_node(w1, TRAKTOR, "test")], TRAKTOR)
    w1.track_tabs.new_tab()
    w1._on_playlists_selected([_node(w1, TRAKTOR, "_LOOPS")], TRAKTOR)
    st = w1._capture_state()
    w1.close()

    w2 = _make_window(tmp_path, monkeypatch, ui_state=json.loads(json.dumps(st)))
    try:
        tables = w2.track_tabs.all_tables()
        assert len(tables) == 2
        spec = w2.track_tabs.capture_layout()
        assert spec["focused"] == [0, 1]
        focused = w2.track_tabs.focused_table()
        assert [tuple(n.node_path) for n in focused.nodes] == [("_LOOPS",)]
    finally:
        w2.close()


def test_only_the_focused_tab_is_filled_at_startup(app, tmp_path, monkeypatch):
    """The whole point of the lazy restore: launch cost doesn't grow with tabs."""
    w1 = _make_window(tmp_path, monkeypatch)
    w1._on_playlists_selected([_node(w1, TRAKTOR, "test")], TRAKTOR)
    w1.track_tabs.new_tab()
    w1._on_playlists_selected([_node(w1, TRAKTOR, "_LOOPS")], TRAKTOR)
    st = w1._capture_state()
    w1.close()

    w2 = _make_window(tmp_path, monkeypatch, ui_state=json.loads(json.dumps(st)))
    try:
        focused = w2.track_tabs.focused_table()
        others = [t for t in w2.track_tabs.all_tables() if t is not focused]

        assert focused.pending_restore is None, "the focused tab is already filled"
        assert all(t.pending_restore is not None for t in others)
        assert all(t.nodes == [] for t in others)

        # showing it fills it
        waiting = others[0]
        group = w2.track_tabs._group_of(waiting)
        group.setCurrentIndex(group.indexOf(waiting))

        assert waiting.pending_restore is None
        assert [tuple(n.node_path) for n in waiting.nodes] == [("breaks", "test")]
        assert waiting.model().rowCount() > 0
    finally:
        w2.close()


def test_a_never_shown_tab_keeps_its_playlist_across_another_restart(
        app, tmp_path, monkeypatch):
    """Quitting without ever clicking a restored tab must not forget what it
    held -- its nodes are still empty, so the pending restore is what to save."""
    w1 = _make_window(tmp_path, monkeypatch)
    w1._on_playlists_selected([_node(w1, TRAKTOR, "test")], TRAKTOR)
    w1.track_tabs.new_tab()
    w1._on_playlists_selected([_node(w1, TRAKTOR, "_LOOPS")], TRAKTOR)
    st = json.loads(json.dumps(w1._capture_state()))
    w1.close()

    w2 = _make_window(tmp_path, monkeypatch, ui_state=st)          # never clicked
    st2 = json.loads(json.dumps(w2._capture_state()))
    w2.close()

    paths = [t["node_paths"] for t in st2["tabs"]["groups"][0]["tabs"]]
    assert [["breaks", "test"]] in paths
    assert [["_LOOPS"]] in paths

    w3 = _make_window(tmp_path, monkeypatch, ui_state=st2)
    try:
        assert [tuple(n.node_path) for n in w3.track_tabs.focused_table().nodes] \
            == [("_LOOPS",)]
    finally:
        w3.close()


def test_a_tab_whose_playlist_vanished_comes_back_empty(app, tmp_path, monkeypatch):
    w1 = _make_window(tmp_path, monkeypatch)
    w1._on_playlists_selected([_node(w1, TRAKTOR, "test")], TRAKTOR)
    st = json.loads(json.dumps(w1._capture_state()))
    w1.close()
    # the playlist was renamed/deleted in Traktor since
    st["tabs"]["groups"][0]["tabs"][0]["node_paths"] = [["breaks", "long gone"]]

    w2 = _make_window(tmp_path, monkeypatch, ui_state=st)
    try:
        tables = w2.track_tabs.all_tables()
        assert len(tables) == 1, "the tab is kept, not dropped"
        assert tables[0].nodes == []
        assert tables[0].model().rowCount() == 0
    finally:
        w2.close()


def test_both_panels_of_a_split_layout_load_at_startup(app, tmp_path, monkeypatch):
    """A split shows two tabs at once; filling only the focused one left the
    other panel blank until it was clicked."""
    w1 = _make_window(tmp_path, monkeypatch)
    w1._on_playlists_selected([_node(w1, TRAKTOR, "test")], TRAKTOR)
    w1.track_tabs.new_tab()                       # a third, hidden tab
    w1._on_playlists_selected([_node(w1, TRAKTOR, "Preparation")], TRAKTOR)
    w1.track_tabs.new_tab()
    w1._on_playlists_selected([_node(w1, TRAKTOR, "_LOOPS")], TRAKTOR)
    w1.track_tabs.split_from(w1.track_tabs._groups()[0], Qt.Horizontal)
    assert w1.track_tabs.is_split()
    st = json.loads(json.dumps(w1._capture_state()))
    w1.close()

    w2 = _make_window(tmp_path, monkeypatch, ui_state=st)
    try:
        assert w2.track_tabs.is_split()
        visible = w2.track_tabs.visible_tables()
        assert len(visible) == 2
        for table in visible:
            assert table.pending_restore is None, "both on-screen panels filled"
            assert table.nodes, "and pointed at their playlist"
        hidden = [t for t in w2.track_tabs.all_tables() if t not in visible]
        assert hidden and all(t.pending_restore is not None for t in hidden), \
            "tabs behind them stay lazy"
    finally:
        w2.close()


# ========================= playlist panel section sizes =====================
# A QSplitter rescales any sizes to its real height and enforces each section's
# minimum, so the sizes only ever round-trip within the total the panel actually
# has. These drag height between sections instead of inventing absolute values.
def _resize_sections(panel, delta=60):
    """Move up to ``delta`` px from one section to the next, keeping the total,
    and return the sizes the splitter settled on.

    The transfer is clamped to what the donor can actually give up. Pushing a
    pane under its minimum doesn't just fail for that pane -- the splitter claws
    the shortfall back out of whichever pane still has slack, usually a collapsed
    one, so the whole layout stops round-tripping and the test measures Qt's
    recovery rather than our own restore.
    """
    sizes = panel.splitter.sizes()
    floor = panel.splitter.widget(1).minimumSizeHint().height()
    delta = min(delta, max(0, sizes[1] - floor))
    assert delta > 0, "no slack to redistribute -- pick a different pane"
    sizes[1] -= delta
    sizes[2] += delta
    panel.restore_sizes(sizes)
    return panel.splitter.sizes()


def test_section_sizes_survive_the_relayout_that_follows_them(window):
    """_restore_state applies sizes, then set_section_order fires a _relayout()
    that used to rebuild every pane from the constructor default weights."""
    panel = window.playlist_panel
    wanted = _resize_sections(panel)

    panel.set_section_order(panel.section_order())     # fires _relayout()

    assert panel.splitter.sizes() == wanted


def test_collapsing_a_section_also_preserves_the_others(window):
    panel = window.playlist_panel
    wanted = _resize_sections(panel)

    panel.set_collapsed_sections([])                   # fires _relayout()

    assert panel.splitter.sizes() == wanted


def test_section_sizes_come_back_after_a_restart(app, tmp_path, monkeypatch):
    w1 = _make_window(tmp_path, monkeypatch)
    wanted = _resize_sections(w1.playlist_panel)
    st = json.loads(json.dumps(w1._capture_state()))
    assert st["playlist_splitter"] == wanted
    w1.close()

    w2 = _make_window(tmp_path, monkeypatch, ui_state=st)
    try:
        assert w2.playlist_panel.splitter.sizes() == wanted
    finally:
        w2.close()


def test_the_defaults_no_longer_win_over_saved_sizes(app, tmp_path, monkeypatch):
    """The reported bug in its plainest form: whatever you left the sections at,
    they came back at the constructor's 120/320/320."""
    w1 = _make_window(tmp_path, monkeypatch)
    defaults = w1.playlist_panel.splitter.sizes()
    wanted = _resize_sections(w1.playlist_panel, delta=80)
    assert wanted != defaults
    st = json.loads(json.dumps(w1._capture_state()))
    w1.close()

    w2 = _make_window(tmp_path, monkeypatch, ui_state=st)
    try:
        assert w2.playlist_panel.splitter.sizes() != defaults
        assert w2.playlist_panel.splitter.sizes() == wanted
    finally:
        w2.close()


def test_a_collapsed_section_still_restores_the_others(app, tmp_path, monkeypatch):
    w1 = _make_window(tmp_path, monkeypatch)
    w1.playlist_panel.set_collapsed_sections(["hidden"])
    wanted = _resize_sections(w1.playlist_panel)
    st = json.loads(json.dumps(w1._capture_state()))
    w1.close()

    w2 = _make_window(tmp_path, monkeypatch, ui_state=st)
    try:
        assert w2.playlist_panel.collapsed_sections() == ["hidden"]
        assert w2.playlist_panel.splitter.sizes() == wanted
    finally:
        w2.close()


def test_a_session_with_no_saved_tabs_still_opens_one(app, tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch, ui_state={"font_pt": 11})
    try:
        assert len(w.track_tabs.all_tables()) == 1
        assert w.track_tabs.focused_table() is not None
    finally:
        w.close()


def test_a_fresh_layout_gives_each_program_room(app, tmp_path, monkeypatch):
    """Registering more programs must not squeeze the ones in use. The first
    layout pass runs before the window is shown, when Qt reports placeholder
    heights below a section's minimum; adopting those as the remembered
    heights left every section but the last clamped to its minimum."""
    w = _make_window(tmp_path, monkeypatch,
                     ui_state={"enabled_programs": ["vdj", "traktor"]})
    try:
        panel = w.playlist_panel
        sizes = dict(zip(panel.section_order(), panel.splitter.sizes()))
        floor = panel.sections["vdj"].minimumSizeHint().height()
        assert sizes["vdj"] > floor and sizes["traktor"] > floor
    finally:
        w.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
