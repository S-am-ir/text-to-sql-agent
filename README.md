# Text-to-SQL Agent

A SQL data analyst agent that converts natural language questions into SQL queries and executes them against a PostgreSQL database. Built on LangGraph with a ReAct (Reasoning + Acting) loop pattern.

## Overview

This agent enables non-technical users to analyze e-commerce transaction data by asking questions in plain English. The LLM reasons through the question, determines which tables and queries are needed, and executes them while maintaining data integrity through multiple safety layers and human-in-the-loop (HITL) write confirmation.

### Key Features

- **Natural Language Queries**: Ask questions like "What are the top 10 countries by revenue?" and get results with SQL explanations
- **ReAct Loop**: LLM-driven agent that reasons about queries, calls tools, and iterates until it reaches a conclusion
- **Read-Only Safe by Default**: All SELECT queries are sandboxed; writes require explicit human approval
- **Stateful Conversations**: Thread-based conversation history stored in PostgreSQL with LangGraph checkpointing
- **Rich UI**: Streamlit frontend with quick-start questions, table statistics, and formatted results
- **Real E-commerce Data**: UCI Online Retail II dataset with ~1 million transactions from 2009–2011

---

## How It Works

### 1. Agent Workflow

When a user asks a question:

1. **Initialization**: Streamlit sends the message to FastAPI `/chat` endpoint
2. **Graph Execution**: LangGraph invokes the agent with the message added to state
3. **LLM Reasoning**: The LLM reads the system prompt (with pre-loaded schema) and decides which tools to call
4. **Tool Calls**: Agent executes tools in sequence (list_tables, get_schema, execute_query, etc.)
5. **Iteration**: After each tool result, the agent checks if it has enough info; if not, loops
6. **Convergence**: Agent either answers the question or hits the iteration limit
7. **Result Return**: Streamlit displays the answer, SQL query, and results table

**Read-Only Mode** (Default):
- PostgreSQL user `analyst` has only `SELECT` privilege
- Python checks for `SELECT` before executing via `execute_query()`
- All writes are blocked at the DB level

**Write Operations** (HITL):
- Agent calls `request_modification()` → LangGraph `interrupt()` pauses the graph
- API waits for user approval via `POST /hitl/respond`
- User reviews the proposed SQL and reason, then approves or denies
- Only after approval does `execute_write()` execute the statement
- Transactions are atomic; if the query fails, no partial state is committed

### 3. State Management & Checkpointing

