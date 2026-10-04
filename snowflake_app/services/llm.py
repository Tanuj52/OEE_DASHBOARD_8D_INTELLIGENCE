"""
LLM client for the Streamlit app.

complete(settings, system, prompt, on_token) -> LLMResult for every provider:
- Snowflake: AI_COMPLETE with bind parameters (never string-built SQL) over a
  pooled connector session. Inside Snowflake it reuses the app's embedded
  st.connection("snowflake"); locally it uses ~/.snowflake/connections.toml or
  credentials entered in Settings.
- Groq / Gemini / NVIDIA NIM / OpenAI / Ollama: their OpenAI-compatible
  /chat/completions endpoints with server-sent-event streaming, so no
  LangChain dependency is needed (inside Snowflake these need an external
  access integration).
"""

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field

from snowflake_app.services.settings import PROVIDERS, hosted, is_configured


class ProviderNotConfigured(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    latency_s: float
    usage: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Snowflake
# ---------------------------------------------------------------------------

_key_der: dict = {}


def _hosted_kwargs() -> dict:
    """Connector arguments from st.secrets["snowflake"] (public deployment). A key pair is preferred because a
    hosted app cannot answer an MFA prompt; a password or programmatic access token also works."""
    sec = hosted()
    if not sec:
        raise ProviderNotConfigured("This deployment has no Snowflake credentials in its secrets.")
    kw = {"account": sec["account"], "user": sec["user"]}
    pem = sec.get("private_key")
    if pem:
        if pem not in _key_der:
            from cryptography.hazmat.primitives import serialization

            pw = sec.get("private_key_passphrase")
            key = serialization.load_pem_private_key(pem.encode(), password=pw.encode() if pw else None)
            _key_der[pem] = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption())
        kw["private_key"] = _key_der[pem]
    elif sec.get("password"):
        kw["password"] = sec["password"]
    else:
        raise ProviderNotConfigured("Add private_key (or password) to the [snowflake] secrets of this deployment.")
    for k in ("role", "warehouse", "database", "schema"):
        if sec.get(k):
            kw[k] = sec[k]
    kw["client_session_keep_alive"] = True
    kw["login_timeout"] = 30
    return kw

class SnowflakePool:
    """One shared connector connection per connection config, guarded by a lock."""

    _lock = threading.RLock()
    _conns: dict[str, object] = {}
    embedded = None  # raw connector connection from st.connection("snowflake") inside Snowflake

    @staticmethod
    def _kwargs(cfg: dict) -> dict:
        mode = cfg.get("mode")
        if mode == "hosted":
            return _hosted_kwargs()
        if mode == "connection":
            if not cfg.get("connection_name"):
                raise ProviderNotConfigured("Select a Snowflake connection in Settings.")
            kw = {"connection_name": cfg["connection_name"]}
        elif mode == "manual":
            if not (cfg.get("account") and cfg.get("user") and cfg.get("secret")):
                raise ProviderNotConfigured("Enter Snowflake account, user and password/PAT in Settings.")
            kw = {"account": cfg["account"], "user": cfg["user"], "password": cfg["secret"]}
        else:
            return {"embedded": True}
        for k in ("role", "warehouse", "database", "schema"):
            if cfg.get(k):
                kw[k] = cfg[k]
        kw["client_session_keep_alive"] = True
        kw["login_timeout"] = 30
        return kw

    @classmethod
    def _get(cls, cfg: dict):
        kw = cls._kwargs(cfg)
        if kw.get("embedded"):
            if cls.embedded is None:
                raise ProviderNotConfigured("The embedded Snowflake connection is not available in this session.")
            return cls.embedded
        import snowflake.connector

        key = hashlib.sha256(json.dumps(kw, sort_keys=True, default=str).encode()).hexdigest()
        conn = cls._conns.get(key)
        if conn is None or conn.is_closed():
            conn = snowflake.connector.connect(**kw)
            cls._conns[key] = conn
        return conn

    @classmethod
    def query(cls, cfg: dict, sql: str, params: tuple = ()):
        return cls._run(cfg, sql, params, lambda cur: cur.fetchall())

    @classmethod
    def query_df(cls, cfg: dict, sql: str):
        """Run a query and return a pandas DataFrame (Arrow result batches)."""
        return cls._run(cfg, sql, (), lambda cur: cur.fetch_pandas_all())

    @classmethod
    def _run(cls, cfg: dict, sql: str, params: tuple, fetch):
        with cls._lock:
            for attempt in (1, 2):
                conn = cls._get(cfg)
                try:
                    cur = conn.cursor()
                    try:
                        cur.execute(sql, params)
                        return fetch(cur)
                    finally:
                        cur.close()
                except Exception as e:
                    # Retry once on an expired/broken session; surface real SQL errors.
                    if attempt == 2 or "session" not in str(e).lower() or cfg.get("mode") == "embedded":
                        raise
                    cls.reset()

    @classmethod
    def reset(cls):
        with cls._lock:
            for c in cls._conns.values():
                try:
                    c.close()
                except Exception:
                    pass
            cls._conns.clear()


