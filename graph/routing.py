"""Pure routing functions. Same state in -> same path out; no I/O, no LLM."""
from __future__ import annotations

from .config import CORRECTABLE, MAX_CORRECTIONS, MAX_FINDING_ATTEMPTS, MAX_MEDIUM_CORRECTIONS
from .contracts import Finding, Severity, is_correctable


def correctable(findings: list[dict], severity: Severity) -> list[Finding]:
    return [f for f in (Finding.model_validate(x) for x in findings) if is_correctable(f, severity)]


def actionable(findings: list[dict], severity: Severity, attempts: dict[str, int]) -> list[Finding]:
    """Correctable findings not yet sent back MAX_FINDING_ATTEMPTS times. One its owner could not fix in that many
    rounds is not sent again: it stays open and goes to the report flagged as unresolved."""
    return [f for f in correctable(findings, severity) if attempts.get(f.id, 0) < MAX_FINDING_ATTEMPTS]


def report_owned(findings: list[dict], severity: Severity) -> list[Finding]:
    """Findings of the given severity only the report agent can address; forwarded to the report, never loop back."""
    return [f for f in (Finding.model_validate(x) for x in findings)
            if f.severity == severity and f.owner == "report"]


def route_after_review(state: dict) -> str:
    """'correct' | 'flag_unresolved' | 'correct_medium' | 'flag_unresolved_medium' | 'mos'.

    HIGH is checked, and if exhausted (MAX_CORRECTIONS rounds, or every open HIGH finding already sent back
    MAX_FINDING_ATTEMPTS times) flagged and the loop ends, before a MEDIUM-only round is ever considered: a
    HIGH-severity issue takes priority, and a run that couldn't fix its HIGH issues should not spend further rounds
    polishing MEDIUM ones. (Open MEDIUM findings still travel with every HIGH round: see plan_corrections.) When
    the loop ends, the MOS agent runs (once) on the final upstream analyses.
    """
    findings, attempts = state.get("findings", []), state.get("finding_attempts") or {}
    if correctable(findings, "HIGH"):
        go = actionable(findings, "HIGH", attempts) and state.get("high_iteration", 0) < MAX_CORRECTIONS
        return "correct" if go else "flag_unresolved"
    if correctable(findings, "MEDIUM"):
        go = actionable(findings, "MEDIUM", attempts) and state.get("medium_iteration", 0) < MAX_MEDIUM_CORRECTIONS
        return "correct_medium" if go else "flag_unresolved_medium"
    return "mos"


def plan_corrections(findings: list[dict], severity: Severity,
                     attempts: dict[str, int] | None = None) -> dict[str, list[Finding]]:
    """owner agent -> its actionable findings for this round, in analyst order. A HIGH round also carries every
    actionable MEDIUM finding (after each owner's HIGH ones), including those of owners with no HIGH finding: all
    owners are corrected in parallel in one round instead of MEDIUM issues waiting for a round of their own, which
    would cost an extra correction run and re-review. MEDIUM rounds handle only what is still open once no
    correctable HIGH finding remains; the MEDIUM round cap counts only those rounds (per-finding attempts count
    in every round)."""
    attempts = attempts or {}
    sel = actionable(findings, severity, attempts)
    if severity == "HIGH":
        sel += actionable(findings, "MEDIUM", attempts)
    return {a: [f for f in sel if f.owner == a] for a in CORRECTABLE if any(f.owner == a for f in sel)}
