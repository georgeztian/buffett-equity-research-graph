# Buffett Equity Research


## Overview

A multi-agent research workflow for Claude Code that analyzes a publicly traded company as a potential long-term investment, using Warren Buffett's documented investment philosophy. You provide a company name and/or ticker. The workflow produces an equity research report.


## How it works

A deterministic data step (Stage 0) is followed by six agent stages, and no agent starts until the analyses it depends on are complete.

0. **SEC data pack:** before the agents start, the graph pulls the company's annual XBRL financials from SEC EDGAR once and writes `research/<KEY>/_data/financials.md`: values exactly as filed with their filing references, plus a few reference calculations with their formulas. Every agent reads the same numbers instead of each fetching and re-deriving them. If the company is not an SEC filer with US-GAAP XBRL data, or no SEC contact is set, the pack says so and the agents research as before. SEC requires each user to identify themselves with a name and contact email, so the data pack is only built once you have given yours (optional; see the setup below).
1. **Research (in parallel):** the moat, management, and valuation agents each analyze one aspect of the company, and the business agent researches the company overview, business model, and financial quality for the report. If one agent fails, the others still finish, and a resume re-runs only the one that failed.
2. **Independent review:** an independent reviewer agent audits the Stage 1 research and rates every problem HIGH, MEDIUM, or LOW. Before it runs, the code checks each analysis mechanically, and a mismatch goes straight back to that agent: the stated score must match the sidecar, and the valuation's share price must be current (at most 14 days old) and appear in the text.
3. **Correction loop:** if there are HIGH-severity issues, only the affected agents (moat, management, valuation) are re-run, then the reviewer checks again; this repeats up to 5 times. Every open MEDIUM issue is sent back in the same round as the HIGH ones, so all affected agents fix both in parallel instead of waiting for a later round. If a HIGH issue is still open after the 5th round, or none that is still open can be sent back again, the workflow stops correcting and moves on to Stage 4 with it flagged unresolved — no MEDIUM-only round is run in that case (any MEDIUM issues still open are flagged unresolved in the report). Only once every HIGH issue in the analyses has actually been resolved does the loop run MEDIUM-only rounds for the MEDIUM issues still open, up to 2 times; a MEDIUM issue still open after that is likewise flagged unresolved. To keep rounds few, every analyst self-checks its file against the reviewer's own checklist before saving, and a correction run also checks that what it changed agrees with the other research files. In both loops, an issue still open after being sent back twice is not sent again, a round in which no research file changed is not re-reviewed, and a re-review is limited to what changed: the reviewer gets the diff of every file changed since its last pass, verifies the earlier findings, and audits the changed passages and whatever depends on them, not the unchanged text. LOW-severity issues are never corrected. Issues the reviewer assigns to the final report itself (for example presentation, or anything in the business agent's analysis) do not loop back; they are passed to the report agent to address in Stage 6. That is, unlike the moat, management, and valuation agents, the business agent is never re-run to correct its analysis: HIGH and MEDIUM issues in its analysis are fixed by the report agent in the report.
4. **Margin of safety:** once the correction loop is over, the MOS agent runs once. It compares the current price with the estimated intrinsic value from the final valuation, and judges how large a margin of safety is required to invest, based on the final moat, management, and valuation analyses and on any issue the review left open in them. The code checks it mechanically, and a mismatch goes straight back to the MOS agent: it must use exactly the valuation's price and date, an intrinsic value within the valuation's range, and a margin of safety computed from them.
5. **MOS audit:** the independent reviewer audits the MOS analysis once (`research/<KEY>/mos_review.md`) and rates every problem HIGH, MEDIUM, or LOW. The MOS agent is never re-run to correct its analysis: the audit's HIGH and MEDIUM issues are passed to the report agent to fix in Stage 6.
6. **Report:** the report agent writes the final investment report. It builds the Company Overview, Business Model, and Financial Quality sections from the business analysis, and fixes the MOS audit's issues in the Margin of Safety section and everything that depends on it. If the MOS score has to change, the report states both the MOS agent's score and the corrected one, and why. Issues the correction loop left open are flagged as unresolved.


## Deterministic graph

The workflow is implemented as a LangGraph in `graph/`. **Code owns control flow; the LLM owns content.** Ordering, parallelism, completion checks, review routing, the correction loop (HIGH rounds, 5-iteration cap, each also carrying every open MEDIUM issue; MEDIUM-only rounds, 2-iteration cap, run only once every HIGH issue is actually resolved — never merely because HIGH's own cap was reached; each issue is sent back at most twice), running the MOS agent and its one-time audit only after the loops end, routing the audit's issues to the report, staleness detection by file content, scoped re-reviews, mechanical consistency checks and the final summary are all plain Python. Each agent is one bounded call through the Claude Agent SDK, and its output is accepted only after deterministic validation. If validation rejects an output, the list of problems goes back into the same agent session, so the agent fixes only what was rejected instead of starting over; a session that fails to execute is replaced by a fresh one.


## Architecture

```
.claude/
  agents/      Seven agents: moat, management, valuation, business, MOS, reviewer (also audits the MOS analysis), report
  skills/      Buffett analysis methodology (with reference files) and the run skill
  settings.json  Pre-approved commands for running the graph
graph/           LangGraph implementation: nodes, routing, validators, contracts, runner, SEC data pack, CLI
research/<KEY>/  Intermediate analyses, the review and the MOS audit, per company (gitignored); _data/ SEC data pack,
                 _meta/ sidecars, manifest and the last reviewed copies, _archive/ outputs of earlier runs (moved aside by a fresh run)
reports/<KEY>/   Final investment report and run summary, per company (gitignored)
.state/          Checkpoint database and detached-run logs (gitignored)
requirements.txt Python dependencies
.env.example     Template for your own SEC contact; the real .env is per user and gitignored
CLAUDE.md        Project rules for Claude
LICENSE          Apache 2.0 license
```


## How to use it

One-time setup, with Python 3.11 or newer (on macOS/Linux, use `.venv/bin/python` wherever `.venv/Scripts/python` appears below):

```
python -m venv .venv
.venv/Scripts/python -m pip install --no-cache-dir -r requirements.txt
.venv/Scripts/python -m graph --set-sec-contact "Your Name you@yourdomain.com"   # optional, recommended
```

The last line is optional but recommended: it enables the SEC data pack. SEC EDGAR asks every automated client to identify itself with a name and contact email in the User-Agent header, so the graph never sends SEC a request without yours. Use your own name and email. They are saved only in this project's `.env` file, which is gitignored (so it is never committed or shared), and are sent only to SEC. Alternatively, 1) copy/rename `.env.example` to `.env` and put in your name and contact email, or 2) set the `SEC_USER_AGENT` environment variable, which takes precedence over `.env`.

If no contact is set, a run started from a terminal asks for it once; press Enter to skip. Skipping is remembered (`SEC_USER_AGENT=declined` in `.env`, or `--set-sec-contact declined`), so you are not asked again. Runs without a contact, including detached runs and runs started by Claude, still go ahead: Stage 0 sends no request to SEC and the data pack just says it is unavailable, the agents research all figures as before, and the run summary says the data pack was not used. To enable it later, run `--set-sec-contact` with your name and email.

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


## License

This project is licensed under the Apache License 2.0. See [LICENSE](LICENSE).


## Disclaimer

This tool provides AI-generated equity analysis for informational purposes only. It does not constitute financial, investment, or trading advice. The output is based on algorithms and historical data and should not be relied upon as a sole source for making investment decisions. Always conduct your own research and consult with a licensed financial advisor before investing.
