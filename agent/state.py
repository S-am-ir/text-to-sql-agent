"""
agent/state.py
──────────────
AgentState is the single source of truth threaded through every graph node.

  messages         Full conversation history (add_messages appends).
                   The LLM only sees the last MESSAGE_WINDOW entries —
                   trimming happens inside agent_node, not here.

  system_prompt    Built once at graph-construction time; injected as
                   a SystemMessage on every LLM call.

  iteration_count  Incremented each time agent_node runs; reset to 0
                   at the start of each user turn by the API caller.

  max_iterations   Safety ceiling for the ReAct loop (default: MAX_ITERATIONS).
"""
from __future__ import annotations

from typing import Annotated

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict):
    messages:        Annotated[list[AnyMessage], add_messages]
    system_prompt:   str
    iteration_count: int
    max_iterations:  int