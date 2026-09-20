"""Static workflow definition: agents, dependencies, limits, and per-company paths."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MAX_CORRECTIONS = 5          # Stage 4 iteration cap
MAX_VALIDATION_RETRIES = 2   # retries per node when output fails validation
AGENT_TIMEOUT_SECONDS = 45 * 60
# The SDK's default 1 MB per-message limit is exceeded when an agent fetches a large filing
# (e.g. a 10-K page), which kills the attempt with "JSON message exceeded maximum buffer size".
AGENT_MAX_BUFFER_BYTES = 100 * 1024 * 1024

RESEARCH_AGENTS = ("moat", "management", "valuation")   # Stage 1 (parallel)
ANALYSTS = RESEARCH_AGENTS + ("mos",)                   # agents that can own a finding
ALL_AGENTS = ANALYSTS + ("review", "report")

# agent -> agents whose output files it reads (the workflow's dependency graph)
DEPENDS: dict[str, tuple[str, ...]] = {
    "moat": (),
    "management": (),
    "valuation": (),
    "mos": RESEARCH_AGENTS,
    "review": ANALYSTS,
    "report": ANALYSTS + ("review",),
}

AGENT_FILE = {
    "moat": "moat-agent",
    "management": "management-agent",
    "valuation": "valuation-agent",
    "mos": "mos-agent",
    "review": "reviewer-agent",
    "report": "report-agent",
}

OUTPUT_NAME = {
    "moat": "moat.md",
    "management": "management.md",
    "valuation": "valuation.md",
    "mos": "mos.md",
    "review": "review.md",
    "report": "final_investment_report.md",
}

SOURCE_TAGS = ("FACT", "CALCULATION", "ASSUMPTION", "JUDGMENT")

REPORT_HEADINGS = (
    "Executive Summary", "Company Overview", "Business Model", "Financial Quality",
    "Economic Moat", "Management Quality", "Valuation", "Margin of Safety",
    "Key Risks", "Final Investment Assessment",
)


def ensure_inside(root: Path, path: Path) -> Path:
    """HARD RULE guard: raise unless `path` resolves inside `root`."""
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"refusing to use a path outside the project folder: {path}")
    return path


STATE_DIR = ROOT / ".state"   # runtime state (checkpoints, detached-run logs); gitignored and Dropbox-ignored


def checkpoint_db_path() -> Path:
    return STATE_DIR / "checkpoints.sqlite"


RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*-\d{8}-\d{6}")   # <KEY>-YYYYmmdd-HHMMSS


def validate_run_id(run_id: str) -> str:
    """Run ids become file and folder names, so only the exact generated format is accepted."""
    if not RUN_ID.fullmatch(run_id) or ".." in run_id:
        raise ValueError(f"invalid run id {run_id!r}; expected <KEY>-YYYYmmdd-HHMMSS")
    return run_id


def key_of(run_id: str) -> str:
    """The company folder key inside a run id."""
    return run_id.rsplit("-", 2)[0]


def log_path(run_id: str) -> Path:
    """Progress log of a detached run."""
    return ensure_inside(ROOT, STATE_DIR / "logs" / f"{validate_run_id(run_id)}.log")


def company_key(company: str) -> str:
    """Filesystem-safe folder key: the ticker if one is given, else a slug of the name."""
    m = re.search(r"\(([A-Za-z][A-Za-z.\-]{0,5})\)", company)
    if m:
        return m.group(1).upper().rstrip(".-")   # a trailing dot would be silently dropped by Windows
    s = company.strip()
    if re.fullmatch(r"[A-Z][A-Z.\-]{0,5}", s):
        return s.rstrip(".-")
    slug = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    if not slug:
        raise ValueError(f"Cannot derive a folder key from company {company!r}")
    return slug


@dataclass(frozen=True)
class Paths:
    root: Path
    key: str

    def __post_init__(self) -> None:   # the key can come from user input (--resume), so validate it
        for d in (self.research_dir, self.reports_dir):
            ensure_inside(self.root, d)

    @property
    def research_dir(self) -> Path:
        return self.root / "research" / self.key

    @property
    def meta_dir(self) -> Path:
        return self.research_dir / "_meta"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports" / self.key

    def output(self, agent: str) -> Path:
        base = self.reports_dir if agent == "report" else self.research_dir
        return base / OUTPUT_NAME[agent]

    def sidecar(self, agent: str) -> Path:
        return self.meta_dir / f"{agent}.json"

    @property
    def manifest(self) -> Path:
        return self.meta_dir / "manifest.json"

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()
