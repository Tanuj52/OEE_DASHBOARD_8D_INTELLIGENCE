"""
Settings for the Streamlit app: AI providers, Snowflake connection, pipeline
and display preferences.

Locally, settings persist to snowflake_app/.local_settings.json (0600,
git-ignored) so keys survive restarts. Plant-wide settings (SHARED: email,
automation, assistant, pipeline, business, digest) are also saved to the OPERATIONS.APP_SETTINGS
table, so inside Snowflake, where there is no durable local disk, they persist
and every viewer of the app sees the same configuration. Secrets never go there.

Object names are not hard-coded per account: every Snowflake object the app
uses lives in one database (see names()). Inside Snowflake that is the database
the Streamlit app is deployed in; locally it is EIGHT_D_DATABASE or
MANUFACTURING_8D. sql/setup.py creates the same layout in any database.
"""

import copy
import json
import os
import threading
import time
import tomllib

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE_PATH = os.path.join(APP_DIR, ".local_settings.json")

# Streamlit in Snowflake (container runtime) runs on SPCS, which mounts a session token here.
IN_SNOWFLAKE = os.path.exists("/snowflake/session/token")

_hosted: dict = {}


def hosted() -> dict | None:
    """Credentials for a public deployment outside Snowflake (Streamlit Community Cloud): the [snowflake]
    block of st.secrets. When present the app always uses them and the connection settings are locked."""
    if IN_SNOWFLAKE:
        return None
    if "v" not in _hosted:
        try:
            import streamlit as st

            sec = st.secrets.get("snowflake")
            sec = dict(sec) if sec and sec.get("account") and sec.get("user") else None
        except Exception:  # no secrets file (local runs, tests)
            sec = None
        _hosted["v"] = sec
    return _hosted["v"]

SECRET_FIELDS = {"api_key", "secret"}

DEFAULT_DATABASE = "MANUFACTURING_8D"
EMAIL_INTEGRATION = os.environ.get("EIGHT_D_EMAIL_INTEGRATION", "EIGHT_D_REPORT_EMAIL")
_database = {"name": None}


def set_database(name: str | None):
    """Bind the app to the database it runs in (the SiS app's CURRENT_DATABASE())."""
    if name and not os.environ.get("EIGHT_D_DATABASE"):
        _database["name"] = name.strip().strip('"').upper()


def database() -> str:
    return (os.environ.get("EIGHT_D_DATABASE") or _database["name"] or DEFAULT_DATABASE).upper()


def names(db: str | None = None) -> dict:
    """Every Snowflake object the app uses, derived from one database (the layout sql/setup.py builds)."""
    db = (db or database()).upper()
    return {"database": db, "data_schema": f"{db}.PRODUCTION", "ops_schema": f"{db}.OPERATIONS",
            "stage": f"{db}.PRODUCTION.REPORT_STAGE", "agent": f"{db}.OPERATIONS.PLANT_ASSISTANT",
            "integration": EMAIL_INTEGRATION, "roll_proc": f"{db}.PRODUCTION.ROLL_ORDER_BOOK",
            "roll_task": f"{db}.PRODUCTION.EIGHT_D_ROLL_ORDER_BOOK", "digest_build": f"{db}.OPERATIONS.BUILD_DAILY_DIGEST",
            "digest_send": f"{db}.OPERATIONS.SEND_DAILY_DIGEST", "digest_task": f"{db}.OPERATIONS.EIGHT_D_DAILY_DIGEST"}


# Plant-wide sections shared by every viewer through OPERATIONS.APP_SETTINGS (secrets stripped)
SHARED = ("email", "automation", "assistant", "pipeline", "business", "digest")
_SHARED_SECRETS = {("email", "smtp", "password"), ("email", "sendgrid", "api_key")}

