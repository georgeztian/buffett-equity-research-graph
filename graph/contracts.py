"""Structured sidecar schemas: the validated boundary between LLM content and graph control flow."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .config import ANALYSTS

Owner = Literal["moat", "management", "valuation", "mos", "report"]
Severity = Literal["HIGH", "MEDIUM", "LOW"]


class ScoreSidecar(BaseModel):
    score: int = Field(ge=1, le=10)
    summary: str = ""


class Finding(BaseModel):
    id: str
    problem: str
    evidence: str
    severity: Severity
    owner: Owner
    required_correction: str


class Counts(BaseModel):
    high: int = Field(ge=0)
    medium: int = Field(ge=0)
    low: int = Field(ge=0)


class ReviewSidecar(BaseModel):
    """Only `findings` is authoritative. Severity counts are derived from it in code (never asked of the model),
    so they cannot disagree with the findings; a `counts` key left in an older-style sidecar is ignored."""
    findings: list[Finding]

    @model_validator(mode="after")
    def _unique_ids(self) -> "ReviewSidecar":
        if len({f.id for f in self.findings}) != len(self.findings):
            raise ValueError("duplicate finding ids")
        return self

    @property
    def counts(self) -> Counts:
        return Counts(**{s.lower(): sum(f.severity == s for f in self.findings) for s in ("HIGH", "MEDIUM", "LOW")})


class ReportSidecar(BaseModel):
    financial_quality_score: int = Field(ge=1, le=10)
    scores_reported: dict[str, int]


SUMMARY_LINE = re.compile(
    r"(\d+)\s+HIGH\b[^0-9\n]{0,40}?(\d+)\s+MEDIUM\b[^0-9\n]{0,40}?(\d+)\s+LOW\b[^0-9\n]{0,80}?(\d+)\s+HIGH[^\n]*unresolved",
    re.IGNORECASE,
)


def parse_summary_line(text: str) -> tuple[int, int, int, int] | None:
    """Last 'H HIGH, M MEDIUM, L LOW; U HIGH unresolved' line in the review, if any."""
    found = SUMMARY_LINE.findall(text)
    return tuple(int(x) for x in found[-1]) if found else None  # type: ignore[return-value]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_score(path: Path) -> ScoreSidecar:
    return ScoreSidecar.model_validate(load_json(path))


def load_review(path: Path) -> ReviewSidecar:
    return ReviewSidecar.model_validate(load_json(path))


def load_report(path: Path) -> ReportSidecar:
    return ReportSidecar.model_validate(load_json(path))


def is_correctable(f: Finding, severity: Severity = "HIGH") -> bool:
    """A finding of the given severity the correction loop can act on (owned by an upstream analyst)."""
    return f.severity == severity and f.owner in ANALYSTS
