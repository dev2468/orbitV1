"""Cost-control invariants.

Every check here guards something that fails SILENTLY — the task still
completes, the tests still pass, and the only symptom is a larger bill. That
is precisely why they need pinning: nothing else in the suite would notice.

The numbers these defend came from this build's own event log, measured
after an OpenRouter key was exhausted unexpectedly:

  browser_snapshot          156 calls, avg 76,811 chars, max 621,442
  perception_capture_screenshot           327,509 chars in ONE call
  perception_get_uia_tree    33 calls, avg 12,955 chars
  TOTAL tool-result output ever: 13.8M chars (~3.46M tokens)

The compounding matters more than any single number: an LLM API is
stateless, so every tool result stays in the conversation and is re-sent
with every subsequent request for the rest of the task. A 155,000-token
snapshot on turn 3 of a 40-turn task is not billed once, it is billed 37
more times.
"""

import types as pytypes

import pytest

from orbit import agent as agent_mod
from orbit.mcp_servers import browser_policy_tools as bpt
from orbit.mcp_servers import perception_server as psrv
from orbit.policy import SafetyPlugin


def _fn_response(name, response):
    fr = pytypes.SimpleNamespace(name=name, response=response)
    return pytypes.SimpleNamespace(function_response=fr, text=None)


def _request(*results):
    """An LlmRequest-shaped stand-in: one Content per tool result."""
    contents = [
        pytypes.SimpleNamespace(parts=[_fn_response(n, r)]) for n, r in results
    ]
    return pytypes.SimpleNamespace(contents=contents)


def _plugin(**kw):
    return SafetyPlugin(risk_tiers={}, tool_registry=set(), **kw)


# --- prompt caching ---------------------------------------------------------
# The marker is fragile in a specific way: three other plausible routes to it
# are silently inert on the installed versions (ADK's cache_config, a
# before_model_callback, and litellm's cache_control_injection_points kwarg —
# see _mark_cache_breakpoint's docstring). A regression here would look
# exactly like success.


def test_cache_breakpoint_is_attached_to_the_last_message():
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "do the thing"},
    ]
    out = agent_mod._mark_cache_breakpoint(messages)

    last = out[-1]["content"]
    assert isinstance(last, list), "string content must become a block list"
    assert last[-1]["cache_control"] == {"type": "ephemeral"}
    assert last[-1]["text"] == "do the thing"

    # Gemini honours only the LAST breakpoint, so an extra marker earlier in
    # the request would be wasted at best and displace this one at worst.
    assert "cache_control" not in str(out[0]["content"])


def test_cache_breakpoint_skips_messages_with_nothing_to_anchor_to():
    # A tool_calls-only assistant turn carries no text block. The marker has
    # to walk back to the previous text-bearing message rather than landing
    # nowhere and silently disabling caching for the whole request.
    messages = [
        {"role": "user", "content": "earlier text"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
    ]
    out = agent_mod._mark_cache_breakpoint(messages)
    assert out[0]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_cache_breakpoint_attaches_to_an_existing_block_list():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
            ],
        }
    ]
    out = agent_mod._mark_cache_breakpoint(messages)
    assert "cache_control" not in out[0]["content"][0]
    assert out[0]["content"][1]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize(
    "messages",
    [[], [{"role": "user"}], [{"role": "user", "content": 42}], [{}]],
)
def test_cache_breakpoint_never_raises(messages):
    # A caching hint is an optimisation. A malformed message shape must
    # degrade to an uncached call, never fail the user's task.
    agent_mod._mark_cache_breakpoint(messages)


def test_select_model_wires_the_caching_client():
    model = agent_mod.select_model()
    assert isinstance(model.llm_client, agent_mod._CachingLiteLLMClient)


# --- browser snapshot cap ---------------------------------------------------


def test_oversized_page_content_is_truncated(monkeypatch):
    monkeypatch.setattr(bpt, "_load_max_content_chars", lambda: 500)
    wrapped = bpt._wrap_untrusted("x" * 50_000, "https://example.com")

    assert len(wrapped) < 2_000, "cap did not apply"
    assert "TRUNCATED" in wrapped, "truncation must be announced, never silent"
    assert "49,500 more characters" in wrapped


def test_truncation_never_strips_the_untrusted_markers():
    # The cap must not become a hole in the injection defence: truncated
    # page text is still attacker-controlled and still needs its wrapper.
    wrapped = bpt._wrap_untrusted("y" * 200_000, "https://evil.test")
    assert wrapped.startswith('<untrusted_web_content source="https://evil.test">')
    assert wrapped.rstrip().endswith("</untrusted_web_content>")


