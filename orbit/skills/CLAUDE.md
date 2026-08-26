# orbit/skills/ — MCPToolset wiring

Six modules, each a single `build_toolset(task_id="")` returning an `MCPToolset`. A skill is the
orchestrator's vocabulary: it composes atomic tools, and its `tool_filter` is an *enforced*
allowlist, not documentation.

**`windows_control.py` is the one skill `orbit/agent.py` does NOT always add.** Its own
`build_toolset()` has no way to refuse being wired in — the gate lives in `build_agent`'s `lane`
parameter instead. See the root `CLAUDE.md` and `orbit/CLAUDE.md` for why: the foreground lock that
actually serializes input-simulating tasks against each other is only held for `lane="foreground"`
tasks, and a headless-lane agent must never even have a function declaration for `windows_click` to
call.

## Point at our own server, never at raw Playwright

`research_product.build_toolset()` spawns
`sys.executable -m orbit.mcp_servers.browser_policy_server`. It once pointed straight at
`npx @playwright/mcp@latest`, which silently bypassed the entire policy layer — URL scheme
allowlist, sensitive-category blocklist, non-owner profile exclusion and `<untrusted_web_content>`
wrapping were all dead code whenever the agent actually ran.

It was well camouflaged: raw Playwright exposes tools with the **same names** the proxy uses
(`browser_navigate`, `browser_snapshot`), which are also the names in `risk_tiers.yaml` — so
`SafetyPlugin` found tier `low`, approved, and wrote a perfectly normal event row. If you ever need
to check which one is really in the path, look for the untrusted marker in snapshot output, not at
the logs.

**`sys.executable`, not a bare `"python"`.** The venv is not on PATH here, so `"python"` resolves to
the wrong interpreter or nothing at all.

## tool_filter: every name is there on purpose

The filter was widened from 8 to 14 tools when the default model switched from Nemotron 3.5
Lightning (3B active params, degraded on large tool surfaces) to Claude Sonnet 4, which handles
50+ tools without reliability loss. The original 3-tool filter (open/navigate/snapshot) was expanded
through two rounds: first to 8 (click, type, select_option, press_key, go_back), then to 14
(hover, go_forward, tab_new/list/select, handle_dialog).

Still held back:

- **`browser_extract`**: takes a raw JS expression; models reach for it when snapshot suffices.
- **`browser_close`**: teardown is the reaper's job (model skipped it 2/4 measured runs).
- **`browser_take_screenshot`**: returns image data; use `browser_snapshot` for text or
  `perception_capture_screenshot` for real screenshots.
- **`browser_drag`**: rarely needed, complex 4-ref params.
- **`browser_tab_close`**: model can just navigate away; closing tabs adds little value.
- **`memory_get_task`**: mainly useful once the model already has a task_id in hand.

Exposed today: `browser_open`, `browser_navigate`, `browser_snapshot`, `browser_click`,
`browser_type`, `browser_select_option`, `browser_press_key`, `browser_go_back`,
`memory_search_tasks`, `memory_get_context`,
`memory_get_policy`, `memory_write`, `fs_list_dir`, `fs_read_file`, `fs_write_file`, `fs_move`,
`fs_copy`, `fs_search`, `fs_create_dir`, `fs_get_metadata`, `email_draft`, `email_search`,
`email_read`, `email_list_threads`, `calendar_list_events`, `calendar_create_event`,
`perception_get_state`, `perception_get_uia_tree`, `perception_find_element`,
`perception_capture_screenshot`, `perception_wait_for_visual_change`, `perception_vision_locate`, and — only when
`lane="foreground"` — `windows_get_foreground_window`, `windows_wait`, `windows_scroll`,
`windows_click`, `windows_drag`, `windows_type`, `windows_key`, `windows_open_app`,
`windows_clipboard_copy_image`.

**`filesystem.py` holds `fs_delete` back**, **`windows_control.py` holds `windows_focus_window`
back**, and **`communication.py` holds `email_send` back**, each from its own `tool_filter`, even
though all three are implemented and registered (tier `high` in `risk_tiers.yaml`). Every call to any
of them would hit `SafetyPlugin`'s unconditional high-tier block and return `confirmation_required` —
exposing them anyway would cost tool-selection surface area for calls that can never succeed until a
confirm channel exists, the same reasoning that held `browser_close` back here. `email_send` goes one
step further and refuses at the tool-body level too (see `orbit/mcp_servers/CLAUDE.md`'s
communication-server section) — held back from `tool_filter` for the same surface-area reason as the
other two, not because that second layer alone would be insufficient.

**`communication.py` and `screen_perception.py` need no lane gating**, unlike `windows_control.py` —
neither simulates OS input, so both are wired into `orbit/agent.py` unconditionally (`lane="headless"`),
the same as `memory.py`/`filesystem.py`. `screen_perception.py` exposes all 5 of its implemented tools —
unlike every other skill here, there's no held-back tool: nothing in this server is high-tier or
otherwise unreachable, so there's nothing to hold back.

**Adding a name to a `tool_filter` without adding it to `orbit/config/risk_tiers.yaml` is a hard
block at runtime and a test failure at build time** —
`tests/test_policy.py::test_every_tool_the_agent_exposes_at_build_time_is_registered` reads the real
`tool_filter` off the built toolsets and fails on any name missing from the registry.

## How task_id reaches the server subprocess

Only via the explicit `env={"ORBIT_TASK_ID": task_id}` on each `StdioServerParameters`.

MCP's `stdio_client` does **not** inherit the parent's `os.environ` — `get_default_environment()`
applies a curated safelist (PATH, APPDATA, …) that excludes arbitrary custom vars. Setting
`os.environ["ORBIT_TASK_ID"]` in the parent alone silently does nothing. Verified by reading the
SDK, not assumed.

The MCP SDK merges `env` on top of that safelist rather than replacing it, so PATH — and therefore
`npx` — still resolves for the browser server's own Playwright launch. Do not pass a full
environment dict here expecting inheritance semantics.

Passing task_id as a tool *argument* instead would make session binding and reaping depend on the
model remembering to thread it through every call, which is exactly the model-dependence this
removes. `task_id=""` is legitimate for ad-hoc use; the servers fall back to an `adhoc-*` row.

## Connection timeouts differ, on purpose

`research_product` uses `timeout=60` because that server spawns its own Playwright subprocess per
`browser_open` — a process launch behind a process launch. `memory` uses `timeout=30`; it only
touches SQLite. These are the *connection* timeouts on `StdioConnectionParams`; the per-tool
timeout inside `BaseTool.execute` is separate and shorter (see `orbit/tools/CLAUDE.md`).

## Shape note

`SKILL_META` in `research_product.py` is descriptive only — nothing reads it at runtime. Lane and
risk tier are enforced by `TaskManager` and `risk_tiers.yaml`, not from this dict.
