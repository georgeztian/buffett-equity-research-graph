"""Pure routing functions. Same state in -> same path out; no I/O, no LLM."""
from __future__ import annotations

from .config import ANALYSTS, MAX_CORRECTIONS
from .contracts import Finding, is_correctable


def correctable_high(findings: list[dict]) -> list[Finding]:
    return [f for f in (Finding.model_validate(x) for x in findings) if is_correctable(f)]


def report_owned_high(findings: list[dict]) -> list[Finding]:
    """HIGH findings only the report agent can address; forwarded to Stage 5, never loop back."""
    return [f for f in (Finding.model_validate(x) for x in findings)
            if f.severity == "HIGH" and f.owner == "report"]


def route_after_review(state: dict) -> str:
    """'correct' | 'flag_unresolved' | 'report'."""
    if not correctable_high(state.get("findings", [])):
        return "report"
    if state.get("iteration", 0) < MAX_CORRECTIONS:
        return "correct"
    return "flag_unresolved"


def plan_corrections(findings: list[dict]) -> dict[str, list[Finding]]:
    """owner agent -> its HIGH findings, in analyst order."""
    high = correctable_high(findings)
    return {a: [f for f in high if f.owner == a] for a in ANALYSTS if any(f.owner == a for f in high)}

