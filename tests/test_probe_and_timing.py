"""Measure each audio file once, ever -- and report a total that means the run.

Three separate defects, all found in the same pair of logs off a machine with a
134 MB ``collection.nml`` and a 52 MB ``database.xml``:

* **The start-time cache was never saved.** ``StartTimeCache.save()`` existed and
  was called from nowhere, so ``start_time_report.csv`` sat at its 24-byte header
  and every launch re-probed every track it wrote -- 28 ms of ``ffprobe`` each,
  about 12 seconds of an 865-track run, paid again every session.
* **The probes ran one at a time**, inside the decision loop, where only one
  track's path is known.
* **The reported total was read when the log was written**, which is after the
  log re-reads the whole collection to check it still parses. About 6.5 seconds
  of each "total" was the log's own after-work, and the phases underneath it
  added up to something else entirely.

``ffprobe`` is never actually run here: these count calls to
``_ffprobe_start_time``, because the number of times a real file gets measured is
the whole point and nothing else would notice it going back up.
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                              # noqa: E402

from trackcleaner.config.paths import Settings                          # noqa: E402
from trackcleaner.io import backup as backup_mod                        # noqa: E402
from trackcleaner.model import cues as cues_mod                         # noqa: E402
from trackcleaner.model.cues import StartTimeCache                      # noqa: E402
from trackcleaner.model.library import TRAKTOR, VDJ                     # noqa: E402
from trackcleaner.sync.engine import NEW, OVERWRITE                     # noqa: E402
from fixtures import write_sample                                       # noqa: E402


# ---------------------------------------------------------------- fixtures --
@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _window(app, where, monkeypatch):
    from trackcleaner.config import paths as paths_mod
    where.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(where / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(where / "settings.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(where / "baseline.csv"))
    monkeypatch.setattr(paths_mod, "app_dir", lambda: str(where))
    from trackcleaner.ui.main_window import MainWindow
    settings = Settings()
    settings.paths = write_sample(where)
    return MainWindow(settings)


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    w = _window(app, tmp_path, monkeypatch)
    w.show()
    yield w
    w.close()


class _Probes:
    """Stands in for ``ffprobe``: answers instantly, counts every call, and
    remembers how many were in flight at once."""

    def __init__(self, answer=0.1, delay=0.0, fails=()):
        self.answer, self.delay, self.fails = answer, delay, set(fails)
        self.paths: list[str] = []
        self.peak = 0
        self._live = 0
        self._lock = threading.Lock()

    def __call__(self, path):
        with self._lock:
            self.paths.append(path)
            self._live += 1
            self.peak = max(self.peak, self._live)
        try:
            if self.delay:
                time.sleep(self.delay)
            return None if path in self.fails else self.answer
        finally:
            with self._lock:
                self._live -= 1

    @property
    def count(self):
        return len(self.paths)


@pytest.fixture
def probes(monkeypatch):
    p = _Probes()
    monkeypatch.setattr(cues_mod, "_ffprobe_start_time", p)
    return p


def _cache(tmp_path, mode="update"):
    return StartTimeCache(str(tmp_path / "start_times.csv"), mode)


# ==================== the measurement outlives the session ==================
def test_a_probed_file_is_never_probed_again(tmp_path, probes):
    """The defect, stated as a property. Probe, save, reload into a fresh cache
    the way a relaunch does -- and the second run must probe nothing.

    Counting probes is the only thing that would catch a regression to
    never-saving: the app behaved correctly the whole time it was doing this, it
    was just doing it again every session.
    """
    first = _cache(tmp_path)
    first.load()
    assert first.get(r"C:\music\a.mp3") == 0.1
    first.save()

    second = _cache(tmp_path)
    second.load()
    probes.paths.clear()

    assert second.get(r"C:\music\a.mp3") == 0.1
    assert probes.count == 0, "a saved measurement was measured again"


def test_the_saved_file_is_what_load_reads_back(tmp_path, probes):
    """Round trip through the csv, not just through the dict: the header row and
    the Class A/B bucket have to survive or ``load`` drops the row."""
    cache = _cache(tmp_path)
    cache.get(r"C:\music\A.MP3")
    cache.save()

    text = (tmp_path / "start_times.csv").read_text(encoding="utf-8")
    assert text.splitlines()[0] == "start_time,bucket,path"
    assert "0.100000,A," in text
    # Case-folded on the way in and on the way out, so a path that differs only
    # in case is the same file.
    assert _reloaded(tmp_path).get(r"c:\MUSIC\a.mp3") == 0.1


def _reloaded(tmp_path):
    cache = _cache(tmp_path)
    cache.load()
    return cache


def test_a_class_b_file_saves_as_class_b(tmp_path, monkeypatch):
    monkeypatch.setattr(cues_mod, "_ffprobe_start_time", _Probes(answer=0.0))
    cache = _cache(tmp_path)
    cache.get(r"C:\music\b.mp3")
    cache.save()

    assert ",B," in (tmp_path / "start_times.csv").read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", ["cache", "probe"])
def test_only_update_mode_writes_the_file(tmp_path, probes, mode):
    """``cache`` is "trust the file", ``probe`` is "trust nothing" -- neither one
    is allowed to rewrite what the user curated."""
    cache = _cache(tmp_path, mode)
    cache.get(r"C:\music\a.mp3")

    cache.save()

    assert not os.path.exists(tmp_path / "start_times.csv")


def test_a_run_that_probed_nothing_writes_nothing(tmp_path, probes):
    """Self-guarding, which is what makes it safe to call from ``closeEvent``:
    quitting the app must not rewrite the csv for nothing."""
    cache = _cache(tmp_path)
    cache.load()

    cache.save()

    assert not os.path.exists(tmp_path / "start_times.csv")


def test_closing_the_window_saves_what_was_probed(window, probes, tmp_path):
    """The wiring, not the method: a probe from any path -- a sync, a waveform,
    a single track -- is on disk before the app exits."""
    window.start_time_cache.mode = "update"
    window.start_time_cache.get(r"C:\music\a.mp3")

    window.close()

    saved = open(window.settings.paths.csv, encoding="utf-8").read()
    assert r"c:\music\a.mp3" in saved


# ========================= many at once, each one once ======================
def test_prime_measures_every_unknown_path(tmp_path, probes):
    cache = _cache(tmp_path)

    assert cache.prime([r"C:\a.mp3", r"C:\b.mp3"]) == 2
    assert cache.get(r"C:\a.mp3") == 0.1 and probes.count == 2


def test_prime_agrees_with_get(tmp_path, probes):
    """An accelerator, not a second implementation: whatever ``prime`` puts in
    the cache is what asking one at a time would have produced."""
    paths = [r"C:\a.mp3", r"C:\b.mp3", r"C:\c.mp3"]
    primed = _cache(tmp_path)
    primed.prime(paths)
    serial = _cache(tmp_path)

    assert [primed.get(p) for p in paths] == [serial.get(p) for p in paths]


def test_prime_skips_what_is_already_known(tmp_path, probes):
    cache = _cache(tmp_path)
    cache.get(r"C:\a.mp3")
    probes.paths.clear()

    assert cache.prime([r"C:\a.mp3", r"C:\b.mp3"]) == 1
    assert probes.paths == [r"C:\b.mp3"]


def test_the_same_path_twice_is_one_probe(tmp_path, probes):
    """A playlist may well hold the same file under two keys."""
    assert _cache(tmp_path).prime([r"C:\a.mp3", r"C:\A.MP3"]) == 1
    assert probes.count == 1


def test_a_file_that_will_not_probe_stays_unknown(tmp_path, monkeypatch):
    """Never cache a guess. An unmeasurable file reports no start time, and
    ``_retimed_pois`` leaves an unknown file's cues exactly where they are --
    caching a wrong value here would move them."""
    monkeypatch.setattr(cues_mod, "_ffprobe_start_time",
                        _Probes(fails=[r"C:\broken.mp3"]))
    cache = _cache(tmp_path)

    cache.prime([r"C:\broken.mp3"])

    cache.save()
    assert not os.path.exists(tmp_path / "start_times.csv")


def test_cache_mode_never_probes(tmp_path, probes):
    """"Use the file and nothing else" has to mean it here too, or the mode does
    not exist."""
    assert _cache(tmp_path, "cache").prime([r"C:\a.mp3"]) == 0
    assert probes.count == 0


def test_probe_mode_re_measures_a_known_file(tmp_path, monkeypatch):
    """The escape hatch for a re-encoded file: ``probe`` ignores what it knows."""
    p = _Probes()
    monkeypatch.setattr(cues_mod, "_ffprobe_start_time", p)
    cache = _cache(tmp_path, "probe")
    cache._cache[r"c:\a.mp3"] = 9.0

    cache.prime([r"C:\a.mp3"])

    assert p.count == 1


def test_the_probes_really_do_run_at_once(tmp_path, monkeypatch):
    """The point of the pool. Serially these would take 8 x 50 ms; the assertion
    is on overlap rather than on wall clock, because a loaded CI machine can be
    slow without being serial."""
    p = _Probes(delay=0.05)
    monkeypatch.setattr(cues_mod, "_ffprobe_start_time", p)

    _cache(tmp_path).prime([rf"C:\{n}.mp3" for n in range(8)], workers=8)

    assert p.count == 8
    assert p.peak >= 2, f"probes ran {p.peak} at a time"


# ===================== a sync measures only what it writes ==================
def _node(window):
    node = next(n for n in window.libraries[TRAKTOR].playlists()
                if n.playlist and n.playlist.track_keys)
    node.playlist.track_keys = list(window.libraries[TRAKTOR].tracks)
    return node


def test_a_sync_probes_each_written_track_at_most_once(window, probes):
    window.start_time_cache.mode = "update"
    node = _node(window)

    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)

    assert probes.count <= len(node.playlist.track_keys)
    assert len(set(p.lower() for p in probes.paths)) == probes.count


def test_a_sync_probes_nothing_for_a_track_it_skips(window, probes):
    """Priming happens after the loop has decided, on the plan -- not before it,
    on the playlist. Moving it earlier would measure every track the run then
    goes on to skip: 865 files probed to write 420."""
    node = _node(window)
    window.start_time_cache.mode = "update"
    window.sync_engine.sync_playlist(node, TRAKTOR, NEW, target=VDJ)
    probes.paths.clear()

    # Everything is in the target now, so New Only writes nothing at all.
    window.sync_engine.sync_playlist(node, TRAKTOR, NEW, target=VDJ)

    assert probes.count == 0


def test_syncing_to_the_same_program_probes_nothing(window, probes):
    """One timeline, no conversion, so there is nothing to measure. The adapters
    are compared before the paths are even collected."""
    from trackcleaner.sync import engine as engine_mod
    window.start_time_cache.mode = "update"
    plan = engine_mod._Plan()
    plan.sources = dict(window.libraries[TRAKTOR].tracks)

    window.sync_engine._prime_start_times(plan, TRAKTOR, TRAKTOR)

    assert probes.count == 0


# ===================== tags and key are one pass, not two ===================
def test_the_key_rides_along_with_the_tags(window):
    """Two walks of a 134 MB collection to set a genre and a key on the same
    entry was one walk too many -- 2.1 s of one run, for a single track's key."""
    node = _node(window)
    window.sync_engine.sync_playlist(node, TRAKTOR, NEW, target=VDJ)
    from trackcleaner.io import vdj_read
    paths = window.settings.paths
    window.libraries[VDJ] = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    for n, key in enumerate(node.playlist.track_keys):
        src = window.libraries[TRAKTOR].tracks[key]
        src.genre = f"Changed {n}"
        src.key_value = 7

    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)

    walks = window.sync_engine.timing.walks
    assert walks <= 2, f"cues plus tags-and-key is two walks, got {walks}"