AI_COMPLETE_SQL = (
    "SELECT AI_COMPLETE(model => %s, prompt => %s, "
    "model_parameters => OBJECT_CONSTRUCT('temperature', %s::FLOAT, 'max_tokens', %s::INT), "
    "show_details => TRUE)"
)


def _snowflake_complete(cfg, system, prompt, temperature, max_tokens) -> LLMResult:
    model = cfg.get("model") or "llama3.3-70b"
    full_prompt = f"{system}\n\n{prompt}" if system else prompt
    t0 = time.time()
    rows = SnowflakePool.query(cfg, AI_COMPLETE_SQL, (model, full_prompt, temperature, max_tokens))
    raw = rows[0][0] if rows else ""
    try:
        details = json.loads(raw)
        text = details["choices"][0]["messages"]
        usage = details.get("usage", {})
    except (ValueError, KeyError, IndexError, TypeError):
        text, usage = str(raw), {}
    return LLMResult(text=text.strip(), provider="snowflake", model=model, latency_s=round(time.time() - t0, 2), usage=usage)


# ---------------------------------------------------------------------------
# OpenAI-compatible providers (streaming)
# ---------------------------------------------------------------------------

def _base_url(name: str, cfg: dict) -> str:
    if name == "ollama":
        return cfg.get("base_url", "http://localhost:11434").rstrip("/") + "/v1"
    return PROVIDERS[name]["base_url"]


def _http_complete(name, cfg, system, prompt, temperature, max_tokens, on_token) -> LLMResult:
    import requests

    body = {"model": cfg["model"], "temperature": temperature, "max_tokens": max_tokens, "stream": True,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
    if name in ("openai", "groq", "nvidia"):
        body["stream_options"] = {"include_usage": True}
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    t0 = time.time()
    parts, usage = [], {}
    with requests.post(f"{_base_url(name, cfg)}/chat/completions", json=body, headers=headers,
                       stream=True, timeout=(10, 120)) as r:
        if r.status_code >= 400:
            try:
                err = r.json()
                msg = (err.get("error") or {}).get("message") if isinstance(err, dict) else None
                msg = msg or (err[0].get("error", {}).get("message") if isinstance(err, list) and err else None) or r.text
            except ValueError:
                msg = r.text
            raise RuntimeError(f"{PROVIDERS[name]['label']} returned HTTP {r.status_code}: {str(msg)[:300]}")
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            for ch in chunk.get("choices") or []:
                piece = (ch.get("delta") or {}).get("content") or ""
                if piece:
                    parts.append(piece)
                    if on_token:
                        on_token(piece)
            if chunk.get("usage"):
                u = chunk["usage"]
                usage = {"prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens"),
                         "total_tokens": u.get("total_tokens")}
    return LLMResult(text="".join(parts).strip(), provider=name, model=cfg.get("model", ""),
                     latency_s=round(time.time() - t0, 2), usage=usage)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def complete(settings: dict, system: str, prompt: str, provider: str | None = None, on_token=None,
             temperature: float | None = None, max_tokens: int | None = None) -> LLMResult:
    name = provider or settings["active_provider"]
    cfg = settings["providers"][name]
    if not is_configured(name, cfg):
        raise ProviderNotConfigured(f"{PROVIDERS[name]['label']} is not configured. Open Settings to add credentials.")
    pipe = settings["pipeline"]
    temperature = pipe["temperature"] if temperature is None else temperature
    max_tokens = pipe["max_tokens"] if max_tokens is None else max_tokens
    if name == "snowflake":
        return _snowflake_complete(cfg, system, prompt, temperature, max_tokens)
    return _http_complete(name, cfg, system, prompt, temperature, max_tokens, on_token)


def bound_complete(settings: dict):
    """A complete(system, prompt, on_token) callable frozen to a settings snapshot (for worker threads)."""
    def fn(system, prompt, on_token=None):
        return complete(settings, system, prompt, on_token=on_token)
    return fn


def test_provider(settings: dict, name: str) -> dict:
    """Small round trip to verify credentials. Returns latency, sample reply and session context."""
    t0 = time.time()
    result = {"provider": name, "ok": False}
    try:
        if name == "snowflake":
            cfg = settings["providers"]["snowflake"]
            if cfg.get("mode") != "embedded":
                SnowflakePool.reset()
            row = SnowflakePool.query(
                cfg, "SELECT CURRENT_ACCOUNT_NAME(), CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE(), CURRENT_REGION()")[0]
            result["session"] = dict(zip(["account", "user", "role", "warehouse", "region"], row))
        reply = complete(settings, "You are a connectivity check.", "Reply with exactly: READY", provider=name,
                         temperature=0.0, max_tokens=8)
        result.update(ok=True, reply=reply.text, model=reply.model, latency_s=round(time.time() - t0, 2))
    except Exception as e:
        result.update(error=str(e).split("\n")[0][:400], latency_s=round(time.time() - t0, 2))
    return result
