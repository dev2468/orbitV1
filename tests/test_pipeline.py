"""Tests for the message pipeline hardening (Phase 3).

Covers: cache breakpoint immutability, Continue injection loop bound,
select_model explicit params, build_agent lane validation.
"""

import pytest

from orbit.agent import (
    _mark_cache_breakpoint,
    _ensure_not_ending_on_model_turn,
    _MAX_CONTINUE_INJECTIONS,
    select_model,
    build_agent,
)


class TestCacheBreakpointImmutability:
    def test_does_not_mutate_original_message(self):
        msg = {"role": "user", "content": "hello"}
        original_content = msg["content"]
        result = _mark_cache_breakpoint([msg])
        assert msg["content"] == original_content
        assert result[0] is not msg

    def test_does_not_mutate_list_content_blocks(self):
        block = {"type": "text", "text": "hello"}
        msg = {"role": "user", "content": [block]}
        result = _mark_cache_breakpoint([msg])
        assert "cache_control" not in block
        assert result[0] is not msg

    def test_marks_last_text_message(self):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ]
        result = _mark_cache_breakpoint(msgs)
        last = result[-1]
        assert isinstance(last["content"], list)
        assert last["content"][0]["cache_control"] == {"type": "ephemeral"}

    def test_empty_messages_returns_empty(self):
        assert _mark_cache_breakpoint([]) == []

    def test_skips_tool_calls_only_messages(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [{"id": "1"}]},
        ]
        result = _mark_cache_breakpoint(msgs)
        first = result[0]
        assert isinstance(first["content"], list)
        assert first["content"][0]["cache_control"] == {"type": "ephemeral"}


class TestContinueInjectionBound:
    def test_appends_continue_on_trailing_assistant(self):
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "thinking..."},
        ]
        result = _ensure_not_ending_on_model_turn(msgs)
        assert result[-1] == {"role": "user", "content": "Continue."}

    def test_does_not_append_on_user_turn(self):
        msgs = [{"role": "user", "content": "go"}]
        result = _ensure_not_ending_on_model_turn(msgs)
        assert len(result) == 1

    def test_does_not_append_on_tool_calls(self):
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "tool_calls": [{"id": "1"}]},
        ]
        result = _ensure_not_ending_on_model_turn(msgs)
        assert len(result) == 2

    def test_stops_after_max_injections(self):
        msgs = [{"role": "user", "content": "go"}]
        for _ in range(_MAX_CONTINUE_INJECTIONS):
            msgs.append({"role": "assistant", "content": "..."})
            msgs.append({"role": "user", "content": "Continue."})
        msgs.append({"role": "assistant", "content": "still thinking"})

        result = _ensure_not_ending_on_model_turn(msgs)
        continue_count = sum(
            1 for m in result
            if m.get("role") == "user" and m.get("content") == "Continue."
        )
        assert continue_count == _MAX_CONTINUE_INJECTIONS

    def test_empty_returns_empty(self):
        assert _ensure_not_ending_on_model_turn([]) == []


class TestSelectModelExplicit:
    def test_accepts_explicit_model_name(self):
        llm = select_model(model_name="openrouter/test/model")
        assert llm.model == "openrouter/test/model"

    def test_accepts_explicit_effort_without_error(self):
        for effort in ("low", "medium", "high"):
            llm = select_model(effort=effort)
            assert llm.model is not None

    def test_unknown_effort_falls_back_without_error(self):
        llm = select_model(effort="ultra")
        assert llm.model is not None


class TestBuildAgentLaneValidation:
    def test_unknown_lane_raises(self):
        with pytest.raises(ValueError, match="Unknown lane"):
            build_agent(lane="sideways")

    def test_headless_is_valid(self):
        agent = build_agent(lane="headless")
        assert agent is not None

    def test_foreground_is_valid(self):
        agent = build_agent(lane="foreground")
        assert agent is not None
