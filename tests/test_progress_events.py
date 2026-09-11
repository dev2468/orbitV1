"""Tests for the live progress-event stream (Day 1 latency work).

The thing under test is a *contract between two processes*: the worker
(`orbit.run_task --serve`) writes `[ORBIT]{json}` lines on stdout, and the
GUI (`gui.main.OrbitWindow`) reads them to drive the step rail. Both halves
are exercised here, plus the step-rail state machine that consumes them.

Nothing here makes a model call — the emitters and the parser are pure, and
the GUI half is driven by feeding it lines directly rather than by running a
task. That is deliberate: this contract has to be checkable offline, because
its failure mode (a silent task and an empty step rail) is exactly what it
was written to prevent.
"""

from __future__ import annotations

import json

import pytest
from PySide6.QtWidgets import QApplication

from gui.main import OrbitWindow, _EVENT_PREFIX
from gui.step_tracker import StepStatus, StepTracker
from orbit.run_task import EVENT_PREFIX, _console_emitter, _jsonl_emitter


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(["--platform", "offscreen"])
    return app


# --- the cross-process contract ---------------------------------------------


def test_gui_and_worker_agree_on_the_event_prefix():
    """gui/main.py copies the prefix rather than importing orbit.run_task
    (which would drag litellm + google-adk into the GUI process). That copy
    is the whole risk of the duplication, so it gets a test."""
    assert _EVENT_PREFIX == EVENT_PREFIX


def test_jsonl_emitter_writes_one_parseable_line(capsys):
    _jsonl_emitter({"kind": "tool_call", "task_id": "T-1", "tool": "list_files"})
    out = capsys.readouterr().out
    assert out.endswith("\n")
    assert out.count("\n") == 1
    assert json.loads(out[len(EVENT_PREFIX):]) == {
        "kind": "tool_call", "task_id": "T-1", "tool": "list_files",
    }


def test_jsonl_emitter_keeps_multiline_payloads_on_one_line(capsys):
    """A tool argument containing newlines must not split the event across
    lines — the reader is line-oriented, so a split payload would surface as
    a corrupt event plus a stray line of JSON in the output pane."""
    _jsonl_emitter({"kind": "tool_call", "tool": "write_file",
                    "args": {"content": "line one\nline two\r\nline three"}})
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    parsed = json.loads(out[len(EVENT_PREFIX):])
    assert parsed["args"]["content"] == "line one\nline two\r\nline three"


def test_jsonl_emitter_survives_unserializable_arguments(capsys):
    """default=str keeps a task alive when a tool argument is not JSON —
    telemetry must never be able to fail the run it is describing."""
    _jsonl_emitter({"kind": "tool_call", "args": {"obj": object()}})
    out = capsys.readouterr().out
    assert json.loads(out[len(EVENT_PREFIX):])["kind"] == "tool_call"


def test_console_emitter_prints_only_tool_calls(capsys):
    _console_emitter({"kind": "tool_call", "tool": "browser_navigate"})
    _console_emitter({"kind": "text_delta", "text": "hello"})
    _console_emitter({"kind": "tool_result", "tool": "browser_navigate"})
    _console_emitter({"kind": "result", "status": "COMPLETED", "text": "done"})
    out = capsys.readouterr().out
    assert out.strip() == "· browser_navigate"


# --- the step rail's half of the contract ------------------------------------


def test_tool_result_completes_the_running_step(qapp):
    tracker = StepTracker()
    tracker.handle_tool_call("browser_navigate")
    assert tracker.steps[-1].status == StepStatus.RUNNING

    tracker.complete_current()
    assert tracker.steps[-1].status == StepStatus.DONE
    assert tracker.steps[-1].finished_at is not None


def test_repeated_tool_reopens_its_row_instead_of_stacking(qapp):
    """Ten screenshots in a row are one step that ran ten times, not ten
    steps — the rail is a progress meter, not a transcript."""
    tracker = StepTracker()
    for _ in range(3):
        tracker.handle_tool_call("perception_capture_screenshot")
        tracker.complete_current()

    assert len(tracker.steps) == 1
    assert tracker.steps[0].description == "Looking at the screen"
    assert tracker.steps[0].progress_detail == "×3"


def test_a_different_phase_starts_a_new_step(qapp):
    tracker = StepTracker()
    tracker.handle_tool_call("browser_navigate")
    tracker.complete_current()
    tracker.handle_tool_call("browser_snapshot")

    assert [s.description for s in tracker.steps] == [
        "Going to a web page", "Reading the page",
    ]
    assert tracker.steps[0].status == StepStatus.DONE
    assert tracker.steps[1].status == StepStatus.RUNNING


def test_tools_in_the_same_phase_share_one_step(qapp):
    """browser_navigate and browser_go_back are both "going to a web page".
    Naming the mechanism instead of the outcome is what produced 48 rows."""
    tracker = StepTracker()
    for tool in ("browser_navigate", "browser_go_back", "browser_tab_new"):
        tracker.handle_tool_call(tool)
        tracker.complete_current()
    assert len(tracker.steps) == 1
    assert tracker.steps[0].description == "Going to a web page"


