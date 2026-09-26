"""Content-hash manifest: records which input versions each output was built from."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .config import DEPENDS, Paths


def sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def input_hashes(paths: Paths, agent: str) -> dict[str, str | None]:
    return {d: sha(paths.output(d)) for d in DEPENDS[agent]}


def load(paths: Paths) -> dict:
    if paths.manifest.exists():
        return json.loads(paths.manifest.read_text(encoding="utf-8"))
    return {}


def record(paths: Paths, agent: str, inputs: dict[str, str | None], rnd: int = 0, attempts: int = 1,
           usage: dict | None = None) -> None:
    """`usage` is what the run cost, so a resume that skips the agent still accounts for it."""
    m = load(paths)
    m[agent] = {"output": sha(paths.output(agent)), "inputs": inputs, "round": rnd, "attempts": attempts,
                "usage": usage}
    paths.meta_dir.mkdir(parents=True, exist_ok=True)
    paths.manifest.write_text(json.dumps(m, indent=2), encoding="utf-8")


def completed_in_round(paths: Paths, agent: str, rnd: int) -> dict | None:
    """The manifest record if `agent` already finished round `rnd` (0 = before any correction) on the current
    inputs. Lets a resumed run skip agents that completed before their node failed. A fresh run archives the
    manifest, so every record is from the current run."""
    rec = load(paths).get(agent)
    current = rec and rec["inputs"] == input_hashes(paths, agent) and rec["output"] == sha(paths.output(agent))
    return rec if current and rec.get("round") == rnd else None


def stale_agents(paths: Paths, agents: tuple[str, ...]) -> list[str]:
    """Agents whose recorded inputs differ from the current upstream files (or that never ran)."""
    m = load(paths)
    out = []
    for a in agents:
        rec = m.get(a)
        if rec is None or rec["inputs"] != input_hashes(paths, a) or rec["output"] != sha(paths.output(a)):
            out.append(a)
    return out
