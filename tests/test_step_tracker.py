"""Tests for StepTracker widget, step markers parsing, and tool call fallbacks."""

from __future__ import annotations

import re
import sys
import time
import pytest
from PySide6.QtWidgets import QApplication

from gui.step_tracker import Step, StepStatus, StepTracker, _TOOL_STEP_MAP
from gui.main import OrbitWindow


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(["--platform", "offscreen"])
    return app


def test_step_elapsed_formatting():
    # Not started
    s = Step(description="Test step")
    assert s.elapsed() == ""

    # Under 60s
    now = time.time()
    s = Step(description="Test step", status=StepStatus.RUNNING, started_at=now - 15, finished_at=now)
    assert s.elapsed() == "15s"

    # Over 60s
    s = Step(description="Test step", status=StepStatus.DONE, started_at=now - 83, finished_at=now)
    assert s.elapsed() == "1m 23s"

    # Active running step (finished_at is None)
    s = Step(description="Test step", status=StepStatus.RUNNING, started_at=now - 5)
    assert "s" in s.elapsed()


def test_step_marker_regex():
    regex = OrbitWindow._STEP_RE

    # Simple START
    m = regex.match("[STEP:START] Opening Word document")
    assert m is not None
    assert m.group(1) == "START"
    assert m.group(2) == "Opening Word document"
    assert (m.group(3) or "") == ""

    # DONE
    m = regex.match("[STEP:DONE] Opening Word document")
    assert m is not None
    assert m.group(1) == "DONE"
    assert m.group(2) == "Opening Word document"

    # FAIL with em-dash
    m = regex.match("[STEP:FAIL] Pasting code — clipboard was empty")
    assert m is not None
    assert m.group(1) == "FAIL"
    assert m.group(2) == "Pasting code"
    assert m.group(3) == "clipboard was empty"

    # FAIL with regular hyphen
    m = regex.match("[STEP:FAIL] Pasting code - clipboard was empty")
    assert m is not None
    assert m.group(1) == "FAIL"
    assert m.group(2) == "Pasting code"
    assert m.group(3) == "clipboard was empty"

    # PROGRESS with en-dash
    m = regex.match("[STEP:PROGRESS] Writing code – function complete")
    assert m is not None
    assert m.group(1) == "PROGRESS"
    assert m.group(2) == "Writing code"
    assert m.group(3) == "function complete"


def test_step_tracker_start_and_auto_completion(qapp):
    tracker = StepTracker()
    assert len(tracker.steps) == 0

    # Start Step 1
    tracker.handle_marker("START", "Opening Word document")
    assert len(tracker.steps) == 1
    assert tracker.steps[0].description == "Opening Word document"
    assert tracker.steps[0].status == StepStatus.RUNNING
    assert tracker.steps[0].started_at is not None
    assert tracker.isVisible()

    # Start Step 2 -> Step 1 should auto-complete to DONE
    tracker.handle_marker("START", "Writing cipher code")
    assert len(tracker.steps) == 2
    assert tracker.steps[0].status == StepStatus.DONE
    assert tracker.steps[0].finished_at is not None
    assert tracker.steps[1].description == "Writing cipher code"
    assert tracker.steps[1].status == StepStatus.RUNNING


def test_step_tracker_done_and_fail_and_progress(qapp):
    tracker = StepTracker()

    # Start and progress
    tracker.handle_marker("START", "Compiling assets")
    assert tracker.steps[0].status == StepStatus.RUNNING
    assert tracker.steps[0].progress_detail == ""

    tracker.handle_marker("PROGRESS", "Compiling assets", "Optimizing PNGs")
    assert tracker.steps[0].progress_detail == "Optimizing PNGs"
    assert tracker.steps[0].status == StepStatus.RUNNING

    # Complete
    tracker.handle_marker("DONE", "Compiling assets")
    assert tracker.steps[0].status == StepStatus.DONE
    assert tracker.steps[0].finished_at is not None

    # Failure
    tracker.handle_marker("START", "Uploading release")
    tracker.handle_marker("FAIL", "Uploading release", "Network timeout")
    assert tracker.steps[1].status == StepStatus.FAILED
    assert tracker.steps[1].progress_detail == "Network timeout"


def test_step_tracker_reset(qapp):
    tracker = StepTracker()
    tracker.handle_marker("START", "Task 1")
    tracker.handle_marker("DONE", "Task 1")
    assert len(tracker.steps) == 1
    assert len(tracker._step_widgets) == 1

    tracker.reset()
    assert len(tracker.steps) == 0
    assert len(tracker._step_widgets) == 0
    assert tracker.isHidden()


def test_tool_call_fallback(qapp):
    tracker = StepTracker()

    # Tool call triggers step creation
    tracker.handle_tool_call("browser_open")
    assert len(tracker.steps) == 1
    assert tracker.steps[0].description == "Opening browser"
    assert tracker.steps[0].status == StepStatus.RUNNING
    assert tracker.steps[0].is_inferred is True

    # Same tool call does not duplicate
    tracker.handle_tool_call("browser_open")
    assert len(tracker.steps) == 1

    # New tool call completes previous inferred step and starts new one
    tracker.handle_tool_call("browser_navigate")
    assert len(tracker.steps) == 2
    assert tracker.steps[0].status == StepStatus.DONE
    assert tracker.steps[1].description == "Navigating to page"
    assert tracker.steps[1].status == StepStatus.RUNNING


def test_explicit_marker_overrides_inferred_step(qapp):
    tracker = StepTracker()

    # Explicit step running
    tracker.handle_marker("START", "Custom business workflow")
    assert tracker.steps[0].description == "Custom business workflow"
    assert tracker.steps[0].is_inferred is False

    # Tool calls during explicit step do not overwrite it
    tracker.handle_tool_call("run_command")
    assert len(tracker.steps) == 1
    assert tracker.steps[0].description == "Custom business workflow"
