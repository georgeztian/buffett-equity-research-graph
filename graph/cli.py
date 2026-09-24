"""python -m graph --company "American Express (AXP)" [--detach] | --resume RUN_ID | --print-graph"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .agent_runner import SdkRunner, Usage
from .build_graph import build_graph
from .config import ROOT, Paths, checkpoint_db_path, company_key, key_of, log_path, validate_run_id
from .nodes import NodeFailure, UsageLimitReached
from .notify import notify
from .summary import write_summary

EXIT_USAGE_LIMIT = 75  # distinct exit code: paused by a usage limit, resumable


def _exclude_from_dropbox(folder: Path) -> None:
    """Best effort (Windows/NTFS only): mark the in-project state folder as not synced, so Dropbox
    cannot lock the checkpoint database. Only touches a stream on the folder itself."""
    if os.name == "nt":
        try:
            Path(f"{folder}:com.dropbox.ignored").write_text("1")
        except OSError:
            pass


async def run(company: str | None, run_id: str, resume: bool) -> int:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    key = key_of(run_id)
    try:
        paths = Paths(ROOT, key)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    db = checkpoint_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    _exclude_from_dropbox(db.parent)
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 100}

    async with AsyncSqliteSaver.from_conn_string(str(db)) as saver:
        graph = build_graph(SdkRunner(), ROOT, saver)
        if resume:
            snap = await graph.aget_state(config)
            if not snap.values:
                print(f"No checkpoint found for run id {run_id}", file=sys.stderr)
                return 2
            if not snap.next:
                print(f"Run {run_id} already finished; see {paths.rel(paths.reports_dir / 'run_summary.md')}")
                return 0
            inp = None
        else:
            inp = {"company": company, "key": key, "run_id": run_id,
                   "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        print(f"Run id: {run_id}  (resume with: python -m graph --resume {run_id})", flush=True)
        try:
            await graph.ainvoke(inp, config)
            return 0
        except Exception as e:
            snap = await graph.aget_state(config)
            values = dict(snap.values)
            msg = f"{type(e).__name__}: {e}"
            paused = isinstance(e, UsageLimitReached)
            if isinstance(e, NodeFailure) and e.agent:   # show the stopped agent, not 'not_run'
                prior = values.get("status", {}).get(e.agent, {})
                spent = Usage.accumulate(prior, e.usage.record()) if e.usage else {}
                values["status"] = {**values.get("status", {}), e.agent: {
                    **prior, **spent, "state": "paused" if paused else "failed"}}
            write_summary(paths, values, error=msg, paused=paused)
            resume_cmd = f"python -m graph --resume {run_id}"
            notify(f"Buffett: {values.get('company') or company} - workflow {'PAUSED' if paused else 'FAILED'}",
                   f"{msg[:180]} Resume: {resume_cmd}")
            if paused:
                print(f"\nWorkflow PAUSED: Claude usage limit reached ({msg}).\nNothing is lost: completed work is "
                      f"checkpointed. After the limit resets, continue with:\n  {resume_cmd}", file=sys.stderr)
                return EXIT_USAGE_LIMIT
            print(f"\nWorkflow FAILED: {msg}\nCompleted nodes are checkpointed; resume with: {resume_cmd}",
                  file=sys.stderr)
            return 1


def detach(run_id: str, child_args: list[str]) -> int:
    """Start the run as a separate process whose output goes to a log file INSIDE the project
    (.state/logs/), so nothing needs a temp or output file outside the project folder."""
    try:
        log = log_path(run_id)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    log.parent.mkdir(parents=True, exist_ok=True)
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    with open(log, "ab") as out:
        subprocess.Popen([sys.executable, "-u", "-m", "graph", *child_args], cwd=ROOT, stdin=subprocess.DEVNULL,
                         stdout=out, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True,
                         start_new_session=os.name != "nt")
    rel = log.relative_to(ROOT).as_posix()
    print(f"Started detached run {run_id}\nProgress log: {rel}\nSummary when done: "
          f"reports/{key_of(run_id)}/run_summary.md")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m graph", description="Deterministic Buffett analysis graph")
    ap.add_argument("--company", help='"Company Name", "TICKER" or "Company Name (TICKER)"')
    ap.add_argument("--resume", metavar="RUN_ID", help="resume a failed/interrupted run from its checkpoint")
    ap.add_argument("--detach", action="store_true",
                    help="run in the background, logging to .state/logs/<RUN_ID>.log inside the project")
    ap.add_argument("--run-id", help=argparse.SUPPRESS)   # internal: lets --detach fix the id up front
    ap.add_argument("--print-graph", action="store_true", help="print the graph as a Mermaid diagram and exit")
    a = ap.parse_args(argv)

    if a.print_graph:
        print(build_graph(SdkRunner(), ROOT).get_graph().draw_mermaid())
        return 0
    if not a.company and not a.resume:
        ap.error("--company is required (or --resume RUN_ID)")

    resume = bool(a.resume)
    try:
        if resume:
            run_id, child = validate_run_id(a.resume), ["--resume", a.resume]
        else:
            run_id = validate_run_id(a.run_id or f"{company_key(a.company)}-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
            child = ["--company", a.company, "--run-id", run_id]
    except ValueError as e:   # bad run id (e.g. path traversal) or company with no usable folder key
        print(e, file=sys.stderr)
        return 2
    if a.detach:
        return detach(run_id, child)
    return asyncio.run(run(a.company, run_id, resume))
