"""Content-hash manifest: records which input versions each output was built from."""
from __future__ import annotations

import difflib
import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import DEPENDS, REVIEW_DIFF_MAX_CHARS, Paths


def sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def input_hashes(paths: Paths, agent: str) -> dict[str, str | None]:
    """The input versions an output is built from: the content hashes of the files it reads."""
    return {d: sha(paths.output(d)) for d in DEPENDS[agent]}


def _reviewed_copy(paths: Paths, agent: str) -> Path:
    return paths.meta_dir / "reviewed" / paths.output(agent).name


def snapshot_reviewed(paths: Paths) -> None:
    """Keep a copy of every file the reviewer just audited, so the next re-review can be limited to what changed."""
    for a in DEPENDS["review"]:
        dest = _reviewed_copy(paths, a)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(paths.output(a), dest)


@dataclass
class Change:
    rel: str                 # project-relative path of the file
    diff: str | None         # unified diff; None if unchanged, or if too long to be useful (then: re-read in full)
    changed: bool


def reviewed_changes(paths: Paths) -> list[Change] | None:
    """How each file the reviewer reads changed since the last review, or None if there is no complete snapshot
    (then the re-review audits everything)."""
    out = []
    for a in DEPENDS["review"]:
        old_p, new_p = _reviewed_copy(paths, a), paths.output(a)
        if not old_p.exists() or not new_p.exists():
            return None
        old = old_p.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
        new = new_p.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
        if old == new:
            out.append(Change(paths.rel(new_p), None, False))
            continue
        diff = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), f"{new_p.name} (as last reviewed)",
                                              f"{new_p.name} (now)", n=2, lineterm=""))
        out.append(Change(paths.rel(new_p), diff if len(diff) <= REVIEW_DIFF_MAX_CHARS else None, True))
    return out


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
    return rec if _current(paths, agent, rec) and rec.get("round") == rnd else None


def stale_agents(paths: Paths, agents: tuple[str, ...]) -> list[str]:
    """Agents whose recorded inputs differ from the current upstream files (or that never ran)."""
    m = load(paths)
    return [a for a in agents if not _current(paths, a, m.get(a))]


def _current(paths: Paths, agent: str, rec: dict | None) -> bool:
    """The record exists and matches the current output file and the current versions of its inputs."""
    return (rec is not None and rec["inputs"] == input_hashes(paths, agent)
            and rec["output"] == sha(paths.output(agent)))
