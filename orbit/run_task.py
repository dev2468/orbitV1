"""Integration entry point: ties db + TaskManager + SafetyPlugin + agent
together into one real run. This is the thing that proves the pieces built
in this session actually work as a system, not just in isolation.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Callable

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types

from orbit import db
from orbit.agent import build_agent, validate_model_key
from orbit.models import resolve_model_name
from orbit.degradation import graceful_degradation_message
from orbit.safety_plugin import SafetyPlugin
from orbit.task_manager import TaskManager

# ---------------------------------------------------------------------------
# Progress events
# ---------------------------------------------------------------------------
#
# A task used to be silent from submission until completion: run_debug()
# buffers every ADK event and returns the list at the end, so the GUI's step
# rail — which scraped [STEP:*] markers out of stdout — could not fill in
# until there was nothing left to watch. Measured on a two-phase task, the
# first tool call was knowable at +8.5s and the GUI learned about it at
# +12.9s, along with everything else, at once.
#
# run_task() now drives runner.run_async() and hands each interesting thing
# to an `on_event` callback the moment ADK yields it. Callers choose what a
# progress event means:
#
#   --serve (the GUI)      -> _jsonl_emitter: one [ORBIT]{json} line per event
#   REPL / one-shot CLI    -> _console_emitter: a human-readable progress line
#   tests / library use    -> None: emit nothing, same as before
#
# Event kinds, all carrying "task_id":
#   tool_call    {tool, args}      a tool is about to run
#   tool_result  {tool}            it returned
#   text_delta   {text}            an incremental slice of the model's answer
#   result       {status, text}    the final answer, emitted BEFORE teardown
#
# text_delta is a DELTA, not a running total — verified against ADK's SSE
# path, where partials arrived as 2 + 64 + 63 + 41 characters and the final
# non-partial event then repeated all 170. So a consumer appends deltas and
# must not also append the final text.

EVENT_PREFIX = "[ORBIT]"


# ---------------------------------------------------------------------------
# Conversation session reuse
# ---------------------------------------------------------------------------
#
# Turn-to-turn continuity used to be a TEXT SUMMARY: prior goals and results
# were formatted into the next prompt (`_build_conversation_context`). That
# works for "do that again in Chrome" and is genuinely useful, but the model
# never sees what actually happened — only a paraphrase of the outcome. Ask it
# to retry something that failed and it cannot see the error, the arguments it
# used, or the page it was looking at.
#
# Reusing the runner keeps the real ADK session, so turn two continues turn
# one's history: the same tool calls, the same results, the same browser
# session still open.
#
# **Only reused when a conversation_id is given, and only within one process.**
# Two properties make that safe, and losing either one breaks it:
#
#   1. Tasks in a `--serve` worker are strictly SEQUENTIAL — `_serve` awaits
#      each goal before reading the next line. The headless lane's
#      Semaphore(5) permits concurrency in principle, so a caller that runs
#      conversation turns concurrently would share one browser session between
#      them, which is the "browser is already in use" collision the eval
#      harness already found once. Do not make this cache concurrent without
#      solving that first.
#   2. Every tool call is stamped with its true task_id by
#      `SafetyPlugin.before_tool_callback`, so a long-lived server still
#      attributes its events to the right task. Without that, every turn's
#      events would be filed under the first turn.
#
# A task with no conversation_id is completely unaffected: fresh runner, fresh
# session, closed at the end, exactly as before.
_RUNNER_CACHE: dict[str, tuple[Any, str]] = {}


async def close_conversation(conversation_id: str) -> None:
    """Tear down a conversation's cached runner and its MCP servers.

    Called when a conversation ends or is replaced. Also the reason the cache
    is keyed by conversation rather than left to grow: each entry holds six
    live subprocesses.
    """
    entry = _RUNNER_CACHE.pop(conversation_id, None)
    if entry is None:
        return
    runner, _ = entry
    try:
        await runner.close()
    except Exception:  # noqa: BLE001
        pass


async def close_all_conversations() -> None:
    for conversation_id in list(_RUNNER_CACHE):
        await close_conversation(conversation_id)

EventSink = Callable[[dict[str, Any]], None]


def _jsonl_emitter(event: dict[str, Any]) -> None:
    """One prefixed JSON line per event — what --serve writes for the GUI.

    json.dumps escapes newlines, so an event is always exactly one line no
    matter what a tool argument contains. Never raises: a progress event is
    telemetry, and a broken pipe or an unserializable argument must not take
    the task down with it.
    """
    try:
        sys.stdout.write(f"{EVENT_PREFIX}{json.dumps(event, default=str)}\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def _console_emitter(event: dict[str, Any]) -> None:
    """Human-readable progress for the REPL and the one-shot CLI.

    Only tool calls are printed. text_delta would interleave with the final
    answer the CLI prints anyway, and tool_result adds a second line per call
    for no information a human wants mid-run.
    """
    if event.get("kind") != "tool_call":
        return
    try:
        print(f"  · {event.get('tool', '?')}", flush=True)
    except Exception:  # noqa: BLE001
        pass


# How many prior turns the text bridge replays. It used to replay ALL of them,
# which was fine while a bridge only ever covered one or two turns — but
# resuming an old chat from the GUI can hand it thirty, at up to ~500
# characters of result each, and the whole lot lands in the first message.
#
# The most recent turns are the ones a follow-up refers to; turn three of a
# thirty-turn chat almost never is. Older turns are still on screen for the
# user, and still in the History tab — they are just not re-sent to the model.
_MAX_CONTEXT_TURNS = 8


def _build_conversation_context(conversation_id: str) -> str:
    """Summarize recent turns in a conversation as context for the next turn.

    Only used when there is no live session to continue — see the runner cache
    above. A resumed chat therefore sees a paraphrase of what happened, not
    the original tool calls and their results.
    """
    turns = db.conversation_turns(conversation_id)
    if not turns:
        return ""
    dropped = max(0, len(turns) - _MAX_CONTEXT_TURNS)
    turns = turns[-_MAX_CONTEXT_TURNS:]
    parts = []
    if dropped:
        # Stated rather than silently omitted, for the same reason the history
        # compactor names what it elided: a model that can see something is
        # missing asks, whereas one shown an unexplained gap confabulates.
        parts.append(
            f"({dropped} earlier turn(s) omitted — ask the user if you need them.)"
        )
    for t in turns:
        status = t.get("status", "UNKNOWN")
        goal = t.get("goal", "")
        result = t.get("result", "")
        # Keep context compact — truncate long results
        if result and len(result) > 500:
            result = result[:500] + "..."
        if not (goal or "").strip():
            continue  # an interrupted turn with nothing to say
        parts.append(f"Turn {t.get('turn_index', '?')}: {goal}\n→ [{status}] {result}")
    return (
        "CONVERSATION HISTORY (prior turns in this conversation):\n"
        + "\n\n".join(parts)
        + "\n\nContinue the conversation. The user's new message follows.\n\n"
    )


async def run_task(
    title: str,
    goal: str,
    *,
    lane: str = "headless",
    risk_tier: str = "low",
    conversation_id: str | None = None,
    on_event: EventSink | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    # Validated against the model actually about to be used, not the ambient
    # default — picking a model in the GUI whose provider key is missing
    # should say so, rather than checking a different model's key and then
    # failing deep inside a provider call.
    validate_model_key(model)
    db.init_db()
    tm = TaskManager()

    if conversation_id:
        conv = db.get_conversation(conversation_id)
        if not conv:
            conversation_id = db.create_conversation(
                title=title, lane=lane, conversation_id=conversation_id
            )
        # The text summary is a BRIDGE, used only when there is no live
        # session to continue — the first turn after a worker restart, say,
        # where the DB remembers the conversation but the ADK session is gone.
        #
        # When a cached runner exists its session already holds the real
        # turns, tool calls and results, so injecting a paraphrase of the same
        # history on top would be both redundant and misleading: the model
        # would see each turn twice, once in full and once summarised.
        if conversation_id in _RUNNER_CACHE:
            effective_goal = goal
        else:
            context = _build_conversation_context(conversation_id)
            effective_goal = context + goal if context else goal
    else:
        effective_goal = goal

    task_id = db.create_task(
        title, goal=goal, lane=lane, risk_tier=risk_tier,
        conversation_id=conversation_id,
        # The model this task actually runs on, resolved exactly as
        # select_model() resolves it. Nothing passed this before 2026-09-11,
        # so every earlier row has NULL here and the task log cannot be split
        # by model.
        model=resolve_model_name(model),
    )
    if conversation_id:
        db.add_turn_to_conversation(conversation_id, task_id)

    # Belt-and-suspenders, not the load-bearing mechanism: verified by
    # reading mcp.client.stdio.get_default_environment() that stdio_client
    # subprocesses do NOT inherit the parent's full os.environ — it applies
    # a curated safelist (PATH, APPDATA, etc.) that excludes arbitrary
    # custom vars. So setting this alone would silently fail to reach the
    # memory/browser-policy server subprocesses. What actually works is the
    # explicit env={"ORBIT_TASK_ID": task_id} each skill's build_toolset()
    # passes into its own StdioServerParameters (orbit/skills/memory.py,
    # orbit/skills/research_product.py) — set here too for consistency and
    # for any future code path that does a plain subprocess inherit.
    os.environ["ORBIT_TASK_ID"] = task_id

    plugin = SafetyPlugin(task_manager=tm)
    # lane is threaded into build_agent too, not just TaskManager.submit
    # below — it's what decides whether the agent even has the
    # windows-control toolset. See build_agent's docstring (orbit/agent.py)
    # for why that gate has to live there rather than being trusted to the
    # model.
    # model=None keeps the existing precedence: ORBIT_MODEL, then
    # DEFAULT_MODEL. A value here (the GUI's selector) overrides both.
    agent = build_agent(task_id=task_id, lane=lane, model_name=model)

    def emit(kind: str, **fields: Any) -> None:
        """Fire one progress event. Never raises, never blocks the task."""
        if on_event is None:
            return
        try:
            on_event({"kind": kind, "task_id": task_id, **fields})
        except Exception:  # noqa: BLE001
            pass

    async def work(token) -> str:
        # A conversation reuses its runner — and therefore its ADK session,
        # its MCP servers, and any browser session still open — so turn two
        # continues turn one rather than starting over. See the cache's
        # comment block above for the two properties that make it safe.
        reuse_key = conversation_id if conversation_id else None
        cached = _RUNNER_CACHE.get(reuse_key) if reuse_key else None

        if cached is not None:
            runner, session_id = cached
            fresh = False
        else:
            runner = InMemoryRunner(agent=agent, plugins=[plugin])
            session_id = reuse_key or task_id
            fresh = True

        try:
            if fresh:
                await runner.session_service.create_session(
                    app_name=runner.app_name,
                    user_id="orbit_user",
                    session_id=session_id,
                    # Seeded with the FIRST task of the conversation. Per-call
                    # attribution comes from SafetyPlugin's task_id injection,
                    # not from this, which is why a stale value here is
                    # harmless rather than a bug.
                    state={"orbit_task_id": task_id},
                )
                if reuse_key:
                    _RUNNER_CACHE[reuse_key] = (runner, session_id)
            # run_async, not run_debug. run_debug is ADK's own debug helper —
            # its docstring says "For production use, please use the standard
            # run_async() method" — and it collects every event into a list
            # that it returns only once the whole task is finished. Under it,
            # a task is silent for its entire duration and then arrives all at
            # once, which is why the step rail never filled in until there was
            # nothing left to watch.
            #
            # StreamingMode.SSE additionally splits the model's answer into
            # incremental partial events. It does NOT make the first token
            # arrive sooner (measured: 2.23s SSE vs 2.24s NONE to first text,
            # MCP connect excluded) — the win is that a long answer appears as
            # it is written instead of in one block at the end.
            final_texts: list[str] = []
            message = genai_types.Content(
                role="user", parts=[genai_types.Part(text=effective_goal)]
            )
            async for ev in runner.run_async(
                user_id="orbit_user",
                session_id=session_id,
                new_message=message,
                run_config=RunConfig(streaming_mode=StreamingMode.SSE),
            ):
                parts = getattr(ev.content, "parts", None) or [] if ev.content else []
                for part in parts:
                    call = getattr(part, "function_call", None)
                    if call is not None:
                        emit("tool_call", tool=call.name, args=dict(call.args or {}))
                        continue
                    response = getattr(part, "function_response", None)
                    if response is not None:
                        emit("tool_result", tool=response.name)
                        continue
                    text = getattr(part, "text", None)
                    if not text:
                        continue
                    if ev.partial:
                        emit("text_delta", text=text)
                    elif ev.is_final_response():
                        # The non-partial final event repeats the whole answer
                        # the deltas already carried — collected here as the
                        # canonical result, never re-emitted as a delta.
                        final_texts.append(text)
            text = (
                "\n".join(final_texts)
                if final_texts
                else "(no final text response)"
            )
            # Emitted BEFORE the finally block, on purpose. runner.close()
            # tears down six MCP server subprocesses and measured 2-3s — time
            # the user previously spent looking at a finished task with the
            # input still locked, because nothing was said until after
            # teardown. The GUI unlocks on this event; [TASK:DONE] then just
            # confirms the process is ready for the next goal.
            emit("result", status="COMPLETED", text=text)
            return text
        finally:
            # A one-off task closes here. Each run spawns its own Playwright
            # MCP subprocess (its own Chrome profile lock), and without an
            # explicit close a leaked subprocess collides with the next task's
            # browser ("browser is already in use") — found by the eval
            # harness, not by inspection.
            #
            # A CONVERSATION's runner deliberately survives: keeping the
            # browser open between turns is most of the point, and the same
            # collision cannot happen because the next turn reuses this exact
            # subprocess rather than spawning a rival one. It is closed by
            # close_conversation() when the conversation ends, and by
            # close_all_conversations() on worker shutdown — so the lock is
            # still released, just later.
            if reuse_key is None:
                await runner.close()

    aio_task = await tm.submit(task_id, lane, work)
    try:
        result = await aio_task
        return {
            "task_id": task_id,
            "status": "COMPLETED",
            "result": result,
            "conversation_id": conversation_id,
        }
    except asyncio.CancelledError:
        emit("result", status="CANCELLED", text="Task was cancelled.")
        return {
            "task_id": task_id,
            "status": "CANCELLED",
            "result": "Task was cancelled.",
        }
    except (ConnectionError, TimeoutError, OSError) as exc:
        friendly = graceful_degradation_message(task_id, exc)
        emit("result", status="FAILED", text=friendly)
        return {
            "task_id": task_id,
            "status": "FAILED",
            "result": friendly,
            "raw_error": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        message = f"Task failed: {type(exc).__name__}: {exc}"
        emit("result", status="FAILED", text=message)
        return {
            "task_id": task_id,
            "status": "FAILED",
            "result": message,
            "raw_error": str(exc),
        }


DEMO_GOAL = (
    "Go to https://example.com and tell me the exact page title and "
    "the first sentence of body text you see."
)


async def _run_and_print(
    goal: str,
    *,
    lane: str,
    conversation_id: str | None = None,
    on_event: EventSink | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Shared by the one-shot CLI path and the REPL: runs one goal through
    the exact same run_task() call, prints the same output shape either
    way. Not a second execution path — just the printing wrapped once
    instead of duplicated.

    on_event defaults to the console emitter, so a human at the REPL now sees
    each tool call as it happens rather than a blank screen. --serve overrides
    it with the JSONL emitter the GUI parses."""
    title = (goal[:60] + "...") if len(goal) > 60 else goal
    print(f"\n> {goal}\n  (working — this takes ~15-45s{', foreground lane' if lane == 'foreground' else ''})\n")
    outcome = await run_task(
        title, goal, lane=lane, conversation_id=conversation_id,
        on_event=_console_emitter if on_event is None else on_event,
        model=model,
    )

    print("-" * 60)
    if outcome["status"] == "COMPLETED":
        print(outcome["result"])
    else:
        print(f"FAILED: {outcome['result']}")
    print("-" * 60)
    conv_id = outcome.get("conversation_id")
    extra = f"  conversation: {conv_id}" if conv_id else ""
    print(f"task_id: {outcome['task_id']}{extra}  (visible in the GUI dashboard)\n")
    return outcome


