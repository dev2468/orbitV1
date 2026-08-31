"""StepTracker widget for Orbit GUI.

Provides visual step-by-step progress tracking for running tasks, parsing
agent phase markers ([STEP:START], [STEP:DONE], [STEP:FAIL], [STEP:PROGRESS])
and tool calls as fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import time
from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPen
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
    "windows_key": "Pressing keyboard shortcut",
    "windows_scroll": "Scrolling window",
    "windows_drag": "Dragging element",
    "windows_batch_actions": "Executing UI actions",
    "perception_capture_screenshot": "Observing the screen",
    "perception_get_state": "Checking screen state",
    "perception_get_uia_tree": "Reading UI structure",
    "perception_find_element": "Finding UI element",
    "perception_wait_for_visual_change": "Waiting for screen update",
    "run_command": "Running a command",
    "read_file": "Reading file",
    "write_file": "Writing file",
    "list_files": "Listing files",
    "fs_read_file": "Reading file",
    "fs_write_file": "Writing file",
    "memory_search_tasks": "Searching memory",
    "memory_add": "Saving context",
}


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Step:
    """Represents a single discrete milestone or phase in a task."""

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
            return f"{int(dur)}s"
        mins = int(dur // 60)
        secs = int(dur % 60)
        return f"{mins}m {secs}s"


class _StepIconWidget(QWidget):
    """Custom paint widget rendering timeline connectors and animated step node icons."""

    def __init__(
        self,
        status: StepStatus = StepStatus.PENDING,
        is_first: bool = False,
        is_last: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.status = status
        self.is_first = is_first
        self.is_last = is_last
        self.pulse_radius_offset = 0
        self.setFixedSize(28, 32)

    def set_status(self, status: StepStatus, is_first: bool, is_last: bool) -> None:
        self.status = status
        self.is_first = is_first
        self.is_last = is_last
        self.update()

    def set_pulse_offset(self, offset: int) -> None:
        self.pulse_radius_offset = offset
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        cx = 14
        cy = 16

        # Draw vertical timeline connector line
        if not (self.is_first and self.is_last):
            pen = QPen(QColor(theme.BORDER), 1)
            painter.setPen(pen)
            if not self.is_first:
                painter.drawLine(cx, 0, cx, cy - 8)
            if not self.is_last:
                painter.drawLine(cx, cy + 8, cx, 32)

        # Draw Node Icon by Status
        if self.status == StepStatus.DONE:
            # Green checkmark circle
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme.SUCCESS))
            painter.drawEllipse(cx - 8, cy - 8, 16, 16)

            painter.setPen(QPen(QColor("#FFFFFF"), 2))
            painter.drawLine(cx - 4, cy, cx - 1, cy + 3)
            painter.drawLine(cx - 1, cy + 3, cx + 4, cy - 3)

        elif self.status == StepStatus.RUNNING:
            # Pulsing Indigo ring + solid core
            painter.setPen(Qt.NoPen)
            halo_color = QColor(theme.ACCENT)
            halo_color.setAlpha(45)
            painter.setBrush(halo_color)
            r_halo = 9 + self.pulse_radius_offset
            painter.drawEllipse(cx - r_halo, cy - r_halo, r_halo * 2, r_halo * 2)

            painter.setBrush(QColor(theme.ACCENT))
            painter.drawEllipse(cx - 6, cy - 6, 12, 12)

            painter.setBrush(QColor(theme.SURFACE))
            painter.drawEllipse(cx - 2, cy - 2, 4, 4)

        elif self.status == StepStatus.FAILED:
            # Red cross circle
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme.DANGER))
            painter.drawEllipse(cx - 8, cy - 8, 16, 16)

            painter.setPen(QPen(QColor("#FFFFFF"), 2))
            painter.drawLine(cx - 3, cy - 3, cx + 3, cy + 3)
            painter.drawLine(cx + 3, cy - 3, cx - 3, cy + 3)

        else:  # PENDING
            # Subtle hollow circle
            painter.setPen(QPen(QColor(theme.BORDER_INPUT), 1.5))
            painter.setBrush(QColor(theme.SURFACE))
            painter.drawEllipse(cx - 5, cy - 5, 10, 10)


class _StepRowWidget(QFrame):
    """Row widget representing one individual step."""

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
        self.setObjectName("stepRow")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 12, 6)
        layout.setSpacing(10)

        # Icon & connector
        self.icon_widget = _StepIconWidget(
            status=step.status, is_first=(index == 0), is_last=is_last
        )
        layout.addWidget(self.icon_widget)

        # Text column (description + optional detail)
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        self.desc_label = QLabel(step.description)
        self.desc_label.setTextInteractionFlags(Qt.NoTextInteraction)
        text_col.addWidget(self.desc_label)

        self.detail_label = QLabel("")
        self.detail_label.setTextInteractionFlags(Qt.NoTextInteraction)
        self.detail_label.setStyleSheet(
            f"color:{theme.TEXT_SECONDARY}; font-size:11px; font-style:italic;"
        )
        self.detail_label.hide()
        text_col.addWidget(self.detail_label)

        layout.addLayout(text_col, stretch=1)

        # Elapsed time badge
        self.elapsed_label = QLabel(step.elapsed())
        self.elapsed_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.elapsed_label.setStyleSheet(
            f"color:{theme.TEXT_SECONDARY}; font-size:11px; font-weight:600; padding: 2px 6px; "
            f"background:{theme.INPUT_BG}; border-radius: 4px;"
        )
        layout.addWidget(self.elapsed_label)

        self.refresh(is_last=is_last)

    def refresh(self, is_last: bool) -> None:
        self.desc_label.setText(self.step.description)
        self.icon_widget.set_status(self.step.status, is_first=(self.index == 0), is_last=is_last)

        if self.step.progress_detail:
            self.detail_label.setText(f"↳ {self.step.progress_detail}")
            self.detail_label.show()
        else:
            self.detail_label.hide()

        elapsed = self.step.elapsed()
        self.elapsed_label.setText(elapsed)
        self.elapsed_label.setVisible(bool(elapsed))

        # Styling
        if self.step.status == StepStatus.RUNNING:
            self.setStyleSheet(
                f"#stepRow {{ background: {theme.ACCENT_LIGHT}; border: 1px solid {theme.ACCENT_BORDER}; border-radius: {theme.RADIUS_SM}px; }}"
            )
            self.desc_label.setStyleSheet(
                f"color: {theme.ACCENT}; font-size: 13px; font-weight: 700;"
            )
            self.elapsed_label.setStyleSheet(
                f"color:{theme.ACCENT}; font-size:11px; font-weight:700; padding: 2px 6px; "
                f"background:#E0E7FF; border-radius: 4px;"
            )
        elif self.step.status == StepStatus.DONE:
            self.setStyleSheet("#stepRow { background: transparent; border: 1px solid transparent; }")
            self.desc_label.setStyleSheet(
                f"color: {theme.TEXT_PRIMARY}; font-size: 13px; font-weight: 500;"
            )
            self.elapsed_label.setStyleSheet(
                f"color:{theme.TEXT_SECONDARY}; font-size:11px; font-weight:600; padding: 2px 6px; "
                f"background:{theme.INPUT_BG}; border-radius: 4px;"
            )
        elif self.step.status == StepStatus.FAILED:
            self.setStyleSheet(f"#stepRow {{ background: {theme.DANGER_BG}; border: 1px solid {theme.DANGER_BORDER}; border-radius: {theme.RADIUS_SM}px; }}")
            self.desc_label.setStyleSheet(
                f"color: {theme.DANGER_TEXT}; font-size: 13px; font-weight: 600;"
            )
            self.elapsed_label.setStyleSheet(
                f"color:{theme.DANGER_TEXT}; font-size:11px; font-weight:700; padding: 2px 6px; "
                f"background:#FEE2E2; border-radius: 4px;"
            )
        else:  # PENDING
            self.setStyleSheet("#stepRow { background: transparent; border: 1px solid transparent; }")
            self.desc_label.setStyleSheet(
                f"color: {theme.TEXT_TERTIARY}; font-size: 13px; font-weight: 400;"
            )
            self.elapsed_label.setStyleSheet(
                f"color:{theme.TEXT_TERTIARY}; font-size:11px; font-weight:600; padding: 2px 6px; "
                f"background:{theme.INPUT_BG}; border-radius: 4px;"
            )


class StepTracker(QWidget):
    """Visual task step progress tracker with animations and live elapsed time."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("stepTrackerContainer")
        self.steps: list[Step] = []
        self._step_widgets: list[_StepRowWidget] = []
        self._animations: list[QPropertyAnimation] = []
        self._pulse_high = True

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Outer card container
        self.card = QFrame()
        self.card.setObjectName("stepTrackerCard")
        self.card.setStyleSheet(
            f"#stepTrackerCard {{ background: {theme.SURFACE}; "
            f"border-radius: {theme.RADIUS_MD}px; }}"
        )
        theme.apply_drop_shadow(self.card, 'sm')
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(16, 20, 16, 20)
        card_layout.setSpacing(6)

        # Header bar
        header_h = QHBoxLayout()
        header_h.setContentsMargins(4, 0, 4, 0)
        header_h.setSpacing(8)

        self.title_label = QLabel("TASK PROGRESSION")
        self.title_label.setStyleSheet(
            f"font-size: 11px; font-weight: 800; color: {theme.TEXT_SECONDARY}; letter-spacing: 0.8px;"
        )
        header_h.addWidget(self.title_label)

        self.badge_count = QLabel("")
        self.badge_count.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {theme.ACCENT}; background: {theme.ACCENT_LIGHT}; "
            f"border: 1px solid {theme.ACCENT_BORDER}; border-radius: 10px; padding: 2px 8px;"
        )
        self.badge_count.hide()
        header_h.addWidget(self.badge_count)

        header_h.addStretch()

        self.status_pill = QLabel("Running")
        self.status_pill.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {theme.TEXT_SECONDARY}; padding: 2px 6px;"
        )
        self.status_pill.hide()
        header_h.addWidget(self.status_pill)

        card_layout.addLayout(header_h)

        # Scroll area for compactness
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_area.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { width: 6px; background: transparent; margin: 0; }"
            f"QScrollBar::handle:vertical {{ background: {theme.BORDER}; border-radius: 3px; }}"
            f"QScrollBar::handle:vertical:hover {{ background: {theme.TEXT_TERTIARY}; }}"
        )
        self.scroll_area.setMaximumHeight(200)

        self.inner_widget = QWidget()
        self.inner_widget.setStyleSheet("background: transparent;")
        self._layout = QVBoxLayout(self.inner_widget)
        self._layout.setContentsMargins(0, 4, 0, 4)
        self._layout.setSpacing(4)
        self._layout.addStretch()

        self.scroll_area.setWidget(self.inner_widget)
        card_layout.addWidget(self.scroll_area)
        root_layout.addWidget(self.card)

        # Timers
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._pulse_active)
        self._pulse_timer.start(500)

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._elapsed_timer.start(1000)

        self.hide()  # Hidden until first step arrives or task starts

    def reset(self) -> None:
        """Clear all steps for a new task."""
        self.steps.clear()
        self._animations.clear()
        for w in self._step_widgets:
            self._layout.removeWidget(w)
            w.deleteLater()
        self._step_widgets.clear()
        self.badge_count.hide()
        self.status_pill.hide()
        self.hide()

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

            pending_step = None
            for s in self.steps:
                if s.status == StepStatus.PENDING and s.description == description:
                    pending_step = s
                    break

            if pending_step:
                pending_step.status = StepStatus.RUNNING
                pending_step.started_at = time.time()
                pending_step.is_inferred = False
            else:
                step = Step(
                    description=description,
                    status=StepStatus.RUNNING,
                    started_at=time.time(),
                    is_inferred=False,
                )
                self.steps.append(step)
                self._add_step_widget(step)

            self.show()

        elif marker_type == "DONE":
            found = False
            for s in self.steps:
                if s.description == description and s.status == StepStatus.RUNNING:
                    s.status = StepStatus.DONE
                    s.finished_at = time.time()
                    found = True
                    break
            if not found:
                for s in self.steps:
                    if s.status == StepStatus.RUNNING:
                        s.status = StepStatus.DONE
                        s.finished_at = time.time()
                        break

        elif marker_type == "FAIL":
            found = False
            for s in self.steps:
                if s.description == description and s.status == StepStatus.RUNNING:
                    s.status = StepStatus.FAILED
                    s.finished_at = time.time()
                    s.progress_detail = detail
                    found = True
                    break
            if not found:
                for s in self.steps:
                    if s.status == StepStatus.RUNNING:
                        s.status = StepStatus.FAILED
                        s.finished_at = time.time()
                        s.progress_detail = detail
                        break

        elif marker_type == "PROGRESS":
            found = False
            for s in self.steps:
                if s.description == description and s.status == StepStatus.RUNNING:
                    s.progress_detail = detail
                    found = True
                    break
            if not found:
                for s in self.steps:
                    if s.status == StepStatus.RUNNING:
                        s.progress_detail = detail
                        break

        self._refresh_all_widgets()
        self._scroll_to_bottom()

    def handle_tool_call(self, tool_name: str, args_preview: str = "") -> None:
        """Auto-infer steps from tool calls if no explicit step is active."""
        clean_tool = tool_name.strip().lower()
        description = _TOOL_STEP_MAP.get(clean_tool)
        if not description:
            description = f"Executing {clean_tool.replace('_', ' ')}"

        running_step: Step | None = None
        for s in self.steps:
            if s.status == StepStatus.RUNNING:
                running_step = s
                break

        if running_step is not None:
            if not running_step.is_inferred:
                return
            if running_step.description == description:
                return
            running_step.status = StepStatus.DONE
            running_step.finished_at = time.time()

        step = Step(
            description=description,
            status=StepStatus.RUNNING,
            started_at=time.time(),
            is_inferred=True,
        )
        self.steps.append(step)
        self._add_step_widget(step)
        self.show()

    def _add_step_widget(self, step: Step) -> None:
        idx = len(self._step_widgets)
        widget = _StepRowWidget(step, index=idx, is_last=True)

        opacity_effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(opacity_effect)
        anim = QPropertyAnimation(opacity_effect, b"opacity")
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
        done_count = sum(1 for s in self.steps if s.status == StepStatus.DONE)
        running_count = sum(1 for s in self.steps if s.status == StepStatus.RUNNING)
        failed_count = sum(1 for s in self.steps if s.status == StepStatus.FAILED)

        for idx, w in enumerate(self._step_widgets):
            is_last = (idx == total - 1)
            w.refresh(is_last=is_last)

        if total > 0:
            self.badge_count.setText(f"{done_count}/{total} complete")
            self.badge_count.show()

            if running_count > 0:
                self.status_pill.setText("Running ●")
                self.status_pill.setStyleSheet(f"color:{theme.ACCENT}; font-size:11px; font-weight:700;")
                self.status_pill.show()
            elif failed_count > 0:
                self.status_pill.setText("Failed ✗")
                self.status_pill.setStyleSheet(f"color:{theme.DANGER_TEXT}; font-size:11px; font-weight:700;")
                self.status_pill.show()
            elif done_count == total:
                self.status_pill.setText("All steps complete ✓")
                self.status_pill.setStyleSheet(f"color:{theme.SUCCESS_TEXT}; font-size:11px; font-weight:700;")
                self.status_pill.show()
            else:
                self.status_pill.hide()

    def _pulse_active(self) -> None:
        self._pulse_high = not self._pulse_high
        offset = 2 if self._pulse_high else 0
        for w in self._step_widgets:
            if w.step.status == StepStatus.RUNNING:
                w.icon_widget.set_pulse_offset(offset)

    def _update_elapsed(self) -> None:
        for w in self._step_widgets:
            if w.step.status == StepStatus.RUNNING:
                elapsed = w.step.elapsed()
                w.elapsed_label.setText(elapsed)
                w.elapsed_label.setVisible(bool(elapsed))

    def _scroll_to_bottom(self) -> None:
        QTimer.singleShot(
            50,
            lambda: self.scroll_area.verticalScrollBar().setValue(
                self.scroll_area.verticalScrollBar().maximum()
            ),
        )