def test_a_realistic_browse_stays_readable(qapp):
    """The regression this whole change exists for: an ordinary browse used
    to render ~48 steps. Simple tasks must stay at or under five."""
    tracker = StepTracker()
    tracker.begin_task()
    calls = (["browser_open", "browser_navigate", "browser_snapshot"]
             + ["browser_press_key", "browser_snapshot"] * 8
             + ["browser_click", "browser_snapshot"] * 6
             + ["browser_navigate", "browser_snapshot"] * 5)
    for tool in calls:
        tracker.handle_tool_call(tool)
        tracker.complete_current()

    assert len(calls) > 40, "fixture should represent the heavy case"
    assert len(tracker.steps) <= 5, (
        f"{len(calls)} tool calls rendered {len(tracker.steps)} steps: "
        + ", ".join(s.description for s in tracker.steps)
    )


def test_an_unknown_tool_still_collapses_on_itself(qapp):
    """A tool nobody has given a phase yet must not flood the rail."""
    tracker = StepTracker()
    for _ in range(4):
        tracker.handle_tool_call("some_brand_new_tool")
        tracker.complete_current()
    assert len(tracker.steps) == 1


# --- the rail is alive from submission, not from the first tool call --------


def test_begin_task_opens_a_running_step(qapp):
    """A cold worker plus MCP connect plus the first model turn measured 18.5s
    before the first tool call. The rail used to show nothing for all of it."""
    tracker = StepTracker()
    tracker.begin_task()

    assert len(tracker.steps) == 1
    assert tracker.steps[0].status == StepStatus.RUNNING
    assert tracker.steps[0].started_at is not None


def test_the_thinking_step_closes_on_the_first_real_tool_call(qapp):
    tracker = StepTracker()
    tracker.begin_task()
    thinking = tracker.steps[0]

    tracker.handle_tool_call("browser_open")

    assert thinking.status == StepStatus.DONE
    assert tracker.steps[-1].status == StepStatus.RUNNING
    assert tracker.steps[-1].description == "Opening a browser"


def test_begin_task_clears_a_previous_task(qapp):
    tracker = StepTracker()
    tracker.begin_task()
    tracker.handle_tool_call("run_command")
    tracker.begin_task()
    assert len(tracker.steps) == 1


def test_complete_current_never_closes_an_explicit_marker_step(qapp):
    """A [STEP:START] marker spans a phase and several tool calls, so one
    tool returning does not end it."""
    tracker = StepTracker()
    tracker.handle_marker("START", "Researching prices")
    tracker.complete_current()
    assert tracker.steps[0].status == StepStatus.RUNNING


def test_complete_current_is_safe_with_no_steps(qapp):
    StepTracker().complete_current()  # must not raise


# --- the GUI's parser --------------------------------------------------------


def _window(qapp) -> OrbitWindow:
    w = OrbitWindow()
    w._task_running = True
    return w


def test_event_lines_drive_the_step_rail(qapp):
    w = _window(qapp)
    w._handle_orbit_event(json.dumps({"kind": "tool_call", "tool": "run_command"}))
    assert w.step_tracker.steps[-1].description == "Running a command"
    assert w.step_tracker.steps[-1].status == StepStatus.RUNNING

    w._handle_orbit_event(json.dumps({"kind": "tool_result", "tool": "run_command"}))
    assert w.step_tracker.steps[-1].status == StepStatus.DONE


def test_result_event_unlocks_input_before_teardown(qapp):
    """The worker emits `result` before closing six MCP subprocesses (~2s).
    Unlocking here is the whole point — waiting for [TASK:DONE] would leave
    the user staring at a finished answer with a dead input box."""
    w = _window(qapp)
    w.goal_input.setEnabled(False)

    w._handle_orbit_event(json.dumps({"kind": "result", "status": "COMPLETED", "text": "ok"}))

    assert w.goal_input.isEnabled()
    assert "Completed" in w.run_indicator.text()


def test_result_event_on_failure_still_unlocks(qapp):
    w = _window(qapp)
    w.goal_input.setEnabled(False)
    w._handle_orbit_event(json.dumps({"kind": "result", "status": "FAILED", "text": "nope"}))
    assert w.goal_input.isEnabled()
    assert "Failed" in w.run_indicator.text()


def test_malformed_event_is_ignored(qapp):
    w = _window(qapp)
    w._handle_orbit_event("{not json")
    w._handle_orbit_event(json.dumps({"kind": "who_knows"}))
    w._handle_orbit_event(json.dumps({"no_kind": True}))
    assert w.step_tracker.steps == []


def test_event_lines_stay_out_of_the_raw_buffer(qapp):
    """_render_final_output re-parses _raw_buffer at the end of a task. An
    event line landing there would print raw JSON into the output pane."""
    w = _window(qapp)
    w._raw_buffer = ""

    for line in (
        f'{_EVENT_PREFIX}{{"kind": "tool_call", "tool": "list_files"}}\n',
        "Here is the real answer.\n",
    ):
        stripped = line.strip()
        if stripped.startswith(_EVENT_PREFIX):
            w._handle_orbit_event(stripped[len(_EVENT_PREFIX):])
            continue
        w._raw_buffer += line

    assert _EVENT_PREFIX not in w._raw_buffer
    assert "Here is the real answer." in w._raw_buffer


def test_text_deltas_are_not_buffered_for_the_final_render(qapp):
    """Deltas are a live preview. The canonical answer reaches _raw_buffer as
    prose from the worker, so counting deltas too would duplicate it."""
    w = _window(qapp)
    w._raw_buffer = ""
    w._handle_orbit_event(json.dumps({"kind": "text_delta", "text": "Twinkle, "}))
    w._handle_orbit_event(json.dumps({"kind": "text_delta", "text": "twinkle"}))
    assert w._raw_buffer == ""
    assert "Twinkle, twinkle" in w.output_text.toPlainText()
