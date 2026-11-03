"""Shape says who changed, colour says what.

Reported: with Traktor as the reference, dragging a track into a Traktor playlist
left another program's copy **green** while its rows already showed deep-green
"gaining" ghosts for tracks it was about to receive. The square claimed "in sync"
while the rows said "you are behind".

The rule that caused it returned GREEN whenever the *other* side made the change
-- coherent when yellow meant "I have something to push", wrong once one program
is the reference. So:

    green   ==  matches the reference, and nothing else
    square  ==  measured against the reference
    triangle==  this copy is the one that changed
    fork    ==  some other program changed

These cover the matrix -- who edited x who is looking -- at the model level, and
then end to end through the window, including the optimistic overlay that has to
be right *before* any refresh.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                            # noqa: E402

from trackcleaner.config.paths import Settings                        # noqa: E402
from trackcleaner.io import backup as backup_mod                      # noqa: E402
from trackcleaner.model import status as st                           # noqa: E402
from trackcleaner.model.library import (Library, Playlist, Track,     # noqa: E402
                                        TreeNode, TRAKTOR, VDJ, path_ci)
from trackcleaner.native.slp import SLIPSTREAM                        # noqa: E402
from trackcleaner.ui import status_squares                            # noqa: E402
from fixtures import write_sample                                     # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# ============================ the glyph rule ================================
def test_the_matrix_of_who_changed_versus_who_is_looking():
    ref = TRAKTOR
    cases = [
        # (viewer,     changed_by,             expected)
        (VDJ,          {VDJ},                  st.YELLOW_SELF),
        (VDJ,          {TRAKTOR},               st.YELLOW_REF),
        (VDJ,          {SLIPSTREAM},            st.YELLOW_OTHER),
        (VDJ,          {VDJ, TRAKTOR},          st.YELLOW_SELF),
        (TRAKTOR,      {TRAKTOR},               st.YELLOW_SELF),
        (TRAKTOR,      {VDJ},                   st.YELLOW_OTHER),
        (TRAKTOR,      {VDJ, SLIPSTREAM},       st.YELLOW_OTHER),
        (SLIPSTREAM,   {VDJ},                   st.YELLOW_OTHER),
        (SLIPSTREAM,   {TRAKTOR},               st.YELLOW_REF),
    ]
    for viewer, changed_by, expected in cases:
        assert st.changed_glyph(viewer, ref, changed_by) == expected, \
            f"{viewer} looking at a change by {sorted(changed_by)}"


def test_self_beats_reference_beats_other():
    """Precedence per side: your own edit is the most actionable thing to be
    told, then that you are behind the reference, then anyone else."""
    ref = TRAKTOR
    assert st.changed_glyph(VDJ, ref, {VDJ, TRAKTOR, SLIPSTREAM}) == st.YELLOW_SELF
    assert st.changed_glyph(VDJ, ref, {TRAKTOR, SLIPSTREAM}) == st.YELLOW_REF
    assert st.changed_glyph(VDJ, ref, {SLIPSTREAM}) == st.YELLOW_OTHER


def test_no_attribution_never_yields_green():
    """The bug in one line: absence of a verdict is not evidence of agreement."""
    for changed_by in (None, set(), frozenset()):
        assert st.changed_glyph(VDJ, TRAKTOR, changed_by) != st.GREEN
        assert st.is_changed(st.changed_glyph(VDJ, TRAKTOR, changed_by))


def test_merge_owner_folds_a_pairs_verdict_into_the_tally():
    """A pair only names its own two members, so the union over pairs is what
    lets a copy learn that a program it is not paired with moved."""
    tally = {}
    st.merge_owner(tally, "k", VDJ, VDJ, TRAKTOR)
    st.merge_owner(tally, "k", "both", SLIPSTREAM, TRAKTOR)

    assert tally["k"] == {VDJ, SLIPSTREAM, TRAKTOR}


def test_merge_owner_ignores_an_empty_verdict():
    tally = {}
    st.merge_owner(tally, "k", "", VDJ, TRAKTOR)
    assert tally == {}


# ============================== the glyphs ==================================
def _image(status, size=12):
    return status_squares.status_icon(status, size).pixmap(size, size).toImage()


def _opaque_pixels(status, size=12):
    img = _image(status, size)
    return sum(1 for y in range(size) for x in range(size)
               if img.pixelColor(x, y).alpha() > 0)


def test_each_status_maps_to_its_intended_shape():
    assert status_squares.shape_for(st.GREEN) == status_squares.SQUARE
    assert status_squares.shape_for(st.BLACK) == status_squares.SQUARE
    assert status_squares.shape_for(st.YELLOW_REF) == status_squares.SQUARE
    assert status_squares.shape_for(st.YELLOW_SELF) == status_squares.TRIANGLE
    assert status_squares.shape_for(st.YELLOW_OTHER) == status_squares.FORK
    assert status_squares.shape_for(st.RED) == status_squares.CROSS


def test_the_three_yellows_share_one_colour(app):
    """Only the shape differs -- they are all "changed"."""
    colours = {status_squares.color_for(s) for s in st.YELLOWS}
    assert len(colours) == 1


def test_the_tombstone_markers_still_render(app):
    """They are not in the shape table; unknown ids must fall back to a square in
    their own colour rather than raising."""
    for marker in status_squares.TOMBSTONE_STATUSES:
        assert status_squares.shape_for(marker) == status_squares.SQUARE
        assert status_squares.status_icon(marker) is not None


def test_every_status_produces_a_non_empty_icon(app):
    """A shape that draws nothing looks exactly like a missing icon, so an empty
    pixmap is checked for rather than just a non-null QIcon."""
    for status in (st.GREEN, st.BLACK, st.RED, *st.YELLOWS):
        assert _opaque_pixels(status) > 20, f"{status} drew (almost) nothing"


def test_the_three_yellows_are_visually_distinguishable(app):
    """They share a colour, so the shape is the *only* thing telling them
    apart -- if two rendered identically the distinction would be invisible."""
    images = {s: _image(s) for s in st.YELLOWS}
    for a, b in ((st.YELLOW_SELF, st.YELLOW_REF),
                 (st.YELLOW_SELF, st.YELLOW_OTHER),
                 (st.YELLOW_REF, st.YELLOW_OTHER)):
        assert images[a] != images[b], f"{a} and {b} render identically"


def test_the_legend_covers_every_status(app):
    labelled = {s for s, _name, _meaning in status_squares.PLAYLIST_LEGEND
                + status_squares.TRACK_LEGEND}
    for status in (st.GREEN, st.BLACK, st.RED, *st.YELLOWS):
        assert status in labelled, f"{status} has no legend entry"


# ========================= playlists, end to end ============================
def _pl(name, keys, source):
    return TreeNode(name=name, kind="playlist", source=source, node_path=(name,),
                    playlist=Playlist(name=name, source=source,
                                      track_keys=list(keys)))


def _lib(source, keys):
    node = _pl("PL", keys, source)
    return Library(source=source,
                   tracks={k: Track(key=k, path=k, source=source) for k in keys},
                   root=TreeNode("r", "folder", source, children=[node])), node


def test_a_playlist_behind_the_reference_is_a_yellow_square():
    """The reported case: the reference gained a track, so this copy is behind."""
    vlib, vnode = _lib(VDJ, ["a"])
    tlib, _ = _lib(TRAKTOR, ["a", "b"])
    report = st.diff_tracks(list(vlib.tracks), vlib, tlib, {}, reference=TRAKTOR)

    got = report.playlist_status(vnode, tlib.playlist_index(), {TRAKTOR}, TRAKTOR)

    assert got == st.YELLOW_REF
    assert status_squares.shape_for(got) == status_squares.SQUARE


def test_the_reference_shows_a_fork_when_another_program_changed():
    tlib, tnode = _lib(TRAKTOR, ["a"])
    vlib, _ = _lib(VDJ, ["a", "b"])
    report = st.diff_tracks_against(list(tlib.tracks), tlib, [vlib], {},
                                    reference=TRAKTOR)

    got = report.reference_playlist_status(
        tnode, [vlib.playlist_index()], {VDJ}, TRAKTOR)

    assert got == st.YELLOW_OTHER
    assert status_squares.shape_for(got) == status_squares.FORK


def test_the_reference_shows_a_triangle_when_it_changed_itself():
    tlib, tnode = _lib(TRAKTOR, ["a", "b"])
    vlib, _ = _lib(VDJ, ["a"])
    report = st.diff_tracks_against(list(tlib.tracks), tlib, [vlib], {},
                                    reference=TRAKTOR)

    got = report.reference_playlist_status(
        tnode, [vlib.playlist_index()], {TRAKTOR}, TRAKTOR)

    assert got == st.YELLOW_SELF


def test_the_reference_is_green_only_when_every_peer_agrees():
    tlib, tnode = _lib(TRAKTOR, ["a"])
    agree, _ = _lib(VDJ, ["a"])
    differs, _ = _lib(SLIPSTREAM, ["a", "b"])
    report = st.diff_tracks_against(list(tlib.tracks), tlib, [agree, differs], {},
                                    reference=TRAKTOR)

    assert report.reference_playlist_status(
        tnode, [agree.playlist_index()], None, TRAKTOR) == st.GREEN
    assert report.reference_playlist_status(
        tnode, [agree.playlist_index(), differs.playlist_index()],
        {SLIPSTREAM}, TRAKTOR) == st.YELLOW_OTHER


def test_a_playlist_no_peer_has_yet_is_black():
    tlib, tnode = _lib(TRAKTOR, ["a"])
    report = st.diff_tracks_against(list(tlib.tracks), tlib, [], {},
                                    reference=TRAKTOR)

    assert report.reference_playlist_status(tnode, [{}], None, TRAKTOR) == st.BLACK


# ===================== the window, before any refresh =======================
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
    w._set_reference_program(TRAKTOR)
    yield w
    w.close()


def _glyphs(window, path):
    """What each program's tree would paint for this playlist path, right now."""
    out = {}
    for source, tree in window.playlist_panel.trees.items():
        base = next((fn for t, fn in window._tree_status_fns if t is tree), None)
        overlay = window._edited_overlay(source, base or (lambda n: st.GREEN))
        node = TreeNode(name=path[-1], kind="playlist", source=source,
                        node_path=path,
                        playlist=Playlist(name=path[-1], source=source))
        out[source] = overlay(node)
    return out


