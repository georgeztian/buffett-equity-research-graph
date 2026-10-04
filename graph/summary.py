"""Workflow execution summary, built from state (never from model prose)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import ALL_AGENTS, Paths, resume_command


def usage_totals(agents: dict[str, dict]) -> dict:
    """Whole-run totals over every run of every agent (cost, time, tokens)."""
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
    status = state.get("status", {})
    # A report file can exist without being accepted (its node failed validation): only a completed report agent
    # makes it the final report, and only then are the MOS audit's HIGH/MEDIUM issues fixed in it (else pending).
    report_done = status.get("report", {}).get("state") == "complete" and report.exists()
    complete = error is None and report_done
    mos_to_fix = [f for f in state.get("mos_findings", []) if f.get("severity") in ("HIGH", "MEDIUM")]
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
        "data_pack": state.get("data_pack"),
        "agents": agents,
        "usage": usage_totals(agents),
        "high_correction_iterations": state.get("high_iteration", 0),
        "medium_correction_iterations": state.get("medium_iteration", 0),
        "total_correction_iterations": state.get("iteration", 0),
        "scores": state.get("scores", {}),
        "reported_scores": state.get("reported_scores", {}),
        "score_changes": state.get("score_changes", {}),
        "unresolved_high_issues": state.get("unresolved_high", []),
        "unresolved_medium_issues": state.get("unresolved_medium", []),
        "mos_audit_issues": state.get("mos_findings", []),
        "mos_audit_issues_fixed_in_report": mos_to_fix if report_done else [],
        "mos_audit_issues_pending_report": [] if report_done else mos_to_fix,
        "final_report": paths.rel(report) if report_done else None,
        "history": state.get("history", []),
    }


_SCORE_LABEL = {"business": "financial quality"}
_SCORE_SOURCE = {"mos": "MOS agent", "business": "business agent"}


def render_markdown(s: dict) -> str:
    pending = {f["id"] for f in s.get("mos_audit_issues_pending_report") or []}
    lines = [f"# Workflow Execution Summary: {s['company']}", "",
             f"- Status: **{s['workflow_status']}**", f"- Run id: `{s['run_id']}`",
             f"- HIGH correction rounds performed (each also corrects the open MEDIUM issues): "
             f"{s['high_correction_iterations']}",
             f"- MEDIUM-only correction rounds performed: {s['medium_correction_iterations']}",
             f"- MOS audit issues pending, to be fixed by the report agent (it has not completed): {len(pending)}"
             if pending else f"- MOS audit issues fixed in the report: {len(s['mos_audit_issues_fixed_in_report'])}",
             f"- Final report: {s['final_report'] or 'not produced'}"]
    dp = s.get("data_pack")
    if dp and dp.startswith("unavailable"):
        lines.append(f"- SEC data pack: **NOT USED** — {dp}. The agents researched all figures from primary sources "
                     "themselves, without the shared SEC numbers.")
    elif dp:
        lines.append(f"- SEC data pack: {dp}")
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
    lines += ["", "## Scores (1-10)", ""]
    reported, changes = s.get("reported_scores") or {}, s.get("score_changes") or {}
    for k, v in s["scores"].items():
        r = reported.get(k)
        line = f"- {_SCORE_LABEL.get(k, k)}: {v}"
        if r is not None and r != v:   # only MOS and financial quality can differ, with a validated recorded change
            line += f" ({_SCORE_SOURCE.get(k, k)}) -> {r} in the report"
            if k == "mos":
                line += ", after the MOS audit"
            if changes.get(k):
                line += f" (issue(s) {', '.join(changes[k]['finding_ids'])}: {changes[k]['reason']})"
        lines.append(line)
    for sev in ("HIGH", "MEDIUM"):
        issues = s[f"unresolved_{sev.lower()}_issues"]
        lines += ["", f"## Unresolved {sev}-severity issues", ""]
        lines += [f"- **[{f['id']}]** ({f['owner']}) {f['problem']} — required: {f['required_correction']}"
                  for f in issues] or ["None."]
    lines += ["", "## MOS audit issues (the MOS agent is not re-run; HIGH and MEDIUM are fixed in the report)", ""]
    if s.get("mos_audit_issues"):
        for f in s["mos_audit_issues"]:
            how = ("recorded only" if f["severity"] not in ("HIGH", "MEDIUM")
                   else "pending: to be fixed in the report" if f["id"] in pending else "fixed in the report")
            lines.append(f"- **[{f['id']}]** ({f['severity']}, {how}) {f['problem']} — required: "
                         f"{f['required_correction']}")
    elif s["agents"].get("mos_review", {}).get("state") == "complete":
        lines.append("None.")
    else:
        lines.append("The MOS audit has not run.")
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
