"""Workflow execution summary, built from state (never from model prose)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import ALL_AGENTS, Paths, resume_command


def usage_totals(agents: dict[str, dict]) -> dict:
    """Whole-run totals over every run of every agent (Phase 0 measurement: cost, time, tokens)."""
    tokens: dict[str, int] = {}
    for rec in agents.values():
        for k, v in (rec.get("total_tokens") or {}).items():
            tokens[k] = tokens.get(k, 0) + v
    return {"cost_usd": round(sum(r.get("total_cost_usd") or 0.0 for r in agents.values()), 4),
            "agent_seconds": round(sum(r.get("total_seconds") or 0.0 for r in agents.values()), 1),
            "turns": sum(r.get("total_turns") or 0 for r in agents.values()),
            "tokens": tokens}


def _elapsed(started: str | None, finished: str) -> float | None:
    try:
        return round((datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds(), 1)
    except (TypeError, ValueError):
        return None


def build_summary(paths: Paths, state: dict, error: str | None = None, paused: bool = False) -> dict:
    report = paths.output("report")
    complete = error is None and report.exists()
    status = state.get("status", {})
    agents = {a: status.get(a, {"state": "not_run"}) for a in ALL_AGENTS}
    finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "company": state.get("company"),
        "key": paths.key,
        "run_id": state.get("run_id"),
        "started_at": state.get("started_at"),
        "finished_at": finished,
        "wall_seconds": _elapsed(state.get("started_at"), finished),   # includes any paused time between resumes
        "workflow_status": "COMPLETE" if complete else "PAUSED" if paused else "FAILED",
        "error": error,
        "agents": agents,
        "usage": usage_totals(agents),
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
        lines.append(f"- Paused by a Claude usage limit. After it resets, run: `{resume_command(s['run_id'])}`")
    lines += ["", "## Agent execution status", "",
              "| Agent | State | Runs | Attempts (last run) | Time, all runs | Est. cost, all runs | Output tokens | "
              "Input tokens (uncached / cache read / cache write) |",
              "|---|---|---|---|---|---|---|---|"]
    for a, rec in s["agents"].items():
        t = rec.get("total_tokens") or {}
        lines.append(f"| {a} | {rec.get('state')} | {rec.get('runs', 0)} | {rec.get('attempts', '-')} | "
                     f"{_mins(rec.get('total_seconds'))} | {_usd(rec.get('total_cost_usd'))} | "
                     f"{t.get('output_tokens', 0):,} | {t.get('input_tokens', 0):,} / "
                     f"{t.get('cache_read_input_tokens', 0):,} / {t.get('cache_creation_input_tokens', 0):,} |")
    u = s.get("usage") or {}
    ut = u.get("tokens") or {}
    lines += ["", f"Run totals: wall time {_mins(s.get('wall_seconds'))} (including any pause), agent time "
                  f"{_mins(u.get('agent_seconds'))}, estimated cost {_usd(u.get('cost_usd'))} (SDK client-side "
                  f"estimate), {ut.get('output_tokens', 0):,} output tokens, "
                  f"{ut.get('input_tokens', 0) + ut.get('cache_read_input_tokens', 0) + ut.get('cache_creation_input_tokens', 0):,} "
                  "input tokens."]
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


def _mins(sec) -> str:
    return "-" if sec is None else f"{sec / 60:.1f} min"


def _usd(x) -> str:
    return "-" if x is None else f"${x:.2f}"


def write_summary(paths: Paths, state: dict, error: str | None = None, paused: bool = False) -> dict:
    s = build_summary(paths, state, error, paused)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    (paths.reports_dir / "run_summary.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
    (paths.reports_dir / "run_summary.md").write_text(render_markdown(s), encoding="utf-8")
    return s
