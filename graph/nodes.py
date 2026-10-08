"""Graph nodes. Every agent run = one bounded agent session + deterministic validation."""
from __future__ import annotations

import asyncio
import shutil
import time
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path

from . import contracts, datapack, manifest, prompts, routing, validators
from .agent_runner import AgentRunner, AgentTask, Usage
from .config import (AGENT_FILE, ALL_AGENTS, CORRECTABLE, DEPENDS, MAX_CORRECTIONS, MAX_FINDING_ATTEMPTS,
                     MAX_MEDIUM_CORRECTIONS, MAX_VALIDATION_RETRIES, SCORED_AGENTS, STAGE1_AGENTS, Paths)
from .notify import notify
from .summary import write_summary


def _log(msg: str) -> None:
    """Timestamped progress line, flushed so it shows up immediately even when output is redirected."""
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _event(msg: str) -> str:
    """Log a progress line and return it for the run history."""
    _log(msg)
    return msg


class NodeFailure(RuntimeError):
    """A node could not produce a complete, valid output within its retry budget."""

    def __init__(self, message: str, agent: str | None = None, usage: Usage | None = None):
        super().__init__(message)
        self.agent = agent
        self.usage = usage   # what the failed run spent, so the summary still accounts for it
        self.completed: list[dict] = []           # updates of sibling agents that finished in the same node
        self.others: list["NodeFailure"] = []     # sibling agents that also failed in the same node


class UsageLimitReached(NodeFailure):
    """Claude usage limit hit. Stop at once (retrying cannot help); resume with --resume after the reset."""


def _unresolved(state: dict) -> list[contracts.Finding]:
    """The upstream issues the correction loop left open, as recorded by the flag nodes (the only way the loop ends
    with a correctable HIGH or MEDIUM finding open). Passed to the MOS agent and to the report."""
    return [contracts.Finding.model_validate(f)
            for f in state.get("unresolved_high", []) + state.get("unresolved_medium", [])]


def _require_fresh_inputs(paths: Paths, agent: str) -> None:
    """Before review, mos, mos_review and report: refuse to run on an upstream file built from outdated inputs."""
    stale = validators.check_fresh_inputs(paths, agent)
    if stale:
        raise NodeFailure(f"{agent} blocked, stale inputs: {stale}", agent)


def _merge(*updates: dict) -> dict:
    out: dict = {"status": {}, "scores": {}, "history": []}
    for u in updates:
        out["status"].update(u.get("status", {}))
        out["scores"].update(u.get("scores", {}))
        out["history"] += u.get("history", [])
    return out


