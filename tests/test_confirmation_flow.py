"""Tests for the REPL confirmation flow (Phase 4).

The thing being protected: approval must grant permission for ONE action a
human actually looked at, and nothing more. Not a raised confidence, not a
lowered floor, not a standing permission, not a second click.

Per tests/CLAUDE.md every assertion is on the pending_confirmations table or
the events table — never on console output. The console asker is injected so
none of this needs a TTY.
"""

from __future__ import annotations

import pytest

import orbit.mcp_servers.windows_control_tools as wc
from orbit import confirmation, db
from orbit.policy import load_windows_control_policy
from orbit.tools.element_ref import ElementRef
from orbit.tools.foundation import ClassifiedToolError

FLOOR = load_windows_control_policy()["min_actuation_confidence"]

YES = lambda action, description: True    # noqa: E731
NO = lambda action, description: False    # noqa: E731


def _vision_target(name="the red knob"):
    return {
        "element_id": "vision:1/knob",
        "name": name,
        "bounds": [100, 200, 180, 240],
        "state": {"vision": {"model": "test"}},
        "source": "vision",
        "confidence": 0.50,
    }


def _uia_target():
    return {
        "element_id": "uia:1/Save",
        "name": "Save",
        "bounds": [10, 20, 90, 60],
        "state": None,
        "source": "uia",
        "confidence": 0.95,
    }


@pytest.fixture
def task_id():
    return db.create_task("confirmation flow test")


def _event_errors(task_id: str) -> list[str]:
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT error FROM events WHERE task_id = ?", (task_id,)
        ).fetchall()
    return [r["error"] or "" for r in rows]


def _event_results(task_id: str) -> list[str]:
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT result FROM events WHERE task_id = ?", (task_id,)
        ).fetchall()
    return [r["result"] or "" for r in rows]


# --- which calls get asked about -------------------------------------------


def test_a_vision_target_triggers_confirmation():
    assert confirmation.target_needs_confirmation(
        "windows_click", {"target": _vision_target()}
    ) is not None


def test_a_confident_uia_target_is_not_asked_about():
    """Confirmation is for what the gate would refuse. Asking about every
    click would train the user to say yes without reading."""
    assert confirmation.target_needs_confirmation(
        "windows_click", {"target": _uia_target()}
    ) is None


def test_raw_coordinates_trigger_confirmation():
    """_resolve_click_target scores raw {x, y} at VISION_INFERRED regardless
    of where the numbers came from, so it must be asked about too."""
    assert confirmation.target_needs_confirmation(
        "windows_click", {"target": {"x": 100, "y": 200}}
    ) is not None


def test_either_drag_endpoint_being_uncertain_triggers_confirmation():
    assert confirmation.target_needs_confirmation(
        "windows_drag", {"from_target": _uia_target(), "to_target": _vision_target()}
    ) is not None
    assert confirmation.target_needs_confirmation(
        "windows_drag", {"from_target": _vision_target(), "to_target": _uia_target()}
    ) is not None


def test_a_fully_confident_drag_is_not_asked_about():
    assert confirmation.target_needs_confirmation(
        "windows_drag", {"from_target": _uia_target(), "to_target": _uia_target()}
    ) is None


def test_non_positional_tools_are_not_confirmable():
    """windows_type sends keystrokes to whatever has focus — a box drawn on a
    screenshot tells a human nothing useful about it, so a confirmation
    prompt there would be theatre."""
    assert confirmation.target_needs_confirmation("windows_type", {"text": "hi"}) is None


def test_an_already_tokened_call_is_not_asked_again():
    """Otherwise the retry after approval would prompt a second time."""
    assert confirmation.target_needs_confirmation(
        "windows_click", {"target": _vision_target(), "approval_token": "abc"}
    ) is None


# --- the flow writes rows either way ---------------------------------------


def test_approval_records_an_approved_row_and_returns_a_token(task_id):
    token, cid = confirmation.request_confirmation(
        task_id, "windows_click", {"target": _vision_target()}, _vision_target(),
        prompt=YES,
    )
    assert token
    row = db.get_pending_confirmation(cid)
    assert row["status"] == "APPROVED"
    assert row["task_id"] == task_id
    assert row["action"] == "windows_click"
    assert row["candidate_box"] == (100, 200, 180, 240)


def test_refusal_records_a_rejected_row_and_no_token(task_id):
    token, cid = confirmation.request_confirmation(
        task_id, "windows_click", {"target": _vision_target()}, _vision_target(),
        prompt=NO,
    )
    assert token is None
    row = db.get_pending_confirmation(cid)
    assert row["status"] == "REJECTED"
    assert row["approval_token"] is None


