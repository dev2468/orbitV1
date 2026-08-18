"""Tests for orbit/mcp_servers/uia_resolver.py — the module extracted out
of windows_control_tools.py so screen-perception and windows-control share
one real implementation instead of two that could drift apart.

get_uia_tree's depth/node-capping algorithm is tested against a fake UIA
element tree (not a real window) so it's deterministic regardless of
whatever happens to be on screen during a test run. process_name/
is_process_running are tested against real, safe, read-only OS state.
"""

from __future__ import annotations

from unittest import mock

from orbit.mcp_servers import uia_resolver


class _FakeRect:
    def __init__(self, bounds):
        self.left, self.top, self.right, self.bottom = bounds


class FakeElement:
    """Minimal stand-in for a pywinauto UIAWrapper — just the methods
    uia_resolver.get_uia_tree calls."""

    def __init__(self, name, *, role="Button", automation_id="", bounds=(0, 0, 10, 10), children=None):
        self._name = name
        self._role = role
        self._automation_id = automation_id
        self._bounds = bounds
        self._children = children or []

    def rectangle(self):
        return _FakeRect(self._bounds)

    def friendly_class_name(self):
        return self._role

    def window_text(self):
        return self._name

    def automation_id(self):
        return self._automation_id

    def is_visible(self):
        return True

    def children(self):
        return self._children


def _sample_tree():
    grandchild_a = FakeElement("grandchild-a")
    grandchild_b = FakeElement("grandchild-b")
    child_1 = FakeElement("child-1", children=[grandchild_a, grandchild_b])
    child_2 = FakeElement("child-2")
    root = FakeElement("root", children=[child_1, child_2])
    return root


def test_get_uia_tree_walks_depth_first_root_first():
    root = _sample_tree()
    with mock.patch.object(uia_resolver, "_connect", return_value=root):
        nodes = uia_resolver.get_uia_tree(12345, max_depth=10, max_nodes=100)

    names = [n["name"] for n in nodes]
    assert names[0] == "root"
    assert set(names) == {"root", "child-1", "grandchild-a", "grandchild-b", "child-2"}
    assert [n["depth"] for n in nodes if n["name"] == "root"] == [0]
    assert [n["depth"] for n in nodes if n["name"] == "grandchild-a"] == [2]


def test_get_uia_tree_respects_max_nodes():
    root = _sample_tree()
    with mock.patch.object(uia_resolver, "_connect", return_value=root):
        nodes = uia_resolver.get_uia_tree(12345, max_depth=10, max_nodes=2)

    assert len(nodes) == 2


def test_get_uia_tree_respects_max_depth_by_not_descending_past_it():
    root = _sample_tree()
    with mock.patch.object(uia_resolver, "_connect", return_value=root):
        nodes = uia_resolver.get_uia_tree(12345, max_depth=1, max_nodes=100)

    names = {n["name"] for n in nodes}
    # depth 0 (root) and depth 1 (child-1, child-2) included; depth 2
    # (grandchildren) never walked because their parent is AT max_depth.
    assert names == {"root", "child-1", "child-2"}


def test_get_uia_tree_node_shape():
    root = FakeElement("solo", role="Button", automation_id="SaveButton", bounds=(1, 2, 3, 4))
    with mock.patch.object(uia_resolver, "_connect", return_value=root):
        nodes = uia_resolver.get_uia_tree(12345, max_depth=5, max_nodes=10)

    assert nodes == [
        {
            "role": "Button",
            "name": "solo",
            "automation_id": "SaveButton",
            "bounds": (1, 2, 3, 4),
            "visible": True,
            "depth": 0,
        }
    ]


def test_is_process_running_true_for_this_test_runners_own_process():
    import os

    assert uia_resolver.is_process_running(os.getpid()) is True


def test_is_process_running_false_for_bogus_pid():
    assert uia_resolver.is_process_running(999999999) is False


def test_process_name_returns_empty_string_for_invalid_pid_rather_than_raising():
    assert uia_resolver.process_name(999999999) == ""