PROVIDERS = {
    "snowflake": {
        "label": "Snowflake AI_COMPLETE", "cost": "Credits",
        "description": "Runs agents inside your Snowflake account with AI_COMPLETE. Billed as Snowflake credits.",
        "models": ["llama3.3-70b", "llama3.1-8b", "claude-3-5-sonnet", "mistral-7b", "snowflake-llama-3.3-70b"],
    },
    "groq": {
        "label": "Groq", "cost": "Free tier", "base_url": "https://api.groq.com/openai/v1",
        "description": "Hosted open models with very fast inference. Free tier with rate limits.",
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"], "key_url": "https://console.groq.com/keys",
    },
    "gemini": {
        "label": "Google Gemini", "cost": "Free tier", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "description": "Gemini models through Google AI Studio. Free tier with rate limits.",
        "models": ["gemini-2.0-flash", "gemini-1.5-flash"], "key_url": "https://aistudio.google.com/app/apikey",
    },
    "nvidia": {
        "label": "NVIDIA NIM", "cost": "Free credits", "base_url": "https://integrate.api.nvidia.com/v1",
        "description": "NVIDIA API catalog endpoints. Free developer credits.",
        "models": ["meta/llama-3.1-70b-instruct", "meta/llama-3.1-8b-instruct"], "key_url": "https://build.nvidia.com",
    },
    "ollama": {
        "label": "Ollama (local)", "cost": "Free, local",
        "description": "Runs open models on this machine. No key needed; requires the Ollama app.",
        "models": ["llama3.1:8b", "qwen2.5:7b", "mistral:7b"],
    },
    "openai": {
        "label": "OpenAI", "cost": "Paid", "base_url": "https://api.openai.com/v1",
        "description": "OpenAI hosted models. Paid API.",
        "models": ["gpt-4o-mini", "gpt-4o"], "key_url": "https://platform.openai.com/api-keys",
    },
}

# Providers offered in the UI. This edition is built for Snowflake, so only AI_COMPLETE is shown;
# the other clients stay wired up in services/llm.py for reference. Add names here to bring them back.
VISIBLE_PROVIDERS = ("snowflake",)


def defaults() -> dict:
    n = names()
    return {
        "active_provider": "snowflake",
        "providers": {
            "snowflake": {"mode": "embedded" if IN_SNOWFLAKE else ("hosted" if hosted() else "connection"), "connection_name": "",
                          "account": "", "user": "", "secret": "", "role": "", "warehouse": "",
                          "database": "", "schema": "", "model": "llama3.3-70b"},
            "groq": {"api_key": "", "model": "llama-3.3-70b-versatile"},
            "gemini": {"api_key": "", "model": "gemini-2.0-flash"},
            "nvidia": {"api_key": "", "model": "meta/llama-3.1-70b-instruct"},
            "ollama": {"base_url": "http://localhost:11434", "model": "llama3.1:8b"},
            "openai": {"api_key": "", "model": "gpt-4o-mini"},
        },
        "pipeline": {"temperature": 0.2, "max_tokens": 900},
        "display": {"refresh_s": 1.0, "follow_risk": True, "engineer_view": False},
        "data": {"source": "snowflake", "schema": n["data_schema"]},
        "email": {"transport": "snowflake", "integration": n["integration"],
                  "stage": n["stage"], "link_days": 7, "attach_docx": False,
                  "smtp": {"host": "", "port": 587, "security": "starttls", "username": "", "password": "",
                           "from_addr": "", "from_name": "8D Intelligence"},
                  "sendgrid": {"api_key": "", "from_addr": "", "from_name": "8D Intelligence"}},
        "ops": {"enabled": True, "schema": n["ops_schema"]},
        "assistant": {"enabled": True, "agent": n["agent"]},
        "automation": {"trip_after_s": 10, "plan_hours": 72, "urgent_hours": 24, "critical_hours": 4,
                       "auto_plan": True, "auto_8d_on_trip": True, "notify_on_trip": True,
                       "auto_complete_repair": False, "near_failure_machine": True},
        # business impact: contribution margin on lost output, idle crew per stopped machine
        "business": {"margin_pct": 35.0, "labour_usd_h": 45.0, "crew": 1},
        # daily digest email (Snowflake task EIGHT_D_DAILY_DIGEST): hour in the task's time zone
        "digest": {"hour": 7},
    }


def _merge(base: dict, extra: dict):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        elif k in base:
            base[k] = v


def _rebase(data: dict):
    """Saved object names that point at the default layout follow the app's database."""
    want, base = names(), names(DEFAULT_DATABASE)
    if want["database"] == DEFAULT_DATABASE:
        return
    for section, key, name in (("data", "schema", "data_schema"), ("email", "stage", "stage"),
                               ("ops", "schema", "ops_schema"), ("assistant", "agent", "agent")):
        if (data[section].get(key) or "").upper() in ("", base[name]):
            data[section][key] = want[name]