def test_the_merged_write_leaves_the_same_file(window):
    """The property the whole merge rests on, at the seam where it happens: one
    call carrying tags and key produces byte for byte what two calls produce."""
    import shutil
    key = _a_vdj_key(window)
    adapter = window.sync_engine.adapter_for(VDJ)
    path = adapter.collection_path
    before = path + ".before"
    shutil.copyfile(path, before)

    adapter.write_metadata_job(key, {"genre": "Merged"})()
    adapter.write_key_job(key, 7)()
    separately = open(path, encoding="utf-8").read()

    shutil.copyfile(before, path)
    adapter.write_metadata_jobs(
        {key: {"genre": "Merged", **adapter.key_fields(7)}})()

    assert open(path, encoding="utf-8").read() == separately


def _a_vdj_key(window):
    node = _node(window)
    window.sync_engine.sync_playlist(node, TRAKTOR, NEW, target=VDJ)
    from trackcleaner.io import vdj_read
    paths = window.settings.paths
    window.libraries[VDJ] = vdj_read.read(paths.xml, paths.vdj_folders_dir())
    return next(iter(window.libraries[VDJ].tracks))


def test_every_adapter_spells_a_key_its_own_way(window):
    """``key_fields`` is the per-program notation rule, lifted out of the write
    so a metadata pass can carry it. Traktor needs both attributes to agree."""
    traktor = window.sync_engine.adapter_for(TRAKTOR).key_fields(7)
    vdj = window.sync_engine.adapter_for(VDJ).key_fields(7)

    assert traktor["key_value"] == "7" and traktor["key"]
    assert vdj["key"] and "key_value" not in vdj
    assert traktor["key"] != vdj["key"], "open key vs musical key"


