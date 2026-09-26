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
    high_iteration: int                               # HIGH-severity correction rounds completed (cap: MAX_CORRECTIONS)
    medium_iteration: int                             # MEDIUM-severity correction rounds completed (cap: MAX_MEDIUM_CORRECTIONS)
    findings: list[dict[str, Any]]                   # findings from the latest review
    unresolved_high: list[dict[str, Any]]            # set only by the HIGH fallback path
    unresolved_medium: list[dict[str, Any]]          # set by the MEDIUM fallback path, and recomputed
                                                      # (authoritatively) by report() to cover the case where the
                                                      # HIGH cap was hit and correct_medium never ran
    scores: Annotated[dict[str, int], merge_dicts]
    status: Annotated[dict[str, dict], merge_dicts]  # agent -> execution record
    history: Annotated[list[str], operator.add]      # routing / lifecycle events
