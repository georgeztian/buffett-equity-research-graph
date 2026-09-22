---
name: run-buffett-analysis
description: Execute the complete Buffett analysis workflow graph for a target company
---

# Run Buffett Analysis

Your job is to execute the complete Buffett analysis workflow for a target company by running the Python workflow graph.

/run-buffett-analysis --company "[Company Name | TICKER | Company Name (TICKER)]"

The workflow is a deterministic LangGraph implemented in `graph/`. Do NOT orchestrate the agents yourself or spawn the sub-agents manually; the graph owns all sequencing, parallelism, validation, the correction loop, and the final summary. The workflow definition lives only in code: `graph/config.py` (agents and dependencies), `graph/build_graph.py` (stages and edges), `graph/routing.py` (correction-loop rules).

## How to run

Follow the hard rule in `CLAUDE.md`: nothing outside the project folder, so no temp folders, scratchpads or background-task output files. A full run takes many minutes, so start it detached; the log stays inside the project:

```
.venv/Scripts/python -m graph --company "<company as the user gave it>" --detach
```

It returns immediately and prints the run id and the progress log path, `.state/logs/<RUN_ID>.log`. (On macOS/Linux use `.venv/bin/python`.) If `.venv` does not exist, first create it: `python -m venv .venv` then `.venv/Scripts/python -m pip install --no-cache-dir -r requirements.txt`.

The log holds timestamped progress lines (agent started / rejected / complete, review results, correction rounds). To see how the run is going, read that log and list the files in `research/<KEY>/`. The run is over when the log ends with COMPLETE, PAUSED or FAILED (also visible in `reports/<KEY>/run_summary.md`). There is no completion notification, so check the log when the user asks, and do not poll in a sleep loop. If the run fails or is interrupted, completed nodes are checkpointed; resume without re-running them:

```
.venv/Scripts/python -m graph --resume <RUN_ID> --detach
```

## Outputs (per company, folder key = ticker if given, otherwise a slug of the name)

- `research/<KEY>/moat.md`, `management.md`, `valuation.md`, `mos.md`, `review.md`
- `research/<KEY>/_meta/` — structured sidecars and content-hash manifest (machine-readable)
- `reports/<KEY>/final_investment_report.md`
- `reports/<KEY>/run_summary.md` and `run_summary.json` — workflow execution summary

## When the run ends

Read `reports/<KEY>/run_summary.md` and report to the user: workflow status, the execution status of every agent, the number of HIGH- and MEDIUM-severity correction iterations performed (separate caps: HIGH up to 5, MEDIUM up to 2 — MEDIUM is only ever attempted once every HIGH issue has actually been resolved; if HIGH still has an open issue when its own cap is reached, the run goes straight to the report with no MEDIUM correction attempted at all), any unresolved HIGH- or MEDIUM-severity issues, and the path of the final report. If the status is FAILED, report the error and offer to resume. If it is PAUSED (Claude usage limit reached, exit code 75), tell the user the run stopped safely, quote the reset time from the error, and give them the `--resume <RUN_ID>` command to run after the reset. Do not resume before the limit resets.

Follow all execution rules and data rules specified in `CLAUDE.md`. Do not edit the research or report files by hand.
