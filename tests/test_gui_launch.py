import sys
from PySide6.QtWidgets import QApplication
from gui.main import OrbitWindow

def test_gui_launch():
    app = QApplication.instance() or QApplication(sys.argv)
    window = OrbitWindow()
    window.show()
    app.processEvents()
    assert window.isVisible()
    
    # Test switching to history
    window._show_history()
    app.processEvents()
    assert window.main_stack.currentIndex() == 1
    
    # Test switching to workbench
    window._show_workbench()
    app.processEvents()
    assert window.main_stack.currentIndex() == 0
    print("ALL GUI TESTS PASSED!")

if __name__ == "__main__":
    test_gui_launch()
