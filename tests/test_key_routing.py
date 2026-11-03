"""Transport keys (1-8, W) must reach the panel from anywhere in the window.

These tests deliver **real** key events with QTest.keyClick rather than calling
keyPressEvent directly -- calling the handler by hand is what previously hid the
fact that the views swallow these keys before the window ever sees them:

    QTreeWidget  ate both digits and "w"   (type-ahead search)
    QTableView   passed digits, ate "w"

so the routing has to happen in an application-level event filter.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                                     # noqa: E402
from PySide6.QtGui import QStandardItem, QStandardItemModel       # noqa: E402
from PySide6.QtTest import QTest                                  # noqa: E402
from PySide6.QtWidgets import (QApplication, QDialog, QLineEdit,  # noqa: E402
                               QMainWindow, QTableView, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from trackcleaner.ui.main_window import (route_transport_key,      # noqa: E402
                                         transport_keys_blocked)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _FakePanel:
    """Records what the routing decided to do."""

    def __init__(self):
        self.pressed, self.released, self.plays = [], [], 0

    def cue_pressed(self, n):
        self.pressed.append(n)

    def cue_released(self, n):
        self.released.append(n)

    def toggle_play(self):
        self.plays += 1


class _Host(QMainWindow):
    """A window using the real routing, but a stand-in panel and real views --
    so the test exercises event delivery, not audio."""

    def __init__(self):
        super().__init__()
        self.waveform_panel = _FakePanel()
        body = QWidget()
        lay = QVBoxLayout(body)
        self.tree = QTreeWidget()
        for name in ("1 intro", "breaks", "wobble"):
            self.tree.addTopLevelItem(QTreeWidgetItem([name]))
        self.table = QTableView()
        model = QStandardItemModel()
        for text in ("1 song", "2 other", "wander"):
            model.appendRow([QStandardItem(text)])
        self.table.setModel(model)
        self.edit = QLineEdit()
        for w in (self.tree, self.table, self.edit):
            lay.addWidget(w)
        self.setCentralWidget(body)
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, event):
        # exactly what MainWindow.eventFilter does
        if route_transport_key(event, self.waveform_panel):
            return True
        return super().eventFilter(obj, event)


@pytest.fixture
def host(app):
    h = _Host()
    h.show()
    app.processEvents()
    yield h
    app.instance().removeEventFilter(h)
    h.close()


@pytest.mark.parametrize("widget_name", ["tree", "table"])
def test_digits_fire_cues_from_the_selection_views(app, host, widget_name):
    widget = getattr(host, widget_name)
    widget.setFocus()
    app.processEvents()
    for key in (Qt.Key_1, Qt.Key_3, Qt.Key_8):
        QTest.keyClick(widget, key)
    app.processEvents()
    assert host.waveform_panel.pressed == [1, 3, 8], f"{widget_name} swallowed digits"
    assert host.waveform_panel.released == [1, 3, 8]


@pytest.mark.parametrize("widget_name", ["tree", "table"])
def test_w_toggles_playback_from_the_selection_views(app, host, widget_name):
    widget = getattr(host, widget_name)
    widget.setFocus()
    app.processEvents()
    QTest.keyClick(widget, Qt.Key_W)
    app.processEvents()
    assert host.waveform_panel.plays == 1, f"{widget_name} swallowed W"


def test_type_ahead_never_sees_the_transport_keys(app, host):
    """The views must not also jump their selection to a matching row."""
    host.tree.setCurrentItem(host.tree.topLevelItem(1))
    app.processEvents()
    before = host.tree.currentItem().text(0)
    QTest.keyClick(host.tree, Qt.Key_W)      # "wobble" would match type-ahead
    QTest.keyClick(host.tree, Qt.Key_1)      # "1 intro" would match too
    app.processEvents()
    assert host.tree.currentItem().text(0) == before


def test_keys_stand_down_while_typing_in_a_text_field(app, host):
    host.edit.setFocus()
    app.processEvents()
    assert transport_keys_blocked() is True
    QTest.keyClick(host.edit, Qt.Key_3)
    QTest.keyClick(host.edit, Qt.Key_W)
    app.processEvents()
    assert host.waveform_panel.pressed == []
    assert host.waveform_panel.plays == 0
    assert host.edit.text() == "3w", "the keys must still type normally"


def test_auto_repeat_is_ignored(app, host):
    """A held key repeats presses; acting on them would re-trigger the cue
    continuously and desynchronise the hold-to-preview bookkeeping."""
    host.tree.setFocus()
    app.processEvents()
    QTest.keyPress(host.tree, Qt.Key_5)
    for _ in range(4):                       # simulate the repeats
        QTest.keyEvent(QTest.Press, host.tree, Qt.Key_5)
    QTest.keyRelease(host.tree, Qt.Key_5)
    app.processEvents()
    assert host.waveform_panel.released == [5]
    assert host.waveform_panel.pressed.count(5) >= 1


def test_a_modal_dialog_owns_the_keyboard(app, host):
    """W behind a confirmation box must not start playback."""
    dlg = QDialog(host)
    dlg.setModal(True)
    dlg.show()
    app.processEvents()
    QTest.keyClick(dlg, Qt.Key_W)
    QTest.keyClick(dlg, Qt.Key_2)
    app.processEvents()
    dlg.close()
    app.processEvents()
    assert host.waveform_panel.plays == 0
    assert host.waveform_panel.pressed == []


def test_other_keys_pass_through_untouched(app, host):
    host.tree.setFocus()
    app.processEvents()
    QTest.keyClick(host.tree, Qt.Key_9)      # outside 1-8
    QTest.keyClick(host.tree, Qt.Key_A)
    app.processEvents()
    assert host.waveform_panel.pressed == []
    assert host.waveform_panel.plays == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
