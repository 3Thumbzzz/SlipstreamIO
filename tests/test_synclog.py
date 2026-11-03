"""The Debug menu's sync logs.

These exist because the interesting sync failures only happen against **real**
libraries: the fixtures have no nested `<GRID>` cue, no attribution history and a
handful of tracks, so a whole class of defect is invisible here and can only be
caught by recording what happened on a user's own collection.

The load-bearing property is therefore **that logging changes nothing**. A
diagnostic that alters, slows or breaks the operation it observes is worse than
no diagnostic — so the null sink, the swallowed failures and the identical
results are tested harder than the formatting is.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                              # noqa: E402

from trackcleaner.config.paths import Settings                          # noqa: E402
from trackcleaner.io import backup as backup_mod                        # noqa: E402
from trackcleaner.io import synclog                                     # noqa: E402
from trackcleaner.model.library import TRAKTOR, VDJ                     # noqa: E402
from trackcleaner.sync.engine import NEW, OVERWRITE, UPDATE             # noqa: E402
from fixtures import write_sample                                       # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
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
    yield w
    w.close()


def _node(window):
    return next(n for n in window.libraries[TRAKTOR].playlists()
                if n.playlist and n.playlist.track_keys)


def _logs(tmp_path):
    d = tmp_path / "logs"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


# ============================ where logs go =================================
def test_the_folder_resolves_through_the_module(tmp_path, monkeypatch):
    """Test isolation: a from-import of app_dir would bind the original and
    write the developer's real folder -- see "Development conventions" in
    HANDOFF.md."""
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(paths_mod, "app_dir", lambda: str(tmp_path))

    assert synclog.logs_dir() == str(tmp_path / "logs")


def test_a_log_is_named_for_its_kind_and_subject(tmp_path, monkeypatch):
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(paths_mod, "app_dir", lambda: str(tmp_path))

    name = os.path.basename(synclog.log_path("sync_update", "Zero To Break Chill"))

    assert name.startswith("sync-update_")
    assert name.endswith("_Zero-To-Break-Chill.log")


def test_awkward_names_do_not_break_the_filename(tmp_path, monkeypatch):
    """Playlist names contain slashes, colons and quotes."""
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(paths_mod, "app_dir", lambda: str(tmp_path))

    name = os.path.basename(synclog.log_path("track_sync", 'A/B: "x" *?'))

    for bad in '/\\:*?"<>|':
        assert bad not in name


def test_two_runs_do_not_collide(tmp_path, monkeypatch):
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(paths_mod, "app_dir", lambda: str(tmp_path))

    a = synclog.log_path("sync_update", "P", when=1000)
    b = synclog.log_path("sync_update", "P", when=1061)

    assert a != b


# ============================ the null sink =================================
def test_the_engine_starts_with_logging_off(window):
    assert isinstance(window.sync_engine.log, synclog.NullLog)
    assert window.sync_engine.log.enabled is False


def test_the_null_sink_answers_every_call_the_engine_makes(window):
    """It is a null *object*, not a flag -- a missing method would crash a sync
    that has logging switched off, which is every ordinary sync."""
    null = synclog.NullLog()
    real = synclog.SyncLog("k", "l")

    for name in ("event", "section", "line", "note_file", "field_comparison",
                 "check_parses"):
        assert hasattr(null, name), name
        assert hasattr(real, name), name


def test_the_null_sink_does_not_read_the_collection(window, monkeypatch):
    """check_parses on the null sink must not parse a 40 MB database.xml to
    answer a question nobody asked."""
    import xml.etree.ElementTree as ET
    called = []
    monkeypatch.setattr(ET, "parse", lambda *a, **k: called.append(a))

    synclog.NullLog().check_parses("before", __file__)

    assert called == []


def test_a_sync_with_logging_off_writes_no_file(window, tmp_path):
    window.sync_engine.sync_playlist(_node(window), TRAKTOR, NEW, target=VDJ)

    assert _logs(tmp_path) == []


# ======================= logging changes no outcome =========================
def test_the_same_sync_gives_the_same_result_either_way(app, tmp_path,
                                                        monkeypatch):
    """The property everything else rests on."""
    from trackcleaner.config import paths as paths_mod

    def run(where, logging_on):
        where.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(where / "b"))
        monkeypatch.setattr(paths_mod, "settings_path", lambda: str(where / "s.json"))
        monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(where / "b.csv"))
        monkeypatch.setattr(paths_mod, "app_dir", lambda: str(where))
        from trackcleaner.ui.main_window import MainWindow
        settings = Settings()
        settings.paths = write_sample(where)
        win = MainWindow(settings)
        if logging_on:
            win.sync_engine.log = synclog.SyncLog("sync_new", "p")
        node = next(n for n in win.libraries[TRAKTOR].playlists()
                    if n.playlist and n.playlist.track_keys)
        r = win.sync_engine.sync_playlist(node, TRAKTOR, NEW, target=VDJ)
        out = (r.tracks_added, r.tracks_cue_synced, r.tracks_skipped,
               r.playlists_created, r.playlist_entries_added, r.errors)
        win.close()
        return out

    quiet = run(tmp_path / "quiet", False)
    loud = run(tmp_path / "loud", True)

    assert quiet == loud


def test_a_sink_that_raises_does_not_break_the_sync(window):
    """Diagnostics must never take a write down half way."""
    class _Exploding(synclog.NullLog):
        enabled = True

        def event(self, event_name, /, **fields):
            raise RuntimeError("boom")

    window.sync_engine.log = _Exploding()

    with pytest.raises(RuntimeError):
        # Proves the sink really does raise -- so the next assertion is not
        # passing because the sink was quietly inert.
        window.sync_engine.log.event("x")

    window._finish_log(None)          # the UI path tolerates a missing log


def test_writing_a_log_that_cannot_be_saved_returns_empty(tmp_path, monkeypatch):
    """A log that cannot be written must not turn a completed sync into a failed
    one."""
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(paths_mod, "app_dir", lambda: str(tmp_path / "nope"))
    monkeypatch.setattr(os, "makedirs",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))

    assert synclog.SyncLog("k", "l").write() == ""


# ============================ what it records ===============================
@pytest.fixture
def logged(window, tmp_path):
    """One Update run with logging on, and its text.

    The parse-check toggle is on here because several tests below read those
    lines. It is **off in normal use** -- two whole-document parses are the most
    expensive thing a log does -- which is pinned separately.
    """
    window._set_debug_log("sync_update", True)
    window._set_debug_log("parse_checks", True)
    log = window._start_log("sync_update", "p")
    window.sync_engine.sync_playlist(_node(window), TRAKTOR, UPDATE, target=VDJ)
    path = window._finish_log(log)
    return open(path, encoding="utf-8").read()


def test_a_track_listed_twice_does_not_cost_the_log(window, tmp_path):
    """The cause of the reported 0-byte logs.

    ``_plan_track`` reserved a line per occurrence but stored the index under
    the *track key*, so a playlist holding the same track twice kept only the
    second and left the first reserved forever. ``"\\n".join`` then raised on the
    sentinel -- inside the ``with open()`` that had already truncated the real
    file, with the caller swallowing the exception. It only bit when both copies
    were decided WRITE, which is why Overwrite hit it and Update did not.
    """
    node = _node(window)
    keys = list(node.playlist.track_keys)
    node.playlist.track_keys = keys + keys          # every track, twice

    window._set_debug_log("sync_overwrite", True)
    log = window._start_log("sync_overwrite", "dupes")
    window.sync_engine.sync_playlist(node, TRAKTOR, OVERWRITE, target=VDJ)
    path = window._finish_log(log)

    assert path, "the log must be written at all"
    text = open(path, encoding="utf-8").read()
    assert os.path.getsize(path) > 0
    assert "<object" not in text, "a reserved line is dropped, never rendered"
    assert text.count("create_entry_job") == len(keys) * 2, \
        "each occurrence reports its own outcome"


def test_a_reserved_line_nobody_filled_is_simply_absent(tmp_path, monkeypatch):
    """What ``fill`` has always promised. Rendering it instead only traded a
    0-byte log for garbage in the middle of a good one."""
    monkeypatch.setattr(synclog.paths_mod, "app_dir", lambda: str(tmp_path))
    log = synclog.SyncLog("k", "l")
    log.line("before")
    log.reserve()
    log.line("after")

    text = open(log.write(), encoding="utf-8").read()

    assert "before" in text and "after" in text
    assert "<object" not in text
    assert log.error == ""


def test_it_records_the_write_decision_and_why(logged):
    """"Skipped" is what the user can already see; which branch skipped it is
    the thing that answers why."""
    assert "decision=" in logged
    assert "reason=" in logged


def test_it_records_the_attribution_verdict(logged):
    """The Update-skips-everything question is entirely about this value."""
    assert "attribution" in logged
    assert "owner=" in logged
    assert "attribution_available=" in logged


def test_it_records_the_writers_own_answers(logged):
    """Not the in-memory guess -- whether the file was actually changed."""
    assert "create_entry_job" in logged


def test_it_records_the_files_it_touched(logged):
    assert "target collection" in logged
    assert "backup" in logged


def test_it_parses_the_target_before_and_after(logged):
    """A file that parsed before a run and not after is the run's own doing."""
    assert "parse-before" in logged
    assert "parse-after" in logged


