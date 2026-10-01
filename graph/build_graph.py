"""Wires the workflow (Stage 0 data pack, then six agent stages) into a LangGraph StateGraph."""
from __future__ import annotations

from pathlib import Path

from langgraph.graph import END, START, StateGraph

from .agent_runner import AgentRunner
from .config import ROOT
from .nodes import Workflow
from .routing import route_after_review
from .state import GraphState


def build_graph(runner: AgentRunner, root: Path = ROOT, checkpointer=None):
    wf = Workflow(runner, root)
    g = StateGraph(GraphState)

    g.add_node("validate_input", wf.validate_input)
    g.add_node("data_pack", wf.data_pack)          # Stage 0 (deterministic SEC data, shared by all agents)
    g.add_node("research", wf.research)            # Stage 1 (moat, management, valuation, business in parallel)
    g.add_node("review", wf.review)                # Stage 2
    g.add_node("correct", wf.correct)              # Stage 3  (HIGH, cap MAX_CORRECTIONS)
    g.add_node("flag_unresolved", wf.flag_unresolved)
    g.add_node("correct_medium", wf.correct_medium)             # Stage 3b (MEDIUM, cap MAX_MEDIUM_CORRECTIONS)
    g.add_node("flag_unresolved_medium", wf.flag_unresolved_medium)
    g.add_node("mos", wf.mos)                      # Stage 4 (once, after the correction loop; never re-run)
    g.add_node("mos_review", wf.mos_review)        # Stage 5 (one-time audit of the MOS analysis)
    g.add_node("report", wf.report)                # Stage 6 (also fixes the MOS audit's issues)
    g.add_node("finalize", wf.finalize)

    g.add_edge(START, "validate_input")
    g.add_edge("validate_input", "data_pack")
    g.add_edge("data_pack", "research")
    g.add_edge("research", "review")
    # HIGH is checked first; MEDIUM is only ever considered once no correctable HIGH finding remains, and
    # the two loops share the same "review" node (every pass records the open findings of every severity).
    g.add_conditional_edges("review", route_after_review, {
        "correct": "correct",
        "flag_unresolved": "flag_unresolved",
        "correct_medium": "correct_medium",
        "flag_unresolved_medium": "flag_unresolved_medium",
        "mos": "mos",
    })
    g.add_edge("correct", "review")
    g.add_edge("flag_unresolved", "mos")
    g.add_edge("correct_medium", "review")
    g.add_edge("flag_unresolved_medium", "mos")
    g.add_edge("mos", "mos_review")
    g.add_edge("mos_review", "report")
    g.add_edge("report", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
