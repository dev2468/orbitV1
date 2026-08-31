"""Orbit unified GUI — chat-centered task submission with live output,
sleek light-mode design, Indigo accents, foreground/headless toggle,
Gemini effort selector, full-window task history inspector, and slide-out approvals.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from PySide6.QtCore import (
    QEasingCurve,
    QProcess,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QCursor,
    QGuiApplication,
)
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
from gui.step_tracker import StepTracker, StepStatus
from gui.history_view import TaskHistoryView
from gui.voice import HotkeyFilter, OrbWidget, VoiceController

_VENV_PYTHON = str(Path(_PROJECT_ROOT) / "venv" / "Scripts" / "python.exe")

# -- palette (Modern Sleek Light Mode) ----------------------------------------

_INDIGO = "#4F46E5"
_INDIGO_DARK = "#4338CA"
_INDIGO_LIGHT = "#EEF2FF"
_INDIGO_BORDER = "#C7D2FE"
_INDIGO_50 = "#E0E7FF"
_INDIGO_TEXT = "#3730A3"

_WHITE = "#FFFFFF"
_BG = "#F7F8FA"
_BG_CARD = "#FFFFFF"
_TEXT = "#111827"
_TEXT_SEC = "#6B7280"
_TEXT_MUTED = "#9CA3AF"
_BORDER = "#E5E7EB"
_BORDER_LIGHT = "#F3F4F6"

_GREEN = "#10B981"
_GREEN_LIGHT = "#ECFDF5"
_GREEN_BORDER = "#A7F3D0"
_GREEN_TEXT = "#047857"

_RED = "#EF4444"
_RED_LIGHT = "#FEF2F2"
_RED_BORDER = "#FECACA"
_RED_TEXT = "#B91C1C"

_AMBER = "#F59E0B"
_AMBER_LIGHT = "#FFFBEB"
_AMBER_BORDER = "#FDE68A"
_AMBER_TEXT = "#B45309"

_GRAY = "#6B7280"
_GRAY_LIGHT = "#F3F4F6"
_GRAY_BORDER = "#E5E7EB"

# -- stylesheet ---------------------------------------------------------------

STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {_BG};
    color: {_TEXT};
    font-family: "Segoe UI Variable", "Segoe UI", "Inter", -apple-system, sans-serif;
    font-size: 13px;
}}
QMainWindow {{
    background-color: {_BG};
}}

/* ---- header brand & buttons ---- */
#appLogo {{
    font-size: 24px;
    font-weight: 800;
    color: {_INDIGO};
    letter-spacing: -0.6px;
}}
#appTagline {{
    font-size: 11px;
    font-weight: 600;
    color: {_TEXT_SEC};
    background: {_WHITE};
    border: 1px solid {_BORDER};
    border-radius: 6px;
    padding: 3px 8px;
}}

/* ---- Navigation Switcher ---- */
#navSwitcher {{
    background: {_GRAY_LIGHT};
    border: 1px solid {_BORDER};
    border-radius: 10px;
    padding: 3px;
}}
.navTabBtn {{
    border: none;
    border-radius: 8px;
    padding: 6px 16px;
    font-size: 12px;
    font-weight: 600;
    color: {_TEXT_SEC};
    background: transparent;
}}
.navTabBtn:hover {{
    color: {_TEXT};
}}
.navTabBtnActive {{
    border: none;
    border-radius: 8px;
    padding: 6px 16px;
    font-size: 12px;
    font-weight: 700;
    color: {_WHITE};
    background: {_INDIGO};
}}

/* ---- Alert & Aux Buttons ---- */
.navBtn {{
    border: 1px solid {_BORDER};
    border-radius: 16px;
    background: {_WHITE};
    padding: 5px 14px;
    font-size: 12px;
    font-weight: 600;
    color: {_TEXT_SEC};
}}
.navBtn:hover {{
    background: {_BG};
    color: {_TEXT};
    border-color: {_TEXT_SEC};
}}
.navBtnActive {{
    border: 1px solid {_INDIGO_BORDER};
    border-radius: 16px;
    background: {_INDIGO_LIGHT};
    padding: 5px 14px;
    font-size: 12px;
    font-weight: 700;
    color: {_INDIGO};
}}
.navBtnAlert {{
    border: 1px solid {_AMBER_BORDER};
    border-radius: 16px;
    background: {_AMBER_LIGHT};
    padding: 5px 14px;
    font-size: 12px;
    font-weight: 700;
    color: {_AMBER_TEXT};
}}
.navBtnAlert:hover {{
    background: #FEF3C7;
}}

/* ---- input card ---- */
#inputCard {{
    background: {_WHITE};
    border: 1.5px solid {_BORDER};
    border-radius: 14px;
    padding: 4px 8px 4px 14px;
}}
#inputCard:focus-within {{
    border-color: {_INDIGO};
}}
#goalInput {{
    border: none;
    background: transparent;
    font-size: 14px;
    color: {_TEXT};
    padding: 8px 4px;
}}
#goalInput:focus {{
    border: none;
    outline: none;
}}

/* ---- toggle pills ---- */
#toggleFrame {{
    background: {_GRAY_LIGHT};
    border: 1px solid {_BORDER};
    border-radius: 10px;
    padding: 3px;
}}
.toggleBtn {{
    border: none;
    border-radius: 7px;
    padding: 5px 14px;
    font-size: 12px;
    font-weight: 600;
    color: {_TEXT_SEC};
    background: transparent;
}}
.toggleBtn:hover {{
    color: {_TEXT};
}}
.toggleBtnActive {{
    border: none;
    border-radius: 7px;
    padding: 5px 14px;
    font-size: 12px;
    font-weight: 700;
    color: {_WHITE};
    background: {_INDIGO};
}}

/* ---- effort combo ---- */
#effortCombo {{
    border: 1px solid {_BORDER};
    border-radius: 8px;
    padding: 5px 24px 5px 10px;
    background: {_WHITE};
    font-size: 12px;
    font-weight: 600;
    color: {_TEXT_SEC};
    min-width: 85px;
}}
#effortCombo::drop-down {{
    border: none;
    width: 20px;
}}
#effortCombo QAbstractItemView {{
    background: {_WHITE};
    border: 1px solid {_BORDER};
    selection-background-color: {_INDIGO_LIGHT};
    selection-color: {_INDIGO};
}}

/* ---- send / stop buttons ---- */
#sendBtn {{
    background-color: {_INDIGO};
    color: white;
    border: none;
    border-radius: 10px;
    padding: 8px 24px;
    font-size: 13px;
    font-weight: 700;
}}
#sendBtn:hover {{ background-color: {_INDIGO_DARK}; }}
#sendBtn:pressed {{ background-color: #3730A3; }}
#sendBtn:disabled {{ background-color: {_TEXT_MUTED}; }}

#stopBtn {{
    background-color: {_RED};
    color: white;
    border: none;
    border-radius: 10px;
    padding: 8px 20px;
    font-size: 13px;
    font-weight: 700;
}}
#stopBtn:hover {{ background-color: #DC2626; }}

#micBtn {{
    background-color: {_SURFACE};
    border: 1.5px solid {_BORDER};
    border-radius: 18px;
    font-size: 16px;
    padding: 0px;
}}
#micBtn:hover {{ background-color: {_INDIGO_LIGHT}; border-color: {_INDIGO}; }}
#micBtn:pressed {{ background-color: {_INDIGO}; color: white; }}

/* ---- floating approval banner ---- */
#approvalBanner {{
    background: {_AMBER_LIGHT};
    border: 1px solid {_AMBER_BORDER};
    border-radius: 12px;
    padding: 10px 16px;
}}
#approvalBannerText {{
    font-weight: 700;
    font-size: 13px;
    color: {_AMBER_TEXT};
}}
#bannerReviewBtn {{
    background: {_AMBER};
    color: white;
    font-weight: 700;
    border: none;
    border-radius: 6px;
    padding: 5px 14px;
    font-size: 12px;
}}
#bannerReviewBtn:hover {{
    background: #D97706;
}}

/* ---- output workbench panel ---- */
#outputWorkbench {{
    background: {_WHITE};
    border: 1px solid {_BORDER};
    border-radius: 14px;
}}
#workbenchHeader {{
    background: {_WHITE};
    border-bottom: 1px solid {_BORDER_LIGHT};
    border-top-left-radius: 14px;
    border-top-right-radius: 14px;
    padding: 8px 16px;
}}
#outputText {{
    background: {_WHITE};
    border: none;
    border-bottom-left-radius: 14px;
    border-bottom-right-radius: 14px;
    font-family: "Cascadia Code", "Consolas", "JetBrains Mono", monospace;
    font-size: 13px;
    color: {_TEXT};
    padding: 16px;
    selection-background-color: {_INDIGO_50};
}}
.toolBtn {{
    border: 1px solid {_BORDER};
    border-radius: 6px;
    background: {_WHITE};
    padding: 4px 10px;
    font-size: 11px;
    font-weight: 600;
    color: {_TEXT_SEC};
}}
.toolBtn:hover {{
    background: {_BG};
    color: {_TEXT};
}}

/* ---- drawer & approvals card ---- */
#drawerContainer {{
    background: {_WHITE};
    border-left: 1px solid {_BORDER};
}}
#drawerHeader {{
    border-bottom: 1px solid {_BORDER_LIGHT};
    padding: 14px 18px;
}}
#drawerTitle {{
    font-size: 15px;
    font-weight: 700;
    color: {_TEXT};
}}
#drawerCloseBtn {{
    border: none;
    background: transparent;
    color: {_TEXT_SEC};
    font-size: 16px;
    font-weight: 700;
    padding: 2px 6px;
}}
#drawerCloseBtn:hover {{
    color: {_RED};
}}
#confirmDrawerCard {{
    background: {_WHITE};
    border: 1px solid {_BORDER};
    border-radius: 12px;
    padding: 14px;
}}
#approveBtn {{
    background-color: {_GREEN}; color: white; border: none;
    border-radius: 8px; padding: 9px 20px; font-weight: 700; font-size: 13px;
}}
#approveBtn:hover {{ background-color: #059669; }}
#approveBtn:disabled {{ background-color: {_TEXT_MUTED}; }}
#rejectBtn {{
    background-color: {_RED}; color: white; border: none;
    border-radius: 8px; padding: 9px 20px; font-weight: 700; font-size: 13px;
}}
#rejectBtn:hover {{ background-color: #DC2626; }}
#rejectBtn:disabled {{ background-color: {_TEXT_MUTED}; }}

/* ---- status bar ---- */
QStatusBar {{
    background: {_WHITE};
    border-top: 1px solid {_BORDER};
    color: {_TEXT_SEC};
    font-size: 12px;
    padding: 4px 12px;
}}
#statusDot {{ font-size: 10px; }}

/* ---- progress bar ---- */
#progressBar {{
    border: none; background: {_BORDER}; border-radius: 2px; max-height: 3px;
}}
#progressBar::chunk {{ background: {_INDIGO}; border-radius: 2px; }}

/* ---- scrollbar ---- */
QScrollBar:vertical {{
    background: transparent; width: 8px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {_BORDER}; border-radius: 4px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {_TEXT_MUTED}; }}
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
                f'<div style="margin:12px 0;"><img src="file:///{src}" alt="{alt}" '
                f'style="max-width:100%;border:1px solid {_BORDER};border-radius:10px;box-shadow: 0 4px 6px -1px rgba(0,0,0,0.06);">'
            )
            if alt:
                html_parts.append(
                    f'<p style="color:{_TEXT_SEC};font-size:11px;margin:4px 0 12px 0;font-style:italic;">{alt}</p>'
                )
            html_parts.append('</div>')
            i += 1; continue

        if stripped.startswith("screenshot_path:") or stripped.startswith("[screenshot:"):
            if in_list:
                html_parts.append("</ul>"); in_list = False
            path = re.sub(r"^(?:screenshot_path:\s*|^\[screenshot:\s*|\]$)", "", stripped).strip().rstrip("]").replace("\\", "/")
            html_parts.append(
                f'<div style="margin:12px 0;"><img src="file:///{path}" '
                f'style="max-width:100%;border:1px solid {_BORDER};border-radius:10px;box-shadow: 0 4px 6px -1px rgba(0,0,0,0.06);"></div>'
            )
            i += 1; continue

        if re.match(r"^-{4,}$", stripped):
            if in_list:
                html_parts.append("</ul>"); in_list = False
            html_parts.append(f'<hr style="border:none;border-top:1px solid {_BORDER_LIGHT};margin:14px 0;">')
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
            sizes = {1: "18px", 2: "16px", 3: "14px", 4: "13px"}
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
            f'<p style="margin:4px 0;color:{_TEXT};line-height:1.6;">{_inline_md(stripped)}</p>'
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
    html = f'<table style="border-collapse:collapse;width:100%;margin:10px 0;font-size:12px;border: 1px solid {_BORDER};border-radius:8px;">'
    for idx, row in enumerate(rows):
        tag = "th" if idx == 0 else "td"
        bg = _INDIGO_LIGHT if idx == 0 else (_BG if idx % 2 == 0 else _WHITE)
        weight = "700" if idx == 0 else "400"
        color = _INDIGO_TEXT if idx == 0 else _TEXT
        html += "<tr>"
        for cell in row:
            html += (
                f'<{tag} style="padding:8px 12px;border-bottom:1px solid {_BORDER};'
                f'background:{bg};font-weight:{weight};color:{color};text-align:left;">'
                f'{_inline_md(cell)}</{tag}>'
            )
        html += "</tr>"
    html += "</table>"
    return html


def _inline_md(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", rf'<b style="color:{_TEXT};font-weight:700;">\1</b>', text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(
        r"`(.+?)`",
        rf'<code style="background:{_BG};padding:2px 6px;border-radius:4px;border:1px solid {_BORDER};'
        rf'font-size:12px;color:{_INDIGO_TEXT};font-weight:600;">\1</code>',
        text,
    )
    return text


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
            if i == self._selected:
                btn.setStyleSheet(
                    f"border:none;border-radius:7px;padding:5px 14px;font-size:12px;font-weight:700;"
                    f"color:{_WHITE};background:{_INDIGO};"
                )
            else:
                btn.setStyleSheet(
                    f"border:none;border-radius:7px;padding:5px 14px;font-size:12px;font-weight:600;"
                    f"color:{_TEXT_SEC};background:transparent;"
                )

    def value(self) -> str:
        return self._buttons[self._selected].text().lower()

    def set_value(self, val: str) -> None:
        for i, b in enumerate(self._buttons):
            if b.text().lower() == val.lower():
                self._select(i)
                break


# -- main window --------------------------------------------------------------

class OrbitWindow(QMainWindow):
    _STEP_RE = re.compile(
        r"^\[STEP:(START|DONE|FAIL|PROGRESS)\]\s*(.+?)(?:\s*[—\-–]\s*(.+))?$",
        re.UNICODE,
    )
    _TOOL_CALL_RE = re.compile(
        r"""(?:^|[\s\[])tool_call:\s*([a-zA-Z0-9_]+)""",
        re.IGNORECASE,
    )
    _SCREENSHOT_RE = re.compile(
        r"""['"]?screenshot_path['"]?\s*[:=]\s*['"]?(.+?\.png)['"]?""", re.IGNORECASE
    )
    _STDERR_NOISE = (
        "IncompleteFieldDefinitionWarning",
        "warnings.warn(",
        "Processing request of type",
        "pydantic_settings",
        "DeprecationWarning",
        "UserWarning",
        "<frozen abc>",
    )

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Orbit — Personal Task Agent")
        self.resize(1180, 800)
        self.setMinimumSize(880, 600)

        self._worker: QProcess | None = None      # persistent warm worker
        self._task_running: bool = False           # True while a task is in flight
        self._voice_ctrl: VoiceController | None = None
        self._raw_buffer = ""
        self._goal_header = ""
        self._auto_scroll = True
        self._drawer_open = False
        self._current_confirm_id: str | None = None

        central = QWidget()
        root_h = QHBoxLayout(central)
        root_h.setContentsMargins(0, 0, 0, 0)
        root_h.setSpacing(0)

        # ===================== MAIN CONTENT COLUMN =====================
        main_col = QWidget()
        main_layout = QVBoxLayout(main_col)
        main_layout.setContentsMargins(28, 18, 28, 14)
        main_layout.setSpacing(12)

        # -- Top Navigation & Brand Header ------------------------------------
        nav_bar = QHBoxLayout()
        nav_bar.setSpacing(14)

        logo = QLabel("Orbit")
        logo.setObjectName("appLogo")
        nav_bar.addWidget(logo)

        tagline = QLabel("Personal Task Agent")
        tagline.setObjectName("appTagline")
        nav_bar.addWidget(tagline)

        nav_bar.addSpacing(10)

        # Main Navigation Switcher (Workbench vs History)
        self.nav_switcher = QFrame()
        self.nav_switcher.setObjectName("navSwitcher")
        ns_lay = QHBoxLayout(self.nav_switcher)
        ns_lay.setContentsMargins(3, 3, 3, 3)
        ns_lay.setSpacing(2)

        self.tab_workbench_btn = QPushButton("⚡ Workbench")
        self.tab_workbench_btn.setProperty("class", "navTabBtnActive")
        self.tab_workbench_btn.setCursor(Qt.PointingHandCursor)
        self.tab_workbench_btn.clicked.connect(self._show_workbench)
        ns_lay.addWidget(self.tab_workbench_btn)

        self.tab_history_btn = QPushButton("📜 History & Analytics")
        self.tab_history_btn.setProperty("class", "navTabBtn")
        self.tab_history_btn.setCursor(Qt.PointingHandCursor)
        self.tab_history_btn.clicked.connect(self._show_history)
        ns_lay.addWidget(self.tab_history_btn)

        nav_bar.addWidget(self.nav_switcher)

        nav_bar.addStretch()

        # Approvals toggle button with counter badge
        self.approvals_btn = QPushButton("Approvals (0)")
        self.approvals_btn.setProperty("class", "navBtn")
        self.approvals_btn.setCursor(Qt.PointingHandCursor)
        self.approvals_btn.clicked.connect(self._toggle_approvals)
        nav_bar.addWidget(self.approvals_btn)

        main_layout.addLayout(nav_bar)

        # ===================== MAIN PAGES (STACKED) =====================
        self.main_stack = QStackedWidget()

        # ----------------- PAGE 0: WORKBENCH VIEW -----------------
        workbench_page = QWidget()
        wb_page_lay = QVBoxLayout(workbench_page)
        wb_page_lay.setContentsMargins(0, 4, 0, 0)
        wb_page_lay.setSpacing(12)

        # Controls Row: Lane toggle + Effort
        controls_row = QHBoxLayout()
        controls_row.setSpacing(14)

        self.lane_toggle = ToggleGroup(["Headless", "Foreground"], default=0)
        controls_row.addWidget(self.lane_toggle)

        effort_label = QLabel("Effort:")
        effort_label.setStyleSheet(f"font-size:12px;color:{_TEXT_SEC};font-weight:600;")
        controls_row.addWidget(effort_label)

        self.effort_combo = QComboBox()
        self.effort_combo.setObjectName("effortCombo")
        self.effort_combo.addItems(["Low", "Medium", "High"])
        self.effort_combo.setCurrentIndex(0)
        self.effort_combo.setToolTip(
            "Low: fast, cheap — simple tasks\n"
            "Medium: balanced — multi-step tasks\n"
            "High: thorough — complex research/creation"
        )
        controls_row.addWidget(self.effort_combo)
        controls_row.addStretch()
        wb_page_lay.addLayout(controls_row)

        # Input Card
        input_card = QFrame()
        input_card.setObjectName("inputCard")
        input_shadow = QGraphicsDropShadowEffect()
        input_shadow.setBlurRadius(16)
        input_shadow.setColor(QColor(0, 0, 0, 10))
        input_shadow.setOffset(0, 3)
        input_card.setGraphicsEffect(input_shadow)

        input_layout = QHBoxLayout(input_card)
        input_layout.setContentsMargins(8, 4, 4, 4)
        input_layout.setSpacing(10)

        self.goal_input = QLineEdit()
        self.goal_input.setObjectName("goalInput")
        self.goal_input.setPlaceholderText("What should I do?  e.g. 'Search for mechanical keyboards under ₹5,000 and summarize'")
        self.goal_input.returnPressed.connect(self._submit_task)
        input_layout.addWidget(self.goal_input, stretch=1)

        self.mic_btn = QPushButton("🎙")
        self.mic_btn.setObjectName("micBtn")
        self.mic_btn.setToolTip("Voice input (F9)")
        self.mic_btn.setCursor(Qt.PointingHandCursor)
        self.mic_btn.setFixedSize(36, 36)
        self.mic_btn.clicked.connect(self._toggle_voice)
        input_layout.addWidget(self.mic_btn)

        self.send_btn = QPushButton("Send ↵")
        self.send_btn.setObjectName("sendBtn")
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.clicked.connect(self._submit_task)
        input_layout.addWidget(self.send_btn)

        self.stop_btn = QPushButton("Stop ■")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setCursor(Qt.PointingHandCursor)
        self.stop_btn.clicked.connect(self._stop_task)
        self.stop_btn.hide()
        input_layout.addWidget(self.stop_btn)

        wb_page_lay.addWidget(input_card)

        # Voice overlay — shown while F9 session is active
        self._voice_panel = QFrame()
        self._voice_panel.setObjectName("voicePanel")
        self._voice_panel.hide()
        vp_lay = QVBoxLayout(self._voice_panel)
        vp_lay.setContentsMargins(0, 8, 0, 4)
        vp_lay.setSpacing(6)
        vp_lay.setAlignment(Qt.AlignHCenter)

        self._orb = OrbWidget()
        vp_lay.addWidget(self._orb, alignment=Qt.AlignHCenter)

        self._voice_status_lbl = QLabel("Listening… (F9 to stop)")
        self._voice_status_lbl.setAlignment(Qt.AlignCenter)
        self._voice_status_lbl.setStyleSheet("color:#6366f1;font-size:12px;font-weight:600;")
        vp_lay.addWidget(self._voice_status_lbl)

        self._transcript_lbl = QLabel("")
        self._transcript_lbl.setWordWrap(True)
        self._transcript_lbl.setAlignment(Qt.AlignCenter)
        self._transcript_lbl.setStyleSheet("color:#374151;font-size:13px;font-style:italic;")
        vp_lay.addWidget(self._transcript_lbl)

        wb_page_lay.addWidget(self._voice_panel)

        # Progress Bar
        self.progress = QProgressBar()
        self.progress.setObjectName("progressBar")
        self.progress.setMaximum(0)
        self.progress.setFixedHeight(3)
        self.progress.hide()
        wb_page_lay.addWidget(self.progress)

        # Floating Approval Alert Banner
        self.approval_banner = QFrame()
        self.approval_banner.setObjectName("approvalBanner")
        banner_lay = QHBoxLayout(self.approval_banner)
        banner_lay.setContentsMargins(14, 8, 14, 8)
        banner_lay.setSpacing(12)

        self.banner_text = QLabel("⚠️ Action requires confirmation")
        self.banner_text.setObjectName("approvalBannerText")
        banner_lay.addWidget(self.banner_text, stretch=1)

        self.banner_review_btn = QPushButton("Review & Resolve →")
        self.banner_review_btn.setObjectName("bannerReviewBtn")
        self.banner_review_btn.setCursor(Qt.PointingHandCursor)
        self.banner_review_btn.clicked.connect(self._open_approvals_drawer)
        banner_lay.addWidget(self.banner_review_btn)

        self.approval_banner.hide()
        wb_page_lay.addWidget(self.approval_banner)

        # Step Progression Component
        self.step_tracker = StepTracker()
        wb_page_lay.addWidget(self.step_tracker)

        # Revamped Live Output Workbench
        workbench = QFrame()
        workbench.setObjectName("outputWorkbench")
        wb_shadow = QGraphicsDropShadowEffect()
        wb_shadow.setBlurRadius(16)
        wb_shadow.setColor(QColor(0, 0, 0, 8))
        wb_shadow.setOffset(0, 3)
        workbench.setGraphicsEffect(wb_shadow)

        wb_layout = QVBoxLayout(workbench)
        wb_layout.setContentsMargins(0, 0, 0, 0)
        wb_layout.setSpacing(0)

        # Workbench Toolbar
        toolbar = QFrame()
        toolbar.setObjectName("workbenchHeader")
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(16, 10, 16, 10)
        tb_layout.setSpacing(10)

        self.wb_status_dot = QLabel("●")
        self.wb_status_dot.setStyleSheet(f"color:{_INDIGO};font-size:12px;")
        tb_layout.addWidget(self.wb_status_dot)

        self.wb_title = QLabel("LIVE EXECUTION STREAM")
        self.wb_title.setStyleSheet(f"font-size:11px;font-weight:800;color:{_TEXT};letter-spacing:0.6px;")
        tb_layout.addWidget(self.wb_title)

        self.wb_lane_badge = QLabel("HEADLESS")
        self.wb_lane_badge.setStyleSheet(
            f"font-size:10px;font-weight:700;color:{_TEXT_SEC};background:{_BG};"
            f"border:1px solid {_BORDER};border-radius:4px;padding:2px 6px;"
        )
        tb_layout.addWidget(self.wb_lane_badge)

        tb_layout.addStretch()

        self.autoscroll_btn = QPushButton("Auto-scroll: ON")
        self.autoscroll_btn.setProperty("class", "toolBtn")
        self.autoscroll_btn.setCursor(Qt.PointingHandCursor)
        self.autoscroll_btn.clicked.connect(self._toggle_autoscroll)
        tb_layout.addWidget(self.autoscroll_btn)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setProperty("class", "toolBtn")
        self.copy_btn.setCursor(Qt.PointingHandCursor)
        self.copy_btn.clicked.connect(self._copy_output)
        tb_layout.addWidget(self.copy_btn)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setProperty("class", "toolBtn")
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.clicked.connect(self._clear_output)
        tb_layout.addWidget(self.clear_btn)

        wb_layout.addWidget(toolbar)

        # Output Stack (Empty vs Active)
        self.output_stack = QStackedWidget()

        # Page 0: Empty State
        empty_widget = QWidget()
        empty_lay = QVBoxLayout(empty_widget)
        empty_lay.setAlignment(Qt.AlignCenter)
        empty_lay.setContentsMargins(40, 40, 40, 40)
        empty_lay.setSpacing(12)

        empty_icon = QLabel("🪐")
        empty_icon.setStyleSheet("font-size: 40px; background: transparent;")
        empty_icon.setAlignment(Qt.AlignCenter)
        empty_lay.addWidget(empty_icon)

        empty_title = QLabel("Ready for your next task")
        empty_title.setStyleSheet(f"font-size: 17px; font-weight: 800; color: {_TEXT};")
        empty_title.setAlignment(Qt.AlignCenter)
        empty_lay.addWidget(empty_title)

        empty_desc = QLabel(
            "Orbit can browse the web, create office documents, write Python code, "
            "inspect screen state, and run commands."
        )
        empty_desc.setStyleSheet(f"font-size: 13px; color: {_TEXT_SEC}; max-width: 480px;")
        empty_desc.setWordWrap(True)
        empty_desc.setAlignment(Qt.AlignCenter)
        empty_lay.addWidget(empty_desc)

        self.output_stack.addWidget(empty_widget)

        # Page 1: Active Output Text
        self.output_text = QTextEdit()
        self.output_text.setObjectName("outputText")
        self.output_text.setReadOnly(True)
        self.output_stack.addWidget(self.output_text)

        wb_layout.addWidget(self.output_stack, stretch=1)
        wb_page_lay.addWidget(workbench, stretch=1)

        self.main_stack.addWidget(workbench_page)

        # ----------------- PAGE 1: FULL-WINDOW TASK HISTORY -----------------
        self.history_view = TaskHistoryView(md_renderer=_md_to_html)
        self.history_view.rerun_requested.connect(self._handle_rerun_task)
        self.main_stack.addWidget(self.history_view)

        main_layout.addWidget(self.main_stack, stretch=1)
        root_h.addWidget(main_col, stretch=1)

        # ===================== SLIDE-OUT APPROVALS DRAWER =====================
        self.drawer = QFrame()
        self.drawer.setObjectName("drawerContainer")
        self.drawer.setFixedWidth(0)
        self._drawer_target_width = 360

        drawer_lay = QVBoxLayout(self.drawer)
        drawer_lay.setContentsMargins(0, 0, 0, 0)
        drawer_lay.setSpacing(0)

        d_header = QFrame()
        d_header.setObjectName("drawerHeader")
        dh_layout = QHBoxLayout(d_header)
        dh_layout.setContentsMargins(16, 14, 14, 14)

        d_title = QLabel("Pending Approvals")
        d_title.setObjectName("drawerTitle")
        dh_layout.addWidget(d_title)
        dh_layout.addStretch()

        close_btn = QPushButton("✕")
        close_btn.setObjectName("drawerCloseBtn")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self._close_drawer)
        dh_layout.addWidget(close_btn)

        drawer_lay.addWidget(d_header)

        # Approvals Review Card
        app_body = QWidget()
        app_lay = QVBoxLayout(app_body)
        app_lay.setContentsMargins(14, 14, 14, 14)
        app_lay.setSpacing(12)

        self.confirm_card = QFrame()
        self.confirm_card.setObjectName("confirmDrawerCard")
        cc_lay = QVBoxLayout(self.confirm_card)
        cc_lay.setContentsMargins(12, 12, 12, 12)
        cc_lay.setSpacing(10)

        self.confirm_heading = QLabel("No pending confirmations")
        self.confirm_heading.setStyleSheet(f"font-weight:700;font-size:14px;color:{_TEXT};")
        self.confirm_heading.setWordWrap(True)
        cc_lay.addWidget(self.confirm_heading)

        self.confirm_detail = QLabel("")
        self.confirm_detail.setStyleSheet(f"color:{_TEXT_SEC};font-size:12px;line-height:1.4;")
        self.confirm_detail.setWordWrap(True)
        cc_lay.addWidget(self.confirm_detail)

        self.confirm_shot = QLabel()
        self.confirm_shot.setAlignment(Qt.AlignCenter)
        self.confirm_shot.setMinimumHeight(180)
        self.confirm_shot.setStyleSheet(f"background:{_BG};border:1px solid {_BORDER};border-radius:8px;")
        cc_lay.addWidget(self.confirm_shot, stretch=1)

        c_buttons = QHBoxLayout()
        c_buttons.setSpacing(10)
        self.approve_btn = QPushButton("Approve Action")
        self.approve_btn.setObjectName("approveBtn")
        self.approve_btn.setCursor(Qt.PointingHandCursor)
        self.reject_btn = QPushButton("Reject")
        self.reject_btn.setObjectName("rejectBtn")
        self.reject_btn.setCursor(Qt.PointingHandCursor)
        self.approve_btn.clicked.connect(lambda: self._resolve_confirmation(True))
        self.reject_btn.clicked.connect(lambda: self._resolve_confirmation(False))
        c_buttons.addWidget(self.approve_btn, stretch=1)
        c_buttons.addWidget(self.reject_btn, stretch=1)
        cc_lay.addLayout(c_buttons)

        app_lay.addWidget(self.confirm_card, stretch=1)
        drawer_lay.addWidget(app_body, stretch=1)

        root_h.addWidget(self.drawer)

        self.setCentralWidget(central)

        # -- Status Bar -------------------------------------------------------
        status = QStatusBar()
        self.status_dot = QLabel()
        self.status_dot.setObjectName("statusDot")
        status.addWidget(self.status_dot)
        self.status_label = QLabel("Ready")
        status.addWidget(self.status_label)
        status.addPermanentWidget(QLabel(f"Python {sys.version.split()[0]}"))
        self.setStatusBar(status)

        # -- Timers & DB Watcher ----------------------------------------------
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._refresh_data)
        self.refresh_timer.start(2000)
        self._refresh_data()
        self._set_status("idle")
        self._setup_voice()

    # -- Voice ----------------------------------------------------------------

    def _setup_voice(self) -> None:
        self._voice_ctrl = VoiceController(self)
        self._voice_ctrl.session_started.connect(self._on_voice_started)
        self._voice_ctrl.session_stopped.connect(self._on_voice_stopped)
        self._voice_ctrl.volume_rms.connect(self._orb.set_volume)
        self._voice_ctrl.transcript_interim.connect(self._on_transcript_interim)
        self._voice_ctrl.transcript_final_segment.connect(self._on_transcript_final)
        self._voice_ctrl.transcript_ready.connect(self._on_transcript_ready)

        self._hotkey_filter = HotkeyFilter()
        self._hotkey_filter.toggled.connect(self._toggle_voice)
        QApplication.instance().installNativeEventFilter(self._hotkey_filter)

    def _toggle_voice(self) -> None:
        if self._voice_ctrl:
            self._voice_ctrl.toggle()

    def _on_voice_started(self) -> None:
        self._voice_panel.show()
        self._transcript_lbl.setText("")
        self._voice_status_lbl.setText("Listening… (F9 to stop)")
        self.mic_btn.setStyleSheet("background:#6366f1;color:white;border-radius:18px;")

    def _on_voice_stopped(self) -> None:
        self._voice_panel.hide()
        self._orb.set_volume(0.0)
        self.mic_btn.setStyleSheet("")

    def _on_transcript_interim(self, text: str) -> None:
        segments = self._transcript_lbl.text()
        # Show committed segments + current interim in italic
        base = segments.rsplit("\n", 1)[0] if "\n" in segments else ""
        display = (base + "\n" + text).strip() if base else text
        self._transcript_lbl.setText(display)

    def _on_transcript_final(self, text: str) -> None:
        current = self._transcript_lbl.text()
        self._transcript_lbl.setText((current + "\n" + text).strip())

    def _on_transcript_ready(self, text: str) -> None:
        if text:
            self.goal_input.setText(text)
            self.goal_input.setFocus()
            self._voice_status_lbl.setText("Edit transcript then press Enter ↵")
            # Keep the panel visible briefly so the user sees the final text
            self._voice_panel.show()
            QTimer.singleShot(200, lambda: self._voice_panel.hide())

    # -- Navigation Switcher --------------------------------------------------

    def _show_workbench(self) -> None:
        self.main_stack.setCurrentIndex(0)
        self.tab_workbench_btn.setStyleSheet(
            f"border:none;border-radius:8px;padding:6px 16px;font-size:12px;font-weight:700;"
            f"color:{_WHITE};background:{_INDIGO};"
        )
        self.tab_history_btn.setStyleSheet(
            f"border:none;border-radius:8px;padding:6px 16px;font-size:12px;font-weight:600;"
            f"color:{_TEXT_SEC};background:transparent;"
        )

    def _show_history(self) -> None:
        self.main_stack.setCurrentIndex(1)
        self.history_view.refresh()
        self.tab_history_btn.setStyleSheet(
            f"border:none;border-radius:8px;padding:6px 16px;font-size:12px;font-weight:700;"
            f"color:{_WHITE};background:{_INDIGO};"
        )
        self.tab_workbench_btn.setStyleSheet(
            f"border:none;border-radius:8px;padding:6px 16px;font-size:12px;font-weight:600;"
            f"color:{_TEXT_SEC};background:transparent;"
        )

    def _handle_rerun_task(self, goal: str, lane: str) -> None:
        self._show_workbench()
        self.lane_toggle.set_value(lane)
        self.goal_input.setText(goal)
        self.goal_input.setFocus()

    # -- Approvals Drawer Slide Animations ------------------------------------

    def _animate_drawer_width(self, target_w: int) -> None:
        self._drawer_anim = QPropertyAnimation(self.drawer, b"maximumWidth")
        self._drawer_anim.setDuration(260)
        self._drawer_anim.setStartValue(self.drawer.width())
        self._drawer_anim.setEndValue(target_w)
        self._drawer_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._drawer_anim.start()

    def _open_approvals_drawer(self) -> None:
        self._drawer_open = True
        self.approvals_btn.setProperty("class", "navBtnActive")
        self.approvals_btn.style().unpolish(self.approvals_btn)
        self.approvals_btn.style().polish(self.approvals_btn)
        self._animate_drawer_width(self._drawer_target_width)

    def _toggle_approvals(self) -> None:
        if self._drawer_open:
            self._close_drawer()
        else:
            self._open_approvals_drawer()

    def _close_drawer(self) -> None:
        self._drawer_open = False
        self.approvals_btn.setProperty("class", "navBtn")
        self.approvals_btn.style().unpolish(self.approvals_btn)
        self.approvals_btn.style().polish(self.approvals_btn)
        self._animate_drawer_width(0)

    # -- Workbench Toolbar Actions --------------------------------------------

    def _toggle_autoscroll(self) -> None:
        self._auto_scroll = not self._auto_scroll
        self.autoscroll_btn.setText(f"Auto-scroll: {'ON' if self._auto_scroll else 'OFF'}")

    def _copy_output(self) -> None:
        plain_text = self.output_text.toPlainText()
        if plain_text:
            QGuiApplication.clipboard().setText(plain_text)
            self.copy_btn.setText("✓ Copied!")
            QTimer.singleShot(1500, lambda: self.copy_btn.setText("Copy"))

    # -- Task Submission ------------------------------------------------------

    def _ensure_worker(self) -> None:
        """Spawn the warm worker process if it is not already running.

        The worker runs `orbit.run_task --serve`, which reads one JSON goal
        line per task from stdin and emits [TASK:DONE exit_code] when done.
        Python imports + the event loop pay their cost once at GUI startup;
        subsequent tasks skip the 7–8 s cold-start and go straight to the
        first LLM call.
        """
        if self._worker is not None and self._worker.state() != QProcess.NotRunning:
            return
        env = QProcess.systemEnvironment()
        w = QProcess(self)
        w.setEnvironment(env)
        w.setProcessChannelMode(QProcess.SeparateChannels)
        w.readyReadStandardOutput.connect(self._read_stdout)
        w.readyReadStandardError.connect(self._read_stderr)
        w.finished.connect(self._worker_exited)
        w.start(_VENV_PYTHON, ["-m", "orbit.run_task", "--serve"])
        self._worker = w

    def _submit_task(self) -> None:
        goal = self.goal_input.text().strip()
        if not goal or self._task_running:
            return

        lane = self.lane_toggle.value()
        effort = self.effort_combo.currentText().lower()

        self._ensure_worker()

        req = json.dumps({"goal": goal, "lane": lane, "effort": effort})
        self._worker.write((req + "\n").encode())  # type: ignore[union-attr]
        self._task_running = True

        self._raw_buffer = ""
        self._goal_header = f"> {goal}\n  ({lane} | effort: {effort})\n"
        self.output_text.clear()
        self.output_stack.setCurrentIndex(1)
        self.step_tracker.reset()

        self.wb_lane_badge.setText(lane.upper())
        self.wb_status_dot.setStyleSheet(f"color:{_AMBER};font-size:12px;")

        self._append_plain(f"> {goal}\n", _INDIGO)
        self._append_plain(f"  {lane} lane  |  effort: {effort}\n\n", _TEXT_SEC)

        self.goal_input.clear()
        self.goal_input.setEnabled(False)
        self.send_btn.hide()
        self.stop_btn.show()
        self.progress.show()
        self._set_status("running")

    def _stop_task(self) -> None:
        if self._worker and self._task_running:
            self._append_plain("\n[task stopped by user]\n", _RED_TEXT)
            # Kill the worker; _worker_exited fires via the finished signal
            # and calls _task_done so the UI is restored exactly once.
            self._worker.kill()

    _TASK_DONE_RE = re.compile(r"\[TASK:DONE (\d+)\]")

    def _read_stdout(self) -> None:
        if not self._worker:
            return
        data = self._worker.readAllStandardOutput()
        text = bytes(data).decode("utf-8", errors="replace")
        for line in text.splitlines(keepends=True):
            stripped = line.strip()

            # Warm-worker sentinel: task finished, restore UI without exiting.
            # Do NOT accumulate this line into _raw_buffer.
            done_m = self._TASK_DONE_RE.search(stripped)
            if done_m:
                self._task_done(int(done_m.group(1)))
                continue

            self._raw_buffer += line

            step_m = self._STEP_RE.match(stripped)
            if step_m:
                marker_type = step_m.group(1)
                description = step_m.group(2).strip()
                detail = (step_m.group(3) or "").strip()
                self.step_tracker.handle_marker(marker_type, description, detail)
                continue

            tool_m = self._TOOL_CALL_RE.search(stripped)
            if tool_m:
                self.step_tracker.handle_tool_call(tool_m.group(1))

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
            f'<div style="margin:12px 0;"><img src="file:///{p.as_posix()}" '
            f'width="580" style="border:1px solid {_BORDER};border-radius:10px;'
            f'box-shadow: 0 4px 6px -1px rgba(0,0,0,0.06);"></div>'
        )
        self.output_text.setTextCursor(cursor)
        if self._auto_scroll:
            self.output_text.ensureCursorVisible()

    def _read_stderr(self) -> None:
        if not self._worker:
            return
        data = self._worker.readAllStandardError()
        text = bytes(data).decode("utf-8", errors="replace")
        for line in text.splitlines(keepends=True):
            if any(noise in line for noise in self._STDERR_NOISE):
                continue
            if line.strip():
                self._append_plain(line, _RED_TEXT)

    def _task_done(self, exit_code: int) -> None:
        """Restore the UI after a task completes (normal finish or kill).
        Called from _read_stdout when [TASK:DONE] arrives, or from
        _worker_exited when the process exits unexpectedly."""
        if not self._task_running:
            return  # guard against duplicate calls
        self._task_running = False

        for s in self.step_tracker.steps:
            if s.status == StepStatus.RUNNING:
                s.status = StepStatus.DONE if exit_code == 0 else StepStatus.FAILED
                if not s.finished_at:
                    s.finished_at = time.time()
        self.step_tracker._refresh_all_widgets()

        self.goal_input.setEnabled(True)
        self.send_btn.show()
        self.stop_btn.hide()
        self.progress.hide()
        self._render_final_output(exit_code)
        if exit_code == 0:
            self._set_status("idle")
            self.wb_status_dot.setStyleSheet(f"color:{_GREEN};font-size:12px;")
        else:
            self._set_status("error")
            self.wb_status_dot.setStyleSheet(f"color:{_RED};font-size:12px;")
        self._refresh_data()
        self.goal_input.setFocus()

    def _worker_exited(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        """Worker process exited (crash or explicit kill). Clean up and let
        the next _submit_task respawn it via _ensure_worker."""
        self._worker = None
        if self._task_running:
            self._task_done(exit_code)

    def _render_final_output(self, exit_code: int) -> None:
        header_html = (
            f'<div style="background:{_INDIGO_LIGHT};border:1px solid {_INDIGO_BORDER};'
            f'border-radius:10px;padding:12px 16px;margin-bottom:14px;">'
            f'<p style="color:{_INDIGO_TEXT};font-size:15px;font-weight:700;margin:0 0 2px 0;">'
            f'{_inline_md(self._goal_header.split(chr(10))[0])}</p>'
            f'<p style="color:{_TEXT_SEC};font-size:12px;margin:0;">'
            f'{_inline_md(self._goal_header.split(chr(10))[1] if chr(10) in self._goal_header else "")}</p>'
            f'</div>'
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
            filtered_lines = [
                l for l in s.splitlines()
                if not self._STEP_RE.match(l.strip())
            ]
            cleaned_s = "\n".join(filtered_lines).strip()
            if cleaned_s:
                body_html += _md_to_html(cleaned_s)

        status_color = _GREEN_TEXT if exit_code == 0 else _RED_TEXT
        status_bg = _GREEN_LIGHT if exit_code == 0 else _RED_LIGHT
        status_border = _GREEN_BORDER if exit_code == 0 else _RED_BORDER
        status_text = "Task completed successfully" if exit_code == 0 else f"Task failed with exit code {exit_code}"
        status_html = (
            f'<div style="background:{status_bg};border:1px solid {status_border};'
            f'border-radius:8px;padding:8px 12px;margin-top:14px;display:inline-block;">'
            f'<span style="color:{status_color};font-size:12px;font-weight:700;">● {status_text}</span>'
            f'</div>'
        )

        full_html = (
            f'<div style="font-family:\'Segoe UI Variable\',\'Segoe UI\',sans-serif;padding:6px;">'
            f'{header_html}{body_html}{status_html}</div>'
        )
        self.output_text.setHtml(full_html)
        cursor = self.output_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        self.output_text.setTextCursor(cursor)

    # -- Output Helpers -------------------------------------------------------

    def _append_plain(self, text: str, color: str) -> None:
        cursor = self.output_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        fmt = cursor.charFormat()
        fmt.setForeground(QColor(color))
        cursor.setCharFormat(fmt)
        cursor.insertText(text)
        self.output_text.setTextCursor(cursor)
        if self._auto_scroll:
            self.output_text.ensureCursorVisible()

    def _clear_output(self) -> None:
        self.output_text.clear()
        self._raw_buffer = ""
        self.step_tracker.reset()
        self.output_stack.setCurrentIndex(0)
        self.wb_status_dot.setStyleSheet(f"color:{_INDIGO};font-size:12px;")

    # -- Database & Approvals Refresh -----------------------------------------

    def _refresh_data(self) -> None:
        db.init_db()
        self._refresh_confirmations()
        tasks = db.list_tasks(limit=100)
        self.tab_history_btn.setText(f"📜 History & Analytics ({len(tasks)})")

    def _refresh_confirmations(self) -> None:
        pending = db.list_pending_confirmations()
        count = len(pending)
        self.approvals_btn.setText(f"Approvals ({count})")

        if count > 0:
            if not self._drawer_open:
                self.approvals_btn.setProperty("class", "navBtnAlert")
                self.approvals_btn.style().unpolish(self.approvals_btn)
                self.approvals_btn.style().polish(self.approvals_btn)

            row = pending[0]
            self._current_confirm_id = row["confirmation_id"]
            action = row.get("action", "UI action")
            self.banner_text.setText(f"⚠️ Confirmation Required: {action} ({count} pending)")
            self.approval_banner.show()

            self.confirm_heading.setText(f"Approval Required: {action}")
            ttl = load_windows_control_policy().get("approval_token_ttl_seconds", 120)
            self.confirm_detail.setText(
                f"<b>Target:</b> {row.get('candidate_label') or 'Low confidence coordinate'}<br>"
                f"<b>Task:</b> {row['task_id']} &nbsp;|&nbsp; <b>Requested:</b> {row['created_at']}<br>"
                f"<i>Approving grants ONE single action, valid for {ttl}s.</i>"
            )
            self._render_confirm_shot(row)
            self.approve_btn.setEnabled(True)
            self.reject_btn.setEnabled(True)
        else:
            self._current_confirm_id = None
            self.approval_banner.hide()
            if not self._drawer_open:
                self.approvals_btn.setProperty("class", "navBtn")
                self.approvals_btn.style().unpolish(self.approvals_btn)
                self.approvals_btn.style().polish(self.approvals_btn)

            self.confirm_heading.setText("No pending confirmations")
            self.confirm_detail.setText("When a tool requires human-in-the-loop approval, review details and screenshot will appear here.")
            self.confirm_shot.setText("(no screenshot)")
            self.approve_btn.setEnabled(False)
            self.reject_btn.setEnabled(False)

    def _render_confirm_shot(self, row: dict) -> None:
        path = row.get("screenshot_path")
        pixmap = QPixmap(path) if path else QPixmap()
        if pixmap.isNull():
            self.confirm_shot.setText("(no screenshot available)")
            return
        box = row.get("candidate_box")
        if box:
            painter = QPainter(pixmap)
            painter.setPen(QPen(QColor(_INDIGO), 3))
            left, top, right, bottom = box
            painter.drawRect(QRect(left, top, right - left, bottom - top))
            painter.end()
        self.confirm_shot.setPixmap(
            pixmap.scaled(
                self.confirm_shot.width() or 320, 180,
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
        )

    def _resolve_confirmation(self, approved: bool) -> None:
        if not self._current_confirm_id:
            return
        try:
            db.resolve_pending_confirmation(
                self._current_confirm_id,
                approved=approved,
                ttl_seconds=int(
                    load_windows_control_policy().get("approval_token_ttl_seconds", 120)
                ),
            )
        except KeyError:
            pass
        self._refresh_confirmations()

    # -- Status Bar -----------------------------------------------------------

    def _set_status(self, state: str) -> None:
        dots = {
            "idle": f'<span style="color:{_GREEN}">●</span>',
            "running": f'<span style="color:{_AMBER}">●</span>',
            "error": f'<span style="color:{_RED}">●</span>',
        }
        labels = {"idle": "Ready", "running": "Task executing...", "error": "Task failed"}
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

