"""Two playlists whose names differ only in case cannot both be written.

Matching playlists across programs is case-insensitive on purpose -- Traktor's
``breaks`` is VirtualDJ's ``Breaks`` and they should sync. But VirtualDJ stores
one *file* per playlist, so on Windows two siblings differing only in case
resolve to the same ``.vdjfolder``. The second write lands on the first one's
file.

That is not hypothetical. A reported folder Overwrite wrote ``Diggs/temp``
(511 tracks) at 02:20:26 and ``Diggs/Temp`` (42) replaced it at 02:20:44; the
file left on disk held 42. Both runs reported ``playlists_created=1``. The
status tree then diffed Traktor's ``temp`` against VirtualDJ's ``Temp``, because
``playlist_index`` resolves the tie with first-match-wins -- so a playlist that
no longer existed showed the rows and colours of a different one.

There is no correct choice between the two, so neither is written and both are
named. The tests here are the three properties that were missing: the clash is
found, nothing is destroyed, and the status stops claiming a match.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                              # noqa: E402

from trackcleaner.config.paths import Settings                          # noqa: E402
from trackcleaner.io import backup as backup_mod, vdj_read              # noqa: E402
from trackcleaner.model.library import (Library, Playlist, TRAKTOR,     # noqa: E402
                                        Track, TreeNode, VDJ, path_ci)
from trackcleaner.model.status import BLACK, GREEN, RED, diff_tracks    # noqa: E402
from trackcleaner.sync.engine import OVERWRITE                          # noqa: E402
from fixtures import write_sample                                       # noqa: E402


def _node(name, keys, source=TRAKTOR, folder=()):
    return TreeNode(name=name, kind="playlist", source=source,
                    node_path=tuple(folder) + (name,),
                    playlist=Playlist(name=name, source=source,
                                      track_keys=list(keys)))


def _lib(source, *nodes, root_name="root"):
    lib = Library(source=source,
                  tracks={k: Track(key=k, path=k, source=source)
                          for n in nodes for k in n.playlist.track_keys})
    lib.root = TreeNode(root_name, "folder", source, children=list(nodes))
    return lib


# ============================ the clash is found ===========================
def test_two_spellings_of_one_name_are_a_collision():
    lib = _lib(TRAKTOR, _node("temp", ["a"]), _node("Temp", ["b"]))

    clashes = lib.colliding_playlist_paths()

    assert list(clashes) == [("temp",)]
    assert sorted(n.name for n in clashes[("temp",)]) == ["Temp", "temp"], \
        "both spellings are named, so the report can say which to rename"


def test_the_same_name_in_different_folders_is_not_a_collision():
    """Only siblings clash: ``A/temp`` and ``B/temp`` are separate files."""
    lib = _lib(TRAKTOR,
               _node("temp", ["a"], folder=("A",)),
               _node("temp", ["b"], folder=("B",)))

    assert lib.colliding_playlist_paths() == {}


def test_one_playlist_is_never_a_collision():
    lib = _lib(TRAKTOR, _node("temp", ["a"]), _node("other", ["b"]))
    assert lib.colliding_playlist_paths() == {}


# ====================== the status stops claiming a match ==================
def test_the_twin_the_target_lacks_reads_black_not_the_other_one_s_colours():
    """The reported symptom: Traktor's ``temp`` showed VirtualDJ's ``Temp``.

    Both hold the same track here, so without the guard the index hands back
    whichever came first and ``temp`` reads GREEN -- in sync with a playlist it
    is not. Two spellings side by side is legal in Traktor and gets no mark of
    its own; the one VirtualDJ does not have is simply not there yet.
    """
    trk = _lib(TRAKTOR, _node("temp", ["a"]), _node("Temp", ["a"]))
    vdj = _lib(VDJ, _node("Temp", ["a"], source=VDJ))

    report = diff_tracks(list(trk.tracks), trk, vdj)
    idx = vdj.playlist_index()
    temp = next(n for n in trk.playlists() if n.name == "temp")
    twin = next(n for n in trk.playlists() if n.name == "Temp")
    clashes = trk.colliding_playlist_paths()

    assert report.playlist_status(temp, idx) == GREEN, "without the guard, a lie"
    assert report.playlist_status(temp, idx, colliding=clashes) == BLACK
    assert report.playlist_status(twin, idx, colliding=clashes) == GREEN, \
        "the one VirtualDJ really holds is unaffected"
    assert RED not in (report.playlist_status(n, idx, colliding=clashes)
                       for n in (temp, twin)), "no error mark for a legal name"


def test_a_plain_case_difference_still_matches():
    """The behaviour the fix must not break: one ``breaks`` on each side, spelled
    differently, is the same playlist and still syncs."""
    trk = _lib(TRAKTOR, _node("breaks", ["a"]))
    vdj = _lib(VDJ, _node("Breaks", ["a"], source=VDJ))

    report = diff_tracks(list(trk.tracks), trk, vdj)
    node = next(trk.playlists())
    assert trk.colliding_playlist_paths() == {}
    assert report.playlist_status(node, vdj.playlist_index(),
                                  colliding=trk.colliding_playlist_paths()) == GREEN


def test_a_playlist_the_target_lacks_is_still_black():
    trk = _lib(TRAKTOR, _node("only here", ["a"]))
    vdj = _lib(VDJ, _node("something else", ["a"], source=VDJ))
    report = diff_tracks(list(trk.tracks), trk, vdj)
    assert report.playlist_status(next(trk.playlists()),
                                  vdj.playlist_index()) == BLACK


# ========================= nothing is destroyed ============================
@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    from trackcleaner.config import paths as paths_mod
    from trackcleaner.ui.main_window import MainWindow
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(tmp_path / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(tmp_path / "s.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(tmp_path / "b.csv"))
    monkeypatch.setattr(paths_mod, "app_dir", lambda: str(tmp_path))
    settings = Settings()
    settings.paths = write_sample(tmp_path)
    w = MainWindow(settings)
    yield w


def _find(node, path):
    if tuple(node.node_path) == tuple(path):
        return node
    for child in node.children:
        found = _find(child, path)
        if found is not None:
            return found
    return None


def _twin(window, name):
    """Add a sibling playlist named ``name`` beside the sample's own, same
    tracks -- the shape the reported library was in."""
    lib = window.libraries[TRAKTOR]
    original = next(n for n in lib.playlists() if n.playlist.track_keys)
    parent = _find(lib.root, original.node_path[:-1]) or lib.root
    twin = TreeNode(name=name, kind="playlist", source=TRAKTOR,
                    node_path=tuple(parent.node_path) + (name,),
                    playlist=Playlist(name=name, source=TRAKTOR,
                                      track_keys=list(original.playlist.track_keys)))
    parent.children.append(twin)
    return original, twin


def test_a_colliding_pair_is_refused_before_anything_is_written(window):
    """Both twins, not just the loser. "The other one worked" is exactly the
    illusion that let this destroy 511 tracks without a word."""
    original, twin = _twin(window, "TEST")
    assert path_ci(original.node_path) == path_ci(twin.node_path)
    paths = window.settings.paths
    before = open(paths.xml, "rb").read()

    first = window.sync_engine.sync_playlist(original, TRAKTOR, OVERWRITE,
                                             target=VDJ)
    second = window.sync_engine.sync_playlist(twin, TRAKTOR, OVERWRITE,
                                              target=VDJ)

    for result in (first, second):
        assert result.errors, "refused, and said so"
        assert "capitalisation" in result.errors[0]
        assert result.playlists_created == 0
    assert open(paths.xml, "rb").read() == before, \
        "a refusal must not even reach the collection"
    assert original.name in first.errors[0] and twin.name in first.errors[0], \
        "both spellings named, so the user knows which to rename"


def test_renaming_one_lets_both_through(window):
    """The fix the message asks for actually works."""
    original, twin = _twin(window, "TEST")
    twin.name = "TEST second"
    twin.node_path = twin.node_path[:-1] + ("TEST second",)
    twin.playlist.name = "TEST second"

    assert not window.sync_engine.sync_playlist(
        original, TRAKTOR, OVERWRITE, target=VDJ).errors
    assert not window.sync_engine.sync_playlist(
        twin, TRAKTOR, OVERWRITE, target=VDJ).errors

    paths = window.settings.paths
    lib = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    names = {n.name for n in lib.playlists()}
    assert {original.name, "TEST second"} <= names, names


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
