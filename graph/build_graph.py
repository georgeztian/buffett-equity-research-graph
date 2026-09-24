"""Wires the five-stage workflow into a LangGraph StateGraph."""
from __future__ import annotations

from pathlib import Path

from langgraph.graph import END, START, StateGraph

from .agent_runner import AgentRunner
from .config import RESEARCH_AGENTS, ROOT
from .nodes import Workflow
from .routing import route_after_review
from .state import GraphState


def build_graph(runner: AgentRunner, root: Path = ROOT, checkpointer=None):
    wf = Workflow(runner, root)
    g = StateGraph(GraphState)

    g.add_node("validate_input", wf.validate_input)
    g.add_node("data_pack", wf.data_pack)          # Stage 0 (deterministic SEC data, shared by all agents)
    for a in RESEARCH_AGENTS:                      # Stage 1 (parallel fan-out)
        g.add_node(a, wf.agent_node(a))
    g.add_node("mos", wf.agent_node("mos"))        # Stage 2
    g.add_node("review", wf.review)                # Stage 3
    g.add_node("correct", wf.correct)              # Stage 4  (HIGH, cap MAX_CORRECTIONS)
    g.add_node("flag_unresolved", wf.flag_unresolved)
    g.add_node("correct_medium", wf.correct_medium)             # Stage 4b (MEDIUM, cap MAX_MEDIUM_CORRECTIONS)
    g.add_node("flag_unresolved_medium", wf.flag_unresolved_medium)
    g.add_node("report", wf.report)                # Stage 5
    g.add_node("finalize", wf.finalize)

    g.add_edge(START, "validate_input")
    g.add_edge("validate_input", "data_pack")
    for a in RESEARCH_AGENTS:
        g.add_edge("data_pack", a)
    g.add_edge(list(RESEARCH_AGENTS), "mos")       # join: waits for all three
    g.add_edge("mos", "review")
    # HIGH is checked first; MEDIUM is only ever considered once no correctable HIGH finding remains, and
    # the two loops share the same "review" node (it re-audits every severity on every pass).
    g.add_conditional_edges("review", route_after_review, {
        "correct": "correct",
        "flag_unresolved": "flag_unresolved",
        "correct_medium": "correct_medium",
        "flag_unresolved_medium": "flag_unresolved_medium",
        "report": "report",
    })
    g.add_edge("correct", "review")
    g.add_edge("flag_unresolved", "report")
    g.add_edge("correct_medium", "review")
    g.add_edge("flag_unresolved_medium", "report")
    g.add_edge("report", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