class Workflow:
    def __init__(self, runner: AgentRunner, root: Path):
        self.runner = runner
        self.root = root

    def paths(self, state: dict) -> Paths:
        return Paths(self.root, state["key"])

    # ------------------------------------------------------------------ core executor
    async def _execute(self, agent: str, state: dict, **ctx) -> dict:
        paths = self.paths(state)
        iteration = state.get("iteration", 0)               # total correction rounds (HIGH + MEDIUM); unique round id
        high_iteration = state.get("high_iteration", 0)     # HIGH-phase rounds so far; cap: MAX_CORRECTIONS
        medium_iteration = state.get("medium_iteration", 0) # MEDIUM-phase rounds so far; cap: MAX_MEDIUM_CORRECTIONS
        phase = ctx.get("phase")                            # "high" | "medium" | None, set by the correction nodes
        prior = state.get("status", {}).get(agent, {})
        prior_runs = prior.get("runs", 0)

        missing = [d for d in DEPENDS[agent] if not paths.output(d).exists()]
        if missing:
            raise NodeFailure(f"{agent}: upstream outputs missing: {missing}", agent)

        done = manifest.completed_in_round(paths, agent, iteration)
        if done:  # finished earlier in this round; its node failed elsewhere, so don't redo it
            where = f" for round {iteration}" if iteration else ""
            return self._result(agent, paths, prior_runs, done["attempts"], done.get("usage"), iteration,
                                _event(f"{agent}: already complete{where}; not re-run"), prior)

        sys_prompt = prompts.system_prompt(self.root, paths, agent, state["company"], iteration,
                                           high_iteration=high_iteration, medium_iteration=medium_iteration)
        base_prompt = prompts.user_prompt(paths, agent, state["company"], iteration=iteration,
                                          high_iteration=high_iteration, medium_iteration=medium_iteration,
                                          scores=state.get("scores"), **ctx)
        in_hashes = manifest.input_hashes(paths, agent)
        allowed = (paths.output(agent), paths.sidecar(agent))
        phase_tag = f" [{phase.upper()}]" if phase else ""
        label = f" (correction round {iteration}{phase_tag})" if iteration and agent in CORRECTABLE else \
                f" (re-review after round {iteration})" if iteration and agent == "review" else ""

        # A validation rejection is sent back into the SAME session (the agent keeps everything it has already
        # read and only fixes what was rejected). A session that failed to execute is replaced by a fresh one
        # that gets the full task again. `started` marks the session start: files must be written after it.
        use = Usage()
        vkw = {"scores": state.get("scores"), "unresolved": ctx.get("unresolved") or [],
               "previous_ids": [f.id for f in ctx.get("previous_findings") or ()],
               "mos_fixes": ctx.get("mos_fixes") or [], "report_notes": ctx.get("report_notes") or []}
        errors, attempts, started = [], 0, time.time()
        async with AsyncExitStack() as stack:
            session = None
            for attempt in range(1, MAX_VALIDATION_RETRIES + 2):
                attempts = attempt
                if session is None:
                    prompt = base_prompt if attempt == 1 else (
                        base_prompt + "\n\n## YOUR PREVIOUS ATTEMPT WAS REJECTED\nFix these problems and "
                        "re-save the required files:\n" + "\n".join(f"- {e}" for e in errors))
                    session_stack = await stack.enter_async_context(AsyncExitStack())
                    session = await session_stack.enter_async_context(
                        self.runner.session(AgentTask(agent, sys_prompt, prompt, self.root, allowed)))
                    started = time.time()
                    how = ""
                else:
                    prompt = prompts.fix_prompt(errors)
                    how = " (same session)"
                _log(f"{agent}: started{label}" + (f", attempt {attempt}{how}" if attempt > 1 else ""))
                res = await session.send(prompt)
                use.add(res)
                if res.usage_limit:
                    raise UsageLimitReached(f"{agent}: {res.error}", agent, use)
                if res.ok:
                    errors = validators.validate(agent, paths, started, **vkw)
                elif validators.saved_before_error(agent, paths, started, **vkw):
                    # the turn failed only after the agent had saved complete outputs: keep them, don't redo the task
                    _log(f"{agent}: {res.error[:120]} - but its saved files are complete and pass validation; kept")
                    errors = []
                else:
                    errors = [f"agent execution error: {res.error}"]
                    await session_stack.aclose()   # a broken session is not reused
                    session = None
                if not errors:
                    break
                _log(f"{agent}: attempt {attempt} rejected: {errors[0][:160]}")
        if errors:
            raise NodeFailure(f"{agent} failed after {attempts} attempts: " + "; ".join(errors), agent, use)

        this = use.record()
        manifest.record(paths, agent, in_hashes, iteration, attempts, this)
        return self._result(agent, paths, prior_runs, attempts, this, iteration,
                            _event(f"{agent}: complete (run {prior_runs + 1}, {attempts} attempt(s), "
                                   f"{use.describe()})"), prior)

    @staticmethod
    def _result(agent: str, paths: Paths, prior_runs: int, attempts: int, this: dict | None,
                iteration: int, event: str, prior: dict) -> dict:
        """`cost_usd`, `seconds` and `tokens` describe this run; `total_*` add up every run of the agent."""
        this = this or {"cost_usd": None, "seconds": 0.0, "turns": 0, "tokens": {}}
        update: dict = {
            "status": {agent: {"state": "complete", "runs": prior_runs + 1, "attempts": attempts,
                               "correction_round": iteration, **this,
                               **Usage.accumulate(prior, this)}},
            "history": [event],
        }
        if agent in SCORED_AGENTS:
            update["scores"] = {agent: contracts.load_score(paths.sidecar(agent)).score}
        return update

    # ------------------------------------------------------------------ nodes
    async def validate_input(self, state: dict) -> dict:
        paths = self.paths(state)
        for a in ALL_AGENTS:
            f = self.root / ".claude" / "agents" / f"{AGENT_FILE[a]}.md"
            if not f.exists():
                raise NodeFailure(f"missing agent definition: {f}")
        for ref in sorted({r for refs in prompts.REFERENCES.values() for r in refs}):
            if not (self.root / prompts.SKILL_DIR / "references" / f"{ref}.md").exists():
                raise NodeFailure(f"missing reference file: {ref}.md")
        try:
            prompts.load_data_rules(self.root)
        except (OSError, ValueError) as e:
            raise NodeFailure(f"cannot load the project data rules for the agents: {e}")
        try:
            prompts.load_review_checklist(self.root)
        except (OSError, ValueError) as e:
            raise NodeFailure(f"cannot load the reviewer's checklist for the analysts' self-check: {e}")

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        old = [p for p in (*paths.research_dir.glob("*.md"), *paths.meta_dir.rglob("*"),
                           *paths.data_dir.rglob("*")) if p.is_file()]
        old += [p for p in paths.reports_dir.glob("*") if p.is_file()]
        if old:  # a fresh run must never mistake earlier outputs for its own
            arch = paths.research_dir / "_archive" / stamp
            for p in old:
                in_reports = p.is_relative_to(paths.reports_dir)
                dest = arch / ("reports" if in_reports else "") / p.relative_to(
                    paths.reports_dir if in_reports else paths.research_dir)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), dest)
        paths.meta_dir.mkdir(parents=True, exist_ok=True)
        paths.reports_dir.mkdir(parents=True, exist_ok=True)
        return {"iteration": 0, "high_iteration": 0, "medium_iteration": 0, "findings": [], "finding_attempts": {},
                "unresolved_high": [], "unresolved_medium": [], "mos_findings": [], "reported_scores": {},
                "score_changes": {},
                "history": [_event(f"input validated for {state['company']} -> key {state['key']}"
                                   + (f"; archived {len(old)} earlier files" if old else ""))]}

    async def data_pack(self, state: dict) -> dict:
        """Stage 0: deterministic SEC XBRL data pack shared by every agent. Never fails the run: if the data
        cannot be fetched, the pack says so and the agents research everything as before."""
        paths = self.paths(state)
        t0 = time.time()
        note = await asyncio.to_thread(datapack.build, paths, state["company"])
        return {"data_pack": note, "history": [_event(f"data pack: {note} ({time.time() - t0:.1f}s)")]}

    async def _parallel(self, calls: list) -> list[dict]:
        """Run agent calls concurrently. Every one is allowed to finish (a completed run persists in the manifest,
        so a resume skips it) before a failure is surfaced, with the siblings' outcomes attached for the summary."""
        results = await asyncio.gather(*calls, return_exceptions=True)
        failures = [r for r in results if isinstance(r, BaseException)]
        if failures:
            err = next((f for f in failures if isinstance(f, UsageLimitReached)), failures[0])
            if isinstance(err, NodeFailure):
                err.completed += [r for r in results if not isinstance(r, BaseException)]
                err.others += [f for f in failures if f is not err and isinstance(f, NodeFailure)]
            raise err
        return list(results)

    async def research(self, state: dict) -> dict:
        """Stage 1: the independent agents in parallel. One node rather than one per agent: LangGraph cancels the
        rest of a step when a node fails, which would throw away the other agents' work in progress."""
        return _merge(*await self._parallel([self._execute(a, state) for a in STAGE1_AGENTS]))

    async def review(self, state: dict) -> dict:
        paths = self.paths(state)
        _require_fresh_inputs(paths, "review")
        it = state.get("iteration", 0)
        if it and not manifest.completed_in_round(paths, "review", it) and not manifest.stale_agents(paths, ("review",)):
            # No file the reviewer reads changed in this round: a re-review could only repeat the last one.
            rev = contracts.load_review(paths.sidecar("review"))
            c = rev.counts
            return {"findings": [f.model_dump() for f in rev.findings],
                    "history": [_event(f"review: no research file changed in round {it}, so the previous review "
                                       f"stands ({c.high} HIGH, {c.medium} MEDIUM, {c.low} LOW); not re-run")]}
        prev = [contracts.Finding.model_validate(f) for f in state.get("findings", [])]
        changes = manifest.reviewed_changes(paths) if it else None   # re-review: only what changed since the last
        upd = await self._execute("review", state, previous_findings=prev, changes=changes)
        manifest.snapshot_reviewed(paths)
        if it:
            scope = (f"scoped to {sum(c.changed for c in changes)} changed file(s)" if changes is not None
                     else "full re-audit (no snapshot of the last reviewed files)")
            upd["history"].append(_event(f"review: re-review {scope}"))
        rev = contracts.load_review(paths.sidecar("review"))
        n_high_correctable = sum(contracts.is_correctable(f, "HIGH") for f in rev.findings)
        n_medium_correctable = sum(contracts.is_correctable(f, "MEDIUM") for f in rev.findings)
        upd["findings"] = [f.model_dump() for f in rev.findings]
        upd["history"].append(_event(f"review: {rev.counts.high} HIGH ({n_high_correctable} correctable), "
                                     f"{rev.counts.medium} MEDIUM ({n_medium_correctable} correctable), "
                                     f"{rev.counts.low} LOW"))
        return upd

    async def _run_correction_round(self, state: dict, *, severity: str, round_field: str, max_rounds: int) -> dict:
        """One correction round for the actionable findings routing.plan_corrections selects: a HIGH round also
        carries every actionable MEDIUM finding, a MEDIUM round only MEDIUM ones. Shared by `correct` (HIGH, cap
        MAX_CORRECTIONS) and `correct_medium` (MEDIUM-only, cap MAX_MEDIUM_CORRECTIONS). `iteration` is the
        single monotonic round id shared by both phases (kept unique across the whole correction stage,
        for manifest/resume bookkeeping); `round_field` is the phase-local counter (`high_iteration` or
        `medium_iteration`) that each phase's own cap is checked against in routing.py, so activity in one
        phase never changes the other phase's remaining budget. Only the CORRECTABLE agents (moat, management,
        valuation) are ever re-run: the MOS agent runs once, after the loop.
        """
        total_rnd = state.get("iteration", 0) + 1
        phase_rnd = state.get(round_field, 0) + 1
        attempts = dict(state.get("finding_attempts") or {})
        plan = routing.plan_corrections(state["findings"], severity, attempts)
        for fs in plan.values():
            for f in fs:
                attempts[f.id] = attempts.get(f.id, 0) + 1
        phase = severity.lower()
        rstate = {**state, "iteration": total_rnd, round_field: phase_rnd}
        updates = [{"history": [_event(f"{severity} correction round {phase_rnd} of {max_rounds} "
                                       f"(overall round {total_rnd}): owners={sorted(plan)}")]}]
        updates += await self._parallel([self._execute(a, rstate, findings=plan[a], phase=phase) for a in plan])
        out = _merge(*updates)
        out["iteration"] = total_rnd
        out[round_field] = phase_rnd
        out["finding_attempts"] = attempts
        return out

    async def correct(self, state: dict) -> dict:
        """HIGH-severity correction round, also carrying the open MEDIUM findings. Cap: MAX_CORRECTIONS."""
        return await self._run_correction_round(state, severity="HIGH", round_field="high_iteration",
                                                 max_rounds=MAX_CORRECTIONS)

    async def correct_medium(self, state: dict) -> dict:
        """MEDIUM-only correction round. Cap: MAX_MEDIUM_CORRECTIONS. Only reached once no
        correctable HIGH finding remains (see routing.route_after_review)."""
        return await self._run_correction_round(state, severity="MEDIUM", round_field="medium_iteration",
                                                 max_rounds=MAX_MEDIUM_CORRECTIONS)

    def _flag(self, state: dict, severity: str, round_field: str, cap: int) -> tuple[list[dict], str]:
        """Record the open findings of `severity` as unresolved: the phase's round cap was reached, or every one was
        already sent back MAX_FINDING_ATTEMPTS times without being fixed. Returns (findings, history event); the
        run summary (run_summary.json) is where they are persisted."""
        unresolved = [f.model_dump() for f in routing.correctable(state["findings"], severity)]
        why = (f"{severity} correction limit ({cap}) reached" if state.get(round_field, 0) >= cap else
               f"every open {severity} issue was already sent back {MAX_FINDING_ATTEMPTS} times without being fixed")
        return unresolved, _event(f"{why}; {len(unresolved)} {severity} issue(s) flagged unresolved")

    async def flag_unresolved(self, state: dict) -> dict:
        """The loop ends with a HIGH issue open, so no MEDIUM-only round follows: every open correctable MEDIUM
        finding is unresolved too (recorded here, so a run that fails before the report still lists it)."""
        unresolved, event = self._flag(state, "HIGH", "high_iteration", MAX_CORRECTIONS)
        medium = [f.model_dump() for f in routing.correctable(state["findings"], "MEDIUM")]
        events = [event] + ([_event(f"no MEDIUM-only round runs while a HIGH issue is open; {len(medium)} MEDIUM "
                                    "issue(s) flagged unresolved")] if medium else [])
        return {"unresolved_high": unresolved, "unresolved_medium": medium, "history": events}

    async def flag_unresolved_medium(self, state: dict) -> dict:
        unresolved, event = self._flag(state, "MEDIUM", "medium_iteration", MAX_MEDIUM_CORRECTIONS)
        return {"unresolved_medium": unresolved, "history": [event]}

    async def mos(self, state: dict) -> dict:
        """Stage 4: the margin of safety analysis, run once on the final upstream analyses after the correction loop
        has ended. It is never re-run: the one-time MOS audit's issues are fixed by the report agent. The upstream
        issues the loop left open are passed on, so the analysis can take them into account."""
        _require_fresh_inputs(self.paths(state), "mos")
        return await self._execute("mos", state, upstream_open=_unresolved(state))

    async def mos_review(self, state: dict) -> dict:
        """Stage 5: the reviewer's one-time audit of the MOS analysis. Its findings never loop back: the HIGH and
        MEDIUM ones go to the report agent, which fixes them in the report."""
        paths = self.paths(state)
        _require_fresh_inputs(paths, "mos_review")
        upd = await self._execute("mos_review", state)
        rev = contracts.load_review(paths.sidecar("mos_review"))
        c = rev.counts
        upd["mos_findings"] = [f.model_dump() for f in rev.findings]
        upd["history"].append(_event(f"mos_review: {c.high} HIGH, {c.medium} MEDIUM, {c.low} LOW; HIGH and MEDIUM "
                                     "issues go to the report agent to fix (the MOS agent is not re-run)"))
        return upd

    async def report(self, state: dict) -> dict:
        paths = self.paths(state)
        _require_fresh_inputs(paths, "report")
        notes = [f for sev in ("HIGH", "MEDIUM") for f in routing.report_owned(state.get("findings", []), sev)]
        mos_fixes = [f for f in (contracts.Finding.model_validate(x) for x in state.get("mos_findings", []))
                     if f.severity in ("HIGH", "MEDIUM")]
        upd = await self._execute("report", state, unresolved=_unresolved(state), report_notes=notes,
                                  mos_fixes=mos_fixes)
        rep = contracts.load_report(paths.sidecar("report"))
        upd["reported_scores"] = {**rep.scores_reported, "business": rep.financial_quality_score}
        upd["score_changes"] = {k: c.model_dump() for k, c in (("mos", rep.mos_score_change),
                                                               ("business", rep.financial_quality_change)) if c}
        return upd

    async def finalize(self, state: dict) -> dict:
        paths = self.paths(state)
        state = {**state, "history": state.get("history", []) + ["workflow complete"]}
        summary = write_summary(paths, state)
        print(f"\nBuffett analysis for {state['company']} is {summary['workflow_status']}.")
        print(f"  Correction iterations: {summary['high_correction_iterations']} HIGH (also correcting open MEDIUM "
              f"issues), {summary['medium_correction_iterations']} MEDIUM-only")
        print(f"  Unresolved HIGH issues: {len(summary['unresolved_high_issues'])}")
        print(f"  Unresolved MEDIUM issues: {len(summary['unresolved_medium_issues'])}")
        print(f"  MOS audit issues passed to the report agent: {len(summary['mos_audit_issues_passed_to_report'])}")
        print(f"  Wall time: {(summary['wall_seconds'] or 0) / 60:.1f} min")
        print(f"  Report:  {summary['final_report']}")
        print(f"  Summary: {paths.rel(paths.reports_dir / 'run_summary.md')}")
        notify(f"Buffett: {state['company']} - analysis {summary['workflow_status']}",
               f"{summary['high_correction_iterations']} HIGH + {summary['medium_correction_iterations']} MEDIUM-only "
               f"correction round(s), {len(summary['unresolved_high_issues'])} unresolved HIGH, "
               f"{len(summary['unresolved_medium_issues'])} unresolved MEDIUM issue(s), "
               f"{len(summary['mos_audit_issues_passed_to_report'])} MOS audit issue(s) passed to the report agent. "
               f"Report: {summary['final_report']}")
        return {"history": ["workflow complete"]}
