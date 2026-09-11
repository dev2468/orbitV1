"""Tool implementations for the `devmcp` MCP server — local machine access.

Four tools: `list_files`, `read_file`, `write_file`, and `run_command`
(PowerShell). Collectively the most powerful surface in this system, and the
one a fresh clone could not previously start at all.

## Where this came from, and what changed on the way in

Until 2026-09-08 these lived in an external server at
`C:\\Users\\HP\\Desktop\\MCP\\server.py` — not in this repository, on its own
Python 3.14 venv, existing only on the author's machine. Every task loaded it,
in both lanes, so anyone else cloning Orbit got a toolset that failed to start.
Vendoring it is what makes the project actually runnable by someone else.

Three things changed in the move, and they are the reason this is a rewrite
rather than a copy:

1. **The security layer the project's docs described did not exist.** The
   external module read, in full:

       # Security restrictions disabled — all PowerShell commands are allowed.
       DANGEROUS_PATTERNS = re.compile(r'(?!)')  # never matches
       def is_command_allowed(command): return True, ""

   So `run_command` ran arbitrary PowerShell, which also made `write_file`'s
   path allowlist decorative — `Set-Content` reaches anywhere. It is now real,
   and it lives in `orbit/config/devmcp_policy.yaml` because policy belongs in
   YAML read at call time (invariant 6), not in Python.

2. **Every tool body now runs inside `BaseTool.execute`** (invariant 4), so
   these get the same timeout, cancellation check, secret redaction and event
   logging as every other tool. The external server had its own ad-hoc
   try/except per tool and logged nothing to the events table.

3. **Paths are configured, not hardcoded.** The old write allowlist named the
   author's own directories, and its check was `startswith()` on a normalised
   string — so `.../Downloads-evil` passed as being inside `.../Downloads`.
   The check is now segment-wise against a resolved path.

## What this is not

A blocklist over a Turing-complete shell is a guard rail, not a sandbox. Read
the top of `devmcp_policy.yaml` for the honest statement of what it does and
does not buy. The containment that matters is that every call still goes
through `SafetyPlugin` and must appear in `risk_tiers.yaml`.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml

from orbit import db
from orbit.task_manager import CancellationToken
from orbit.tools.foundation import BaseTool, ClassifiedToolError, Confidence, ToolMetadata

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "devmcp_policy.yaml"

_FALLBACK_TASK_ID = "adhoc-devmcp-server"
_ORBIT_TASK_ID = os.environ.get("ORBIT_TASK_ID", "").strip()


def _resolve_task_id(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    if _ORBIT_TASK_ID:
        return _ORBIT_TASK_ID
    if db.get_task(_FALLBACK_TASK_ID) is None:
        db.create_task("devmcp server adhoc calls", task_id=_FALLBACK_TASK_ID)
    return _FALLBACK_TASK_ID


def load_devmcp_policy(path: Optional[Path] = None) -> dict:
    """Read at call time, never cached — the convention every loader here
    follows, so an edit takes effect on the next tool call."""
    return yaml.safe_load((path or _CONFIG_PATH).read_text(encoding="utf-8")) or {}


# --- path checks -------------------------------------------------------------


def _is_inside(child: Path, parent: Path) -> bool:
    """True when `child` is `parent` or beneath it, compared by path SEGMENT.

    Not `str.startswith`. The external server used a string prefix, which
    accepts `C:/Users/HP/Downloads-evil` as being inside
    `C:/Users/HP/Downloads` — a sibling directory an attacker can create.
    `Path.relative_to` compares components, so it cannot be fooled that way.
    """
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _resolve(path_str: str) -> Path:
    """Absolute, symlink-resolved, `~` expanded.

    Resolution happens BEFORE any allowlist check, so a symlink planted inside
    an allowed root cannot redirect a write outside one.
    """
    return Path(os.path.expanduser(path_str)).resolve()


def _denied_by_keyword(path: Path, keywords: list) -> Optional[str]:
    lowered = str(path).replace("\\", "/").lower()
    for keyword in keywords or []:
        if str(keyword).lower() in lowered:
            return str(keyword)
    return None


def _check_write_path(path_str: str) -> Path:
    policy = load_devmcp_policy().get("write", {})
    resolved = _resolve(path_str)

    hit = _denied_by_keyword(resolved, policy.get("denylist_keywords", []))
    if hit:
        # The denylist wins over the allowlist, deliberately — it is what
        # keeps a later-widened root from exposing secrets without anyone
        # remembering to re-derive a denylist at that point.
        raise ClassifiedToolError(
            "permission_denied",
            f"write refused: path matches the denied keyword {hit!r}. This is "
            "a policy block, not a retryable error — do not try a variant path.",
        )

    roots = [_resolve(r) for r in policy.get("allowed_roots", [])]
    if not any(_is_inside(resolved, root) for root in roots):
        raise ClassifiedToolError(
            "permission_denied",
            f"write refused: {resolved} is outside the allowed roots "
            f"({', '.join(str(r) for r in roots) or 'none configured'}). Add a "
            "root to orbit/config/devmcp_policy.yaml if this is intended — do "
            "not try to route around it.",
        )
    return resolved


def _check_read_path(path_str: str) -> Path:
    """Reading is not root-scoped — the point of this toolset is the user's
    real files, and a read damages nothing. The denylist is about not pulling
    secrets into the model's context (and thence the event log and the
    provider's logs), not about access control."""
    resolved = _resolve(path_str)
    policy = load_devmcp_policy().get("read", {})
    hit = _denied_by_keyword(resolved, policy.get("denylist_keywords", []))
    if hit:
        raise ClassifiedToolError(
            "permission_denied",
            f"read refused: path matches the denied keyword {hit!r}. Secrets "
            "are kept out of the model's context deliberately.",
        )
    return resolved