def test_the_parse_checks_are_off_unless_asked_for(window):
    """The expensive part of a log, and the reason a one-track sync took 5 s.

    Two whole-document ``ET.parse`` calls cost 1.13 s each on a 44 MB
    ``database.xml`` -- 2.3 s of a run that touched a single track. They stay
    available behind their own toggle, because "was the file already broken
    before this run?" is worth being able to ask; they are not worth asking on
    every sync.
    """
    window._set_debug_log("sync_update", True)
    log = window._start_log("sync_update", "p")
    window.sync_engine.sync_playlist(_node(window), TRAKTOR, UPDATE, target=VDJ)
    text = open(window._finish_log(log), encoding="utf-8").read()

    assert "parse-before" not in text and "parse-after" not in text
    assert "track" in text, "the rest of the log is unaffected"


def test_a_run_with_the_toggle_off_never_reads_the_collection_to_parse_it(window,
                                                                         monkeypatch):
    """Pinned on the call, not on the output: a check that ran and then threw its
    answer away would still cost the 2.3 s this exists to save."""
    import xml.etree.ElementTree as ET
    calls = []
    monkeypatch.setattr(synclog.ET, "parse",
                        lambda *a, **kw: calls.append(a) or ET.ElementTree())

    window._set_debug_log("sync_update", True)
    log = window._start_log("sync_update", "p")
    window.sync_engine.sync_playlist(_node(window), TRAKTOR, UPDATE, target=VDJ)
    window._finish_log(log)

    assert calls == []


