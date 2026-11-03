"""The serialized, atomic background write queue used for playlist edits."""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                        # noqa: E402

from trackcleaner.io.write_queue import WriteQueue, atomic_write_text  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_atomic_write_replaces_content_and_leaves_no_tmp(tmp_path):
    path = str(tmp_path / "sub" / "file.txt")
    atomic_write_text(path, "hello")
    assert open(path).read() == "hello"
    atomic_write_text(path, "world")
    assert open(path).read() == "world"
    assert not os.path.exists(path + ".tmp")


def test_queue_runs_jobs_and_flush_waits(app, tmp_path):
    q = WriteQueue()
    path = str(tmp_path / "out.txt")
    q.submit(lambda: atomic_write_text(path, "done"))
    assert q.flush() is True
    assert open(path).read() == "done"


def test_queue_serializes_jobs_in_order(app, tmp_path):
    """A single worker must run jobs FIFO, so successive writes to the same file
    can't interleave -- the last submitted wins."""
    q = WriteQueue()
    path = str(tmp_path / "seq.txt")
    order = []
    worker_threads = set()

    def make(n):
        def job():
            worker_threads.add(threading.get_ident())
            order.append(n)
            atomic_write_text(path, str(n))
        return job

    for n in range(20):
        q.submit(make(n))
    q.flush()

    assert order == list(range(20)), "jobs ran out of order (not serialized)"
    assert len(worker_threads) == 1, "more than one worker -> not serialized"
    assert open(path).read() == "19"


def test_a_failing_job_does_not_break_the_queue(app, tmp_path):
    q = WriteQueue()
    path = str(tmp_path / "after.txt")
    q.submit(lambda: (_ for _ in ()).throw(RuntimeError("boom")))   # raises
    q.submit(lambda: atomic_write_text(path, "survived"))
    q.flush()
    assert open(path).read() == "survived"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
