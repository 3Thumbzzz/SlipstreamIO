"""Recycle-Bin helper: never permanently deletes, and off-Windows it's a no-op."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.io import recycle                              # noqa: E402


def test_double_null_terminated_path():
    assert recycle._double_null(r"C:\a.mp3") == "C:\\a.mp3\0\0"


def test_missing_or_empty_path_returns_false():
    assert recycle.send_to_recycle_bin(r"Z:\definitely\missing.mp3") is False
    assert recycle.send_to_recycle_bin("") is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shell API only")
def test_calls_shell_without_actually_deleting(tmp_path, monkeypatch):
    import ctypes
    f = tmp_path / "throwaway.mp3"
    f.write_bytes(b"x")
    calls = []
    _ = ctypes.windll.shell32.SHFileOperationW            # ensure attr exists to patch
    monkeypatch.setattr(ctypes.windll.shell32, "SHFileOperationW",
                        lambda p: calls.append(p) or 0, raising=False)

    ok = recycle.send_to_recycle_bin(str(f))

    assert ok is True
    assert len(calls) == 1, "the shell op must be invoked once"
    assert f.exists(), "the real file must be untouched (the shell call was mocked)"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
