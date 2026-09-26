"""Graph nodes. Every node = one bounded agent call + deterministic validation."""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path

from . import contracts, datapack, manifest, prompts, routing, validators
from .agent_runner import AgentRunner, AgentTask, Usage
from .config import (AGENT_FILE, ALL_AGENTS, ANALYSTS, DEPENDS, MAX_CORRECTIONS, MAX_MEDIUM_CORRECTIONS,
                     MAX_VALIDATION_RETRIES, RESEARCH_AGENTS, Paths)
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


class UsageLimitReached(NodeFailure):
    """Claude usage limit hit. Stop at once (retrying cannot help); resume with --resume after the reset."""


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
        prior_runs = state.get("status", {}).get(agent, {}).get("runs", 0)

        missing = [d for d in DEPENDS[agent] if not paths.output(d).exists()]
        if missing:
            raise NodeFailure(f"{agent}: upstream outputs missing: {missing}", agent)

        prior = state.get("status", {}).get(agent, {})
        done = manifest.completed_in_round(paths, agent, iteration) if iteration else None
        if done:  # finished earlier in this correction round; the node failed elsewhere, so don't redo it
            return self._result(agent, paths, prior_runs, done["attempts"], None, iteration,
                                _event(f"{agent}: already complete for round {iteration}; not re-run"), prior)

        sys_prompt = prompts.system_prompt(self.root, paths, agent, state["company"], iteration,
                                           high_iteration=high_iteration, medium_iteration=medium_iteration)
        base_prompt = prompts.user_prompt(paths, agent, state["company"], iteration=iteration,
                                          high_iteration=high_iteration, medium_iteration=medium_iteration,
                                          scores=state.get("scores"), **ctx)
        in_hashes = manifest.input_hashes(paths, agent)
        allowed = (paths.output(agent), paths.sidecar(agent))
        phase_tag = f" [{phase.upper()}]" if phase else ""
        label = f" (correction round {iteration}{phase_tag})" if iteration and agent in ANALYSTS else \
                f" (re-review after round {iteration})" if iteration and agent == "review" else ""

        # A validation rejection is sent back into the SAME session (the agent keeps everything it has already
        # read and only fixes what was rejected). A session that failed to execute is replaced by a fresh one
        # that gets the full task again. `started` marks the session start: files must be written after it.
        use = Usage()
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
                    errors = validators.validate(agent, paths, started, scores=state.get("scores"),
                                                 unresolved=ctx.get("unresolved") or [])
                else:
                    errors = [f"agent execution error: {res.error}"]
                    await session_stack.aclose()   # a broken session is not reused
                    session = None
                if not errors:
                    break
                _log(f"{agent}: attempt {attempt} rejected: {errors[0][:160]}")
        if errors:
            raise NodeFailure(f"{agent} failed after {attempts} attempts: " + "; ".join(errors), agent, use)

        manifest.record(paths, agent, in_hashes, iteration, attempts)
        return self._result(agent, paths, prior_runs, attempts, use, iteration,
                            _event(f"{agent}: complete (run {prior_runs + 1}, {attempts} attempt(s), "
                                   f"{use.describe()})"), prior)

    @staticmethod
    def _result(agent: str, paths: Paths, prior_runs: int, attempts: int, use: "Usage | None",
                iteration: int, event: str, prior: dict) -> dict:
        """`cost_usd`, `seconds` and `tokens` describe this run; `total_*` add up every run of the agent."""
        this = use.record() if use else {"cost_usd": None, "seconds": 0.0, "turns": 0, "tokens": {}}
        update: dict = {
            "status": {agent: {"state": "complete", "runs": prior_runs + 1, "attempts": attempts,
                               "correction_round": iteration, **this,
                               **Usage.accumulate(prior, this)}},
            "history": [event],
        }
        if agent in ANALYSTS:
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

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        old = [p for p in (*paths.research_dir.glob("*.md"), *paths.meta_dir.glob("*.json"),
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
        return {"iteration": 0, "high_iteration": 0, "medium_iteration": 0, "findings": [],
                "unresolved_high": [], "unresolved_medium": [],
                "history": [_event(f"input validated for {state['company']} -> key {state['key']}"
                                   + (f"; archived {len(old)} earlier files" if old else ""))]}

    async def data_pack(self, state: dict) -> dict:
        """Stage 0: deterministic SEC XBRL data pack shared by every agent. Never fails the run: if the data
        cannot be fetched, the pack says so and the agents research everything as before."""
        paths = self.paths(state)
        t0 = time.time()
        note = await asyncio.to_thread(datapack.build, paths, state["company"])
        return {"data_pack": note, "history": [_event(f"data pack: {note} ({time.time() - t0:.1f}s)")]}

    def agent_node(self, agent: str):
        async def node(state: dict) -> dict:
            return await self._execute(agent, state)
        return node

    async def review(self, state: dict) -> dict:
        paths = self.paths(state)
        stale = validators.check_fresh_inputs(paths, "review")
        if stale:
            raise NodeFailure(f"review blocked, stale inputs: {stale}", "review")
        prev = [contracts.Finding.model_validate(f) for f in state.get("findings", [])]
        upd = await self._execute("review", state, previous_findings=prev)
        rev = contracts.load_review(paths.sidecar("review"))
        n_high_correctable = sum(contracts.is_correctable(f, "HIGH") for f in rev.findings)
        n_medium_correctable = sum(contracts.is_correctable(f, "MEDIUM") for f in rev.findings)
        upd["findings"] = [f.model_dump() for f in rev.findings]
        upd["history"].append(_event(f"review: {rev.counts.high} HIGH ({n_high_correctable} correctable), "
                                     f"{rev.counts.medium} MEDIUM ({n_medium_correctable} correctable), "
                                     f"{rev.counts.low} LOW"))
        return upd

    async def _run_correction_round(self, state: dict, *, severity: str, round_field: str, max_rounds: int) -> dict:
        """One correction round acting only on findings of `severity`. Shared by `correct` (HIGH, cap
        MAX_CORRECTIONS) and `correct_medium` (MEDIUM, cap MAX_MEDIUM_CORRECTIONS). `iteration` is the
        single monotonic round id shared by both phases (kept unique across the whole correction stage,
        for manifest/resume bookkeeping); `round_field` is the phase-local counter (`high_iteration` or
        `medium_iteration`) that each phase's own cap is checked against in routing.py, so activity in one
        phase never changes the other phase's remaining budget.
        """
        paths = self.paths(state)
        total_rnd = state.get("iteration", 0) + 1
        phase_rnd = state.get(round_field, 0) + 1
        plan = routing.plan_corrections(state["findings"], severity)
        phase = severity.lower()
        rstate = {**state, "iteration": total_rnd, round_field: phase_rnd}
        first = [a for a in RESEARCH_AGENTS if a in plan]
        updates = [{"history": [_event(f"{severity} correction round {phase_rnd} of {max_rounds} "
                                       f"(overall round {total_rnd}): owners={sorted(plan)}")]}]
        if first:
            # Let every sibling finish (their results persist in the manifest) before surfacing a failure.
            results = await asyncio.gather(*(
                self._execute(a, rstate, findings=plan[a], phase=phase) for a in first), return_exceptions=True)
            failures = [r for r in results if isinstance(r, BaseException)]
            if failures:
                raise next((f for f in failures if isinstance(f, UsageLimitReached)), failures[0])
            updates += results
        # Downstream refresh comes from content hashes, not from a hand-written rule. A mos that already
        # finished this round (before an interruption) must still be merged into the state.
        if "mos" in plan or manifest.stale_agents(paths, ("mos",)) or manifest.completed_in_round(paths, "mos", total_rnd):
            merged = _merge(*updates)
            mstate = {**rstate, "status": {**state.get("status", {}), **merged["status"]},
                      "scores": {**state.get("scores", {}), **merged["scores"]}}
            updates.append(await self._execute("mos", mstate, findings=plan.get("mos", ()),
                                               upstream_changed=bool(first), phase=phase))
        out = _merge(*updates)
        out["iteration"] = total_rnd
        out[round_field] = phase_rnd
        return out

    async def correct(self, state: dict) -> dict:
        """HIGH-severity correction round. Cap: MAX_CORRECTIONS."""
        return await self._run_correction_round(state, severity="HIGH", round_field="high_iteration",
                                                 max_rounds=MAX_CORRECTIONS)

    async def correct_medium(self, state: dict) -> dict:
        """MEDIUM-severity correction round. Cap: MAX_MEDIUM_CORRECTIONS. Only reached once no
        correctable HIGH finding remains (see routing.route_after_review)."""
        return await self._run_correction_round(state, severity="MEDIUM", round_field="medium_iteration",
                                                 max_rounds=MAX_MEDIUM_CORRECTIONS)

    async def flag_unresolved(self, state: dict) -> dict:
        paths = self.paths(state)
        unresolved = [f.model_dump() for f in routing.correctable(state["findings"], "HIGH")]
        (paths.meta_dir / "unresolved_high.json").write_text(json.dumps(unresolved, indent=2), encoding="utf-8")
        return {"unresolved_high": unresolved,
                "history": [_event(f"correction limit ({MAX_CORRECTIONS}) reached; "
                                   f"{len(unresolved)} HIGH issue(s) flagged unresolved")]}

    async def flag_unresolved_medium(self, state: dict) -> dict:
        paths = self.paths(state)
        unresolved = [f.model_dump() for f in routing.correctable(state["findings"], "MEDIUM")]
        (paths.meta_dir / "unresolved_medium.json").write_text(json.dumps(unresolved, indent=2), encoding="utf-8")
        return {"unresolved_medium": unresolved,
                "history": [_event(f"MEDIUM correction limit ({MAX_MEDIUM_CORRECTIONS}) reached; "
                                   f"{len(unresolved)} MEDIUM issue(s) flagged unresolved")]}

    async def report(self, state: dict) -> dict:
        paths = self.paths(state)
        stale = validators.check_fresh_inputs(paths, "report")
        if stale:
            raise NodeFailure(f"report blocked, stale inputs: {stale}", "report")
        # Recomputed from the latest findings, not from state["unresolved_medium"]: if the HIGH cap was
        # reached first, route_after_review goes straight to report without ever running correct_medium
        # or flag_unresolved_medium, so a correctable MEDIUM finding can still be sitting unattended in
        # `findings` here. (No equivalent gap exists for HIGH: report is only ever reached with a
        # correctable HIGH finding still present after flag_unresolved has already populated
        # unresolved_high, since route_after_review never falls through to report or correct_medium while
        # one remains.)
        unresolved_medium = [f.model_dump() for f in routing.correctable(state.get("findings", []), "MEDIUM")]
        unresolved = [contracts.Finding.model_validate(f) for f in state.get("unresolved_high", []) + unresolved_medium]
        notes = [f for sev in ("HIGH", "MEDIUM") for f in routing.report_owned(state.get("findings", []), sev)]
        upd = await self._execute("report", state, unresolved=unresolved, report_notes=notes)
        upd["unresolved_medium"] = unresolved_medium  # authoritative as of report time, for run_summary.json
        return upd

    async def finalize(self, state: dict) -> dict:
        paths = self.paths(state)
        summary = write_summary(paths, state)
        print(f"\nBuffett analysis for {state['company']} is {summary['workflow_status']}.")
        print(f"  Correction iterations: {summary['high_correction_iterations']} HIGH, "
             f"{summary['medium_correction_iterations']} MEDIUM")
        print(f"  Unresolved HIGH issues: {len(summary['unresolved_high_issues'])}")
        print(f"  Unresolved MEDIUM issues: {len(summary['unresolved_medium_issues'])}")
        print(f"  Wall time: {(summary['wall_seconds'] or 0) / 60:.1f} min")
        print(f"  Report:  {summary['final_report']}")
        print(f"  Summary: {paths.rel(paths.reports_dir / 'run_summary.md')}")
        notify(f"Buffett: {state['company']} - analysis {summary['workflow_status']}",
               f"{summary['high_correction_iterations']} HIGH + {summary['medium_correction_iterations']} MEDIUM "
               f"correction round(s), {len(summary['unresolved_high_issues'])} unresolved HIGH, "
               f"{len(summary['unresolved_medium_issues'])} unresolved MEDIUM issue(s). "
               f"Report: {summary['final_report']}")
        return {"history": ["workflow complete"]}
