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
             "mos_review": 1000, "report": 6000}

# Who may own a finding: the main review audits everything but the MOS analysis (which does not exist yet); the
# one-time MOS audit records only issues in the MOS analysis.
REVIEW_OWNERS = {"review": ("moat", "management", "valuation", "report"), "mos_review": ("mos",)}
FRESHNESS_SLACK = 2.0  # seconds; filesystem mtime granularity


def _has_heading(text: str, name: str) -> bool:
    pattern = rf"^#{{1,4}}\s*(?:\d+[.)]\s*)?{re.escape(name)}\b"
    return re.search(pattern, text, re.IGNORECASE | re.MULTILINE) is not None


def validate(agent: str, paths: Paths, started: float, scores: dict[str, int] | None = None,
             unresolved: list | None = None, previous_ids: list[str] | None = None,
             mos_fixes: list | None = None) -> list[str]:
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
        elif agent in REVIEW_OWNERS:
            errs += _check_review(text, contracts.load_review(side), previous_ids or [], REVIEW_OWNERS[agent],
                                  paths.rel(out))
        elif agent == "report":
            rep = contracts.load_report(side)
            errs += _check_report(text, rep, scores or {}, unresolved or [], mos_fixes or [])
            errs += _check_same_price("report", text, rep.share_price, rep.price_date, paths)
    except (ValidationError, ValueError) as e:  # ValueError covers bad JSON
        errs.append(f"sidecar {paths.rel(side)} invalid: {str(e)[:600]}")
    return errs


# Mechanical consistency checks. They run as part of each agent's own validation, so a mismatch is fixed in the
# agent's session right away instead of costing a review pass and a correction round (or, for the MOS analysis,
# a fix in the report).

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


def _check_review(text: str, rev: contracts.ReviewSidecar, previous_ids: list[str], owners: tuple[str, ...],
                  rel: str) -> list[str]:
    errs = []
    missing = [i for i in previous_ids if not re.search(rf"(?<![A-Za-z0-9]){re.escape(i)}(?![A-Za-z0-9])", text)]
    if missing:
        errs.append(f"{rel} does not state the status (fixed or still open) of previous finding(s) {missing}")
    bad = [f.id for f in rev.findings if f.owner not in owners]
    if bad:
        errs.append(f"finding(s) {bad} have an owner this review may not assign; allowed owners: {list(owners)}")
    lines = text.strip().splitlines()
    summ = contracts.parse_summary_line(lines[-1]) if lines else None
    if summ is None:
        errs.append(f"the last line of {rel} must be 'Summary: H HIGH, M MEDIUM, L LOW'")
    else:
        c = rev.counts
        if summ != (c.high, c.medium, c.low):
            errs.append(f"summary line counts {summ} disagree with the sidecar findings, which contain "
                        f"{c.high} HIGH, {c.medium} MEDIUM, {c.low} LOW")
    for f in rev.findings:
        if not re.search(rf"(?<![A-Za-z0-9]){re.escape(f.id)}(?![A-Za-z0-9])", text):
            errs.append(f"finding id {f.id!r} from sidecar does not appear in {rel}")
    return errs


def _check_report(text: str, rep: contracts.ReportSidecar, scores: dict[str, int],
                  unresolved: list, mos_fixes: list) -> list[str]:
    """Scores are reported exactly as upstream, except that the MOS score may differ when an issue of the MOS
    audit (fixed in the report, since the MOS agent is never re-run) requires it; see _check_mos_score_change."""
    errs = [f"missing report section: '{h}'" for h in REPORT_HEADINGS if not _has_heading(text, h)]
    for agent, s in scores.items():
        got = rep.scores_reported.get(agent)
        if agent == "mos" and got != s and mos_fixes:
            errs += _check_mos_score_change(text, rep.mos_score_change, s, got, mos_fixes)
        elif got != s:
            errs.append(f"scores_reported[{agent!r}]={got} != upstream score {s}"
                        + (" (the MOS score may change only when an issue of the MOS audit requires it; this run's "
                           "audit sent none)" if agent == "mos" else ""))
    if rep.mos_score_change is not None and "mos" in scores and rep.scores_reported.get("mos") == scores["mos"]:
        errs.append("the sidecar sets mos_score_change, but scores_reported['mos'] equals the MOS analysis's score "
                    f"{scores['mos']}; set mos_score_change to null")
    if unresolved and not re.search(r"unresolved", text, re.IGNORECASE):
        errs.append("report must flag the unresolved HIGH- and/or MEDIUM-severity issues")
    return errs


