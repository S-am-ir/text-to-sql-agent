"""
api/main.py
───────────
FastAPI backend — owns all agent logic and exposes four endpoints:

  GET  /health         
  GET  /tables         Table names + row counts (for the sidebar)
  POST /chat           Run the agent with a new user message
  POST /hitl/respond   Resume a paused graph after approve / deny
"""
from __future__ import annotations

import ast
import logging
import re
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel

from agent.graph import build_graph
from config import settings
from db.connection import get_sql_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Graph singleton — built once on startup, shared across all requests
# ---------------------------------------------------------------------------
_graph = None
_system_prompt: str = ""

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _graph, _system_prompt
    logger.info("Building LangGraph agent...")
    _graph, _system_prompt = build_graph()
    logger.info("Agent ready.")
    yield

app = FastAPI(title="SQL Data Analyst API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    thread_id: str
    message: str

class ChatResponse(BaseModel):
    response: str
    sql:            Optional[str] = None
    result_str:     Optional[str] = None
    hitl_payload:   Optional[dict] = None

class HITLRequest(BaseModel):
    thread_id: str
    decision: Literal["approved", "denied"]    

class HITLResponse(BaseModel):
    response: str

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}

def _strip_chart(text: str) -> str:
    """Remove the ```chart ... ``` block from display text."""
    return re.sub(r"\n?```chart\n.*?\n```", "", text, flags=re.DOTALL).strip()

def _extract_artifacts(thread_id: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Walk the current graph state messages to extract:
      sql        — query arg from the last execute_query tool call
      result_str — content of the last execute_query ToolMessage
      chart_cfg  — ChartConfig JSON from the last narrative AIMessage
    """
    state = _graph.get_state(_cfg(thread_id))
    if not state or not state.values.get("messages"):
        return None, None
    
    sql = result_str = None
    
    for msg in reversed(state.values["messages"]):
        if isinstance(msg, AIMessage):
            if not sql and getattr(msg, "tool_calls", None):
                for tc in msg.tool_calls:
                    if tc["name"] == "execute_query":
                        sql = tc["args"].get("query")
                        break
        if isinstance(msg, ToolMessage) and not result_str:
            c = msg.content or ""
            if c.strip() and "SELECT" not in c[:30] and "BLOCKED" not in c:
                result_str = c
            
        if sql and result_str:
            break

    return sql, result_str
    
def _get_hitl_payload(thread_id: str) -> Optional[dict]:
    """Return the write_confirmation interrupt payload if the graph is paused."""
    state = _graph.get_state(_cfg(thread_id))
    if not (state and state.next):
        return None
    for task in state.tasks:
        for intr in getattr(task, "interrupts", []):
            if isinstance(intr.value, dict) and intr.value.get("type") == "write_confirmation":
                return intr.value
    return None

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/tables")
def tables():
    """Return {table_name: row_count} for the sidebar display."""
    db = get_sql_db()
    result = {}
    for table in db.get_usable_table_names():
        try:
            raw = db.run(f"SELECT COUNT(*) FROM {table}")
            result[table] = int(ast.literal_eval(raw.strip())[0][0])
        except Exception:
            result[table] = -1
    return result

@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if _graph is None:
        raise HTTPException(503, "Agent not initialised yet — try again shortly.")

    config = _cfg(req.thread_id)
    agent_input = {
        "messages":        [HumanMessage(content=req.message)],
        "system_prompt":   _system_prompt,
        "iteration_count": 0,
        "max_iterations":  settings.MAX_ITERATIONS,
    }

    final_text = ""
    try:
        for event in _graph.stream(agent_input, config=config, stream_mode="values"):
            msgs = event.get("messages", [])
            if not msgs:
                continue
            last = msgs[-1]
            if isinstance(last, AIMessage) and not getattr(last, "tool_calls", None):
                final_text = _strip_chart(last.content or "")
    except Exception as exc:
        logger.exception("Agent error: %s", exc)

    sql, result_str = _extract_artifacts(req.thread_id)

    return ChatResponse(
        response=final_text,
        sql=sql,
        result_str=result_str,
        hitl_payload=_get_hitl_payload(req.thread_id),
    )

@app.post("/hitl/respond", response_model=HITLResponse)
def hitl_response(req: HITLRequest):
    if _graph is None:
        raise HTTPException(503, "Agent not initialised yet.")
    
    try:
        result = _graph.invoke(Command(resume=req.decision), config=_cfg(req.thread_id))
    except Exception as exc:
        logger.exception("HITL resume error: %s", exc)    
        raise HTTPException(500, detail=str(exc))
    
    response = ""
    for msg in reversed(result.get("messages", [])):
        if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
            response = _strip_chart(msg.content or "")
            break

    return HITLResponse(response=response)