r"""A track sync recolours one track, not the whole library.

Syncing a single track took 30-40 seconds from click to dialog while the sync
itself took 3.3. The rest was the refresh afterwards: re-parse both collections
(178 MB, ~16 s), re-diff all 165,794 tracks (~2 s), and stat every audio file
(~7 s) -- to reflect one changed track.

So a track sync now re-reads **only the entries it touched** and recolours those.
The saving is only real if the whole-library read genuinely does not happen, so
that is asserted on the call rather than on the clock.

The property that keeps it honest: **the colours still come from disk.** Each
program is asked to read the entry back rather than being taken at its word, so
a write that did not land cannot show as green -- which is precisely what the
full reload was defending. The targeted path and the full reload must agree on
what they paint, and that is the central test here.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                              # noqa: E402

from trackcleaner.config.paths import Settings                          # noqa: E402
from trackcleaner.io import backup as backup_mod                        # noqa: E402
from trackcleaner.io import traktor_read, vdj_read                      # noqa: E402
from trackcleaner.model.library import TRAKTOR, VDJ                     # noqa: E402
from trackcleaner.model.status import GREEN                             # noqa: E402
from fixtures import write_sample                                       # noqa: E402


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
    # Closed rather than left to the collector: 13 live MainWindows piling up in
    # one xdist worker is the shape Qt is least happy about. Safe here because
    # the settings/baseline/app paths above are all redirected into tmp_path,
    # so ``closeEvent``'s save lands in the sandbox.
    w.close()


@pytest.fixture
def quiet(monkeypatch):
    """Swallow the result dialog; return what it would have said."""
    from trackcleaner.ui import main_window as mw
    said = []
    monkeypatch.setattr(mw.QMessageBox, "information",
                        lambda *a, **k: said.append(a[2]))
    monkeypatch.setattr(mw.QMessageBox, "warning",
                        lambda *a, **k: said.append(a[2]))
    return said


def _synced_key(window):
    """A track both programs hold, so a sync has something to converge.

    The sample VirtualDJ database starts empty, so the entry is created first
    and the libraries re-read -- that setup is deliberately done *before* any
    test installs its counters, so it is never mistaken for the sync's own work.
    """
    trk = window.libraries[TRAKTOR]
    key = next(k for k in trk.tracks if trk.tracks[k].path)
    window.sync_engine.sync_track(key, TRAKTOR, VDJ)
    window._reload_preserving_view()
    assert key in window.libraries[VDJ].tracks, "the entry was not created"
    # `_sync_track` takes its source from the window, which starts at VirtualDJ.
    # Leaving it there would sync VirtualDJ into itself and prove nothing.
    window._current_source = TRAKTOR
    return key


def _count_reads(monkeypatch):
    """Count whole-library parses, which is what the reload does and this must
    not."""
    calls = []
    for mod, name in ((traktor_read, "read"), (vdj_read, "read")):
        real = getattr(mod, name)

        def counted(*a, _real=real, _mod=mod, **k):
            calls.append(_mod.__name__)
            return _real(*a, **k)

        monkeypatch.setattr(mod, name, counted)
    return calls


# ====================== the whole library is not re-read ====================
def test_a_track_sync_does_not_re_read_either_collection(window, quiet,
                                                         monkeypatch):
    """Pinned on the call, not the clock. A version that reloaded and *then*
    patched would still cost the 30 seconds this exists to remove."""
    key = _synced_key(window)
    reads = _count_reads(monkeypatch)

    window._sync_track(key, VDJ)

    assert reads == [], f"the library was re-parsed: {reads}"
    assert quiet, "and the user was still told what happened"


def test_a_refresh_still_re_reads_everything(window, monkeypatch):
    """The escape hatch has to keep working: Refresh is what picks up an edit
    another program made behind this app's back."""
    reads = _count_reads(monkeypatch)

    window._reload_preserving_view()

    assert reads, "Refresh must still go to disk"


# ===================== the two paths must paint the same ====================
def test_the_targeted_colour_equals_what_a_full_reload_gives(window, quiet):
    """The central property. If these ever disagree, one of them is lying to
    the user and the fast one is the one nobody would check."""
    key = _synced_key(window)

    window._sync_track(key, VDJ)
    targeted = {src: rep.track_status.get(key)
                for src, rep in window.reports.items()}

    window._reload_preserving_view()
    reloaded = {src: rep.track_status.get(key)
                for src, rep in window.reports.items()}

    assert targeted == reloaded
    # A third loaded program that does not hold this track has no opinion about
    # it, and must not be given one.
    assert {targeted[s] for s in (TRAKTOR, VDJ)} == {GREEN}, targeted


def test_the_synced_track_goes_green_in_both_panes(window, quiet):
    key = _synced_key(window)

    window._sync_track(key, VDJ)

    for source in (TRAKTOR, VDJ):
        assert window.reports[source].track_status.get(key) == GREEN, source


def test_other_tracks_are_left_alone(window, quiet):
    """A targeted update must be targeted: nothing it did not touch may move."""
    key = _synced_key(window)
    before = {src: dict(rep.track_status) for src, rep in window.reports.items()}

    window._sync_track(key, VDJ)

    for source, report in window.reports.items():
        for other, colour in before[source].items():
            if other != key:
                assert report.track_status.get(other) == colour, other


