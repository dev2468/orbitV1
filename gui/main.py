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

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from orbit import db
from orbit.policy import load_windows_control_policy

COLUMNS = ["task_id", "title", "status", "lane", "risk_tier", "created_at", "failure_reason"]


class ConfirmationPanel(QWidget):
    """The approval channel gui/CLAUDE.md said belonged here.

    Renders the OLDEST pending confirmation — its screenshot with the
    candidate box drawn over it — and offers Approve / Reject.

    WHY WRITING HERE IS ALLOWED, WHEN CANCELLATION STILL IS NOT.
    The standing rule is that this process must not write to `orbit.db`,
    because task status is owned by `TaskManager`'s in-memory registry and a
    direct write would desync the two. `pending_confirmations` has no such
    in-memory owner: the waiting process is *polling this table* for an
    answer, so the table IS the channel. These buttons therefore call
    `db.resolve_pending_confirmation` and nothing else — no task rows, no
    event rows, no status changes. That restriction is the whole reason this
    is safe.

    Oldest-first so the queue is answered in the order the human was asked.
    """

    def __init__(self) -> None:
        super().__init__()
        self.current_id: str | None = None

        layout = QVBoxLayout(self)
        self.heading = QLabel("No pending confirmations")
        self.heading.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.heading)

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

        self.shot = QLabel()
        self.shot.setAlignment(Qt.AlignCenter)
        self.shot.setMinimumHeight(220)
        layout.addWidget(self.shot, stretch=1)

        buttons = QHBoxLayout()
        self.approve = QPushButton("Approve")
        self.reject = QPushButton("Reject")
        self.approve.clicked.connect(lambda: self._resolve(True))
        self.reject.clicked.connect(lambda: self._resolve(False))
        buttons.addWidget(self.approve)
        buttons.addWidget(self.reject)
        layout.addLayout(buttons)

        self._set_enabled(False)

    def _set_enabled(self, on: bool) -> None:
        self.approve.setEnabled(on)
        self.reject.setEnabled(on)

    def refresh(self) -> None:
        pending = db.list_pending_confirmations()
        if not pending:
            self.current_id = None
            self.heading.setText("No pending confirmations")
            self.detail.setText("")
            self.shot.clear()
            self._set_enabled(False)
            return

        row = pending[0]
        self.current_id = row["confirmation_id"]
        extra = len(pending) - 1
        self.heading.setText(
            f"Approval needed: {row['action']}"
            + (f"   (+{extra} more waiting)" if extra else "")
        )
        ttl = load_windows_control_policy().get("approval_token_ttl_seconds", 120)
        self.detail.setText(
            f"{row.get('candidate_label') or ''}\n"
            f"task {row['task_id']} · requested {row['created_at']}\n"
            f"Approving grants ONE action, usable once, for {ttl}s. It does not "
            f"raise the target's confidence or lower the actuation floor."
        )
        self._render_shot(row)
        self._set_enabled(True)

    def _render_shot(self, row: dict) -> None:
        """Draw the candidate box over the stored screenshot.

        The box is the whole point: a human approving a *visual guess* has to
        see which thing was guessed. With no screenshot on disk the panel says
        so rather than showing a blank frame that could be mistaken for
        "nothing selected"."""
        path = row.get("screenshot_path")
        pixmap = QPixmap(path) if path else QPixmap()
        if pixmap.isNull():
            self.shot.setText(
                "(no screenshot stored for this confirmation — approve only if "
                "the description above is enough to judge)"
            )
            return

        box = row.get("candidate_box")
        if box:
            painter = QPainter(pixmap)
            painter.setPen(QPen(QColor(255, 32, 96), 3))
            left, top, right, bottom = box
            painter.drawRect(QRect(left, top, right - left, bottom - top))
            painter.end()
        self.shot.setPixmap(
            pixmap.scaled(
                self.shot.width() or 480, self.shot.height() or 220,
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
        )

    def _resolve(self, approved: bool) -> None:
        if not self.current_id:
            return
        try:
            db.resolve_pending_confirmation(
                self.current_id,
                approved=approved,
                ttl_seconds=int(
                    load_windows_control_policy().get("approval_token_ttl_seconds", 120)
                ),
            )
        except KeyError:
            # Already decided elsewhere (the REPL asker, or a second window).
            # Not an error worth a dialog — refreshing shows the truth.
            pass
        self._set_enabled(False)
        self.refresh()


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

        self.confirmations = ConfirmationPanel()
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self.confirmations)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)

        self.setCentralWidget(central)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

    def refresh(self) -> None:
        db.init_db()
        self.confirmations.refresh()
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