def test_it_records_playlist_membership_both_sides(logged):
    assert "Playlist membership, before" in logged
    assert "Playlist membership, after" in logged


def test_a_blank_field_is_visible_as_blank(window):
    """A blank source value erasing a populated target is one of the open
    questions, so "" must not render as an empty column."""
    assert synclog.shown("") == "<blank>"
    assert synclog.shown(None) == "<blank>"
    assert synclog.shown("x") == "x"


def test_the_field_comparison_names_both_sides(window):
    from trackcleaner.model.library import Track
    log = synclog.SyncLog("k", "l")
    a = Track(key="k", source=TRAKTOR, path="p", title="Same", comment="Jazz")
    b = Track(key="k", source=VDJ, path="p", title="Same", comment="")

    log.field_comparison(("title", "comment"), a, b)

    text = log.text()
    assert "title" in text and "same" in text
    assert "DIFFERS" in text and "<blank>" in text


def test_a_malformed_target_is_reported_with_the_offending_lines(tmp_path):
    bad = tmp_path / "broken.nml"
    bad.write_text("<NML>\n<ENTRY>\n<GRID></GRID>\n</CUE_V2>\n</NML>\n",
                   encoding="utf-8")
    log = synclog.SyncLog("k", "l")
    log.want_parse_checks = True

    assert log.check_parses("after", str(bad)) is False
    assert "MALFORMED" in log.text()
    assert "CUE_V2" in log.text(), "the offending lines must travel with the log"


