"""Agent execution boundary. Nodes depend on the AgentRunner protocol; SdkRunner is the real one."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import AGENT_MAX_BUFFER_BYTES, AGENT_TIMEOUT_SECONDS, ensure_inside

READ_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch"]
WRITE_TOOLS = ["Write", "Edit"]


@dataclass
class AgentTask:
    agent: str
    system_prompt: str
    prompt: str
    cwd: Path
    allowed_writes: tuple[Path, ...]   # the only files this agent may write


USAGE_LIMIT = re.compile(r"(hit|reached) your .{0,40}limit|usage limit|session limit|weekly limit", re.IGNORECASE)


@dataclass
class RunResult:
    ok: bool
    error: str = ""
    cost_usd: float | None = None

    @property
    def usage_limit(self) -> bool:
        """True if the failure is a Claude usage/session limit: retrying cannot help until it resets."""
        return not self.ok and USAGE_LIMIT.search(self.error) is not None


def find_claude_cli() -> str | None:
    """CLAUDE_CLI_PATH, else `claude` on PATH, else the newest VS Code extension's bundled binary.
    Returns None to let the SDK use its own bundled/default CLI."""
    env = os.environ.get("CLAUDE_CLI_PATH")
    if env and Path(env).exists():
        return env
    on_path = shutil.which("claude")
    if on_path and not on_path.lower().endswith(".cmd"):   # the SDK refuses .cmd shims on Windows
        return on_path
    ext = Path.home() / ".vscode" / "extensions"

    def version(p: Path) -> tuple[int, ...]:   # ".../anthropic.claude-code-2.1.278-win32-x64/resources/..."
        m = re.search(r"claude-code-(\d+(?:\.\d+)*)", p.parents[2].name)
        return tuple(int(x) for x in m.group(1).split(".")) if m else ()

    found = [p for p in ext.glob("anthropic.claude-code-*/resources/native-binary/claude*") if p.is_file()]
    return str(max(found, key=version)) if found else None


class AgentRunner(Protocol):
    async def run(self, task: AgentTask) -> RunResult: ...


class SdkRunner:
    """Runs one agent through the Claude Agent SDK using the local Claude Code login."""

    def __init__(self, timeout: float = AGENT_TIMEOUT_SECONDS):
        self.timeout = timeout

    async def run(self, task: AgentTask) -> RunResult:
        from claude_agent_sdk import (ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny,
                                      ResultMessage, query)

        allowed = {ensure_inside(task.cwd, p).resolve() for p in task.allowed_writes}

        async def guard(tool: str, tool_input: dict, _ctx):
            if tool in READ_TOOLS:
                return PermissionResultAllow()
            if tool in WRITE_TOOLS:
                target = (task.cwd / tool_input.get("file_path", "")).resolve()   # only allowed files, all in-project
                if target in allowed:
                    return PermissionResultAllow()
                return PermissionResultDeny(message=f"{task.agent} may only write: "
                                                    f"{sorted(p.name for p in allowed)}")
            return PermissionResultDeny(message=f"tool {tool} is not permitted")

        async def stream():  # can_use_tool requires streaming input mode
            yield {"type": "user", "message": {"role": "user", "content": task.prompt},
                   "parent_tool_use_id": None, "session_id": "default"}

        options = ClaudeAgentOptions(
            system_prompt=task.system_prompt,
            cwd=str(task.cwd),
            tools=READ_TOOLS + WRITE_TOOLS,
            can_use_tool=guard,
            setting_sources=[],   # do not inherit user/project settings, hooks or skills
            cli_path=find_claude_cli(),
            max_buffer_size=AGENT_MAX_BUFFER_BYTES,
            extra_args={"no-session-persistence": None},   # do not write session transcripts to disk
        )

        async def drain() -> RunResult:
            result = RunResult(ok=False, error="agent produced no result message")
            async for msg in query(prompt=stream(), options=options):
                if isinstance(msg, ResultMessage):
                    result = RunResult(ok=not msg.is_error,
                                       error=(msg.result or msg.subtype or "agent error") if msg.is_error else "",
                                       cost_usd=getattr(msg, "total_cost_usd", None))
            return result

        try:
            return await asyncio.wait_for(drain(), timeout=self.timeout)
        except asyncio.TimeoutError:
            return RunResult(ok=False, error=f"agent timed out after {self.timeout:.0f}s")
        except Exception as e:  # SDK / CLI / transport failure
            return RunResult(ok=False, error=f"{type(e).__name__}: {e}")
