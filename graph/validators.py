"""Deterministic completeness checks, run after every agent call.

An agent is complete only if its output file is fresh, has the required content, and its
structured sidecar validates (CLAUDE.md "Criteria for complete").
"""
from __future__ import annotations

import re
from datetime import date

from pydantic import ValidationError

from . import contracts, manifest
from .config import (ANALYSTS, BUSINESS_HEADINGS, DEPENDS, IV_TOLERANCE, MOS_PCT_TOLERANCE, PRICE_MAX_AGE_DAYS,
                     REPORT_HEADINGS, SOURCE_TAGS, Paths)

MIN_CHARS = {"moat": 2500, "management": 2500, "valuation": 3000, "business": 3000, "mos": 1500, "review": 1500,
             "report": 6000}
FRESHNESS_SLACK = 2.0  # seconds; filesystem mtime granularity


def _has_heading(text: str, name: str) -> bool:
    pattern = rf"^#{{1,4}}\s*(?:\d+[.)]\s*)?{re.escape(name)}\b"
    return re.search(pattern, text, re.IGNORECASE | re.MULTILINE) is not None


def validate(agent: str, paths: Paths, started: float, scores: dict[str, int] | None = None,
             unresolved: list | None = None, previous_ids: list[str] | None = None) -> list[str]:
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
        if agent in ANALYSTS or agent == "business":
            sc = contracts.load_score(side)
            found = [t for t in SOURCE_TAGS if re.search(rf"\b{t}\b", text)]
            if len(found) < 2:
                errs.append(f"output must label statements with {SOURCE_TAGS}; found only {found}")
            if not re.search(r"score", text, re.IGNORECASE):
                errs.append("output does not state the required 1-10 score")
            errs += _check_score_matches(text, sc.score, paths.rel(out))
            if agent == "business":
                errs += [f"missing section: '{h}'" for h in BUSINESS_HEADINGS if not _has_heading(text, h)]
            elif agent == "valuation":
                errs += _check_valuation(text, contracts.load_valuation(side), paths)
            elif agent == "mos":
                errs += _check_mos(text, contracts.load_mos(side), paths)
        elif agent == "review":
            errs += _check_review(text, contracts.load_review(side), previous_ids or [])
        elif agent == "report":
            rep = contracts.load_report(side)
            errs += _check_report(text, rep, scores or {}, unresolved or [])
            errs += _check_same_price("report", text, rep.share_price, rep.price_date, paths)
    except (ValidationError, ValueError) as e:  # ValueError covers bad JSON
        errs.append(f"sidecar {paths.rel(side)} invalid: {str(e)[:600]}")
    return errs


# Mechanical consistency checks. They run as part of each agent's own validation, so a mismatch is fixed in the
# agent's session right away instead of costing a review pass and a correction round.

_SCORE_STATED = (re.compile(r"\bscore\b[^\n]{0,60}?(?<![\d.])(\d{1,2})\s*(?:/\s*10|out of 10)\b", re.IGNORECASE),
                 re.compile(r"\bscore\b[\s*_:|\-]{1,8}(\d{1,2})(?![\d.]|\s*[-–]\s*\d)", re.IGNORECASE))  # not "1-10"


def _check_score_matches(text: str, score: int, rel: str) -> list[str]:
    """The sidecar score must be one the markdown states. Files may mention other scores (the MOS analysis cites
    the upstream ones), so this only fails when a score is stated and none of the stated ones is the sidecar's."""
    stated = {int(n) for p in _SCORE_STATED for n in p.findall(text)}
    if stated and score not in stated:
        return [f"the sidecar score {score} is not a score stated in {rel} (stated: {sorted(stated)}); "
                "make them agree"]
    return []


def _price_in_text(text: str, price: float) -> bool:
    forms = {f"{price:,.2f}", f"{price:.2f}", f"{price:,.2f}".rstrip("0").rstrip("."),
             f"{price:.2f}".rstrip("0").rstrip(".")}
    # not followed by more digits, even after a separator: 150 must not match "150.75" or "150,000"
    return any(re.search(rf"(?<![\d.,]){re.escape(f)}(?![\d]|[.,]\d)", text) for f in forms)


