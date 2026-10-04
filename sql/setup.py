"""
Set up 8D Intelligence in a Snowflake account with one command.

    python sql/setup.py --connection <name> [--database MANUFACTURING_8D] [--warehouse COMPUTE_WH]
                        [--app-role <role>] [--steps data,email,ops,business,assistant,grants]

Steps (all by default, in this order):
  data       01/02/04: database, PRODUCTION schema, the 16 plant tables loaded from
             data/synthetic with row-count checks, analytical views
  email      06: EIGHT_D_REPORT_EMAIL notification integration and REPORT_STAGE
  ops        07: OPERATIONS schema (live machine state, work orders, events,
             recipient lists, email log, investigations, app settings)
  business   10: rolling ORDER_BOOK, corrective actions, spare-part ledger and
             purchase orders, stock views, the order-book and daily-digest
             procedures (code uploaded to OPERATIONS.APP_LIB) and their daily tasks
  assistant  09: PLANT_OPERATIONS_SV semantic view and PLANT_ASSISTANT Cortex Agent
  grants     everything the app's owner role needs (only with --app-role)

The scripts are written for MANUFACTURING_8D and COMPUTE_WH; --database and
--warehouse rewrite those names, so the same scripts serve any account. The app
finds its objects from the database it is deployed in, so nothing in the code
changes per account. External email (08_external_email.sql) needs your mail
credentials and stays a manual step. Safe to re-run.
"""

import argparse
import csv
import io
import os
import re
import sys
import tempfile
import zipfile

import snowflake.connector
from snowflake.connector.util_text import split_statements

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL_DIR = os.path.join(ROOT, "sql")
DEFAULT_DB, DEFAULT_WH = "MANUFACTURING_8D", "COMPUTE_WH"
TABLES = ["machines", "operators", "suppliers", "customers", "material_lots", "production_orders", "bom_consumption",
          "sensor_readings", "process_records", "quality_inspections", "defect_records", "maintenance_logs",
          "shipments", "parts_inventory", "products", "part_vendors"]
STEPS = ["data", "email", "ops", "business", "assistant", "grants"]
# stored-procedure code: sql/procs.py plus the standard-library app modules it imports
PROC_FILES = [("sql/procs.py", "procs.py")] + [(f"snowflake_app/{f}", f"snowflake_app/{f}") for f in (
    "__init__.py", "core/__init__.py", "core/orderbook.py", "core/impact.py", "core/digest.py")]
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def ident(v: str, what: str) -> str:
    if not IDENT.match(v or ""):
        sys.exit(f"{what} must be a plain Snowflake identifier, got {v!r}")
    return v.upper()


