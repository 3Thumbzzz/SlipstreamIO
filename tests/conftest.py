"""Suite-wide isolation from the machine the tests run on.

Whether VirtualDJ or Serato is open decides whether a write asks for
confirmation first. The suite must not depend on what the developer happens to
have running: with VirtualDJ open, every test that syncs without stubbing the
dialog had its sync cancelled. So "nothing is running" is the default here, and
a test about the warning says otherwise for itself (``test_sync_to_all``
patches ``is_running`` on the adapter).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _no_dj_program_is_running(monkeypatch):
    from trackcleaner.io import vdj_process
    monkeypatch.setattr(vdj_process, "is_any_running", lambda _names: False)
