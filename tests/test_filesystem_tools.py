"""Tests for the filesystem MCP server (Prompt 5): scoped-root enforcement
(including `..` traversal and absolute-path escapes), denylist-wins-over-
allowlist, fs_write_file's refuse-to-clobber default, fs_move/fs_copy's
independent src/dest scope checks, fs_delete's quarantine-not-destroy
behavior, and fs_read_file's untrusted-content wrapping.

Mirrors test_memory_tools.py's structure: call the BaseTool instances
directly (no MCP stdio transport needed), assert on ToolResult/DB state,
never on prose.
"""

from __future__ import annotations

import json

import pytest

import orbit.db as db
import orbit.mcp_servers.filesystem_tools as fs_tools


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point the filesystem server's policy at an isolated tmp_path root
    instead of the real project's data/fs_workspace, so these tests never
    touch or leave residue in real project files. `(PROJECT_ROOT / r)` is a
    no-op when `r` is already absolute (pathlib's join-with-absolute-rhs
    behavior), so an absolute tmp_path root works unchanged through
    _resolve_scoped_path's normal join logic."""
    root = tmp_path / "workspace"
    root.mkdir()
    quarantine = tmp_path / "quarantine"

    def _fake_policy():
        return {
            "allowed_roots": [str(root)],
            "denylist_keywords": [".env", ".git", "secret"],
            "quarantine_dir": str(quarantine),
            "quarantine_ttl_hours": 1,
            "max_read_bytes": 1000,
        }

    monkeypatch.setattr(fs_tools, "load_filesystem_policy", _fake_policy)
    return root


@pytest.mark.asyncio
async def test_write_then_read_round_trips_with_untrusted_wrapping(sandbox):
    caller = db.create_task("caller")
    target = str(sandbox / "notes.txt")

    write_result = await fs_tools.write_file_tool.execute(
        {"path": target, "content": "hello world", "mode": "create"}, task_id=caller
    )
    assert write_result.ok

    read_result = await fs_tools.read_file_tool.execute({"path": target}, task_id=caller)
    assert read_result.ok
    assert "hello world" in read_result.data["content"]
    assert read_result.data["content"].startswith("<untrusted_local_content")
    assert read_result.data["content"].rstrip().endswith("</untrusted_local_content>")


@pytest.mark.asyncio
async def test_write_file_create_mode_refuses_to_clobber_existing(sandbox):
    caller = db.create_task("caller")
    target = str(sandbox / "existing.txt")
    (sandbox / "existing.txt").write_text("original")

    result = await fs_tools.write_file_tool.execute(
        {"path": target, "content": "clobbered", "mode": "create"}, task_id=caller
    )
    assert result.ok is False
    assert result.error.kind == "state_failure"
    assert (sandbox / "existing.txt").read_text() == "original"


@pytest.mark.asyncio
async def test_write_file_overwrite_mode_replaces_content(sandbox):
    caller = db.create_task("caller")
    target = str(sandbox / "existing.txt")
    (sandbox / "existing.txt").write_text("original")

    result = await fs_tools.write_file_tool.execute(
        {"path": target, "content": "replaced", "mode": "overwrite"}, task_id=caller
    )
    assert result.ok
    assert result.data["overwrote_existing"] is True
    assert (sandbox / "existing.txt").read_text() == "replaced"


@pytest.mark.asyncio
async def test_dotdot_traversal_outside_root_is_refused(sandbox):
    caller = db.create_task("caller")
    escape_path = str(sandbox / ".." / "outside.txt")

    result = await fs_tools.read_file_tool.execute({"path": escape_path}, task_id=caller)
    assert result.ok is False
    assert result.error.kind == "permission_denied"
    assert not (sandbox.parent / "outside.txt").exists()


@pytest.mark.asyncio
async def test_absolute_path_outside_root_is_refused(sandbox, tmp_path):
    caller = db.create_task("caller")
    outside = tmp_path / "elsewhere" / "file.txt"
    outside.parent.mkdir()
    outside.write_text("x")

    result = await fs_tools.read_file_tool.execute({"path": str(outside)}, task_id=caller)
    assert result.ok is False
    assert result.error.kind == "permission_denied"


@pytest.mark.asyncio
async def test_denylist_wins_even_inside_allowed_root(sandbox):
    caller = db.create_task("caller")
    denylisted = str(sandbox / ".env")

    result = await fs_tools.write_file_tool.execute(
        {"path": denylisted, "content": "SECRET=1", "mode": "create"}, task_id=caller
    )
    assert result.ok is False
    assert result.error.kind == "permission_denied"
    assert not (sandbox / ".env").exists()