# --- run_command -------------------------------------------------------------

_POWERSHELL_WRAPPER_RE = re.compile(
    r"^powershell(?:\.exe)?\s+-Command\s+(.*)", re.IGNORECASE | re.DOTALL
)


def check_command_allowed(command: str) -> tuple[bool, str]:
    """(allowed, reason). Public so a caller can pre-validate.

    Patterns come from `devmcp_policy.yaml` and are matched case-insensitively
    against the WHOLE command, after the redundant `powershell -Command`
    wrapper is stripped — otherwise a wrapped command would present a
    different string to the rules than the one that actually runs.
    """
    command = (command or "").strip()
    if not command:
        return False, "empty command"

    patterns = load_devmcp_policy().get("command", {}).get("denied_patterns", [])
    for raw in patterns:
        try:
            if re.search(str(raw), command, re.IGNORECASE):
                return False, f"matches denied pattern {raw!r}"
        except re.error:
            # A malformed pattern must not silently disable the whole
            # blocklist, but it also must not fail every command. Skip it and
            # keep checking the rest.
            continue
    return True, ""


def _strip_powershell_wrapper(command: str) -> str:
    match = _POWERSHELL_WRAPPER_RE.match(command.strip())
    if not match:
        return command.strip()
    inner = match.group(1).strip()
    if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'":
        inner = inner[1:-1].strip()
    return inner


# --- tools -------------------------------------------------------------------

_MEDIUM_HEADLESS = dict(
    risk_tier="medium",
    lane="headless",
    requires_confirmation=False,
    is_destructive=False,
    returns_untrusted_content=True,
)


class ListFilesTool(BaseTool):
    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        folder = (args.get("folder") or "").strip()
        if not folder:
            raise ClassifiedToolError("reasoning_failure", "no folder path provided")
        resolved = _check_read_path(folder)
        if not resolved.exists():
            raise ClassifiedToolError(
                "state_failure", f"folder does not exist: {resolved}"
            )
        if not resolved.is_dir():
            raise ClassifiedToolError(
                "reasoning_failure", f"not a folder: {resolved}"
            )

        limit = int(load_devmcp_policy().get("read", {}).get("max_listing_entries", 200))
        names = sorted(p.name + ("/" if p.is_dir() else "") for p in resolved.iterdir())
        truncated = len(names) > limit
        return (
            {
                "path": str(resolved),
                "entries": names[:limit],
                "total": len(names),
                "truncated": truncated,
            },
            Confidence.API_SUCCESS,
        )


class ReadFileTool(BaseTool):
    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        filepath = (args.get("filepath") or "").strip()
        if not filepath:
            raise ClassifiedToolError("reasoning_failure", "no filepath provided")
        resolved = _check_read_path(filepath)
        if not resolved.exists():
            raise ClassifiedToolError(
                "state_failure", f"file does not exist: {resolved}"
            )
        if resolved.is_dir():
            raise ClassifiedToolError(
                "reasoning_failure",
                f"{resolved} is a folder — use list_files for directories.",
            )

        # Binary Office formats are NOT parsed here, unlike the external
        # server, which pulled in pypdf/python-docx/openpyxl/python-pptx/PIL
        # to do it. Those are five dependencies for a path the agent is told
        # not to take anyway: the foreground instruction routes Office
        # documents through their real application via windows-control, and a
        # silently-garbled text extraction is worse than an honest refusal.
        suffix = resolved.suffix.lower()
        if suffix in {".docx", ".xlsx", ".pptx", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".bmp"}:
            raise ClassifiedToolError(
                "reasoning_failure",
                f"{suffix} is a binary format this tool does not parse. Open it "
                "in its application via windows_open_app (foreground mode), or "
                "use perception tools to look at it.",
            )

        max_chars = int(load_devmcp_policy().get("read", {}).get("max_chars", 50000))
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ClassifiedToolError("tool_failure", f"could not read {resolved}: {exc}")

        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]
        if not text.strip():
            return ({"path": str(resolved), "content": "", "empty": True},
                    Confidence.API_SUCCESS)

        # Wrapped for the same reason browser and filesystem content is:
        # anything read off the machine is data, never instruction.
        wrapped = (
            f'<untrusted_local_content source="{resolved}">\n{text}\n'
            "</untrusted_local_content>"
        )
        return (
            {"path": str(resolved), "content": wrapped, "truncated": truncated},
            Confidence.API_SUCCESS,
        )