def _check_mos_score_change(text: str, change: contracts.MosScoreChange | None, original: int, got: int | None,
                            mos_fixes: list) -> list[str]:
    """A changed MOS score must be recorded in the sidecar (from, to, the MOS audit issues requiring it, why) and
    explained in the report in one place that is about the margin of safety and states both scores. Merely having
    both numbers somewhere in the report (e.g. another analysis's score that happens to be equal) is not enough."""
    if got is None or not 1 <= got <= 10:
        return [f"scores_reported['mos'] must be the MOS score the report uses, 1-10 (got {got})"]
    if change is None:
        return [f"scores_reported['mos']={got} differs from the MOS analysis's score {original}: record the change "
                "in the sidecar as mos_score_change (original, corrected, finding_ids, reason)"]
    errs = []
    if (change.original, change.corrected) != (original, got):
        errs.append(f"mos_score_change records {change.original} -> {change.corrected}, but the MOS analysis's score "
                    f"is {original} and scores_reported['mos'] is {got}")
    allowed = [f.id for f in mos_fixes]
    unknown = [i for i in change.finding_ids if i not in allowed]
    if unknown:
        errs.append(f"mos_score_change.finding_ids {unknown} are not HIGH or MEDIUM issues of the MOS audit "
                    f"(those are {allowed})")
    if not _explains_mos_score_change(text, original, got):
        errs.append(f"the report must explain the MOS score change in one paragraph (or one score-table row) that "
                    f"refers to the margin of safety and states both scores, as {original}/10 (the margin of safety "
                    f"analysis's) and {got}/10 (the report's), with the reason")
    return errs


_MOS_TERM = re.compile(r"margin[\s-]+of[\s-]+safety|\bMOS\b", re.IGNORECASE)


def _explains_mos_score_change(text: str, old: int, new: int) -> bool:
    """Some single unit of the report (a paragraph, or one row of a table) refers to the margin of safety and states
    both scores as 'N/10' (or 'N out of 10')."""
    def states(unit: str, n: int) -> bool:
        return re.search(rf"(?<![\d.]){n}\s*(?:/\s*10|out of 10)\b", unit, re.IGNORECASE) is not None

    units: list[str] = []
    for para in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        prose: list[str] = []
        for line in para.splitlines():
            if line.lstrip().startswith("|"):
                units.append(line)   # a table row stands alone, so two rows' scores never combine
            else:
                prose.append(line)
        units.append("\n".join(prose))
    return any(_MOS_TERM.search(u) and states(u, old) and states(u, new) for u in units)


def saved_before_error(agent: str, paths: Paths, started: float, **kw) -> bool:
    """After a failed turn (timeout, transport error): True if the agent had already saved complete, valid outputs.
    The sidecar is written after the main file, so it must be the newer of the two (else the agent may have been
    still editing the main file when it stopped)."""
    side, out = paths.sidecar(agent), paths.output(agent)
    return (side.exists() and out.exists() and side.stat().st_mtime >= out.stat().st_mtime
            and not validate(agent, paths, started, **kw))


def check_fresh_inputs(paths: Paths, agent: str) -> list[str]:
    """Before review, mos, mos_review and report: no upstream file may be stale relative to the inputs it was built
    from."""
    return [f"stale output: {a}" for a in manifest.stale_agents(paths, DEPENDS[agent])]
