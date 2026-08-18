"""Minimal GUI shell — Section 12/13's open GUI-framework decision, resolved
as PySide6 for this build: single-language (matches the Python backend
directly, no Tauri/IPC bridge to stand up), fastest path to something real.
Tauri remains the better end-state per the tech-stack review once there's
time to build the bridge — revisit then.

Per Section 3: "GUI Dashboard — window into runtime state only." This does
not drive the agent; it only reads orbit.db and displays it. Auto-refreshes
every 2s so it stays live while tasks run from run_task.py / eval/run_eval.py
in another process.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Running this as a plain script (`python gui/main.py`) puts gui/ on
# sys.path rather than the project root, so `from orbit import db` below
# would fail with ModuleNotFoundError. Add the project root explicitly so
# both `python gui/main.py` and `python -m gui.main` work.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QHeaderView,
    QMainWindow,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from orbit import db

COLUMNS = ["task_id", "title", "status", "lane", "risk_tier", "created_at", "failure_reason"]


class OrbitDashboard(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Orbit — Task Dashboard")
        self.resize(1000, 500)

        central = QWidget()
        layout = QVBoxLayout(central)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)

        self.setCentralWidget(central)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

    def refresh(self) -> None:
        db.init_db()
        tasks = db.list_tasks()
        self.table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            for col, key in enumerate(COLUMNS):
                value = task.get(key) or ""
                item = QTableWidgetItem(str(value))
                if key == "status":
                    item.setForeground(_status_color(value))
                self.table.setItem(row, col, item)


def _status_color(status: str):
    from PySide6.QtGui import QColor

    return {
        "COMPLETED": QColor("green"),
        "FAILED": QColor("red"),
        "CANCELLED": QColor("gray"),
        "RUNNING": QColor("#cc8800"),
    }.get(status, QColor("black"))


def main() -> int:
    app = QApplication(sys.argv)
    window = OrbitDashboard()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
