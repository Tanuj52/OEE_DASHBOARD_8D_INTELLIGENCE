"""
Deploy the Streamlit app to Streamlit in Snowflake (container runtime) without
the Snowflake CLI. Same result as `snow streamlit deploy` with snowflake.yml.

    python sql/deploy_app.py --connection <name> [--database MANUFACTURING_8D] [--warehouse COMPUTE_WH]
                             [--compute-pool SYSTEM_COMPUTE_POOL_CPU] [--mail-eai EIGHT_D_MAIL_EAI]

1. uploads the app code, theme and fonts to <db>.PRODUCTION.APP_CODE (never
   .local_settings.json, .runs/, .ops/, tests or caches),
2. installs requirements.txt from Snowflake's built-in PyPI mirror
   (ARTIFACT_REPOSITORIES = snowflake.snowpark.pypi_shared_repository; works on
   trial accounts, no internet egress needed),
3. CREATE OR REPLACE STREAMLIT <db>.PRODUCTION.EIGHT_D_INTELLIGENCE on the
   container runtime and makes it live.

The app needs no per-account changes: it reads its objects from the database it
is deployed in. Run sql/setup.py first. --mail-eai attaches the SMTP/SendGrid
integration and secrets from 08_external_email.sql.
"""

import argparse
import glob
import os
import sys

import snowflake.connector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from setup import DEFAULT_DB, DEFAULT_WH, ident  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = "EIGHT_D_INTELLIGENCE"
FILES = ["snowflake_app/__init__.py", "snowflake_app/streamlit_app.py", "snowflake_app/requirements.txt",
         "snowflake_app/.streamlit/config.toml", "snowflake_app/app_pages/*.py", "snowflake_app/core/*.py",
         "snowflake_app/services/*.py", "snowflake_app/tools/*.py", "snowflake_app/ui/*.py",
         "snowflake_app/static/*.ttf"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--connection", required=True)
    ap.add_argument("--database", default=DEFAULT_DB)
    ap.add_argument("--warehouse", default=DEFAULT_WH)
    ap.add_argument("--compute-pool", default="SYSTEM_COMPUTE_POOL_CPU")
    ap.add_argument("--mail-eai", help="Also attach this external access integration and the mail secrets "
                    "(not available on trial accounts)")
    args = ap.parse_args()
    db, wh, pool = ident(args.database, "--database"), ident(args.warehouse, "--warehouse"), ident(args.compute_pool, "--compute-pool")
    schema = f"{db}.PRODUCTION"
    stage = f"{schema}.APP_CODE"

    conn = snowflake.connector.connect(connection_name=args.connection, warehouse=wh)
    cur = conn.cursor()
    try:
        cur.execute(f"CREATE STAGE IF NOT EXISTS {stage} DIRECTORY = (ENABLE = TRUE) "
                    "COMMENT = '8D Intelligence: Streamlit app source'")
        cur.execute(f"REMOVE @{stage}")
        n = 0
        for pattern in FILES:
            for path in sorted(glob.glob(os.path.join(ROOT, pattern))):
                rel = os.path.dirname(os.path.relpath(path, ROOT))
                cur.execute(f"PUT 'file://{path}' @{stage}/{rel} AUTO_COMPRESS = FALSE OVERWRITE = TRUE")
                n += 1
        print(f"uploaded {n} files to @{stage}")

        role = cur.execute("SELECT CURRENT_ROLE()").fetchone()[0]
        try:
            cur.execute(f"GRANT DATABASE ROLE SNOWFLAKE.PYPI_REPOSITORY_USER TO ROLE {role}")
        except snowflake.connector.ProgrammingError:
            pass  # already granted, or granted by an administrator
        eai = ""
        secrets = ""
        if args.mail_eai:
            eai = f" EXTERNAL_ACCESS_INTEGRATIONS = ({ident(args.mail_eai, '--mail-eai')})"
            secrets = (f" SECRETS = ('mail_smtp' = {schema}.EIGHT_D_SMTP_SECRET, "
                       f"'mail_sendgrid_key' = {schema}.EIGHT_D_SENDGRID_SECRET)")
        cur.execute(f"CREATE OR REPLACE STREAMLIT {schema}.{APP} FROM '@{stage}' "
                    "MAIN_FILE = 'snowflake_app/streamlit_app.py' "
                    f"QUERY_WAREHOUSE = {wh} RUNTIME_NAME = 'SYSTEM$ST_CONTAINER_RUNTIME_PY3_11' COMPUTE_POOL = {pool} "
                    f"ARTIFACT_REPOSITORIES = (snowflake.snowpark.pypi_shared_repository){eai}{secrets} "
                    "TITLE = '8D Intelligence'")
        cur.execute(f"ALTER STREAMLIT {schema}.{APP} ADD LIVE VERSION FROM LAST")
        acct = cur.execute("SELECT CURRENT_ORGANIZATION_NAME() || '-' || CURRENT_ACCOUNT_NAME()").fetchone()[0]
        print(f"deployed {schema}.{APP}: https://app.snowflake.com/{acct.lower().replace('-', '/', 1)}"
              f"/#/streamlit-apps/{schema}.{APP}")
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
