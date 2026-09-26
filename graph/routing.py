"""Pure routing functions. Same state in -> same path out; no I/O, no LLM."""
from __future__ import annotations

from .config import ANALYSTS, MAX_CORRECTIONS, MAX_MEDIUM_CORRECTIONS
from .contracts import Finding, Severity, is_correctable


def correctable(findings: list[dict], severity: Severity = "HIGH") -> list[Finding]:
    return [f for f in (Finding.model_validate(x) for x in findings) if is_correctable(f, severity)]


def report_owned(findings: list[dict], severity: Severity = "HIGH") -> list[Finding]:
    """Findings of the given severity only the report agent can address; forwarded to Stage 5, never loop back."""
    return [f for f in (Finding.model_validate(x) for x in findings)
            if f.severity == severity and f.owner == "report"]


def route_after_review(state: dict) -> str:
    """'correct' | 'flag_unresolved' | 'correct_medium' | 'flag_unresolved_medium' | 'report'.

    HIGH is checked, and if exhausted (MAX_CORRECTIONS rounds) flagged and sent straight to report, before MEDIUM is
    ever considered: a HIGH-severity issue takes priority, and a run that couldn't fix its HIGH issues in
    that many rounds should not spend further rounds polishing MEDIUM ones.
    """
    if correctable(state.get("findings", []), "HIGH"):
        return "correct" if state.get("high_iteration", 0) < MAX_CORRECTIONS else "flag_unresolved"
    if correctable(state.get("findings", []), "MEDIUM"):
        return "correct_medium" if state.get("medium_iteration", 0) < MAX_MEDIUM_CORRECTIONS else "flag_unresolved_medium"
    return "report"


def plan_corrections(findings: list[dict], severity: Severity = "HIGH") -> dict[str, list[Finding]]:
    """owner agent -> its findings of the given severity, in analyst order."""
    sel = correctable(findings, severity)
    return {a: [f for f in sel if f.owner == a] for a in ANALYSTS if any(f.owner == a for f in sel)}
