"""
Creates the nhl_dashboard database if it doesn't exist and applies
schema.sql. Safe to re-run.

Usage:
    python init_db.py
"""
import os
from pathlib import Path
import psycopg2
from psycopg2 import sql
from dotenv import load_dotenv

load_dotenv()

DB_NAME = os.getenv("DB_NAME", "nhl_dashboard")


def _connect(dbname):
    return psycopg2.connect(
        dbname=dbname,
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD"),
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
    )


def main():
    admin = _connect("postgres")
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
    if not cur.fetchone():
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB_NAME)))
        print(f"Created database {DB_NAME}")
    cur.close()
    admin.close()

    conn = _connect(DB_NAME)
    cur = conn.cursor()
    cur.execute((Path(__file__).parent / "schema.sql").read_text())
    conn.commit()
    cur.close()
    conn.close()
    print("Schema applied.")


if __name__ == "__main__":
    main()
