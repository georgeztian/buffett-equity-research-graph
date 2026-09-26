"""Static workflow definition: agents, dependencies, limits, per-company paths, and the user's SEC contact."""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MAX_CORRECTIONS = 5          # Stage 4 iteration cap, HIGH-severity findings
MAX_MEDIUM_CORRECTIONS = 2   # Stage 4b iteration cap, MEDIUM-severity findings (runs after HIGH is clear)
MAX_VALIDATION_RETRIES = 2   # retries per node when output fails validation
AGENT_TIMEOUT_SECONDS = 45 * 60
# The SDK's default 1 MB per-message limit is exceeded when an agent fetches a large filing
# (e.g. a 10-K page), which kills the attempt with "JSON message exceeded maximum buffer size".
AGENT_MAX_BUFFER_BYTES = 100 * 1024 * 1024
# SEC EDGAR requires automated clients to identify themselves with a name and a contact email, and may answer
# 403 Forbidden otherwise. Each user supplies their own, in the SEC_USER_AGENT env var or the gitignored
# project .env file; there is deliberately no built-in default, and no request is ever sent without a contact.
# The contact is optional: without one, Stage 0 skips SEC and the agents research as before. See sec_contact().
ENV_FILE = ROOT / ".env"
SEC_TIMEOUT_SECONDS = 30
DATA_PACK_YEARS = 15         # fiscal years of XBRL history in the data pack

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


_EMAIL = re.compile(r"[^\s@<>]+@[^\s@<>]+\.[A-Za-z]{2,}")
# Placeholder domains (.env.example and the help text use them), never a real user's contact.
_PLACEHOLDER_DOMAINS = ("example.com", "example.org", "example.net", "domain.com", "yourdomain.com")

# SEC_USER_AGENT value that records the user's choice to run without a contact (and not be asked again).
SEC_DECLINED = "declined"

SEC_SETUP_HELP = (
    "The SEC data pack is optional. To enable it, SEC EDGAR requires your name and a contact email; set them\n"
    "once for this project (saved in the gitignored .env file, sent only to SEC):\n"
    '  python -m graph --set-sec-contact "Your Name you@yourdomain.com"\n'
    "(or set the SEC_USER_AGENT environment variable, which takes precedence over .env)."
)


def check_sec_user_agent(value: str) -> str:
    """The cleaned User-Agent if it has a name and a real-looking contact email, else raise ValueError."""
    v = " ".join(value.strip().strip("\"'").split())
    m = _EMAIL.search(v)
    if not m:
        raise ValueError(f"no contact email in {v!r}")
    if not v.replace(m.group(0), "").strip(" <>()"):
        raise ValueError(f"no name next to the email in {v!r}")
    if m.group(0).lower().rsplit("@", 1)[1] in _PLACEHOLDER_DOMAINS:
        raise ValueError(f"{v!r} is a placeholder; use your own name and email")
    return v


def _env_key(line: str) -> str | None:
    """The variable name on a `NAME=value` (or `export NAME=value`) line of .env, else None."""
    k, sep, _ = line.strip().partition("=")
    return k.strip().removeprefix("export ").strip() if sep else None


def _read_env_file(name: str) -> str | None:
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    return next((l.partition("=")[2].strip().strip("\"'") for l in lines if _env_key(l) == name), None)


def _sec_setting() -> tuple[str | None, str]:
    """(raw SEC_USER_AGENT value or None, where it came from): the environment first, else the project .env."""
    env = os.environ.get("SEC_USER_AGENT")
    return (env, "the SEC_USER_AGENT environment variable") if env else (_read_env_file("SEC_USER_AGENT"), ".env")


def is_declined(value: str | None) -> bool:
    return (value or "").strip().strip("\"'").lower() == SEC_DECLINED


def sec_contact_declined() -> bool:
    """True if the user chose to run without an SEC contact (SEC_USER_AGENT=declined)."""
    return is_declined(_sec_setting()[0])


def sec_contact() -> tuple[str | None, str]:
    """(User-Agent, "") for this user's SEC contact: SEC_USER_AGENT from the environment, else from the project
    .env file. (None, why) if it is missing, declined, or not a valid name + contact email."""
    raw, where = _sec_setting()
    if not raw:
        return None, "SEC contact not set"
    if is_declined(raw):
        return None, f"SEC contact declined (set in {where})"
    try:
        return check_sec_user_agent(raw), ""
    except ValueError as e:
        return None, f"invalid SEC contact in {where}: {e}"


def save_sec_user_agent(value: str) -> str:
    """Write SEC_USER_AGENT into the project .env, keeping any other lines: a validated contact, or `declined`
    to record that the user chose to run without one. Returns the value written."""
    v = SEC_DECLINED if is_declined(value) else check_sec_user_agent(value)
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    kept = [l for l in lines if _env_key(l) != "SEC_USER_AGENT"]
    ENV_FILE.write_text("\n".join(kept + [f"SEC_USER_AGENT={v}"]) + "\n", encoding="utf-8")
    return v


STATE_DIR = ROOT / ".state"   # runtime state (checkpoints, detached-run logs); gitignored and Dropbox-ignored


def checkpoint_db_path() -> Path:
    return STATE_DIR / "checkpoints.sqlite"


RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*-\d{8}-\d{6}")   # <KEY>-YYYYmmdd-HHMMSS


def resume_command(run_id: str) -> str:
    """The resume command as the user should type it: the project venv's python when that is what is running
    (the form documented and pre-approved in .claude/settings.json), else plain `python`."""
    exe = Path(sys.executable)
    py = exe.relative_to(ROOT).with_suffix("").as_posix() if exe.is_relative_to(ROOT) else "python"
    return f"{py} -m graph --resume {run_id}"


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
    def data_dir(self) -> Path:
        """Stage 0 SEC data pack (deterministic, fetched once per run and shared by every agent)."""
        return self.research_dir / "_data"

    @property
    def data_pack(self) -> Path:
        return self.data_dir / "financials.md"

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
