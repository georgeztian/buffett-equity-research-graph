"""Graph nodes. Every node = one bounded agent call + deterministic validation."""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from datetime import datetime
from pathlib import Path

from . import contracts, manifest, prompts, routing, validators
from .agent_runner import AgentRunner, AgentTask
from .config import (AGENT_FILE, ALL_AGENTS, ANALYSTS, DEPENDS, MAX_CORRECTIONS, MAX_VALIDATION_RETRIES,
                     RESEARCH_AGENTS, Paths)
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

    def __init__(self, message: str, agent: str | None = None):
        super().__init__(message)
        self.agent = agent


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
        iteration = state.get("iteration", 0)
        prior_runs = state.get("status", {}).get(agent, {}).get("runs", 0)

        missing = [d for d in DEPENDS[agent] if not paths.output(d).exists()]
        if missing:
            raise NodeFailure(f"{agent}: upstream outputs missing: {missing}", agent)

        done = manifest.completed_in_round(paths, agent, iteration) if iteration else None
        if done:  # finished earlier in this correction round; the node failed elsewhere, so don't redo it
            return self._result(agent, paths, prior_runs, done["attempts"], None, iteration,
                                _event(f"{agent}: already complete for round {iteration}; not re-run"))

        sys_prompt = prompts.system_prompt(self.root, paths, agent, state["company"], iteration)
        base_prompt = prompts.user_prompt(paths, agent, state["company"], iteration=iteration,
                                          scores=state.get("scores"), **ctx)
        in_hashes = manifest.input_hashes(paths, agent)
        allowed = (paths.output(agent), paths.sidecar(agent))

        prompt, errors, attempts, cost = base_prompt, [], 0, 0.0
        for attempt in range(1, MAX_VALIDATION_RETRIES + 2):
            attempts = attempt
            started = time.time()
            label = f" (correction round {iteration})" if iteration and agent in ANALYSTS else \
                    f" (re-review after round {iteration})" if iteration and agent == "review" else ""
            _log(f"{agent}: started{label}" + (f", attempt {attempt}" if attempt > 1 else ""))
            res = await self.runner.run(AgentTask(agent, sys_prompt, prompt, self.root, allowed))
            cost += res.cost_usd or 0.0
            if res.usage_limit:
                raise UsageLimitReached(f"{agent}: {res.error}", agent)
            errors = [f"agent execution error: {res.error}"] if not res.ok else validators.validate(
                agent, paths, started, scores=state.get("scores"),
                unresolved=ctx.get("unresolved") or [])
            if not errors:
                break
            _log(f"{agent}: attempt {attempt} rejected: {errors[0][:160]}")
            prompt = (base_prompt + "\n\n## YOUR PREVIOUS ATTEMPT WAS REJECTED\nFix these problems and "
                      "re-save the required files:\n" + "\n".join(f"- {e}" for e in errors))
        if errors:
            raise NodeFailure(f"{agent} failed after {attempts} attempts: " + "; ".join(errors), agent)

        manifest.record(paths, agent, in_hashes, iteration, attempts)
        return self._result(agent, paths, prior_runs, attempts, round(cost, 4), iteration,
                            _event(f"{agent}: complete (run {prior_runs + 1}, {attempts} attempt(s))"))

    @staticmethod
    def _result(agent: str, paths: Paths, prior_runs: int, attempts: int, cost: float | None,
                iteration: int, event: str) -> dict:
        update: dict = {
            "status": {agent: {"state": "complete", "runs": prior_runs + 1, "attempts": attempts,
                               "cost_usd": cost, "correction_round": iteration}},
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
        for ref in ("economic_moat", "management_quality", "margin_of_safety", "valuation"):
            if not (self.root / ".claude/skills/buffett-analysis/references" / f"{ref}.md").exists():
                raise NodeFailure(f"missing reference file: {ref}.md")

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        old = [p for p in (*paths.research_dir.glob("*.md"), *paths.meta_dir.glob("*.json")) if p.is_file()]
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
        return {"iteration": 0, "findings": [], "unresolved_high": [],
                "history": [_event(f"input validated for {state['company']} -> key {state['key']}"
                                   + (f"; archived {len(old)} earlier files" if old else ""))]}

    def agent_node(self, agent: str):
        async def node(state: dict) -> dict:
            return await self._execute(agent, state)
        return node

    async def review(self, state: dict) -> dict:
        paths = self.paths(state)
        stale = validators.check_fresh_inputs(paths, "review")
        if stale:
            raise NodeFailure(f"review blocked, stale inputs: {stale}", "review")
        upd = await self._execute("review", state)
        rev = contracts.load_review(paths.sidecar("review"))
        n_correctable = sum(contracts.is_correctable(f) for f in rev.findings)
        upd["findings"] = [f.model_dump() for f in rev.findings]
        upd["history"].append(_event(f"review: {rev.counts.high} HIGH ({n_correctable} correctable), "
                                     f"{rev.counts.medium} MEDIUM, {rev.counts.low} LOW"))
        return upd

    async def correct(self, state: dict) -> dict:
        paths = self.paths(state)
        rnd = state.get("iteration", 0) + 1
        plan = routing.plan_corrections(state["findings"])
        rstate = {**state, "iteration": rnd}
        first = [a for a in RESEARCH_AGENTS if a in plan]
        updates = [{"history": [_event(f"correction round {rnd}: owners={sorted(plan)}")]}]
        if first:
            # Let every sibling finish (their results persist in the manifest) before surfacing a failure.
            results = await asyncio.gather(*(
                self._execute(a, rstate, findings=plan[a]) for a in first), return_exceptions=True)
            failures = [r for r in results if isinstance(r, BaseException)]
            if failures:
                raise next((f for f in failures if isinstance(f, UsageLimitReached)), failures[0])
            updates += results
        # Downstream refresh comes from content hashes, not from a hand-written rule. A mos that already
        # finished this round (before an interruption) must still be merged into the state.
        if "mos" in plan or manifest.stale_agents(paths, ("mos",)) or manifest.completed_in_round(paths, "mos", rnd):
            merged = _merge(*updates)
            mstate = {**rstate, "status": {**state.get("status", {}), **merged["status"]},
                      "scores": {**state.get("scores", {}), **merged["scores"]}}
            updates.append(await self._execute("mos", mstate, findings=plan.get("mos", ()),
                                               upstream_changed=bool(first)))
        out = _merge(*updates)
        out["iteration"] = rnd
        return out

    async def flag_unresolved(self, state: dict) -> dict:
        paths = self.paths(state)
        unresolved = [f.model_dump() for f in routing.correctable_high(state["findings"])]
        (paths.meta_dir / "unresolved_high.json").write_text(json.dumps(unresolved, indent=2), encoding="utf-8")
        return {"unresolved_high": unresolved,
                "history": [_event(f"correction limit ({MAX_CORRECTIONS}) reached; "
                                   f"{len(unresolved)} HIGH issue(s) flagged unresolved")]}

    async def report(self, state: dict) -> dict:
        paths = self.paths(state)
        stale = validators.check_fresh_inputs(paths, "report")
        if stale:
            raise NodeFailure(f"report blocked, stale inputs: {stale}", "report")
        unresolved = [contracts.Finding.model_validate(f) for f in state.get("unresolved_high", [])]
        notes = routing.report_owned_high(state.get("findings", []))
        return await self._execute("report", state, unresolved=unresolved, report_notes=notes)

    async def finalize(self, state: dict) -> dict:
        paths = self.paths(state)
        summary = write_summary(paths, state)
        print(f"\nBuffett analysis for {state['company']} is {summary['workflow_status']}.")
        print(f"  Correction iterations: {summary['correction_iterations']}")
        print(f"  Unresolved HIGH issues: {len(summary['unresolved_high_issues'])}")
        print(f"  Report:  {summary['final_report']}")
        print(f"  Summary: {paths.rel(paths.reports_dir / 'run_summary.md')}")
        notify(f"Buffett: {state['company']} - analysis {summary['workflow_status']}",
               f"{summary['correction_iterations']} correction round(s), "
               f"{len(summary['unresolved_high_issues'])} unresolved HIGH issue(s). "
               f"Report: {summary['final_report']}")
        return {"history": ["workflow complete"]}
