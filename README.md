# Buffett Equity Research


## Overview

A multi-agent research workflow for Claude Code that analyzes a publicly traded company as a potential long-term investment, using Warren Buffett's documented investment philosophy. You provide a company name and/or ticker. The workflow produces an equity research report.


## How it works

A deterministic data step (Stage 0) is followed by five agent stages, and no agent starts until the analyses it depends on are complete.

0. **SEC data pack:** before the agents start, the graph pulls the company's annual XBRL financials from SEC EDGAR once and writes `research/<KEY>/_data/financials.md`: values exactly as filed with their filing references, plus a few reference calculations with their formulas. Every agent reads the same numbers instead of each fetching and re-deriving them. If the company is not an SEC filer with US-GAAP XBRL data, or no SEC contact is set, the pack says so and the agents research as before. SEC requires each user to identify themselves with a name and contact email, so the data pack is only built once you have given yours (optional; see the setup below).
1. **Research (in parallel):** the moat, management, and valuation agents each analyze one aspect of the company.
2. **Margin of safety:** the MOS agent compares the current price with the estimated intrinsic value, using the Stage 1 results.
3. **Independent review:** a reviewer agent audits all research and rates every problem HIGH, MEDIUM, or LOW.
4. **Correction loop:** if there are HIGH-severity issues, only the affected agents are re-run (plus any downstream agents), then the reviewer checks again. This repeats up to 5 times. If a HIGH issue is still open after the 5th round, the workflow stops correcting and goes straight to Stage 5 with it flagged unresolved — MEDIUM issues are not attempted in that case (any that are open are flagged unresolved in the report). Only once every HIGH issue in the analyses has actually been resolved does the same loop run again for MEDIUM-severity issues, up to 2 times; a MEDIUM issue still open after that is likewise flagged unresolved. LOW-severity issues are never corrected. Issues the reviewer assigns to the final report itself (for example presentation, or the Financial Quality section the report agent writes) do not loop back; they are passed to the report agent to address in Stage 5.
5. **Report:** the report agent writes the final investment report once the review has passed, or once a correction loop has run out of rounds with issues of its severity still open (HIGH takes priority: reaching its cap goes straight to Stage 5 without any MEDIUM attempt).


## Deterministic graph

The workflow is implemented as a LangGraph in `graph/`. **Code owns control flow; the LLM owns content.** Ordering, parallelism, completion checks, review routing, the two correction loops (HIGH: 5-iteration cap; MEDIUM: 2-iteration cap, attempted only once every HIGH issue is actually resolved — never merely because HIGH's own cap was reached), staleness detection and the final summary are all plain Python. Each agent is one bounded call through the Claude Agent SDK, and its output is accepted only after deterministic validation. If validation rejects an output, the list of problems goes back into the same agent session, so the agent fixes only what was rejected instead of starting over; a session that fails to execute is replaced by a fresh one.


## Architecture

```
.claude/
  agents/      Six agents: moat, management, valuation, MOS, reviewer, report
  skills/      Buffett analysis methodology (with reference files) and the run skill
  settings.json  Pre-approved commands for running the graph
graph/           LangGraph implementation: nodes, routing, validators, contracts, runner, SEC data pack, CLI
research/<KEY>/  Intermediate analyses and the review, per company (gitignored); _data/ SEC data pack,
                 _meta/ sidecars and manifest, _archive/ outputs of earlier runs (moved aside by a fresh run)
reports/<KEY>/   Final investment report and run summary, per company (gitignored)
.state/          Checkpoint database and detached-run logs (gitignored)
requirements.txt Python dependencies
.env.example     Template for your own SEC contact; the real .env is per user and gitignored
CLAUDE.md        Project rules for Claude
LICENSE          MIT license
```


## How to use

One-time setup, with Python 3.11 or newer (on macOS/Linux, use `.venv/bin/python` wherever `.venv/Scripts/python` appears below):

```
python -m venv .venv
.venv/Scripts/python -m pip install --no-cache-dir -r requirements.txt
.venv/Scripts/python -m graph --set-sec-contact "Your Name you@yourdomain.com"   # optional, recommended
```

The last line is optional but recommended: it enables the SEC data pack. SEC EDGAR asks every automated client to identify itself with a name and contact email in the User-Agent header, so the graph never sends SEC a request without yours. Use your own name and email. They are saved only in this project's `.env` file, which is gitignored (so it is never committed or shared), and are sent only to SEC. You can also copy `.env.example` to `.env` and edit it, or set the `SEC_USER_AGENT` environment variable, which takes precedence over `.env`.

If no contact is set, a run started from a terminal asks for it once; press Enter to skip. Skipping is remembered (`SEC_USER_AGENT=declined` in `.env`, or `--set-sec-contact declined`), so you are not asked again. Runs without a contact, including detached runs and runs started by Claude, still go ahead: Stage 0 is skipped, the agents research all figures as before, and the run summary says the data pack was not used. To enable it later, run `--set-sec-contact` with your name and email.

In Claude Code, from this project, run:

```
/run-buffett-analysis --company "Company Name (Ticker)"
```

or directly:

```
.venv/Scripts/python -m graph --company "Company Name (Ticker)"            # run in the foreground
.venv/Scripts/python -m graph --company "Company Name (Ticker)" --detach   # run in the background; log in .state/logs/
.venv/Scripts/python -m graph --resume <RUN_ID>                            # resume a failed/interrupted/paused run
.venv/Scripts/python -m graph --print-graph                                # Mermaid diagram of the graph
.venv/Scripts/python -m graph --set-sec-contact "Your Name you@yourdomain.com"   # save your SEC contact to .env
.venv/Scripts/python -m graph --set-sec-contact declined                         # run without it; don't ask again
```

The project's `.claude/settings.json` pre-approves `python -m graph …` and the requirements install, so `/run-buffett-analysis` does not prompt for each one. The company can be given as a company name, a ticker, or both. If `claude` is not on PATH, the runner falls back to the binary bundled with the VS Code extension; set `CLAUDE_CLI_PATH` to override.

While it runs (or in `.state/logs/<RUN_ID>.log` with `--detach`), the graph prints timestamped progress lines (each agent starting, being rejected and retried, completing, and the review and correction decisions). When the run finishes (or fails, or pauses at a Claude usage limit), a desktop notification says so, and you get the final report in `reports/<KEY>/final_investment_report.md` and a workflow summary in `reports/<KEY>/run_summary.md`.


## Disclaimer

This tool provides AI-generated equity analysis for informational purposes only. It does not constitute financial, investment, or trading advice. The output is based on algorithms and historical data and should not be relied upon as a sole source for making investment decisions. Always conduct your own research and consult with a licensed financial advisor before investing.
