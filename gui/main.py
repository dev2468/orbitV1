"""Orbit unified GUI — Studio (warm / organic) direction.

Layout, top to bottom: a 48px nav bar (brand, Workbench/History switcher,
approvals bell), the page stack, and a 28px status bar. The Workbench page is
two columns — a reading column carrying the input card, quick chips and the
output pane, and a fixed 220px step rail on the right (see
``gui/step_tracker.py`` for why progress lives out there).

## Overlays are children of the central widget, not dialogs

The voice modal and the approvals drawer are both plain widgets reparented
onto ``central`` and ``raise_()``d, each behind its own scrim. Neither is a
QDialog, and that is deliberate on both counts:

* the voice modal must not take the keyboard, because F9 has to keep reaching
  the app's native event filter to stop the recording it started;
* the drawer used to animate a layout width, which reflowed the entire
  workbench beside it on every frame. As an overlay it composites over a
  static page instead.

Both are positioned manually from :meth:`OrbitWindow._layout_overlays`, called
from ``resizeEvent`` and whenever one is shown.

## Task submission still spawns nothing per task

The warm worker (``orbit.run_task --serve``) is spawned once at first submit
and fed one JSON line per goal; ``[TASK:DONE n]`` on stdout ends a task
without ending the process. That contract is unchanged by this redesign —
see ``_ensure_worker``. The standing rule that this process never writes task
or event rows to orbit.db is likewise unchanged; the approvals card remains
the single, deliberate exception (``gui/CLAUDE.md``).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QProcess,
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QMenu,
)

from orbit import db
from orbit.policy import load_windows_control_policy
# orbit.models is deliberately litellm-free — see its docstring. Importing
# orbit.agent for the same names would cost this process 8.7s at startup.
from orbit.models import (
    DEFAULT_MODEL as _DEFAULT_MODEL,
    KNOWN_MODELS as _KNOWN_MODELS,
    short_model_name as _short_model_name,
)
from gui import theme
from gui.stats import format_duration
from gui.step_tracker import StepTracker, StepStatus
from gui.history_view import TaskHistoryView
from gui.voice import HotkeyFilter, ScrimWidget, VoiceController, VoiceModal
from gui.ack_controller import AckController
from gui.speech import SpeechPlayer, split_sentences

_VENV_PYTHON = str(Path(_PROJECT_ROOT) / "venv" / "Scripts" / "python.exe")

# Marks a structured progress line on the worker's stdout: `[ORBIT]{json}`.
#
# Duplicated from orbit.run_task.EVENT_PREFIX rather than imported, and that
# is deliberate: importing orbit.run_task would pull litellm and google-adk
# into the GUI process, which measured 10.6s of import time this process has
# no other reason to pay. The GUI already keeps to the light end of orbit
# (db, policy) for the same reason. Change one, change the other.
_EVENT_PREFIX = "[ORBIT]"

# How long a submitted goal waits for the acknowledgement to classify it before
# being sent to the worker anyway. Classification normally lands ~1s in; this is
# the ceiling for a slow or failed provider, not the expected wait.
_DISPATCH_GUARD_MS = 3000

# The foreground lane waits the FULL window even once classified, so the spoken
# acknowledgement lands before anything touches the real mouse and keyboard and
# the user can still cancel with Esc. Headless does not need it: a headless
# task's first several seconds are MCP connect, which changes nothing.
_FOREGROUND_HOLD_MS = int(os.environ.get("ORBIT_VOICE_HOLD_MS", "") or 2500)

# How long after a transcript before it submits itself. Short, because the
# acknowledgement is the real confirmation — the user hears what was understood
# a second later, and Esc still stops everything.
_AUTO_SUBMIT_MS = int(os.environ.get("ORBIT_AUTO_SUBMIT_MS", "") or 900)

# How many finished turns the thread keeps rendered. A QTextEdit re-lays
# out its whole document on setHtml, so an unbounded thread makes every
# later turn slower to render. Twenty is far more than anyone scrolls
# back through, and the full history is in the History tab regardless.
_MAX_THREAD_TURNS = 20

# Starter prompts offered under the input when the workbench is idle. Each is
# a full goal, not a category label — clicking one should be runnable as-is.
_QUICK_CHIPS = [
    ("Search for flights", "Find the cheapest round-trip flights from Delhi to Goa next weekend"),
    ("Draft an email", "Draft a polite follow-up email to a client who hasn't replied in a week"),
    ("Summarize a PDF", "Summarize the key points of the most recent PDF in my Downloads folder"),
    ("Research a product", "Compare the top 3 mechanical keyboards under 5000 rupees and recommend one"),
]


# -- markdown to HTML ---------------------------------------------------------

def _md_to_html(text: str) -> str:
    lines = text.split("\n")
    html_parts: list[str] = []
    in_table = False
    in_list = False
    table_rows: list[list[str]] = []
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
                f'style="max-width:100%;border:1px solid {theme.BORDER};border-radius:14px;">'
            )
            if alt:
                html_parts.append(
                    f'<p style="color:{theme.TEXT_TERTIARY};font-size:11px;margin:4px 0 12px 0;'
                    f'font-style:italic;">{alt}</p>'
                )
            html_parts.append('</div>')
            i += 1; continue

        if stripped.startswith("screenshot_path:") or stripped.startswith("[screenshot:"):
            if in_list:
                html_parts.append("</ul>"); in_list = False
            path = re.sub(
                r"^(?:screenshot_path:\s*|^\[screenshot:\s*|\]$)", "", stripped
            ).strip().rstrip("]").replace("\\", "/")
            html_parts.append(
                f'<div style="margin:12px 0;"><img src="file:///{path}" '
                f'style="max-width:100%;border:1px solid {theme.BORDER};border-radius:14px;"></div>'
            )
            i += 1; continue

        if re.match(r"^-{4,}$", stripped):
            if in_list:
                html_parts.append("</ul>"); in_list = False
            html_parts.append(
                f'<hr style="border:none;border-top:1px solid {theme.BORDER_LIGHT};margin:14px 0;">'
            )
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
                f'color:{theme.TEXT_PRIMARY};margin:14px 0 6px 0;">{_inline_md(m.group(2))}</p>'
            )
            i += 1; continue

        m = re.match(r"^[-*]\s+(.+)$", stripped)
        if m:
            if not in_list:
                in_list = True
                html_parts.append(
                    f'<ul style="margin:4px 0 4px 18px;padding:0;color:{theme.TEXT_PRIMARY};">'
                )
            html_parts.append(f'<li style="margin:3px 0;">{_inline_md(m.group(1))}</li>')
            i += 1; continue

        if in_list and not stripped:
            html_parts.append("</ul>"); in_list = False

        if not stripped:
            html_parts.append("<br>"); i += 1; continue

        html_parts.append(
            f'<p style="margin:4px 0;color:{theme.TEXT_PRIMARY};line-height:1.7;">'
            f'{_inline_md(stripped)}</p>'
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
    html = (
        f'<table style="border-collapse:collapse;width:100%;margin:10px 0;font-size:12px;'
        f'border:1px solid {theme.BORDER};border-radius:8px;">'
    )
    for idx, row in enumerate(rows):
        tag = "th" if idx == 0 else "td"
        bg = theme.INPUT_BG if idx == 0 else (theme.BG_CANVAS if idx % 2 == 0 else theme.SURFACE)
        weight = "700" if idx == 0 else "400"
        color = theme.TEXT_PRIMARY if idx == 0 else theme.TEXT_PRIMARY
        html += "<tr>"
        for cell in row:
            html += (
                f'<{tag} style="padding:8px 12px;border-bottom:1px solid {theme.BORDER};'
                f'background:{bg};font-weight:{weight};color:{color};text-align:left;">'
                f'{_inline_md(cell)}</{tag}>'
            )
        html += "</tr>"
    html += "</table>"
    return html


def _inline_md(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(
        r"\*\*(.+?)\*\*",
        rf'<b style="color:{theme.TEXT_PRIMARY};font-weight:700;">\1</b>',
        text,
    )
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(
        r"`(.+?)`",
        rf'<code style="background:{theme.INPUT_BG};padding:2px 6px;border-radius:4px;'
        rf'border:1px solid {theme.BORDER};font-size:12px;color:{theme.ACCENT_PRESSED};'
        rf'font-weight:600;">\1</code>',
        text,
    )
    return text


# -- small painted pieces -----------------------------------------------------

class LogoMark(QWidget):
    """The Orbit glyph: an indigo disc with a punched-out ring.

    Painted rather than shipped as an icon file so it stays crisp at any DPI
    and picks up the theme accent without a second asset to keep in sync.
    """

    def __init__(self, size: int = 18, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = self._size
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.ACCENT))
        p.drawEllipse(0, 0, s, s)
        # Ring: draw the surface color as an annulus via two circles.
        p.setBrush(QColor(theme.SURFACE))
        r2 = s * 0.45
        p.drawEllipse(int((s - r2) / 2), int((s - r2) / 2), int(r2), int(r2))
        p.setBrush(QColor(theme.ACCENT))
        r3 = s * 0.20
        p.drawEllipse(int((s - r3) / 2), int((s - r3) / 2), int(r3), int(r3))
        p.end()


class SegmentedToggle(QFrame):
    """Pill-shaped segmented control (Headless / Foreground, Workbench / History).

    A QFrame with an id selector, not a bare QWidget: a plain QWidget ignores
    a QSS ``background`` unless WA_StyledBackground is set, so the track would
    render invisible and leave the buttons floating.
    """

    def __init__(
        self,
        options: list[str],
        default: int = 0,
        *,
        compact: bool = False,
        on_change=None,
    ) -> None:
        super().__init__()
        self._buttons: list[QPushButton] = []
        self._selected = default
        self._compact = compact
        self._on_change = on_change

        self.setObjectName("segTrack")
        self.setStyleSheet(
            f"#segTrack {{ background:{theme.INPUT_BG};"
            f" border:none; border-radius:{10 if compact else 14}px; }}"
        )
        lay = QHBoxLayout(self)
        pad = 2 if compact else 3
        lay.setContentsMargins(pad, pad, pad, pad)
        lay.setSpacing(2)
        for idx, label in enumerate(options):
            btn = QPushButton(label)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFlat(True)
            btn.clicked.connect(lambda _=False, i=idx: self._select(i))
            self._buttons.append(btn)
            lay.addWidget(btn)
        self._apply_styles()

    def _select(self, idx: int) -> None:
        if idx == self._selected:
            return
        self._selected = idx
        self._apply_styles()
        if self._on_change:
            self._on_change(self.value())

    def _apply_styles(self) -> None:
        if self._compact:
            pad, fs, radius = "4px 14px", "12px", 8
        else:
            pad, fs, radius = "7px 20px", "13px", 12
        for i, btn in enumerate(self._buttons):
            if i == self._selected:
                btn.setStyleSheet(
                    f"border:none;border-radius:{radius}px;padding:{pad};font-size:{fs};"
                    f"font-weight:600;color:{theme.TEXT_PRIMARY};background:{theme.SURFACE};"
                )
            else:
                btn.setStyleSheet(
                    f"border:none;border-radius:{radius}px;padding:{pad};font-size:{fs};"
                    f"font-weight:500;color:{theme.TEXT_TERTIARY};background:transparent;"
                )

    def value(self) -> str:
        return self._buttons[self._selected].text().lower()

    def set_value(self, val: str) -> None:
        for i, b in enumerate(self._buttons):
            if b.text().lower() == val.lower():
                self._select(i)
                break

    def set_index(self, idx: int) -> None:
        self._select(idx)


class BellButton(QPushButton):
    """Approvals bell with a count badge painted into the corner.

    The badge is painted rather than being a child QLabel so it can overhang
    the button's rounded corner without the parent clipping it.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._count = 0
        self.setFixedSize(36, 36)
        self.setCursor(Qt.PointingHandCursor)
        self._apply_style()

    def set_count(self, count: int) -> None:
        if count == self._count:
            return
        self._count = count
        self._apply_style()
        self.update()

    def _apply_style(self) -> None:
        if self._count:
            self.setStyleSheet(
                f"QPushButton {{ background:{theme.WARNING_BG};"
                f" border:1px solid {theme.WARNING_BORDER};"
                f" border-radius:{theme.RADIUS_MD}px; }}"
                f"QPushButton:hover {{ background:#FFEDD5; }}"
            )
            self.setToolTip(f"{self._count} action(s) awaiting approval")
        else:
            self.setStyleSheet(
                f"QPushButton {{ background:{theme.INPUT_BG}; border:none;"
                f" border-radius:{theme.RADIUS_MD}px; }}"
                f"QPushButton:hover {{ background:{theme.BORDER}; }}"
            )
            self.setToolTip("No pending approvals")

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        stroke = QColor(theme.WARNING if self._count else theme.TEXT_TERTIARY)
        p.setPen(QPen(stroke, 1.8))
        p.setBrush(Qt.NoBrush)
        # Bell: a dome plus a base line plus a clapper arc.
        p.drawArc(QRect(11, 10, 14, 14), 0, 180 * 16)
        p.drawLine(11, 17, 11, 22)
        p.drawLine(25, 17, 25, 22)
        p.drawLine(9, 23, 27, 23)
        p.drawArc(QRect(15, 24, 6, 4), 180 * 16, 180 * 16)

        if self._count:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.SURFACE))
            p.drawEllipse(23, 1, 14, 14)
            p.setBrush(QColor(theme.WARNING))
            p.drawEllipse(24, 2, 12, 12)
            p.setPen(QColor("#FFFFFF"))
            f = p.font()
            f.setPointSizeF(6.5)
            f.setBold(True)
            p.setFont(f)
            label = "9+" if self._count > 9 else str(self._count)
            p.drawText(QRect(24, 2, 12, 12), Qt.AlignCenter, label)
        p.end()


