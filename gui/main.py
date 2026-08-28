"""Orbit unified GUI — chat-centered task submission with live output,
glassmorphism-inspired white+blue design, foreground/headless toggle,
Gemini effort selector, and a slide-out history drawer.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from PySide6.QtCore import (
    QEasingCurve, QProcess, QPropertyAnimation, QRect, QSize, Qt, QTimer,
)
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from orbit import db
from orbit.policy import load_windows_control_policy

_VENV_PYTHON = str(Path(_PROJECT_ROOT) / "venv" / "Scripts" / "python.exe")

# -- palette ------------------------------------------------------------------

_BLUE = "#2563EB"
_BLUE_DARK = "#1D4ED8"
_BLUE_LIGHT = "#EFF6FF"
_BLUE_BORDER = "#BFDBFE"
_BLUE_50 = "#DBEAFE"
_WHITE = "#FFFFFF"
_BG = "#F8FAFC"
_TEXT = "#0F172A"
_TEXT_SEC = "#64748B"
_BORDER = "#E2E8F0"
_GREEN = "#16A34A"
_RED = "#DC2626"
_AMBER = "#D97706"
_GRAY = "#94A3B8"
_GLASS = "rgba(255,255,255,0.85)"
_GLASS_BORDER = "rgba(255,255,255,0.45)"

# -- stylesheet ---------------------------------------------------------------

STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {_BG};
    color: {_TEXT};
    font-family: "Segoe UI", sans-serif;
    font-size: 13px;
}}
QMainWindow {{
    background-color: {_WHITE};
}}

/* ---- input bar ---- */
#inputBar {{
    background: {_WHITE};
    border: 1.5px solid {_BORDER};
    border-radius: 16px;
    padding: 6px 14px;
}}
#inputBar:focus-within {{
    border-color: {_BLUE};
}}
#goalInput {{
    border: none;
    background: transparent;
    font-size: 15px;
    padding: 10px 4px;
    color: {_TEXT};
}}
#goalInput:focus {{
    border: none;
    outline: none;
}}

/* ---- toggle pills ---- */
#toggleFrame {{
    background: {_BG};
    border: 1px solid {_BORDER};
    border-radius: 10px;
    padding: 3px;
}}
.toggleBtn {{
    border: none;
    border-radius: 8px;
    padding: 6px 16px;
    font-size: 12px;
    font-weight: 600;
    color: {_TEXT_SEC};
    background: transparent;
}}
.toggleBtnActive {{
    border: none;
    border-radius: 8px;
    padding: 6px 16px;
    font-size: 12px;
    font-weight: 600;
    color: {_WHITE};
    background: {_BLUE};
}}

/* ---- effort combo ---- */
#effortCombo {{
    border: 1px solid {_BORDER};
    border-radius: 8px;
    padding: 6px 26px 6px 10px;
    background: {_WHITE};
    font-size: 12px;
    font-weight: 600;
    color: {_TEXT_SEC};
    min-width: 90px;
}}
#effortCombo::drop-down {{
    border: none;
    width: 20px;
}}
#effortCombo QAbstractItemView {{
    background: {_WHITE};
    border: 1px solid {_BORDER};
    selection-background-color: {_BLUE_LIGHT};
    selection-color: {_BLUE};
}}

/* ---- send / stop ---- */
#sendBtn {{
    background-color: {_BLUE};
    color: white;
    border: none;
    border-radius: 12px;
    padding: 10px 32px;
    font-size: 14px;
    font-weight: 700;
}}
#sendBtn:hover {{ background-color: {_BLUE_DARK}; }}
#sendBtn:pressed {{ background-color: #1E40AF; }}
#sendBtn:disabled {{ background-color: {_GRAY}; }}
#stopBtn {{
    background-color: {_RED};
    color: white;
    border: none;
    border-radius: 12px;
    padding: 10px 24px;
    font-size: 14px;
    font-weight: 700;
}}
#stopBtn:hover {{ background-color: #B91C1C; }}

/* ---- output panel ---- */
#outputPanel {{
    background: {_WHITE};
    border: 1px solid {_BORDER};
    border-radius: 14px;
}}
#outputText {{
    background: {_WHITE};
    border: none;
    font-family: "Cascadia Code", "Consolas", monospace;
    font-size: 13px;
    color: {_TEXT};
    padding: 16px;
    selection-background-color: {_BLUE_50};
}}

/* ---- section labels ---- */
#sectionLabel {{
    font-size: 11px;
    font-weight: 700;
    color: {_TEXT_SEC};
    letter-spacing: 1px;
    padding: 8px 4px 4px 4px;
}}

/* ---- task table ---- */
#taskTable {{
    background: {_WHITE};
    border: 1px solid {_BORDER};
    border-radius: 10px;
    gridline-color: {_BORDER};
    selection-background-color: {_BLUE_LIGHT};
    selection-color: {_TEXT};
}}
#taskTable::item {{ padding: 6px 10px; }}
#taskTable QHeaderView::section {{
    background: {_BG};
    border: none;
    border-bottom: 2px solid {_BORDER};
    padding: 8px 10px;
    font-weight: 600;
    font-size: 11px;
    color: {_TEXT_SEC};
    text-transform: uppercase;
}}

/* ---- confirmation panel ---- */
#confirmPanel {{
    background: {_WHITE};
    border: 1px solid {_BORDER};
    border-radius: 14px;
    padding: 14px;
}}
#confirmHeading {{ font-weight: 700; font-size: 13px; color: {_TEXT}; }}
#confirmDetail {{ color: {_TEXT_SEC}; font-size: 12px; }}
#approveBtn {{
    background-color: {_GREEN}; color: white; border: none;
    border-radius: 8px; padding: 8px 24px; font-weight: 600;
}}
#approveBtn:hover {{ background-color: #15803D; }}
#approveBtn:disabled {{ background-color: {_GRAY}; }}
#rejectBtn {{
    background-color: {_RED}; color: white; border: none;
    border-radius: 8px; padding: 8px 24px; font-weight: 600;
}}
#rejectBtn:hover {{ background-color: #B91C1C; }}
#rejectBtn:disabled {{ background-color: {_GRAY}; }}

/* ---- sidebar ---- */
#sidebar {{
    background: {_WHITE};
    border-left: 1px solid {_BORDER};
}}
#sidebarToggle {{
    border: 1px solid {_BORDER};
    border-radius: 8px;
    background: {_WHITE};
    padding: 6px 10px;
    font-size: 13px;
    color: {_TEXT_SEC};
}}
#sidebarToggle:hover {{ background: {_BG}; }}

/* ---- status bar ---- */
QStatusBar {{
    background: {_WHITE};
    border-top: 1px solid {_BORDER};
    color: {_TEXT_SEC};
    font-size: 12px;
    padding: 4px 12px;
}}
#statusDot {{ font-size: 10px; }}

/* ---- progress ---- */
#progressBar {{
    border: none; background: {_BORDER}; border-radius: 2px; max-height: 4px;
}}
#progressBar::chunk {{ background: {_BLUE}; border-radius: 2px; }}

/* ---- scrollbar ---- */
QScrollBar:vertical {{
    background: transparent; width: 8px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {_BORDER}; border-radius: 4px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {_GRAY}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""


# -- markdown to HTML ---------------------------------------------------------

def _md_to_html(text: str) -> str:
    lines = text.split("\n")
    html_parts: list[str] = []
    in_table = False
    in_list = False
    table_rows: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        img_match = re.match(r"^!\[([^\]]*)\]\((.+)\)\s*$", stripped)
        if img_match:
            if in_list:
                html_parts.append("</ul>"); in_list = False
            alt, src = img_match.group(1), img_match.group(2).replace("\\", "/")
            html_parts.append(
                f'<p style="margin:8px 0;"><img src="file:///{src}" alt="{alt}" '
                f'style="max-width:100%;border:1px solid {_BORDER};border-radius:8px;"></p>'
            )
            if alt:
                html_parts.append(
                    f'<p style="color:{_TEXT_SEC};font-size:11px;margin:0 0 8px 0;font-style:italic;">{alt}</p>'
                )
            i += 1; continue

        if stripped.startswith("screenshot_path:") or stripped.startswith("[screenshot:"):
            if in_list:
                html_parts.append("</ul>"); in_list = False
            path = re.sub(r"^(?:screenshot_path:\s*|^\[screenshot:\s*|\]$)", "", stripped).strip().rstrip("]").replace("\\", "/")
            html_parts.append(
                f'<p style="margin:8px 0;"><img src="file:///{path}" '
                f'style="max-width:100%;border:1px solid {_BORDER};border-radius:8px;"></p>'
            )
            i += 1; continue

        if re.match(r"^-{4,}$", stripped):
            if in_list:
                html_parts.append("</ul>"); in_list = False
            html_parts.append(f'<hr style="border:none;border-top:1px solid {_BORDER};margin:12px 0;">')
            i += 1; continue

        if "|" in stripped and stripped.startswith("|") and stripped.endswith("|"):
            if in_list:
                html_parts.append("</ul>"); in_list = False
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.match(r"^[-:]+$", c) for c in cells):
                i += 1; continue
            if not in_table:
                in_table = True; table_rows = []
            table_rows.append(cells)
            i += 1; continue
        elif in_table:
            html_parts.append(_build_table(table_rows)); in_table = False; table_rows = []

        m = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if m:
            if in_list:
                html_parts.append("</ul>"); in_list = False
            level = len(m.group(1))
            sizes = {1: "20px", 2: "17px", 3: "15px", 4: "13px"}
            html_parts.append(
                f'<p style="font-size:{sizes.get(level,"14px")};font-weight:700;'
                f'color:{_TEXT};margin:14px 0 6px 0;">{_inline_md(m.group(2))}</p>'
            )
            i += 1; continue

        m = re.match(r"^[-*]\s+(.+)$", stripped)
        if m:
            if not in_list:
                in_list = True
                html_parts.append(f'<ul style="margin:4px 0 4px 18px;padding:0;color:{_TEXT};">')
            html_parts.append(f'<li style="margin:3px 0;">{_inline_md(m.group(1))}</li>')
            i += 1; continue

        if in_list and not stripped:
            html_parts.append("</ul>"); in_list = False

        if not stripped:
            html_parts.append("<br>"); i += 1; continue

        html_parts.append(
            f'<p style="margin:3px 0;color:{_TEXT};line-height:1.5;">{_inline_md(stripped)}</p>'
        )
        i += 1

    if in_table:
        html_parts.append(_build_table(table_rows))
    if in_list:
        html_parts.append("</ul>")
    return "\n".join(html_parts)


def _build_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    html = f'<table style="border-collapse:collapse;width:100%;margin:8px 0;font-size:12px;">'
    for idx, row in enumerate(rows):
        tag = "th" if idx == 0 else "td"
        bg = _BLUE_LIGHT if idx == 0 else (_BG if idx % 2 == 0 else _WHITE)
        weight = "600" if idx == 0 else "400"
        color = _BLUE_DARK if idx == 0 else _TEXT
        html += "<tr>"
        for cell in row:
            html += (
                f'<{tag} style="padding:6px 10px;border-bottom:1px solid {_BORDER};'
                f'background:{bg};font-weight:{weight};color:{color};text-align:left;">'
                f'{_inline_md(cell)}</{tag}>'
            )
        html += "</tr>"
    html += "</table>"
    return html


def _inline_md(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", rf'<b style="color:{_TEXT};">\1</b>', text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(
        r"`(.+?)`",
        rf'<code style="background:{_BG};padding:1px 5px;border-radius:3px;'
        rf'font-size:12px;color:{_BLUE_DARK};">\1</code>',
        text,
    )
    return text


TASK_COLUMNS = [
    ("Title", "title"),
    ("Status", "status"),
    ("Lane", "lane"),
    ("Created", "created_at"),
    ("Failure", "failure_reason"),
]


# -- confirmation panel -------------------------------------------------------

class ConfirmationPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("confirmPanel")
        self.current_id: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        self.heading = QLabel("No pending confirmations")
        self.heading.setObjectName("confirmHeading")
        layout.addWidget(self.heading)

        self.detail = QLabel("")
        self.detail.setObjectName("confirmDetail")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

        self.shot = QLabel()
        self.shot.setAlignment(Qt.AlignCenter)
        self.shot.setMinimumHeight(140)
        layout.addWidget(self.shot, stretch=1)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.approve = QPushButton("Approve")
        self.approve.setObjectName("approveBtn")
        self.reject = QPushButton("Reject")
        self.reject.setObjectName("rejectBtn")
        self.approve.clicked.connect(lambda: self._resolve(True))
        self.reject.clicked.connect(lambda: self._resolve(False))
        buttons.addStretch()
        buttons.addWidget(self.approve)
        buttons.addWidget(self.reject)
        buttons.addStretch()
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
            + (f"   (+{extra} more)" if extra else "")
        )
        ttl = load_windows_control_policy().get("approval_token_ttl_seconds", 120)
        self.detail.setText(
            f"{row.get('candidate_label') or ''}\n"
            f"task {row['task_id']}  |  requested {row['created_at']}\n"
            f"Approving grants ONE action, usable once, for {ttl}s."
        )
        self._render_shot(row)
        self._set_enabled(True)

    def _render_shot(self, row: dict) -> None:
        path = row.get("screenshot_path")
        pixmap = QPixmap(path) if path else QPixmap()
        if pixmap.isNull():
            self.shot.setText("(no screenshot)")
            return
        box = row.get("candidate_box")
        if box:
            painter = QPainter(pixmap)
            painter.setPen(QPen(QColor(_BLUE), 3))
            left, top, right, bottom = box
            painter.drawRect(QRect(left, top, right - left, bottom - top))
            painter.end()
        self.shot.setPixmap(
            pixmap.scaled(
                self.shot.width() or 480, self.shot.height() or 140,
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
            pass
        self._set_enabled(False)
        self.refresh()


# -- toggle button helper -----------------------------------------------------

class ToggleGroup(QWidget):
    """Pill-shaped segmented toggle (Headless / Foreground)."""

    def __init__(self, options: list[str], default: int = 0) -> None:
        super().__init__()
        self.setObjectName("toggleFrame")
        self._buttons: list[QPushButton] = []
        self._selected = default
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        for idx, label in enumerate(options):
            btn = QPushButton(label)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _, i=idx: self._select(i))
            self._buttons.append(btn)
            lay.addWidget(btn)
        self._apply_styles()

    def _select(self, idx: int) -> None:
        self._selected = idx
        self._apply_styles()

    def _apply_styles(self) -> None:
        for i, btn in enumerate(self._buttons):
            btn.setProperty("class", "toggleBtnActive" if i == self._selected else "toggleBtn")
            btn.setStyleSheet(
                f"border:none;border-radius:8px;padding:6px 16px;font-size:12px;font-weight:600;"
                f"color:{'white' if i == self._selected else _TEXT_SEC};"
                f"background:{'#2563EB' if i == self._selected else 'transparent'};"
            )

    def value(self) -> str:
        return self._buttons[self._selected].text().lower()


# -- main window --------------------------------------------------------------

class OrbitWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Orbit")
        self.resize(1100, 760)
        self.setMinimumSize(720, 520)

        self.process: QProcess | None = None
        self._raw_buffer = ""
        self._goal_header = ""
        self._sidebar_visible = False

        central = QWidget()
        root_h = QHBoxLayout(central)
        root_h.setContentsMargins(0, 0, 0, 0)
        root_h.setSpacing(0)

        # ===================== MAIN COLUMN =====================
        main_col = QWidget()
        main_layout = QVBoxLayout(main_col)
        main_layout.setContentsMargins(28, 18, 28, 0)
        main_layout.setSpacing(0)

        # -- header -----------------------------------------------------------
        header = QHBoxLayout()
        header.setSpacing(12)

        logo = QLabel("Orbit")
        logo.setStyleSheet(
            f"font-size: 26px; font-weight: 800; color: {_BLUE}; "
            "letter-spacing: -0.5px; padding: 0 4px;"
        )
        header.addWidget(logo)

        tag = QLabel("Personal Task Agent")
        tag.setStyleSheet(f"font-size: 12px; color: {_TEXT_SEC}; padding-top: 8px;")
        header.addWidget(tag)
        header.addStretch()

        # sidebar toggle
        self.sidebar_btn = QPushButton("History")
        self.sidebar_btn.setObjectName("sidebarToggle")
        self.sidebar_btn.setCursor(Qt.PointingHandCursor)
        self.sidebar_btn.clicked.connect(self._toggle_sidebar)
        header.addWidget(self.sidebar_btn)

        main_layout.addLayout(header)
        main_layout.addSpacing(20)

        # -- controls row: toggle + effort ------------------------------------
        controls = QHBoxLayout()
        controls.setSpacing(14)

        self.lane_toggle = ToggleGroup(["Headless", "Foreground"], default=0)
        controls.addWidget(self.lane_toggle)

        effort_label = QLabel("Effort:")
        effort_label.setStyleSheet(f"font-size:12px;color:{_TEXT_SEC};font-weight:600;")
        controls.addWidget(effort_label)

        self.effort_combo = QComboBox()
        self.effort_combo.setObjectName("effortCombo")
        self.effort_combo.addItems(["Low", "Medium", "High"])
        self.effort_combo.setCurrentIndex(0)
        self.effort_combo.setToolTip(
            "Low: fast, cheap — simple tasks\n"
            "Medium: balanced — multi-step tasks\n"
            "High: thorough — complex research/creation"
        )
        controls.addWidget(self.effort_combo)

        controls.addStretch()
        main_layout.addLayout(controls)
        main_layout.addSpacing(14)

        # -- input bar --------------------------------------------------------
        input_frame = QFrame()
        input_frame.setObjectName("inputBar")
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 25))
        shadow.setOffset(0, 4)
        input_frame.setGraphicsEffect(shadow)

        input_layout = QHBoxLayout(input_frame)
        input_layout.setContentsMargins(12, 4, 8, 4)
        input_layout.setSpacing(10)

        self.goal_input = QLineEdit()
        self.goal_input.setObjectName("goalInput")
        self.goal_input.setPlaceholderText("What should I do?  e.g. 'open Word and fill in experiment 3'")
        self.goal_input.returnPressed.connect(self._submit_task)
        input_layout.addWidget(self.goal_input, stretch=1)

        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("sendBtn")
        self.send_btn.clicked.connect(self._submit_task)
        input_layout.addWidget(self.send_btn)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.clicked.connect(self._stop_task)
        self.stop_btn.hide()
        input_layout.addWidget(self.stop_btn)

        main_layout.addWidget(input_frame)

        # -- progress bar -----------------------------------------------------
        self.progress = QProgressBar()
        self.progress.setObjectName("progressBar")
        self.progress.setMaximum(0)
        self.progress.setFixedHeight(4)
        self.progress.hide()
        main_layout.addWidget(self.progress)

        main_layout.addSpacing(14)

        # -- output panel (THE MAIN AREA) ------------------------------------
        output_container = QWidget()
        output_container.setObjectName("outputPanel")
        output_vbox = QVBoxLayout(output_container)
        output_vbox.setContentsMargins(0, 0, 0, 0)
        output_vbox.setSpacing(0)

        output_header = QHBoxLayout()
        output_label = QLabel("LIVE OUTPUT")
        output_label.setObjectName("sectionLabel")
        output_header.addWidget(output_label)
        output_header.addStretch()
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setStyleSheet(
            f"border:none;color:{_TEXT_SEC};font-size:11px;padding:4px 8px;"
        )
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.clicked.connect(self._clear_output)
        output_header.addWidget(self.clear_btn)
        output_vbox.addLayout(output_header)

        self.output_text = QTextEdit()
        self.output_text.setObjectName("outputText")
        self.output_text.setReadOnly(True)
        output_vbox.addWidget(self.output_text)

        # -- confirmation below output ----------------------------------------
        self.confirmations = ConfirmationPanel()

        main_splitter = QSplitter(Qt.Vertical)
        main_splitter.setHandleWidth(1)
        main_splitter.addWidget(output_container)
        main_splitter.addWidget(self.confirmations)
        main_splitter.setStretchFactor(0, 4)
        main_splitter.setStretchFactor(1, 1)

        main_layout.addWidget(main_splitter, stretch=1)

        root_h.addWidget(main_col, stretch=1)

        # ===================== SIDEBAR (HISTORY) =====================
        self.sidebar = QWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(340)
        self.sidebar.setVisible(False)
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(14, 18, 14, 14)
        sidebar_layout.setSpacing(8)

        sidebar_header = QHBoxLayout()
        hist_label = QLabel("TASK HISTORY")
        hist_label.setObjectName("sectionLabel")
        sidebar_header.addWidget(hist_label)
        sidebar_header.addStretch()

        self.task_count_label = QLabel("")
        self.task_count_label.setStyleSheet(f"font-size:11px;color:{_TEXT_SEC};")
        sidebar_header.addWidget(self.task_count_label)
        sidebar_layout.addLayout(sidebar_header)

        self.table = QTableWidget(0, len(TASK_COLUMNS))
        self.table.setObjectName("taskTable")
        self.table.setHorizontalHeaderLabels([c[0] for c in TASK_COLUMNS])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setStyleSheet(
            self.table.styleSheet()
            + f"\nQTableWidget {{ alternate-background-color: {_BG}; }}"
        )
        sidebar_layout.addWidget(self.table, stretch=1)

        root_h.addWidget(self.sidebar)

        self.setCentralWidget(central)

        # -- status bar -------------------------------------------------------
        status = QStatusBar()
        self.status_dot = QLabel()
        self.status_dot.setObjectName("statusDot")
        status.addWidget(self.status_dot)
        self.status_label = QLabel("Ready")
        status.addWidget(self.status_label)
        status.addPermanentWidget(QLabel(f"Python {sys.version.split()[0]}"))
        self.setStatusBar(status)

        # -- timers -----------------------------------------------------------
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._refresh_tasks)
        self.refresh_timer.start(2000)
        self._refresh_tasks()
        self._set_status("idle")

    # -- sidebar toggle -------------------------------------------------------

    def _toggle_sidebar(self) -> None:
        self._sidebar_visible = not self._sidebar_visible
        self.sidebar.setVisible(self._sidebar_visible)
        self.sidebar_btn.setText("Hide" if self._sidebar_visible else "History")

    # -- task submission ------------------------------------------------------

    def _submit_task(self) -> None:
        goal = self.goal_input.text().strip()
        if not goal or self.process is not None:
            return

        lane = self.lane_toggle.value()
        effort = self.effort_combo.currentText().lower()

        args = ["-m", "orbit.run_task"]
        if lane == "foreground":
            args.append("--foreground")
        args.extend(goal.split())

        env = QProcess.systemEnvironment()
        env.append(f"ORBIT_EFFORT={effort}")

        self.process = QProcess(self)
        self.process.setEnvironment(env)
        self.process.setProcessChannelMode(QProcess.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.finished.connect(self._task_finished)

        self._raw_buffer = ""
        self._goal_header = f"> {goal}\n  ({lane} | effort: {effort})\n"
        self.output_text.clear()
        self._append_plain(f"> {goal}\n", _BLUE)
        self._append_plain(f"  {lane} lane  |  effort: {effort}\n\n", _TEXT_SEC)

        self.process.start(_VENV_PYTHON, args)

        self.goal_input.clear()
        self.goal_input.setEnabled(False)
        self.send_btn.hide()
        self.stop_btn.show()
        self.progress.show()
        self._set_status("running")

    def _stop_task(self) -> None:
        if self.process:
            self.process.kill()
            self._append_plain("\n[task stopped by user]\n", _RED)

    _STDERR_NOISE = (
        "IncompleteFieldDefinitionWarning",
        "warnings.warn(",
        "Processing request of type",
        "pydantic_settings",
        "DeprecationWarning",
        "UserWarning",
        "<frozen abc>",
    )

    _SCREENSHOT_RE = re.compile(
        r"""['"]?screenshot_path['"]?\s*[:=]\s*['"]?(.+?\.png)['"]?""", re.IGNORECASE
    )

    def _read_stdout(self) -> None:
        if not self.process:
            return
        data = self.process.readAllStandardOutput()
        text = bytes(data).decode("utf-8", errors="replace")
        self._raw_buffer += text
        for line in text.splitlines(keepends=True):
            m = self._SCREENSHOT_RE.search(line)
            if m:
                self._append_plain(line, _TEXT_SEC)
                self._insert_screenshot(m.group(1).strip())
            else:
                self._append_plain(line, _TEXT)

    def _insert_screenshot(self, path: str) -> None:
        p = Path(path)
        if not p.exists():
            return
        cursor = self.output_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertHtml(
            f'<br><img src="file:///{p.as_posix()}" '
            f'width="600" style="border:1px solid {_BORDER};border-radius:8px;"><br>'
        )
        self.output_text.setTextCursor(cursor)
        self.output_text.ensureCursorVisible()

    def _read_stderr(self) -> None:
        if not self.process:
            return
        data = self.process.readAllStandardError()
        text = bytes(data).decode("utf-8", errors="replace")
        for line in text.splitlines(keepends=True):
            if any(noise in line for noise in self._STDERR_NOISE):
                continue
            if line.strip():
                self._append_plain(line, _RED)

    def _task_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self.process = None
        self.goal_input.setEnabled(True)
        self.send_btn.show()
        self.stop_btn.hide()
        self.progress.hide()
        self._render_final_output(exit_code)
        if exit_code == 0:
            self._set_status("idle")
        else:
            self._set_status("error")
        self._refresh_tasks()
        self.goal_input.setFocus()

    def _render_final_output(self, exit_code: int) -> None:
        header_html = (
            f'<p style="color:{_BLUE};font-size:16px;font-weight:700;'
            f'margin:0 0 2px 0;font-family:\'Segoe UI\',sans-serif;">'
            f'{_inline_md(self._goal_header.split(chr(10))[0])}</p>'
            f'<p style="color:{_TEXT_SEC};font-size:12px;margin:0 0 14px 0;'
            f'font-family:\'Segoe UI\',sans-serif;">'
            f'{_inline_md(self._goal_header.split(chr(10))[1] if chr(10) in self._goal_header else "")}</p>'
        )

        body = self._raw_buffer.replace("\r\n", "\n")
        sections = re.split(r"\n-{4,}\n", body)

        body_html = ""
        for section in sections:
            s = section.strip()
            if not s:
                continue
            if s.startswith("> ") or s.startswith("(working"):
                continue
            if s.startswith("task_id:"):
                task_line = s.split("\n")[0]
                body_html += (
                    f'<p style="color:{_TEXT_SEC};font-size:11px;margin:12px 0 0 0;'
                    f'font-family:\'Cascadia Code\',\'Consolas\',monospace;">'
                    f'{_inline_md(task_line)}</p>'
                )
                continue
            body_html += _md_to_html(s)

        status_color = _GREEN if exit_code == 0 else _RED
        status_text = "done" if exit_code == 0 else f"exited with code {exit_code}"
        status_html = (
            f'<p style="color:{status_color};font-size:12px;font-weight:600;'
            f'margin:14px 0 0 0;">[{status_text}]</p>'
        )

        full_html = (
            f'<div style="font-family:\'Segoe UI\',sans-serif;padding:12px;">'
            f'{header_html}{body_html}{status_html}</div>'
        )
        self.output_text.setHtml(full_html)
        cursor = self.output_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        self.output_text.setTextCursor(cursor)

    # -- output helpers -------------------------------------------------------

    def _append_plain(self, text: str, color: str) -> None:
        cursor = self.output_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        fmt = cursor.charFormat()
        fmt.setForeground(QColor(color))
        cursor.setCharFormat(fmt)
        cursor.insertText(text)
        self.output_text.setTextCursor(cursor)
        self.output_text.ensureCursorVisible()

    def _clear_output(self) -> None:
        self.output_text.clear()
        self._raw_buffer = ""

    # -- task table refresh ---------------------------------------------------

    def _refresh_tasks(self) -> None:
        db.init_db()
        self.confirmations.refresh()
        tasks = db.list_tasks()
        self.task_count_label.setText(f"{len(tasks)} tasks")

        self.table.setRowCount(len(tasks))
        for row_idx, task in enumerate(tasks):
            for col_idx, (_, key) in enumerate(TASK_COLUMNS):
                value = task.get(key) or ""
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if key == "status":
                    color = {
                        "COMPLETED": _GREEN, "FAILED": _RED,
                        "CANCELLED": _GRAY, "RUNNING": _AMBER,
                    }.get(str(value), _TEXT)
                    item.setForeground(QColor(color))
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                elif key == "failure_reason" and value:
                    item.setForeground(QColor(_RED))
                elif key in ("lane", "created_at"):
                    item.setForeground(QColor(_TEXT_SEC))
                self.table.setItem(row_idx, col_idx, item)

    # -- status bar -----------------------------------------------------------

    def _set_status(self, state: str) -> None:
        dots = {"idle": f'<span style="color:{_GREEN}">●</span>',
                "running": f'<span style="color:{_AMBER}">●</span>',
                "error": f'<span style="color:{_RED}">●</span>'}
        labels = {"idle": "Ready", "running": "Task running...", "error": "Task failed"}
        self.status_dot.setText(dots.get(state, ""))
        self.status_label.setText(f"  {labels.get(state, state)}")


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    window = OrbitWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
