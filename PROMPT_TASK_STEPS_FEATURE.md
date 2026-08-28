# Prompt: Implement Step-by-Step Task Progress Visualization in Orbit GUI

## Context

Orbit is a Python desktop task agent (PySide6 GUI, Google ADK + LiteLLM backend). The agent runs tasks by calling MCP tools (browser automation, file operations, Windows mouse/keyboard control, screenshots). The GUI (`gui/main.py`) currently shows a flat live-text output stream. We want a visual step-by-step progress panel that breaks a running task into phases and shows status for each.

The agent subprocess is spawned via `QProcess` in `gui/main.py`. Its stdout is the only communication channel — there is no IPC socket. The agent's stdout already contains structured markers we can parse: tool names, screenshot paths, and markdown-formatted results.

## What to build

### 1. Agent-side: Emit structured step markers to stdout

In `orbit/agent.py`, modify the `_ORBIT_INSTRUCTION_SUFFIX` (the system prompt) to tell the model to emit step markers at natural phase boundaries. The model should print lines like:

```
[STEP:START] Opening the document
[STEP:DONE] Opening the document
[STEP:START] Writing the code
[STEP:PROGRESS] Writing the code — function encrypt_columnar complete
[STEP:DONE] Writing the code
[STEP:START] Pasting code and screenshot into document
[STEP:FAIL] Pasting code and screenshot into document — clipboard was empty
```

Add this to the instruction suffix:
```
STEP MARKERS: At the start of each logical phase of your work, print exactly:
[STEP:START] <short description>
When that phase completes, print:
[STEP:DONE] <same description>
If a phase fails, print:
[STEP:FAIL] <same description> — <reason>
For progress within a long phase, print:
[STEP:PROGRESS] <same description> — <detail>
Keep descriptions short (under 60 chars). A task typically has 3-8 steps.
Examples of good step boundaries: "Opening Word document", "Writing Python script",
"Running the code", "Taking screenshot of output", "Saving as PDF".
```

### 2. GUI-side: StepTracker widget

Create a `StepTracker` widget class in `gui/main.py` (or a new `gui/step_tracker.py` if cleaner). This sits between the input bar and the output panel.

#### Visual design:
- Horizontal or vertical step indicators (vertical is better for >4 steps)
- Each step is a row with: status icon + step description + elapsed time
- Status icons:
  - Pending: `○` (gray circle)
  - Running: `◉` (blue pulsing dot — use QTimer to toggle opacity)
  - Done: `✓` (green checkmark)
  - Failed: `✗` (red X)
- Connected by a thin vertical line between steps (like a timeline)
- Smooth fade-in animation when a new step appears (use QPropertyAnimation on opacity)
- The currently-running step should have a subtle blue background highlight
- Elapsed time shown as "12s", "1m 23s" etc, right-aligned, in secondary text color

#### Data model:
```python
from dataclasses import dataclass, field
from enum import Enum
import time

class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"

@dataclass
class Step:
    description: str
    status: StepStatus = StepStatus.PENDING
    started_at: float | None = None
    finished_at: float | None = None
    progress_detail: str = ""

    def elapsed(self) -> str:
        if not self.started_at:
            return ""
        end = self.finished_at or time.time()
        secs = int(end - self.started_at)
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m {secs % 60}s"
```

#### StepTracker class:
```python
class StepTracker(QWidget):
    def __init__(self):
        super().__init__()
        self.steps: list[Step] = []
        self._step_widgets: list[QWidget] = []
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(16, 8, 16, 8)
        self._layout.setSpacing(0)
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._pulse_active)
        self._pulse_timer.start(800)
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._elapsed_timer.start(1000)
        self.hide()  # hidden until first step

    def reset(self):
        """Clear all steps (new task)."""
        self.steps.clear()
        for w in self._step_widgets:
            w.deleteLater()
        self._step_widgets.clear()
        self.hide()

    def handle_marker(self, marker_type: str, description: str, detail: str = ""):
        """Process a parsed [STEP:XXX] marker."""
        if marker_type == "START":
            # If there's a running step, auto-complete it
            for s in self.steps:
                if s.status == StepStatus.RUNNING:
                    s.status = StepStatus.DONE
                    s.finished_at = time.time()
            step = Step(description=description, status=StepStatus.RUNNING,
                        started_at=time.time())
            self.steps.append(step)
            self._add_step_widget(step)
            self.show()

        elif marker_type == "DONE":
            for s in self.steps:
                if s.description == description and s.status == StepStatus.RUNNING:
                    s.status = StepStatus.DONE
                    s.finished_at = time.time()
                    break

        elif marker_type == "FAIL":
            for s in self.steps:
                if s.description == description and s.status == StepStatus.RUNNING:
                    s.status = StepStatus.FAILED
                    s.finished_at = time.time()
                    s.progress_detail = detail
                    break

        elif marker_type == "PROGRESS":
            for s in self.steps:
                if s.description == description and s.status == StepStatus.RUNNING:
                    s.progress_detail = detail
                    break

        self._refresh_all_widgets()

    def _add_step_widget(self, step: Step):
        """Create and animate in a new step row."""
        row = self._build_step_row(step, len(self.steps) - 1)
        # Fade-in animation
        opacity = QGraphicsOpacityEffect(row)
        row.setGraphicsEffect(opacity)
        anim = QPropertyAnimation(opacity, b"opacity")
        anim.setDuration(300)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        self._layout.addWidget(row)
        self._step_widgets.append(row)
        anim.start()

    def _build_step_row(self, step: Step, index: int) -> QWidget:
        """Build one step row: connector line + icon + text + elapsed."""
        # ... build with QHBoxLayout, QLabel for icon, QLabel for text,
        # QLabel for elapsed. Use the palette colors from the main file.
        # Add a 1px-wide vertical connector line on the left (except first step).
        pass

    def _refresh_all_widgets(self):
        """Update icons, colors, backgrounds for all step widgets."""
        pass

    def _pulse_active(self):
        """Toggle opacity of the running step's icon for a pulse effect."""
        pass

    def _update_elapsed(self):
        """Update elapsed time labels for running steps."""
        pass
```

