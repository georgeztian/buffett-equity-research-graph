"""Workflow execution summary, built from state (never from model prose)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import ALL_AGENTS, Paths


def build_summary(paths: Paths, state: dict, error: str | None = None, paused: bool = False) -> dict:
    report = paths.output("report")
    complete = error is None and report.exists()
    status = state.get("status", {})
    return {
        "company": state.get("company"),
        "key": paths.key,
        "run_id": state.get("run_id"),
        "started_at": state.get("started_at"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "workflow_status": "COMPLETE" if complete else "PAUSED" if paused else "FAILED",
        "error": error,
        "agents": {a: status.get(a, {"state": "not_run"}) for a in ALL_AGENTS},
        "correction_iterations": state.get("high_iteration", 0),         # kept: HIGH rounds, as before this field existed
        "high_correction_iterations": state.get("high_iteration", 0),
        "medium_correction_iterations": state.get("medium_iteration", 0),
        "total_correction_iterations": state.get("iteration", 0),
        "scores": state.get("scores", {}),
        "unresolved_high_issues": state.get("unresolved_high", []),
        "unresolved_medium_issues": state.get("unresolved_medium", []),
        "final_report": paths.rel(report) if report.exists() else None,
        "history": state.get("history", []),
    }


def render_markdown(s: dict) -> str:
    lines = [f"# Workflow Execution Summary: {s['company']}", "",
             f"- Status: **{s['workflow_status']}**", f"- Run id: `{s['run_id']}`",
             f"- HIGH correction rounds performed: {s['high_correction_iterations']}",
             f"- MEDIUM correction rounds performed: {s['medium_correction_iterations']}",
             f"- Final report: {s['final_report'] or 'not produced'}"]
    if s["error"]:
        lines.append(f"- Error: {s['error']}")
    if s["workflow_status"] == "PAUSED":
        lines.append(f"- Paused by a Claude usage limit. After it resets, run: `python -m graph --resume {s['run_id']}`")
    lines += ["", "## Agent execution status", "", "| Agent | State | Runs | Attempts (last run) |", "|---|---|---|---|"]
    for a, rec in s["agents"].items():
        lines.append(f"| {a} | {rec.get('state')} | {rec.get('runs', 0)} | {rec.get('attempts', '-')} |")
    lines += ["", "## Scores (1-10)", ""] + [f"- {k}: {v}" for k, v in s["scores"].items()]
    lines += ["", "## Unresolved HIGH-severity issues", ""]
    if s["unresolved_high_issues"]:
        for f in s["unresolved_high_issues"]:
            lines.append(f"- **[{f['id']}]** ({f['owner']}) {f['problem']} — required: {f['required_correction']}")
    else:
        lines.append("None.")
    lines += ["", "## Unresolved MEDIUM-severity issues", ""]
    if s["unresolved_medium_issues"]:
        for f in s["unresolved_medium_issues"]:
            lines.append(f"- **[{f['id']}]** ({f['owner']}) {f['problem']} — required: {f['required_correction']}")
    else:
        lines.append("None.")
    lines += ["", "## Execution history", ""] + [f"1. {h}" for h in s["history"]]
    return "\n".join(lines) + "\n"


def write_summary(paths: Paths, state: dict, error: str | None = None, paused: bool = False) -> dict:
    s = build_summary(paths, state, error, paused)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    (paths.reports_dir / "run_summary.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
    (paths.reports_dir / "run_summary.md").write_text(render_markdown(s), encoding="utf-8")
    return s
