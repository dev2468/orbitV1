import sys
from PySide6.QtWidgets import QApplication
from orbit import db
from gui.history_view import TaskHistoryView

def test_history_view_full():
    app = QApplication.instance() or QApplication(sys.argv)
    
    # Create sample task in db
    task_id = db.create_task("Open GitHub in Chrome", lane="foreground")
    db.update_task_status(task_id, "COMPLETED", result="Success! Commit checked.")

    hv = TaskHistoryView(md_renderer=lambda x: f"<p>{x}</p>")
    hv.show()
    app.processEvents()

    # Verify initial load
    assert hv.table.rowCount() == 1
    assert hv._selected_task_id == task_id
    assert hv.inspector_stack.currentIndex() == 1

    # Test clicking row
    hv._select_row_by_index(0)
    app.processEvents()
    assert hv.inspector_stack.currentIndex() == 1

    # Test search filter matching
    hv.search_input.setText("GitHub")
    app.processEvents()
    assert hv.table.rowCount() == 1

    # Test search filter non-matching
    hv.search_input.setText("xyzNonExistent123")
    app.processEvents()
    assert hv.table.rowCount() == 0
    assert hv.inspector_stack.currentIndex() == 0

    # Reset search
    hv.search_input.clear()
    app.processEvents()
    assert hv.table.rowCount() == 1

    # Test status filter matching
    hv.status_combo.setCurrentText("Completed")
    app.processEvents()
    assert hv.table.rowCount() == 1

    # Test status filter non-matching
    hv.status_combo.setCurrentText("Failed")
    app.processEvents()
    assert hv.table.rowCount() == 0

    # Reset status
    hv.status_combo.setCurrentText("All Statuses")
    app.processEvents()
    assert hv.table.rowCount() == 1

    print("ALL HISTORY VIEW TESTS PASSED PERFECTLY!")

if __name__ == "__main__":
    test_history_view_full()
