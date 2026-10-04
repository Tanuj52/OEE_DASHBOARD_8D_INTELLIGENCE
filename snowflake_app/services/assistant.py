"""
Plant Assistant: chat with the PLANT_ASSISTANT Cortex Agent (sql/09_assistant.sql).

Inside Snowflake (container runtime) the app calls the Cortex Agents REST API
with the container's OAuth session token:
    POST https://$SNOWFLAKE_HOST/api/v2/databases/{db}/schemas/{schema}/agents/{name}:run
Locally, or if REST is unavailable, it runs SNOWFLAKE.CORTEX.DATA_AGENT_RUN over
SQL on a dedicated connection, so a 10-30 s agent turn never blocks the shared
connection the pages and the operations writer use.

Each turn runs on a worker thread; the chat UI polls the job. The user's message
is prefixed with a short [Live context] block (what is down, what is at risk, the
machine on screen) because live state can be newer than the tables.
"""

import json
import os
import threading
import time
import uuid

from snowflake_app.services.llm import SnowflakePool
from snowflake_app.services.settings import IN_SNOWFLAKE, names

TOKEN_FILE = "/snowflake/session/token"
HISTORY_TURNS = 6
TIMEOUT_S = 120
JOBS: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_conns: dict[str, object] = {}
_conn_lock = threading.Lock()


class AssistantError(RuntimeError):
    pass


def agent_name(settings: dict) -> str:
    return ((settings.get("assistant") or {}).get("agent") or names()["agent"]).strip()


def _parts(fqn: str) -> tuple[str, str, str]:
    p = [x.strip('"') for x in fqn.split(".")]
    if len(p) != 3 or not all(p):
        raise AssistantError(f"Agent must be DATABASE.SCHEMA.NAME, got {fqn!r}")
    return p[0], p[1], p[2]


def _body(history: list[dict], question: str, context: str) -> dict:
    msgs = []
    for m in history[-HISTORY_TURNS * 2:]:
        if m.get("text"):
            msgs.append({"role": "assistant" if m["role"] == "assistant" else "user",
                         "content": [{"type": "text", "text": m["text"]}]})
    text = f"[Live context]\n{context}\n[/Live context]\n\n{question}" if context else question
    msgs.append({"role": "user", "content": [{"type": "text", "text": text}]})
    return {"messages": msgs, "stream": False}


def parse(resp: dict) -> dict:
    """Agent response -> {text, tables[{title, columns, rows}], charts[spec], sql[], suggestions[]}."""
    out = {"text": "", "tables": [], "charts": [], "sql": [], "suggestions": []}
    texts = []
    for c in resp.get("content") or []:
        t = c.get("type")
        if t == "text" and c.get("text"):
            texts.append(c["text"].strip())
        elif t == "table":
            tb = c.get("table") or {}
            rs = tb.get("result_set") or {}
            cols = [r["name"] for r in (rs.get("resultSetMetaData") or {}).get("rowType", [])]
            out["tables"].append({"title": (tb.get("title") or "").strip(), "columns": cols, "rows": rs.get("data") or []})
        elif t == "chart":
            try:
                out["charts"].append(json.loads((c.get("chart") or {}).get("chart_spec") or "{}"))
            except ValueError:
                pass
        elif t == "tool_result":
            for item in (c.get("tool_result") or {}).get("content") or []:
                sql = (item.get("json") or {}).get("sql") if isinstance(item, dict) else None
                if sql:
                    out["sql"].append(sql)
        elif t == "suggested_queries":
            out["suggestions"] = [q.get("query") for q in c.get("suggested_queries") or [] if q.get("query")]
    # the last text block is the answer; earlier ones narrate the tool calls
    out["text"] = texts[-1] if texts else ""
    out["steps"] = texts[:-1]
    return out