class EmptyState(QWidget):
    """Idle workbench: concentric ring glyph over two lines of copy."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(12)

        glyph = _RingGlyph()
        lay.addWidget(glyph, alignment=Qt.AlignHCenter)

        title = QLabel("Ready for your next task")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"font-size:15px;font-weight:500;color:{theme.TEXT_SECONDARY};background:transparent;"
        )
        lay.addWidget(title)

        sub = QLabel("Type a goal or press F9 to speak")
        sub.setAlignment(Qt.AlignCenter)
        sub.setStyleSheet(
            f"font-size:12px;color:{theme.TEXT_TERTIARY};background:transparent;"
        )
        lay.addWidget(sub)


class _RingGlyph(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(56, 56)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(theme.BORDER_INPUT), 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(4, 4, 48, 48)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.ACCENT_LIGHT))
        p.drawEllipse(18, 18, 20, 20)
        p.setBrush(QColor(theme.ACCENT))
        p.drawEllipse(24, 24, 8, 8)
        p.end()


# -- main window --------------------------------------------------------------

class OrbitWindow(QMainWindow):
    _STEP_RE = re.compile(
        r"^\[STEP:(START|DONE|FAIL|PROGRESS)\]\s*(.+?)(?:\s*[—\-–]\s*(.+))?$",
        re.UNICODE,
    )
    _TOOL_CALL_RE = re.compile(r"""(?:^|[\s\[])tool_call:\s*([a-zA-Z0-9_]+)""", re.IGNORECASE)
    _SCREENSHOT_RE = re.compile(
        r"""['"]?screenshot_path['"]?\s*[:=]\s*['"]?(.+?\.png)['"]?""", re.IGNORECASE
    )
    _TASK_DONE_RE = re.compile(r"\[TASK:DONE (\d+)\]")
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
        self.setMinimumSize(960, 640)

        self._worker: QProcess | None = None
        self._task_running = False
        self._task_started_at: float | None = None
        self._conversation_id: str = f"CONV-{uuid.uuid4().hex[:12]}"
        self._voice_ctrl: VoiceController | None = None
        self._committed_text = ""
        self._raw_buffer = ""
        self._goal_header = ""
        # Set when the goal in the box arrived by voice, cleared on submit.
        # It decides whether the acknowledgement is *spoken* — someone typing
        # at a keyboard has the screen in front of them and did not ask to be
        # talked at. The acknowledgement itself runs either way, because the
        # useful part of "I'll search Amazon for that" is knowing you were
        # understood before the work track has said anything at all.
        self._voice_originated = False
        # Latched from _voice_originated at submit time, so that clearing the
        # box mid-task cannot change whether the reply already in flight for
        # THIS task gets spoken.
        self._spoken_submission = False
        self._ack_text = ""
        self._last_goal = ""
        # A goal held back from the worker until the acknowledgement says
        # whether it is work at all — see "deferred dispatch".
        self._pending_work: dict | None = None
        self._hold_for_full_window = False
        # The last goal the classifier called purely social. Re-submitting it
        # verbatim forces the work track, which is the recovery path for a
        # wrong classification.
        self._chat_only_goal = ""
        # How the last task actually ended, from the `result` event —
        # richer than the exit code the sentinel carries.
        self._result_status = ""
        self._result_text = ""
        # Rendered HTML for each finished turn of the current conversation.
        self._turn_html: list[str] = []
        # True between a CHAT classification and the ack finishing, which is
        # when a social turn actually ends. See _on_ack_classified.
        self._chat_turn = False
        self._auto_scroll = True
        self._drawer_open = False
        self._current_confirm_id: str | None = None
        self._pending_count = 0

        central = QWidget()
        central.setStyleSheet(f"background: {theme.BG_CANVAS};")
        self._central = central
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_nav_bar())

        self.main_stack = QStackedWidget()
        self.main_stack.addWidget(self._build_workbench_page())

        self.history_view = TaskHistoryView(md_renderer=_md_to_html)
        self.history_view.rerun_requested.connect(self._handle_rerun_task)
        self.main_stack.addWidget(self.history_view)

        root.addWidget(self.main_stack, stretch=1)
        self.setCentralWidget(central)

        self._build_status_bar()
        self._build_overlays()
        self._setup_voice()
        self._setup_speech()
        self._setup_shortcuts()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._refresh_data)
        self.refresh_timer.start(2000)

        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.timeout.connect(self._tick_status)
        self.elapsed_timer.start(1000)

        self._refresh_data()
        self._set_status("idle")
        self._show_workbench()

    # ===================== construction =====================

    def _build_nav_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("navBar")
        bar.setFixedHeight(theme.NAV_HEIGHT)
        bar.setStyleSheet(
            f"#navBar {{ background:{theme.SURFACE};"
            f" border-bottom:1px solid {theme.BORDER}; }}"
        )
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(24, 0, 24, 0)
        lay.setSpacing(0)

        lay.addWidget(LogoMark(18))
        lay.addSpacing(8)

        logo = QLabel("Orbit")
        logo.setStyleSheet(
            f"font-size:17px;font-weight:800;color:{theme.TEXT_PRIMARY};"
            f"letter-spacing:-0.3px;background:transparent;"
        )
        lay.addWidget(logo)
        lay.addSpacing(28)

        self.nav_tabs = SegmentedToggle(
            ["Workbench", "History"], default=0, on_change=self._on_nav_change
        )
        lay.addWidget(self.nav_tabs)

        lay.addStretch()

        # Recent chats sits next to New Chat because resuming and starting are
        # the same decision, made in the same moment. Putting it in the
        # History tab would mean leaving the place you type to get back to the
        # place you type.
        self.chats_btn = QPushButton("Recent  ▾")
        self.chats_btn.setObjectName("chatsBtn")
        self.chats_btn.setCursor(Qt.PointingHandCursor)
        self.chats_btn.setFixedHeight(32)
        self.chats_btn.setToolTip("Reopen an earlier chat and carry on in it")
        self.chats_btn.setStyleSheet(
            f"#chatsBtn {{ background:transparent; color:{theme.TEXT_SECONDARY};"
            f" border:1px solid {theme.BORDER}; border-radius:{theme.RADIUS_SM}px;"
            f" padding:0 12px; font-size:12px; font-weight:500; }}"
            f"#chatsBtn:hover {{ border-color:{theme.ACCENT};"
            f" color:{theme.ACCENT}; }}"
            f"#chatsBtn::menu-indicator {{ image:none; width:0px; }}"
            f"#chatsBtn QMenu {{ background:{theme.SURFACE};"
            f" border:1px solid {theme.BORDER}; border-radius:8px; padding:4px; }}"
        )
        chats_menu = QMenu(self.chats_btn)
        chats_menu.setStyleSheet(
            f"QMenu {{ background:{theme.SURFACE}; border:1px solid {theme.BORDER};"
            f" border-radius:8px; padding:4px; font-size:12px; }}"
            f"QMenu::item {{ padding:6px 14px; border-radius:6px;"
            f" color:{theme.TEXT_SECONDARY}; }}"
            f"QMenu::item:selected {{ background:{theme.ACCENT_LIGHT};"
            f" color:{theme.ACCENT}; }}"
            f"QMenu::item:disabled {{ color:{theme.TEXT_TERTIARY}; }}"
        )
        self.chats_btn.setMenu(chats_menu)
        # Rebuilt on open, not on a timer: the list is short, reading it costs
        # one query, and a stale menu is the one thing that makes a picker
        # feel broken.
        chats_menu.aboutToShow.connect(self._refresh_chat_picker)
        lay.addWidget(self.chats_btn)
        lay.addSpacing(8)

        self.new_chat_btn = QPushButton("+ New Chat")
        self.new_chat_btn.setObjectName("newChatBtn")
        self.new_chat_btn.setCursor(Qt.PointingHandCursor)
        self.new_chat_btn.setFixedHeight(32)
        self.new_chat_btn.setStyleSheet(
            f"#newChatBtn {{ background:transparent; color:{theme.ACCENT};"
            f" border:1px solid {theme.ACCENT}; border-radius:{theme.RADIUS_SM}px;"
            f" padding:0 14px; font-size:12px; font-weight:600; }}"
            f"#newChatBtn:hover {{ background:{theme.ACCENT}; color:#FFFFFF; }}"
        )
        self.new_chat_btn.clicked.connect(self._new_conversation)
        lay.addWidget(self.new_chat_btn)
        lay.addSpacing(12)

        self.bell_btn = BellButton()
        self.bell_btn.clicked.connect(self._toggle_drawer)
        lay.addWidget(self.bell_btn)
        return bar

    def _build_workbench_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        cols = QHBoxLayout(page)
        cols.setContentsMargins(0, 0, 0, 0)
        cols.setSpacing(0)

        # ---- left reading column -------------------------------------------
        left = QWidget()
        left.setStyleSheet("background: transparent;")
        col = QVBoxLayout(left)
        col.setContentsMargins(28, 24, 28, 20)
        col.setSpacing(16)

        col.addWidget(self._build_input_card())

        self.chips_row = QWidget()
        self.chips_row.setStyleSheet("background: transparent;")
        chips_lay = QHBoxLayout(self.chips_row)
        chips_lay.setContentsMargins(0, 0, 0, 0)
        chips_lay.setSpacing(8)
        for label, goal in _QUICK_CHIPS:
            chips_lay.addWidget(self._make_chip(label, goal))
        chips_lay.addStretch()
        col.addWidget(self.chips_row)

        # Shown only after the acknowledgement classified a turn as purely
        # social and skipped the work track. It is the recovery path for a
        # wrong classification, and it is a real button rather than the line
        # of grey text it started as — an affordance nobody notices is not a
        # recovery path, and this one has to be found in the two seconds
        # before the user concludes the app ignored them.
        self.rerun_row = QWidget()
        self.rerun_row.setStyleSheet("background: transparent;")
        rerun_lay = QHBoxLayout(self.rerun_row)
        rerun_lay.setContentsMargins(0, 0, 0, 0)
        rerun_lay.setSpacing(8)
        self.rerun_hint = QLabel("Answered directly, without running a task.")
        self.rerun_hint.setStyleSheet(
            f"font-size:12px;color:{theme.TEXT_TERTIARY};background:transparent;"
        )
        rerun_lay.addWidget(self.rerun_hint)
        self.rerun_btn = QPushButton("Run it as a task  →")
        self.rerun_btn.setObjectName("rerunBtn")
        self.rerun_btn.setCursor(Qt.PointingHandCursor)
        self.rerun_btn.setStyleSheet(
            f"#rerunBtn {{ background:{theme.ACCENT_LIGHT};"
            f" border:1px solid {theme.ACCENT_BORDER}; border-radius:10px;"
            f" padding:4px 12px; font-size:12px; font-weight:600;"
            f" color:{theme.ACCENT_PRESSED}; }}"
            f"#rerunBtn:hover {{ background:{theme.ACCENT_BORDER}; }}"
        )
        self.rerun_btn.clicked.connect(self._force_task_rerun)
        rerun_lay.addWidget(self.rerun_btn)
        rerun_lay.addStretch()
        self.rerun_row.hide()
        col.addWidget(self.rerun_row)

        self.progress = QProgressBar()
        self.progress.setObjectName("progressBar")
        self.progress.setMaximum(0)
        self.progress.setFixedHeight(3)
        self.progress.hide()
        col.addWidget(self.progress)

        col.addWidget(self._build_approval_banner())
        col.addWidget(self._build_output_stack(), stretch=1)

        cols.addWidget(left, stretch=1)

        # ---- right step rail -------------------------------------------------
        self.step_tracker = StepTracker()
        cols.addWidget(self.step_tracker)
        return page

    def _make_chip(self, label: str, goal: str) -> QPushButton:
        chip = QPushButton(label)
        chip.setCursor(Qt.PointingHandCursor)
        chip.setStyleSheet(
            f"QPushButton {{ background:{theme.SURFACE};"
            f" border:1px solid {theme.BORDER}; border-radius:20px;"
            f" padding:7px 16px; font-size:12px; color:{theme.TEXT_SECONDARY}; }}"
            f"QPushButton:hover {{ border-color:{theme.ACCENT_BORDER};"
            f" color:{theme.ACCENT}; background:{theme.ACCENT_LIGHT}; }}"
        )
        chip.clicked.connect(lambda _=False, g=goal: self._use_chip(g))
        return chip

    def _use_chip(self, goal: str) -> None:
        self.goal_input.setText(goal)
        self.goal_input.setFocus()

    def _build_input_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName("inputCard")
        card.setStyleSheet(
            f"#inputCard {{ background:{theme.SURFACE};"
            f" border:1px solid {theme.BORDER};"
            f" border-radius:{theme.RADIUS_LG}px; }}"
        )
        theme.apply_drop_shadow(card, "md")

        outer = QVBoxLayout(card)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # -- top row: field + mic + run/stop ---------------------------------
        top = QWidget()
        top.setStyleSheet("background: transparent;")
        top_lay = QHBoxLayout(top)
        top_lay.setContentsMargins(20, 14, 14, 14)
        top_lay.setSpacing(12)

        self.goal_input = QLineEdit()
        self.goal_input.setObjectName("goalInput")
        self.goal_input.setPlaceholderText("What would you like to do?")
        self.goal_input.setStyleSheet(
            f"#goalInput {{ border:none; background:transparent; font-size:15px;"
            f" color:{theme.TEXT_PRIMARY}; padding:2px 0; }}"
        )
        self.goal_input.returnPressed.connect(self._submit_task)
        top_lay.addWidget(self.goal_input, stretch=1)

        self.mic_btn = QPushButton("🎙")
        self.mic_btn.setObjectName("micBtn")
        self.mic_btn.setToolTip("Voice input (F9)")
        self.mic_btn.setCursor(Qt.PointingHandCursor)
        self.mic_btn.setFixedSize(40, 40)
        self.mic_btn.clicked.connect(self._toggle_voice)
        self._style_mic(active=False)
        top_lay.addWidget(self.mic_btn)

        self.send_btn = QPushButton("Run  →")
        self.send_btn.setObjectName("sendBtn")
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setFixedHeight(40)
        self.send_btn.setStyleSheet(
            f"#sendBtn {{ background:{theme.ACCENT}; color:#FFFFFF; border:none;"
            f" border-radius:{theme.RADIUS_MD}px; padding:0 22px;"
            f" font-size:14px; font-weight:600; }}"
            f"#sendBtn:hover {{ background:{theme.ACCENT_HOVER}; }}"
            f"#sendBtn:pressed {{ background:{theme.ACCENT_PRESSED}; }}"
            f"#sendBtn:disabled {{ background:{theme.BORDER_INPUT}; color:{theme.SURFACE}; }}"
        )
        self.send_btn.clicked.connect(self._submit_task)
        top_lay.addWidget(self.send_btn)

        self.stop_btn = QPushButton("■  Stop")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setCursor(Qt.PointingHandCursor)
        self.stop_btn.setFixedHeight(40)
        self.stop_btn.setStyleSheet(
            f"#stopBtn {{ background:{theme.DANGER}; color:#FFFFFF; border:none;"
            f" border-radius:{theme.RADIUS_MD}px; padding:0 22px;"
            f" font-size:14px; font-weight:600; }}"
            f"#stopBtn:hover {{ background:#B91C1C; }}"
        )
        self.stop_btn.clicked.connect(self._stop_task)
        self.stop_btn.hide()
        top_lay.addWidget(self.stop_btn)

        outer.addWidget(top)

        # -- bottom strip: lane, effort, hint --------------------------------
        self.options_bar = QFrame()
        self.options_bar.setObjectName("optionsBar")
        self.options_bar.setStyleSheet(
            f"#optionsBar {{ background:transparent;"
            f" border-top:1px solid {theme.BORDER_LIGHT}; }}"
        )
        opt = QHBoxLayout(self.options_bar)
        opt.setContentsMargins(20, 8, 20, 8)
        opt.setSpacing(12)

        self.lane_toggle = SegmentedToggle(
            ["Headless", "Foreground"], default=0, compact=True,
            on_change=lambda _: self._update_status_context(),
        )
        opt.addWidget(self.lane_toggle)

        divider = QFrame()
        divider.setFixedSize(1, 16)
        divider.setStyleSheet(f"background:{theme.BORDER};")
        opt.addWidget(divider)

        effort_label = QLabel("Effort")
        effort_label.setStyleSheet(
            f"font-size:12px;color:{theme.TEXT_TERTIARY};background:transparent;"
        )
        opt.addWidget(effort_label)

        self.effort_combo = QComboBox()
        self.effort_combo.setObjectName("effortCombo")
        self.effort_combo.addItems(["Low", "Medium", "High"])
        self.effort_combo.setCurrentIndex(0)
        self.effort_combo.setCursor(Qt.PointingHandCursor)
        self.effort_combo.setToolTip(
            "Low: fast, cheap — simple tasks\n"
            "Medium: balanced — multi-step tasks\n"
            "High: thorough — complex research/creation"
        )
        self.effort_combo.setStyleSheet(
            f"#effortCombo {{ background:{theme.INPUT_BG}; border:none;"
            f" border-radius:10px; padding:4px 10px 4px 14px;"
            f" font-size:12px; font-weight:500; color:{theme.TEXT_SECONDARY};"
            f" min-width:64px; }}"
            f"#effortCombo::drop-down {{ border:none; width:18px; }}"
            f"#effortCombo QAbstractItemView {{ background:{theme.SURFACE};"
            f" border:1px solid {theme.BORDER}; border-radius:8px; padding:4px;"
            f" selection-background-color:{theme.ACCENT_LIGHT};"
            f" selection-color:{theme.ACCENT}; outline:none; }}"
        )
        self.effort_combo.currentTextChanged.connect(lambda _: self._update_status_context())
        opt.addWidget(self.effort_combo)

        # --- model selector --------------------------------------------------
        # North star #3 is "the user picks the brain", and until now that meant
        # editing .env and restarting. Every layer below already took the
        # parameter — `build_agent(model_name=…)` → `select_model(model_name=…)`
        # — so this is the missing surface, not a new capability.
        #
        # Populated from KNOWN_MODELS, which is also the list of models
        # verified to support tool calling. A model that cannot call tools
        # cannot drive this agent at all, so an arbitrary free-text box would
        # be a way to break the app rather than a way to choose.
        divider2 = QLabel()
        divider2.setFixedSize(1, 16)
        divider2.setStyleSheet(f"background:{theme.BORDER};")
        opt.addWidget(divider2)

        model_label = QLabel("Model")
        model_label.setStyleSheet(
            f"font-size:12px;color:{theme.TEXT_TERTIARY};background:transparent;"
        )
        opt.addWidget(model_label)

        self.model_combo = QComboBox()
        self.model_combo.setObjectName("modelCombo")
        for full_name, note in _KNOWN_MODELS.items():
            self.model_combo.addItem(_short_model_name(full_name), full_name)
            self.model_combo.setItemData(
                self.model_combo.count() - 1, note, Qt.ToolTipRole
            )
        default_idx = self.model_combo.findData(_DEFAULT_MODEL)
        self.model_combo.setCurrentIndex(max(0, default_idx))
        self.model_combo.setCursor(Qt.PointingHandCursor)
        self.model_combo.setToolTip(
            "Which model runs the task. Hover an entry for its trade-offs.\n"
            "The fast reply and the vision tier have their own models "
            "(ORBIT_ACK_MODEL, _VISION_MODEL) and are not changed here."
        )
        self.model_combo.setStyleSheet(
            f"#modelCombo {{ background:{theme.INPUT_BG}; border:none;"
            f" border-radius:10px; padding:4px 10px 4px 14px;"
            f" font-size:12px; font-weight:500; color:{theme.TEXT_SECONDARY};"
            f" min-width:120px; }}"
            f"#modelCombo::drop-down {{ border:none; width:18px; }}"
            f"#modelCombo QAbstractItemView {{ background:{theme.SURFACE};"
            f" border:1px solid {theme.BORDER}; border-radius:8px; padding:4px;"
            f" selection-background-color:{theme.ACCENT_LIGHT};"
            f" selection-color:{theme.ACCENT}; outline:none; }}"
        )
        self.model_combo.currentTextChanged.connect(lambda _: self._update_status_context())
        opt.addWidget(self.model_combo)

        opt.addStretch()

        hint = QLabel("F9 voice")
        hint.setStyleSheet(
            f"font-size:11px;color:{theme.TEXT_TERTIARY};background:transparent;"
        )
        opt.addWidget(hint)

        outer.addWidget(self.options_bar)
        return card

    def _style_mic(self, *, active: bool) -> None:
        if active:
            self.mic_btn.setStyleSheet(
                f"#micBtn {{ background:{theme.ACCENT}; border:none;"
                f" border-radius:{theme.RADIUS_MD}px; font-size:16px; color:#FFFFFF; }}"
            )
        else:
            self.mic_btn.setStyleSheet(
                f"#micBtn {{ background:{theme.INPUT_BG}; border:none;"
                f" border-radius:{theme.RADIUS_MD}px; font-size:16px; }}"
                f"#micBtn:hover {{ background:{theme.ACCENT_LIGHT}; }}"
            )

    def _build_approval_banner(self) -> QWidget:
        self.approval_banner = QFrame()
        self.approval_banner.setObjectName("approvalBanner")
        self.approval_banner.setStyleSheet(
            f"#approvalBanner {{ background:{theme.WARNING_BG};"
            f" border:1px solid {theme.WARNING_BORDER};"
            f" border-radius:{theme.RADIUS_MD}px; }}"
        )
        lay = QHBoxLayout(self.approval_banner)
        lay.setContentsMargins(16, 10, 12, 10)
        lay.setSpacing(12)

        self.banner_text = QLabel("Action requires confirmation")
        self.banner_text.setStyleSheet(
            f"font-weight:600;font-size:13px;color:{theme.WARNING_TEXT};background:transparent;"
        )
        lay.addWidget(self.banner_text, stretch=1)

        review = QPushButton("Review  →")
        review.setCursor(Qt.PointingHandCursor)
        review.setStyleSheet(
            f"QPushButton {{ background:{theme.WARNING}; color:#FFFFFF; border:none;"
            f" border-radius:{theme.RADIUS_SM}px; padding:6px 14px;"
            f" font-size:12px; font-weight:600; }}"
            f"QPushButton:hover {{ background:#92400E; }}"
        )
        review.clicked.connect(self._open_drawer)
        lay.addWidget(review)

        self.approval_banner.hide()
        return self.approval_banner

    def _build_output_stack(self) -> QWidget:
        self.output_stack = QStackedWidget()
        self.output_stack.setStyleSheet("background: transparent;")

        # Page 0 — idle. No card chrome: the design keeps the empty workbench
        # open, so an outlined box around nothing would only add furniture.
        self.output_stack.addWidget(EmptyState())

        # Page 1 — live output card
        card = QFrame()
        card.setObjectName("outputCard")
        card.setStyleSheet(
            f"#outputCard {{ background:{theme.SURFACE};"
            f" border:1px solid {theme.BORDER};"
            f" border-radius:{theme.RADIUS_LG}px; }}"
        )
        theme.apply_drop_shadow(card, "sm")

        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        header = QFrame()
        header.setObjectName("outputHeader")
        header.setFixedHeight(42)
        header.setStyleSheet(
            f"#outputHeader {{ background:transparent;"
            f" border-bottom:1px solid {theme.BORDER_LIGHT}; }}"
        )
        h = QHBoxLayout(header)
        h.setContentsMargins(18, 0, 14, 0)
        h.setSpacing(10)

        title = QLabel("Live Output")
        title.setStyleSheet(
            f"font-size:12px;font-weight:700;color:{theme.TEXT_PRIMARY};background:transparent;"
        )
        h.addWidget(title)

        self.lane_badge = QLabel("HEADLESS")
        self.lane_badge.setStyleSheet(
            f"font-size:10px;font-weight:600;color:{theme.ACCENT};"
            f"background:{theme.ACCENT_LIGHT};border-radius:8px;padding:3px 10px;"
        )
        h.addWidget(self.lane_badge)

        self.run_indicator = QLabel("● Running")
        self.run_indicator.setStyleSheet(
            f"font-size:11px;font-weight:500;color:{theme.ACCENT};background:transparent;"
        )
        h.addWidget(self.run_indicator)

        h.addStretch()

        for label, slot in (("Copy", self._copy_output), ("Clear", self._clear_output)):
            btn = QPushButton(label)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton {{ background:{theme.INPUT_BG}; border:none;"
                f" border-radius:{theme.RADIUS_SM}px; padding:4px 12px;"
                f" font-size:11px; font-weight:500; color:{theme.TEXT_SECONDARY}; }}"
                f"QPushButton:hover {{ background:{theme.BORDER}; color:{theme.TEXT_PRIMARY}; }}"
            )
            btn.clicked.connect(slot)
            h.addWidget(btn)
            if label == "Copy":
                self.copy_btn = btn

        card_lay.addWidget(header)

        self.output_text = QTextEdit()
        self.output_text.setObjectName("outputText")
        self.output_text.setReadOnly(True)
        self.output_text.setStyleSheet(
            f"#outputText {{ background:transparent; border:none;"
            f" font-family:{theme.FONT_MONO}; font-size:12px;"
            f" color:{theme.TEXT_PRIMARY}; padding:16px 18px;"
            f" selection-background-color:{theme.ACCENT_LIGHT}; }}"
        )
        card_lay.addWidget(self.output_text, stretch=1)

        self.output_stack.addWidget(card)
        return self.output_stack

    def _build_status_bar(self) -> None:
        status = QStatusBar()
        status.setSizeGripEnabled(False)
        status.setFixedHeight(theme.STATUS_HEIGHT)

        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet(
            f"color:{theme.SUCCESS};font-size:9px;background:transparent;"
        )
        status.addWidget(self.status_dot)

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet(
            f"font-size:11px;font-weight:500;color:{theme.TEXT_SECONDARY};background:transparent;"
        )
        status.addWidget(self.status_label)

        sep = QLabel("·")
        sep.setStyleSheet(f"color:{theme.BORDER_INPUT};font-size:11px;background:transparent;")
        status.addWidget(sep)

        self.status_context = QLabel("")
        self.status_context.setStyleSheet(
            f"font-size:11px;color:{theme.TEXT_TERTIARY};background:transparent;"
        )
        status.addWidget(self.status_context)

        self.setStatusBar(status)
        self._update_status_context()

    def _build_overlays(self) -> None:
        """Voice modal and approvals drawer, each over its own scrim.

        Children of the central widget rather than dialogs — see the module
        docstring. Everything starts hidden and is positioned by
        _layout_overlays().
        """
        self.voice_scrim = ScrimWidget(self._central)
        self.voice_scrim.hide()
        self.voice_scrim.mousePressEvent = lambda _e: self._cancel_voice()

        self.voice_modal = VoiceModal(self._central)
        self.voice_modal.hide()
        self.voice_modal.cancel_requested.connect(self._cancel_voice)
        self.voice_modal.commit_requested.connect(self._commit_voice)

        self.drawer_scrim = ScrimWidget(self._central)
        self.drawer_scrim.hide()
        self.drawer_scrim.mousePressEvent = lambda _e: self._close_drawer()

        self.drawer = self._build_drawer()
        self.drawer.hide()

    def _build_drawer(self) -> QFrame:
        drawer = QFrame(self._central)
        drawer.setObjectName("drawer")
        drawer.setFixedWidth(theme.DRAWER_W)
        drawer.setStyleSheet(
            f"#drawer {{ background:{theme.SURFACE};"
            f" border-left:1px solid {theme.BORDER}; }}"
        )
        theme.apply_drop_shadow(drawer, "lg")

        lay = QVBoxLayout(drawer)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        header = QFrame()
        header.setObjectName("drawerHeader")
        # ID selector, not a bare property list: a bare `border-bottom` on a
        # parent is inherited by every child, so the labels below would each
        # draw their own underline.
        header.setStyleSheet(
            f"#drawerHeader {{ border-bottom:1px solid {theme.BORDER}; }}"
        )
        h = QHBoxLayout(header)
        h.setContentsMargins(24, 20, 16, 16)
        h.setSpacing(8)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title = QLabel("Pending Approvals")
        title.setStyleSheet(
            f"font-size:16px;font-weight:800;color:{theme.TEXT_PRIMARY};"
            f"letter-spacing:-0.3px;background:transparent;"
        )
        title_col.addWidget(title)
        self.drawer_subtitle = QLabel("Nothing waiting")
        self.drawer_subtitle.setStyleSheet(
            f"font-size:12px;color:{theme.TEXT_TERTIARY};background:transparent;"
        )
        title_col.addWidget(self.drawer_subtitle)
        h.addLayout(title_col, stretch=1)

        close = QPushButton("✕")
        close.setFixedSize(32, 32)
        close.setCursor(Qt.PointingHandCursor)
        close.setStyleSheet(
            f"QPushButton {{ background:{theme.INPUT_BG}; border:none;"
            f" border-radius:10px; font-size:13px; color:{theme.TEXT_SECONDARY}; }}"
            f"QPushButton:hover {{ background:{theme.BORDER}; color:{theme.TEXT_PRIMARY}; }}"
        )
        close.clicked.connect(self._close_drawer)
        h.addWidget(close, alignment=Qt.AlignTop)

        lay.addWidget(header)

        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(24, 18, 24, 18)
        body_lay.setSpacing(14)

        self.confirm_card = QFrame()
        self.confirm_card.setObjectName("confirmCard")
        self.confirm_card.setStyleSheet(
            f"#confirmCard {{ background:{theme.SURFACE};"
            f" border:1px solid {theme.BORDER};"
            f" border-radius:{theme.RADIUS_XL}px; }}"
        )
        cc = QVBoxLayout(self.confirm_card)
        cc.setContentsMargins(0, 0, 0, 0)
        cc.setSpacing(0)

        self.confirm_shot = QLabel("No screenshot")
        self.confirm_shot.setAlignment(Qt.AlignCenter)
        self.confirm_shot.setMinimumHeight(200)
        self.confirm_shot.setStyleSheet(
            f"background:{theme.INPUT_BG};"
            f"border-bottom:1px solid {theme.BORDER};"
            f"border-top-left-radius:{theme.RADIUS_XL}px;"
            f"border-top-right-radius:{theme.RADIUS_XL}px;"
            f"color:{theme.TEXT_TERTIARY};font-size:11px;"
        )
        cc.addWidget(self.confirm_shot)

        detail = QWidget()
        d = QVBoxLayout(detail)
        d.setContentsMargins(20, 18, 20, 18)
        d.setSpacing(10)

        kicker = QLabel("REQUESTED ACTION")
        kicker.setStyleSheet(
            f"font-size:10px;font-weight:700;color:{theme.TEXT_TERTIARY};"
            f"letter-spacing:0.5px;background:transparent;"
        )
        d.addWidget(kicker)

        self.confirm_heading = QLabel("No pending confirmations")
        self.confirm_heading.setWordWrap(True)
        self.confirm_heading.setStyleSheet(
            f"font-size:14px;font-weight:600;color:{theme.TEXT_PRIMARY};background:transparent;"
        )
        d.addWidget(self.confirm_heading)

        self.confirm_detail = QLabel("")
        self.confirm_detail.setWordWrap(True)
        self.confirm_detail.setStyleSheet(
            f"font-size:12px;color:{theme.WARNING_TEXT};background:{theme.WARNING_BG};"
            f"border:1px solid {theme.WARNING_BORDER};border-radius:{theme.RADIUS_MD}px;"
            f"padding:10px 14px;"
        )
        d.addWidget(self.confirm_detail)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.approve_btn = QPushButton("Approve")
        self.approve_btn.setFixedHeight(44)
        self.approve_btn.setCursor(Qt.PointingHandCursor)
        self.approve_btn.setStyleSheet(
            f"QPushButton {{ background:{theme.SUCCESS}; color:#FFFFFF; border:none;"
            f" border-radius:{theme.RADIUS_MD}px; font-size:14px; font-weight:700; }}"
            f"QPushButton:hover {{ background:#15803D; }}"
            f"QPushButton:disabled {{ background:{theme.BORDER_INPUT}; color:{theme.SURFACE}; }}"
        )
        self.approve_btn.clicked.connect(lambda: self._resolve_confirmation(True))
        buttons.addWidget(self.approve_btn, stretch=1)

        self.reject_btn = QPushButton("Reject")
        self.reject_btn.setFixedHeight(44)
        self.reject_btn.setCursor(Qt.PointingHandCursor)
        self.reject_btn.setStyleSheet(
            f"QPushButton {{ background:{theme.DANGER_BG}; color:{theme.DANGER};"
            f" border:1px solid {theme.DANGER_BORDER};"
            f" border-radius:{theme.RADIUS_MD}px; font-size:14px; font-weight:600; }}"
            f"QPushButton:hover {{ background:#FEE2E2; }}"
            f"QPushButton:disabled {{ background:{theme.INPUT_BG};"
            f" color:{theme.TEXT_TERTIARY}; border-color:{theme.BORDER}; }}"
        )
        self.reject_btn.clicked.connect(lambda: self._resolve_confirmation(False))
        buttons.addWidget(self.reject_btn, stretch=1)

        d.addLayout(buttons)
        cc.addWidget(detail)

        body_lay.addWidget(self.confirm_card)
        body_lay.addStretch()
        lay.addWidget(body, stretch=1)
        return drawer

    def _setup_shortcuts(self) -> None:
        # Esc: cancel voice first, then close the drawer. Only one overlay is
        # ever open at a time, so the order just picks a winner if that changes.
        esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        esc.activated.connect(self._on_escape)

    def _on_escape(self) -> None:
        """Esc is the universal "no, stop".

        Ordered most-recent-intent first. The two additions are the ones that
        make auto-submitted voice safe to live with: a transcript that has not
        gone out yet is cancellable, and speech that is playing is silenceable
        without touching the running task.
        """
        if self.voice_modal.isVisible():
            self._cancel_voice()
        elif self._auto_submit_timer.isActive():
            self._cancel_auto_submit()
        elif self._pending_work is not None:
            # A goal held for the foreground window, not yet sent to the
            # worker. Nothing has touched the mouse; cancelling is free.
            self._cancel_pending_work()
            self._ack.cancel()
            self._speech.stop()
            self._append_plain("\n[not sent]\n", theme.DANGER_TEXT)
            self._task_done(0)
        elif self._speech.is_speaking:
            self._speech.stop()
        elif self._drawer_open:
            self._close_drawer()

    # ===================== overlay geometry =====================

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._layout_overlays()

    def _layout_overlays(self) -> None:
        w, h = self._central.width(), self._central.height()
        self.voice_scrim.setGeometry(0, 0, w, h)
        self.drawer_scrim.setGeometry(0, 0, w, h)
        self.drawer.setGeometry(w - theme.DRAWER_W, 0, theme.DRAWER_W, h)
        if self.voice_modal.isVisible():
            self.voice_modal.adjustSize()
            self.voice_modal.center_on(self._central)

    # ===================== navigation =====================

    def _resume_conversation(self, conversation_id: str) -> None:
        """Reopen a past chat and carry on in it.

        The worker needs nothing for this: sending a goal with an existing
        conversation_id already replays that conversation's stored turns into
        the prompt (`run_task._build_conversation_context`), which is what
        made resuming work before any of this UI existed. All that is missing
        is putting the thread back on screen and pointing new goals at it.

        **A resumed chat is lower fidelity than one you never left.** A live
        conversation continues the real ADK session — the actual tool calls,
        their arguments and their results. A resumed one gets a summary, with
        results clipped. "Do that again" works; "why did that fail" will not
        have the error text.
        """
        if self._task_running:
            return
        turns = db.conversation_turns(conversation_id)
        if not turns:
            return

        self._close_active_conversation()
        self._conversation_id = conversation_id
        self._turn_html = [
            html for html in (self._turn_html_from_row(t) for t in turns) if html
        ][-_MAX_THREAD_TURNS:]

        self.rerun_row.hide()
        self.output_stack.setCurrentIndex(1)
        self._append_thread_so_far()
        self.step_tracker.reset()
        self._show_workbench()
        self.goal_input.setFocus()
        self._set_status("idle")

    def _turn_html_from_row(self, turn: dict) -> str:
        """Render one stored task row as a thread turn.

        Goes through the same `_turn_html_for` the live path uses, so a
        resumed chat is indistinguishable from one still running. Returns ""
        for a turn with nothing to show — an interrupted or crashed task
        leaves a row with an empty result, and a blank card in the thread
        looks like a rendering bug rather than an abandoned turn.
        """
        goal = (turn.get("goal") or "").strip()
        result = (turn.get("result") or "").strip()
        if not goal:
            return ""

        status = (turn.get("status") or "").upper()
        label, tone = self._OUTCOMES.get(status, ("Task finished", "neutral"))
        if tone == "success":
            bg, border, fg = theme.SUCCESS_BG, theme.SUCCESS_BORDER, theme.SUCCESS_TEXT
        elif tone == "danger":
            bg, border, fg = theme.DANGER_BG, theme.DANGER_BORDER, theme.DANGER_TEXT
        else:
            bg, border, fg = theme.INPUT_BG, theme.BORDER, theme.TEXT_SECONDARY

        status_html = (
            f'<div style="background:{bg};border:1px solid {border};'
            f'border-radius:10px;padding:8px 14px;margin-top:14px;">'
            f'<span style="color:{fg};font-size:12px;font-weight:600;">'
            f'● {label}</span></div>'
        )
        body_html = _md_to_html(result) if result else (
            f'<p style="color:{theme.TEXT_TERTIARY};font-size:12px;'
            f'font-style:italic;margin:0;">No result was recorded for this turn.</p>'
        )
        return self._turn_html_for(
            goal_line=f"> {goal}",
            subtitle=f"{turn.get('lane', 'headless')} lane · earlier",
            # No acknowledgement is stored — it was streamed, never persisted.
            ack="",
            body_html=body_html,
            status_html=status_html,
        )

    def _close_active_conversation(self) -> None:
        """Release the current conversation's runner in the worker."""
        if self._worker is None or self._worker.state() == QProcess.NotRunning:
            return
        try:
            payload = json.dumps({"close_conversation": self._conversation_id})
            self._worker.write((payload + chr(10)).encode())
        except Exception:  # noqa: BLE001
            pass

    def _refresh_chat_picker(self) -> None:
        """Repopulate the recent-chats menu from the conversations table."""
        menu = self.chats_btn.menu()
        menu.clear()
        try:
            conversations = db.list_conversations(limit=12)
        except Exception:  # noqa: BLE001
            conversations = []

        usable = 0
        for conv in conversations:
            conv_id = conv.get("conversation_id", "")
            if conv_id == self._conversation_id:
                continue  # already open
            title = (conv.get("title") or "").strip() or conv_id
            if len(title) > 52:
                title = title[:52] + "…"
            action = menu.addAction(title)
            action.triggered.connect(
                lambda _checked=False, cid=conv_id: self._resume_conversation(cid)
            )
            usable += 1
        if not usable:
            menu.addAction("No earlier chats").setEnabled(False)

    def _new_conversation(self) -> None:
        if self._task_running:
            return
# Release the old conversation's runner and its six MCP subprocesses —
        # otherwise starting a new chat leaks a browser nothing comes back to.
        self._close_active_conversation()
        self._conversation_id = f"CONV-{uuid.uuid4().hex[:12]}"
        self._turn_html.clear()
        self.output_text.clear()
        self.step_tracker.reset()
        self.goal_input.clear()
        self.goal_input.setFocus()

    def _on_nav_change(self, value: str) -> None:
        if value == "workbench":
            self.main_stack.setCurrentIndex(0)
        else:
            self.main_stack.setCurrentIndex(1)
            self.history_view.refresh()

    def _show_workbench(self) -> None:
        self.nav_tabs.set_index(0)
        self.main_stack.setCurrentIndex(0)

    def _show_history(self) -> None:
        # Goes through the toggle so the nav pill and the page stack can never
        # disagree about which tab is current.
        self.nav_tabs.set_index(1)
        self.main_stack.setCurrentIndex(1)
        self.history_view.refresh()

    def _handle_rerun_task(self, goal: str, lane: str) -> None:
        self._show_workbench()
        self.lane_toggle.set_value(lane)
        self.goal_input.setText(goal)
        self.goal_input.setFocus()

    # ===================== the acknowledgement track =====================

    def _setup_speech(self) -> None:
        """Wire the fast reply and its voice.

        This is the second of the two tracks a goal starts. The work track
        (the warm worker) takes seconds before it says anything — MCP toolsets
        connect in ~3.3s and the first model turn carries an ~8,000-token
        prompt — which is fine for doing a job and far too slow to sound like
        an assistant answering. The acknowledgement track answers a smaller
        question ("what did I just hear") with a small model and no tools, in
        about a second, and speaks it while the work track is still starting.

        Both are prewarmed here rather than lazily, because the two costs they
        would otherwise pay on first use — TLS setup and opening the audio
        device — measured about 0.9s each, and the first spoken request of a
        session is the worst possible time to pay either.
        """
        self._ack = AckController(self)
        self._ack.delta.connect(self._on_ack_delta)
        self._ack.completed.connect(self._on_ack_completed)
        self._ack.classified.connect(self._on_ack_classified)
        self._ack.summary_completed.connect(self._on_summary_ready)
        self._ack.failed.connect(self._on_ack_failed)

        self._speech = SpeechPlayer(self)
        self._speech.failed.connect(self._on_speech_failed)

        # Safety net for the deferred dispatch below. If the acknowledgement
        # never classifies — the network is down, the provider is slow — the
        # work track must still run. Failing to dispatch is the one outcome
        # this whole arrangement must never produce.
        self._dispatch_guard = QTimer(self)
        self._dispatch_guard.setSingleShot(True)
        self._dispatch_guard.timeout.connect(self._dispatch_pending_work)

        self._ack.prewarm()
        self._speech.prewarm()

    # -- deferred dispatch of the work track ---------------------------------
    #
    # The work track is NOT sent the moment a goal is submitted. It waits for
    # the acknowledgement's [CHAT]/[TASK] classification, which lands about a
    # second in, on the stream's first tokens.
    #
    # That second is bought deliberately. A purely social turn — "hi",
    # "thanks", "what can you do" — does not need six MCP servers spawned and
    # an 8,000-token prompt sent to answer it, and skipping the work track
    # takes those from ~9s to ~1.3s. Real tasks pay one second on top of a
    # ~9s job, during which the user is not waiting in silence: the
    # acknowledgement is already streaming and about to be spoken.
    #
    # Every failure path dispatches. Classification failure, provider error,
    # and the guard timer all end in `_dispatch_pending_work`, because a task
    # that silently never ran is far worse than a wasted worker turn.

    def _defer_work(
        self, payload: dict, *, hold_ms: int, hold_full: bool = False
    ) -> None:
        """Queue a goal for the worker, to be sent when the hold ends.

        `hold_full` says what the timer MEANS, and it is passed explicitly
        rather than inferred from `hold_ms` — the two durations do not order
        the way the intent does. The headless guard (3s) is deliberately
        *longer* than the foreground hold (2.5s), because one is a
        rarely-reached ceiling for a dead provider and the other is a window
        that always elapses. Deriving intent from duration got this backwards.

        hold_full=False: the timer is a fallback. Classification dispatches
            as soon as it arrives, usually ~1s in.
        hold_full=True: the timer is the dispatcher. Classification does not
            short-circuit it, so the spoken acknowledgement lands and can be
            countermanded before anything touches the real mouse.
        """
        self._pending_work = payload
        self._hold_for_full_window = hold_full
        self._dispatch_guard.start(hold_ms)

    def _dispatch_pending_work(self) -> None:
        """Send the queued goal to the worker. Idempotent."""
        self._dispatch_guard.stop()
        payload = self._pending_work
        if payload is None:
            return
        self._pending_work = None
        self._ensure_worker()
        self._worker.write((json.dumps(payload) + "\n").encode())  # type: ignore[union-attr]

    def _cancel_pending_work(self) -> None:
        """Drop a queued goal without sending it (a chat turn, or a Stop)."""
        self._dispatch_guard.stop()
        self._pending_work = None

    def _on_ack_classified(self, chat_only: bool) -> None:
        # Logged for every turn, because a wrong classification is otherwise
        # undetectable after the fact: a bad [CHAT] leaves no task row, no
        # event row, and no trace anywhere that a goal was ever submitted.
        print(
            f"[classify] chat_only={chat_only} goal={self._last_goal[:80]!r}",
            flush=True,
        )
        if self._pending_work is None:
            return  # already dispatched, or nothing queued
        if not chat_only:
            if not self._hold_for_full_window:
                self._dispatch_pending_work()
            # else: the foreground window is still open; the guard timer sends it
            return

        # Purely social: the acknowledgement IS the answer, so there is no
        # work track for this turn.
        #
        # The task is NOT finished here, even though nothing more will run.
        # `classified` fires on the stream's first tokens, with the reply still
        # arriving — and `_task_done` re-renders the output pane wholesale.
        # Finishing now would render a half-written sentence and then append
        # the rest underneath it. `_on_ack_completed` closes the turn instead.
        goal = self._pending_work.get("goal", "")
        self._cancel_pending_work()
        self._chat_turn = True
        self._chat_only_goal = goal

    def _on_ack_delta(self, text: str) -> None:
        if not self._ack_text:
            self._append_plain("\n", theme.TEXT_PRIMARY)
        self._ack_text += text
        self._append_plain(text, theme.ACCENT_PRESSED)

    def _on_ack_completed(self, text: str) -> None:
        self._append_plain("\n\n", theme.TEXT_PRIMARY)
        self._ack_text = text
        if self._spoken_submission and text:
            self._speech.enqueue(text)

        if self._chat_turn:
            # A social turn ends here: no work track ran, and this is the last
            # thing that will be written. The goal is put back in the box so
            # pressing Enter on it re-submits and forces the work track — the
            # whole recovery path for a wrong classification, and it needs no
            # new widget.
            self._chat_turn = False
            self.goal_input.setText(self._chat_only_goal)
            self.rerun_row.show()
            self._task_done(0)

    def _on_summary_ready(self, text: str) -> None:
        """Speak the finished task's result, in sentence-sized pieces.

        Split so playback starts on the first sentence rather than waiting for
        the whole thing to synthesise — the reason SpeechPlayer is a queue.
        """
        if not text or not self._spoken_submission:
            return
        for sentence in split_sentences(text):
            self._speech.enqueue(sentence)

    def _force_task_rerun(self) -> None:
        """Run the goal the classifier called social, as a task, now.

        Re-submitting the same text is already what `_submit_task` treats as
        an override — it skips the classification wait entirely — so this only
        has to put the text back and press send.
        """
        self.rerun_row.hide()
        if not self._chat_only_goal or self._task_running:
            return
        self.goal_input.setText(self._chat_only_goal)
        self._submit_task()

    def _on_ack_failed(self, message: str) -> None:
        # Deliberately quiet in the output pane. The acknowledgement is a
        # courtesy running alongside the real task; a failed one must not look
        # like a failed task, and the work track is entirely unaffected.
        print(f"[ack] {message}", flush=True)

    def _on_speech_failed(self, message: str) -> None:
        # The budget guard reports through here too, and that one the user
        # does need to see — silence would otherwise be indistinguishable from
        # a broken microphone setup.
        if "budget" in message.lower():
            self._append_plain(f"\n[speech] {message}\n", theme.DANGER_TEXT)
        else:
            print(f"[speech] {message}", flush=True)

    # ===================== voice =====================

    def _setup_voice(self) -> None:
        self._voice_ctrl = VoiceController(self)
        self._voice_ctrl.session_started.connect(self._on_voice_started)
        self._voice_ctrl.session_stopped.connect(self._on_voice_stopped)
        self._voice_ctrl.volume_rms.connect(self.voice_modal.orb.set_volume)
        self._voice_ctrl.transcript_interim.connect(self._on_transcript_interim)
        self._voice_ctrl.transcript_final_segment.connect(self._on_transcript_final)
        self._voice_ctrl.transcript_ready.connect(self._on_transcript_ready)
        self._voice_ctrl.budget_exceeded.connect(self._on_voice_budget_exceeded)

        self._auto_submit_timer = QTimer(self)
        self._auto_submit_timer.setSingleShot(True)
        self._auto_submit_timer.timeout.connect(self._auto_submit)

        self._hotkey_filter = HotkeyFilter()
        self._hotkey_filter.toggled.connect(self._toggle_voice)
        QApplication.instance().installNativeEventFilter(self._hotkey_filter)

    def _toggle_voice(self) -> None:
        """F9 / mic button. Starting to listen always stops Orbit talking.

        Barge-in: without it, pressing the hotkey while a reply is playing
        queues you behind it, and the assistant reads out an answer you have
        already moved on from. Interrupting is how people actually talk, and
        it is also the fastest way to shut it up.
        """
        if not self._voice_ctrl:
            return
        if not self._voice_ctrl.is_active:
            self._speech.stop()
            self._cancel_auto_submit()
        self._voice_ctrl.toggle()

    def _on_voice_budget_exceeded(self, message: str) -> None:
        """The mic refused to open because the day's STT budget is spent.

        Surfaced loudly rather than logged: pressing F9 and getting silence
        is indistinguishable from a broken microphone, and the user would
        reasonably go looking for a hardware problem that is not there."""
        self.output_stack.setCurrentIndex(1)
        self._append_plain(f"\n[voice] {message}\n", theme.DANGER_TEXT)
        self._set_status("error")

    def _cancel_voice(self) -> None:
        if self._voice_ctrl:
            self._voice_ctrl.cancel()

    def _commit_voice(self) -> None:
        """"Use transcript" — same commit path as a second F9 press."""
        if self._voice_ctrl:
            self._voice_ctrl.toggle()

    def _on_voice_started(self) -> None:
        self._committed_text = ""
        self.voice_modal.begin()
        self.voice_scrim.show()
        self.voice_scrim.raise_()
        self.voice_modal.show()
        self.voice_modal.adjustSize()
        self.voice_modal.center_on(self._central)
        self.voice_modal.raise_()
        self._style_mic(active=True)
        self._set_status("recording")

    def _on_voice_stopped(self) -> None:
        self.voice_modal.end()
        self.voice_modal.hide()
        self.voice_scrim.hide()
        self._style_mic(active=False)
        self._set_status("running" if self._task_running else "idle")

    def _on_transcript_interim(self, text: str) -> None:
        display = f"{self._committed_text} {text}".strip() if self._committed_text else text
        self.voice_modal.set_transcript(display)

    def _on_transcript_final(self, text: str) -> None:
        self._committed_text = f"{self._committed_text} {text}".strip()
        self.voice_modal.set_transcript(self._committed_text)

    def _on_transcript_ready(self, text: str) -> None:
        """A finished transcript submits itself.

        This is what makes voice the primary input rather than a fancy way to
        fill a text box. Requiring Enter after speaking meant every spoken goal
        ended at the keyboard, which is the thing voice exists to avoid.

        The delay before submitting is short on purpose. It is not the safety
        mechanism — the spoken acknowledgement is, arriving about a second
        later and saying out loud what was understood, with Esc live the whole
        time. A window long enough to *read* a transcript would push first
        audio from ~2s to ~4.5s and undo the entire point.
        """
        if not text:
            return
        self.goal_input.setText(text)
        self.goal_input.setFocus()
        # Marks the goal now in the box as spoken, so the acknowledgement for
        # it is spoken back rather than only shown. Cleared on submit.
        self._voice_originated = True

        if self._task_running:
            return  # a task is already running; leave the text for the user
        self._append_plain(f"\n[heard: {text}]\n", theme.TEXT_TERTIARY)
        self._auto_submit_timer.start(_AUTO_SUBMIT_MS)

    def _cancel_auto_submit(self) -> None:
        if self._auto_submit_timer.isActive():
            self._auto_submit_timer.stop()
            self._append_plain("[cancelled]\n", theme.DANGER_TEXT)

    def _auto_submit(self) -> None:
        if self._task_running or not self.goal_input.text().strip():
            return
        self._submit_task()

    # ===================== task submission =====================

    def _ensure_worker(self) -> None:
        """Spawn the warm worker if it is not already running.

        The worker runs `orbit.run_task --serve`, reading one JSON goal line
        per task from stdin and emitting [TASK:DONE exit_code] when done, so
        Python imports and the event loop pay their cost once instead of per
        task (~7-8 s of cold start each).
        """
        if self._worker is not None and self._worker.state() != QProcess.NotRunning:
            return
        w = QProcess(self)
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

        # Re-submitting the exact goal the classifier just called social is
        # how the user overrides it. One repeat forces the work track and skips
        # the classification wait entirely.
        forced = goal == self._chat_only_goal
        self._chat_only_goal = ""

        # --- track one: the acknowledgement ---------------------------------
        # Started FIRST, before the worker is written to. This one wins by
        # several seconds: it speaks in ~1.3s while the worker is still
        # connecting MCP servers. Anything that makes this line wait on the
        # work track defeats the entire arrangement.
        self._spoken_submission = self._voice_originated
        self._voice_originated = False
        self._ack_text = ""
        self._chat_turn = False
        self._result_status = ""
        self._result_text = ""
        self._last_goal = goal
        self.rerun_row.hide()
        self._speech.stop()  # a new goal supersedes whatever is still playing
        self._ack.start(goal, self._conversation_id)

        # --- track two: the actual work -------------------------------------
        payload = {
            "goal": goal,
            "lane": lane,
            "effort": effort,
            "model": self.model_combo.currentData(),
            "conversation_id": self._conversation_id,
        }
        if forced:
            self._pending_work = payload
            self._dispatch_pending_work()
        else:
            # Held until the acknowledgement classifies the turn — see
            # "deferred dispatch" above. The hold is longer in the foreground
            # lane: that is the one where a misheard goal moves the real mouse
            # and keyboard, so the spoken acknowledgement gets time to land
            # and be countermanded with Esc before anything touches the OS.
            # Headless work is inert for its first several seconds (MCP
            # connect), so it does not need the same margin.
            foreground = lane == "foreground"
            self._defer_work(
                payload,
                hold_ms=_FOREGROUND_HOLD_MS if foreground else _DISPATCH_GUARD_MS,
                hold_full=foreground,
            )

        self._task_running = True
        self._task_started_at = time.time()
        self._raw_buffer = ""
        self._goal_header = f"> {goal}\n  ({lane} | effort: {effort})\n"

        # Deliberately NOT output_text.clear(): the finished turns above stay
        # on screen while this one streams in underneath them. The live text
        # is transient anyway — _render_final_output replaces the pane with
        # the full thread when the turn ends.
        self._append_thread_so_far()
        self.output_stack.setCurrentIndex(1)
        # begin_task resets internally and opens a running step, so the rail
        # is alive from submission rather than from the first tool call.
        self.step_tracker.begin_task()

        self.lane_badge.setText(lane.upper())
        self.run_indicator.setText("● Running")
        self.run_indicator.setStyleSheet(
            f"font-size:11px;font-weight:500;color:{theme.ACCENT};background:transparent;"
        )

        self._append_plain(f"> {goal}\n", theme.ACCENT)
        self._append_plain(f"  {lane} lane · effort: {effort}\n\n", theme.TEXT_TERTIARY)

        self.goal_input.clear()
        self.goal_input.setEnabled(False)
        self.chips_row.hide()
        self.send_btn.hide()
        self.stop_btn.show()
        self.progress.show()
        self._set_status("running")

    def _append_thread_so_far(self) -> None:
        """Re-render finished turns, then leave the cursor at the end so the
        new turn's streamed output appends beneath them."""
        self.output_text.clear()
        if not self._turn_html:
            return
        separator = (
            f'<div style="border-top:1px solid {theme.BORDER};'
            f'margin:22px 0 18px 0;"></div>'
        )
        self.output_text.setHtml(
            f'<div style="font-family:{theme.FONT_FAMILY};padding:4px;">'
            f'{separator.join(self._turn_html)}{separator}</div>'
        )
        cursor = self.output_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.output_text.setTextCursor(cursor)

    def _stop_task(self) -> None:
        # Stop means both tracks. Leaving the acknowledgement to finish
        # speaking after the user pressed Stop would be the clearest possible
        # way to look like the button did nothing.
        self._ack.cancel()
        self._speech.stop()
        self._cancel_auto_submit()
        # A goal still held for classification has not reached the worker, so
        # killing the process below would not stop it — it would arrive after.
        self._cancel_pending_work()
        if self._task_running:
            # So the status card reads "cancelled" rather than "failed". The
            # user asking a task to stop is not the task going wrong, and
            # colouring it red says otherwise.
            self._result_status = "CANCELLED"
        if self._worker and self._task_running:
            self._append_plain("\n[task stopped by user]\n", theme.TEXT_SECONDARY)
            # _worker_exited fires via finished and calls _task_done exactly once.
            self._worker.kill()

    def _handle_orbit_event(self, payload: str) -> None:
        """Act on one `[ORBIT]{json}` progress event from the worker.

        These arrive live, as ADK yields them — the whole point of the change
        that introduced them. Before it, the worker used ``run_debug``, which
        buffers every event and returns them only once the task is over, so
        the step rail could not fill in until there was nothing left to watch.

        Never raises: a malformed or unknown event is telemetry, and dropping
        it silently is correct. The task's real output still arrives as prose
        on the same stream, and `[TASK:DONE]` still ends the task, so nothing
        here is load-bearing for correctness.
        """
        try:
            event = json.loads(payload)
        except (ValueError, TypeError):
            return
        kind = event.get("kind")

        if kind == "tool_call":
            self.step_tracker.handle_tool_call(str(event.get("tool", "")))
        elif kind == "tool_result":
            self.step_tracker.complete_current()
        elif kind == "text_delta":
            # A delta, not a running total — appended as it arrives so the
            # answer types itself out. Deliberately NOT added to _raw_buffer:
            # _render_final_output re-renders from that buffer at the end,
            # using the canonical prose the worker prints, and counting the
            # deltas too would duplicate the whole answer.
            self._append_plain(str(event.get("text", "")), theme.TEXT_PRIMARY)
        elif kind == "result":
            # The worker emits this before tearing down its six MCP server
            # subprocesses, which measured ~2s. Unlocking here rather than on
            # [TASK:DONE] hands those seconds back to the user, who would
            # otherwise be looking at a finished answer with a dead input box.
            self._on_result_ready(
                str(event.get("status", "")), str(event.get("text", ""))
            )

    def _on_result_ready(self, status: str, text: str = "") -> None:
        """Re-enable input as soon as the answer exists, before teardown."""
        if not self._task_running:
            return
        # Kept for the status card, which renders later on [TASK:DONE]. The
        # exit code alone cannot distinguish a provider outage from a cancel
        # from a crash; this can.
        self._result_status = status
        self._result_text = text
        # Started here rather than on [TASK:DONE] so the summary generates
        # during the worker's ~2s teardown instead of after it.
        if text and self._spoken_submission:
            self._ack.summarize(text, goal=self._last_goal)
        self.goal_input.setEnabled(True)
        self.chips_row.show()
        self.send_btn.show()
        self.stop_btn.hide()
        ok = status == "COMPLETED"
        self.run_indicator.setText("● Completed" if ok else f"● {status.title()}")
        self.run_indicator.setStyleSheet(
            f"font-size:11px;font-weight:500;"
            f"color:{theme.SUCCESS if ok else theme.DANGER};background:transparent;"
        )
        self.goal_input.setFocus()

    def _read_stdout(self) -> None:
        if not self._worker:
            return
        data = self._worker.readAllStandardOutput()
        text = bytes(data).decode("utf-8", errors="replace")
        for line in text.splitlines(keepends=True):
            stripped = line.strip()

            done_m = self._TASK_DONE_RE.search(stripped)
            if done_m:
                # Sentinel — never goes into _raw_buffer, which is what the
                # final render re-parses.
                self._task_done(int(done_m.group(1)))
                continue

            if stripped.startswith(_EVENT_PREFIX):
                # Structured progress. Kept out of _raw_buffer for the same
                # reason as the sentinel: the final render re-parses that
                # buffer and would print raw JSON into the output pane.
                self._handle_orbit_event(stripped[len(_EVENT_PREFIX):])
                continue

            self._raw_buffer += line

            step_m = self._STEP_RE.match(stripped)
            if step_m:
                self.step_tracker.handle_marker(
                    step_m.group(1), step_m.group(2).strip(), (step_m.group(3) or "").strip()
                )
                continue

            tool_m = self._TOOL_CALL_RE.search(stripped)
            if tool_m:
                self.step_tracker.handle_tool_call(tool_m.group(1))

            m = self._SCREENSHOT_RE.search(line)
            if m:
                self._append_plain(line, theme.TEXT_SECONDARY)
                self._insert_screenshot(m.group(1).strip())
            else:
                self._append_plain(line, theme.TEXT_PRIMARY)

    def _read_stderr(self) -> None:
        if not self._worker:
            return
        data = self._worker.readAllStandardError()
        text = bytes(data).decode("utf-8", errors="replace")
        for line in text.splitlines(keepends=True):
            if any(noise in line for noise in self._STDERR_NOISE):
                continue
            if line.strip():
                self._append_plain(line, theme.DANGER_TEXT)

    def _insert_screenshot(self, path: str) -> None:
        p = Path(path)
        if not p.exists():
            return
        cursor = self.output_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertHtml(
            f'<div style="margin:12px 0;"><img src="file:///{p.as_posix()}" '
            f'width="520" style="border:1px solid {theme.BORDER};border-radius:14px;"></div>'
        )
        self.output_text.setTextCursor(cursor)
        if self._auto_scroll:
            self.output_text.ensureCursorVisible()

    # Everything the runtime can tell us about how a task ended, and what the
    # user should be told. `[TASK:DONE <exit_code>]` only distinguishes zero
    # from non-zero — the `result` event carries the real status and message,
    # and this is where the two are reconciled.
    #
    # The distinction matters because the three failure modes want different
    # things from the user: a provider outage means try again in a minute, a
    # cancel means nothing went wrong, and a crash means read the message.
    # Rendering all three as "Task failed (exit 1)" told them none of that.
    _OUTCOMES = {
        "COMPLETED": ("Task completed successfully", "success"),
        "CANCELLED": ("Task cancelled", "neutral"),
        "FAILED":    ("Task failed", "danger"),
    }

    def _status_card_html(self, exit_code: int) -> str:
        """The coloured card at the foot of a finished task."""
        status = self._result_status or ("COMPLETED" if exit_code == 0 else "FAILED")
        label, tone = self._OUTCOMES.get(status, ("Task failed", "danger"))

        if tone == "success":
            bg, border, fg = theme.SUCCESS_BG, theme.SUCCESS_BORDER, theme.SUCCESS_TEXT
        elif tone == "neutral":
            bg, border, fg = theme.INPUT_BG, theme.BORDER, theme.TEXT_SECONDARY
        else:
            bg, border, fg = theme.DANGER_BG, theme.DANGER_BORDER, theme.DANGER_TEXT

        detail = ""
        if tone == "danger":
            # The worker's own words, not an exit code. run_task classifies by
            # failure class — a provider outage says so, a tool bug names its
            # exception type — and that text is the only thing that tells the
            # user which of those happened.
            reason = (self._result_text or "").strip()
            if not reason:
                reason = (
                    f"The worker exited with code {exit_code} without reporting a "
                    "reason. Check the terminal it was started from."
                )
            detail = (
                f'<p style="color:{fg};font-size:12px;font-weight:400;'
                f'margin:6px 0 0 0;">{_inline_md(reason[:600])}</p>'
            )

        return (
            f'<div style="background:{bg};border:1px solid {border};'
            f'border-radius:10px;padding:10px 14px;margin-top:14px;">'
            f'<span style="color:{fg};font-size:12px;font-weight:600;">● {label}</span>'
            f'{detail}</div>'
        )

    def _task_done(self, exit_code: int) -> None:
        """Restore the UI after a task ends (normal finish or kill)."""
        if not self._task_running:
            return  # guard against the sentinel and finished() both firing
        self._task_running = False

        for s in self.step_tracker.steps:
            if s.status == StepStatus.RUNNING:
                s.status = StepStatus.DONE if exit_code == 0 else StepStatus.FAILED
                if not s.finished_at:
                    s.finished_at = time.time()
        self.step_tracker._refresh_all_widgets()

        self.goal_input.setEnabled(True)
        self.chips_row.show()
        self.send_btn.show()
        self.stop_btn.hide()
        self.progress.hide()

        ok = exit_code == 0
        self.run_indicator.setText("● Completed" if ok else "● Failed")
        self.run_indicator.setStyleSheet(
            f"font-size:11px;font-weight:500;"
            f"color:{theme.SUCCESS if ok else theme.DANGER};background:transparent;"
        )

        self._render_final_output(exit_code)
        self._set_status("idle" if ok else "error")
        self._refresh_data()
        self.goal_input.setFocus()

    def _worker_exited(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        """Worker died (crash or explicit kill); next submit respawns it."""
        self._worker = None
        if self._task_running:
            self._task_done(exit_code)

    def _turn_html_for(
        self,
        *,
        goal_line: str,
        subtitle: str,
        ack: str,
        body_html: str,
        status_html: str,
    ) -> str:
        """One turn's rendered HTML.

        Shared by the live path and by conversation replay, so a resumed chat
        is indistinguishable from one you never left. Building the two
        separately is how they drift into looking subtly different, which
        makes a resumed chat feel like a transcript rather than the chat.
        """
        header = (
            f'<div style="background:{theme.ACCENT_LIGHT};border:1px solid {theme.ACCENT_BORDER};'
            f'border-radius:14px;padding:12px 16px;margin-bottom:14px;">'
            f'<p style="color:{theme.ACCENT_PRESSED};font-size:15px;font-weight:700;margin:0 0 2px 0;">'
            f'{_inline_md(goal_line)}</p>'
            f'<p style="color:{theme.TEXT_SECONDARY};font-size:12px;margin:0;">'
            f'{_inline_md(subtitle)}</p>'
            f'</div>'
        )
        if ack:
            header += (
                f'<p style="color:{theme.ACCENT_PRESSED};font-size:13px;'
                f'font-style:italic;margin:0 0 14px 2px;">{_inline_md(ack)}</p>'
            )
        return f"{header}{body_html}{status_html}"

    def _render_final_output(self, exit_code: int) -> None:
        # The acknowledgement was streamed straight into the output pane while
        # the task ran, and this render replaces the pane wholesale from
        # _raw_buffer — which deliberately never held it. Passing it to
        # _turn_html_for is what stops the fast reply vanishing the moment the
        # slow one lands.
        lines = self._goal_header.split(chr(10))
        goal_line = lines[0]
        subtitle = lines[1] if len(lines) > 1 else ""

        body = self._raw_buffer.replace("\r\n", "\n")
        sections = re.split(r"\n-{4,}\n", body)

        body_html = ""
        for section in sections:
            s = section.strip()
            if not s or s.startswith("> ") or s.startswith("(working"):
                continue
            if s.startswith("task_id:"):
                body_html += (
                    f'<p style="color:{theme.TEXT_TERTIARY};font-size:11px;margin:12px 0 0 0;'
                    f'font-family:{theme.FONT_MONO};">{_inline_md(s.splitlines()[0])}</p>'
                )
                continue
            cleaned = "\n".join(
                l for l in s.splitlines() if not self._STEP_RE.match(l.strip())
            ).strip()
            if cleaned:
                body_html += _md_to_html(cleaned)

        turn_html = self._turn_html_for(
            goal_line=goal_line,
            subtitle=subtitle,
            ack=self._ack_text,
            body_html=body_html,
            status_html=self._status_card_html(exit_code),
        )

        # The pane is a THREAD, not a single exchange. Finished turns are kept
        # and re-rendered above this one, so a conversation reads as a
        # conversation — you can see what you asked two turns ago and what it
        # said, which is most of what "conversational, not fire-and-forget"
        # means from the user's side.
        #
        # Kept as rendered HTML rather than re-derived from the DB: the pane
        # shows exactly what was shown at the time, including the streamed
        # acknowledgement, which no table records.
        self._turn_html.append(turn_html)
        del self._turn_html[:-_MAX_THREAD_TURNS]

        separator = (
            f'<div style="border-top:1px solid {theme.BORDER};margin:22px 0 18px 0;">'
            f'</div>'
        )
        body = separator.join(self._turn_html)
        self.output_text.setHtml(
            f'<div style="font-family:{theme.FONT_FAMILY};padding:4px;">{body}</div>'
        )
        # Scrolled to the newest turn, not the top: in a thread the thing you
        # want is the answer that just arrived.
        cursor = self.output_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.output_text.setTextCursor(cursor)
        self.output_text.ensureCursorVisible()

    # ===================== output helpers =====================

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

    def _copy_output(self) -> None:
        text = self.output_text.toPlainText()
        if text:
            QGuiApplication.clipboard().setText(text)
            self.copy_btn.setText("Copied")
            QTimer.singleShot(1400, lambda: self.copy_btn.setText("Copy"))

    def _clear_output(self) -> None:
        self.output_text.clear()
        self._turn_html.clear()
        self._raw_buffer = ""
        self.step_tracker.reset()
        self.output_stack.setCurrentIndex(0)

    # ===================== approvals =====================

    def _open_drawer(self) -> None:
        self._drawer_open = True
        self._layout_overlays()
        self.drawer_scrim.show()
        self.drawer_scrim.raise_()
        self.drawer.show()
        self.drawer.raise_()

        # Slide in from the right edge.
        end = QPoint(self._central.width() - theme.DRAWER_W, 0)
        self._drawer_anim = QPropertyAnimation(self.drawer, b"pos")
        self._drawer_anim.setDuration(220)
        self._drawer_anim.setStartValue(QPoint(self._central.width(), 0))
        self._drawer_anim.setEndValue(end)
        self._drawer_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._drawer_anim.start()

    def _close_drawer(self) -> None:
        if not self._drawer_open:
            return
        self._drawer_open = False
        self._drawer_anim = QPropertyAnimation(self.drawer, b"pos")
        self._drawer_anim.setDuration(180)
        self._drawer_anim.setStartValue(self.drawer.pos())
        self._drawer_anim.setEndValue(QPoint(self._central.width(), 0))
        self._drawer_anim.setEasingCurve(QEasingCurve.InCubic)
        self._drawer_anim.finished.connect(self.drawer.hide)
        self._drawer_anim.start()
        self.drawer_scrim.hide()

    def _toggle_drawer(self) -> None:
        self._close_drawer() if self._drawer_open else self._open_drawer()

    def _refresh_data(self) -> None:
        db.init_db()
        self._refresh_confirmations()

    def _refresh_confirmations(self) -> None:
        pending = db.list_pending_confirmations()
        count = len(pending)
        self._pending_count = count
        self.bell_btn.set_count(count)

        if count:
            row = pending[0]
            self._current_confirm_id = row["confirmation_id"]
            action = row.get("action", "UI action")
            self.banner_text.setText(f"Confirmation required: {action}")
            self.approval_banner.show()

            self.drawer_subtitle.setText(
                f"{count} action{'s' if count != 1 else ''} need"
                f"{'' if count != 1 else 's'} your confirmation"
            )
            self.confirm_heading.setText(
                f"{action} — {row.get('candidate_label') or 'low-confidence target'}"
            )
            ttl = load_windows_control_policy().get("approval_token_ttl_seconds", 120)
            self.confirm_detail.setText(
                f"Approving grants ONE single action, valid for {ttl}s. "
                f"Task {row['task_id']}."
            )
            self.confirm_detail.show()
            self._render_confirm_shot(row)
            self.approve_btn.setEnabled(True)
            self.reject_btn.setEnabled(True)
        else:
            self._current_confirm_id = None
            self.approval_banner.hide()
            self.drawer_subtitle.setText("Nothing waiting")
            self.confirm_heading.setText("No pending confirmations")
            self.confirm_detail.hide()
            self.confirm_shot.setPixmap(QPixmap())
            self.confirm_shot.setText("No screenshot")
            self.approve_btn.setEnabled(False)
            self.reject_btn.setEnabled(False)

    def _render_confirm_shot(self, row: dict) -> None:
        path = row.get("screenshot_path")
        pixmap = QPixmap(path) if path else QPixmap()
        if pixmap.isNull():
            self.confirm_shot.setPixmap(QPixmap())
            self.confirm_shot.setText("No screenshot available")
            return
        box = row.get("candidate_box")
        if box:
            painter = QPainter(pixmap)
            painter.setPen(QPen(QColor(theme.WARNING), 3))
            left, top, right, bottom = box
            painter.drawRect(QRect(left, top, right - left, bottom - top))
            painter.end()
        self.confirm_shot.setText("")
        self.confirm_shot.setPixmap(
            pixmap.scaled(
                theme.DRAWER_W - 48, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation
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
            # Already decided — by the REPL asker or a second dashboard.
            # A race, not an error; the refresh below shows the truth.
            pass
        self._refresh_confirmations()

    # ===================== status bar =====================

    def _set_status(self, state: str) -> None:
        colors = {
            "idle": theme.SUCCESS,
            "running": theme.WARNING,
            "recording": theme.DANGER,
            "error": theme.DANGER,
        }
        labels = {
            "idle": "Ready",
            "running": "Running",
            "recording": "Recording",
            "error": "Failed",
        }
        color = colors.get(state, theme.TEXT_TERTIARY)
        self._status_state = state
        self.status_dot.setStyleSheet(f"color:{color};font-size:9px;background:transparent;")
        self.status_label.setText(labels.get(state, state))
        self.status_label.setStyleSheet(
            f"font-size:11px;font-weight:{'600' if state in ('recording', 'error') else '500'};"
            f"color:{color if state in ('recording', 'error') else theme.TEXT_SECONDARY};"
            f"background:transparent;"
        )
        self._update_status_context()

    def _update_status_context(self) -> None:
        """Right half of the status line: lane · effort [· elapsed]."""
        if not hasattr(self, "status_context"):
            return
        lane = self.lane_toggle.value().capitalize()
        effort = self.effort_combo.currentText().lower()
        parts = [f"{lane} · {effort} effort"]
        if self._task_running and self._task_started_at:
            parts.append(f"{format_duration(time.time() - self._task_started_at)} elapsed")
        self.status_context.setText("  ·  ".join(parts))

    def _tick_status(self) -> None:
        if self._task_running:
            self._update_status_context()

    def closeEvent(self, event) -> None:  # noqa: N802
        """Release what the two voice tracks are holding.

        The speech player keeps the audio output device open between replies
        (re-opening it costs 0.90s), and the acknowledgement controller holds
        a pooled HTTPS connection. Both are daemon-threaded and would die with
        the process anyway; closing them explicitly means the audio device is
        handed back promptly rather than whenever the interpreter gets round
        to it, which is visible to other applications.
        """
        try:
            self._speech.shutdown()
            self._ack.shutdown()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(theme.STYLESHEET)
    window = OrbitWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
