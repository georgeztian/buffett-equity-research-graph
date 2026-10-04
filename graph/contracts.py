"""Structured sidecar schemas: the validated boundary between LLM content and graph control flow."""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .config import CORRECTABLE

# `mos` owns only findings of the MOS audit (mos_review); they are fixed by the report agent, never sent back.
Owner = Literal["moat", "management", "valuation", "mos", "report"]
Severity = Literal["HIGH", "MEDIUM", "LOW"]


class ScoreSidecar(BaseModel):
    score: int = Field(ge=1, le=10)
    summary: str = ""


class IntrinsicValueRange(BaseModel):
    low: float = Field(gt=0)
    base: float = Field(gt=0)
    high: float = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "IntrinsicValueRange":
        if not self.low <= self.base <= self.high:
            raise ValueError(f"intrinsic value range must satisfy low <= base <= high, got "
                             f"{self.low} / {self.base} / {self.high}")
        return self


class ValuationSidecar(ScoreSidecar):
    """Key figures stated in valuation.md, so the workflow can check them (and the MOS analysis and report against
    them) mechanically."""
    share_price: float = Field(gt=0)
    price_date: date
    intrinsic_value_per_share: IntrinsicValueRange


class MosSidecar(ScoreSidecar):
    share_price: float = Field(gt=0)
    price_date: date
    intrinsic_value_per_share: float = Field(gt=0)   # the valuation's estimate this analysis measures against
    margin_of_safety_pct: float                        # (intrinsic value - price) / intrinsic value * 100


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
    so they cannot disagree with the findings."""
    findings: list[Finding]

    @model_validator(mode="after")
    def _unique_ids(self) -> "ReviewSidecar":
        if len({f.id for f in self.findings}) != len(self.findings):
            raise ValueError("duplicate finding ids")
        return self

    @property
    def counts(self) -> Counts:
        return Counts(**{s.lower(): sum(f.severity == s for f in self.findings) for s in ("HIGH", "MEDIUM", "LOW")})


class ScoreChange(BaseModel):
    """Why the report uses a different score than the analysis it comes from. Allowed only for the two analyses that
    are never re-run to correct their own score: the MOS analysis (when issues of the MOS audit require it) and the
    business analysis's financial quality score (when issues owned by the report require it)."""
    original: int = Field(ge=1, le=10)            # the analysis's score
    corrected: int = Field(ge=1, le=10)           # the score the report uses
    finding_ids: list[str] = Field(min_length=1)  # the finding(s) that require the change
    reason: str = Field(min_length=20)


class ReportSidecar(BaseModel):
    financial_quality_score: int = Field(ge=1, le=10)
    scores_reported: dict[str, int]
    share_price: float = Field(gt=0)   # the report's share price reference: must be the valuation's
    price_date: date
    mos_score_change: ScoreChange | None = None           # set only when scores_reported["mos"] differs from the MOS's
    financial_quality_change: ScoreChange | None = None   # set only when financial_quality_score differs from business's


SUMMARY_LINE = re.compile(
    r"^\W*Summary:[\s*_]*(\d+)[*_]*\s+HIGH\b[^0-9\n]{0,40}?(\d+)[*_]*\s+MEDIUM\b[^0-9\n]{0,40}?(\d+)[*_]*\s+LOW\b",
    re.IGNORECASE | re.MULTILINE,
)


def parse_summary_line(text: str) -> tuple[int, int, int] | None:
    """Last 'Summary: H HIGH, M MEDIUM, L LOW' line in the review, if any."""
    found = SUMMARY_LINE.findall(text)
    return tuple(int(x) for x in found[-1]) if found else None  # type: ignore[return-value]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_score(path: Path) -> ScoreSidecar:
    return ScoreSidecar.model_validate(load_json(path))


def load_valuation(path: Path) -> ValuationSidecar:
    return ValuationSidecar.model_validate(load_json(path))


def load_mos(path: Path) -> MosSidecar:
    return MosSidecar.model_validate(load_json(path))


def load_review(path: Path) -> ReviewSidecar:
    return ReviewSidecar.model_validate(load_json(path))


def load_report(path: Path) -> ReportSidecar:
    return ReportSidecar.model_validate(load_json(path))


def is_correctable(f: Finding, severity: Severity) -> bool:
    """A finding of the given severity the correction loop can act on (owned by moat, management or valuation)."""
    return f.severity == severity and f.owner in CORRECTABLE
