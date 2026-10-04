"""
Where the Streamlit app reads its plant tables from.

"snowflake" (default) reads <app database>.PRODUCTION (or the schema set in
Settings) through the same session the agents use: the app's embedded
connection inside Snowflake, the saved/entered connection locally. If the
schema cannot be read the result carries the error and the app shows it
instead of the pages (no silent switch to files); the check is retried after
RETRY_S. "files" (offline demos and tests only) uses the bundled CSV files.
"""

import hashlib
import json
import time

from snowflake_app.core import data as D
from snowflake_app.services.llm import SnowflakePool
from snowflake_app.services.settings import names

RETRY_S = 120
_status: dict = {}


def _key(sf: dict, schema: str) -> str:
    ident = {k: sf.get(k) for k in ("mode", "connection_name", "account", "user", "role", "warehouse")}
    return hashlib.sha256(json.dumps([ident, schema.upper()], sort_keys=True).encode()).hexdigest()[:16]


def apply(settings: dict) -> dict:
    """Point the shared data layer at the configured source. Returns {kind, schema, error, changed}."""
    want = settings["data"]["source"]
    schema = (settings["data"].get("schema") or names()["data_schema"]).strip()
    before = (D.source_info()["kind"], D.source_info()["schema"])
    if want == "files":
        D.use_files()
        _status.clear()
        return {"kind": "files", "wanted": "files", "schema": None, "error": None, "changed": before != ("files", None)}

    sf = dict(settings["providers"]["snowflake"])
    key = _key(sf, schema)
    st = _status.get(key)
    if st and st.get("error") and time.time() - st["checked"] < RETRY_S:
        D.use_files()
        return {"kind": "files", "wanted": want, "schema": schema, "error": st["error"], "changed": before != ("files", None)}
    try:
        D.use_snowflake(lambda sql: SnowflakePool.query_df(sf, sql), schema, key=key)
        if not st or st.get("error"):
            D.table("machines")  # probe once per configuration
        _status.clear()
        _status[key] = {"error": None, "checked": time.time()}
        info = D.source_info()
        return {"kind": "snowflake", "wanted": want, "schema": info["schema"], "error": None,
                "changed": before != ("snowflake", info["schema"])}
    except Exception as e:
        _status[key] = {"error": str(e).split("\n")[0][:300], "checked": time.time()}
        D.use_files()
        return {"kind": "files", "wanted": want, "schema": schema, "error": _status[key]["error"],
                "changed": before != ("files", None)}


def retry():
    _status.clear()