def test_a_refusal_is_logged_as_an_event(task_id):
    """The audit trail has to show the refusal happened. A declined action
    that leaves no trace is indistinguishable from one never attempted."""
    confirmation.request_confirmation(
        task_id, "windows_click", {"target": _vision_target()}, _vision_target(),
        prompt=NO,
    )
    assert any("confirmation_denied" in e for e in _event_errors(task_id))


def test_an_approval_is_logged_as_an_event(task_id):
    confirmation.request_confirmation(
        task_id, "windows_click", {"target": _vision_target()}, _vision_target(),
        prompt=YES,
    )
    assert any("confirmation_approved" in r for r in _event_results(task_id))


def test_the_row_is_written_before_the_human_is_asked(task_id):
    """A crash mid-prompt must still leave evidence that something was
    proposed, rather than the request vanishing."""
    seen = {}

    def inspect(action, description):
        seen["pending"] = db.list_pending_confirmations(task_id=task_id)
        return False

    confirmation.request_confirmation(
        task_id, "windows_click", {"target": _vision_target()}, _vision_target(),
        prompt=inspect,
    )
    assert len(seen["pending"]) == 1
    assert seen["pending"][0]["status"] == "PENDING"


def test_no_interactive_console_fails_closed(task_id, monkeypatch):
    """THE unattended-run test. With no TTY the answer is no, automatically.
    A build where a headless process self-approves guessed clicks is exactly
    what the confidence floor exists to prevent."""
    monkeypatch.setattr(confirmation, "_console_is_interactive", lambda: False)
    token, cid = confirmation.request_confirmation(
        task_id, "windows_click", {"target": _vision_target()}, _vision_target()
    )
    assert token is None
    assert db.get_pending_confirmation(cid)["status"] == "REJECTED"


# --- what an approval actually buys ----------------------------------------


def test_an_approved_token_lets_exactly_one_click_through(task_id):
    element = ElementRef(**_vision_target())
    token, _ = confirmation.request_confirmation(
        task_id, "windows_click", {"target": _vision_target()}, _vision_target(),
        prompt=YES,
    )

    wc._require_confidence(element, token)  # first use: allowed

    with pytest.raises(ClassifiedToolError) as exc:
        wc._require_confidence(element, token)  # replay: refused
    assert exc.value.kind == "permission_denied"


def test_approval_does_not_change_the_elements_confidence(task_id):
    """The whole point. The element is still a 0.50 guess afterwards; what
    was granted was permission for one action, not a promotion."""
    target = _vision_target()
    element = ElementRef(**target)
    token, _ = confirmation.request_confirmation(
        task_id, "windows_click", {"target": target}, target, prompt=YES
    )
    wc._require_confidence(element, token)
    assert element.confidence == 0.50
    assert element.confidence < FLOOR


def test_approval_does_not_lower_the_floor(task_id):
    """A second, unapproved click must be refused exactly as before."""
    target = _vision_target()
    token, _ = confirmation.request_confirmation(
        task_id, "windows_click", {"target": target}, target, prompt=YES
    )
    wc._require_confidence(ElementRef(**target), token)

    assert load_windows_control_policy()["min_actuation_confidence"] == FLOOR
    with pytest.raises(ClassifiedToolError):
        wc._require_confidence(ElementRef(**target))


def test_a_rejected_confirmation_yields_nothing_usable(task_id):
    token, cid = confirmation.request_confirmation(
        task_id, "windows_click", {"target": _vision_target()}, _vision_target(),
        prompt=NO,
    )
    assert token is None
    with pytest.raises(ClassifiedToolError):
        wc._require_confidence(ElementRef(**_vision_target()), None)
    assert db.get_pending_confirmation(cid)["approval_token"] is None


def test_a_fabricated_token_is_refused(task_id):
    """The token is validated in the tool process against the DB, not merely
    trusted from whoever passed it."""
    with pytest.raises(ClassifiedToolError) as exc:
        wc._require_confidence(ElementRef(**_vision_target()), "made-up-token")
    assert exc.value.kind == "permission_denied"


def test_an_expired_token_is_refused(task_id):
    cid = db.create_pending_confirmation(
        "windows_click", {}, task_id=task_id, candidate_box=(1, 2, 3, 4)
    )
    token = db.resolve_pending_confirmation(cid, approved=True, ttl_seconds=-1)
    with pytest.raises(ClassifiedToolError):
        wc._require_confidence(ElementRef(**_vision_target()), token)


