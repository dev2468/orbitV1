"""TaskHistoryView: filtering, selection, and the KPI tiles.

The view renders its task list as a column of `_TaskRowWidget`s inside a
QScrollArea rather than a QTableWidget, so assertions go through
`_filtered_tasks` / `_row_widgets` — there is no `.table` and no
`.status_combo`. Filters are set through `_set_status_filter` /
`_set_lane_filter`, which is exactly what the pill buttons call.
"""

import sys

import pytest
from PySide6.QtWidgets import QApplication

from orbit import db
from gui.history_view import TaskHistoryView


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def view(app):
    hv = TaskHistoryView(md_renderer=lambda x: f"<p>{x}</p>")
    yield hv
    hv.deleteLater()


def test_history_view_filters_and_selection(app, view):
    task_id = db.create_task("Open GitHub in Chrome", lane="foreground")
    db.update_task_status(task_id, "COMPLETED", result="Success! Commit checked.")

    view.refresh()
    app.processEvents()

    # Initial load selects the newest task and shows the inspector.
    assert len(view._filtered_tasks) == 1
    assert len(view._row_widgets) == 1
    assert view._selected_task_id == task_id
    assert view.inspector_stack.currentIndex() == 1

    # Selecting a row keeps the inspector open.
    view._select_row_by_index(0)
    app.processEvents()
    assert view.inspector_stack.currentIndex() == 1
    assert view._row_widgets[0].title_lbl.text() == "Open GitHub in Chrome"

    # Search — matching, then not.
    view.search_input.setText("GitHub")
    app.processEvents()
    assert len(view._filtered_tasks) == 1

    view.search_input.setText("xyzNonExistent123")
    app.processEvents()
    assert view._filtered_tasks == []
    assert view.inspector_stack.currentIndex() == 0  # falls back to empty state

    view.search_input.clear()
    app.processEvents()
    assert len(view._filtered_tasks) == 1

    # Status filter — matching, then not.
    view._set_status_filter("COMPLETED")
    app.processEvents()
    assert len(view._filtered_tasks) == 1

    view._set_status_filter("FAILED")
    app.processEvents()
    assert view._filtered_tasks == []

    view._set_status_filter("ALL")
    app.processEvents()
    assert len(view._filtered_tasks) == 1

    # Lane filter — the task was created foreground.
    view._set_lane_filter("FOREGROUND")
    app.processEvents()
    assert len(view._filtered_tasks) == 1

    view._set_lane_filter("HEADLESS")
    app.processEvents()
    assert view._filtered_tasks == []


def test_kpi_tiles_reflect_task_outcomes(app, view):
    """The tiles are driven by gui.stats.compute_kpis, so this checks wiring.

    Success rate is over decided tasks only: one completed + one failed is
    50%, and the still-PENDING third task must not drag it to 33%.
    """
    done = db.create_task("done task", lane="headless")
    db.update_task_status(done, "COMPLETED", result="ok")
    bad = db.create_task("bad task", lane="headless")
    db.update_task_status(bad, "FAILED", failure_reason="nope")
    db.create_task("still pending", lane="headless")

    view.refresh()
    app.processEvents()

    assert view.kpi_total.value_label.text() == "3"
    assert view.kpi_success.value_label.text() == "50%"
    # Two tasks finished, so a duration is available (not the em-dash).
    assert view.kpi_duration.value_label.text() != "—"


def test_kpi_tiles_render_placeholder_when_nothing_decided(app, view):
    """No decided tasks means no rate to show — an em-dash, not a false 0%."""
    db.create_task("only pending", lane="headless")

    view.refresh()
    app.processEvents()

    assert view.kpi_total.value_label.text() == "1"
    assert view.kpi_success.value_label.text() == "—"
    assert view.kpi_duration.value_label.text() == "—"