# ========================== the colour comes from disk =======================
def test_a_write_that_did_not_land_does_not_go_green(window, quiet, monkeypatch):
    """The reason the entry is read back rather than believed.

    Here the collection is left exactly as it was while memory is told the write
    succeeded. Reading the file back is what keeps the square honest; trusting
    the in-memory copy would paint it green over an unchanged file.
    """
    key = _synced_key(window)
    window.libraries[VDJ].tracks[key].title = "Only In Memory"
    window.libraries[TRAKTOR].tracks[key].title = "Only In Memory"

    adapter = window.adapters[VDJ]
    monkeypatch.setattr(adapter, "read_tracks", lambda by_key: {})

    window._restatus_tracks({TRAKTOR, VDJ}, [key])

    assert window.reports[VDJ].track_status.get(key) != GREEN


def test_the_entry_is_read_back_from_the_collection(window, quiet, monkeypatch):
    """Pinned on the call: the fix is "ask the file", so asking has to happen."""
    key = _synced_key(window)
    asked = {}
    for source in (TRAKTOR, VDJ):
        adapter = window.adapters[source]
        real = adapter.read_tracks
        def spy(by_key, _r=real, _s=source):
            asked[_s] = dict(by_key)
            return _r(by_key)

        monkeypatch.setattr(adapter, "read_tracks", spy)

    window._sync_track(key, VDJ)

    assert set(asked) == {TRAKTOR, VDJ}, "both sides -- a sync can write to either"
    for source, by_key in asked.items():
        assert list(by_key) == [key], source


def test_a_failing_read_falls_back_to_the_full_reload(window, quiet, monkeypatch):
    """A targeted read that breaks must not leave a stale colour behind: it
    gives up and does the thing that is always right."""
    key = _synced_key(window)

    def boom(by_key):
        raise RuntimeError("collection unreadable")

    monkeypatch.setattr(window.adapters[VDJ], "read_tracks", boom)
    reloaded = []
    monkeypatch.setattr(window, "_reload_preserving_view",
                        lambda *a, **k: reloaded.append(True))

    window._restatus_tracks({TRAKTOR, VDJ}, [key])

    assert reloaded == [True]


def _diverge(window, key):
    """Change the track on the VirtualDJ side only, on disk.

    Every assertion about an *in-sync* track is symmetric, so it cannot tell a
    diff run in the right direction from one run backwards, nor a maintained
    ownership record from none at all. A genuine divergence can.
    """
    adapter = window.adapters[VDJ]
    job = adapter.write_metadata_job(key, {"title": "Changed On The VDJ Side"})
    assert job() is True, "the fixture edit did not land"


def test_an_out_of_sync_track_gets_the_same_colour_as_a_full_reload(window,
                                                                    quiet):
    """The central property, on a track where direction matters.

    A yellow says *who* moved, so diffing the pair the wrong way round produces
    a plausible-looking but opposite answer. Only a real divergence exposes it.
    """
    key = _synced_key(window)
    _diverge(window, key)

    window._restatus_tracks({TRAKTOR, VDJ}, [key])
    targeted = {src: rep.track_status.get(key)
                for src, rep in window.reports.items()}

    window._reload_preserving_view()
    reloaded = {src: rep.track_status.get(key)
                for src, rep in window.reports.items()}

    assert targeted[VDJ] != GREEN, "the fixture must actually diverge"
    assert targeted == reloaded


# ============================== attribution =================================
def test_the_ownership_record_matches_a_full_reload(window, quiet):
    """Not cosmetic: this decides whether a later Update overwrites or skips.

    Asserted on a diverged track, because an in-sync one has no owner at all --
    ``attribute_changes`` omits keys that agree — and comparing two Nones proves
    nothing.
    """
    key = _synced_key(window)
    _diverge(window, key)

    window._restatus_tracks({TRAKTOR, VDJ}, [key])
    targeted = {pair: owners.get(key)
                for pair, owners in window._owners.items()}

    window._reload_preserving_view()
    reloaded = {pair: owners.get(key)
                for pair, owners in window._owners.items()}

    assert any(v is not None for v in targeted.values()), "no verdict to compare"
    assert targeted == reloaded


def test_a_verdict_that_has_gone_away_is_cleared(window, quiet):
    """The other half: once the two agree there is no owner, and a targeted
    update that only ever *added* would leave the old one standing for ever."""
    from trackcleaner.model import baseline as bl
    key = _synced_key(window)
    # The pair is named for the non-reference side; Traktor is the reference in
    # this fixture, so it is the VirtualDJ pair that carries the verdict.
    pair = bl.pair_id(VDJ, window.reference_program())
    window._owners.setdefault(pair, {})[key] = VDJ      # a stale verdict

    window._sync_track(key, VDJ)

    assert key not in window._owners.get(pair, {}), \
        "a verdict that no longer applies must be removed, not overwritten"


def test_the_change_tally_matches_a_full_reload(window, quiet):
    """``_track_changed_by`` decides *which* yellow a row shows, and a verdict
    that has since gone away has to go with it.

    A track that was attributed to one side is absent from the result once the
    two agree -- ``attribute_changes`` omits in-sync keys -- so a targeted update
    that only ever adds would leave the old verdict standing for ever.
    """
    key = _synced_key(window)
    window._track_changed_by[key] = {TRAKTOR, VDJ}   # a stale verdict

    window._sync_track(key, VDJ)
    targeted = window._track_changed_by.get(key)

    window._reload_preserving_view()

    assert targeted == window._track_changed_by.get(key)


def test_the_engine_sees_the_updated_owners(window, quiet):
    key = _synced_key(window)

    window._sync_track(key, VDJ)

    assert window.sync_engine.owners is window._owners


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
