"""术语表管理对话框：表格编辑，保存到 ~/.tram/glossary.json。"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..core import glossary as gs


class GlossaryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("术语表")
        self.resize(520, 400)
        self._build_ui()
        self._load()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["原文 (Source)", "译文 (Target)"])
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("新增一行")
        add_btn.clicked.connect(self._add_row)
        remove_btn = QPushButton("删除选中")
        remove_btn.clicked.connect(self._remove_selected)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load(self) -> None:
        entries = gs.load_glossary()
        self.table.setRowCount(len(entries))
        for i, e in enumerate(entries):
            self.table.setItem(i, 0, QTableWidgetItem(e.get("source", "")))
            self.table.setItem(i, 1, QTableWidgetItem(e.get("target", "")))

    def _add_row(self) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(""))
        self.table.setItem(row, 1, QTableWidgetItem(""))
        self.table.setCurrentCell(row, 0)

    def _remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def _collect(self) -> list[dict]:
        entries = []
        for r in range(self.table.rowCount()):
            src = (self.table.item(r, 0).text() if self.table.item(r, 0) else "").strip()
            dst = (self.table.item(r, 1).text() if self.table.item(r, 1) else "").strip()
            if src and dst:
                entries.append({"source": src, "target": dst})
        return entries

    def _on_save(self) -> None:
        gs.save_glossary(self._collect())
        self.accept()