def test_a_confident_target_never_consumes_a_token(task_id):
    """A UIA-resolved element passes on its own merits. Spending an approval
    on it would waste a single-use grant the user meant for the guess."""
    cid = db.create_pending_confirmation("windows_click", {}, task_id=task_id)
    token = db.resolve_pending_confirmation(cid, approved=True)

    wc._require_confidence(ElementRef(**_uia_target()), token)
    assert db.get_pending_confirmation(cid)["token_consumed_at"] is None


# --- drag: one action, one approval ----------------------------------------


def test_one_token_covers_both_drag_endpoints(task_id):
    """A drag is one action with two ends. Checking each endpoint separately
    with the same single-use token would spend it on the first and refuse the
    second, turning a granted approval into a denial."""
    cid = db.create_pending_confirmation("windows_drag", {}, task_id=task_id)
    token = db.resolve_pending_confirmation(cid, approved=True)

    wc._require_confidence_pair(
        ElementRef(**_vision_target()), ElementRef(**_vision_target()), token
    )
    assert db.get_pending_confirmation(cid)["token_consumed_at"] is not None


def test_a_drag_with_an_uncertain_endpoint_is_refused_without_a_token(task_id):
    with pytest.raises(ClassifiedToolError) as exc:
        wc._require_confidence_pair(
            ElementRef(**_uia_target()), ElementRef(**_vision_target()), None
        )
    assert exc.value.kind == "permission_denied"


def test_a_fully_confident_drag_needs_no_token(task_id):
    wc._require_confidence_pair(
        ElementRef(**_uia_target()), ElementRef(**_uia_target()), None
    )


# --- the description a human is shown --------------------------------------


def test_the_description_names_the_source_and_the_box():
    """The person answering needs to know this is a GUESS. "the model thinks
    it's here" and "UIA resolved this" deserve different answers."""
    described = confirmation.describe_target(_vision_target())
    assert "vision" in described
    assert "0.50" in described
    assert "the red knob" in described


# --- Phase 5: the GUI channel ----------------------------------------------


def test_gui_approval_is_picked_up_by_the_waiting_process(task_id):
    """The GUI writes to pending_confirmations from a DIFFERENT process; the
    waiting side polls that row. This is the whole channel — no push, no IPC,
    just the shared SQLite file the dashboard already polls for tasks."""
    cid = db.create_pending_confirmation("windows_click", {}, task_id=task_id)
    db.resolve_pending_confirmation(cid, approved=True)  # stands in for the button

    token = confirmation._wait_for_gui_resolution(cid, timeout_s=5)
    assert token
    assert db.consume_approval_token(token) is not None


def test_gui_rejection_yields_no_token(task_id):
    cid = db.create_pending_confirmation("windows_click", {}, task_id=task_id)
    db.resolve_pending_confirmation(cid, approved=False)
    assert confirmation._wait_for_gui_resolution(cid, timeout_s=5) is None


def test_waiting_times_out_rather_than_blocking_forever(task_id):
    """Nobody at the dashboard must not mean a hung agent."""
    cid = db.create_pending_confirmation("windows_click", {}, task_id=task_id)
    assert confirmation._wait_for_gui_resolution(cid, timeout_s=0.2) is None
    assert db.get_pending_confirmation(cid)["status"] == "PENDING"


def test_the_gui_channel_is_off_by_default():
    """A non-zero wait makes every unattended run block on each confirmation
    before failing closed. Opt in deliberately."""
    assert confirmation._gui_wait_seconds() == 0


# --- Phase 6: provider keys -------------------------------------------------


def test_every_benchmark_arm_model_has_a_known_provider_key():
    """A model with no key mapping would fail every call and report as a 0%
    hit-rate — a configuration gap wearing the costume of a result."""
    from benchmarks.config import load_benchmark_config
    from benchmarks.grounding_bench import _REQUIRED_KEY_BY_PREFIX

    for arm in load_benchmark_config().arms:
        if arm.model is None:
            continue
        assert any(arm.model.startswith(p) for p in _REQUIRED_KEY_BY_PREFIX), arm.model


def test_a_missing_provider_key_fails_loudly_with_the_env_line(monkeypatch):
    from benchmarks.grounding_bench import _api_key_for

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    with pytest.raises(RuntimeError) as exc:
        _api_key_for("anthropic/claude-sonnet-4-5")
    assert "ANTHROPIC_API_KEY" in str(exc.value)
