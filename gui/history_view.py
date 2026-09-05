"""Task History & Execution Inspector — master/detail with KPI tiles.

Left: three KPI tiles, search, status/lane filter pills, and the task list.
Right: the selected task's result, tool-event timeline, screenshot gallery and
raw JSON.

The arithmetic behind the tiles and every duration/"2m ago" string lives in
``gui/stats.py`` so it can be tested without a QApplication — this module only
formats what that returns. Note the deliberate choice recorded there: success
rate is computed over *decided* tasks (completed + failed), not over every row,
so a running task does not drag the number down and then bounce it back up
when it lands.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QGuiApplication, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from orbit import db
from gui import theme
from gui.stats import (
    compute_kpis,
    count_tool_calls,
    format_duration,
    format_percent,
    relative_time,
    task_duration_seconds,
)

_STATUS_STYLE = {
    "COMPLETED": (theme.SUCCESS_BG, theme.SUCCESS, theme.SUCCESS_BORDER, "DONE"),
    "FAILED": (theme.DANGER_BG, theme.DANGER, theme.DANGER_BORDER, "FAILED"),
    "RUNNING": (theme.ACCENT_LIGHT, theme.ACCENT, theme.ACCENT_BORDER, "RUNNING"),
    "CANCELLED": (theme.INPUT_BG, theme.TEXT_SECONDARY, theme.BORDER, "CANCELLED"),
}


def _status_style(status: str) -> tuple[str, str, str, str]:
    return _STATUS_STYLE.get(
        (status or "").upper(),
        (theme.INPUT_BG, theme.TEXT_SECONDARY, theme.BORDER, (status or "PENDING").upper()),
    )


class _KpiTile(QFrame):
    """One statistic: a big number over a small label."""

    def __init__(self, label: str, *, accent: bool = False) -> None:
        super().__init__()
        self.setObjectName("kpiTile")
        bg = theme.SUCCESS_BG if accent else theme.INPUT_BG
        self._value_color = theme.SUCCESS if accent else theme.TEXT_PRIMARY
        self._label_color = theme.SUCCESS if accent else theme.TEXT_TERTIARY
        self.setStyleSheet(
            f"#kpiTile {{ background:{bg}; border:none;"
            f" border-radius:{theme.RADIUS_LG}px; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(2)

        self.value_label = QLabel("—")
        self.value_label.setStyleSheet(
            f"font-size:24px;font-weight:800;color:{self._value_color};"
            f"letter-spacing:-1px;background:transparent;"
        )
        lay.addWidget(self.value_label)

        self.name_label = QLabel(label)
        self.name_label.setStyleSheet(
            f"font-size:10px;font-weight:500;color:{self._label_color};background:transparent;"
        )
        lay.addWidget(self.name_label)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)


class _ElidedLabel(QLabel):
    """QLabel that truncates with an ellipsis instead of forcing its parent wider.

    Task titles here are whole goal sentences and routinely run to 80+
    characters. A plain QLabel reports that full width as its sizeHint, the
    row grows to match, and the scroll area then scrolls horizontally — so
    every title runs off the panel edge and gets visually cut mid-word with
    no ellipsis to signal it. Eliding at paint time keeps the row exactly as
    wide as the viewport, whatever width the splitter is dragged to.
    """

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self._full_text = text
        # Painted explicitly rather than read back from the palette: the color
        # arrives via QSS, which does not reliably land in palette().
        self._color = theme.TEXT_PRIMARY
        # Ignored horizontally so the layout may shrink us below the text width.
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full_text = text
        super().setText(text)
        self.update()

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setFont(self.font())
        metrics = QFontMetrics(self.font())
        elided = metrics.elidedText(self._full_text, Qt.ElideRight, self.width())
        painter.setPen(QColor(self._color))
        painter.drawText(self.rect(), int(self.alignment() | Qt.AlignVCenter), elided)
        painter.end()


class _TaskRowWidget(QFrame):
    """Inbox-style row: title + status badge over a relative-time meta line."""

    def __init__(
        self,
        task: dict,
        row_idx: int = 0,
        on_click: Optional[Callable[[int], None]] = None,
        is_selected: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.row_idx = row_idx
        self.on_click = on_click
        self.setCursor(Qt.PointingHandCursor)

        status = (task.get("status") or "PENDING").upper()
        title = task.get("title") or task.get("goal") or "Untitled task"
        lane = (task.get("lane") or "headless").lower()
        _, badge_fg, _, badge_text = _status_style(status)
        badge_bg = _status_style(status)[0]

        self.setStyleSheet(self._sheet(is_selected))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(4)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)

        self.title_lbl = _ElidedLabel(title)
        self.title_lbl.setStyleSheet(self._title_sheet(is_selected))
        self.title_lbl.set_color(theme.ACCENT if is_selected else theme.TEXT_PRIMARY)
        self.title_lbl.setToolTip(task.get("goal") or title)
        self.title_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        top.addWidget(self.title_lbl, stretch=1)

        badge = QLabel(badge_text)
        badge.setStyleSheet(
            f"font-size:9px;font-weight:700;color:{badge_fg};background:{badge_bg};"
            f"border-radius:{theme.RADIUS_XS}px;padding:2px 8px;"
        )
        badge.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        top.addWidget(badge)
        lay.addLayout(top)

        # "2m ago · headless · 34s" — duration omitted while still running,
        # because a partial elapsed time reads as a final one.
        bits = [relative_time(task.get("created_at")), lane]
        if task.get("completed_at"):
            dur = format_duration(task_duration_seconds(task))
            if dur:
                bits.append(dur)
        meta_color = theme.ACCENT if status == "RUNNING" else theme.TEXT_TERTIARY
        meta = QLabel("  ·  ".join(b for b in bits if b))
        meta.setStyleSheet(f"font-size:11px;color:{meta_color};background:transparent;")
        meta.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(meta)

    @staticmethod
    def _sheet(selected: bool) -> str:
        bg = theme.ACCENT_LIGHT if selected else "transparent"
        edge = theme.ACCENT if selected else "transparent"
        hover = theme.ACCENT_LIGHT if selected else theme.ROW_HOVER
        return (
            f"_TaskRowWidget {{ background:{bg}; border-left:3px solid {edge};"
            f" border-bottom:1px solid {theme.BORDER_LIGHT}; }}"
            f"_TaskRowWidget:hover {{ background:{hover}; }}"
        )

    @staticmethod
    def _title_sheet(selected: bool) -> str:
        color = theme.ACCENT if selected else theme.TEXT_PRIMARY
        return f"font-size:13px;font-weight:600;color:{color};background:transparent;"

    def set_selected(self, selected: bool) -> None:
        self.setStyleSheet(self._sheet(selected))
        self.title_lbl.setStyleSheet(self._title_sheet(selected))
        self.title_lbl.set_color(theme.ACCENT if selected else theme.TEXT_PRIMARY)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        super().mousePressEvent(event)
        if self.on_click is not None:
            self.on_click(self.row_idx)


class TaskHistoryView(QWidget):
    """Full-window master-detail history, inspector and analytics."""

    rerun_requested = Signal(str, str)  # (goal, lane)

    def __init__(
        self, md_renderer: Callable[[str], str], parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self._md_to_html = md_renderer
        self._all_tasks: list[dict] = []
        self._filtered_tasks: list[dict] = []
        self._row_widgets: list[_TaskRowWidget] = []
        self._selected_task_id: str | None = None
        self._status_filter = "ALL"
        self._lane_filter = "ALL"

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_inspector())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([400, 780])
        root.addWidget(splitter)

        self.refresh()

    # ===================== construction =====================

    def _build_left_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("historyLeft")
        panel.setMinimumWidth(340)
        panel.setStyleSheet(
            f"#historyLeft {{ background:{theme.SURFACE};"
            f" border-right:1px solid {theme.BORDER}; }}"
        )
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # -- KPI tiles ---------------------------------------------------------
        kpi_wrap = QFrame()
        kpi_wrap.setObjectName("kpiWrap")
        # ID selector: a bare `border-bottom` here is inherited by every
        # descendant, underlining each KPI number individually.
        kpi_wrap.setStyleSheet(f"#kpiWrap {{ border-bottom:1px solid {theme.BORDER}; }}")
        kpi_lay = QHBoxLayout(kpi_wrap)
        kpi_lay.setContentsMargins(16, 14, 16, 14)
        kpi_lay.setSpacing(8)

        self.kpi_total = _KpiTile("Total tasks")
        self.kpi_success = _KpiTile("Success rate", accent=True)
        self.kpi_duration = _KpiTile("Avg duration")
        for tile in (self.kpi_total, self.kpi_success, self.kpi_duration):
            kpi_lay.addWidget(tile, stretch=1)
        lay.addWidget(kpi_wrap)

        # -- Search + filters --------------------------------------------------
        filter_wrap = QFrame()
        filter_wrap.setObjectName("filterWrap")
        filter_wrap.setStyleSheet(
            f"#filterWrap {{ border-bottom:1px solid {theme.BORDER}; }}"
        )
        f_lay = QVBoxLayout(filter_wrap)
        f_lay.setContentsMargins(16, 12, 16, 12)
        f_lay.setSpacing(8)

        self.search_input = QLineEdit()
        self.search_input.setFixedHeight(36)
        self.search_input.setPlaceholderText("Search tasks…")
        self.search_input.setStyleSheet(
            f"QLineEdit {{ background:{theme.INPUT_BG}; border:1px solid transparent;"
            f" border-radius:{theme.RADIUS_MD}px; padding:0 14px; font-size:13px;"
            f" color:{theme.TEXT_PRIMARY}; }}"
            f"QLineEdit:focus {{ border-color:{theme.ACCENT_BORDER}; background:{theme.SURFACE}; }}"
        )
        self.search_input.textChanged.connect(self._apply_filters)
        f_lay.addWidget(self.search_input)

        # Two rows, not one. Six pills plus a divider need ~400px of natural
        # width; the panel is user-resizable via the splitter, so on one row
        # QHBoxLayout compresses them below their text width and the labels
        # clip mid-word ("Completed" -> ":ompletec"). Stacking by dimension
        # also groups them the way they are actually used.
        status_row = QHBoxLayout()
        status_row.setSpacing(4)
        status_row.setContentsMargins(0, 0, 0, 0)
        self.status_btns: dict[str, QPushButton] = {}
        for key, label in (("ALL", "All"), ("COMPLETED", "Completed"), ("FAILED", "Failed")):
            btn = QPushButton(label)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            btn.clicked.connect(lambda _=False, k=key: self._set_status_filter(k))
            self.status_btns[key] = btn
            status_row.addWidget(btn)
        status_row.addStretch()
        f_lay.addLayout(status_row)

        lane_row = QHBoxLayout()
        lane_row.setSpacing(4)
        lane_row.setContentsMargins(0, 0, 0, 0)
        self.lane_btns: dict[str, QPushButton] = {}
        for key, label in (("ALL", "All lanes"), ("HEADLESS", "Headless"), ("FOREGROUND", "Foreground")):
            btn = QPushButton(label)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            btn.clicked.connect(lambda _=False, k=key: self._set_lane_filter(k))
            self.lane_btns[key] = btn
            lane_row.addWidget(btn)
        lane_row.addStretch()
        f_lay.addLayout(lane_row)
        lay.addWidget(filter_wrap)
        self._restyle_pills()

        # -- Task list ---------------------------------------------------------
        self.list_scroll = QScrollArea()
        self.list_scroll.setWidgetResizable(True)
        self.list_scroll.setFrameShape(QFrame.NoFrame)
        self.list_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self.list_inner = QWidget()
        self.list_inner.setStyleSheet("background: transparent;")
        self.list_layout = QVBoxLayout(self.list_inner)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(0)
        self.list_layout.addStretch()
        self.list_scroll.setWidget(self.list_inner)

        lay.addWidget(self.list_scroll, stretch=1)
        return panel

    def _build_inspector(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("historyRight")
        wrap.setStyleSheet(f"#historyRight {{ background:{theme.SURFACE}; }}")
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.inspector_stack = QStackedWidget()

        empty = QWidget()
        e_lay = QVBoxLayout(empty)
        e_lay.setAlignment(Qt.AlignCenter)
        msg = QLabel("Select a task to inspect its execution")
        msg.setAlignment(Qt.AlignCenter)
        msg.setStyleSheet(
            f"font-size:14px;font-weight:500;color:{theme.TEXT_TERTIARY};background:transparent;"
        )
        e_lay.addWidget(msg)
        self.inspector_stack.addWidget(empty)

        active = QWidget()
        a_lay = QVBoxLayout(active)
        a_lay.setContentsMargins(0, 0, 0, 0)
        a_lay.setSpacing(0)

        # -- Header ------------------------------------------------------------
        header = QFrame()
        header.setObjectName("inspectorHeader")
        header.setStyleSheet(
            f"#inspectorHeader {{ border-bottom:1px solid {theme.BORDER}; }}"
        )
        h_lay = QVBoxLayout(header)
        h_lay.setContentsMargins(28, 20, 28, 16)
        h_lay.setSpacing(8)

        pill_row = QHBoxLayout()
        pill_row.setSpacing(8)
        self.insp_status_pill = QLabel("Completed")
        pill_row.addWidget(self.insp_status_pill)
        self.insp_lane_pill = QLabel("Headless")
        self.insp_lane_pill.setStyleSheet(
            f"font-size:10px;color:{theme.TEXT_SECONDARY};background:{theme.INPUT_BG};"
            f"border-radius:{theme.RADIUS_SM}px;padding:3px 10px;"
        )
        pill_row.addWidget(self.insp_lane_pill)
        pill_row.addStretch()

        copy_id = QPushButton("Copy ID")
        copy_id.setCursor(Qt.PointingHandCursor)
        copy_id.setStyleSheet(self._ghost_button_sheet())
        copy_id.clicked.connect(self._copy_task_id)
        pill_row.addWidget(copy_id)

        self.rerun_btn = QPushButton("Re-run  →")
        self.rerun_btn.setCursor(Qt.PointingHandCursor)
        self.rerun_btn.setStyleSheet(
            f"QPushButton {{ background:{theme.ACCENT}; color:#FFFFFF; border:none;"
            f" border-radius:{theme.RADIUS_SM}px; padding:6px 14px;"
            f" font-size:12px; font-weight:600; }}"
            f"QPushButton:hover {{ background:{theme.ACCENT_HOVER}; }}"
        )
        self.rerun_btn.clicked.connect(self._trigger_rerun)
        pill_row.addWidget(self.rerun_btn)
        h_lay.addLayout(pill_row)

        self.insp_title = QLabel("Task title")
        self.insp_title.setWordWrap(True)
        self.insp_title.setStyleSheet(
            f"font-size:18px;font-weight:800;color:{theme.TEXT_PRIMARY};"
            f"letter-spacing:-0.3px;background:transparent;"
        )
        h_lay.addWidget(self.insp_title)

        self.insp_meta = QLabel("")
        self.insp_meta.setStyleSheet(
            f"font-size:12px;color:{theme.TEXT_TERTIARY};background:transparent;"
        )
        h_lay.addWidget(self.insp_meta)
        a_lay.addWidget(header)

        # -- Tabs ---------------------------------------------------------------
        self.insp_tabs = QTabWidget()
        self.insp_tabs.setDocumentMode(True)
        self.insp_tabs.setStyleSheet(
            f"QTabWidget::pane {{ border:none; background:{theme.SURFACE}; }}"
            f"QTabBar {{ background:transparent; }}"
            f"QTabBar::tab {{ background:transparent; border:none;"
            f" border-bottom:2px solid transparent; padding:10px 18px;"
            f" font-size:12px; color:{theme.TEXT_TERTIARY}; }}"
            f"QTabBar::tab:hover {{ color:{theme.TEXT_PRIMARY}; }}"
            f"QTabBar::tab:selected {{ color:{theme.ACCENT}; font-weight:700;"
            f" border-bottom:2px solid {theme.ACCENT}; }}"
        )

        # Result
        result_tab = QWidget()
        r_lay = QVBoxLayout(result_tab)
        r_lay.setContentsMargins(28, 18, 28, 18)
        self.result_view = QTextEdit()
        self.result_view.setReadOnly(True)
        self.result_view.setStyleSheet(
            f"border:none;background:transparent;font-size:13px;color:{theme.TEXT_PRIMARY};"
        )
        r_lay.addWidget(self.result_view)
        self.insp_tabs.addTab(result_tab, "Result")

        # Events
        events_tab = QWidget()
        e2 = QVBoxLayout(events_tab)
        e2.setContentsMargins(28, 14, 28, 18)
        e2.setSpacing(10)
        self.events_count_lbl = QLabel("")
        self.events_count_lbl.setStyleSheet(
            f"font-size:11px;font-weight:500;color:{theme.TEXT_TERTIARY};background:transparent;"
        )
        e2.addWidget(self.events_count_lbl)
        self.events_scroll, self.events_inner, self.events_layout = self._scroll_column()
        e2.addWidget(self.events_scroll, stretch=1)
        self.insp_tabs.addTab(events_tab, "Events")

        # Screenshots
        shots_tab = QWidget()
        s2 = QVBoxLayout(shots_tab)
        s2.setContentsMargins(28, 14, 28, 18)
        s2.setSpacing(10)
        self.shots_count_lbl = QLabel("")
        self.shots_count_lbl.setStyleSheet(
            f"font-size:11px;font-weight:500;color:{theme.TEXT_TERTIARY};background:transparent;"
        )
        s2.addWidget(self.shots_count_lbl)
        self.shots_scroll, self.shots_inner, self.shots_layout = self._scroll_column()
        s2.addWidget(self.shots_scroll, stretch=1)
        self.insp_tabs.addTab(shots_tab, "Screenshots")

        # Raw JSON
        raw_tab = QWidget()
        raw_lay = QVBoxLayout(raw_tab)
        raw_lay.setContentsMargins(28, 14, 28, 18)
        raw_lay.setSpacing(8)
        raw_top = QHBoxLayout()
        raw_top.addStretch()
        copy_json = QPushButton("Copy JSON")
        copy_json.setCursor(Qt.PointingHandCursor)
        copy_json.setStyleSheet(self._ghost_button_sheet())
        copy_json.clicked.connect(self._copy_raw_json)
        raw_top.addWidget(copy_json)
        raw_lay.addLayout(raw_top)

        self.raw_json_view = QTextEdit()
        self.raw_json_view.setReadOnly(True)
        self.raw_json_view.setStyleSheet(
            f"border:1px solid {theme.BORDER}; border-radius:{theme.RADIUS_MD}px;"
            f"font-family:{theme.FONT_MONO}; font-size:12px;"
            f"background:{theme.INPUT_BG}; color:{theme.TEXT_PRIMARY}; padding:12px;"
        )
        raw_lay.addWidget(self.raw_json_view)
        self.insp_tabs.addTab(raw_tab, "Raw JSON")

        a_lay.addWidget(self.insp_tabs, stretch=1)
        self.inspector_stack.addWidget(active)
        lay.addWidget(self.inspector_stack, stretch=1)
        return wrap

    @staticmethod
    def _ghost_button_sheet() -> str:
        return (
            f"QPushButton {{ background:{theme.INPUT_BG}; border:none;"
            f" border-radius:{theme.RADIUS_SM}px; padding:6px 12px;"
            f" font-size:11px; font-weight:600; color:{theme.TEXT_SECONDARY}; }}"
            f"QPushButton:hover {{ background:{theme.BORDER}; color:{theme.TEXT_PRIMARY}; }}"
        )

    @staticmethod
    def _scroll_column() -> tuple[QScrollArea, QWidget, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")
        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addStretch()
        scroll.setWidget(inner)
        return scroll, inner, lay

    # ===================== data =====================

    def refresh(self) -> None:
        db.init_db()
        self._all_tasks = db.list_tasks(limit=250)
        self._update_kpis()
        self._apply_filters()

    def _update_kpis(self) -> None:
        k = compute_kpis(self._all_tasks)
        self.kpi_total.set_value(str(k["total"]))
        self.kpi_success.set_value(format_percent(k["success_rate"]))
        self.kpi_duration.set_value(format_duration(k["avg_duration_seconds"]) or "—")

    def _set_status_filter(self, status: str) -> None:
        self._status_filter = status
        self._restyle_pills()
        self._apply_filters()

    def _set_lane_filter(self, lane: str) -> None:
        self._lane_filter = lane
        self._restyle_pills()
        self._apply_filters()

    def _restyle_pills(self) -> None:
        active = (
            f"QPushButton {{ background:{theme.TEXT_PRIMARY}; color:{theme.SURFACE};"
            f" border:none; border-radius:{theme.RADIUS_SM}px; padding:4px 12px;"
            f" font-size:11px; font-weight:600; }}"
        )
        idle = (
            f"QPushButton {{ background:{theme.INPUT_BG}; color:{theme.TEXT_SECONDARY};"
            f" border:none; border-radius:{theme.RADIUS_SM}px; padding:4px 12px;"
            f" font-size:11px; font-weight:500; }}"
            f"QPushButton:hover {{ background:{theme.BORDER}; color:{theme.TEXT_PRIMARY}; }}"
        )
        for key, btn in self.status_btns.items():
            btn.setStyleSheet(active if key == self._status_filter else idle)
        for key, btn in self.lane_btns.items():
            btn.setStyleSheet(active if key == self._lane_filter else idle)

    def _apply_filters(self) -> None:
        query = self.search_input.text().strip().lower()
        self._filtered_tasks = []
        for task in self._all_tasks:
            if self._status_filter != "ALL":
                if (task.get("status") or "").upper() != self._status_filter:
                    continue
            if self._lane_filter != "ALL":
                if (task.get("lane") or "").upper() != self._lane_filter:
                    continue
            if query:
                haystack = " ".join(
                    str(task.get(k) or "").lower() for k in ("title", "goal", "task_id")
                )
                if query not in haystack:
                    continue
            self._filtered_tasks.append(task)
        self._render_list()

    def _render_list(self) -> None:
        for w in self._row_widgets:
            self.list_layout.removeWidget(w)
            w.deleteLater()
        self._row_widgets.clear()

        if not self._filtered_tasks:
            self.inspector_stack.setCurrentIndex(0)
            return

        selected_idx = 0
        if self._selected_task_id:
            for idx, t in enumerate(self._filtered_tasks):
                if t.get("task_id") == self._selected_task_id:
                    selected_idx = idx
                    break

        for idx, task in enumerate(self._filtered_tasks):
            row = _TaskRowWidget(
                task,
                row_idx=idx,
                on_click=self._select_row_by_index,
                is_selected=(idx == selected_idx),
            )
            self.list_layout.insertWidget(self.list_layout.count() - 1, row)
            self._row_widgets.append(row)

        self._load_inspector(self._filtered_tasks[selected_idx])

    def _select_row_by_index(self, row_idx: int) -> None:
        if not (0 <= row_idx < len(self._filtered_tasks)):
            return
        for i, w in enumerate(self._row_widgets):
            w.set_selected(i == row_idx)
        self._load_inspector(self._filtered_tasks[row_idx])

    # ===================== inspector =====================

    def _load_inspector(self, task: dict) -> None:
        self._selected_task_id = task.get("task_id")
        self.inspector_stack.setCurrentIndex(1)

        status = (task.get("status") or "PENDING").upper()
        task_id = task.get("task_id") or ""
        bg, fg, border, label = _status_style(status)

        self.insp_status_pill.setText(label.capitalize())
        self.insp_status_pill.setStyleSheet(
            f"font-size:10px;font-weight:700;color:{fg};background:{bg};"
            f"border:1px solid {border};border-radius:{theme.RADIUS_SM}px;padding:3px 10px;"
        )
        self.insp_lane_pill.setText((task.get("lane") or "headless").capitalize())
        self.insp_title.setText(task.get("title") or task.get("goal") or "Untitled task")

        events = db.list_events(task_id=task_id, limit=250)

        # "Started … · Duration 34s · 12 tool calls" — assembled from parts
        # that may each be missing, so build a list and join what survives.
        started = (task.get("created_at") or "").replace("T", " ")[:16]
        meta_bits = []
        if started:
            meta_bits.append(f"Started {started}")
        if task.get("completed_at"):
            dur = format_duration(task_duration_seconds(task))
            if dur:
                meta_bits.append(f"Duration {dur}")
        calls = count_tool_calls(events)
        if calls:
            meta_bits.append(f"{calls} tool call{'s' if calls != 1 else ''}")
        self.insp_meta.setText("  ·  ".join(meta_bits))

        # -- Result tab ---------------------------------------------------------
        goal = task.get("goal") or ""
        result = task.get("result") or "(No result recorded)"
        failure = task.get("failure_reason")

        html = ""
        if goal and goal != task.get("title"):
            html += (
                f'<div style="background:{theme.INPUT_BG};border-radius:14px;'
                f'padding:14px 16px;margin-bottom:14px;">'
                f'<b style="color:{theme.TEXT_TERTIARY};font-size:11px;letter-spacing:0.3px;">'
                f'GOAL</b>'
                f'<p style="color:{theme.TEXT_PRIMARY};margin:6px 0 0 0;font-size:13px;'
                f'line-height:1.6;">{goal}</p></div>'
            )
        if failure:
            html += (
                f'<div style="background:{theme.DANGER_BG};border:1px solid {theme.DANGER_BORDER};'
                f'border-radius:14px;padding:12px 16px;margin-bottom:14px;">'
                f'<b style="color:{theme.DANGER_TEXT};font-size:12px;">Failure</b>'
                f'<p style="color:{theme.DANGER_TEXT};font-family:{theme.FONT_MONO};'
                f'font-size:12px;margin:6px 0 0 0;">{failure}</p></div>'
            )
        html += self._md_to_html(result)
        self.result_view.setHtml(html)

        # -- Events tab ---------------------------------------------------------
        self.events_count_lbl.setText(f"{len(events)} trace event(s) recorded")
        self._clear_column(self.events_layout)
        if events:
            for idx, ev in enumerate(events, start=1):
                self.events_layout.insertWidget(
                    self.events_layout.count() - 1, self._build_event_card(ev, idx)
                )
        else:
            self.events_layout.insertWidget(0, self._placeholder("No tool events recorded."))

        # -- Screenshots tab ----------------------------------------------------
        shots = self._extract_task_screenshots(task, events)
        self.shots_count_lbl.setText(f"{len(shots)} screenshot(s) recorded")
        self.insp_tabs.setTabText(2, f"Screenshots ({len(shots)})" if shots else "Screenshots")
        self._clear_column(self.shots_layout)
        if shots:
            for idx, shot in enumerate(shots, start=1):
                self.shots_layout.insertWidget(
                    self.shots_layout.count() - 1, self._build_screenshot_card(shot, idx)
                )
        else:
            self.shots_layout.insertWidget(
                0, self._placeholder("No screenshots captured for this task.")
            )

        # -- Raw JSON -----------------------------------------------------------
        self.raw_json_view.setPlainText(
            json.dumps(
                {
                    "task": task,
                    "events_count": len(events),
                    "screenshots_count": len(shots),
                    "events": events,
                },
                indent=2,
            )
        )

    @staticmethod
    def _clear_column(layout: QVBoxLayout) -> None:
        while layout.count() > 1:
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    @staticmethod
    def _placeholder(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(
            f"color:{theme.TEXT_TERTIARY};font-style:italic;padding:14px;background:transparent;"
        )
        return lbl

    # ===================== screenshot extraction =====================

    def _extract_task_screenshots(self, task: dict, events: list[dict]) -> list[dict]:
        """Collect every distinct screenshot referenced by a task.

        Three sources, deduped on normalised path: the approvals table, the
        event log (both structured JSON and raw Windows paths appearing in
        text), and the task's own goal/result text.
        """
        screenshots: list[dict] = []
        seen: set[str] = set()
        task_id = task.get("task_id")

        if task_id:
            try:
                with db.get_connection() as conn:
                    rows = conn.execute(
                        "SELECT * FROM pending_confirmations WHERE task_id = ?"
                        " ORDER BY created_at ASC",
                        (task_id,),
                    ).fetchall()
                for r in rows:
                    rd = dict(r)
                    p = rd.get("screenshot_path")
                    if not p:
                        continue
                    norm = os.path.normpath(p)
                    if norm in seen:
                        continue
                    seen.add(norm)
                    box = None
                    try:
                        raw_box = rd.get("candidate_box")
                        if raw_box:
                            box = json.loads(raw_box) if isinstance(raw_box, str) else raw_box
                    except Exception:
                        pass
                    screenshots.append({
                        "path": norm,
                        "source": f"Approval: {rd.get('action', 'action')}",
                        "timestamp": rd.get("created_at") or "",
                        "candidate_label": rd.get("candidate_label") or "",
                        "box": box,
                    })
            except Exception:
                pass

        for ev in events:
            eid = ev.get("event_id") or 0
            tool = ev.get("tool_call") or "tool"
            ts = ev.get("timestamp") or ""
            for raw in (ev.get("result"), ev.get("args")):
                if not raw:
                    continue
                try:
                    self._collect_from_json(json.loads(raw), tool, ts, eid, screenshots, seen)
                except Exception:
                    pass
                for m in re.finditer(r'([A-Za-z]:\\[^"\'\n\r\t]+\.png)', raw):
                    norm = os.path.normpath(m.group(1).replace("\\\\", "\\").strip())
                    if norm in seen:
                        continue
                    seen.add(norm)
                    screenshots.append({
                        "path": norm, "source": f"Tool: {tool}",
                        "timestamp": ts, "candidate_label": "", "box": None,
                    })
                if f"b64_{eid}" not in seen:
                    m_b64 = re.search(
                        r'["\'](?:image_base64|image_small_b64)["\']\s*:\s*'
                        r'["\'](iVBORw0KGgo[a-zA-Z0-9+/=]+)["\']',
                        raw,
                    )
                    if m_b64:
                        seen.add(f"b64_{eid}")
                        screenshots.append({
                            "b64": m_b64.group(1), "source": f"Tool: {tool} (inline)",
                            "timestamp": ts, "candidate_label": "", "box": None,
                        })

        for text, name in ((task.get("result"), "Task output"), (task.get("goal"), "Task goal")):
            if not text:
                continue
            for m in re.finditer(r'([A-Za-z]:\\[^"\'\n\r\t]+\.png)', str(text)):
                norm = os.path.normpath(m.group(1).replace("\\\\", "\\").strip())
                if norm in seen:
                    continue
                seen.add(norm)
                screenshots.append({
                    "path": norm, "source": name,
                    "timestamp": task.get("completed_at") or task.get("created_at") or "",
                    "candidate_label": "", "box": None,
                })

        return screenshots

    def _collect_from_json(
        self, data: object, tool: str, ts: str, eid: int,
        screenshots: list[dict], seen: set[str],
    ) -> None:
        if not isinstance(data, dict):
            return
        p = data.get("image_path") or data.get("screenshot_path")
        if p:
            norm = os.path.normpath(p)
            if norm not in seen:
                seen.add(norm)
                seen.add(f"b64_{eid}")
                screenshots.append({
                    "path": norm,
                    "b64": data.get("image_base64") or data.get("image_small_b64"),
                    "source": f"Tool: {tool}", "timestamp": ts,
                    "candidate_label": "", "box": None,
                })
            return
        b64 = data.get("image_base64") or data.get("image_small_b64")
        if b64 and f"b64_{eid}" not in seen:
            seen.add(f"b64_{eid}")
            screenshots.append({
                "b64": b64, "source": f"Tool: {tool}", "timestamp": ts,
                "candidate_label": "", "box": None,
            })
        if isinstance(data.get("content"), list):
            for item in data["content"]:
                if isinstance(item, dict) and "text" in item:
                    try:
                        self._collect_from_json(
                            json.loads(item["text"]), tool, ts, eid, screenshots, seen
                        )
                    except Exception:
                        pass

    # ===================== cards =====================

    def _build_screenshot_card(self, shot: dict, index: int) -> QWidget:
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background:{theme.INPUT_BG}; border:1px solid {theme.BORDER};"
            f" border-radius:{theme.RADIUS_LG}px; }}"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 14)
        lay.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel(f"#{index}  ·  {shot.get('source', '')}")
        title.setStyleSheet(
            f"font-size:12px;font-weight:600;color:{theme.TEXT_PRIMARY};background:transparent;"
        )
        top.addWidget(title)
        top.addStretch()
        ts = (shot.get("timestamp") or "").replace("T", " ")[:19]
        if ts:
            t = QLabel(ts)
            t.setStyleSheet(
                f"font-size:11px;color:{theme.TEXT_TERTIARY};background:transparent;"
            )
            top.addWidget(t)
        lay.addLayout(top)

        img_lbl = QLabel()
        img_lbl.setAlignment(Qt.AlignCenter)
        img_lbl.setStyleSheet(
            f"background:{theme.SURFACE};border:1px solid {theme.BORDER};"
            f"border-radius:10px;padding:6px;"
        )

        pixmap = QPixmap()
        path_str = shot.get("path") or ""
        p = Path(path_str) if path_str else None
        if p and p.exists():
            pixmap.load(str(p))
        elif shot.get("b64"):
            try:
                pixmap.loadFromData(base64.b64decode(shot["b64"]))
            except Exception:
                pass

        if not pixmap.isNull():
            box = shot.get("box")
            if box:
                painter = QPainter(pixmap)
                painter.setPen(QPen(QColor(theme.ACCENT), 3))
                left, top_, right, bottom = box
                painter.drawRect(QRect(left, top_, right - left, bottom - top_))
                painter.end()
            img_lbl.setPixmap(pixmap.scaled(560, 340, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            img_lbl.setText(f"Not on disk: {path_str}" if path_str else "(cannot render)")
            img_lbl.setFixedHeight(70)
        lay.addWidget(img_lbl)

        if path_str:
            bottom_row = QHBoxLayout()
            bottom_row.setSpacing(8)
            path_lbl = QLabel(path_str)
            path_lbl.setWordWrap(True)
            path_lbl.setStyleSheet(
                f"font-family:{theme.FONT_MONO};font-size:11px;"
                f"color:{theme.TEXT_TERTIARY};background:transparent;"
            )
            bottom_row.addWidget(path_lbl, stretch=1)

            copy_p = QPushButton("Copy path")
            copy_p.setCursor(Qt.PointingHandCursor)
            copy_p.setStyleSheet(self._ghost_button_sheet())
            copy_p.clicked.connect(
                lambda _=False, s=path_str: QGuiApplication.clipboard().setText(s)
            )
            bottom_row.addWidget(copy_p)

            if p and p.exists():
                open_btn = QPushButton("Open")
                open_btn.setCursor(Qt.PointingHandCursor)
                open_btn.setStyleSheet(self._ghost_button_sheet())
                open_btn.clicked.connect(
                    lambda _=False, s=str(p): os.startfile(s)  # noqa: S606
                    if hasattr(os, "startfile") else None
                )
                bottom_row.addWidget(open_btn)
            lay.addLayout(bottom_row)
        return card

    def _build_event_card(self, ev: dict, index: int) -> QWidget:
        error = ev.get("error")
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background:{theme.DANGER_BG if error else theme.INPUT_BG};"
            f" border:1px solid {theme.DANGER_BORDER if error else theme.BORDER};"
            f" border-radius:{theme.RADIUS_MD}px; }}"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 10, 14, 12)
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        idx = QLabel(f"#{index}")
        idx.setStyleSheet(
            f"font-size:11px;font-weight:700;color:{theme.TEXT_TERTIARY};background:transparent;"
        )
        top.addWidget(idx)

        tool = QLabel(ev.get("tool_call") or "event")
        tool.setStyleSheet(
            f"font-family:{theme.FONT_MONO};font-size:11px;font-weight:600;"
            f"color:{theme.ACCENT};background:{theme.ACCENT_LIGHT};"
            f"border-radius:{theme.RADIUS_XS}px;padding:2px 8px;"
        )
        top.addWidget(tool)
        top.addStretch()

        ts = QLabel((ev.get("timestamp") or "").replace("T", " ")[:19])
        ts.setStyleSheet(f"font-size:11px;color:{theme.TEXT_TERTIARY};background:transparent;")
        top.addWidget(ts)
        lay.addLayout(top)

        args = ev.get("args")
        if args and args != "null":
            a = QLabel(str(args)[:220])
            a.setWordWrap(True)
            a.setStyleSheet(
                f"font-family:{theme.FONT_MONO};font-size:11px;"
                f"color:{theme.TEXT_SECONDARY};background:transparent;"
            )
            lay.addWidget(a)

        if error:
            e = QLabel(str(error))
            e.setWordWrap(True)
            e.setStyleSheet(
                f"font-size:11px;color:{theme.DANGER_TEXT};font-weight:600;background:transparent;"
            )
            lay.addWidget(e)
        elif ev.get("result"):
            res = str(ev["result"])
            r = QLabel(res[:180] + ("…" if len(res) > 180 else ""))
            r.setWordWrap(True)
            r.setStyleSheet(
                f"font-size:11px;color:{theme.TEXT_SECONDARY};background:transparent;"
            )
            lay.addWidget(r)
        return card

    # ===================== actions =====================

    def _copy_task_id(self) -> None:
        if self._selected_task_id:
            QGuiApplication.clipboard().setText(self._selected_task_id)

    def _copy_raw_json(self) -> None:
        text = self.raw_json_view.toPlainText()
        if text:
            QGuiApplication.clipboard().setText(text)

    def _trigger_rerun(self) -> None:
        if not self._selected_task_id:
            return
        task = next(
            (t for t in self._all_tasks if t.get("task_id") == self._selected_task_id), None
        )
        if task:
            self.rerun_requested.emit(
                task.get("goal") or task.get("title") or "",
                task.get("lane") or "headless",
            )
