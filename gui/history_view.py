"""Full-window Task History & Execution Inspector for Orbit GUI.

Provides a modern, sleek Light Mode master-detail analytics and inspection
interface for all executed tasks, tool events timeline, screenshot gallery,
performance metrics, and replay controls.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Optional, Callable

from PySide6.QtCore import Qt, Signal, QRect
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPixmap, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from orbit import db
from gui import theme


class _TaskRowWidget(QFrame):
    """Inbox-style task list row with status dot, title, lane, and timestamp."""

    def __init__(
        self,
        task: dict,
        row_idx: int = 0,
        on_click = None,
        is_selected: bool = False,
        parent = None,
    ) -> None:
        super().__init__(parent)
        self.row_idx = row_idx
        self.on_click = on_click
        self.setCursor(Qt.PointingHandCursor)
        
        status = (task.get("status") or "PENDING").upper()
        title = task.get("title") or task.get("goal") or "Untitled Task"
        lane = (task.get("lane") or "headless").capitalize()
        created = (task.get("created_at") or "").replace("T", " ")[:16]

        dot_color = {
            "COMPLETED": theme.SUCCESS,
            "FAILED": theme.DANGER,
            "RUNNING": theme.WARNING,
            "CANCELLED": theme.TEXT_TERTIARY,
        }.get(status, theme.TEXT_TERTIARY)

        bg = theme.ACCENT_LIGHT if is_selected else "transparent"
        border_left = f"3px solid {theme.ACCENT}" if is_selected else "3px solid transparent"
        title_color = theme.ACCENT if is_selected else theme.TEXT_PRIMARY

        self.setStyleSheet(
            f"_TaskRowWidget {{ background: {bg}; border-left: {border_left}; border-bottom: 1px solid {theme.BORDER}; border-radius: 0px; }}"
            f"_TaskRowWidget:hover {{ background: {theme.ROW_HOVER if not is_selected else theme.ACCENT_LIGHT}; }}"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(3)

        top_h = QHBoxLayout()
        top_h.setContentsMargins(0, 0, 0, 0)
        top_h.setSpacing(8)

        dot = QLabel("●")
        dot.setStyleSheet(f"color: {dot_color}; font-size: 11px; background: transparent;")
        dot.setFixedWidth(12)
        dot.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        top_h.addWidget(dot)

        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {title_color}; background: transparent;")
        self.title_lbl.setToolTip(task.get("goal") or title)
        self.title_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        top_h.addWidget(self.title_lbl, stretch=1)

        lay.addLayout(top_h)

        bot_h = QHBoxLayout()
        bot_h.setContentsMargins(20, 0, 0, 0)
        bot_h.setSpacing(6)

        meta_lbl = QLabel(f"{lane}  ·  {created}")
        meta_lbl.setStyleSheet(f"font-size: 11px; color: {theme.TEXT_TERTIARY}; background: transparent;")
        meta_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        bot_h.addWidget(meta_lbl)
        bot_h.addStretch()

        lay.addLayout(bot_h)

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        if self.on_click is not None:
            self.on_click(self.row_idx)

    @property
    def title_color(self) -> str:
        return self.title_lbl.styleSheet()
    
    @title_color.setter
    def title_color(self, color: str) -> None:
        self.title_lbl.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {color}; background: transparent;")

class TaskHistoryView(QWidget):
    """Full-window master-detail Task History, Inspector & Analytics view."""

    rerun_requested = Signal(str, str)  # (goal, lane)

    def __init__(self, md_renderer: Callable[[str], str], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._md_to_html = md_renderer
        self._all_tasks: list[dict] = []
        self._filtered_tasks: list[dict] = []
        self._selected_task_id: str | None = None
        self._active_status_filter = "ALL"
        self._active_lane_filter = "ALL"

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(32, 20, 32, 16)
        main_layout.setSpacing(14)

        # -- Search & Filter Toolbar ------------------------------------------
        filter_card = QFrame()
        filter_card.setStyleSheet(
            f"QFrame {{ background: {theme.SURFACE}; border-radius: {theme.RADIUS_MD}px; border: none; }}"
        )
        theme.apply_drop_shadow(filter_card, 'sm')

        filter_layout = QHBoxLayout(filter_card)
        filter_layout.setContentsMargins(16, 10, 16, 10)
        filter_layout.setSpacing(12)

        # Search Bar
        self.search_input = QLineEdit()
        self.search_input.setFixedHeight(44)
        self.search_input.setPlaceholderText("🔍  Search tasks by title, goal, or ID...")
        self.search_input.setStyleSheet(
            f"QLineEdit {{ background: {theme.INPUT_BG}; border: none; border-radius: 22px; padding: 8px 20px; font-size: 13px; color: {theme.TEXT_PRIMARY}; }}"
            f"QLineEdit:focus {{ border: 1.5px solid {theme.ACCENT}; background: {theme.SURFACE}; }}"
        )
        self.search_input.textChanged.connect(self._apply_filters)
        filter_layout.addWidget(self.search_input, stretch=2)

        # Status Filter Pills
        self.status_btns: dict[str, QPushButton] = {}
        for s in ["ALL", "COMPLETED", "FAILED", "RUNNING"]:
            btn = QPushButton(s.capitalize() if s != "ALL" else "All Statuses")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _, st=s: self._set_status_filter(st))
            self.status_btns[s] = btn
            filter_layout.addWidget(btn)
        self._update_status_btn_styles()

        # Lane Filter Pills
        filter_layout.addSpacing(6)
        self.lane_btns: dict[str, QPushButton] = {}
        for l in ["ALL", "HEADLESS", "FOREGROUND"]:
            btn = QPushButton(l.capitalize() if l != "ALL" else "All Lanes")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _, ln=l: self._set_lane_filter(ln))
            self.lane_btns[l] = btn
            filter_layout.addWidget(btn)
        self._update_lane_btn_styles()

        filter_layout.addStretch()

        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.setCursor(Qt.PointingHandCursor)
        refresh_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.SURFACE}; border: 1px solid {theme.BORDER_INPUT}; border-radius: {theme.RADIUS_SM}px; "
            f"padding: 6px 14px; font-weight: 600; color: {theme.TEXT_SECONDARY}; font-size: 12px; }}"
            f"QPushButton:hover {{ background: {theme.INPUT_BG}; color: {theme.TEXT_PRIMARY}; }}"
        )
        refresh_btn.clicked.connect(self.refresh)
        filter_layout.addWidget(refresh_btn)

        main_layout.addWidget(filter_card)

        # -- Master-Detail Splitter -------------------------------------------
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setStyleSheet(f"QSplitter::handle {{ background: {theme.BORDER}; }}")

        # LEFT MASTER: Tasks Directory
        left_container = QFrame()
        left_container.setStyleSheet(
            f"QFrame {{ background: {theme.SURFACE}; border: none; border-radius: {theme.RADIUS_LG}px; }}"
        )
        theme.apply_drop_shadow(left_container, 'md')

        left_lay = QVBoxLayout(left_container)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(0)

        left_header = QFrame()
        left_header.setStyleSheet(f"background: {theme.BG_CANVAS}; border-bottom: 1px solid {theme.BORDER}; padding: 12px 18px; border-top-left-radius: {theme.RADIUS_LG}px; border-top-right-radius: {theme.RADIUS_LG}px;")
        lh_layout = QHBoxLayout(left_header)
        lh_layout.setContentsMargins(0, 0, 0, 0)
        self.task_list_title = QLabel("TASK DIRECTORY")
        self.task_list_title.setStyleSheet(f"font-size: 11px; font-weight: 600; color: {theme.TEXT_SECONDARY}; letter-spacing: 0.6px; background: transparent;")
        lh_layout.addWidget(self.task_list_title)
        left_lay.addWidget(left_header)

        self.table = QTableWidget(0, 1)
        self.table.horizontalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setStyleSheet(
            f"QTableWidget {{ background: {theme.SURFACE}; border: none; alternate-background-color: {theme.BG_CANVAS}; outline: none; border-bottom-left-radius: {theme.RADIUS_LG}px; border-bottom-right-radius: {theme.RADIUS_LG}px; }}"
            f"QTableWidget::item {{ padding: 12px 16px; border-bottom: 1px solid {theme.BORDER}; color: {theme.TEXT_PRIMARY}; }}"
            f"QTableWidget::item:hover {{ background: {theme.ROW_HOVER}; }}"
            f"QTableWidget::item:selected {{ background: {theme.ACCENT_LIGHT}; color: {theme.ACCENT}; }}"
            f"QHeaderView::section {{ background: {theme.BG_CANVAS}; border: none; border-bottom: 1px solid {theme.BORDER}; "
            f"padding: 10px 12px; font-weight: 600; font-size: 11px; color: {theme.TEXT_SECONDARY}; text-transform: uppercase; letter-spacing: 0.5px; }}"
        )
        self.table.itemSelectionChanged.connect(self._on_table_selection)
        left_lay.addWidget(self.table, stretch=1)
        splitter.addWidget(left_container)

        # RIGHT DETAIL: Task Inspector Panel
        self.right_container = QFrame()
        self.right_container.setStyleSheet(
            f"QFrame {{ background: {theme.SURFACE}; border: none; border-radius: {theme.RADIUS_LG}px; }}"
        )
        theme.apply_drop_shadow(self.right_container, 'md')

        right_lay = QVBoxLayout(self.right_container)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(0)

        # Stacked Widget (Page 0 = Empty Inspector, Page 1 = Active Inspector)
        self.inspector_stack = QStackedWidget()

        # Page 0: Empty Inspector
        empty_insp = QWidget()
        ei_lay = QVBoxLayout(empty_insp)
        ei_lay.setAlignment(Qt.AlignCenter)
        ei_lay.setSpacing(10)
        ei_text = QLabel("Select a task to inspect execution details")
        ei_text.setStyleSheet(f"font-size: 15px; font-weight: 600; color: {theme.TEXT_SECONDARY}; background: transparent;")
        ei_text.setAlignment(Qt.AlignCenter)
        ei_lay.addWidget(ei_text)
        self.inspector_stack.addWidget(empty_insp)

        # Page 1: Active Inspector Panel
        active_insp = QWidget()
        ai_lay = QVBoxLayout(active_insp)
        ai_lay.setContentsMargins(20, 18, 20, 18)
        ai_lay.setSpacing(14)

        # Task Header Info Card
        header_card = QFrame()
        header_card.setStyleSheet(
            f"QFrame {{ background: {theme.BG_CANVAS}; border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS_MD}px; padding: 12px 16px; }}"
        )
        hc_lay = QVBoxLayout(header_card)
        hc_lay.setContentsMargins(12, 10, 12, 10)
        hc_lay.setSpacing(8)

        top_meta = QHBoxLayout()
        top_meta.setSpacing(8)

        self.insp_status_pill = QLabel("COMPLETED")
        self.insp_status_pill.setStyleSheet(
            f"font-size: 11px; font-weight: 800; color: {theme.SUCCESS_TEXT}; background: {theme.SUCCESS_BG}; "
            f"border: 1px solid {theme.SUCCESS_BORDER}; border-radius: 12px; padding: 4px 10px;"
        )
        top_meta.addWidget(self.insp_status_pill)

        self.insp_lane_pill = QLabel("HEADLESS")
        self.insp_lane_pill.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {theme.ACCENT}; background: {theme.ACCENT_LIGHT}; "
            f"border: 1px solid {theme.ACCENT_BORDER}; border-radius: 12px; padding: 4px 10px;"
        )
        top_meta.addWidget(self.insp_lane_pill)

        self.insp_task_id = QLabel("task-xxxx")
        self.insp_task_id.setStyleSheet(
            f"font-size: 11px; font-family: {theme.FONT_MONO}; color: {theme.TEXT_SECONDARY}; "
            f"background: {theme.INPUT_BG}; border: 1px solid {theme.BORDER_INPUT}; border-radius: 12px; padding: 4px 10px;"
        )
        top_meta.addWidget(self.insp_task_id)

        top_meta.addStretch()

        copy_id_btn = QPushButton("Copy ID")
        copy_id_btn.setCursor(Qt.PointingHandCursor)
        copy_id_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.SURFACE}; border: 1px solid {theme.BORDER_INPUT}; border-radius: {theme.RADIUS_SM}px; "
            f"padding: 5px 12px; font-size: 11px; font-weight: 600; color: {theme.TEXT_SECONDARY}; }}"
            f"QPushButton:hover {{ background: {theme.INPUT_BG}; color: {theme.TEXT_PRIMARY}; }}"
        )
        copy_id_btn.clicked.connect(self._copy_task_id)
        top_meta.addWidget(copy_id_btn)

        self.rerun_btn = QPushButton("⚡ Re-run in Workbench")
        self.rerun_btn.setCursor(Qt.PointingHandCursor)
        self.rerun_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.ACCENT}; border: none; border-radius: {theme.RADIUS_SM}px; "
            f"padding: 6px 16px; font-size: 12px; font-weight: 700; color: white; }}"
            f"QPushButton:hover {{ background: {theme.ACCENT_HOVER}; }}"
        )
        self.rerun_btn.clicked.connect(self._trigger_rerun)
        top_meta.addWidget(self.rerun_btn)

        hc_lay.addLayout(top_meta)

        self.insp_title = QLabel("Task Title")
        self.insp_title.setStyleSheet(f"font-size: 22px; font-weight: 600; color: {theme.TEXT_PRIMARY}; line-height: 1.3; background: transparent;")
        self.insp_title.setWordWrap(True)
        hc_lay.addWidget(self.insp_title)

        self.insp_timestamps = QLabel("Created: - | Completed: -")
        self.insp_timestamps.setStyleSheet(f"font-size: 12px; color: {theme.TEXT_TERTIARY}; background: transparent;")
        hc_lay.addWidget(self.insp_timestamps)

        ai_lay.addWidget(header_card)

        # Inspector Tabs (Result, Event Logs, Screenshots, Raw Data)
        self.insp_tabs = QTabWidget()
        self.insp_tabs.setStyleSheet(
            f"QTabWidget::pane {{ border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS_MD}px; background: {theme.SURFACE}; }}"
            f"QTabBar::tab {{ background: transparent; border: none; border-bottom: 2px solid transparent; "
            f"padding: 10px 24px; font-weight: 600; font-size: 13px; color: {theme.TEXT_SECONDARY}; margin-right: 6px; }}"
            f"QTabBar::tab:hover {{ color: {theme.TEXT_PRIMARY}; }}"
            f"QTabBar::tab:selected {{ color: {theme.ACCENT}; font-weight: 600; border-bottom: 2px solid {theme.ACCENT}; }}"
        )

        # Tab 1: Result & Summary
        result_tab = QWidget()
        rt_lay = QVBoxLayout(result_tab)
        rt_lay.setContentsMargins(14, 14, 14, 14)
        rt_lay.setSpacing(10)

        self.result_view = QTextEdit()
        self.result_view.setReadOnly(True)
        self.result_view.setStyleSheet(f"border: none; font-size: 14px; color: {theme.TEXT_PRIMARY}; line-height: 1.6; background: transparent;")
        rt_lay.addWidget(self.result_view)
        self.insp_tabs.addTab(result_tab, "📄 Result & Summary")

        # Tab 2: Tool Event Traces Timeline
        events_tab = QWidget()
        et_lay = QVBoxLayout(events_tab)
        et_lay.setContentsMargins(14, 14, 14, 14)
        et_lay.setSpacing(10)

        self.events_count_lbl = QLabel("0 tool events recorded")
        self.events_count_lbl.setStyleSheet(f"font-size: 11px; font-weight: 600; color: {theme.TEXT_SECONDARY}; background: transparent;")
        et_lay.addWidget(self.events_count_lbl)

        self.events_scroll = QScrollArea()
        self.events_scroll.setWidgetResizable(True)
        self.events_scroll.setFrameShape(QFrame.NoFrame)
        self.events_scroll.setStyleSheet("background: transparent; border: none;")

        self.events_inner = QWidget()
        self.events_inner.setStyleSheet("background: transparent;")
        self.events_layout = QVBoxLayout(self.events_inner)
        self.events_layout.setContentsMargins(0, 0, 0, 0)
        self.events_layout.setSpacing(8)
        self.events_layout.addStretch()

        self.events_scroll.setWidget(self.events_inner)
        et_lay.addWidget(self.events_scroll, stretch=1)
        self.insp_tabs.addTab(events_tab, "🛠️ Tool Traces Timeline")

        # Tab 3: Screenshots Gallery
        shots_tab = QWidget()
        st_lay = QVBoxLayout(shots_tab)
        st_lay.setContentsMargins(14, 14, 14, 14)
        st_lay.setSpacing(10)

        self.shots_count_lbl = QLabel("0 screenshots recorded")
        self.shots_count_lbl.setStyleSheet(f"font-size: 11px; font-weight: 600; color: {theme.TEXT_SECONDARY}; background: transparent;")
        st_lay.addWidget(self.shots_count_lbl)

        self.shots_scroll = QScrollArea()
        self.shots_scroll.setWidgetResizable(True)
        self.shots_scroll.setFrameShape(QFrame.NoFrame)
        self.shots_scroll.setStyleSheet("background: transparent; border: none;")

        self.shots_inner = QWidget()
        self.shots_inner.setStyleSheet("background: transparent;")
        self.shots_layout = QVBoxLayout(self.shots_inner)
        self.shots_layout.setContentsMargins(0, 0, 0, 0)
        self.shots_layout.setSpacing(12)
        self.shots_layout.addStretch()

        self.shots_scroll.setWidget(self.shots_inner)
        st_lay.addWidget(self.shots_scroll, stretch=1)
        self.insp_tabs.addTab(shots_tab, "🖼️ Screenshots (0)")

        # Tab 4: Raw JSON Dump
        raw_tab = QWidget()
        raw_lay = QVBoxLayout(raw_tab)
        raw_lay.setContentsMargins(14, 14, 14, 14)
        raw_lay.setSpacing(10)

        raw_tools_h = QHBoxLayout()
        raw_tools_h.addStretch()
        copy_json_btn = QPushButton("Copy JSON")
        copy_json_btn.setCursor(Qt.PointingHandCursor)
        copy_json_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.SURFACE}; border: 1px solid {theme.BORDER_INPUT}; border-radius: {theme.RADIUS_SM}px; "
            f"padding: 5px 12px; font-size: 11px; font-weight: 600; color: {theme.TEXT_SECONDARY}; }}"
            f"QPushButton:hover {{ background: {theme.INPUT_BG}; color: {theme.TEXT_PRIMARY}; }}"
        )
        copy_json_btn.clicked.connect(self._copy_raw_json)
        raw_tools_h.addWidget(copy_json_btn)
        raw_lay.addLayout(raw_tools_h)

        self.raw_json_view = QTextEdit()
        self.raw_json_view.setReadOnly(True)
        self.raw_json_view.setStyleSheet(
            f"border: 1px solid {theme.BORDER}; border-radius: {theme.RADIUS_MD}px; font-family: {theme.FONT_MONO}; "
            f"font-size: 13px; background: {theme.INPUT_BG}; color: {theme.TEXT_PRIMARY}; padding: 12px;"
        )
        raw_lay.addWidget(self.raw_json_view)
        self.insp_tabs.addTab(raw_tab, "🔍 Raw JSON Log")

        ai_lay.addWidget(self.insp_tabs, stretch=1)
        self.inspector_stack.addWidget(active_insp)

        right_lay.addWidget(self.inspector_stack, stretch=1)
        splitter.addWidget(self.right_container)

        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 6)

        main_layout.addWidget(splitter, stretch=1)

        self.refresh()

    # -- Data Fetch & Refresh -------------------------------------------------

    def refresh(self) -> None:
        db.init_db()
        self._all_tasks = db.list_tasks(limit=250)
        self._update_kpi_cards()
        self._apply_filters()

    def _update_kpi_cards(self) -> None:
        total = len(self._all_tasks)
        completed = sum(1 for t in self._all_tasks if t.get("status") == "COMPLETED")
        failed = sum(1 for t in self._all_tasks if t.get("status") == "FAILED")
        rate = f"{int((completed / total) * 100)}%" if total > 0 else "0%"
        
    def _set_status_filter(self, status: str) -> None:
        self._active_status_filter = status
        self._update_status_btn_styles()
        self._apply_filters()

    def _set_lane_filter(self, lane: str) -> None:
        self._active_lane_filter = lane
        self._update_lane_btn_styles()
        self._apply_filters()

    def _update_status_btn_styles(self) -> None:
        for s, btn in self.status_btns.items():
            if s == self._active_status_filter:
                btn.setStyleSheet(
                    f"QPushButton {{ background: {theme.TEXT_PRIMARY}; color: white; border: none; "
                    f"border-radius: 16px; padding: 6px 14px; font-weight: 700; font-size: 11px; }}"
                )
            else:
                btn.setStyleSheet(
                    f"QPushButton {{ background: {theme.INPUT_BG}; color: {theme.TEXT_SECONDARY}; border: none; "
                    f"border-radius: 16px; padding: 6px 14px; font-weight: 600; font-size: 11px; }}"
                    f"QPushButton:hover {{ background: {theme.SURFACE}; color: {theme.TEXT_PRIMARY}; }}"
                )

    def _update_lane_btn_styles(self) -> None:
        for l, btn in self.lane_btns.items():
            if l == self._active_lane_filter:
                btn.setStyleSheet(
                    f"QPushButton {{ background: {theme.TEXT_PRIMARY}; color: white; border: none; "
                    f"border-radius: 16px; padding: 6px 14px; font-weight: 700; font-size: 11px; }}"
                )
            else:
                btn.setStyleSheet(
                    f"QPushButton {{ background: {theme.INPUT_BG}; color: {theme.TEXT_SECONDARY}; border: none; "
                    f"border-radius: 16px; padding: 6px 14px; font-weight: 600; font-size: 11px; }}"
                    f"QPushButton:hover {{ background: {theme.SURFACE}; color: {theme.TEXT_PRIMARY}; }}"
                )

    def _apply_filters(self) -> None:
        query = self.search_input.text().strip().lower()
        self._filtered_tasks = []

        for task in self._all_tasks:
            t_status = (task.get("status") or "").upper()
            t_lane = (task.get("lane") or "").upper()
            t_title = (task.get("title") or "").lower()
            t_goal = (task.get("goal") or "").lower()
            t_id = (task.get("task_id") or "").lower()

            if self._active_status_filter != "ALL" and t_status != self._active_status_filter:
                continue
            if self._active_lane_filter != "ALL" and t_lane != self._active_lane_filter:
                continue
            if query and not (query in t_title or query in t_goal or query in t_id):
                continue

            self._filtered_tasks.append(task)

        self._render_table()

    def _render_table(self) -> None:
        self.table.setRowCount(len(self._filtered_tasks))
        
        # Retain selection or select first
        selected_idx = 0
        if self._filtered_tasks and self._selected_task_id:
            for idx, t in enumerate(self._filtered_tasks):
                if t.get("task_id") == self._selected_task_id:
                    selected_idx = idx
                    break

        for row_idx, task in enumerate(self._filtered_tasks):
            is_sel = (row_idx == selected_idx)
            row_widget = _TaskRowWidget(
                task,
                row_idx=row_idx,
                on_click=self._select_row_by_index,
                is_selected=is_sel
            )
            self.table.setCellWidget(row_idx, 0, row_widget)
            self.table.setRowHeight(row_idx, 68)

        if self._filtered_tasks:
            self.table.selectRow(selected_idx)
            self._load_inspector(self._filtered_tasks[selected_idx])
        else:
            self.inspector_stack.setCurrentIndex(0)

    def _select_row_by_index(self, row_idx: int) -> None:
        if not (0 <= row_idx < len(self._filtered_tasks)):
            return
        self.table.selectRow(row_idx)
        task = self._filtered_tasks[row_idx]
        self._selected_task_id = task.get("task_id")

        # Update row widget visual selection styles
        for r in range(self.table.rowCount()):
            w = self.table.cellWidget(r, 0)
            if isinstance(w, _TaskRowWidget):
                is_sel = (r == row_idx)
                bg = theme.ACCENT_LIGHT if is_sel else "transparent"
                border_left = f"3px solid {theme.ACCENT}" if is_sel else "3px solid transparent"
                w.setStyleSheet(
                    f"_TaskRowWidget {{ background: {bg}; border-left: {border_left}; "
                    f"border-bottom: 1px solid {theme.BORDER}; border-radius: 0px; }}"
                    f"_TaskRowWidget:hover {{ background: {theme.ROW_HOVER if not is_sel else theme.ACCENT_LIGHT}; }}"
                )
                w.title_color = theme.ACCENT if is_sel else theme.TEXT_PRIMARY

        self._load_inspector(task)

    def _on_table_selection(self) -> None:
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            return
        self._select_row_by_index(selected_rows[0].row())

    # -- Inspector Rendering --------------------------------------------------

    def _load_inspector(self, task: dict) -> None:
        self._selected_task_id = task.get("task_id")
        self.inspector_stack.setCurrentIndex(1)

        status = task.get("status") or "PENDING"
        lane = (task.get("lane") or "headless").upper()
        task_id = task.get("task_id") or ""
        title = task.get("title") or task.get("goal") or "Untitled Task"
        created = (task.get("created_at") or "").replace("T", " ")[:19]
        completed = (task.get("completed_at") or "Ongoing").replace("T", " ")[:19]

        self.insp_title.setText(title)
        self.insp_task_id.setText(f"ID: {task_id}")
        self.insp_lane_pill.setText(lane)
        self.insp_timestamps.setText(f"Created: {created}  |  Completed: {completed}")

        # Status Pill Style
        if status == "COMPLETED":
            self.insp_status_pill.setText("COMPLETED ✓")
            self.insp_status_pill.setStyleSheet(
                f"font-size: 11px; font-weight: 800; color: {theme.SUCCESS_TEXT}; background: {theme.SUCCESS_BG}; "
                f"border: 1px solid {theme.SUCCESS_BORDER}; border-radius: 12px; padding: 4px 10px;"
            )
        elif status == "FAILED":
            self.insp_status_pill.setText("FAILED ✗")
            self.insp_status_pill.setStyleSheet(
                f"font-size: 11px; font-weight: 800; color: {theme.DANGER_TEXT}; background: {theme.DANGER_BG}; "
                f"border: 1px solid {theme.DANGER_BORDER}; border-radius: 12px; padding: 4px 10px;"
            )
        elif status == "CANCELLED":
            self.insp_status_pill.setText("CANCELLED")
            self.insp_status_pill.setStyleSheet(
                f"font-size: 11px; font-weight: 800; color: {theme.TEXT_SECONDARY}; background: {theme.INPUT_BG}; "
                f"border: 1px solid {theme.BORDER_INPUT}; border-radius: 12px; padding: 4px 10px;"
            )
        else:
            self.insp_status_pill.setText(f"{status} ●")
            self.insp_status_pill.setStyleSheet(
                f"font-size: 11px; font-weight: 800; color: {theme.WARNING_TEXT}; background: {theme.WARNING_BG}; "
                f"border: 1px solid {theme.WARNING_BORDER}; border-radius: 12px; padding: 4px 10px;"
            )

        # Tab 1: Render Result / Goal HTML
        goal = task.get("goal") or ""
        result = task.get("result") or "(No result recorded)"
        failure = task.get("failure_reason")

        result_html = ""
        if goal and goal != title:
            result_html += (
                f'<div style="background:{theme.INPUT_BG};border:1px solid {theme.BORDER};border-radius:10px;padding:12px 16px;margin-bottom:14px;">'
                f'<b style="color:{theme.TEXT_TERTIARY};font-size:11px;text-transform:uppercase;letter-spacing:0.5px;">Goal Description:</b>'
                f'<p style="color:{theme.TEXT_PRIMARY};margin:6px 0 0 0;font-size:13px;line-height:1.5;">{goal}</p></div>'
            )

        if failure:
            result_html += (
                f'<div style="background:{theme.DANGER_BG};border-left:4px solid {theme.DANGER};border-radius:10px;padding:12px 16px;margin-bottom:14px;">'
                f'<b style="color:{theme.DANGER_TEXT};font-size:12px;">⚠️ Failure Diagnostic:</b>'
                f'<p style="color:{theme.DANGER_TEXT};font-family:{theme.FONT_MONO};font-size:12px;margin:6px 0 0 0;">{failure}</p></div>'
            )

        result_html += self._md_to_html(result)
        self.result_view.setHtml(result_html)

        # Fetch granular events
        events = db.list_events(task_id=task_id, limit=250)

        # Tab 2: Render Tool Events Timeline
        self.events_count_lbl.setText(f"{len(events)} tool trace events recorded for this task")
        while self.events_layout.count() > 1:
            child = self.events_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        if events:
            for idx, ev in enumerate(events):
                card = self._build_event_card(ev, idx + 1)
                self.events_layout.insertWidget(self.events_layout.count() - 1, card)
        else:
            no_ev = QLabel("No granular tool events recorded for this task.")
            no_ev.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-style: italic; padding: 14px; background: transparent;")
            self.events_layout.insertWidget(0, no_ev)

        # Tab 3: Render Screenshots Gallery
        screenshots = self._extract_task_screenshots(task, events)
        self.shots_count_lbl.setText(f"{len(screenshots)} screenshots recorded for this task")
        self.insp_tabs.setTabText(2, f"🖼️ Screenshots ({len(screenshots)})")

        while self.shots_layout.count() > 1:
            child = self.shots_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        if screenshots:
            for idx, shot_data in enumerate(screenshots):
                card = self._build_screenshot_card(shot_data, idx + 1)
                self.shots_layout.insertWidget(self.shots_layout.count() - 1, card)
        else:
            no_shot = QLabel("No screenshots captured for this task. Screenshots are recorded during visual perception and confirmation requests.")
            no_shot.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-style: italic; padding: 14px; background: transparent;")
            no_shot.setWordWrap(True)
            self.shots_layout.insertWidget(0, no_shot)

        # Tab 4: Raw JSON Dump
        full_dump = {
            "task": task,
            "events_count": len(events),
            "screenshots_count": len(screenshots),
            "screenshots": [s.get("path") or f"inline_base64_({len(s.get('b64', ''))} chars)" for s in screenshots],
            "events": events,
        }
        self.raw_json_view.setPlainText(json.dumps(full_dump, indent=2))

    def _extract_task_screenshots(self, task: dict, events: list[dict]) -> list[dict]:
        """Extract all distinct screenshot records associated with a task."""
        screenshots: list[dict] = []
        seen_identifiers: set[str] = set()
        task_id = task.get("task_id")

        # 1. From pending_confirmations table
        if task_id:
            try:
                with db.get_connection() as conn:
                    c_rows = conn.execute(
                        "SELECT * FROM pending_confirmations WHERE task_id = ? ORDER BY created_at ASC",
                        (task_id,)
                    ).fetchall()
                    for r in c_rows:
                        r_dict = dict(r)
                        p = r_dict.get("screenshot_path")
                        if p:
                            norm_p = os.path.normpath(p)
                            if norm_p not in seen_identifiers:
                                seen_identifiers.add(norm_p)
                                box = None
                                try:
                                    if r_dict.get("candidate_box"):
                                        box = json.loads(r_dict["candidate_box"]) if isinstance(r_dict["candidate_box"], str) else r_dict["candidate_box"]
                                except Exception:
                                    pass
                                screenshots.append({
                                    "path": norm_p,
                                    "source": f"Approval Request: {r_dict.get('action', 'Action')}",
                                    "timestamp": r_dict.get("created_at") or "",
                                    "candidate_label": r_dict.get("candidate_label") or "",
                                    "box": box,
                                })
            except Exception:
                pass

        # 2. From events table
        for ev in events:
            eid = ev.get("event_id") or 0
            tool = ev.get("tool_call") or "Tool Action"
            ts = ev.get("timestamp") or ""

            for raw_content, field_name in [(ev.get("result"), "result"), (ev.get("args"), "args")]:
                if not raw_content:
                    continue

                try:
                    data = json.loads(raw_content)
                    self._collect_from_json(data, tool, ts, eid, screenshots, seen_identifiers)
                except Exception:
                    pass

                for m in re.finditer(r'([A-Za-z]:\\[^"\'\n\r\t]+\.png)', raw_content):
                    raw_p = m.group(1).replace('\\\\', '\\').strip()
                    norm_p = os.path.normpath(raw_p)
                    if norm_p not in seen_identifiers:
                        seen_identifiers.add(norm_p)
                        screenshots.append({
                            "path": norm_p,
                            "source": f"Tool: {tool}",
                            "timestamp": ts,
                            "candidate_label": "",
                            "box": None,
                        })

                if f"b64_{eid}" not in seen_identifiers:
                    m_b64 = re.search(r'[\"\'](?:image_base64|image_small_b64)[\"\']\s*:\s*[\"\'](iVBORw0KGgo[a-zA-Z0-9+/=]+)[\"\']', raw_content)
                    if m_b64:
                        seen_identifiers.add(f"b64_{eid}")
                        screenshots.append({
                            "b64": m_b64.group(1),
                            "source": f"Tool: {tool} (inline capture)",
                            "timestamp": ts,
                            "candidate_label": "",
                            "box": None,
                        })

        # 3. From task output or goal text
        for text_source, src_name in [(task.get("result"), "Task Output"), (task.get("goal"), "Task Goal")]:
            if not text_source:
                continue
            for m in re.finditer(r'([A-Za-z]:\\[^"\'\n\r\t]+\.png)', str(text_source)):
                raw_p = m.group(1).replace('\\\\', '\\').strip()
                norm_p = os.path.normpath(raw_p)
                if norm_p not in seen_identifiers:
                    seen_identifiers.add(norm_p)
                    screenshots.append({
                        "path": norm_p,
                        "source": src_name,
                        "timestamp": task.get("completed_at") or task.get("created_at") or "",
                        "candidate_label": "",
                        "box": None,
                    })

        return screenshots

    def _collect_from_json(
        self,
        data: object,
        tool: str,
        ts: str,
        eid: int,
        screenshots: list[dict],
        seen_identifiers: set[str],
    ) -> None:
        """Recursively collect screenshot paths and base64 images from JSON structures."""
        if not isinstance(data, dict):
            return

        p = data.get("image_path") or data.get("screenshot_path")
        if p:
            norm_p = os.path.normpath(p)
            if norm_p not in seen_identifiers:
                seen_identifiers.add(norm_p)
                b64 = data.get("image_base64") or data.get("image_small_b64")
                screenshots.append({
                    "path": norm_p,
                    "b64": b64,
                    "source": f"Tool: {tool}",
                    "timestamp": ts,
                    "candidate_label": "",
                    "box": None,
                })
                seen_identifiers.add(f"b64_{eid}")
                return

        b64 = data.get("image_base64") or data.get("image_small_b64")
        if b64 and f"b64_{eid}" not in seen_identifiers:
            seen_identifiers.add(f"b64_{eid}")
            screenshots.append({
                "b64": b64,
                "source": f"Tool: {tool}",
                "timestamp": ts,
                "candidate_label": "",
                "box": None,
            })

        if isinstance(data.get("content"), list):
            for item in data["content"]:
                if isinstance(item, dict) and "text" in item:
                    try:
                        inner = json.loads(item["text"])
                        self._collect_from_json(inner, tool, ts, eid, screenshots, seen_identifiers)
                    except Exception:
                        pass

    def _build_screenshot_card(self, shot_data: dict, index: int) -> QWidget:
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background: {theme.INPUT_BG}; border: 1px solid {theme.BORDER}; "
            f"border-radius: {theme.RADIUS_MD}px; padding: 14px; }}"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)

        # Header row
        top_h = QHBoxLayout()
        title_lbl = QLabel(f"<b>Screenshot #{index}</b> &nbsp;·&nbsp; <span style='color:{theme.ACCENT};font-weight:700;'>{shot_data.get('source', '')}</span>")
        title_lbl.setStyleSheet(f"font-size: 13px; color: {theme.TEXT_PRIMARY}; background: transparent;")
        top_h.addWidget(title_lbl)
        top_h.addStretch()

        ts = (shot_data.get("timestamp") or "").replace("T", " ")[:19]
        if ts:
            time_lbl = QLabel(ts)
            time_lbl.setStyleSheet(f"font-size: 11px; color: {theme.TEXT_TERTIARY}; background: transparent;")
            top_h.addWidget(time_lbl)
        lay.addLayout(top_h)

        if shot_data.get("candidate_label"):
            lbl_widget = QLabel(f"<b>Target:</b> {shot_data['candidate_label']}")
            lbl_widget.setStyleSheet(f"font-size: 11px; color: {theme.TEXT_SECONDARY}; background: transparent;")
            lay.addWidget(lbl_widget)

        path_str = shot_data.get("path") or ""
        b64_data = shot_data.get("b64") or ""
        p = Path(path_str) if path_str else None

        # Image Viewer Frame
        img_frame = QFrame()
        img_frame.setStyleSheet(
            f"QFrame {{ background: {theme.SURFACE}; border: 1px solid {theme.BORDER}; "
            f"border-radius: 10px; padding: 6px; }}"
        )
        if_lay = QVBoxLayout(img_frame)
        if_lay.setContentsMargins(4, 4, 4, 4)
        if_lay.setAlignment(Qt.AlignCenter)

        img_lbl = QLabel()
        img_lbl.setAlignment(Qt.AlignCenter)

        pixmap = QPixmap()
        if p and p.exists():
            pixmap.load(str(p))
        elif b64_data:
            try:
                raw_bytes = base64.b64decode(b64_data)
                pixmap.loadFromData(raw_bytes)
            except Exception:
                pass

        if not pixmap.isNull():
            box = shot_data.get("box")
            if box:
                painter = QPainter(pixmap)
                painter.setPen(QPen(QColor(theme.ACCENT), 3))
                left, top, right, bottom = box
                painter.drawRect(QRect(left, top, right - left, bottom - top))
                painter.end()

            scaled = pixmap.scaled(
                580, 360, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            img_lbl.setPixmap(scaled)
        else:
            if path_str:
                img_lbl.setText(f"File not found on disk: {path_str}")
            else:
                img_lbl.setText("(Image could not be rendered)")
            img_lbl.setFixedHeight(70)

        if_lay.addWidget(img_lbl)
        lay.addWidget(img_frame)

        # Path & Action Toolbar Footer
        bot_h = QHBoxLayout()
        bot_h.setSpacing(8)

        display_text = path_str if path_str else "(Captured in-memory / event log)"
        path_lbl = QLabel(display_text)
        path_lbl.setStyleSheet(f"font-family: {theme.FONT_MONO}; font-size: 11px; color: {theme.TEXT_SECONDARY}; background: transparent;")
        path_lbl.setWordWrap(True)
        bot_h.addWidget(path_lbl, stretch=1)

        if path_str:
            copy_p_btn = QPushButton("Copy Path")
            copy_p_btn.setCursor(Qt.PointingHandCursor)
            copy_p_btn.setStyleSheet(
                f"QPushButton {{ background: {theme.SURFACE}; border: 1px solid {theme.BORDER_INPUT}; border-radius: {theme.RADIUS_SM}px; "
                f"padding: 5px 12px; font-size: 11px; font-weight: 600; color: {theme.TEXT_SECONDARY}; }}"
                f"QPushButton:hover {{ background: {theme.INPUT_BG}; color: {theme.TEXT_PRIMARY}; }}"
            )
            copy_p_btn.clicked.connect(lambda _, ps=path_str: QGuiApplication.clipboard().setText(ps))
            bot_h.addWidget(copy_p_btn)

            if p and p.exists():
                open_btn = QPushButton("↗ Open Image")
                open_btn.setCursor(Qt.PointingHandCursor)
                open_btn.setStyleSheet(
                    f"QPushButton {{ background: {theme.SURFACE}; border: 1px solid {theme.ACCENT_BORDER}; border-radius: {theme.RADIUS_SM}px; "
                    f"padding: 5px 12px; font-size: 11px; font-weight: 600; color: {theme.ACCENT}; }}"
                    f"QPushButton:hover {{ background: {theme.ACCENT_LIGHT}; }}"
                )
                open_btn.clicked.connect(lambda _, ps=str(p): os.startfile(ps) if hasattr(os, "startfile") else None)
                bot_h.addWidget(open_btn)

        lay.addLayout(bot_h)
        return card

    def _build_event_card(self, ev: dict, index: int) -> QWidget:
        card = QFrame()
        tool = ev.get("tool_call") or "general_event"
        error = ev.get("error")
        timestamp = (ev.get("timestamp") or "").replace("T", " ")[:19]

        bg_color = theme.DANGER_BG if error else theme.SURFACE
        border_color = theme.DANGER_BORDER if error else theme.BORDER

        card.setStyleSheet(
            f"QFrame {{ background: {bg_color}; border: 1px solid {border_color}; "
            f"border-radius: 10px; padding: 10px 14px; }}"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        top_h = QHBoxLayout()
        idx_lbl = QLabel(f"#{index}")
        idx_lbl.setStyleSheet(f"font-size: 11px; font-weight: 700; color: {theme.TEXT_SECONDARY}; background: transparent;")
        top_h.addWidget(idx_lbl)

        tool_lbl = QLabel(tool)
        tool_lbl.setStyleSheet(
            f"font-family: {theme.FONT_MONO}; font-size: 12px; font-weight: 700; color: {theme.ACCENT}; "
            f"background: {theme.ACCENT_LIGHT}; border: 1px solid {theme.ACCENT_BORDER}; border-radius: 6px; padding: 2px 8px;"
        )
        top_h.addWidget(tool_lbl)

        top_h.addStretch()

        time_lbl = QLabel(timestamp)
        time_lbl.setStyleSheet(f"font-size: 11px; color: {theme.TEXT_TERTIARY}; background: transparent;")
        top_h.addWidget(time_lbl)
        lay.addLayout(top_h)

        # Args & Result summary
        args = ev.get("args")
        if args and args != "null":
            args_lbl = QLabel(f"<b>Args:</b> <code style='color:{theme.TEXT_PRIMARY};'>{args}</code>")
            args_lbl.setWordWrap(True)
            args_lbl.setStyleSheet(f"font-size: 11px; background: transparent;")
            lay.addWidget(args_lbl)

        if error:
            err_lbl = QLabel(f"<b>Error:</b> <span style='color:{theme.DANGER_TEXT};'>{error}</span>")
            err_lbl.setWordWrap(True)
            err_lbl.setStyleSheet("font-size: 11px; background: transparent;")
            lay.addWidget(err_lbl)
        elif ev.get("result"):
            res_str = str(ev.get("result"))[:180] + ("..." if len(str(ev.get("result"))) > 180 else "")
            res_lbl = QLabel(f"<b>Result:</b> <span style='color:{theme.TEXT_SECONDARY};'>{res_str}</span>")
            res_lbl.setWordWrap(True)
            res_lbl.setStyleSheet("font-size: 11px; background: transparent;")
            lay.addWidget(res_lbl)

        return card

    # -- User Actions ---------------------------------------------------------

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
        task = next((t for t in self._all_tasks if t.get("task_id") == self._selected_task_id), None)
        if task:
            goal = task.get("goal") or task.get("title") or ""
            lane = task.get("lane") or "headless"
            self.rerun_requested.emit(goal, lane)
