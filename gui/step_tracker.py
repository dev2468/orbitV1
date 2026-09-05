"""StepTracker — the Workbench's right-hand step rail.

Parses the agent's phase markers ([STEP:START], [STEP:DONE], [STEP:FAIL],
[STEP:PROGRESS]) and falls back to inferring a step from each tool call when
the model emits no markers at all.

## Why this is a fixed-width rail, not a card in the main column

The Studio design moves progress out of the reading column entirely. Steps are
*peripheral* information — you glance at them to answer "is it stuck?", then go
back to the output. Sitting inline above the output stream, the tracker pushed
the thing you actually read down the page and resized it on every step, so the
output jumped while you were reading it. As a fixed 220px rail the output pane
never reflows, and the step list can grow to whatever length it needs.

The rail is always visible, including when empty (it shows a placeholder). An
appearing/disappearing sidebar would reflow the output pane exactly the way
the inline card did, which is the thing this layout exists to stop.

## Connector color encodes the *next* step, not the current one

Each node's trailing connector is colored by the status of the step below it:
green once that step is done, indigo while it is running, stone while it is
still pending. So the rail reads as a filling pipe — color has reached exactly
as far as work has — instead of a column of disconnected status dots.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import time
from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gui import theme

_TOOL_STEP_MAP = {
    "browser_open": "Opening browser",
    "browser_navigate": "Navigating to page",
    "browser_snapshot": "Observing web page",
    "browser_click": "Clicking in browser",
    "browser_type": "Typing in browser",
    "browser_press_key": "Pressing key in browser",
    "windows_open_app": "Opening application",
    "windows_click": "Interacting with window",
    "windows_type": "Typing text",
    "windows_key": "Pressing shortcut",
    "windows_scroll": "Scrolling window",
    "windows_drag": "Dragging element",
    "windows_batch_actions": "Executing UI actions",
    "perception_capture_screenshot": "Observing the screen",
    "perception_get_state": "Checking screen state",
    "perception_get_uia_tree": "Reading UI structure",
    "perception_find_element": "Finding UI element",
    "perception_wait_for_visual_change": "Waiting for screen",
    "run_command": "Running a command",
    "read_file": "Reading file",
    "write_file": "Writing file",
    "list_files": "Listing files",
    "fs_read_file": "Reading file",
    "fs_write_file": "Writing file",
    "memory_search_tasks": "Searching memory",
    "memory_add": "Saving context",
}

_NODE = 22          # node diameter, per the design
_ICON_W = 26        # icon column width
_ROW_MIN_H = 34


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Step:
    """A single discrete milestone or phase in a task."""

    description: str
    status: StepStatus = StepStatus.PENDING
    progress_detail: str = ""
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    is_inferred: bool = False

    def elapsed(self) -> str:
        if self.started_at is None:
            return ""
        end = self.finished_at if self.finished_at else time.time()
        dur = max(0.0, end - self.started_at)
        if dur < 60:
            return f"{dur:.1f}s" if dur < 10 else f"{int(dur)}s"
        mins = int(dur // 60)
        secs = int(dur % 60)
        return f"{mins}m{secs:02d}s"


class _StepIconWidget(QWidget):
    """Timeline node + trailing connector, custom-painted.

    ``next_status`` colors the connector — see the module docstring.
    """

    def __init__(
        self,
        status: StepStatus = StepStatus.PENDING,
        is_last: bool = False,
        next_status: Optional[StepStatus] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.status = status
        self.is_last = is_last
        self.next_status = next_status
        self.pulse_offset = 0
        self.setFixedWidth(_ICON_W)

    def set_state(
        self,
        status: StepStatus,
        is_last: bool,
        next_status: Optional[StepStatus],
    ) -> None:
        self.status = status
        self.is_last = is_last
        self.next_status = next_status
        self.update()

    def set_pulse_offset(self, offset: int) -> None:
        self.pulse_offset = offset
        self.update()

    def _connector_color(self) -> str:
        if self.next_status == StepStatus.DONE:
            return theme.SUCCESS_BORDER
        if self.next_status == StepStatus.RUNNING:
            return theme.ACCENT_PALE
        if self.next_status == StepStatus.FAILED:
            return theme.DANGER_BORDER
        return theme.BORDER

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        cx = _ICON_W // 2
        cy = _NODE // 2 + 4
        r = _NODE // 2

        # Trailing connector — runs from under this node to the widget bottom.
        if not self.is_last:
            p.setPen(QPen(QColor(self._connector_color()), 2))
            p.drawLine(cx, cy + r + 3, cx, self.height())

        if self.status == StepStatus.DONE:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.SUCCESS))
            p.drawEllipse(cx - r, cy - r, _NODE, _NODE)
            p.setPen(QPen(QColor("#FFFFFF"), 2))
            p.drawLine(cx - 5, cy, cx - 1, cy + 4)
            p.drawLine(cx - 1, cy + 4, cx + 5, cy - 4)

        elif self.status == StepStatus.RUNNING:
            halo = QColor(theme.ACCENT)
            halo.setAlpha(40)
            p.setPen(Qt.NoPen)
            p.setBrush(halo)
            hr = r + 3 + self.pulse_offset
            p.drawEllipse(cx - hr, cy - hr, hr * 2, hr * 2)

            p.setBrush(QColor(theme.ACCENT))
            p.drawEllipse(cx - r, cy - r, _NODE, _NODE)
            p.setBrush(QColor("#FFFFFF"))
            p.drawEllipse(cx - 3, cy - 3, 7, 7)

        elif self.status == StepStatus.FAILED:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.DANGER))
            p.drawEllipse(cx - r, cy - r, _NODE, _NODE)
            p.setPen(QPen(QColor("#FFFFFF"), 2))
            p.drawLine(cx - 4, cy - 4, cx + 4, cy + 4)
            p.drawLine(cx + 4, cy - 4, cx - 4, cy + 4)

        else:  # PENDING — hollow ring
            p.setPen(QPen(QColor(theme.BORDER_INPUT), 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(cx - r + 1, cy - r + 1, _NODE - 2, _NODE - 2)

        p.end()


class _StepRowWidget(QFrame):
    """One step: node + connector on the left, description + timing on the right."""

    def __init__(
        self,
        step: Step,
        index: int,
        is_last: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.step = step
        self.index = index
        self.setStyleSheet("background: transparent;")
        # Fixed vertically, or the rail's QVBoxLayout hands each row an equal
        # share of the leftover height: four steps in an 800px rail become
        # four ~160px rows with the connector stretched between them, instead
        # of a compact list at the top.
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.icon_widget = _StepIconWidget(status=step.status, is_last=is_last)
        layout.addWidget(self.icon_widget)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 3, 0, 10)
        text_col.setSpacing(1)

        self.desc_label = QLabel(step.description)
        self.desc_label.setWordWrap(True)
        self.desc_label.setTextInteractionFlags(Qt.NoTextInteraction)
        text_col.addWidget(self.desc_label)

        self.meta_label = QLabel("")
        self.meta_label.setWordWrap(True)
        self.meta_label.setTextInteractionFlags(Qt.NoTextInteraction)
        text_col.addWidget(self.meta_label)

        layout.addLayout(text_col, stretch=1)

        self.refresh(is_last=is_last, next_status=None)

    def refresh(self, is_last: bool, next_status: Optional[StepStatus]) -> None:
        self.desc_label.setText(self.step.description)
        self.icon_widget.set_state(self.step.status, is_last, next_status)
        self.setMinimumHeight(_ROW_MIN_H)

        status = self.step.status
        if status == StepStatus.RUNNING:
            self.desc_label.setStyleSheet(
                f"color:{theme.ACCENT};font-size:12px;font-weight:700;background:transparent;"
            )
            meta = self.step.progress_detail or "Running…"
            self.meta_label.setStyleSheet(
                f"color:{theme.ACCENT};font-size:10px;background:transparent;"
            )
        elif status == StepStatus.DONE:
            self.desc_label.setStyleSheet(
                f"color:{theme.TEXT_PRIMARY};font-size:12px;font-weight:600;background:transparent;"
            )
            meta = self.step.elapsed()
            self.meta_label.setStyleSheet(
                f"color:{theme.TEXT_TERTIARY};font-size:10px;background:transparent;"
            )
        elif status == StepStatus.FAILED:
            self.desc_label.setStyleSheet(
                f"color:{theme.DANGER_TEXT};font-size:12px;font-weight:600;background:transparent;"
            )
            meta = self.step.progress_detail or "Failed"
            self.meta_label.setStyleSheet(
                f"color:{theme.DANGER_TEXT};font-size:10px;background:transparent;"
            )
        else:  # PENDING
            self.desc_label.setStyleSheet(
                f"color:{theme.TEXT_TERTIARY};font-size:12px;font-weight:500;background:transparent;"
            )
            meta = ""
            self.meta_label.setStyleSheet(
                f"color:{theme.TEXT_TERTIARY};font-size:10px;background:transparent;"
            )

        self.meta_label.setText(meta)
        self.meta_label.setVisible(bool(meta))


class StepTracker(QFrame):
    """Fixed-width step rail for the right edge of the Workbench.

    Always visible: `reset()` returns it to the placeholder state rather than
    hiding it, so the output pane beside it never reflows.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("stepRail")
        self.setFixedWidth(theme.STEP_SIDEBAR_W)
        self.setStyleSheet(
            f"#stepRail {{ background: {theme.SURFACE};"
            f" border-left: 1px solid {theme.BORDER}; }}"
        )

        self.steps: list[Step] = []
        self._step_widgets: list[_StepRowWidget] = []
        self._animations: list[QPropertyAnimation] = []
        self._pulse_high = True

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 18, 16, 16)
        root.setSpacing(0)

        # -- Header: "STEPS" + n/total ---------------------------------------
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        title = QLabel("STEPS")
        title.setStyleSheet(
            f"font-size:10px;font-weight:700;color:{theme.TEXT_TERTIARY};"
            f"letter-spacing:0.8px;background:transparent;"
        )
        header.addWidget(title)
        header.addStretch()

        self.count_label = QLabel("")
        self.count_label.setStyleSheet(
            f"font-size:10px;font-weight:600;color:{theme.TEXT_SECONDARY};background:transparent;"
        )
        self.count_label.hide()
        header.addWidget(self.count_label)

        root.addLayout(header)
        root.addSpacing(16)

        # -- Empty placeholder ------------------------------------------------
        self.placeholder = QWidget()
        ph_lay = QVBoxLayout(self.placeholder)
        ph_lay.setContentsMargins(0, 0, 0, 0)
        ph_lay.setSpacing(8)
        ph_lay.setAlignment(Qt.AlignCenter)

        ph_icon = QLabel("○")
        ph_icon.setAlignment(Qt.AlignCenter)
        ph_icon.setStyleSheet(
            f"font-size:22px;color:{theme.BORDER_INPUT};background:transparent;"
        )
        ph_lay.addWidget(ph_icon)

        ph_text = QLabel("Steps will appear here\nwhen a task runs")
        ph_text.setAlignment(Qt.AlignCenter)
        ph_text.setWordWrap(True)
        ph_text.setStyleSheet(
            f"font-size:11px;color:{theme.TEXT_TERTIARY};background:transparent;"
        )
        ph_lay.addWidget(ph_text)

        root.addWidget(self.placeholder, stretch=1)

        # -- Scrollable step list ---------------------------------------------
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self.inner = QWidget()
        self.inner.setStyleSheet("background: transparent;")
        self._layout = QVBoxLayout(self.inner)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._layout.addStretch()

        self.scroll_area.setWidget(self.inner)
        self.scroll_area.hide()
        root.addWidget(self.scroll_area, stretch=1)

        # -- Timers ------------------------------------------------------------
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._pulse_active)
        self._pulse_timer.start(500)

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._elapsed_timer.start(1000)

    # -- state ---------------------------------------------------------------

    def reset(self) -> None:
        """Clear all steps and return to the placeholder. Never hides the rail."""
        self.steps.clear()
        self._animations.clear()
        for w in self._step_widgets:
            self._layout.removeWidget(w)
            w.deleteLater()
        self._step_widgets.clear()
        self.count_label.hide()
        self.scroll_area.hide()
        self.placeholder.show()

    def _activate(self) -> None:
        self.placeholder.hide()
        self.scroll_area.show()

    def handle_marker(self, marker_type: str, description: str, detail: str = "") -> None:
        """Process a parsed [STEP:XXX] marker."""
        marker_type = marker_type.upper().strip()
        description = description.strip()
        detail = detail.strip()

        if marker_type == "START":
            for s in self.steps:
                if s.status == StepStatus.RUNNING:
                    s.status = StepStatus.DONE
                    if not s.finished_at:
                        s.finished_at = time.time()

            pending = next(
                (s for s in self.steps
                 if s.status == StepStatus.PENDING and s.description == description),
                None,
            )
            if pending:
                pending.status = StepStatus.RUNNING
                pending.started_at = time.time()
                pending.is_inferred = False
            else:
                step = Step(
                    description=description,
                    status=StepStatus.RUNNING,
                    started_at=time.time(),
                    is_inferred=False,
                )
                self.steps.append(step)
                self._add_step_widget(step)
            self._activate()

        elif marker_type == "DONE":
            target = next(
                (s for s in self.steps
                 if s.description == description and s.status == StepStatus.RUNNING),
                None,
            ) or next((s for s in self.steps if s.status == StepStatus.RUNNING), None)
            if target:
                target.status = StepStatus.DONE
                target.finished_at = time.time()

        elif marker_type == "FAIL":
            target = next(
                (s for s in self.steps
                 if s.description == description and s.status == StepStatus.RUNNING),
                None,
            ) or next((s for s in self.steps if s.status == StepStatus.RUNNING), None)
            if target:
                target.status = StepStatus.FAILED
                target.finished_at = time.time()
                target.progress_detail = detail

        elif marker_type == "PROGRESS":
            target = next(
                (s for s in self.steps
                 if s.description == description and s.status == StepStatus.RUNNING),
                None,
            ) or next((s for s in self.steps if s.status == StepStatus.RUNNING), None)
            if target:
                target.progress_detail = detail

        self._refresh_all_widgets()
        self._scroll_to_bottom()

    def handle_tool_call(self, tool_name: str, args_preview: str = "") -> None:
        """Infer a step from a tool call when no explicit marker is active."""
        clean = tool_name.strip().lower()
        description = _TOOL_STEP_MAP.get(clean) or f"Executing {clean.replace('_', ' ')}"

        running = next((s for s in self.steps if s.status == StepStatus.RUNNING), None)
        if running is not None:
            # An explicit marker outranks inference: never displace a real step.
            if not running.is_inferred:
                return
            if running.description == description:
                return
            running.status = StepStatus.DONE
            running.finished_at = time.time()

        step = Step(
            description=description,
            status=StepStatus.RUNNING,
            started_at=time.time(),
            is_inferred=True,
        )
        self.steps.append(step)
        self._add_step_widget(step)
        self._activate()

    # -- widgets --------------------------------------------------------------

    def _add_step_widget(self, step: Step) -> None:
        widget = _StepRowWidget(step, index=len(self._step_widgets), is_last=True)

        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity")
        anim.setDuration(240)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        self._animations.append(anim)
        anim.start()

        self._layout.insertWidget(self._layout.count() - 1, widget)
        self._step_widgets.append(widget)
        self._refresh_all_widgets()
        self._scroll_to_bottom()

    def _refresh_all_widgets(self) -> None:
        total = len(self._step_widgets)
        done = sum(1 for s in self.steps if s.status == StepStatus.DONE)

        for idx, w in enumerate(self._step_widgets):
            is_last = idx == total - 1
            next_status = None if is_last else self.steps[idx + 1].status
            w.refresh(is_last=is_last, next_status=next_status)

        if total:
            self.count_label.setText(f"{done}/{total}")
            self.count_label.show()
        else:
            self.count_label.hide()

    def _pulse_active(self) -> None:
        self._pulse_high = not self._pulse_high
        offset = 2 if self._pulse_high else 0
        for w in self._step_widgets:
            if w.step.status == StepStatus.RUNNING:
                w.icon_widget.set_pulse_offset(offset)

    def _update_elapsed(self) -> None:
        for w in self._step_widgets:
            if w.step.status == StepStatus.RUNNING:
                w.refresh(
                    is_last=(w is self._step_widgets[-1]),
                    next_status=None if w is self._step_widgets[-1]
                    else self.steps[w.index + 1].status,
                )

    def _scroll_to_bottom(self) -> None:
        QTimer.singleShot(
            50,
            lambda: self.scroll_area.verticalScrollBar().setValue(
                self.scroll_area.verticalScrollBar().maximum()
            ),
        )