async def _serve() -> None:
    """Warm-worker mode for the GUI dashboard.

    Reads one JSON line per task from stdin:
        {"goal": "...", "lane": "headless|foreground", "effort": "low|medium|high",
         "conversation_id": "CONV-..."}

    conversation_id is optional. When present, prior turn results from that
    conversation are injected as context, enabling follow-up questions.

    Writes two interleaved things to stdout:

      * `[ORBIT]{json}` lines — one per progress event, live, as ADK yields
        them (see the Progress events section at the top of this module).
        This is what drives the GUI's step rail while the task is running.
      * everything else — the same human-readable prose the CLI prints.

    Keeping both means the worker's stdout is still readable by a human
    tailing it, and a consumer that does not know about events simply sees a
    few extra lines rather than a format it cannot parse.

    Emits [TASK:DONE exit_code] so the GUI knows when the task finished.

    The process stays alive between tasks — imports and the event loop pay
    their cost once. MCP server subprocesses still restart per task (the
    Playwright Chrome lock prevents sharing them).
    """
    # Retention, once per worker. `db.purge_old_events` has existed and been
    # correct since the original schema and had no caller anywhere, so nothing
    # ever aged out — a database that only grew. Here rather than per task
    # because it is a sweep, not part of a task, and the warm worker starts
    # exactly once per GUI session.
    #
    # Never fatal: a failed purge is a housekeeping problem, and refusing to
    # accept goals because of one would be absurd.
    try:
        purged = await asyncio.to_thread(db.purge_old_events)
        if purged:
            print(f"[retention] purged {purged:,} old event rows", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[retention] skipped: {type(exc).__name__}: {exc}", flush=True)

    while True:
        try:
            line = await asyncio.to_thread(sys.stdin.readline)
        except Exception:
            break
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            # `{"close_conversation": "CONV-..."}` ends a conversation: closes
            # its runner and the six MCP subprocesses it was holding. The GUI
            # sends this when the user starts a new chat, so an abandoned
            # conversation does not keep a browser open indefinitely.
            closing = (req.get("close_conversation") or "").strip()
            if closing:
                await close_conversation(closing)
                print(f"[conversation] closed {closing}", flush=True)
                continue
            goal = req.get("goal", "").strip()
            lane = req.get("lane", "headless")
            effort = req.get("effort", "medium")
            # Optional. Absent or null means "whatever the process default
            # is", so an older client that does not send it is unaffected.
            model = (req.get("model") or "").strip() or None
            conv_id = req.get("conversation_id")
        except json.JSONDecodeError:
            continue
        if not goal:
            continue
        os.environ["ORBIT_EFFORT"] = effort
        outcome = await _run_and_print(
            goal, lane=lane, conversation_id=conv_id,
            on_event=_jsonl_emitter, model=model,
        )
        exit_code = 0 if outcome["status"] == "COMPLETED" else 1
        print(f"[TASK:DONE {exit_code}]", flush=True)

    # stdin closed — the GUI is gone. Release every conversation's MCP
    # subprocesses rather than orphaning them; a leaked Playwright process
    # holds a Chrome profile lock the next run would collide with.
    await close_all_conversations()


async def _repl() -> None:
    """Interactive REPL: a persistent loop with conversation continuity.

    All goals within a REPL session share one conversation_id, so the
    agent sees prior turn results when processing follow-ups. Type '/new'
    to start a fresh conversation within the same session.

    Runs every typed goal under lane="headless" (no --foreground inside
    the REPL) — a foreground-lane goal still goes through the one-shot
    CLI path with --foreground.
    """
    db.init_db()
    conv_id = db.create_conversation(title="REPL session", lane="headless")
    print("Orbit REPL — type a goal and press Enter.")
    print(f'  e.g. "{DEMO_GOAL}"')
    print("Type '/new' to start a fresh conversation.")
    print("Type 'exit' or 'quit' (or Ctrl+C/Ctrl+D) to leave.\n")
    while True:
        try:
            goal = input("orbit> ").strip()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            break
        if not goal:
            continue
        if goal.lower() in ("exit", "quit"):
            break
        if goal.lower() == "/new":
            await close_conversation(conv_id)
            conv_id = db.create_conversation(
                title="REPL session", lane="headless",
            )
            print("(new conversation started)\n")
            continue
        try:
            await _run_and_print(
                goal, lane="headless", conversation_id=conv_id,
            )
        except KeyboardInterrupt:
            print("\n(interrupted)\n")
    print("Goodbye.")


def _main() -> int:
    # Model output routinely contains non-ASCII (em-dashes, curly quotes,
    # accented names). The Windows console's default codepage renders those
    # as mojibake, so force UTF-8 on the output streams.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass  # non-reconfigurable stream (piped/redirected) — harmless

    if "--list-models" in sys.argv:
        from orbit.agent import DEFAULT_MODEL, KNOWN_MODELS

        current = os.environ.get("ORBIT_MODEL") or DEFAULT_MODEL
        print("\nKnown-good models (all verified to support tool calling):\n")
        for name, note in KNOWN_MODELS.items():
            marker = "*" if name == current else " "
            print(f" {marker} {name}\n     {note}\n")
        print(f"Active: {current}")
        print("Change it by setting ORBIT_MODEL in .env\n")
        return 0

    # --foreground opts into lane="foreground", the ONLY way the agent gets
    # the windows-control toolset at all (orbit/agent.py's build_agent).
    # Without it every task runs headless, same as before this flag
    # existed — this is additive, not a behavior change for existing
    # callers. Stripped out of argv before the rest is joined into the
    # goal string so it doesn't leak into the task title/goal text.
    if "--serve" in sys.argv[1:]:
        asyncio.run(_serve())
        return 0

    args = [a for a in sys.argv[1:] if a != "--foreground"]
    foreground = "--foreground" in sys.argv[1:]
    lane = "foreground" if foreground else "headless"

    # Everything after the module name (minus --foreground) is the goal,
    # so quoting is optional:
    #   python -m orbit.run_task find the cheapest 65 inch tv
    #   python -m orbit.run_task --foreground open notepad and type hello
    #
    # No goal at all (just `python -m orbit.run_task`, or
    # `--foreground` alone) now drops into the REPL instead of running
    # DEMO_GOAL once and exiting — that one-shot fallback was never a
    # real interactive entry point, and voice used to be the only actual
    # one. --foreground with no goal still just starts the (headless)
    # REPL; there is no foreground REPL mode (see _repl's docstring) —
    # use --foreground with an explicit goal for a one-shot foreground task.
    goal = " ".join(args).strip()
    if not goal:
        asyncio.run(_repl())
        return 0

    outcome = asyncio.run(_run_and_print(goal, lane=lane))
    return 0 if outcome["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