def _check_valuation(text: str, v: contracts.ValuationSidecar, paths: Paths) -> list[str]:
    errs = []
    age = (date.today() - v.price_date).days
    if age < 0:
        errs.append(f"sidecar price_date {v.price_date} is in the future")
    elif age > PRICE_MAX_AGE_DAYS:
        errs.append(f"the share price is dated {v.price_date}, {age} days old (limit {PRICE_MAX_AGE_DAYS}); "
                    "use a current price with its source and date")
    if not _price_in_text(text, v.share_price):
        errs.append(f"the sidecar share_price {v.share_price} does not appear in {paths.rel(paths.output('valuation'))}")
    return errs


def _load_valuation(paths: Paths) -> tuple[contracts.ValuationSidecar | None, list[str]]:
    try:
        return contracts.load_valuation(paths.sidecar("valuation")), []
    except (OSError, ValidationError, ValueError) as e:
        return None, [f"cannot check against the valuation sidecar: {str(e)[:200]}"]


def _check_same_price(agent: str, text: str, price: float, price_date: date, paths: Paths) -> list[str]:
    """The run has one reference share price, set by the valuation: `agent` must state exactly it and its date."""
    v, errs = _load_valuation(paths)
    if v is None:
        return errs
    if abs(price - v.share_price) > 0.005 or price_date != v.price_date:
        errs.append(f"share price {price} ({price_date}) differs from the valuation's "
                    f"{v.share_price} ({v.price_date}); use exactly the valuation's price and date")
    if not _price_in_text(text, v.share_price):
        errs.append(f"the valuation's share price {v.share_price} does not appear in "
                    f"{paths.rel(paths.output(agent))}")
    return errs


def _check_mos(text: str, m: contracts.MosSidecar, paths: Paths) -> list[str]:
    """The MOS analysis must measure against the valuation's price, date and intrinsic value, with its stated
    margin of safety computed from them."""
    errs = _check_same_price("mos", text, m.share_price, m.price_date, paths)
    v, _ = _load_valuation(paths)
    if v is None:
        return errs
    iv = v.intrinsic_value_per_share
    if not iv.low * (1 - IV_TOLERANCE) <= m.intrinsic_value_per_share <= iv.high * (1 + IV_TOLERANCE):
        errs.append(f"intrinsic_value_per_share {m.intrinsic_value_per_share} is outside the valuation's range "
                    f"{iv.low}-{iv.high}; measure against the valuation's estimate, do not redo it")
    implied = (m.intrinsic_value_per_share - m.share_price) / m.intrinsic_value_per_share * 100
    if abs(implied - m.margin_of_safety_pct) > MOS_PCT_TOLERANCE:
        errs.append(f"margin_of_safety_pct {m.margin_of_safety_pct} does not match (intrinsic value - price) / "
                    f"intrinsic value = {implied:.1f}%")
    return errs


def _check_review(text: str, rev: contracts.ReviewSidecar, previous_ids: list[str] = ()) -> list[str]:
    errs = []
    missing = [i for i in previous_ids if not re.search(rf"(?<![A-Za-z0-9]){re.escape(i)}(?![A-Za-z0-9])", text)]
    if missing:
        errs.append(f"review.md does not state the status (fixed or still open) of previous finding(s) {missing}")
    lines = text.strip().splitlines()
    summ = contracts.parse_summary_line(lines[-1]) if lines else None
    if summ is None:
        errs.append("the last line of review.md must be 'Summary: H HIGH, M MEDIUM, L LOW'")
    else:
        c = rev.counts
        if summ != (c.high, c.medium, c.low):
            errs.append(f"summary line counts {summ} disagree with the sidecar findings, which contain "
                        f"{c.high} HIGH, {c.medium} MEDIUM, {c.low} LOW")
    for f in rev.findings:
        if not re.search(rf"(?<![A-Za-z0-9]){re.escape(f.id)}(?![A-Za-z0-9])", text):
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


def saved_before_error(agent: str, paths: Paths, started: float, **kw) -> bool:
    """After a failed turn (timeout, transport error): True if the agent had already saved complete, valid outputs.
    The sidecar is written after the main file, so it must be the newer of the two (else the agent may have been
    still editing the main file when it stopped)."""
    side, out = paths.sidecar(agent), paths.output(agent)
    return (side.exists() and out.exists() and side.stat().st_mtime >= out.stat().st_mtime
            and not validate(agent, paths, started, **kw))


def check_fresh_inputs(paths: Paths, agent: str) -> list[str]:
    """Before review/report: no upstream file may be stale relative to the inputs it was built from."""
    return [f"stale output: {a}" for a in manifest.stale_agents(paths, DEPENDS[agent])]
