"""
Create MANUFACTURING_8D.PRODUCTION and load the 14 tables from the CSV files.

    python sql/load_data.py --connection <name in ~/.snowflake/connections.toml> [--data-dir data/synthetic]

Runs 01_setup_database.sql and 02_create_tables.sql, uploads the CSVs to an
internal stage, loads every table with ON_ERROR = ABORT_STATEMENT (a bad row
fails the load instead of being skipped), checks row counts against the files,
then creates the analytical views (04_create_views.sql). Safe to re-run: tables
are recreated and reloaded.
"""

import argparse
import csv
import os
import sys

import snowflake.connector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL_DIR = os.path.join(ROOT, "sql")
SCHEMA = "MANUFACTURING_8D.PRODUCTION"
TABLES = ["machines", "operators", "suppliers", "customers", "material_lots", "production_orders", "bom_consumption",
          "sensor_readings", "process_records", "quality_inspections", "defect_records", "maintenance_logs",
          "shipments", "parts_inventory"]


def run_script(conn, name: str):
    with open(os.path.join(SQL_DIR, name)) as f:
        for cur in conn.execute_string(f.read(), return_cursors=True):
            cur.close()
    print(f"ran {name}")


def csv_rows(path: str) -> int:
    with open(path, newline="") as f:
        return sum(1 for _ in csv.reader(f)) - 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--connection", required=True, help="Connection name from ~/.snowflake/connections.toml")
    ap.add_argument("--data-dir", default=os.path.join(ROOT, "data", "synthetic"))
    ap.add_argument("--warehouse", help="Warehouse to use for the load (defaults to the connection's)")
    args = ap.parse_args()

    kw = {"connection_name": args.connection}
    if args.warehouse:
        kw["warehouse"] = args.warehouse
    conn = snowflake.connector.connect(**kw)
    try:
        run_script(conn, "01_setup_database.sql")
        run_script(conn, "02_create_tables.sql")
        cur = conn.cursor()
        cur.execute(f"USE SCHEMA {SCHEMA}")
        cur.execute("CREATE OR REPLACE FILE FORMAT csv_format TYPE = 'CSV' FIELD_OPTIONALLY_ENCLOSED_BY = '\"' "
                    "SKIP_HEADER = 1 NULL_IF = ('', 'NULL', 'None') EMPTY_FIELD_AS_NULL = TRUE")
        cur.execute("CREATE OR REPLACE STAGE data_stage FILE_FORMAT = csv_format")
        failed = []
        for t in TABLES:
            path = os.path.join(args.data_dir, f"{t}.csv")
            cur.execute(f"PUT 'file://{path}' @data_stage AUTO_COMPRESS = TRUE OVERWRITE = TRUE")
            cur.execute(f"COPY INTO {t} FROM @data_stage/{t}.csv.gz FILE_FORMAT = (FORMAT_NAME = 'csv_format') "
                        "ON_ERROR = 'ABORT_STATEMENT' FORCE = TRUE")
            loaded = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            expected = csv_rows(path)
            ok = loaded == expected
            failed += [] if ok else [t]
            print(f"{t:22s} {loaded:>7,} rows {'OK' if ok else f'MISMATCH (file has {expected:,})'}")
        run_script(conn, "04_create_views.sql")
        if failed:
            sys.exit(f"Row-count mismatch: {', '.join(failed)}")
        print(f"Loaded {len(TABLES)} tables into {SCHEMA}.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