def test_a_missing_target_is_reported_rather_than_raising(tmp_path):
    log = synclog.SyncLog("k", "l")
    log.want_parse_checks = True

    assert log.check_parses("before", str(tmp_path / "gone.nml")) is False
    assert "missing" in log.text()


# ============================== the menu ====================================
def test_the_toggles_exist(window):
    from trackcleaner.ui.main_window import DEBUG_LOG_KINDS

    assert [k for k, _l, _h in DEBUG_LOG_KINDS] == \
        ["sync_update", "sync_overwrite", "track_sync", "performance",
         "parse_checks"]
    assert set(window._debug_actions) == {k for k, _l, _h in DEBUG_LOG_KINDS}


def test_they_start_off(window):
    assert window._debug_logs == set()
    assert all(not a.isChecked() for a in window._debug_actions.values())


def test_toggling_one_does_not_toggle_the_others(window):
    window._set_debug_log("sync_update", True)

    assert window.debug_logging("sync_update")
    assert not window.debug_logging("sync_overwrite")
    assert not window.debug_logging("track_sync")


def test_the_choice_is_remembered(window):
    """The machine gathering the logs gets restarted between runs."""
    window._set_debug_log("track_sync", True)

    assert "track_sync" in window._capture_state()["debug_logs"]


def test_a_saved_choice_is_restored(app, tmp_path, monkeypatch):
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(tmp_path / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(tmp_path / "s.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(tmp_path / "b.csv"))
    monkeypatch.setattr(paths_mod, "app_dir", lambda: str(tmp_path))
    from trackcleaner.ui.main_window import MainWindow
    settings = Settings()
    settings.paths = write_sample(tmp_path)
    settings.ui_state = {"debug_logs": ["sync_overwrite"]}

    win = MainWindow(settings)

    assert win.debug_logging("sync_overwrite")
    assert win._debug_actions["sync_overwrite"].isChecked()
    win.close()


def test_an_unknown_saved_kind_is_ignored(app, tmp_path, monkeypatch):
    """A file from a build that knew more toggles must not break this one."""
    from trackcleaner.config import paths as paths_mod
    monkeypatch.setattr(backup_mod, "backups_dir", lambda: str(tmp_path / "backups"))
    monkeypatch.setattr(paths_mod, "settings_path", lambda: str(tmp_path / "s.json"))
    monkeypatch.setattr(paths_mod, "baseline_path", lambda: str(tmp_path / "b.csv"))
    monkeypatch.setattr(paths_mod, "app_dir", lambda: str(tmp_path))
    from trackcleaner.ui.main_window import MainWindow
    settings = Settings()
    settings.paths = write_sample(tmp_path)
    settings.ui_state = {"debug_logs": ["sync_update", "future_thing"]}

    win = MainWindow(settings)

    assert win._debug_logs == {"sync_update"}
    win.close()


def test_starting_a_log_for_an_unticked_kind_does_nothing(window):
    assert window._start_log("sync_overwrite", "p") is None
    assert isinstance(window.sync_engine.log, synclog.NullLog)


def test_finishing_restores_the_null_sink(window, tmp_path):
    """Or one logged run would keep logging for the rest of the session."""
    window._set_debug_log("sync_update", True)
    log = window._start_log("sync_update", "p")
    assert window.sync_engine.log is log

    window._finish_log(log)

    assert isinstance(window.sync_engine.log, synclog.NullLog)


def test_each_run_gets_its_own_file(window, tmp_path):
    window._set_debug_log("sync_update", True)
    for i in range(2):
        log = window._start_log("sync_update", f"playlist-{i}")
        window._finish_log(log)

    assert len(_logs(tmp_path)) == 2


def test_the_overwrite_toggle_covers_overwrite_runs(window):
    from trackcleaner.ui.main_window import _MODE_LOG_KIND

    assert _MODE_LOG_KIND[UPDATE] == "sync_update"
    assert _MODE_LOG_KIND[OVERWRITE] == "sync_overwrite"
    assert NEW not in _MODE_LOG_KIND, "New Only has no reported fault to log"


def test_the_report_mentions_a_written_log(window):
    assert window._logged_note([]) == ""
    assert "a.log" in window._logged_note(["a.log"])


def test_the_report_names_a_log_it_could_not_write(window):
    """A missing log used to be indistinguishable from logging being off. Two
    playlists in a reported folder sync left 0-byte files and said nothing, so
    the user could see something was wrong with no way to say what."""
    window._log_failures = ["Breaks Warmup: UnicodeEncodeError: bad char"]
    note = window._logged_note([])
    assert "Breaks Warmup" in note and "UnicodeEncodeError" in note
    assert "completed" in note, "the sync itself succeeded; say so"
    assert window._logged_note([]) == "", "drained, so it reports once"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ========================== the performance section =========================
def test_performance_is_a_modifier_not_a_kind(window):
    """It adds a section to whichever logs are already on; on its own there is
    no file for it to be added to."""
    from trackcleaner.ui.main_window import PERF_LOG_KIND, _MODE_LOG_KIND

    assert PERF_LOG_KIND not in _MODE_LOG_KIND.values()


def test_timings_are_omitted_unless_asked_for(window, tmp_path):
    """An ordinary log must not be padded with numbers nobody wanted."""
    window._set_debug_log("sync_update", True)
    log = window._start_log("sync_update", "p")
    window.sync_engine.sync_playlist(_node(window), TRAKTOR, UPDATE, target=VDJ)
    text = open(window._finish_log(log), encoding="utf-8").read()

    assert "Performance" not in text


def test_timings_appear_when_the_toggle_is_on(window, tmp_path):
    window._set_debug_log("sync_update", True)
    window._set_debug_log("performance", True)
    log = window._start_log("sync_update", "p")
    window.sync_engine.sync_playlist(_node(window), TRAKTOR, UPDATE, target=VDJ)
    text = open(window._finish_log(log), encoding="utf-8").read()

    assert "Performance" in text
    assert "whole-file collection reads" in text
    assert "whole-file collection writes" in text
    # The number the section exists for now that the I/O is one read and one
    # write: a walk is a pass over every entry, and one per track was the cost.
    assert "document walks" in text


def test_the_baseline_is_read_once_per_run_not_once_per_track(window):
    """A performance contract, asserted because nothing else would catch a
    regression: this was reloading and reparsing the whole CSV for every track,
    costing about half a second each on a real library -- two logged runs that
    wrote nothing still took 68 s and 26 s."""
    window._set_debug_log("sync_update", True)
    log = window._start_log("sync_update", "p")
    node = _node(window)
    assert len(node.playlist.track_keys) >= 1

    window.sync_engine.sync_playlist(node, TRAKTOR, UPDATE, target=VDJ)
    window._finish_log(log)

    assert window.sync_engine.baseline_loads <= 1


def test_a_second_run_reloads_it(window):
    """Cached for a run, not for the session -- an edit between two syncs has to
    be seen."""
    window._set_debug_log("sync_update", True)
    node = _node(window)
    for _ in range(2):
        log = window._start_log("sync_update", "p")
        window.sync_engine.sync_playlist(node, TRAKTOR, UPDATE, target=VDJ)
        window._finish_log(log)

    assert window.sync_engine.baseline_loads <= 1, "per run, and reset each time"


def test_whole_file_writes_are_counted(window):
    """The number the "one rewrite per track" suspicion stands or falls on."""
    node = _node(window)
    window.sync_engine.sync_playlist(node, TRAKTOR, NEW, target=VDJ)

    assert window.sync_engine.timing.file_writes >= 1
    assert window.sync_engine.timing.file_reads >= 1
