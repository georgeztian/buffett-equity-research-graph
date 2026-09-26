# Buffett Investment Research


## Project Purpose

This project analyzes a publicly traded company as a potential long-term investment using Warren Buffett's documented investment philosophy.

The user provides the target company/ticker.

Use the buffett-analysis skill for the research methodology.
Execute the workflow via the run-buffett-analysis skill, which runs the deterministic Python graph (`python -m graph --company "..."`). Do not orchestrate the agents by hand.


## Project structure

- `.claude/agents/` — contains specialized sub-agents responsible for specific research and analysis tasks. The graph runs them; `<KEY>` in their paths is the company folder key.
- `.claude/skills/buffett-analysis` — contains the reusable Buffett-based investment analysis methodology and instructions for applying it.
- `.claude/skills/buffett-analysis/references` — contains detailed reference materials that support and expand the Buffett analysis methodology.
- `.claude/skills/run-buffett-analysis/` — the invocation interface; runs the Python graph
- `.claude/settings.json` — pre-approved commands for running the graph
- `graph/` — the LangGraph implementation of the 5-stage Buffett workflow and the single source of truth for it (routing, validation, correction loop, summary)
- `.state/` — runtime state: the checkpoint database and detached-run logs (gitignored)
- `.env` — per-user settings, currently the user's own SEC EDGAR contact (`SEC_USER_AGENT`); gitignored, template in `.env.example`. Never fill it with anything but what the user provides.
- `research/<KEY>/` — intermediate research and analysis results per company (`<KEY>` = ticker, or a slug of the name); `_meta/` holds machine-readable sidecars; `_data/` holds the deterministic SEC XBRL data pack (Stage 0) shared by all agents
- `reports/<KEY>/` — includes final investment reports intended for end users and the run summary; only analyses that have passed the review should be incorporated into final reports.


## HARD RULE — project folder only

**Never edit, create, or delete anything outside the project folder** (including home, temp, scratchpad, config and cache folders). Only an explicit user authorization for one specific operation is an exception, and it covers only that operation. Keep all files, logs and checkpoints inside the project; if a task seems to need writing elsewhere, stop and ask.


## Execution Rules

- Do not skip workflow dependencies.
- Do not run a downstream agent before its required upstream analyses are complete. 
- Execute independent tasks concurrently whenever supported.
- Do not unnecessarily rerun completed agents.
- Criteria for "complete": An output file existing alone does not mean the task is complete. An agent task is complete only when all of the following conditions are satisfied:
    - The agent finishes successfully without a material execution error.
    - The required output file exists at the specified path.
    - The output contains all required analyses, evidence, calculations, and conclusions specified in the agent's instructions.
    - Required upstream inputs and references have been processed.
- Do not silently resolve contradictions between agents. Identify and address them through the correction process.
- Do not alter CLAUDE.md, SKILL.md, references, agent definitions, or the workflow graph (`graph/`) unless explicitly authorized by the user.


## Data Rules

Distinguish clearly between:
- Buffett's documented views
- factual company information
- calculations
- inferences
- investment judgments

Never attribute an idea to Buffett without supporting evidence.
Never modify raw data.