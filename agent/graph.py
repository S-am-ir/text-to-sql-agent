"""
agent/graph.py
──────────────
Custom ReAct loop on LangGraph's low-level StateGraph.
 
Topology:
  START → agent ──(has tool calls?)──► tools → agent → ...
                └──(no tool calls?)──► END
 
HITL write-confirmation is handled inside the request_modification tool
via LangGraph's interrupt() — the graph pauses mid-tool-execution and
resumes when the user approves or denies via the API.
"""
from __future__ import annotations
 
import logging
from typing import Literal
 
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_groq import ChatGroq
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
 
from agent.prompts import build_system_prompt
from agent.state import AgentState
from agent.tools import ALL_TOOLS
from config import settings
from db.connection import get_checkpointer_pool

logger = logging.getLogger(__name__)

def _build_llm() -> ChatGroq:
    return ChatGroq(
        model=settings.PRIMARY_MODEL,
        temperature=0,
        max_retries=2,
    )

def _trim_messages(messages: list, window: int) -> list:
    """
    Return the last `window` messages.
    if the slice would start on a ToolMessage (orphaning it from its
    AIMessage with tool_calls), include the AIMessage too.
    """
    if len(messages) <= window:
        return messages
    tail = list(messages[-window:])
    if isinstance(tail[0], ToolMessage):
        prev_idx = len(messages) - window - 1
        if prev_idx >= 0 and getattr(messages[prev_idx], "tool_calls", None):
            tail = [messages[prev_idx]] + tail
    return tail

def build_graph():
    """Build and return (compiled_graph, system_prompt)."""
    system_prompt = build_system_prompt()
    llm = _build_llm().bind_tools(ALL_TOOLS)

    # ── Nodes ──────────────────────────────────────────────────────────────

    def agent_node(state: AgentState) -> dict:
        count = state.get("iteration_count", 0)
        if count >= state.get("max_iterations", settings.MAX_ITERATIONS):
            return {
                "messages": [AIMessage(content="I've reached the step limit. Please try a more focused question.")],
                "iteration_count": count + 1,
            }
        window = _trim_messages(state["messages"], settings.MESSAGE_WINDOW)
        response = llm.invoke([SystemMessage(content=state["system_prompt"])] + window)
        return {"messages": [response], "iteration_count": count + 1}
    
    # ── Routing ────────────────────────────────────────────────────────────

    def route(state: AgentState) -> Literal["tools", "__end__"]:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            return "tools"
        return END
    
    # ── Graph assembly ─────────────────────────────────────────────────────
    sg = StateGraph(AgentState)
    sg.add_node("agent", agent_node)
    sg.add_node("tools", ToolNode(ALL_TOOLS))
    sg.add_edge(START, "agent")
    sg.add_conditional_edges("agent", route)
    sg.add_edge("tools", "agent")

    checkpointer = PostgresSaver(get_checkpointer_pool())

    checkpointer.setup()

    return sg.compile(checkpointer=checkpointer), system_prompt