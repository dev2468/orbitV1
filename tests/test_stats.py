"""gui/stats.py — the arithmetic behind the History KPI tiles and every
duration / relative-time string in the GUI.

No QApplication here on purpose: this module is pure logic precisely so the
analytics can be pinned without standing up a widget tree.
"""

from datetime import datetime, timezone

import pytest

from gui.stats import (
    compute_kpis,
    count_tool_calls,
    format_duration,
    format_percent,
    parse_ts,
    relative_time,
    task_duration_seconds,
)

NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)


# -- parse_ts ----------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "2026-09-06T11:58:00+00:00",  # what db._now() writes
        "2026-09-06T11:58:00Z",       # trailing Z
        "2026-09-06T11:58:00",        # naive — assumed UTC
    ],
)
def test_parse_ts_accepts_every_shape_that_reaches_the_db(raw):
    dt = parse_ts(raw)
    assert dt is not None
    assert dt.tzinfo is not None, "must return an aware datetime"
    assert dt.utcoffset().total_seconds() == 0


@pytest.mark.parametrize("raw", [None, "", "   ", "not a date", 12345])
def test_parse_ts_returns_none_rather_than_raising(raw):
    """One malformed row must not take out the whole History view."""
    assert parse_ts(raw) is None


def test_naive_timestamps_are_comparable_with_aware_now():
    """The bug this guards: comparing naive vs aware raises TypeError.

    Any row written without an offset would otherwise crash every duration
    and relative-time call that touches it.
    """
    assert relative_time("2026-09-06T11:58:00", now=NOW) == "2m ago"


# -- format_duration ---------------------------------------------------------

@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0s"),
        (34, "34s"),
        (59, "59s"),
        (60, "1m00s"),
        (72, "1m12s"),
        (3599, "59m59s"),
        (3600, "1h00m"),
        (7440, "2h04m"),
    ],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


@pytest.mark.parametrize("seconds", [None, -1, -100])
def test_format_duration_is_empty_when_there_is_nothing_to_show(seconds):
    """Empty string, so a caller can hide the label rather than print '0s'."""
    assert format_duration(seconds) == ""


# -- relative_time -----------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-09-06T11:59:40+00:00", "Now"),     # <60s reads as Now
        ("2026-09-06T11:58:00+00:00", "2m ago"),
        ("2026-09-06T09:00:00+00:00", "3h ago"),
        ("2026-09-03T12:00:00+00:00", "3d ago"),
        ("2026-08-20T12:00:00+00:00", "Aug 20"),  # >1 week: absolute date
    ],
)
def test_relative_time(raw, expected):
    assert relative_time(raw, now=NOW) == expected


def test_future_timestamps_read_as_now_not_negative():
    """Clock skew must never render 'in 5m' or '-5m ago'."""
    assert relative_time("2026-09-06T12:05:00+00:00", now=NOW) == "Now"


# -- task_duration_seconds ---------------------------------------------------

def test_duration_of_a_finished_task_spans_created_to_completed():
    task = {
        "created_at": "2026-09-06T11:59:26+00:00",
        "completed_at": "2026-09-06T12:00:00+00:00",
    }
    assert task_duration_seconds(task) == 34.0


def test_duration_of_a_running_task_counts_up_to_now():
    task = {"created_at": "2026-09-06T11:59:00+00:00"}
    assert task_duration_seconds(task, now=NOW) == 60.0


def test_duration_is_none_without_a_parseable_start():
    assert task_duration_seconds({}) is None
    assert task_duration_seconds({"created_at": "garbage"}) is None


# -- compute_kpis ------------------------------------------------------------

def _tasks():
    return [
        {"status": "COMPLETED", "created_at": "2026-09-06T11:59:26+00:00",
         "completed_at": "2026-09-06T12:00:00+00:00"},                       # 34s
        {"status": "COMPLETED", "created_at": "2026-09-06T11:58:00+00:00",
         "completed_at": "2026-09-06T11:59:00+00:00"},                       # 60s
        {"status": "FAILED", "created_at": "2026-09-06T11:00:00+00:00",
         "completed_at": "2026-09-06T11:00:45+00:00"},                       # 45s
        {"status": "RUNNING", "created_at": "2026-09-06T11:59:00+00:00"},
    ]


def test_success_rate_is_over_decided_tasks_not_all_rows():
    """2 completed of 3 *decided* = 67%, not 2 of 4 rows = 50%.

    Counting a RUNNING task as a non-success makes the rate sag while a task
    is in flight and jump when it lands, which reads as a bug to the user.
    """
    k = compute_kpis(_tasks())
    assert k["completed"] == 2
    assert k["failed"] == 1
    assert k["running"] == 1
    assert k["success_rate"] == pytest.approx(2 / 3)
    assert format_percent(k["success_rate"]) == "67%"


def test_average_duration_excludes_unfinished_tasks():
    """An in-flight task has no duration; including its partial elapsed time
    would drag the average toward zero."""
    k = compute_kpis(_tasks())
    assert k["avg_duration_seconds"] == pytest.approx((34 + 60 + 45) / 3)


def test_empty_input_yields_none_not_a_confident_zero():
    k = compute_kpis([])
    assert k["total"] == 0
    assert k["success_rate"] is None
    assert k["avg_duration_seconds"] is None
    assert format_percent(k["success_rate"]) == "—"
    assert format_duration(k["avg_duration_seconds"]) == ""


def test_all_running_has_no_success_rate():
    k = compute_kpis([{"status": "RUNNING", "created_at": "2026-09-06T11:00:00+00:00"}])
    assert k["total"] == 1
    assert k["success_rate"] is None


def test_status_matching_is_case_insensitive():
    k = compute_kpis([
        {"status": "completed", "created_at": "2026-09-06T11:00:00+00:00",
         "completed_at": "2026-09-06T11:00:10+00:00"},
    ])
    assert k["completed"] == 1


# -- count_tool_calls --------------------------------------------------------

def test_count_tool_calls_ignores_rows_without_a_tool():
    events = [{"tool_call": "browser_open"}, {"tool_call": None},
              {"tool_call": "browser_click"}, {}]
    assert count_tool_calls(events) == 2
    assert count_tool_calls([]) == 0
