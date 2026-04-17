"""
agent/tools.py
──────────────
Three read-only tools + one HITL write-request tool.

The docstrings are the agent's "API docs" — they are what the LLM reads to
decide when and how to call each tool. Write them carefully.

Safety model (defence in depth):
  1. PostgreSQL user is GRANT SELECT only → writes fail at DB level.
  2. execute_query checks for SELECT at Python level before touching the DB.
  3. request_modification uses LangGraph interrupt() so a human must confirm
    before any write reaches the database.
"""
from __future__ import annotations

import logging

import psycopg

from langchain_core.tools import tool
from langgraph.types import interrupt

from db.connection import get_sql_db, get_write_dsn

logger = logging.getLogger(__name__)

# DML/DDL keywords that are valid for execute_write
_WRITE_KEYWORDS = {"INSERT", "UPDATE", "DELETE", "TRUNCATE", "DROP", "ALTER", "CREATE"}

# ---------------------------------------------------------------------------
# Tool 1 — list_tables
# ---------------------------------------------------------------------------
@tool
def list_tables() -> str:
    """
    Return the names of all tables in the database.

    Call this FIRST when you don't yet know what tables exists.
    Returns a comma-seperated list of table names.
    """
    db = get_sql_db()
    tables = db.get_usable_table_names()
    return "Available tables:" + ", ".join(tables)

 
# ---------------------------------------------------------------------------
# Tool 2 — get_schema
# ---------------------------------------------------------------------------

@tool
def get_schema(table_names: str) -> str:
    """
    Return the CREATE TABLE schema and 3 sample rows for one or more tables.

    Input: comma-seperated table names, e.g. "invoices, invoice_items"
    Call this to understand column names, data types, foreign key relationships,
    and what the actual data looks like BEFORE writing any SQL query.
    Example input: "customers, invoices"
    """
    db = get_sql_db()
    tables = [t.strip() for t in table_names.split(",") if t.strip()]
    result = db.get_table_info(tables)
    if not result:
        return f"No schema found for: {table_names}. Check the table names using list_tables."
    return result


# ---------------------------------------------------------------------------
# Tool 3 — execute_query  (read-only SELECT)
# ---------------------------------------------------------------------------

@tool
def execute_query(query: str):
    """
    Execute a read-only SQL SELECT query against the PostgreSQL database.
    Returns the result rows as a formatted string.
 
    Rules you MUST follow:
    - Only SELECT statements are permitted. No INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE.
    - Always include a LIMIT clause (default 100 rows) unless you are doing an aggregation
      that naturally produces a small result set (e.g. GROUP BY country → ~40 rows).
    - For revenue calculations: SUM(quantity * unit_price)
    - For cancelled invoices: always filter WHERE is_cancelled = FALSE
    - For NULL customer IDs: use WHERE customer_id IS NOT NULL
    - For date operations: use date_trunc('month', invoice_date) or EXTRACT(year FROM invoice_date)
    - If you receive a SQL error, analyse the message, fix the query, and call this tool again.
 
    Returns: formatted table of results, or an error message to fix and retry.
    """
    db = get_sql_db()

    # Python-level guard 
    stripped = query.strip()
    first_word = stripped.split()[0].upper() if stripped.split() else ""

    if first_word != "SELECT":
        return ("BLOCKED: Only SELECT queries are allowed in read-only mode."
        "If you need to modify data, call the request_modification tool instead."
        )
    try:
        result = db.run(query)
        if not result or result.strip() == "":
            return "Query executed successfully but returned 0 rows."
        return result
    except Exception as exc:
        logger.warning("Query error: %s\nQuery was: %s", exc, query)
        return (
            f"SQL ERROR: {exc}\n\n"
            "Please carefully analyse the error, adjust the query, and call execute_query again."
        )
    

# ---------------------------------------------------------------------------
# Tool 4 — request_modification  (HITL gate — step 1 of 2)
# ---------------------------------------------------------------------------

@tool
def request_modification(proposed_sql: str, reason: str) -> str:
    """
    Request human approval to execute a data-modification SQL statement.
    This is STEP 1 of 2 for any write operation.
 
    This tool PAUSES execution and shows the human user the proposed SQL and
    your reason. They will choose to APPROVE or DENY.
 
    Arguments:
      proposed_sql: The exact SQL statement you want to execute.
      reason:       Plain-English explanation of what this change does and why.
 
    After this tool returns:
      - If the result contains "APPROVED": call execute_write(sql=proposed_sql)
      - If the result contains "DENIED":   stop, inform the user it was rejected
 
    Example:
      proposed_sql = "DELETE FROM invoice_items WHERE quantity < 0"
      reason       = "Remove negative-quantity rows that represent data entry errors."
    """

    confirmation = interrupt({
        "type": "write_confirmation",
        "proposed_sql": proposed_sql,
        "reason": reason,
    })

    decision = str(confirmation).strip().lower()
    if decision == "approved":
        return (
            f"APPROVED — you may now call execute_write with this exact SQL:\n{proposed_sql}"
        )
    else:
        return (
            "DENIED — the user rejected this modification. "
            "Do not call execute_write. Inform the user the operation was cancelled."
        )
    

# ---------------------------------------------------------------------------
# Tool 5 — execute_write  (actual write execution — step 2 of 2)
# ---------------------------------------------------------------------------

@tool
def execute_write(sql: str) -> str:
    """
    Execute an approved data-modification SQL statement (INSERT, UPDATE, DELETE, etc.).
    This is STEP 2 of 2 — only call this after request_modification returned "APPROVED".
 
    DO NOT call this tool unless you have just received an APPROVED response from
    request_modification. Calling it without prior approval violates the safety contract.
 
    The SQL must be the exact same statement that was approved via request_modification.
    Do not modify it between approval and execution.
 
    Returns: success message with rows affected, or an error description.
    """
    stripped = sql.strip()
    first_word = stripped.split()[0].upper() if stripped.split() else ""

    if first_word == "SELECT":
        return (
            "WRONG TOOL: Use execute_query for SELECT statements. "
            "execute_write is only for INSERT, UPDATE, DELETE, and approved DDL."
        )
    
    # Only allow known write keywords
    if first_word not in _WRITE_KEYWORDS:
        return (
            f"BLOCKED: Unrecognised statement type '{first_word}'. "
            f"execute_write only accepts: {', '.join(sorted(_WRITE_KEYWORDS))}."
        )
    
    dsn = get_write_dsn()
    try:
        with psycopg.connect(dsn, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rowcount = cur.rowcount
            conn.commit()
        
        # rowcount is -1 for DDL statements (CREATE, DROP, ALTER) — normalise
        if rowcount == -1:
            return f"Write operation executed successfully. (DDL — no row count available)"
        else:
            return f"Write operation executed successfully. Rows affected: {rowcount}"

    except psycopg.errors.InsufficientPrivilege as exc:
        logger.error("Write permission denied: %s", exc)
        return (
            "PERMISSION DENIED: The database user does not have write privileges. "
            "Contact your database administrator."
        )

    except Exception as exc:
        logger.error("Write operation failed: %s\nSQL: %s", exc, sql)
        return f"Write operation FAILED: {exc}"
    

# ---------------------------------------------------------------------------
# All tools exported for graph assembly
# ---------------------------------------------------------------------------

ALL_TOOLS = [
    list_tables,
    get_schema,
    execute_query,
    request_modification,
    execute_write,
]

TOOLS_NAMES = {t.name: t for t in ALL_TOOLS}