### 3. Parser: Extract step markers from stdout

In `OrbitWindow._read_stdout`, add regex parsing for step markers:

```python
_STEP_RE = re.compile(r"^\[STEP:(START|DONE|FAIL|PROGRESS)\]\s*(.+?)(?:\s*—\s*(.+))?$")

def _read_stdout(self):
    # ... existing code ...
    for line in text.splitlines(keepends=True):
        m = self._STEP_RE.match(line.strip())
        if m:
            marker_type, description, detail = m.group(1), m.group(2).strip(), (m.group(3) or "").strip()
            self.step_tracker.handle_marker(marker_type, description, detail)
            continue  # don't echo raw marker to output
        # ... rest of existing line handling ...
```

### 4. Integration in the layout

In `OrbitWindow.__init__`, insert the step tracker between the progress bar and the output panel:

```python
self.step_tracker = StepTracker()
main_layout.addWidget(self.step_tracker)
main_layout.addSpacing(8)
```

In `_submit_task`, reset it:
```python
self.step_tracker.reset()
```

In `_task_finished`, mark any still-running step as done/failed:
```python
for s in self.step_tracker.steps:
    if s.status == StepStatus.RUNNING:
        s.status = StepStatus.DONE if exit_code == 0 else StepStatus.FAILED
        s.finished_at = time.time()
self.step_tracker._refresh_all_widgets()
```

### 5. Visual design details

Colors (from the existing palette in `gui/main.py`):
- `_BLUE = "#2563EB"` — running step icon + background highlight
- `_GREEN = "#16A34A"` — done step icon
- `_RED = "#DC2626"` — failed step icon
- `_GRAY = "#94A3B8"` — pending step icon
- `_TEXT = "#0F172A"` — step description text
- `_TEXT_SEC = "#64748B"` — elapsed time + progress detail
- `_BORDER = "#E2E8F0"` — connector line
- `_BLUE_LIGHT = "#EFF6FF"` — running step background

Step row height: ~36px. Connector line: 1px wide, `_BORDER` color, centered under the icon column. The connector connects adjacent step icons vertically.

Running step row: add a subtle `background: _BLUE_LIGHT` with `border-radius: 8px`.

### 6. Fallback: Auto-infer steps from tool calls

Not all models will reliably emit `[STEP:XXX]` markers. Add a fallback that infers steps from tool calls visible in stdout. The agent's stdout contains lines like:

```
tool_call: browser_open(url="https://...")
tool_call: windows_click(target={...})
tool_call: perception_capture_screenshot()
tool_call: run_command(command="python script.py")
```

Map tool patterns to step descriptions:
```python
_TOOL_STEP_MAP = {
    "browser_open": "Opening browser",
    "browser_navigate": "Navigating to page",
    "windows_click": "Interacting with window",
    "perception_capture_screenshot": "Observing the screen",
    "run_command": "Running a command",
    "file_write": "Writing a file",
    "perception_find_element": "Finding UI element",
}
```

When a tool call is detected in stdout and no explicit `[STEP:START]` was emitted for a similar description, auto-create a step. This ensures the progress panel is useful even without model cooperation.

### 7. Testing

- Add a test that parses sample stdout containing `[STEP:START]` / `[STEP:DONE]` markers and verifies the StepTracker state transitions correctly.
- Test that auto-completion works: START step A, then START step B → step A should auto-complete.
- Test elapsed time formatting.
- Test reset clears everything.

### 8. Files to modify

1. `orbit/agent.py` — Add step marker instructions to `_ORBIT_INSTRUCTION_SUFFIX`
2. `gui/main.py` — Add `StepTracker` class, `_STEP_RE` regex, integrate into layout and stdout parsing
3. Optionally `gui/step_tracker.py` — If the widget is complex enough to warrant its own file

### 9. Key constraints

- The GUI spawns the agent as a **subprocess** via QProcess. The only communication channel is stdout/stderr. No shared memory, no sockets.
- The step markers must be **plain text** printable by the model (it generates them as part of its response). No binary protocol.
- The model is Gemini Flash via OpenRouter/LiteLLM. It handles simple formatting instructions well but may occasionally forget markers. The fallback (point 6) covers this.
- PySide6 is the UI framework. Use QPropertyAnimation for animations, QTimer for periodic updates.
- The existing color palette in `gui/main.py` must be reused — do not introduce new colors.
- The step tracker should be **compact** — it sits between the input and the main output area, so it should not take more than ~200px vertical space even with 8 steps. Use scrolling if needed.

### 10. Example flow

User types: "Open Word, fill in experiment details, write cipher code, run it, paste output, save as PDF"

The step tracker would show:
```
◉ Opening Word document                    3s     ← blue highlight, pulsing
○ Writing cipher code                              ← gray, pending
○ Running the code                                 ← gray, pending
○ Pasting into document                            ← gray, pending
○ Saving as PDF                                    ← gray, pending
```

Then progresses to:
```
✓ Opening Word document                    12s    ← green check
✓ Writing cipher code                      28s    ← green check
◉ Running the code                         5s     ← blue, pulsing
    Running columnar_cipher.py...                  ← progress detail
○ Pasting into document                            ← gray
○ Saving as PDF                                    ← gray
```

Final state:
```
✓ Opening Word document                    12s
✓ Writing cipher code                      28s
✓ Running the code                         8s
✓ Pasting into document                    15s
✓ Saving as PDF                            6s
```
