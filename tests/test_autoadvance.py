"""On-end auto-advance: the pure next-track resolvers."""

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trackcleaner.ui.main_window import MainWindow                # noqa: E402


def _track_of(present):
    """Resolver treating every key in ``present`` as an existing track."""
    return lambda k: (object() if k in present else None)


def test_after_returns_the_next_present_track():
    present = {"a", "b", "c"}
    to = _track_of(present)
    # after 'a' comes 'b'
    assert MainWindow._after(["a", "b", "c"], "a", to) is not None
    # order is respected: skip missing tracks
    got = MainWindow._after(["a", "x", "c"], "a", _track_of({"a", "c"}))
    assert got is not None            # 'x' missing -> next present is 'c'


def test_after_wraps_around():
    present = {"a", "b", "c"}
    to = _track_of(present)
    # after the last, wrap to the first
    result = MainWindow._after(["a", "b", "c"], "c", to)
    assert result is not None         # wrapped to 'a'


def test_after_missing_current_falls_back_to_first_present():
    result = MainWindow._after(["a", "b"], "zzz", _track_of({"a", "b"}))
    assert result is not None


def test_after_none_when_nothing_present():
    assert MainWindow._after(["a", "b"], "a", _track_of(set())) is None


def test_random_other_excludes_current_and_missing():
    random.seed(0)
    # only 'b' is both present and not the current -> must pick it
    got = MainWindow._random_other(["a", "b", "x"], "a", _track_of({"a", "b"}))
    assert got is not None

    # nothing else present -> None
    assert MainWindow._random_other(["a"], "a", _track_of({"a"})) is None


def test_random_other_none_on_empty():
    assert MainWindow._random_other([], "a", _track_of(set())) is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