# ============================ the total means the run =======================
def test_the_phases_add_up_to_the_total(window):
    """Nothing may hide between the phases. Whatever is not in one of them is
    charged to "decision loop", so this holds by construction -- which is the
    point: the next unphased cost shows up as a suspiciously large loop rather
    than as a total nobody can account for."""
    node = _node(window)

    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)

    timing = window.sync_engine.timing
    assert sum(timing.phases.values()) == pytest.approx(timing.elapsed(),
                                                        abs=1e-6)


def test_the_run_stops_being_timed_when_it_ends(window):
    """The total used to be read when the log was written, which is after the
    log re-reads the whole collection to verify it. On the big machine that put
    6.5 seconds of the log's own work inside the sync's total."""
    node = _node(window)
    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)
    timing = window.sync_engine.timing

    frozen = timing.elapsed()
    time.sleep(0.05)

    assert timing.elapsed() == frozen


def test_the_fixed_costs_are_named(window):
    """The backup copy, the parse guard and the one whole-file write are real
    seconds on a 52 MB collection. Unphased, they were indistinguishable from
    the loop."""
    node = _node(window)

    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)

    phases = window.sync_engine.timing.phases
    for name in ("backup", "collection flush", "decision loop"):
        assert name in phases, f"{name} is not timed"


