"""Deterministic completeness checks, run after every agent call.

An agent is complete only if its output file is fresh, has the required content, and its
structured sidecar validates (CLAUDE.md "Criteria for complete").
"""
from __future__ import annotations

import re

from pydantic import ValidationError

from . import contracts, manifest
from .config import ANALYSTS, DEPENDS, REPORT_HEADINGS, SOURCE_TAGS, Paths

MIN_CHARS = {"moat": 2500, "management": 2500, "valuation": 3000, "mos": 1500, "review": 1500, "report": 6000}
FRESHNESS_SLACK = 2.0  # seconds; filesystem mtime granularity


def _has_heading(text: str, name: str) -> bool:
    pattern = rf"^#{{1,4}}\s*(?:\d+[.)]\s*)?{re.escape(name)}\b"
    return re.search(pattern, text, re.IGNORECASE | re.MULTILINE) is not None


def validate(agent: str, paths: Paths, started: float, scores: dict[str, int] | None = None,
             unresolved: list | None = None) -> list[str]:
    """Return a list of human-readable problems; empty means the agent's output is complete."""
    errs: list[str] = []
    out = paths.output(agent)

    if not out.exists():
        return [f"output file not written: {paths.rel(out)}"]
    if out.stat().st_mtime < started - FRESHNESS_SLACK:
        errs.append(f"{paths.rel(out)} was not updated by this run (stale file)")
    text = out.read_text(encoding="utf-8", errors="replace")
    if len(text) < MIN_CHARS[agent]:
        errs.append(f"{paths.rel(out)} is too short ({len(text)} chars < {MIN_CHARS[agent]})")

    side = paths.sidecar(agent)
    if not side.exists():
        return errs + [f"sidecar not written: {paths.rel(side)}"]
    if side.stat().st_mtime < started - FRESHNESS_SLACK:
        errs.append(f"{paths.rel(side)} was not updated by this run (stale file)")

    try:
        if agent in ANALYSTS:
            contracts.load_score(side)
            found = [t for t in SOURCE_TAGS if re.search(rf"\b{t}\b", text)]
            if len(found) < 2:
                errs.append(f"output must label statements with {SOURCE_TAGS}; found only {found}")
            if not re.search(r"score", text, re.IGNORECASE):
                errs.append("output does not state the required 1-10 score")
        elif agent == "review":
            errs += _check_review(text, contracts.load_review(side))
        elif agent == "report":
            errs += _check_report(text, contracts.load_report(side), scores or {}, unresolved or [])
    except (ValidationError, ValueError) as e:  # ValueError covers bad JSON
        errs.append(f"sidecar {paths.rel(side)} invalid: {str(e)[:600]}")
    return errs


def _check_review(text: str, rev: contracts.ReviewSidecar) -> list[str]:
    errs = []
    summ = contracts.parse_summary_line(text)
    if summ is None:
        errs.append("review.md must end with 'H HIGH, M MEDIUM, L LOW; U HIGH unresolved' summary line")
    else:
        c = rev.counts
        if summ[:3] != (c.high, c.medium, c.low):
            errs.append(f"summary line counts {summ[:3]} disagree with sidecar {(c.high, c.medium, c.low)}")
    for f in rev.findings:
        if f.id not in text:
            errs.append(f"finding id {f.id!r} from sidecar does not appear in review.md")
    return errs


def _check_report(text: str, rep: contracts.ReportSidecar, scores: dict[str, int],
                  unresolved: list) -> list[str]:
    errs = [f"missing report section: '{h}'" for h in REPORT_HEADINGS if not _has_heading(text, h)]
    for agent, s in scores.items():
        if rep.scores_reported.get(agent) != s:
            errs.append(f"scores_reported[{agent!r}]={rep.scores_reported.get(agent)} != upstream score {s}")
    if unresolved and not re.search(r"unresolved", text, re.IGNORECASE):
        errs.append("report must flag the unresolved HIGH- and/or MEDIUM-severity issues")
    return errs


def check_fresh_inputs(paths: Paths, agent: str) -> list[str]:
    """Before review/report: no upstream file may be stale relative to the inputs it was built from."""
    return [f"stale output: {a}" for a in manifest.stale_agents(paths, DEPENDS[agent])]