class WriteFileTool(BaseTool):
    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        filepath = (args.get("filepath") or "").strip()
        content = args.get("content")
        if not filepath:
            raise ClassifiedToolError("reasoning_failure", "no filepath provided")
        if not isinstance(content, str):
            raise ClassifiedToolError("reasoning_failure", "content must be a string")

        max_bytes = int(load_devmcp_policy().get("write", {}).get("max_bytes", 5_000_000))
        if len(content.encode("utf-8")) > max_bytes:
            raise ClassifiedToolError(
                "reasoning_failure",
                f"refusing to write more than {max_bytes:,} bytes in one call.",
            )

        resolved = _check_write_path(filepath)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        # Written to a temporary file and renamed, so a crash mid-write leaves
        # the previous content intact rather than a truncated file.
        tmp = resolved.with_suffix(resolved.suffix + ".orbit-tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, resolved)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise ClassifiedToolError("tool_failure", f"could not write {resolved}: {exc}")

        return (
            {"path": str(resolved), "bytes_written": len(content.encode("utf-8"))},
            Confidence.API_SUCCESS,
        )


class RunCommandTool(BaseTool):
    # Overridden because a real command (pip install, a python script) routinely
    # outlives BaseTool's 30s default, and the policy file owns the real number.
    default_timeout_s = 120.0

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        command = _strip_powershell_wrapper(args.get("command") or "")
        if not command:
            raise ClassifiedToolError("reasoning_failure", "no command provided")

        allowed, reason = check_command_allowed(command)
        if not allowed:
            raise ClassifiedToolError(
                "permission_denied",
                f"command refused by policy ({reason}). This is a hard block — "
                "surface it to the user rather than retrying or rewriting the "
                "command to get around it.",
            )

        policy = load_devmcp_policy().get("command", {})
        timeout = float(policy.get("timeout_s", 120))
        max_chars = int(policy.get("max_output_chars", 20000))

        proc = await asyncio.create_subprocess_exec(
            "powershell", "-ExecutionPolicy", "RemoteSigned",
            "-NonInteractive", "-NoProfile", "-Command", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # DEVNULL, never inherited. This process is an MCP stdio server:
            # its own stdin IS the JSON-RPC request pipe. A child that
            # inherits that handle interferes with the protocol stream and
            # wedges the event loop. The symptom is specific and misleading —
            # `python --version` returns fine (it exits before fully
            # initialising stdin) while `python -c ...` hangs until the
            # client's timeout, and the wait_for below never fires because the
            # loop itself is stuck. Carried over from the external server,
            # where it was found the hard way.
            stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()  # reap, or the zombie outlives the task
            raise ClassifiedToolError(
                "timeout", f"command exceeded {timeout}s and was killed.",
            )

        def _clip(raw: bytes) -> str:
            text = raw.decode(errors="replace")
            if len(text) > max_chars:
                return text[:max_chars] + f"\n... truncated at {max_chars} chars."
            return text

        stdout, stderr = _clip(out_b), _clip(err_b)
        combined = (stdout + stderr).strip()
        return (
            {
                "command": command,
                "exit_code": proc.returncode,
                # Output is machine content, so it carries the same untrusted
                # marker as a web page or a file.
                "output": (
                    f"<untrusted_local_content source=\"powershell\">\n{combined}\n"
                    "</untrusted_local_content>"
                    if combined else "(no output)"
                ),
            },
            Confidence.API_SUCCESS,
        )


def _metadata(name: str, description: str, **overrides) -> ToolMetadata:
    fields = dict(_MEDIUM_HEADLESS)
    fields.update(overrides)
    return ToolMetadata(name=name, description=description, **fields)


list_files_tool = ListFilesTool(
    _metadata("list_files", "List files in any folder on this machine.",
              returns_untrusted_content=True)
)
read_file_tool = ReadFileTool(
    _metadata("read_file", "Read a text file from anywhere on this machine.")
)
write_file_tool = WriteFileTool(
    _metadata("write_file", "Write a text file inside an allowed root.",
              is_destructive=True)
)
run_command_tool = RunCommandTool(
    _metadata("run_command", "Run a PowerShell command, subject to policy.",
              is_destructive=True)
)
