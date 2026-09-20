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
    iteration: int                                   # correction rounds completed
    findings: list[dict[str, Any]]                   # findings from the latest review
    unresolved_high: list[dict[str, Any]]            # set only by the fallback path
    scores: Annotated[dict[str, int], merge_dicts]
    status: Annotated[dict[str, dict], merge_dicts]  # agent -> execution record
    history: Annotated[list[str], operator.add]      # routing / lifecycle events