def _rest(fqn: str, body: dict) -> dict:
    import requests

    db, schema, name = _parts(fqn)
    host = os.environ.get("SNOWFLAKE_HOST")
    if not (host and os.path.exists(TOKEN_FILE)):
        raise AssistantError("REST not available")
    with open(TOKEN_FILE) as f:
        token = f.read().strip()
    r = requests.post(f"https://{host}/api/v2/databases/{db}/schemas/{schema}/agents/{name}:run", json=body, timeout=TIMEOUT_S,
                      headers={"Authorization": f"Bearer {token}", "X-Snowflake-Authorization-Token-Type": "OAUTH",
                               "Content-Type": "application/json", "Accept": "application/json"})
    if r.status_code >= 300:
        raise AssistantError(f"Agent API returned {r.status_code}: {r.text[:300]}")
    return r.json()


def _sql(settings: dict, fqn: str, body: dict) -> dict:
    sf = settings["providers"]["snowflake"]
    kw = SnowflakePool._kwargs(sf)
    if kw.get("embedded"):
        rows = SnowflakePool.query(sf, "SELECT SNOWFLAKE.CORTEX.DATA_AGENT_RUN(%s, %s)", (fqn, json.dumps(body)))
    else:
        import snowflake.connector

        key = json.dumps(kw, sort_keys=True, default=str)
        with _conn_lock:
            conn = _conns.get(key)
            if conn is None or conn.is_closed():
                conn = _conns[key] = snowflake.connector.connect(**kw)
            with conn.cursor() as cur:
                cur.execute("SELECT SNOWFLAKE.CORTEX.DATA_AGENT_RUN(%s, %s)", (fqn, json.dumps(body)))
                rows = cur.fetchall()
    raw = rows[0][0]
    return json.loads(raw) if isinstance(raw, str) else raw


def ask(settings: dict, history: list[dict], question: str, context: str = "") -> dict:
    fqn = agent_name(settings)
    body = _body(history, question, context)
    t0 = time.time()
    via = "rest"
    try:
        if not IN_SNOWFLAKE:
            raise AssistantError("local")
        resp = _rest(fqn, body)
    except AssistantError:
        via = "sql"
        try:
            resp = _sql(settings, fqn, body)
        except Exception as e:
            msg = str(e).splitlines()[0][:300]
            if "does not exist" in msg.lower() or "not authorized" in msg.lower():
                msg = f"The Plant Assistant agent {fqn} is not available to this role. Run sql/09_assistant.sql. ({msg})"
            raise AssistantError(msg) from e
    out = parse(resp)
    out.update(latency_s=round(time.time() - t0, 1), via=via)
    return out


# ---------------------------------------------------------------------------
# Background jobs (the chat UI polls these)
# ---------------------------------------------------------------------------

def submit(settings: dict, history: list[dict], question: str, context: str = "") -> str:
    job = uuid.uuid4().hex[:10]
    with _jobs_lock:
        JOBS[job] = {"status": "running", "started": time.time()}
        for k in [k for k, v in JOBS.items() if time.time() - v.get("started", 0) > 3600]:
            JOBS.pop(k, None)

    def run():
        try:
            res = ask(settings, history, question, context)
            JOBS[job] = {**JOBS[job], "status": "done", "result": res}
        except Exception as e:
            JOBS[job] = {**JOBS[job], "status": "failed", "error": str(e)[:400]}

    threading.Thread(target=run, daemon=True, name=f"assistant-{job}").start()
    return job


def poll(job: str) -> dict | None:
    return JOBS.get(job)


_avail: dict = {}


def available(settings: dict) -> dict:
    """{ok, error} - whether the agent exists for this role (cached 5 minutes)."""
    fqn = agent_name(settings)
    hit = _avail.get(fqn)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    try:
        db, schema, name = _parts(fqn)
        rows = SnowflakePool.query(settings["providers"]["snowflake"], f'SHOW AGENTS LIKE %s IN SCHEMA "{db}"."{schema}"', (name,))
        res = {"ok": bool(rows), "error": None if rows else f"{fqn} was not found. Run sql/09_assistant.sql."}
    except Exception as e:
        res = {"ok": False, "error": str(e).splitlines()[0][:200]}
    _avail[fqn] = (time.time(), res)
    return res
