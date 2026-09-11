# Orbit Baseline Audit — 2026-09-06

**Post-fix status**: 362 tests pass (29 new), 1 skipped (opt-in live UI), 0 failures.
Findings 1-8, 12-13 are fixed. 9, 10 are fixed or assessed as acceptable.

Original baseline: 149 offline tests passed at audit time.

## Findings

| # | Sev | Category | Fix | Status |
|---|-----|----------|-----|--------|
| 1 | S1 | Vision workflow instruction lies about confidence gate | Rewrote `_ORBIT_INSTRUCTION_SUFFIX` and `WINDOWS_CONTROL_INSTRUCTION` — UIA-first, raw coords described as requiring approval | **FIXED** |
| 2 | S1 | ui_memory cache described as primary click path | Cache described as hints requiring fresh validation | **FIXED** |
| 3 | S1 | Preamble claims desktop control in both lanes | Split into `_ORBIT_INSTRUCTION_PREAMBLE_HEADLESS` / `_FOREGROUND` | **FIXED** |
| 4 | S1 | read_file claims Office formats; binary warning contradicts | Removed Office formats from read_file, added binary-format note | **FIXED** |
| 5 | S2 | Hardcoded Chrome profile directory | Removed; now references policy-managed profiles | **FIXED** |
| 6 | S2 | "COMPLETE tasks" preamble for all inputs including greetings | Lane-specific preambles with WHEN TO ACT vs WHEN TO TALK section | **FIXED** |
| 7 | S2 | build_agent treats unknown lane as headless via else | Added explicit `ValueError` for unknown lanes | **FIXED** |
| 8 | S2 | Every exception reported as provider outage | Separated CancelledError, connection errors, and generic exceptions | **FIXED** |
| 9 | S2 | _IMAGE_B64_RE matches any tool result | Assessed: field name `image_small_b64` is unique to screenshots; risk is theoretical | Acceptable |
| 10 | S2 | _mark_cache_breakpoint mutates messages in place | Rewritten to create new message dicts via dict spread | **FIXED** |
| 11 | S2 | select_model reads from process-global os.environ | Added explicit `model_name` and `effort` params; build_agent forwards them | **FIXED** |
| 12 | S2 | Dev-MCP hardcodes path outside repo | Configurable via `DEVMCP_PYTHON` / `DEVMCP_SCRIPT` env vars | **FIXED** |
| 13 | S3 | "Continue." injection has no loop bound | Capped at `_MAX_CONTINUE_INJECTIONS` (3) | **FIXED** |
| 14 | S3 | DEFAULT_MODEL / KNOWN_MODELS inconsistency | Not addressed (user's in-flight experiment) | Open |
| 15 | S3 | No conversation continuity | Added conversations table, turn linkage, context injection, GUI + REPL support | **FIXED** |
| 16 | S3 | _ORBIT_TASK_ID read once at import | By design; documented | N/A |
| 17 | S3 | Various documented issues (browser_open timeout, etc.) | Not in scope for this session | Open |

### Key for Severity
- **S1**: Instruction tells the model to do something the runtime will refuse — wastes turns and confuses the agent
- **S2**: Incorrect/inconsistent behavior or documentation that degrades reliability
- **S3**: Improvement, documented gap, or forward-looking concern