- **Thread ID**: Each conversation has a unique UUID
- **PostgresSaver**: LangGraph checkpoint backend stores graph state in PostgreSQL
- **Message History**: Full conversation stored in agentstate.messages
- **Context Window**: Only the last `MESSAGE_WINDOW` messages sent to the LLM (reduces token usage)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     STREAMLIT FRONTEND                          │
│                    (Chat UI + Sidebar)                          │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTP
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                     FASTAPI BACKEND                             │
│   (/chat, /hitl/respond, /health, /tables)                     │
│                                                                 │
│  ┌────────────────────────────────────────────────────────┐    │
│  │            LANGGRAPH REACT AGENT                       │    │
│  │  ┌──────────────────────────────────────────────────┐  │    │
│  │  │  START                                           │  │    │
│  │  │    │                                             │  │    │
│  │  │    ▼                                             │  │    │
│  │  │  ┌─────────────┐      Has Tool Calls?           │  │    │
│  │  │  │ Agent Node  ├──────Yes─────┐                 │  │    │
│  │  │  │ (LLM Call)  │              │                 │  │    │
│  │  │  └─────────────┘              │                 │  │    │
│  │  │        │                      ▼                 │  │    │
│  │  │       No               ┌──────────────┐         │  │    │
│  │  │        │               │  Tool Node   │         │  │    │
│  │  │        │               │  (Execute)   │         │  │    │
│  │  │        │               └──────┬───────┘         │  │    │
│  │  │        │                      │                 │  │    │
│  │  │        └──────────────────────┘                 │  │    │
│  │  │                     │                           │  │    │
│  │  │                     ▼                           │  │    │
│  │  │                   END                           │  │    │
│  │  └──────────────────────────────────────────────────┘  │    │
│  │                                                        │    │
│  │  TOOLS:                                               │    │
│  │  ├─ list_tables()          → Get available tables     │    │
│  │  ├─ get_schema(tables)     → Schema + sample rows     │    │
│  │  ├─ execute_query(sql)     → Run SELECT queries       │    │
│  │  ├─ request_modification() → HITL gate for writes    │    │
│  │  └─ execute_write(sql)     → Execute approved writes  │    │
│  │                                                        │    │
│  │  STATE MANAGEMENT:                                    │    │
│  │  ├─ messages[]             → Full conversation        │    │
│  │  ├─ system_prompt          → Pre-loaded DB schema     │    │
│  │  ├─ iteration_count        → ReAct loop counter       │    │
│  │  └─ max_iterations         → Safety ceiling (10)      │    │
│  └────────────────────────────────────────────────────────┘    │
│                          │                                      │
│                          ▼                                      │
│              ┌────────────────────────┐                         │
│              │  PostgreSQL Connection │                         │
│              │  (SQLAlchemy + Pool)   │                         │
│              └────────────────────────┘                         │
└──────────────────┬──────────────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────────┐
│                    POSTGRESQL DATABASE                          │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │   customers  │  │   products   │  │   invoices   │          │
│  ├──────────────┤  ├──────────────┤  ├──────────────┤          │
│  │ customer_id  │  │ stock_code   │  │ invoice_no   │          │
│  │ country      │  │ description  │  │ customer_id  │          │
│  └──────────────┘  │ avg_unit_    │  │ invoice_date │          │
│                    │ price        │  │ is_cancelled │          │
│                    └──────────────┘  └──────────────┘          │
│                                                                 │
│  ┌──────────────────────────────┐    ┌──────────────────────┐  │
│  │      invoice_items           │    │  langgraph_storage   │  │
│  ├──────────────────────────────┤    ├──────────────────────┤  │
│  │ id (PK)                      │    │ (Checkpoint tables)  │  │
│  │ invoice_no (FK)              │    │ (Thread history)     │  │
│  │ stock_code (FK)              │    └──────────────────────┘  │
│  │ quantity                     │                              │
│  │ unit_price                   │                              │
│  └──────────────────────────────┘                              │
│                                                                 │
│  Indices: customer, invoice_date, invoice_no, stock_code       │
└─────────────────────────────────────────────────────────────────┘
```

### Data Flow

1. **User Question**: Streamlit UI sends natural language question to backend
2. **Agent Reasoning**: LLM reads system prompt (with pre-loaded schema) and decides which tools to call
3. **Tool Execution**: Agent calls tools in sequence:
   - `list_tables()` / `get_schema()` to understand data structure
   - `execute_query()` to run SELECT queries
   - `request_modification()` to request write approval (HITL)
4. **Iteration**: Agent checks if it has the answer; if not, loops back with tool results
5. **HITL Confirmation**: For write operations, the API pauses via `interrupt()` and waits for user approval
6. **Result Return**: Agent synthesizes findings and returns to frontend
7. **Storage**: Conversation history and tool results stored in PostgreSQL for continuity

---

## Database Schema

The dataset contains ~540,000 transactions from a UK-based online retailer (2009–2011).

### Tables

#### `customers`
Customer master data extracted from the transaction dataset.

| Column | Type | Notes |
|--------|------|-------|
| `customer_id` | INTEGER PRIMARY KEY | UCI dataset customer ID |
| `country` | VARCHAR(100) NOT NULL | Country of residence |

**Sample**: 4,372 unique customers from 38 countries

---

#### `products`
Product catalog with aggregated pricing.

| Column | Type | Notes |
|--------|------|-------|
| `stock_code` | VARCHAR(20) PRIMARY KEY | Product SKU |
| `description` | TEXT | Product name/details |
| `avg_unit_price` | NUMERIC(12,4) | Average price across transactions |

**Sample**: 4,070 unique products

---

#### `invoices`
Transaction header with cancellation status.

| Column | Type | Notes |
|--------|------|-------|
| `invoice_no` | VARCHAR(20) PRIMARY KEY | Transaction ID (starts with "C" if cancelled) |
| `customer_id` | INTEGER FK → customers | Links to customer |
| `invoice_date` | TIMESTAMP | Transaction timestamp |
| `is_cancelled` | BOOLEAN DEFAULT FALSE | Derived from invoice_no prefix |

**Sample**: 395,268 invoice headers (range: 2009-12-01 to 2011-12-09)

---

#### `invoice_items`
Line items within each transaction.

| Column | Type | Notes |
|--------|------|-------|
| `id` | BIGSERIAL PRIMARY KEY | Line item ID |
| `invoice_no` | VARCHAR(20) FK → invoices | Links to transaction |
| `stock_code` | VARCHAR(20) FK → products | Links to product |
| `quantity` | INTEGER NOT NULL | Units ordered |
| `unit_price` | NUMERIC(12,4) NOT NULL | Price per unit |

**Sample**: ~540,000 line items (only positive quantities kept)

---

### Key Constraints & Indices

```sql
-- Foreign keys (referential integrity)
invoices.customer_id REFERENCES customers(customer_id)
invoice_items.invoice_no REFERENCES invoices(invoice_no)
invoice_items.stock_code REFERENCES products(stock_code)

