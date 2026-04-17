"""
db/seed.py
──────────────
One time ingestion of the UCI Online Retail II dataset into PostgreSQL.

Dataset: https://archive.ics.uci.edu/dataset/502/online+retail+ii
~1 million real UK e-commerce transactions, 2009–2011.

Run:
    python -m db.seed            # full seed
    python -m db.seed --check    # skip silently if already seeded (used by entrypoint)

Normalized into 4 tables:
    customers      (customer_id, country)
    products       (stock_code, description, avg_unit_price)
    invoices       (invoice_no, customer_id, invoice_date, is_cancelled)
    invoice_items  (id, invoice_no, stock_code, quantity, unit_price)
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import zipfile
from pathlib import Path

import pandas as pd
import psycopg
import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataset URL and local cache
# ---------------------------------------------------------------------------

UCI_URL = "https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip"
CACHE_DIR = Path("tmp/retail_data")
XLSX_NAME = "online_retail_II.xlsx"

DDL = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id INTEGER PRIMARY KEY,
    country     VARCHAR(100) NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    stock_code VARCHAR(20) PRIMARY KEY,
    description TEXT,
    avg_unit_price NUMERIC(12, 4)
);
CREATE TABLE IF NOT EXISTS invoices (
    invoice_no VARCHAR(20) PRIMARY KEY,
    customer_id INTEGER REFERENCES customers(customer_id),
    invoice_date TIMESTAMP,
    is_cancelled BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS invoice_items (
    id BIGSERIAL PRIMARY KEY,
    invoice_no VARCHAR(20) NOT NULL REFERENCES invoices(invoice_no),
    stock_code VARCHAR(20) NOT NULL REFERENCES products(stock_code),
    quantity INTEGER NOT NULL,
    unit_price NUMERIC(12, 4) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_invoices_customer ON invoices(customer_id);
CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(invoice_date);
CREATE INDEX IF NOT EXISTS idx_items_invoice ON invoice_items(invoice_no);
CREATE INDEX IF NOT EXISTS idx_items_stock ON invoice_items(stock_code);
"""
SEED_FLAG_TABLE = "SELECT to_regclass('public.invoice_items') IS NOT NULL"


def download_and_extract() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    xlsx_path = CACHE_DIR / XLSX_NAME
 
    if xlsx_path.exists():
        logger.info("Cached file found at %s — skipping download.", xlsx_path)
        return xlsx_path
 
    logger.info("Downloading UCI Online Retail II dataset (~50MB)…")
    resp = requests.get(UCI_URL, timeout=120, stream=True)
    resp.raise_for_status()
 
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        for name in zf.namelist():
            if name.endswith(".xlsx"):
                zf.extract(name, CACHE_DIR)
                extracted = CACHE_DIR / name
                extracted.rename(xlsx_path)
                logger.info("Extracted → %s", xlsx_path)
                break
 
    return xlsx_path

def load_and_clean(xlsx_path: Path) -> pd.DataFrame:
    logger.info("Loading both Excel sheets (this takes ~30s)…")
    sheets = []
    for sheet in ["Year 2009-2010", "Year 2010-2011"]:
        df = pd.read_excel(xlsx_path, sheet_name=sheet, engine="openpyxl")
        sheets.append(df)
 
    df = pd.concat(sheets, ignore_index=True)
    logger.info("Raw rows: %d", len(df))
 
    # Standardise column names
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
 
    # Drop rows without a customer (can't link to customers table)
    df = df.dropna(subset=["customer_id"])
    df["customer_id"] = df["customer_id"].astype(int)
 
    # Derive cancellation flag from InvoiceNo prefix 'C'
    df["is_cancelled"] = df["invoice"].astype(str).str.startswith("C")
 
    # Keep only positive quantities (negatives are returns/adjustments)
    df = df[df["quantity"] > 0]
 
    # Drop rows with missing stock code or price
    df = df.dropna(subset=["stockcode", "price"])
    df = df[df["price"] > 0]
 
    # Clean string columns
    df["stockcode"] = df["stockcode"].astype(str).str.strip().str.upper()
    df["description"] = df["description"].astype(str).str.strip()
 
    logger.info("Clean rows: %d", len(df))
    return df


def build_customers(df: pd.DataFrame) -> pd.DataFrame:
    return (
        df[["customer_id", "country"]]
        .drop_duplicates(subset="customer_id")
        .reset_index(drop=True)
    )

def build_products(df: pd.DataFrame) -> pd.DataFrame:
    return (
        df.groupby("stockcode", as_index=False)
        .agg(description=("description", "first"), avg_unit_price=("price", "mean"))
        .rename(columns={"stockcode": "stock_code"})
    )

def build_invoices(df: pd.DataFrame) -> pd.DataFrame:
    return (
        df[["invoice", "customer_id", "invoicedate", "is_cancelled"]]
        .drop_duplicates(subset="invoice")
        .rename(columns={"invoice": "invoice_no", "invoicedate": "invoice_date"})
        .reset_index(drop=True)
    )
 
def build_items(df: pd.DataFrame) -> pd.DataFrame:
    return (
        df[["invoice", "stockcode", "quantity", "price"]]
        .rename(columns={
            "invoice":   "invoice_no",
            "stockcode": "stock_code",
            "price":     "unit_price",
        })
        .reset_index(drop=True)
    )

def get_dsn() -> str:
    url = os.environ.get("DATABASE_URL", "")
    for prefix in ("postgresql+psycopg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url

def bulk_insert(conn, table: str, df: pd.DataFrame, columns: list[str]) -> int:
    """Fast bulk insert via COPY-style execute_many workaround using executemany."""
    records = [tuple(row) for row in df[columns].itertuples(index=False, name=None)]
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = (
        f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT DO NOTHING"
    )
    with conn.cursor() as cur:
        cur.executemany(sql, records)
    return len(records)
 
 
def seed(conn, df: pd.DataFrame) -> None:
    logger.info("Creating tables…")
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()
 
    customers = build_customers(df)
    products  = build_products(df)
    invoices  = build_invoices(df)
    items     = build_items(df)
 
    logger.info("Inserting %d customers…",    len(customers))
    bulk_insert(conn, "customers", customers, ["customer_id", "country"])
 
    logger.info("Inserting %d products…",     len(products))
    bulk_insert(conn, "products", products, ["stock_code", "description", "avg_unit_price"])
 
    logger.info("Inserting %d invoices…",     len(invoices))
    bulk_insert(conn, "invoices", invoices, ["invoice_no", "customer_id", "invoice_date", "is_cancelled"])
 
    logger.info("Inserting %d invoice items…", len(items))
    bulk_insert(conn, "invoice_items", items, ["invoice_no", "stock_code", "quantity", "unit_price"])
 
    conn.commit()
    logger.info("✅  Seed complete.")

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Skip seeding silently if invoice_items table already exists.",
    )

    args = parser.parse_args()
    
    dsn = get_dsn()
    if not dsn:
        logger.error("DATABASE_URL not set. Aborting.")
        sys.exit(1)

    with psycopg.connect(dsn, autocommit=False) as conn:
        if args.check:
            with conn.cursor() as cur:
                cur.execute(SEED_FLAG_TABLE)
                already_seeded = cur.fetchone()[0]
            if already_seeded:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM invoice_items LIMIT 1")
                    count = cur.fetchone()[0]
                if count > 0:
                    logger.info("Data already present (%d invoice items). Skipping seed.", count)

                    return
                
        xlsx_path = download_and_extract()
        df = load_and_clean(xlsx_path)
        seed(conn, df)

if __name__ == "__main__":
    main()
