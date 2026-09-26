"""Agent execution boundary. Nodes depend on the AgentRunner protocol; SdkRunner is the real one."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncContextManager, AsyncIterator, Protocol

from .config import AGENT_MAX_BUFFER_BYTES, AGENT_TIMEOUT_SECONDS, ENV_FILE, ensure_inside

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


TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


@dataclass
class RunResult:
    """Outcome of one turn (one prompt sent to an agent session). Cost and tokens are this turn's own."""
    ok: bool
    error: str = ""
    cost_usd: float | None = None
    tokens: dict[str, int] = field(default_factory=dict)
    turns: int = 0          # model steps in this turn

    @property
    def usage_limit(self) -> bool:
        """True if the failure is a Claude usage/session limit: retrying cannot help until it resets."""
        return not self.ok and USAGE_LIMIT.search(self.error) is not None


class Usage:
    """Cost, wall time, model steps and tokens of one node run (all of its attempts)."""

    def __init__(self):
        self.t0 = time.time()
        self.cost = 0.0
        self.cost_known = False
        self.turns = 0
        self.tokens = dict.fromkeys(TOKEN_KEYS, 0)

    def add(self, res: RunResult) -> None:
        if res.cost_usd is not None:
            self.cost += res.cost_usd
            self.cost_known = True
        self.turns += res.turns
        for k, v in res.tokens.items():
            self.tokens[k] = self.tokens.get(k, 0) + v

    def record(self) -> dict:
        return {"cost_usd": round(self.cost, 4) if self.cost_known else None,
                "seconds": round(time.time() - self.t0, 1), "turns": self.turns, "tokens": dict(self.tokens)}

    def describe(self) -> str:
        """Progress-log text: elapsed time only (cost and tokens are recorded in the run summary, not the log)."""
        return f"{self.record()['seconds']:.0f}s"

    @staticmethod
    def accumulate(prior: dict, this: dict) -> dict:
        """Running totals over every run of an agent: the previous record's totals plus this run."""
        tokens = dict(prior.get("total_tokens") or {})
        for k, v in (this.get("tokens") or {}).items():
            tokens[k] = tokens.get(k, 0) + v
        return {"total_cost_usd": round((prior.get("total_cost_usd") or 0.0) + (this.get("cost_usd") or 0.0), 4),
                "total_seconds": round((prior.get("total_seconds") or 0.0) + (this.get("seconds") or 0.0), 1),
                "total_turns": (prior.get("total_turns") or 0) + (this.get("turns") or 0),
                "total_tokens": tokens}


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


class AgentSession(Protocol):
    async def send(self, prompt: str) -> RunResult: ...


class AgentRunner(Protocol):
    def session(self, task: AgentTask) -> "AsyncContextManager[AgentSession]": ...


class SdkSession:
    """One live Claude Code session. A follow-up `send` continues the same conversation, so a
    rejected output can be fixed without re-reading every input from scratch."""

    def __init__(self, client, timeout: float):
        self.client = client
        self.timeout = timeout
        self._cost_so_far = 0.0   # total_cost_usd is a running total for the session

    async def send(self, prompt: str) -> RunResult:
        from claude_agent_sdk import ResultMessage

        result = RunResult(ok=False, error="agent produced no result message")
        try:
            async with asyncio.timeout(self.timeout):   # runs in this task (the SDK client needs that)
                await self.client.query(prompt)
                async for msg in self.client.receive_response():
                    if isinstance(msg, ResultMessage):
                        total = msg.total_cost_usd
                        cost = None
                        if total is not None:
                            cost = total - self._cost_so_far if total >= self._cost_so_far else total
                            self._cost_so_far = total
                        usage = msg.usage or {}   # per turn in streaming mode
                        result = RunResult(ok=not msg.is_error,
                                           error=(msg.result or msg.subtype or "agent error") if msg.is_error else "",
                                           cost_usd=cost, turns=msg.num_turns or 0,
                                           tokens={k: int(usage.get(k) or 0) for k in TOKEN_KEYS})
        except TimeoutError:
            return RunResult(ok=False, error=f"agent timed out after {self.timeout:.0f}s")
        except Exception as e:  # SDK / CLI / transport failure
            return RunResult(ok=False, error=f"{type(e).__name__}: {e}")
        return result


class SdkRunner:
    """Runs agents through the Claude Agent SDK using the local Claude Code login."""

    def __init__(self, timeout: float = AGENT_TIMEOUT_SECONDS):
        self.timeout = timeout

    def _options(self, task: AgentTask):
        from claude_agent_sdk import ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny

        allowed = {ensure_inside(task.cwd, p).resolve() for p in task.allowed_writes}

        async def guard(tool: str, tool_input: dict, _ctx):
            if tool == "Read" and (task.cwd / tool_input.get("file_path", "")).resolve() == ENV_FILE.resolve():
                return PermissionResultDeny(message="the user's private settings file is not readable")
            if tool in READ_TOOLS:
                return PermissionResultAllow()
            if tool in WRITE_TOOLS:
                target = (task.cwd / tool_input.get("file_path", "")).resolve()   # only allowed files, all in-project
                if target in allowed:
                    return PermissionResultAllow()
                return PermissionResultDeny(message=f"{task.agent} may only write: "
                                                    f"{sorted(p.name for p in allowed)}")
            return PermissionResultDeny(message=f"tool {tool} is not permitted")

        return ClaudeAgentOptions(
            system_prompt=task.system_prompt,
            cwd=str(task.cwd),
            tools=READ_TOOLS + WRITE_TOOLS,
            can_use_tool=guard,   # requires streaming mode, which the client always uses
            setting_sources=[],   # do not inherit user/project settings, hooks or skills
            cli_path=find_claude_cli(),
            max_buffer_size=AGENT_MAX_BUFFER_BYTES,
            extra_args={"no-session-persistence": None},   # do not write session transcripts to disk
        )

    @asynccontextmanager
    async def session(self, task: AgentTask) -> AsyncIterator[AgentSession]:
        from claude_agent_sdk import ClaudeSDKClient

        client = ClaudeSDKClient(self._options(task))
        try:
            await client.connect()
        except Exception as e:   # surfaced as a failed turn so the caller's retry logic applies
            yield _FailedSession(f"{type(e).__name__}: {e}")
            return
        try:
            yield SdkSession(client, self.timeout)
        finally:
            try:
                async with asyncio.timeout(30):
                    await client.disconnect()
            except BaseException:   # never let cleanup mask the node's own outcome
                pass


class _FailedSession:
    def __init__(self, error: str):
        self.error = error

    async def send(self, prompt: str) -> RunResult:
        return RunResult(ok=False, error=self.error)