def test_editing_the_reference_puts_every_other_copy_behind_it(window):
    """Drag into a Traktor playlist: Traktor changed it, everyone else is behind
    the reference -- and it shows immediately, with no refresh."""
    path = ("breaks", "test")
    window._edited_paths[path_ci(path)] = TRAKTOR

    glyphs = _glyphs(window, path)

    assert glyphs[TRAKTOR] == st.YELLOW_SELF
    assert glyphs[VDJ] == st.YELLOW_REF
    assert glyphs[SLIPSTREAM] == st.YELLOW_REF


def test_editing_a_non_reference_forks_everyone_else(window):
    """Drag into a VirtualDJ playlist: VDJ changed it; Traktor and SlipStream are
    not behind the reference, they have been diverged *from* by a third party."""
    path = ("breaks", "test")
    window._edited_paths[path_ci(path)] = VDJ

    glyphs = _glyphs(window, path)

    assert glyphs[VDJ] == st.YELLOW_SELF
    assert glyphs[TRAKTOR] == st.YELLOW_OTHER
    assert glyphs[SLIPSTREAM] == st.YELLOW_OTHER


def test_an_untouched_playlist_keeps_its_diff_glyph(window):
    """The overlay must only speak for paths that were actually edited; every
    other path still shows whatever the last diff decided."""
    path = ("something", "else")
    before = _glyphs(window, path)

    window._edited_paths[path_ci(("breaks", "test"))] = VDJ

    assert _glyphs(window, path) == before


def test_the_edit_marker_is_keyed_by_path_not_by_program(window):
    """Keyed by (program, path) it only lit the copy you edited, which is exactly
    why the others kept a stale colour until a refresh."""
    path = ("breaks", "test")
    window._edited_paths[path_ci(path)] = TRAKTOR

    assert set(window._edited_paths) == {path_ci(path)}
    assert window._edited_paths[path_ci(path)] == TRAKTOR


def test_the_reference_gets_a_painter_with_three_programs(window):
    """It used to be left unpainted entirely once there were several peers; it
    now needs its own glyph to show the fork."""
    window._recompute_status()

    painted = {aid for aid, tree in window.playlist_panel.trees.items()
               if any(t is tree for t, _fn in window._tree_status_fns)}
    assert TRAKTOR in painted


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
