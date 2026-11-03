"""Whole-state snapshots: nml + xml + the VDJ Folders tree, and revert."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.config.paths import Paths                       # noqa: E402
from trackcleaner.io import backup                                # noqa: E402


def _setup(tmp_path):
    """A live library layout: collection.nml, database.xml, and a Folders/ tree
    with a nested .vdjfolder."""
    live = tmp_path / "live"
    (live / "Folders" / "Breaks").mkdir(parents=True)
    nml = live / "collection.nml"
    nml.write_text("<NML>traktor</NML>", encoding="utf-8")
    xml = live / "database.xml"
    xml.write_text("<VirtualDJ_Database/>", encoding="utf-8")
    (live / "Folders" / "root.vdjfolder").write_text("<VirtualFolder/>", encoding="utf-8")
    (live / "Folders" / "Breaks" / "Exotic.vdjfolder").write_text(
        "<VirtualFolder>nested</VirtualFolder>", encoding="utf-8")
    paths = Paths(nml=str(nml), xml=str(xml))
    return live, paths


def test_snapshot_captures_all_three_targets(tmp_path):
    live, paths = _setup(tmp_path)
    root = str(tmp_path / "backups")
    dest = backup.snapshot(paths, stamp="2026_07_24_120000", root=root)
    assert dest is not None

    assert (tmp_path / "backups" / "2026_07_24_120000" / "collection.nml").read_text() \
        == "<NML>traktor</NML>"
    assert (tmp_path / "backups" / "2026_07_24_120000" / "database.xml").read_text() \
        == "<VirtualDJ_Database/>"
    # the whole Folders tree, including the nested .vdjfolder
    nested = (tmp_path / "backups" / "2026_07_24_120000" / "Folders"
              / "Breaks" / "Exotic.vdjfolder")
    assert nested.read_text() == "<VirtualFolder>nested</VirtualFolder>"

    # originals untouched
    assert (live / "collection.nml").exists()


def test_snapshot_leaves_no_stub_when_nothing_exists(tmp_path):
    paths = Paths(nml=str(tmp_path / "missing.nml"), xml=str(tmp_path / "missing.xml"))
    root = str(tmp_path / "backups")
    assert backup.snapshot(paths, stamp="2026_07_24_120000", root=root) is None
    # the timestamped dir must not be left behind as an empty stub
    assert not os.path.isdir(os.path.join(root, "2026_07_24_120000"))


def test_list_backups_is_newest_first(tmp_path):
    _live, paths = _setup(tmp_path)
    root = str(tmp_path / "backups")
    backup.snapshot(paths, stamp="2026_07_24_090000", root=root)
    backup.snapshot(paths, stamp="2026_07_24_120000", root=root)
    backup.snapshot(paths, stamp="2026_07_23_235959", root=root)
    stamps = [s for s, _ in backup.list_backups(root=root)]
    assert stamps == ["2026_07_24_120000", "2026_07_24_090000", "2026_07_23_235959"]


def test_two_snapshots_in_the_same_second_do_not_collide(tmp_path):
    """copytree fails if two snapshots share a directory -- the same-second case
    must get distinct dirs."""
    _live, paths = _setup(tmp_path)
    root = str(tmp_path / "backups")
    a = backup.snapshot(paths, stamp="2026_07_24_120000", root=root)
    b = backup.snapshot(paths, stamp="2026_07_24_120000", root=root)
    assert a is not None and b is not None and a != b
    # both are complete snapshots
    for d in (a, b):
        assert os.path.exists(os.path.join(d, "collection.nml"))
        assert os.path.isdir(os.path.join(d, "Folders"))


def test_snapshots_are_pruned_to_the_newest_n(tmp_path, monkeypatch):
    _live, paths = _setup(tmp_path)
    root = str(tmp_path / "backups")
    monkeypatch.setattr(backup, "_KEEP", 3)
    for i in range(6):
        backup.snapshot(paths, stamp=f"2026_07_24_00000{i}", root=root)
    kept = [s for s, _ in backup.list_backups(root=root)]
    assert kept == ["2026_07_24_000005", "2026_07_24_000004", "2026_07_24_000003"]


def test_revert_restores_all_three(tmp_path):
    live, paths = _setup(tmp_path)
    root = str(tmp_path / "backups")
    dest = backup.snapshot(paths, stamp="2026_07_24_120000", root=root)

    # mutate the live state: change the nml, and add + remove Folders files
    (live / "collection.nml").write_text("EDITED", encoding="utf-8")
    (live / "Folders" / "Breaks" / "Exotic.vdjfolder").unlink()
    (live / "Folders" / "Intruder.vdjfolder").write_text("junk", encoding="utf-8")

    backup.revert(dest, paths)

    assert (live / "collection.nml").read_text() == "<NML>traktor</NML>"
    # the deleted playlist file is back...
    assert (live / "Folders" / "Breaks" / "Exotic.vdjfolder").exists()
    # ...and the stray one is gone (Folders replaced wholesale)
    assert not (live / "Folders" / "Intruder.vdjfolder").exists()


def _with_extras(tmp_path):
    live, paths = _setup(tmp_path)
    (live / "library.slp").write_text("<SLP/>", encoding="utf-8")
    (live / "rekordbox.xml").write_text("<DJ_PLAYLISTS/>", encoding="utf-8")
    paths.slp = str(live / "library.slp")
    paths.rekordbox = str(live / "rekordbox.xml")
    return live, paths


def test_every_other_library_file_is_snapshotted(tmp_path):
    """The snapshot before a destructive change has to cover every program,
    not only the two that predate the adapter layer."""
    _live, paths = _with_extras(tmp_path)

    dest = backup.snapshot(paths, stamp="2026_07_24_120000",
                           root=str(tmp_path / "backups"))

    assert open(os.path.join(dest, "files", "slp", "library.slp")).read() == "<SLP/>"
    assert open(os.path.join(dest, "files", "rekordbox", "rekordbox.xml")).read() \
        == "<DJ_PLAYLISTS/>"


def test_the_cache_is_not_a_library_and_is_not_copied(tmp_path):
    _live, paths = _with_extras(tmp_path)
    csv = tmp_path / "live" / "start_time_report.csv"
    csv.write_text("start_time,bucket,path\n", encoding="utf-8")
    paths.csv = str(csv)

    dest = backup.snapshot(paths, stamp="2026_07_24_120000",
                           root=str(tmp_path / "backups"))

    assert not os.path.exists(os.path.join(dest, "files", "csv"))


def test_revert_restores_every_other_library_file(tmp_path):
    live, paths = _with_extras(tmp_path)
    dest = backup.snapshot(paths, stamp="2026_07_24_120000",
                           root=str(tmp_path / "backups"))
    (live / "rekordbox.xml").write_text("EDITED", encoding="utf-8")
    (live / "library.slp").write_text("EDITED", encoding="utf-8")

    backup.revert(dest, paths)

    assert (live / "rekordbox.xml").read_text() == "<DJ_PLAYLISTS/>"
    assert (live / "library.slp").read_text() == "<SLP/>"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