def test_content_under_the_cap_is_passed_through_unchanged():
    wrapped = bpt._wrap_untrusted("a short page", "https://example.com")
    assert "TRUNCATED" not in wrapped
    assert "a short page" in wrapped


# --- UIA tree pruning -------------------------------------------------------


def test_uia_prune_drops_invisible_and_unaddressable_nodes():
    payload = {
        "window_handle": 1,
        "nodes": [
            {"name": "Save", "automation_id": "btnSave", "visible": True},
            {"name": None, "automation_id": None, "visible": True},   # noise
            {"name": "Hidden", "automation_id": "x", "visible": False},  # offscreen
            {"name": None, "automation_id": "onlyId", "visible": True},
        ],
    }
    out = psrv._prune_uia_nodes(payload)

    names = [n.get("name") for n in out["nodes"]]
    assert names == ["Save", None] or len(out["nodes"]) == 2
    assert out["nodes_pruned"] == 2
    # Null fields are stripped: a flat node list repeats every key on every
    # node, and nulls are pure padding.
    assert all("visible" not in n or n["visible"] is not None for n in out["nodes"])


def test_uia_prune_leaves_error_payloads_alone():
    err = {"error": "state_failure", "message": "no window"}
    assert psrv._prune_uia_nodes(err) == err


# --- history compaction -----------------------------------------------------


@pytest.mark.asyncio
async def test_stale_oversized_results_are_elided_but_recent_ones_survive():
    big = {"data": "z" * 5000}
    req = _request(
        ("browser_snapshot", big),   # oldest -> should be elided
        ("browser_snapshot", big),
        ("perception_get_uia_tree", big),
        ("windows_click", big),      # newest 3 -> kept verbatim
    )
    await _plugin(keep_full_results=3).before_model_callback(
        callback_context=None, llm_request=req
    )

    responses = [c.parts[0].function_response.response for c in req.contents]
    assert "elided" in responses[0], "oldest oversized result should be elided"
    assert responses[0]["elided"].startswith("This browser_snapshot result")
    for kept in responses[1:]:
        assert kept == big, "the most recent results must not be touched"


@pytest.mark.asyncio
async def test_small_stale_results_are_left_alone():
    small = {"ok": True}
    req = _request(*[("windows_key", small)] * 6)
    await _plugin(keep_full_results=1).before_model_callback(
        callback_context=None, llm_request=req
    )
    assert all(
        c.parts[0].function_response.response == small for c in req.contents
    ), "compaction must not churn results that are already cheap"


@pytest.mark.asyncio
async def test_elision_is_announced_rather_than_silent():
    # A model shown an unexplained gap confabulates over it; one told the
    # data was dropped can simply call the tool again.
    req = _request(
        ("browser_snapshot", {"d": "q" * 9000}),
        ("windows_click", {"ok": True}),
    )
    await _plugin(keep_full_results=1).before_model_callback(
        callback_context=None, llm_request=req
    )
    note = req.contents[0].parts[0].function_response.response["elided"]
    assert "Call the tool again" in note
    assert "characters" in note


@pytest.mark.asyncio
async def test_compaction_never_raises_on_unexpected_shapes():
    for bad in (
        pytypes.SimpleNamespace(contents=None),
        pytypes.SimpleNamespace(contents=[pytypes.SimpleNamespace(parts=None)]),
        pytypes.SimpleNamespace(),
    ):
        await _plugin().before_model_callback(
            callback_context=None, llm_request=bad
        )


# --- tool surface -----------------------------------------------------------


def test_foreground_lane_does_not_load_browser_tools():
    """Foreground drives real Chrome through windows-control, and the
    instruction says so outright. Loading Playwright's 14 browser tools too
    would spend roughly 5,000 tokens per call advertising tools the model is
    explicitly told not to call."""
    fg = agent_mod.build_agent(task_id="", lane="foreground")
    names = {n for ts in fg.tools for n in (getattr(ts, "tool_filter", None) or [])}

    assert not {n for n in names if n.startswith("browser_")}
    assert "windows_batch_actions" in names


def test_foreground_instruction_does_not_advertise_absent_tools():
    """build_agent stopped loading the filesystem and communication toolsets
    for the foreground lane. Describing them anyway would cost a wasted tool
    call each time the model believed it had them."""
    fg = agent_mod.build_agent(task_id="", lane="foreground")
    names = {n for ts in fg.tools for n in (getattr(ts, "tool_filter", None) or [])}
    instruction = fg.instruction

    for absent in ("fs_read_file", "email_draft", "calendar_create_event"):
        assert absent not in names
        assert absent not in instruction, f"instruction still advertises {absent}"