-- Indices (query performance)
idx_invoices_customer  ON invoices(customer_id)
idx_invoices_date      ON invoices(invoice_date)
idx_items_invoice      ON invoice_items(invoice_no)
idx_items_stock        ON invoice_items(stock_code)
```

---

## Demo

### Example 1: Top Countries by Revenue

**Query**: "What are the top 10 countries by revenue?"

![Query 1](demo_images/query_1.png)

**SQL Generated**:
```sql
SELECT c.country, SUM(i1.quantity * i1.unit_price) AS total_revenue
FROM customers c
JOIN invoices i ON c.customer_id = i.customer_id
JOIN invoice_items i1 ON i.invoice_no = i1.invoice_no
WHERE i.is_cancelled = FALSE AND i.customer_id IS NOT NULL
GROUP BY c.country
ORDER BY total_revenue DESC
LIMIT 10;
```

**Results Table**: Shows country names and their total revenue (UK leads with ~14.7M, followed by EIRE with ~622K)

**Business Insight**: The United Kingdom dominates revenue, contributing roughly 90% of total sales, reflecting the retailer's strong domestic market presence. International penetration is limited, with the next five countries (EIRE, Netherlands, Germany, France, Australia) combined accounting for only about 5% of total revenue.

---

### Example 2: Cancelled Products Analysis

**Query**: "Which products are most frequently cancelled?"

![Query 2](demo_images/query_2.png)

**SQL Generated**:
```sql
SELECT COUNT(*) FROM invoices WHERE is_cancelled = TRUE;
```

**Results**: The agent discovered there are **0 cancelled invoices** in the dataset.

**Agent Response**: "I checked the data and found that there are no cancelled invoices in the dataset. Because there are no cancelled orders, there are no products that have been cancelled, and therefore no 'most-frequently cancelled' products to report."

**Business Insight**: The dataset contains only completed (non-cancelled) transactions. This is a data limitation — if you need to study cancellation patterns, you would require a dataset that includes cancelled invoices.

---

## Installation & Setup

### Prerequisites

- Docker & Docker Compose (recommended)
- Python 3.10+
- PostgreSQL 16+ (if running locally)
- Groq API key (free tier available at [console.groq.com](https://console.groq.com))

### Quick Start (Docker Compose)

1. **Clone the repository**:
   ```bash
   git clone https://github.com/S-am-ir/text-to-sql-agent.git
   cd text-to-sql-agent
   ```

2. **Set up environment**:
   ```bash
   cp .env.example .env
   # Edit .env and add your GROQ_API_KEY
   ```

3. **Build and run**:
   ```bash
   docker-compose up --build
   ```

   This will:
   - Start PostgreSQL on `localhost:5432`
   - Download and seed the UCI Online Retail II dataset (~50MB)
   - Start FastAPI backend on `localhost:8000`
   - Start Streamlit UI on `localhost:8501`

4. **Access the UI**:
   Open [http://localhost:8501](http://localhost:8501)

### Local Development Setup

1. **Create Python virtual environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Set up PostgreSQL**:
   ```bash
   # Ensure PostgreSQL is running on localhost:5432
   # Create database manually or use .env to point to your instance
   ```

4. **Configure environment**:
   ```bash
   cp .env.example .env
   # Edit .env with your database URL and Groq API key
   ```

5. **Seed the database**:
   ```bash
   python -m db.seed
   ```

6. **Run backend** (terminal 1):
   ```bash
   uvicorn api.main:app --reload --port 8000
   ```

7. **Run frontend** (terminal 2):
   ```bash
   streamlit run ui/app.py
   ```

---

## Project Structure

```
text-to-sql-agent/
├── agent/
│   ├── graph.py         # LangGraph StateGraph + ReAct loop
│   ├── state.py         # AgentState TypedDict definition
│   ├── tools.py         # Agent tools (list_tables, execute_query, etc.)
│   └── prompts.py       # System prompt builder with schema injection
│
├── api/
│   └── main.py          # FastAPI backend + endpoints
│
├── db/
│   ├── connection.py    # SQLDatabase + PostgresSaver connection layer
│   └── seed.py          # UCI dataset download + PostgreSQL ingestion
│
├── ui/
│   ├── app.py           # Streamlit chat interface
│   └── styles.css       # Dark theme styling
│
├── demo_images/
│   ├── query_1.png      # Example: Top 10 countries by revenue
│   └── query_2.png      # Example: Cancelled products analysis
│
├── config.py            # Pydantic Settings for environment variables
├── requirements.txt     # Python dependencies
├── Dockerfile           # Container image definition
├── docker-compose.yml   # Multi-container orchestration
├── style.css            # Additional UI styling
├── README.md            # This file
├── .env.example         # Template environment variables
└── .gitignore           # Git exclusion rules
```

---

## API Endpoints

### `GET /health`
Health check for the backend.

**Response** (200):
```json
{"status": "ok"}
```

---

### `GET /tables`
List all tables and their row counts (used by Streamlit sidebar).

**Response** (200):
```json
{
  "customers": 4372,
  "products": 4070,
  "invoices": 395268,
  "invoice_items": 540000
}
```

---

### `POST /chat`
Execute a user message through the agent.

**Request**:
```json
{
  "thread_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": "What are the top 10 countries by revenue?"
}
```

**Response** (200):
```json
{
  "response": "The United Kingdom leads with 14.7M in revenue...",
  "sql": "SELECT c.country, SUM(...)",
  "result_str": "country | total_revenue\n...",
  "hitl_payload": null
}
```

---

### `POST /hitl/respond`
Approve or deny a proposed write operation (only called during HITL pause).

**Request**:
```json
{
  "thread_id": "550e8400-e29b-41d4-a716-446655440000",
  "decision": "approved"
}
```

**Response** (200):
```json
{
  "response": "Write operation completed. 5 rows affected."
}
```

---

## Development & Debugging

### Logs

Logs are written to console. For debugging, set log level in `config.py` or via environment:

```bash
PYTHONLOGLEVEL=DEBUG uvicorn api.main:app
```

### Testing Queries

Use the Streamlit UI or call the API directly:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "thread_id": "test-thread-123",
    "message": "How many invoices are in the database?"
  }'
```

### Database Inspection

Connect to PostgreSQL directly:

```bash
psql postgresql://analyst:analyst_pass@localhost:5432/retail_db

-- List tables
\dt

-- Check row counts
SELECT 'customers' as table_name, COUNT(*) FROM customers
UNION ALL
SELECT 'products', COUNT(*) FROM products
UNION ALL
SELECT 'invoices', COUNT(*) FROM invoices
UNION ALL
SELECT 'invoice_items', COUNT(*) FROM invoice_items;

-- Inspect schema
\d customers
```
---

## Citation

Dataset: [UCI Online Retail II Dataset](https://archive.ics.uci.edu/dataset/502/online+retail+ii)
