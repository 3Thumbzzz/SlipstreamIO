"""The Clean up results dialog: what the scan found, grouped, one choice each.

The scan itself writes nothing, so this dialog is the whole decision point. It
shows one section per problem *kind* -- what it is, how many, and a sample of
the actual renames -- with its own checkbox, because "fix the ampersands but
leave the percent signs alone" is a real answer and a single Apply button cannot
express it.

Findings that cannot be applied are shown too, greyed, with the reason. Hiding
them would turn "6 I cannot fix, and why" into a silently smaller number.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFrame,
                               QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from ..model import cleanup

#: How many renames to list per section before summarising the rest. Enough to
#: recognise the pattern, few enough that 412 findings stay one screen.
SAMPLE = 8


class CleanupDialog(QDialog):
    """Present ``findings`` and return the kinds the user chose to apply.

    ``chosen_kinds()`` is the result, valid after ``exec()`` returns Accepted.
    A section with nothing applicable can be read but not ticked.
    """

    def __init__(self, findings, scope_label: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Clean up")
        self.resize(760, 560)
        self._boxes: dict[str, QCheckBox] = {}
        self._groups = cleanup.group(findings)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(self._headline(findings, scope_label)))

        if not self._groups:
            layout.addWidget(QLabel("Nothing to clean up."))
        else:
            layout.addWidget(self._sections())

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._ok = buttons.button(QDialogButtonBox.Ok)
        self._ok.setText("Apply selected")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._sync_ok()

    # ------------------------------------------------------------ building
    def _headline(self, findings, scope_label: str) -> str:
        findings = list(findings)
        fixable = sum(1 for f in findings if f.applicable)
        if not findings:
            return f"Scanned {scope_label}. No problems found."
        return (f"Scanned {scope_label}: {len(findings)} problem"
                f"{'' if len(findings) == 1 else 's'} found, "
                f"{fixable} that can be fixed.")

    def _sections(self) -> QWidget:
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(0, 0, 0, 0)
        for kind, items in self._groups.items():
            box.addWidget(self._section(kind, items))
        box.addStretch(1)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(inner)
        return area

    def _section(self, kind: str, items: list) -> QWidget:
        detector = cleanup.DETECTORS_BY_KIND.get(kind)
        applicable = [f for f in items if f.applicable]
        blocked = [f for f in items if not f.applicable]

        frame = QFrame()
        frame.setFrameShape(QFrame.StyledPanel)
        col = QVBoxLayout(frame)

        head = QHBoxLayout()
        check = QCheckBox(detector.label if detector else kind)
        check.setChecked(bool(applicable))
        check.setEnabled(bool(applicable))
        check.toggled.connect(self._sync_ok)
        self._boxes[kind] = check
        head.addWidget(check)
        head.addStretch(1)
        head.addWidget(QLabel(f"{len(applicable)} of {len(items)} fixable"))
        col.addLayout(head)

        if detector is not None:
            note = QLabel(detector.explain)
            note.setWordWrap(True)
            note.setEnabled(False)
            col.addWidget(note)

        col.addWidget(_rename_table(
            applicable, blocked,
            relinks_only=bool(detector and detector.relinks_only)))
        return frame

    # ------------------------------------------------------------- result
    def _sync_ok(self, *_):
        self._ok.setEnabled(bool(self.chosen_kinds()))

    def chosen_kinds(self) -> list[str]:
        return [kind for kind, box in self._boxes.items()
                if box.isChecked() and box.isEnabled()]

    def chosen_findings(self) -> list:
        """Only the applicable findings of the chosen kinds -- what the caller
        renames. A blocked finding is never returned however its section is
        ticked."""
        kinds = set(self.chosen_kinds())
        return [f for kind, items in self._groups.items() if kind in kinds
                for f in items if f.applicable]


def _rename_table(applicable: list, blocked: list,
                  relinks_only: bool = False) -> QTreeWidget:
    """A sample of the renames, then every blocked finding with its reason.

    A relink between Unicode spellings shows the same text in both columns, so
    its third column names the code points that differ -- otherwise the user is
    asked to approve a change they cannot see.
    """
    tree = QTreeWidget()
    tree.setColumnCount(3)
    tree.setHeaderLabels(["Recorded as", "On disk", "Difference"] if relinks_only
                         else ["File", "Becomes", ""])
    tree.setRootIsDecorated(False)
    tree.setUniformRowHeights(True)
    tree.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    tree.setMinimumHeight(140)

    for finding in applicable[:SAMPLE]:
        note = cleanup.spelling_difference(finding) if relinks_only else ""
        skipped = cleanup.skipped_note(finding)
        item = QTreeWidgetItem(
            tree, [finding.current_name, finding.proposed_name,
                   f"{note}; {skipped}" if skipped else note])
        if skipped:
            item.setToolTip(2, skipped)
    if len(applicable) > SAMPLE:
        rest = QTreeWidgetItem(
            tree, [f"… and {len(applicable) - SAMPLE} more", "", ""])
        rest.setDisabled(True)

    for finding in blocked:
        item = QTreeWidgetItem(
            tree, [finding.current_name, finding.proposed_name, finding.blocked])
        item.setDisabled(True)
        item.setToolTip(2, finding.blocked)

    for column in range(3):
        tree.resizeColumnToContents(column)
    tree.setTextElideMode(Qt.ElideMiddle)
    return tree
