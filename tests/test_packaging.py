"""How a packaged build finds its files.

The Windows build is portable: settings, backups and images sit next to the
exe. A packaged macOS app cannot write into its own bundle, so it keeps its
files in Application Support; images fall back to the copy bundled inside the
package; and an app started from Finder is given Homebrew's directories so
ffmpeg and ffprobe can be found.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner import app as app_mod                                  # noqa: E402
from trackcleaner.config import paths                                    # noqa: E402


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    exe_dir = tmp_path / "dist"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "SlipStreamIO.exe"))
    return exe_dir


def test_a_packaged_windows_build_keeps_its_files_beside_the_exe(frozen, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    assert paths.app_dir() == str(frozen)


def test_a_packaged_mac_app_keeps_its_files_in_application_support(
        frozen, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "home"))

    where = paths.app_dir()

    assert where == os.path.join(str(tmp_path / "home"), "Library",
                                 "Application Support", "SlipStreamIO")
    assert os.path.isdir(where), "created on first use"


def test_running_from_source_uses_the_project_root(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    assert paths.app_dir() == root


def test_a_package_falls_back_to_its_bundled_images(frozen, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    bundle = tmp_path / "bundle"
    (bundle / "Images").mkdir(parents=True)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)

    assert paths.images_dir() == str(bundle / "Images")


def test_an_images_folder_beside_the_app_wins(frozen, monkeypatch, tmp_path):
    """So a logo can still be replaced without a rebuild."""
    monkeypatch.setattr(sys, "platform", "win32")
    (frozen / "Images").mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)

    assert paths.images_dir() == str(frozen / "Images")


def test_a_mac_app_from_finder_can_find_homebrew_tools(monkeypatch, tmp_path):
    brew = tmp_path / "homebrew-bin"
    brew.mkdir()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(app_mod, "_MAC_TOOL_DIRS", (str(brew), str(tmp_path / "absent")))
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))

    app_mod._find_mac_tools()
    app_mod._find_mac_tools()                       # idempotent

    assert os.environ["PATH"].split(os.pathsep) == ["/usr/bin", "/bin", str(brew)]


def test_other_platforms_keep_their_path(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("PATH", "C:\\Windows")

    app_mod._find_mac_tools()

    assert os.environ["PATH"] == "C:\\Windows"


# ================= no console window per external tool call ==================
def test_every_command_line_call_hides_its_console_window():
    """Reported on the packaged exe: an Overwrite sync flashed an ffprobe
    console window per track, non-stop, freezing the desktop. Run from source
    the tools share Python's console, so it never showed; a windowed build has
    none, and Windows opens one per call unless told not to.

    Every ``subprocess.run`` in the package must pass ``creationflags``
    (``CREATE_NO_WINDOW`` on Windows, 0 elsewhere). ``Popen`` is only used for
    Explorer, a windowed program that opens no console."""
    import ast
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "trackcleaner")
    missing = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            tree = ast.parse(open(path, encoding="utf-8").read())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("run", "check_output", "call")
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "subprocess"
                        and not any(k.arg == "creationflags" for k in node.keywords)):
                    missing.append(f"{os.path.relpath(path, root)}:{node.lineno}")

    assert missing == []


# ===================== first-run library guesses ==============================
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: str(home) if p == "~" else p)
    return home


def _settings():
    from trackcleaner.config.paths import Settings
    return Settings()


def test_a_first_run_finds_the_traktor_collection_written_last(tmp_path, monkeypatch):
    import time
    from trackcleaner.ui import main_window
    home = _home(tmp_path, monkeypatch)
    ni = home / "Documents" / "Native Instruments"
    old, new = ni / "Traktor 3.5.1" / "collection.nml", ni / "Traktor 4.0.0" / "collection.nml"
    for f in (old, new):
        f.parent.mkdir(parents=True)
        f.write_text("<NML/>")
    os.utime(old, (time.time() - 86400, time.time() - 86400))
    monkeypatch.setattr(main_window, "_vdj_database_candidates", lambda: [])
    s = _settings()

    main_window._default_paths(s)

    assert s.paths.nml == str(new)


def test_a_first_run_never_guesses_a_stray_collection(tmp_path, monkeypatch):
    """1.0.0 took D:\VirtualDJ\traktor.nml -- an old Traktor 3 collection on
    the reported machine -- for the Traktor library, and an Overwrite synced
    into it. Only the program's own location is a guess."""
    from trackcleaner.ui import main_window
    _home(tmp_path, monkeypatch)
    monkeypatch.setattr(main_window, "_vdj_database_candidates", lambda: [])
    s = _settings()

    main_window._default_paths(s)

    assert s.paths.nml == ""
    assert all("traktor.nml" not in c.lower() and "tententen" not in c.lower()
               for c in main_window._traktor_collection_candidates()
               + main_window._vdj_database_candidates())


def test_a_first_run_finds_virtualdjs_home_database(tmp_path, monkeypatch):
    from trackcleaner.ui import main_window
    home = _home(tmp_path, monkeypatch)
    db = home / "Documents" / "VirtualDJ" / "database.xml"
    db.parent.mkdir(parents=True)
    db.write_text("<VirtualDJ_Database/>")
    s = _settings()

    main_window._default_paths(s)

    assert s.paths.xml == str(db)


def test_a_configured_path_is_never_replaced(tmp_path, monkeypatch):
    from trackcleaner.ui import main_window
    _home(tmp_path, monkeypatch)
    s = _settings()
    s.paths.nml, s.paths.xml = r"X:\mine.nml", r"X:\mine.xml"

    main_window._default_paths(s)

    assert (s.paths.nml, s.paths.xml) == (r"X:\mine.nml", r"X:\mine.xml")