@pytest.mark.asyncio
async def test_move_checks_dest_scope_independently_of_src(sandbox, tmp_path):
    caller = db.create_task("caller")
    src = sandbox / "movable.txt"
    src.write_text("data")
    outside_dest = str(tmp_path / "outside" / "movable.txt")

    result = await fs_tools.move_tool.execute(
        {"src": str(src), "dest": outside_dest}, task_id=caller
    )
    assert result.ok is False
    assert result.error.kind == "permission_denied"
    # src must be untouched — a partial move (src gone, dest also refused)
    # would be worse than an outright refusal.
    assert src.exists()


@pytest.mark.asyncio
async def test_copy_leaves_src_untouched(sandbox):
    caller = db.create_task("caller")
    src = sandbox / "original.txt"
    src.write_text("copy me")
    dest = str(sandbox / "copied.txt")

    result = await fs_tools.copy_tool.execute({"src": str(src), "dest": dest}, task_id=caller)
    assert result.ok
    assert src.read_text() == "copy me"
    assert (sandbox / "copied.txt").read_text() == "copy me"


@pytest.mark.asyncio
async def test_delete_quarantines_rather_than_destroys(sandbox, tmp_path):
    caller = db.create_task("caller")
    target = sandbox / "doomed.txt"
    target.write_text("please keep me safe")

    result = await fs_tools.delete_tool.execute({"path": str(target)}, task_id=caller)
    assert result.ok
    assert not target.exists()  # gone from its original location...

    quarantine_dir = tmp_path / "quarantine"
    quarantined_files = [p for p in quarantine_dir.iterdir() if p.suffix != ".json"]
    assert len(quarantined_files) == 1
    assert quarantined_files[0].read_text() == "please keep me safe"

    meta_files = list(quarantine_dir.glob("*.meta.json"))
    assert len(meta_files) == 1
    meta = json.loads(meta_files[0].read_text())
    assert meta["original_path"] == str(target)
    assert "ttl_expires_at" in meta


@pytest.mark.asyncio
async def test_list_dir_and_get_metadata(sandbox):
    caller = db.create_task("caller")
    (sandbox / "a.txt").write_text("aaa")
    (sandbox / "subdir").mkdir()

    list_result = await fs_tools.list_dir_tool.execute({"path": str(sandbox)}, task_id=caller)
    assert list_result.ok
    names = {e["name"] for e in list_result.data["entries"]}
    assert names == {"a.txt", "subdir"}

    meta_result = await fs_tools.get_metadata_tool.execute(
        {"path": str(sandbox / "a.txt")}, task_id=caller
    )
    assert meta_result.ok
    assert meta_result.data["type"] == "file"
    assert meta_result.data["size"] == 3


@pytest.mark.asyncio
async def test_search_matches_by_name_and_content(sandbox):
    caller = db.create_task("caller")
    (sandbox / "report.txt").write_text("quarterly numbers")
    (sandbox / "other.txt").write_text("nothing relevant here")

    by_name = await fs_tools.search_tool.execute(
        {"root": str(sandbox), "query": "report", "match_content": False, "limit": 50}, task_id=caller
    )
    assert by_name.ok
    assert [m["path"] for m in by_name.data["matches"]] and "report.txt" in by_name.data["matches"][0]["path"]

    by_content = await fs_tools.search_tool.execute(
        {"root": str(sandbox), "query": "quarterly", "match_content": True, "limit": 50}, task_id=caller
    )
    assert by_content.ok
    assert any("report.txt" in m["path"] for m in by_content.data["matches"])


def test_fs_delete_is_registered_high_tier():
    """fs_delete must stay tier='high' in risk_tiers.yaml so SafetyPlugin's
    unconditional high-tier block (no confirm channel wired) is what keeps
    it unreachable, not an accidental omission from the registry."""
    from orbit.policy import load_risk_tiers

    tiers = load_risk_tiers()
    assert tiers.get("fs_delete") == "high"


def test_fs_delete_is_not_in_the_exposed_tool_filter():
    """Exposing a guaranteed-to-be-blocked tool would cost tool-selection
    surface area for nothing (same reasoning that held browser_close back
    from research_product's filter) — confirm the skill actually holds it
    back."""
    from orbit.skills import filesystem as filesystem_skill

    toolset = filesystem_skill.build_toolset(task_id="probe")
    assert "fs_delete" not in toolset.tool_filter