def test_the_backup_is_inside_the_run(window, monkeypatch):
    """It used to happen before the clock started, so a second and a half of
    copying simply did not appear anywhere."""
    def slow_backup(path, program):
        time.sleep(0.05)
        return ""
    monkeypatch.setattr(backup_mod, "make_backup", slow_backup)
    node = _node(window)

    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)

    assert window.sync_engine.timing.phases["backup"] >= 0.05


def test_a_track_sync_copies_no_collection_beside_the_users_own(window,
                                                                monkeypatch):
    """A playlist sync can rewrite membership and hundreds of entries, so it
    snapshots the collection first. A track sync touches one entry, and copying
    44 MB for each of them left the user's VirtualDJ folder growing without
    bound -- 88 MB from two syncs 42 seconds apart.

    What still covers a track sync is the whole-library snapshot taken at
    launch, plus Ctrl-S and the autosave timer.
    """
    copied = []
    monkeypatch.setattr(backup_mod, "make_backup",
                        lambda path, program: copied.append(path) or "")
    key = next(iter(_node(window).playlist.track_keys))

    window.sync_engine.sync_track(key, TRAKTOR, VDJ)
    assert copied == [], "a single track needs no collection copy"

    window.sync_engine.sync_playlist(_node(window), TRAKTOR, OVERWRITE,
                                     target=VDJ)
    assert copied, "a playlist sync still takes one"


def test_a_run_of_several_playlists_copies_each_collection_once(window, monkeypatch):
    """Looped over a folder, every playlist used to copy the whole target
    collection first: 322 copies of collection.nml (40 GB) and 602 of
    database.xml (42 GB) had piled up beside the user's own. Inside a batch the
    copy taken before the first playlist is the run's backup."""
    copied = []
    monkeypatch.setattr(backup_mod, "make_backup",
                        lambda path, program: copied.append(path) or f"{path}.bak")
    nodes = [n for n in window.libraries[TRAKTOR].playlists() if n.playlist is not None]
    assert len(nodes) > 1

    with window.sync_engine.batch():
        results = [window.sync_engine.sync_playlist(n, TRAKTOR, OVERWRITE, target=VDJ)
                   for n in nodes]

    assert len(copied) == 1, "one copy of the VirtualDJ database for the whole run"
    assert {r.backup_path for r in results} == {copied[0] + ".bak"}, \
        "every playlist reports the run's backup"

    window.sync_engine.sync_playlist(nodes[0], TRAKTOR, OVERWRITE, target=VDJ)
    assert len(copied) == 2, "outside a batch a playlist sync still takes its own"


def test_the_sync_command_runs_as_one_batch(window, monkeypatch):
    """The window's Sync over a selection is the case that piled them up."""
    copied = []
    monkeypatch.setattr(backup_mod, "make_backup",
                        lambda path, program: copied.append(path) or "")
    nodes = [n for n in window.libraries[TRAKTOR].playlists() if n.playlist is not None]
    tree = window.playlist_panel.tree_for(TRAKTOR)
    tree.select_nodes(nodes)
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok)

    window._sync_selected(TRAKTOR, OVERWRITE, None, VDJ)

    assert len(copied) == 1
