"""Builds the system and user prompts. Agent/skill files are read verbatim; a runtime contract
is appended so the agent files themselves never need to change."""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from .config import AGENT_FILE, ANALYSTS, MAX_CORRECTIONS, MAX_MEDIUM_CORRECTIONS, Paths

SKILL_DIR = Path(".claude/skills/buffett-analysis")


def _strip_frontmatter(text: str) -> str:
    return re.sub(r"\A---\n.*?\n---\n", "", text.replace("\r\n", "\n"), count=1, flags=re.DOTALL).strip()


def load_agent_body(root: Path, agent: str) -> str:
    return _strip_frontmatter((root / ".claude" / "agents" / f"{AGENT_FILE[agent]}.md").read_text(encoding="utf-8"))


def load_skill_body(root: Path) -> str:
    return _strip_frontmatter((root / SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))


def _sidecar_schema(agent: str) -> str:
    if agent in ANALYSTS:
        return ('{"score": <integer 1-10, the same score stated in your markdown file>, '
                '"summary": "<one sentence>"}')
    if agent == "review":
        return json.dumps({
            "findings": [{"id": "R1", "problem": "...", "evidence": "...", "severity": "HIGH|MEDIUM|LOW",
                          "owner": "moat|management|valuation|mos|report", "required_correction": "..."}],
            "counts": {"high": 0, "medium": 0, "low": 0, "high_unresolved": 0},
        }, indent=2)
    return ('{"financial_quality_score": <integer 1-10>, "scores_reported": '
            '{"moat": <int>, "management": <int>, "valuation": <int>, "mos": <int>}}')


def system_prompt(root: Path, paths: Paths, agent: str, company: str, iteration: int, *,
                  high_iteration: int = 0, medium_iteration: int = 0) -> str:
    key = paths.key
    contract = [
        "## RUNTIME CONTRACT (highest priority; overrides paths and details in the instructions above)",
        f"- Target company: {company}. Today's date: {date.today().isoformat()}.",
        f"- `<KEY>` in your instructions is `{key}`: your files live under `research/{key}/` and `reports/{key}/`.",
        f"- Framework reference files are in `{SKILL_DIR.as_posix()}/references/` (relative to the working directory).",
        "- You cannot run shell commands or git. You may only write your own output file and your own sidecar, listed below.",
        f"- Read only markdown files directly in `research/{key}/` (not `_meta/` or `_archive/`).",
        f"- Main output file: `{paths.rel(paths.output(agent))}`",
        f"- After the main file is saved, write a JSON sidecar to `{paths.rel(paths.sidecar(agent))}` "
        "containing ONLY valid JSON of this shape:",
        _sidecar_schema(agent),
    ]
    if agent in ANALYSTS:
        contract.append(
            "- Label substantive statements in your markdown as FACT, CALCULATION, ASSUMPTION or JUDGMENT, "
            "record source and period for key numbers, and state your 1-10 score explicitly under the word 'Score'.")
    if agent == "review":
        contract += [
            f"- This is review pass {iteration + 1}.",
            f"- HIGH-severity correction rounds already performed: {high_iteration} of {MAX_CORRECTIONS}.",
            f"- MEDIUM-severity correction rounds already performed: {medium_iteration} of {MAX_MEDIUM_CORRECTIONS} "
            "(these only start once no correctable HIGH finding remains; LOW findings are never corrected).",
            "- Give every finding a unique id (R1, R2, ...) and write that id next to the finding in review.md.",
            "- Set `owner` to the single agent whose file must change: moat, management, valuation, mos, "
            "or `report` if only the final report can fix it (e.g. presentation, financial quality section).",
            "- The sidecar counts must equal the findings. `high_unresolved` = HIGH findings still open after this review.",
            "- The LAST line of review.md must be exactly: "
            "`Summary: H HIGH, M MEDIUM, L LOW; U HIGH unresolved` with real numbers "
            "(this matches the sidecar; U = counts.high_unresolved).",
        ]
        if high_iteration >= MAX_CORRECTIONS:
            contract.append("- The maximum number of HIGH-severity correction iterations has been reached: "
                            "list any remaining HIGH issue as unresolved.")
        if medium_iteration >= MAX_MEDIUM_CORRECTIONS:
            contract.append("- The maximum number of MEDIUM-severity correction iterations has been reached: "
                            "list any remaining MEDIUM issue as unresolved.")
    if agent == "report":
        contract.append("- Use exactly the upstream scores given in the task for `scores_reported`; do not average them.")
    return "\n\n".join([
        load_agent_body(root, agent),
        "## Buffett analysis skill (already loaded; follow it)\n" + load_skill_body(root),
        "\n".join(contract),
    ])


def _fmt_findings(findings) -> str:
    return "\n".join(
        f"- [{f.id}] ({f.severity}, owner={f.owner}) Problem: {f.problem}\n  Evidence: {f.evidence}\n"
        f"  Required correction: {f.required_correction}" for f in findings)


def user_prompt(paths: Paths, agent: str, company: str, *, findings=(), upstream_changed: bool = False,
                scores: dict[str, int] | None = None, unresolved=(), report_notes=(), iteration: int = 0,
                high_iteration: int = 0, medium_iteration: int = 0, phase: str | None = None) -> str:
    parts = [f"Perform your role for the company: {company}.",
             f"Read your upstream inputs from `research/{paths.key}/` as your instructions describe."]
    if agent in ANALYSTS and (findings or upstream_changed) and iteration:
        round_label = (f"MEDIUM correction round {medium_iteration} of {MAX_MEDIUM_CORRECTIONS}" if phase == "medium"
                       else f"round {high_iteration} of {MAX_CORRECTIONS}")
        parts.append(f"\n## CORRECTION RUN ({round_label})\n"
                     f"Your existing file `{paths.rel(paths.output(agent))}` failed independent review. "
                     "Update it in place: fix exactly the problems below, change anything else only where these fixes require it, "
                     "and rewrite your sidecar.")
        if findings:
            parts.append(_fmt_findings(findings))
        if upstream_changed:
            parts.append("Upstream research files changed in this round; re-read them and make this analysis consistent with them.")
    if agent == "review" and iteration:
        parts.append(f"\nThis re-reviews corrections made in round {iteration}. Re-audit everything, and explicitly verify "
                     "the previously reported issues are fixed.")
    if agent == "report":
        parts.append("\nUpstream scores (use exactly): " + json.dumps(scores or {}))
        if unresolved:
            parts.append("\n## UNRESOLVED ISSUES (correction limit reached)\n"
                         "Use your best judgment to resolve each, and clearly flag every one as an unresolved "
                         "issue in the report, labeled with its severity (each item below states HIGH or MEDIUM):\n"
                         + _fmt_findings(unresolved))
        if report_notes:
            parts.append("\n## Issues owned by the report (you must address these)\n" + _fmt_findings(report_notes))
    return "\n".join(parts)
