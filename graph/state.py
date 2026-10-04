"""Graph state. Reducers make parallel node updates merge deterministically."""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


def merge_dicts(a: dict, b: dict) -> dict:
    return {**(a or {}), **(b or {})}


class GraphState(TypedDict, total=False):
    company: str
    key: str
    run_id: str
    started_at: str
    data_pack: str                                   # Stage 0 outcome: data source note, or "unavailable (reason)"
    iteration: int                                   # total correction rounds completed (HIGH + MEDIUM)
    high_iteration: int                              # HIGH-severity correction rounds completed (cap: MAX_CORRECTIONS)
    medium_iteration: int                            # MEDIUM-only correction rounds completed (cap: MAX_MEDIUM_CORRECTIONS)
    findings: list[dict[str, Any]]                   # findings from the latest review
    finding_attempts: dict[str, int]                 # finding id -> correction rounds it was sent to its owner in
    unresolved_high: list[dict[str, Any]]            # set by flag_unresolved (the loop ended with a HIGH open)
    unresolved_medium: list[dict[str, Any]]          # set by flag_unresolved (no MEDIUM-only round follows an open
                                                     # HIGH) and by flag_unresolved_medium
    mos_findings: list[dict[str, Any]]               # findings of the one-time MOS audit (fixed by the report agent)
    reported_scores: dict[str, int]                  # scores as the report states them (MOS, business may differ)
    score_changes: dict[str, dict[str, Any]]         # "mos" / "business" -> the report's recorded change (validated)
    scores: Annotated[dict[str, int], merge_dicts]   # the analyses' own scores (business: financial quality)
    status: Annotated[dict[str, dict], merge_dicts]  # agent -> execution record
    history: Annotated[list[str], operator.add]      # routing / lifecycle events