class Setup:
    def __init__(self, conn, database: str, warehouse: str):
        self.conn, self.db, self.wh = conn, database, warehouse

    def sql(self, text: str) -> str:
        text = re.sub(rf"\b{DEFAULT_DB}\b", self.db, text)
        return re.sub(rf"\b{DEFAULT_WH}\b", self.wh, text)

    def run(self, statement: str):
        with self.conn.cursor() as cur:
            cur.execute(self.sql(statement))
            return cur.fetchall()

    def script(self, name: str, extra: dict | None = None):
        with open(os.path.join(SQL_DIR, name)) as f:
            text = self.sql(f.read())
        for k, v in (extra or {}).items():
            text = text.replace(k, v)
        n = 0
        for stmt, _ in split_statements(io.StringIO(text), remove_comments=True):
            if stmt.strip().rstrip(";").strip():
                with self.conn.cursor() as cur:
                    cur.execute(stmt)
                n += 1
        print(f"ran {name} ({n} statements)")

    # ------------------------------------------------------------ steps
    def data(self, data_dir: str):
        self.script("01_setup_database.sql")
        self.script("02_create_tables.sql")
        cur = self.conn.cursor()
        cur.execute(f"USE SCHEMA {self.db}.PRODUCTION")
        cur.execute("CREATE OR REPLACE FILE FORMAT csv_format TYPE = 'CSV' FIELD_OPTIONALLY_ENCLOSED_BY = '\"' "
                    "SKIP_HEADER = 1 NULL_IF = ('', 'NULL', 'None') EMPTY_FIELD_AS_NULL = TRUE")
        cur.execute("CREATE OR REPLACE STAGE data_stage FILE_FORMAT = csv_format")
        failed = []
        for t in TABLES:
            path = os.path.join(data_dir, f"{t}.csv")
            cur.execute(f"PUT 'file://{path}' @data_stage AUTO_COMPRESS = TRUE OVERWRITE = TRUE")
            cur.execute(f"COPY INTO {t} FROM @data_stage/{t}.csv.gz FILE_FORMAT = (FORMAT_NAME = 'csv_format') "
                        "ON_ERROR = 'ABORT_STATEMENT' FORCE = TRUE")
            loaded = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            with open(path, newline="") as f:
                expected = sum(1 for _ in csv.reader(f)) - 1
            failed += [] if loaded == expected else [t]
            print(f"  {t:22s} {loaded:>7,} rows {'OK' if loaded == expected else f'MISMATCH (file has {expected:,})'}")
        cur.close()
        self.script("04_create_views.sql")
        if failed:
            sys.exit(f"Row-count mismatch: {', '.join(failed)}")

    def email(self):
        self.script("06_report_email.sql")

    def ops(self):
        self.script("07_operations.sql")

    def business(self, tz: str | None = None):
        tz = tz or self.run("SHOW PARAMETERS LIKE 'TIMEZONE' IN ACCOUNT")[0][1] or "UTC"
        stage = f"{self.db}.OPERATIONS.APP_LIB"
        self.run(f"CREATE STAGE IF NOT EXISTS {stage} COMMENT = '8D Intelligence: stored-procedure code (uploaded by sql/setup.py)'")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "eight_d_procs.zip")
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                for src, arc in PROC_FILES:
                    z.write(os.path.join(ROOT, src), arc)
            with self.conn.cursor() as cur:
                cur.execute(f"PUT 'file://{path}' @{stage} AUTO_COMPRESS = FALSE OVERWRITE = TRUE")
        print(f"  uploaded procedure code to @{stage} (time zone {tz})")
        self.script("10_business.sql", {"EIGHT_D_TZ": tz})

    def assistant(self):
        self.script("09_assistant.sql")

    def grants(self, role: str):
        db, wh = self.db, self.wh
        stmts = [
            f"GRANT USAGE ON DATABASE {db} TO ROLE {role}",
            f"GRANT USAGE ON WAREHOUSE {wh} TO ROLE {role}",
            f"GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE {role}",
            f"GRANT USAGE ON SCHEMA {db}.PRODUCTION TO ROLE {role}",
            f"GRANT SELECT ON ALL TABLES IN SCHEMA {db}.PRODUCTION TO ROLE {role}",
            f"GRANT SELECT ON ALL VIEWS IN SCHEMA {db}.PRODUCTION TO ROLE {role}",
            f"GRANT READ, WRITE ON STAGE {db}.PRODUCTION.REPORT_STAGE TO ROLE {role}",
            f"GRANT USAGE ON INTEGRATION EIGHT_D_REPORT_EMAIL TO ROLE {role}",
            f"GRANT USAGE, CREATE TABLE ON SCHEMA {db}.OPERATIONS TO ROLE {role}",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {db}.OPERATIONS TO ROLE {role}",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON FUTURE TABLES IN SCHEMA {db}.OPERATIONS TO ROLE {role}",
            f"GRANT SELECT ON SEMANTIC VIEW {db}.OPERATIONS.PLANT_OPERATIONS_SV TO ROLE {role}",
            f"GRANT USAGE ON AGENT {db}.OPERATIONS.PLANT_ASSISTANT TO ROLE {role}",
            f"GRANT SELECT ON ALL VIEWS IN SCHEMA {db}.OPERATIONS TO ROLE {role}",
            f"GRANT USAGE ON PROCEDURE {db}.OPERATIONS.BUILD_DAILY_DIGEST(STRING) TO ROLE {role}",
            f"GRANT USAGE ON PROCEDURE {db}.OPERATIONS.SEND_DAILY_DIGEST(STRING, STRING) TO ROLE {role}",
            f"GRANT USAGE ON PROCEDURE {db}.PRODUCTION.ROLL_ORDER_BOOK(STRING) TO ROLE {role}",
            f"GRANT OPERATE, MONITOR ON TASK {db}.OPERATIONS.EIGHT_D_DAILY_DIGEST TO ROLE {role}",
            f"GRANT MONITOR ON TASK {db}.PRODUCTION.EIGHT_D_ROLL_ORDER_BOOK TO ROLE {role}",
        ]
        for s in stmts:
            try:
                with self.conn.cursor() as cur:
                    cur.execute(s)
            except snowflake.connector.ProgrammingError as e:
                print(f"  skipped: {s}  ({str(e).splitlines()[-1][:120]})")
        print(f"granted app access to role {role}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--connection", required=True, help="Connection name from ~/.snowflake/connections.toml")
    ap.add_argument("--database", default=DEFAULT_DB)
    ap.add_argument("--warehouse", default=DEFAULT_WH)
    ap.add_argument("--app-role", help="Role that owns or runs the Streamlit app (grants step)")
    ap.add_argument("--steps", default=",".join(STEPS), help=f"Comma-separated subset of {', '.join(STEPS)}")
    ap.add_argument("--data-dir", default=os.path.join(ROOT, "data", "synthetic"))
    ap.add_argument("--timezone", help="IANA time zone for the nightly order-book roll and the 07:00 digest "
                                       "(default: the account's TIMEZONE parameter)")
    args = ap.parse_args()
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    unknown = set(steps) - set(STEPS)
    if unknown:
        sys.exit(f"Unknown steps: {', '.join(sorted(unknown))}")

    db, wh = ident(args.database, "--database"), ident(args.warehouse, "--warehouse")
    conn = snowflake.connector.connect(connection_name=args.connection, warehouse=wh)
    try:
        s = Setup(conn, db, wh)
        for step in STEPS:
            if step not in steps:
                continue
            print(f"[{step}]")
            if step == "data":
                s.data(args.data_dir)
            elif step == "business":
                if args.timezone and not re.match(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+)*$", args.timezone):
                    sys.exit(f"--timezone must be an IANA name such as Asia/Kolkata, got {args.timezone!r}")
                s.business(args.timezone)
            elif step == "grants":
                if args.app_role:
                    s.grants(ident(args.app_role, "--app-role"))
                else:
                    print("  no --app-role: the deploying role owns everything it created")
            else:
                getattr(s, step)()
        print(f"Done: {db} on warehouse {wh}.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