def load() -> dict:
    if hosted():
        set_database(hosted().get("database"))
    data = defaults()
    if not IN_SNOWFLAKE and not hosted() and os.path.exists(STORE_PATH):
        try:
            with open(STORE_PATH) as f:
                _merge(data, json.load(f))
        except (OSError, json.JSONDecodeError):
            pass
    _rebase(data)
    if data["active_provider"] not in VISIBLE_PROVIDERS:
        data["active_provider"] = VISIBLE_PROVIDERS[0]
    sf = data["providers"]["snowflake"]
    if sf["mode"] == "connection" and not sf["connection_name"]:
        conns = list_snowflake_connections()
        if conns:
            sf["connection_name"] = conns[0]["name"]
    return data


_save_lock = threading.Lock()


def save(data: dict) -> bool:
    """Persist settings locally (0600). Returns False inside Snowflake and on a hosted deployment, where
    there is no durable local disk (plant-wide settings still persist through OPERATIONS.APP_SETTINGS)."""
    if IN_SNOWFLAKE or hosted():
        return False
    with _save_lock:
        tmp = STORE_PATH + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, STORE_PATH)
    return True


def shared_part(data: dict) -> dict:
    """The plant-wide settings to store in Snowflake: SHARED sections plus the AI model, never secrets."""
    out = {k: copy.deepcopy(data[k]) for k in SHARED}
    for path in _SHARED_SECRETS:
        node = out
        for p in path[:-1]:
            node = node.get(p) or {}
        node.pop(path[-1], None)
    out["providers"] = {"snowflake": {"model": data["providers"]["snowflake"].get("model")}}
    return out


def apply_shared(data: dict, shared: dict | None):
    """Merge stored plant-wide settings into a session's settings (local secrets are kept)."""
    if shared:
        _merge(data, {k: v for k, v in shared.items() if k in SHARED or k == "providers"})


def update_provider(data: dict, name: str, values: dict):
    cfg = data["providers"][name]
    for k, v in values.items():
        if k not in cfg or v is None:
            continue
        if k in SECRET_FIELDS and v == "" and not values.get(f"clear_{k}"):
            continue  # empty secret input means "keep existing"
        cfg[k] = v.strip() if isinstance(v, str) else v


def secret_hint(v: str) -> str:
    return f"••••{v[-4:]}" if v and len(v) > 8 else ("••••" if v else "")


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------

_reach_cache: dict[str, tuple[float, bool]] = {}


def ollama_reachable(base_url: str) -> bool:
    import requests

    now = time.time()
    hit = _reach_cache.get(base_url)
    if hit and now - hit[0] < 20:
        return hit[1]
    try:
        ok = requests.get(f"{base_url.rstrip('/')}/api/tags", timeout=0.4).status_code == 200
    except Exception:
        ok = False
    _reach_cache[base_url] = (now, ok)
    return ok


def is_configured(name: str, cfg: dict) -> bool:
    if name == "ollama":
        return bool(cfg.get("base_url") and cfg.get("model")) and ollama_reachable(cfg["base_url"])
    if name == "snowflake":
        mode = cfg.get("mode")
        if mode == "embedded":
            return IN_SNOWFLAKE
        if mode == "hosted":
            return bool(hosted())
        if mode == "connection":
            return any(c["name"] == cfg.get("connection_name") for c in list_snowflake_connections())
        return bool(cfg.get("account") and cfg.get("user") and cfg.get("secret"))
    return bool(cfg.get("api_key"))


def describe_active(data: dict) -> dict:
    name = data["active_provider"]
    cfg = data["providers"][name]
    return {"provider": name, "label": PROVIDERS[name]["label"], "model": cfg.get("model", ""),
            "configured": is_configured(name, cfg)}


# ---------------------------------------------------------------------------
# Snowflake CLI connections (names and non-secret fields only)
# ---------------------------------------------------------------------------

def _snowflake_home() -> str:
    return os.environ.get("SNOWFLAKE_HOME", os.path.expanduser("~/.snowflake"))


def list_snowflake_connections() -> list[dict]:
    if IN_SNOWFLAKE or hosted():
        return []
    found: dict[str, dict] = {}
    home = _snowflake_home()
    for path, section in ((os.path.join(home, "connections.toml"), None), (os.path.join(home, "config.toml"), "connections")):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as f:
                doc = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        block = doc.get(section, {}) if section else doc
        for name, cfg in block.items():
            if isinstance(cfg, dict) and name not in found:
                found[name] = {"name": name, "account": cfg.get("account", ""), "user": cfg.get("user", ""),
                               "role": cfg.get("role", ""), "warehouse": cfg.get("warehouse", ""),
                               "authenticator": cfg.get("authenticator", "snowflake"), "source": os.path.basename(path)}
    return list(found.values())


def snapshot(data: dict) -> dict:
    return copy.deepcopy(data)
