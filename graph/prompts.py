"""Builds the system and user prompts. Agent/skill files are read verbatim; a runtime contract
is appended so the agent files themselves never need to change."""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from .config import (AGENT_FILE, ANALYSTS, CORRECTABLE, DEPENDS, MAX_CORRECTIONS, MAX_MEDIUM_CORRECTIONS,
                     PRICE_MAX_AGE_DAYS, Paths)

SKILL_DIR = Path(".claude/skills/buffett-analysis")


def _strip_frontmatter(text: str) -> str:
    return re.sub(r"\A---\n.*?\n---\n", "", text.replace("\r\n", "\n"), count=1, flags=re.DOTALL).strip()


def load_agent_body(root: Path, agent: str) -> str:
    return _strip_frontmatter((root / ".claude" / "agents" / f"{AGENT_FILE[agent]}.md").read_text(encoding="utf-8"))


def load_skill_body(root: Path) -> str:
    return _strip_frontmatter((root / SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))


def load_data_rules(root: Path) -> str:
    """The "Data Rules" section of CLAUDE.md. Agents run without project settings (so CLAUDE.md is not loaded for
    them); the rules are passed on verbatim so CLAUDE.md stays their single source."""
    text = (root / "CLAUDE.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    m = re.search(r"^## Data Rules[ \t]*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not m or not m.group(1).strip():
        raise ValueError("CLAUDE.md has no '## Data Rules' section")
    return m.group(1).strip()


def load_review_checklist(root: Path) -> str:
    """The reviewer's audit checklist (the numbered list under "Check and audit:" in its agent definition), given
    to the analysts as a self-check before they save, so the reviewer's own wording stays the single source."""
    lines = load_agent_body(root, "review").splitlines()
    start = next((i + 1 for i, l in enumerate(lines) if l.strip() == "Check and audit:"), len(lines))
    items = []
    for line in lines[start:]:   # the numbered lines right after the heading line, blank lines allowed
        m = re.match(r"\s*\d+\.\s*(.+?)\s*$", line)
        if m:
            items.append(m.group(1))
        elif line.strip():
            break
    if not items:
        raise ValueError(f"{AGENT_FILE['review']}.md has no numbered list under 'Check and audit:'")
    return "\n".join(f"  {i}. {t}" for i, t in enumerate(items, 1))


# Reference files each agent's instructions tell it to read (valuation.md is named by the moat, management and
# margin-of-safety references for the operating-income-after-taxes definition; the main review checks the moat,
# management and valuation analyses against theirs, and the MOS audit the MOS analysis against its own).
# They are included in the system prompt so the agent does not spend tool calls reading them.
REFERENCES: dict[str, tuple[str, ...]] = {
    "moat": ("economic_moat", "valuation"),
    "management": ("management_quality", "valuation"),
    "valuation": ("valuation",),
    "business": (),
    "mos": ("margin_of_safety", "valuation"),
    "review": ("economic_moat", "management_quality", "valuation"),
    "mos_review": ("margin_of_safety", "valuation"),
    "report": (),
}


def load_references(root: Path, agent: str) -> str:
    parts = []
    for ref in REFERENCES[agent]:
        rel = (SKILL_DIR / "references" / f"{ref}.md").as_posix()
        body = _strip_frontmatter((root / rel).read_text(encoding="utf-8"))
        parts.append(f"### Reference file `{rel}` (full text)\n\n{body}")
    return "\n\n".join(parts)


def _sidecar_schema(agent: str) -> str:
    if agent == "valuation":
        return ('{"score": <integer 1-10, the same score stated in your markdown file>, "summary": "<one sentence>", '
                '"share_price": <the current share price used, a number, exactly as stated in your markdown>, '
                '"price_date": "<YYYY-MM-DD, the date of that price>", '
                '"intrinsic_value_per_share": {"low": <number>, "base": <number>, "high": <number>}}\n'
                "The intrinsic value figures are your per-share estimate's range and central value, in the price's "
                "currency (low <= base <= high; give the same number three times if you state a single value).")
    if agent == "mos":
        return ('{"score": <integer 1-10, the same score stated in your markdown file>, "summary": "<one sentence>", '
                '"share_price": <exactly the valuation sidecar\'s share_price>, '
                '"price_date": "<exactly the valuation sidecar\'s price_date>", '
                '"intrinsic_value_per_share": <the valuation\'s per-share intrinsic value you measure against, '
                'within its low-high range>, '
                '"margin_of_safety_pct": <(intrinsic_value_per_share - share_price) / intrinsic_value_per_share * 100, '
                'negative if the price is above it>}')
    if agent in ANALYSTS or agent == "business":
        return ('{"score": <integer 1-10, the same score stated in your markdown file>, '
                '"summary": "<one sentence>"}')
    if agent in ("review", "mos_review"):
        return json.dumps({
            "findings": [{"id": "R1" if agent == "review" else "M1", "problem": "...", "evidence": "...",
                          "severity": "HIGH|MEDIUM|LOW",
                          "owner": "moat|management|valuation|report" if agent == "review" else "mos",
                          "required_correction": "..."}],
        }, indent=2)
    return ('{"financial_quality_score": <integer 1-10>, "scores_reported": '
            '{"moat": <int>, "management": <int>, "valuation": <int>, "mos": <int, the MOS score the report uses>}, '
            '"share_price": <the report\'s share price reference: exactly the valuation sidecar\'s share_price>, '
            '"price_date": "<exactly the valuation sidecar\'s price_date>", '
            '"mos_score_change": null}\n'
            'Set "mos_score_change" only when issues of the MOS audit require a MOS score different from the margin '
            'of safety analysis\'s, as {"original": <its score>, "corrected": <the score you use>, '
            '"finding_ids": ["<the MOS audit finding id(s) requiring it>"], "reason": "<why, in a sentence>"}.')


def system_prompt(root: Path, paths: Paths, agent: str, company: str, iteration: int, *,
                  high_iteration: int = 0, medium_iteration: int = 0) -> str:
    key = paths.key
    contract = [
        "## RUNTIME CONTRACT (highest priority; overrides paths and details in the instructions above)",
        f"- Target company: {company}. Today's date: {date.today().isoformat()}.",
        f"- `<KEY>` in your instructions is `{key}`: your files live under `research/{key}/` and `reports/{key}/`.",
        f"- Framework reference files are in `{SKILL_DIR.as_posix()}/references/` (relative to the working directory)."
        + (" The ones your instructions name are included in full at the end of this prompt: they are already "
           "read, so do not open them again." if REFERENCES[agent] else ""),
        "- You cannot run shell commands or git. You may only write your own output file and your own sidecar, listed below.",
        f"- Read only markdown files directly in `research/{key}/` (not `_meta/` or `_archive/`), plus the SEC data "
        f"pack `{paths.rel(paths.data_pack)}` and its companion `{paths.rel(paths.data_dir / 'financials.json')}` "
        "(originally filed values of restated figures).",
        f"- SEC data pack: `{paths.rel(paths.data_pack)}` was generated by the workflow directly from the company's "
        "SEC XBRL filings (a primary source) and is shared by every agent in this run. Read it before other research. "
        "Its FACT values carry their filing references and may be cited as SEC filings without fetching those filings "
        "again; its CALCULATION rows show their formulas. Research as usual anything it does not cover or that needs "
        "context (segment detail, narrative" + (", market data, current share price" if agent in ("valuation", "review")
                                                 else "") + "). If it says it is unavailable, research everything as usual.",
        f"- Main output file: `{paths.rel(paths.output(agent))}`",
        f"- After the main file is saved, write a JSON sidecar to `{paths.rel(paths.sidecar(agent))}` "
        "containing ONLY valid JSON of this shape:",
        _sidecar_schema(agent),
    ]
    if agent in ANALYSTS or agent == "business":
        contract.append(
            "- Label substantive statements in your markdown as FACT, CALCULATION, ASSUMPTION or JUDGMENT, "
            "record source and period for key numbers, and state your 1-10 score explicitly under the word 'Score'.")
        contract.append("- The workflow checks your files mechanically before the "
                        + ("MOS audit" if agent == "mos" else "review") + ": the sidecar must agree "
                        "with the markdown" + (f", and the share price must be current (dated within {PRICE_MAX_AGE_DAYS} days)"
                                               if agent == "valuation"
                                               else ", and it must use the valuation's exact price and date and an "
                                               "intrinsic value within its range" if agent == "mos" else "") + ".")
        cost = ("a fix in the final report, as the MOS agent is not re-run" if agent == "mos"
                else "the report agent's time" if agent == "business"
                else "a correction run and a re-review")
        contract.append("- SELF-CHECK before you save: the independent reviewer will audit your file against the "
                        f"checklist below, and every issue it finds costs {cost}. Once your file is written, re-read "
                        "it against the items that apply to your analysis, and fix what you find. Check in "
                        "particular that every key figure has its source and period, agrees with the SEC data pack "
                        "where the pack covers it, and is the same wherever your file states it. Do not add a "
                        "section about this self-check to your file.\n"
                        "  Reviewer's checklist:\n" + load_review_checklist(root))
    if agent in ("mos", "mos_review", "report"):
        contract.append(f"- The valuation's key figures, including the reference share price and its date, are in its "
                        f"sidecar `{paths.rel(paths.sidecar('valuation'))}` (an exception to the `_meta/` rule above: "
                        "you may read that one file).")
    if agent in ("moat", "management", "business"):
        contract.append("- The valuation analysis, running in parallel with you, sets the run's single reference share "
                        "price. Do not state a current share price, or figures computed from one (market "
                        "capitalization, P/E, P/B, dividend or earnings yield, EV multiples, and the like). Historical "
                        "prices with their dates are fine where your analysis needs them (e.g. prices paid in past "
                        "share repurchases, or a holding valued at the price on a stated past date).")
    if agent == "review":
        contract += [
            (f"- This is the re-review after correction round {iteration}." if iteration else
             "- This is the initial review, before any correction round."),
            f"- HIGH-severity correction rounds already performed: {high_iteration} of {MAX_CORRECTIONS}.",
            f"- MEDIUM-only correction rounds already performed: {medium_iteration} of {MAX_MEDIUM_CORRECTIONS} "
            "(open MEDIUM findings are also sent back in every HIGH round; MEDIUM-only rounds start once no "
            "correctable HIGH finding remains, and none runs if HIGH correction stops with a HIGH finding still "
            "open; LOW findings are never corrected).",
            "- Give every finding a unique id (R1, R2, ...) and write that id next to the finding in review.md.",
            "- Set `owner` to the single agent whose file must change: moat, management, valuation, "
            "or `report` if only the final report can fix it (e.g. presentation, or anything in business.md: the "
            "report agent turns it into the Company Overview, Business Model and Financial Quality sections).",
            "- The margin of safety analysis is not part of this review: it is written only after the correction "
            "stage ends, and you audit it then in a separate, one-time pass. Do not audit it or assign issues to "
            "`mos` now.",
            "- Also audit compliance with the project data rules above (for example, an idea attributed to Buffett "
            "without supporting evidence).",
            "- The sidecar `findings` list holds ONLY issues that are open after this review, at every severity. "
            "An issue you verified as fixed is reported as fixed in review.md and left out of the sidecar: the "
            "workflow sends HIGH and MEDIUM findings in the sidecar to their owner for correction (findings owned by "
            "`report` go to the report agent).",
            "- The workflow counts the findings itself and decides what is corrected; you only audit and classify.",
            "- The LAST line of review.md must be exactly `Summary: H HIGH, M MEDIUM, L LOW` with real numbers: "
            "H, M and L equal the number of sidecar findings of each severity.",
        ]
        if high_iteration >= MAX_CORRECTIONS:
            contract.append("- The maximum number of HIGH-severity correction rounds has been reached: any HIGH "
                            "finding owned by moat, management or valuation that you record now will not be "
                            "corrected; it goes to the final report flagged as unresolved, and so does every open "
                            "MEDIUM finding then, as no MEDIUM-only round follows.")
        if medium_iteration >= MAX_MEDIUM_CORRECTIONS:
            contract.append("- The maximum number of MEDIUM-only correction rounds has been reached: a MEDIUM "
                            "finding owned by moat, management or valuation that you record now is corrected only "
                            "if a HIGH round still runs (it carries the open MEDIUM findings); otherwise it goes to "
                            "the final report flagged as unresolved.")
    if agent == "mos_review":
        mos_rel, rev_rel = paths.rel(paths.output("mos")), paths.rel(paths.output("review"))
        contract += [
            "- This is your one-time MOS AUDIT pass, not the main review: audit only the margin of safety analysis "
            f"`{mos_rel}`. The moat, management and valuation analyses have finished their review and correction "
            f"stage; read them (and `{rev_rel}`, the final main review) only as the MOS analysis's inputs. Do not "
            f"re-audit them, and do not re-report the issues still open in `{rev_rel}`; record an upstream problem "
            "only where the MOS analysis itself handles it wrongly.",
            "- In particular check: that the price, its date and the intrinsic value used are the valuation's, and the "
            "margin of safety is computed correctly from them; that the required discount is chosen as "
            "`margin_of_safety.md` prescribes (every condition considered, the largest applicable discount applied, "
            "ROE computed as defined there); that the moat, management and valuation conclusions, and the upstream "
            "issues left open, are reflected correctly; the score; and compliance with the project data rules.",
            "- Give every finding a unique id (M1, M2, ...) and write that id next to the finding in the audit file. "
            "Set `owner` to `mos` for every finding.",
            "- The MOS agent is never re-run. HIGH and MEDIUM findings go to the report agent, which fixes them in "
            "the final report (its Margin of Safety section, the MOS score and everything that depends on them); "
            "LOW findings are recorded only. Classify severity on the merits.",
            "- The sidecar `findings` list holds every issue you found, at every severity.",
            f"- The LAST line of `{paths.rel(paths.output('mos_review'))}` must be exactly "
            "`Summary: H HIGH, M MEDIUM, L LOW` with real numbers: H, M and L equal the number of sidecar findings "
            "of each severity.",
        ]
    if agent == "report":
        contract.append("- Use exactly the upstream scores given in the task for `scores_reported`; do not average "
                        "them. The one exception is the MOS score, when an issue of the MOS audit requires changing "
                        "it: then report the score you use, record the change in the sidecar's `mos_score_change`, "
                        "and explain it in the report in one paragraph (or one score-table row) that refers to the "
                        "margin of safety and states both the margin of safety analysis's score and yours, each "
                        "written as `N/10`, with the reason (the workflow checks all of this mechanically).")
        contract.append("- The share price reference and every mention of the current price use exactly the "
                        "valuation's share price and date; the workflow checks this mechanically.")
        contract.append("- `financial_quality_score` is the score in the business analysis; change it only where an "
                        "issue owned by the report requires it, and then say in the report why.")
    sections = [
        load_agent_body(root, agent),
        "## Buffett analysis skill (already loaded; follow it)\n" + load_skill_body(root),
        "## Project data rules (from the project's CLAUDE.md; follow them)\n" + load_data_rules(root),
        "\n".join(contract),
    ]
    if REFERENCES[agent]:
        sections.append("## Framework reference files (already loaded; read and apply them as your instructions "
                        "say)\n\n" + load_references(root, agent))
    return "\n\n".join(sections)


def _fmt_findings(findings) -> str:
    return "\n".join(
        f"- [{f.id}] ({f.severity}, owner={f.owner}) Problem: {f.problem}\n  Evidence: {f.evidence}\n"
        f"  Required correction: {f.required_correction}" for f in findings)


def user_prompt(paths: Paths, agent: str, company: str, *, findings=(), scores: dict[str, int] | None = None,
                unresolved=(), report_notes=(), mos_fixes=(), upstream_open=(), iteration: int = 0,
                high_iteration: int = 0, medium_iteration: int = 0, phase: str | None = None,
                previous_findings=(), changes=None) -> str:
    parts = [f"Perform your role for the company: {company}."]
    if DEPENDS[agent]:
        parts.append(f"Read your upstream inputs from `research/{paths.key}/` as your instructions describe.")
    if agent in CORRECTABLE and findings and iteration:
        round_label = (f"MEDIUM correction round {medium_iteration} of {MAX_MEDIUM_CORRECTIONS}" if phase == "medium"
                       else f"HIGH correction round {high_iteration} of {MAX_CORRECTIONS}")
        parts.append(f"\n## CORRECTION RUN ({round_label})\n"
                     f"Your existing file `{paths.rel(paths.output(agent))}` failed independent review. "
                     "Update it in place: fix exactly the problems below; change anything else only where this "
                     "requires it, and rewrite your sidecar. Make the changes as targeted edits (the Edit tool) to the "
                     "affected passages, including every figure or conclusion elsewhere in the file that depends on "
                     "them, rather than rewriting the whole file.")
        if phase == "high" and any(f.severity == "MEDIUM" for f in findings):
            parts.append("Open MEDIUM findings are corrected in the same round as HIGH ones, so yours are "
                         "included below; fix them too.")
        parts.append(_fmt_findings(findings))
        parts.append("\nBefore saving, apply the SELF-CHECK in your instructions to every passage you changed, and "
                     "make sure each figure or conclusion you changed agrees with the other research files in "
                     f"`research/{paths.key}/` that state it (read them where needed; you can only change your own "
                     "file). A changed figure that now contradicts another file is a new finding in the re-review.")
    if agent == "mos":
        if upstream_open:
            parts.append("\n## UPSTREAM ISSUES LEFT OPEN BY THE REVIEW\n"
                         "The moat, management and valuation analyses have finished their review and correction stage "
                         "with these issues still open (they are flagged as unresolved in the final report). Take "
                         "them into account where they bear on the margin of safety, for example on the confidence "
                         "in the intrinsic value estimate and the discount required, and say how:\n"
                         + _fmt_findings(upstream_open))
        else:
            parts.append("\nThe moat, management and valuation analyses passed review with no HIGH or MEDIUM issue "
                         "left open.")
    if agent == "review" and iteration:
        if changes is not None:
            parts.append(_scope(iteration, changes))
        else:
            parts.append(f"\nThis re-reviews corrections made in round {iteration}. Re-audit everything, and "
                         "explicitly verify the previously reported issues are fixed.")
        if previous_findings:
            parts.append("\n## Findings of the previous review pass\n"
                         "State in review.md whether each one is now fixed. Keep the same id for an issue that is still "
                         "open; give each new issue a new id that no earlier finding has used.\n"
                         + _fmt_findings(previous_findings))
    if agent == "report":
        parts.append("\nUpstream scores (use exactly; the MOS score may change only as described below): "
                     + json.dumps(scores or {}))
        if unresolved:
            parts.append("\n## UNRESOLVED ISSUES (left open by the correction loop)\n"
                         "Use your best judgment to resolve each, and clearly flag every one as an unresolved "
                         "issue in the report, labeled with its severity (each item below states HIGH or MEDIUM):\n"
                         + _fmt_findings(unresolved))
        if report_notes:
            parts.append("\n## Issues owned by the report (you must address these)\n" + _fmt_findings(report_notes))
        if mos_fixes:
            parts.append("\n## Issues found by the audit of the margin of safety analysis (you must fix these)\n"
                         "The margin of safety analysis is never re-run, so fix each issue below in the report: "
                         "correct the Margin of Safety section, the MOS score where an issue requires it, and every "
                         "statement that depends on them (score table, investment thesis, Key Risks, Final Investment "
                         "Assessment). Where the report departs from a figure, conclusion or score of the margin of "
                         "safety analysis, say so and why. If you judge an issue cannot be fixed, flag it as an "
                         "unresolved issue labeled with its severity.\n" + _fmt_findings(mos_fixes))
        else:
            parts.append("\nThe audit of the margin of safety analysis found no HIGH or MEDIUM issue: report the MOS "
                         "score exactly as given.")
    return "\n".join(parts)


def _scope(iteration: int, changes) -> str:
    """Re-review limited to what changed since the last review (manifest.reviewed_changes)."""
    unchanged = [c.rel for c in changes if not c.changed]
    full = [c.rel for c in changes if c.changed and c.diff is None]
    out = [f"\n## SCOPE OF THIS RE-REVIEW (after correction round {iteration})",
           "Earlier review passes have audited every research file. Since the last pass, only the changes shown below "
           "were made. Do not re-audit unchanged text from scratch:",
           "1. Verify each previously reported finding against the current files.",
           "2. Audit every changed passage in full, including its consistency with the rest of its file and with the "
           "other research files (figures, prices, scores and conclusions that depend on it, in any file).",
           "3. A previous finding on text that did not change stays open with the same id unless the change "
           "resolves it.",
           "Open other files only as far as steps 1-2 require."]
    if unchanged:
        out.append("Unchanged since the last review: " + ", ".join(f"`{r}`" for r in unchanged) + ".")
    if full:
        out.append("Changed too extensively to show as a diff; read in full and audit as new: "
                   + ", ".join(f"`{r}`" for r in full) + ".")
    for c in changes:
        if c.changed and c.diff is not None:
            out.append(f"\n### Changes to `{c.rel}`\n```diff\n{c.diff}\n```")
    return "\n".join(out)


def fix_prompt(errors: list[str]) -> str:
    """Follow-up in the same session after the workflow's validation rejected the saved files."""
    return ("The workflow's validation REJECTED your saved files. Fix these problems and re-save the affected "
            "files (only what is needed to fix them):\n" + "\n".join(f"- {e}" for e in errors))
