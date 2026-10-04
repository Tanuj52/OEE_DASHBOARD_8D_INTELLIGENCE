import pandas as pd
import streamlit as st

from snowflake_app.core import data as D
from snowflake_app.core.pipeline import AGENTS, STEPS
from snowflake_app.services import datasource, digest_ops, mailer, ops_store
from snowflake_app.services import settings as S
from snowflake_app.services.llm import test_provider
from snowflake_app.ui import kit as K, shell

PATH = "app_pages/settings.py"
shell.topbar(PATH)
ss = st.session_state
cfg = shell.settings()
ss.setdefault("sel_provider", cfg["active_provider"])
ss.setdefault("test_results", {})


def persist(msg: str):
    if shell.save_settings():
        shell.notify(msg, icon=":material/check_circle:")
    else:
        shell.notify(msg + " (kept for this session: the operations schema is not writable, so it is not shared)",
                 icon=":material/info:")


def show_test(name: str):
    r = ss.test_results.get(name)
    if not r:
        return
    if r["ok"]:
        sess = r.get("session") or {}
        extra = " · ".join(f"{k} **{v}**" for k, v in sess.items() if v)
        st.success(f"Connected in {r['latency_s']}s · model `{r.get('model')}` replied “{r.get('reply')}”" + (f"  \n{extra}" if extra else ""),
                   icon=":material/check_circle:")
    else:
        st.error(f"Test failed after {r['latency_s']}s: {r.get('error')}", icon=":material/error:")


def run_test(name: str):
    with st.spinner(f"Testing {S.PROVIDERS[name]['label']}…"):
        ss.test_results[name] = test_provider(S.snapshot(cfg), name)


# on_click callbacks (see shell.goto): the topbar/sidebar fragments rerun on a timer
def _test(name: str):
    ss.test_results[name] = test_provider(S.snapshot(cfg), name)


def _use_provider(name: str):
    cfg["active_provider"] = name
    persist(f"{S.PROVIDERS[name]['label']} is now the active provider")


def _clear_key(name: str):
    S.update_provider(cfg, name, {"api_key": "", "clear_api_key": True})
    ss.test_results.pop(name, None)
    persist(f"{S.PROVIDERS[name]['label']} key removed")


def _save_data_source():
    cfg["data"]["source"] = ss.get("ds_kind") or cfg["data"]["source"]
    cfg["data"]["schema"] = (ss.get("ds_schema") or S.names()["data_schema"]).strip()
    shell.set_plant_source(cfg["data"])
    datasource.retry()
    res = shell.apply_data_source()
    st.cache_data.clear()
    try:
        counts = D.check_source() if not res["error"] else None
        ss.ds_check = {"ok": not res["error"], "res": res, "counts": counts}
    except Exception as e:
        ss.ds_check = {"ok": False, "res": res, "error": str(e)}
    persist("Data source saved")


def model_picker(name: str, prefix: str) -> str:
    meta = S.PROVIDERS[name]
    cur = cfg["providers"][name]["model"]
    opts = meta["models"] + ([cur] if cur not in meta["models"] else [])
    c1, c2 = st.columns(2)
    pick = c1.selectbox("Model", opts, index=opts.index(cur), key=f"{prefix}_model")
    custom = c2.text_input("Or a custom model ID", key=f"{prefix}_custom", placeholder="Optional, e.g. llama3.1-405b")
    return custom.strip() or pick


TABS = ([("providers", ":material/memory: AI providers")] if len(S.VISIBLE_PROVIDERS) > 1 else []) + [
    ("snowflake", ":material/database: Snowflake & AI"), ("email", ":material/mail: Email & recipients"),
    ("automation", ":material/bolt: Automation"), ("business", ":material/payments: Business"),
    ("pipeline", ":material/account_tree: Agent pipeline"),
    ("appearance", ":material/palette: Appearance & accessibility")]
tabs = dict(zip([k for k, _ in TABS], st.tabs([label for _, label in TABS])))

# --------------------------------------------------------------------- providers
def providers_tab():
    """Provider picker; only shown when more than one provider is enabled (S.VISIBLE_PROVIDERS)."""
    left, right = st.columns([5, 7])
    names = list(S.VISIBLE_PROVIDERS)

    def caption(n):
        c = cfg["providers"][n]
        ready = S.is_configured(n, c)
        state = "Ready" if ready else ("Not running" if n == "ollama" else ("Needs key" if n != "snowflake" else "Needs connection"))
        return f"{c.get('model')} · {S.PROVIDERS[n]['cost']} · {state}" + (" · ACTIVE" if n == cfg["active_provider"] else "")

    with left, K.card("plist"):
        st.html(K.card_title("Providers", "Pick a provider to configure. The active one runs the 8D agents.", "cpu"))
        st.radio("Provider", names, key="sel_provider", label_visibility="collapsed",
                 format_func=lambda n: ("★ " if n == cfg["active_provider"] else "") + S.PROVIDERS[n]["label"],
                 captions=[caption(n) for n in names])

    name = ss.sel_provider
    meta = S.PROVIDERS[name]
    pc = cfg["providers"][name]
    ready = S.is_configured(name, pc)
    with right, K.card("pcfg", accent=name == cfg["active_provider"]):
        badge = K.pill("critical", "Active provider", "xs") if name == cfg["active_provider"] else K.pill("healthy" if ready else "warning", "Ready" if ready else "Setup needed", "xs")
        st.html(K.card_title(meta["label"], meta["description"], "database" if name == "snowflake" else "cpu", badge))
        if name == "snowflake":
            st.html('<div class="sx-muted" style="font-size:12.5px">Connection, role, warehouse and model are configured in the '
                    '<b>Snowflake connection</b> tab. AI_COMPLETE runs with bind parameters inside your account.</div>')
        else:
            with st.form(f"pform_{name}", border=False):
                if name == "ollama":
                    base = st.text_input("Base URL", value=pc["base_url"], key=f"p_{name}_url")
                    key_in = None
                else:
                    hint = S.secret_hint(pc.get("api_key", ""))
                    key_in = st.text_input("API key", type="password", key=f"p_{name}_key", autocomplete="off",
                                           placeholder=f"Saved {hint} · leave blank to keep" if hint else "Paste your API key",
                                           help="Stored locally in snowflake_app/.local_settings.json with 0600 permissions; never shown again.")
                model = model_picker(name, f"p_{name}")
                c1, c2 = st.columns(2)
                save_test = c1.form_submit_button("Save & test", type="primary", icon=":material/bolt:", width="stretch")
                save_only = c2.form_submit_button("Save only", width="stretch")
            if save_test or save_only:
                vals = {"model": model}
                if name == "ollama":
                    vals["base_url"] = base
                    S._reach_cache.clear()
                else:
                    vals["api_key"] = key_in or ""
                S.update_provider(cfg, name, vals)
                persist(f"{meta['label']} settings saved")
                if save_test:
                    run_test(name)
            if meta.get("key_url"):
                st.link_button("Get an API key", meta["key_url"], icon=":material/open_in_new:", type="tertiary")
            if S.IN_SNOWFLAKE:
                st.caption("Inside Snowflake, calls to this provider need an external access integration on the Streamlit app.")
        with st.container(horizontal=True):
            if name != cfg["active_provider"]:
                st.button("Use for agents", icon=":material/star:", key=f"use_{name}", disabled=not ready,
                          on_click=_use_provider, args=(name,))
            if name == "snowflake":
                st.button("Test connection", icon=":material/bolt:", key="test_sf_short", on_click=_test, args=("snowflake",))
            if name not in ("snowflake", "ollama") and pc.get("api_key"):
                st.button("Clear key", icon=":material/delete:", key=f"clear_{name}", on_click=_clear_key, args=(name,))
        show_test(name)

if "providers" in tabs:
    with tabs["providers"]:
        providers_tab()

# --------------------------------------------------------------------- snowflake
with tabs["snowflake"]:
    sf = cfg["providers"]["snowflake"]
    left, right = st.columns([7, 5])
    with left, K.card("sf", accent=cfg["active_provider"] == "snowflake"):
        st.html(K.card_title("Snowflake connection", "Used by AI_COMPLETE for the agents. Queries run with this role and warehouse.", "database",
                             (K.pill("critical", "Active provider", "xs") if len(S.VISIBLE_PROVIDERS) > 1 else K.pill("healthy", "Runs the agents", "xs"))
                             if cfg["active_provider"] == "snowflake" else ""))
        if S.IN_SNOWFLAKE or S.hosted():
            if S.IN_SNOWFLAKE:
                st.info("Running inside Snowflake: the agents use this Streamlit app's embedded connection and owner role. No credentials needed.",
                        icon=":material/verified_user:")
            else:
                h = S.hosted()
                st.info(f"This deployment connects to Snowflake with its own credentials (account **{K.esc(h['account'])}**, "
                        f"role **{K.esc(h.get('role') or 'default')}**). The connection is fixed here; only the model can be changed.",
                        icon=":material/verified_user:")
            with st.form("sf_embedded", border=False):
                model = model_picker("snowflake", "sfe")
                c1, c2 = st.columns(2)
                st_test = c1.form_submit_button("Save & test", type="primary", icon=":material/bolt:", width="stretch")
                st_save = c2.form_submit_button("Save only", width="stretch")
            if st_test or st_save:
                S.update_provider(cfg, "snowflake", {"model": model, "mode": "embedded" if S.IN_SNOWFLAKE else "hosted"})
                persist("Snowflake settings saved")
                if st_test:
                    run_test("snowflake")
        else:
            conns = S.list_snowflake_connections()
            mode = st.segmented_control("Authentication source", ["connection", "manual"], default=sf["mode"] if sf["mode"] in ("connection", "manual") else "connection",
                                        key="sf_mode", format_func=lambda m: ":material/description: Saved connection" if m == "connection" else ":material/key: Enter credentials")
            mode = mode or "connection"
            with st.form("sf_form", border=False):
                vals = {"mode": mode}
                if mode == "connection":
                    if not conns:
                        st.warning("No connections found in ~/.snowflake/connections.toml. Add one with `snow connection add`, or enter credentials.",
                                   icon=":material/warning:")
                    names = [c["name"] for c in conns]
                    by = {c["name"]: c for c in conns}
                    if names:
                        sel = st.selectbox("Connection", names, index=names.index(sf["connection_name"]) if sf["connection_name"] in names else 0,
                                           key="sf_conn", format_func=lambda n: f"{n} · {by[n]['account']}")
                        c = by[sel]
                        st.html('<div class="sx-inset" style="padding:10px 12px;display:grid;grid-template-columns:1fr 1fr;gap:8px 14px">' + "".join(
                            f'<div><div class="sx-label">{k}</div><div style="font-size:13px;font-weight:500">{K.esc(v or "—")}</div></div>'
                            for k, v in (("Account", c["account"]), ("User", c["user"]), ("Authenticator", c["authenticator"]), ("Source", c["source"]))) + "</div>")
                        vals["connection_name"] = sel
                else:
                    c1, c2 = st.columns(2)
                    vals["account"] = c1.text_input("Account identifier", value=sf["account"], placeholder="orgname-accountname", key="sf_acct")
                    vals["user"] = c2.text_input("User", value=sf["user"], key="sf_user")
                    hint = S.secret_hint(sf.get("secret", ""))
                    vals["secret"] = st.text_input("Password or programmatic access token", type="password", key="sf_secret", autocomplete="off",
                                                   placeholder=f"Saved {hint} · leave blank to keep" if hint else "Password or PAT")
                c1, c2 = st.columns(2)
                vals["role"] = c1.text_input("Role", value=sf["role"], key="sf_role", help="Optional; defaults to the user's default role")
                vals["warehouse"] = c2.text_input("Warehouse", value=sf["warehouse"], key="sf_wh", help="Optional")
                vals["database"] = c1.text_input("Database", value=sf["database"], key="sf_db", help="Optional")
                vals["schema"] = c2.text_input("Schema", value=sf["schema"], key="sf_schema", help="Optional")
                vals["model"] = model_picker("snowflake", "sf")
                st.caption("Model availability depends on your region and cross-region inference setting.")
                c1, c2 = st.columns(2)
                st_test = c1.form_submit_button("Save & test", type="primary", icon=":material/bolt:", width="stretch")
                st_save = c2.form_submit_button("Save only", width="stretch")
            if st_test or st_save:
                S.update_provider(cfg, "snowflake", vals)
                persist("Snowflake connection saved")
                if st_test:
                    run_test("snowflake")
        show_test("snowflake")
    with right, K.card("datasrc"):
        ds = ss.get("data_source") or {}
        live_badge = K.pill("healthy", "Connected", "xs") if ds.get("kind") == "snowflake" else K.pill("warning" if ds.get("error") else "pending", "Local files", "xs")
        st.html(K.card_title("Plant data source", "Where every page and agent reads the plant tables", "layers", live_badge))
        ss.setdefault("ds_kind", cfg["data"]["source"])
        ss.setdefault("ds_schema", cfg["data"]["schema"])
        locked = bool(S.hosted())  # a public deployment always reads its own database
        st.segmented_control("Source", ["snowflake", "files"], key="ds_kind", disabled=locked,
                             format_func=lambda k: ":material/database: Snowflake tables" if k == "snowflake" else ":material/folder: Local files")
        st.text_input("Schema", key="ds_schema", disabled=locked or ss.get("ds_kind") != "snowflake",
                      help="DATABASE.SCHEMA holding the tables; load it with sql/load_data.py. Read with the connection on the left.")
        if not locked:
            st.button("Save & load", icon=":material/sync:", type="primary", key="ds_save", on_click=_save_data_source)
        chk = ss.get("ds_check")
        if ds.get("error"):
            st.error(f"Snowflake tables unavailable: {ds['error']}. Pages stay closed until they can be read.",
                     icon=":material/error:")
        elif chk and chk.get("counts"):
            st.success(f"Loaded {sum(chk['counts'].values()):,} rows from {len(chk['counts'])} tables"
                       + (f" in **{ds.get('schema')}**" if ds.get("kind") == "snowflake" else ""), icon=":material/check_circle:")
        elif chk and chk.get("error"):
            st.error(chk["error"], icon=":material/error:")
        elif ds.get("kind") == "snowflake":
            st.caption(f"Reading **{ds.get('schema')}** through the Snowflake connection.")
    with right, K.card("sfhow"):
        st.html(K.card_title("How the agents call Snowflake", None, "info") +
                '<div class="sx-muted" style="font-size:12.5px;display:grid;gap:8px">'
                '<div>Each agent sends one parameterised statement; prompts are bound, never concatenated into SQL:</div>'
                '<pre style="background:#F5F4F2;border:1px solid #E5E2DE;border-radius:8px;padding:10px;font-size:11.5px;white-space:pre-wrap;margin:0" class="sx-mono">'
                "SELECT AI_COMPLETE(\n  model => %s,\n  prompt => %s,\n  model_parameters => OBJECT_CONSTRUCT(\n    'temperature', %s::FLOAT,\n    'max_tokens', %s::INT),\n  show_details => TRUE)</pre>"
                '<div>Token usage from <span class="sx-mono">show_details</span> is shown per agent and per run. Calls are billed as Snowflake credits.</div></div>')

# ------------------------------------------------------------------------- email
def _save_email():
    em = cfg["email"]
    em["transport"] = ss.get("em_transport") or em.get("transport", "snowflake")
    em.update(integration=(ss.get("em_integ") or "").strip(), stage=(ss.get("em_stage") or "").strip(),
              link_days=int(ss.get("em_days") or 7), attach_docx=bool(ss.get("em_docx")))
    sm = em["smtp"]
    sm.update(host=(ss.get("smtp_host") or "").strip(), port=int(ss.get("smtp_port") or 587), security=ss.get("smtp_sec") or "starttls",
              username=(ss.get("smtp_user") or "").strip(), from_addr=(ss.get("smtp_from") or "").strip(),
              from_name=(ss.get("smtp_name") or "").strip() or "8D Intelligence")
    if ss.get("smtp_pass"):
        sm["password"] = ss.smtp_pass
    sg = em["sendgrid"]
    sg.update(from_addr=(ss.get("sg_from") or "").strip(), from_name=(ss.get("sg_name") or "").strip() or "8D Intelligence")
    if ss.get("sg_key"):
        sg["api_key"] = ss.sg_key.strip()
    ss.pop("smtp_pass", None)
    ss.pop("sg_key", None)
    ss.em_check = mailer.ready(cfg, force=True)
    persist("Email settings saved")


def _check_email():
    ss.em_check = mailer.ready(cfg, force=True)


def _test_email():
    to = (ss.get("em_test_to") or "").strip()
    try:
        res = mailer.send_html(cfg, to, "8D Intelligence test email",
                               '<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#1B1816">'
                               '<p style="margin:0 0 8px"><b>Email delivery works.</b></p><p style="margin:0;color:#57524D">Sent from 8D '
                               f'Intelligence with {mailer.T.TRANSPORTS[mailer.transport(cfg)]}.</p></div>', reason="test")
        ss.em_test = {"ok": True, "msg": f"Test email sent to {', '.join(res['to'])}"}
    except Exception as e:
        ss.em_test = {"ok": False, "msg": str(e)}


def _edit_list(name: str | None):
    rec = next((x for x in mailer.lists() if x["name"] == name), None) if name else None
    ss.rl_edit = name or ""
    ss.rl_name = rec["name"] if rec else ""
    ss.rl_emails = list(rec["emails"]) if rec else []
    ss.rl_report = bool(rec and rec.get("on_report"))
    ss.rl_down = bool(rec and rec.get("on_machine_down"))
    ss.rl_digest = bool(rec and rec.get("daily_digest"))
    ss.rl_default = bool(rec and rec.get("is_default"))
    ss.pop("rl_error", None)


def _save_list():
    try:
        mailer.save_list(ss.get("rl_name"), ss.get("rl_emails") or [], ss.get("rl_report"), ss.get("rl_down"),
                         ss.get("rl_default"), old_name=ss.get("rl_edit") or None, daily_digest=ss.get("rl_digest"))
        shell.notify(f"Recipient list '{ss.rl_name}' saved", icon=":material/check_circle:")
        ss.pop("rl_edit", None)
        ss.pop("rl_error", None)
    except Exception as e:
        ss.rl_error = str(e)


def _delete_list(name: str):
    mailer.delete_list(name)
    ss.pop("rl_edit", None)
    shell.notify(f"Recipient list '{name}' deleted", icon=":material/delete:")


def _digest_send():
    try:
        res = digest_ops.send_now(shell.monitor(), cfg)
        ss.dg_msg = {"ok": not res["failed"], "msg": (f"Digest sent to {', '.join(res['sent'])}" if res["sent"] else "Not sent")
                     + ("".join(f" · {k}: {v}" for k, v in res["failed"].items()))}
    except Exception as e:
        ss.dg_msg = {"ok": False, "msg": str(e)}


def _digest_hour():
    try:
        t = digest_ops.set_hour(cfg, int(ss.dg_hour), ss.get("dg_tz"))
        cfg["digest"]["hour"] = int(ss.dg_hour)
        persist(f"Daily digest moved to {int(ss.dg_hour):02d}:00 ({t.get('tz')})")
        ss.dg_msg = None
    except Exception as e:
        ss.dg_msg = {"ok": False, "msg": f"Could not change the schedule: {str(e).splitlines()[0][:200]}"}


def _zones(current: str | None) -> list[str]:
    import zoneinfo
    return sorted(zoneinfo.available_timezones() | {"UTC"} | ({current} if current else set()))


def digest_card():
    t = digest_ops.task(cfg)
    targets = digest_ops.lists()
    state = (t.get("state") or "").lower()
    badge = (K.pill("healthy", "Scheduled", "xs") if state == "started" else K.pill("warning", "Paused", "xs") if t.get("exists")
             else K.pill("pending", "Not set up", "xs"))
    st.html(K.card_title("Daily digest", "Overdue actions, parts to order, purchase requests and customer orders at risk, every morning",
                         "mail", badge))
    if t.get("exists"):
        st.caption(f"Snowflake task {S.names()['digest_task']} sends it with Snowflake email at "
                   f"{(t.get('hour') or 0):02d}:{t.get('minute', 0):02d} ({t.get('tz')}), also when nobody has the app open.")
        ss.setdefault("dg_hour", t.get("hour") if t.get("hour") is not None else cfg["digest"]["hour"])
        ss.setdefault("dg_tz", t.get("tz") or "UTC")
        c1, c2, c3 = st.columns([2, 3, 1.4], vertical_alignment="bottom")
        c1.selectbox("Send at", list(range(24)), key="dg_hour", format_func=lambda h: f"{h:02d}:00")
        c2.selectbox("Time zone", _zones(t.get("tz")), key="dg_tz")
        c3.button("Apply", key="dg_apply", icon=":material/schedule:", on_click=_digest_hour, width="stretch")
    else:
        st.caption("The scheduled send needs the EIGHT_D_DAILY_DIGEST task: `python sql/setup.py --steps business`. "
                   + (t.get("error") or ""))
    st.html(f'<div class="sx-muted" style="font-size:12.5px">Lists: <b>{K.esc(", ".join(x["name"] for x in targets) or "none yet")}</b>'
            '<span class="sx-subtle"> (tick “Daily digest” on a recipient list)</span></div>')
    with st.container(horizontal=True):
        st.button("Send now", key="dg_send", icon=":material/send:", on_click=_digest_send, disabled=not targets,
                  help=f"Sends today's digest with {mailer.T.TRANSPORTS[mailer.transport(cfg)]}")
        st.button("Hide preview" if ss.get("dg_preview") else "Preview", key="dg_prev", icon=":material/visibility:",
                  type="tertiary", on_click=lambda: ss.update(dg_preview=not ss.get("dg_preview")))
    if ss.get("dg_preview"):
        try:
            d = digest_ops.build(shell.monitor(), cfg)
            st.caption(d["subject"])
            st.html(d["html"])
        except Exception as e:
            st.error(f"Could not build the digest: {str(e)[:200]}", icon=":material/error:")
    msg = ss.get("dg_msg")
    if msg:
        (st.success if msg["ok"] else st.error)(msg["msg"], icon=":material/mail:")


with tabs["email"]:
    em = cfg["email"]
    left, right = st.columns([7, 5])
    with left, K.card("email"):
        chk = ss.get("em_check") or mailer.ready(cfg)
        st.html(K.card_title("Delivery", "How reports and machine-down alerts are sent", "mail",
                             K.pill("healthy", "Ready", "xs") if chk["ok"] else K.pill("warning", "Setup needed", "xs")))
        ss.setdefault("em_transport", em.get("transport", "snowflake"))
        st.segmented_control("Send with", list(mailer.T.TRANSPORTS), key="em_transport", format_func=mailer.T.TRANSPORTS.get)
        t = ss.get("em_transport") or "snowflake"
        ss.setdefault("em_integ", em["integration"])
        ss.setdefault("em_stage", em["stage"])
        ss.setdefault("em_days", int(em["link_days"]))
        ss.setdefault("em_docx", bool(em.get("attach_docx")))
        sm, sg = em["smtp"], em["sendgrid"]
        for k, v in (("smtp_host", sm["host"]), ("smtp_port", int(sm["port"] or 587)), ("smtp_sec", sm["security"]),
                     ("smtp_user", sm["username"]), ("smtp_from", sm["from_addr"]), ("smtp_name", sm["from_name"]),
                     ("sg_from", sg["from_addr"]), ("sg_name", sg["from_name"])):
            ss.setdefault(k, v)
        if t == "snowflake":
            st.caption("Reaches users of this Snowflake account with a verified email. The PDF is shared as a secure link.")
            c1, c2 = st.columns(2)
            c1.text_input("Notification integration", key="em_integ", help="An EMAIL notification integration this role can use")
            c2.text_input("PDF stage", key="em_stage", help="DATABASE.SCHEMA.STAGE with SNOWFLAKE_SSE encryption; created on first send")
            st.slider("Download link valid for (days)", 1, mailer.MAX_LINK_DAYS, key="em_days",
                      help="Pre-signed URLs on internal stages last at most 7 days")
        elif t == "smtp":
            st.caption("Reaches any address with the PDF attached: Office 365, Gmail (app password), Amazon SES, Mailgun or a company relay.")
            c1, c2, c3 = st.columns([3, 1, 2])
            c1.text_input("SMTP host", key="smtp_host", placeholder="smtp.office365.com")
            c2.number_input("Port", 1, 65535, key="smtp_port")
            c3.selectbox("Security", ["starttls", "ssl", "none"], key="smtp_sec",
                         format_func={"starttls": "STARTTLS (587)", "ssl": "SSL/TLS (465)", "none": "None (25)"}.get)
            c1, c2 = st.columns(2)
            c1.text_input("Username", key="smtp_user")
            src = mailer.T.credentials(cfg, "smtp")
            hint = "Using the mail_smtp app secret" if src.get("source") == "app secret" else (
                f"Saved {S.secret_hint(sm.get('password', ''))} · leave blank to keep" if sm.get("password") else "Password or app password")
            c2.text_input("Password", type="password", key="smtp_pass", placeholder=hint, autocomplete="off")
            c1.text_input("From address", key="smtp_from", placeholder="quality@yourcompany.com")
            c2.text_input("From name", key="smtp_name")
        else:
            st.caption("Reaches any address with the PDF attached, through the SendGrid HTTPS API.")
            src = mailer.T.credentials(cfg, "sendgrid")
            hint = "Using the mail_sendgrid_key app secret" if src.get("source") == "app secret" else (
                f"Saved {S.secret_hint(sg.get('api_key', ''))} · leave blank to keep" if sg.get("api_key") else "SG.xxxxx")
            st.text_input("API key", type="password", key="sg_key", placeholder=hint, autocomplete="off")
            c1, c2 = st.columns(2)
            c1.text_input("From address (verified sender)", key="sg_from")
            c2.text_input("From name", key="sg_name")
        if t != "snowflake":
            st.toggle("Also attach a Word copy", key="em_docx")
        with st.container(horizontal=True):
            st.button("Save & check", type="primary", icon=":material/save:", key="em_save", on_click=_save_email)
            st.button("Check setup", icon=":material/bolt:", key="em_chk", on_click=_check_email)
        if chk.get("transport") == "snowflake" and t == "snowflake":
            rows = [("Notification integration", chk["integration"], chk.get("integration_name") or em["integration"]),
                    ("PDF stage", chk["stage"], chk.get("stage_name") or em["stage"])]
            st.html('<div class="sx-inset" style="padding:10px 12px;display:grid;gap:7px;font-size:12.5px;margin-top:6px">' + "".join(
                f'<div class="sx-row" style="align-items:flex-start">{K.pill("healthy" if ok else "pending", "Found" if ok else "Missing", "xs")}'
                f'<div style="min-width:0"><div class="sx-muted">{K.esc(k)}</div><div class="sx-mono sx-subtle" '
                f'style="font-size:11.5px;overflow-wrap:anywhere">{K.esc(v)}</div></div></div>'
                for k, ok, v in rows) + "</div>")
        if chk.get("transport") != t:
            st.caption("Save to switch the transport.")
        for i in chk["issues"]:
            st.caption(i)
        c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
        c1.text_input("Send a test email to", key="em_test_to", placeholder="you@company.com")
        c2.button("Send test", icon=":material/send:", key="em_test_btn", on_click=_test_email, width="stretch",
                  disabled=not chk["ok"])
        if ss.get("em_test"):
            (st.success if ss.em_test["ok"] else st.error)(ss.em_test["msg"], icon=":material/mail:")

    with left, K.card("rlists"):
        ls = mailer.lists()
        st.html(K.card_title("Recipient lists", "Saved distribution lists. The default list is used for one-click sending.", "users",
                             f'<span class="sx-pill off xs tabular">{len(ls)}</span>'))
        for r in ls:
            tags = "".join(t for t in (K.pill("healthy", "Default", "xs") if r.get("is_default") else "",
                                       K.pill("pending", "Every report", "xs") if r.get("on_report") else "",
                                       K.pill("critical", "Machine down", "xs") if r.get("on_machine_down") else "",
                                       K.pill("queued", "Daily digest", "xs") if r.get("daily_digest") else ""))
            c1, c2 = st.columns([5, 2], vertical_alignment="center")
            c1.html(f'<div class="sx-row" style="gap:6px;flex-wrap:wrap"><b style="font-size:13px">{K.esc(r["name"])}</b>{tags}</div>'
                    f'<div class="sx-muted" style="font-size:12px;overflow-wrap:anywhere">{K.esc(", ".join(r["emails"]))}</div>')
            with c2.container(horizontal=True, horizontal_alignment="right"):
                st.button("Edit", key=f"rl_e_{r['name']}", type="tertiary", icon=":material/edit:", on_click=_edit_list, args=(r["name"],))
                st.button("Delete", key=f"rl_d_{r['name']}", type="tertiary", icon=":material/delete:", on_click=_delete_list, args=(r["name"],))
        if "rl_edit" not in ss:
            st.button("New list", icon=":material/add:", key="rl_new", on_click=_edit_list, args=(None,))
        else:
            with st.form("rl_form", border=True):
                st.text_input("List name", key="rl_name", placeholder="Quality leads")
                known = sorted({e for r in ls for e in r["emails"]} | set(ss.get("rl_emails") or []))
                st.multiselect("Email addresses", known, key="rl_emails", accept_new_options=True,
                               placeholder="Type an address and press Enter",
                               help="Any address works with SMTP or SendGrid. With Snowflake email, recipients must be verified users of this account.")
                c1, c2 = st.columns(2)
                c1.checkbox("Default list", key="rl_default")
                c2.checkbox("Send every finished report", key="rl_report")
                c1.checkbox("Alert when a machine goes down", key="rl_down")
                c2.checkbox("Daily digest", key="rl_digest", help="Overdue actions, parts to order, purchase requests and orders at risk, every morning")
                with st.container(horizontal=True):
                    st.form_submit_button("Save list", type="primary", icon=":material/save:", on_click=_save_list)
                    st.form_submit_button("Cancel", on_click=lambda: ss.pop("rl_edit", None))
            if ss.get("rl_error"):
                st.error(ss.rl_error, icon=":material/error:")

    with right, K.card("digest"):
        digest_card()
    with right, K.card("emhow"):
        st.html(K.card_title("Choosing a transport", None, "info") +
                '<div class="sx-muted" style="font-size:12.5px;display:grid;gap:10px">'
                '<div><b style="color:#1B1816">Snowflake email</b> needs no setup outside Snowflake. It reaches only users of this account '
                'who have verified their email, sends from no-reply@snowflake.net and cannot attach files, so the PDF is a secure link. '
                'Setup: <span class="sx-mono">sql/06_report_email.sql</span>.</div>'
                '<div><b style="color:#1B1816">SMTP or SendGrid</b> reach anyone, customers and suppliers included, from your own address, '
                'with the PDF attached. Inside Snowflake the app needs an external access integration for the mail host and a secret '
                'holding the credentials: <span class="sx-mono">sql/08_external_email.sql</span>, then redeploy. Locally, the values above are used.</div>'
                '<div>Lists marked <b style="color:#1B1816">Send every finished report</b> receive each report automatically; lists marked '
                '<b style="color:#1B1816">Alert when a machine goes down</b> get the work order as soon as a machine stops '
                '(Settings &rsaquo; Automation).</div></div>')
    with right, K.card("emlog"):
        rows = mailer.log(limit=15)
        st.html(K.card_title("Recent emails", f"{len(rows)} most recent sends", "list"))
        if rows:
            st.dataframe(pd.DataFrame([{"Sent": K.ts(r.get("sent_at")), "To": ", ".join(r.get("recipients") or []),
                                        "What": r.get("reason") or "", "Status": r.get("status"), "Via": r.get("transport")}
                                       for r in rows]), hide_index=True, width="stretch", height=min(38 + 35 * len(rows), 300))
        else:
            st.caption("Nothing sent yet.")

# -------------------------------------------------------------------- automation
def _save_automation():
    a = cfg["automation"]
    a.update(auto_plan=bool(ss.au_plan), auto_8d_on_trip=bool(ss.au_8d), notify_on_trip=bool(ss.au_notify),
             auto_complete_repair=bool(ss.au_complete), near_failure_machine=bool(ss.au_near),
             trip_after_s=int(ss.au_trip), plan_hours=float(ss.au_plan_h), urgent_hours=float(ss.au_urgent_h),
             critical_hours=float(ss.au_crit_h))
    mon = shell.monitor()
    if mon.active:
        mon.context.setdefault("automation", {}).update(a)  # actions apply now; limits and tiers at the next start
    persist("Automation settings saved")


def _reset_plant():
    if shell.monitor().reset_conditions():
        shell.notify("Plant conditions reset. The next monitoring session assigns new machine conditions.", icon=":material/restart_alt:")
    else:
        shell.notify("Stop monitoring first.", icon=":material/error:")


with tabs["automation"]:
    a = cfg["automation"]
    for k, v in (("au_plan", a["auto_plan"]), ("au_8d", a["auto_8d_on_trip"]), ("au_notify", a["notify_on_trip"]),
                 ("au_complete", a["auto_complete_repair"]), ("au_near", a["near_failure_machine"]), ("au_trip", int(a["trip_after_s"])),
                 ("au_plan_h", float(a["plan_hours"])), ("au_urgent_h", float(a["urgent_hours"])), ("au_crit_h", float(a["critical_hours"]))):
        ss.setdefault(k, v)
    left, right = st.columns([7, 5])
    with left, K.card("auto"):
        st.html(K.card_title("What happens automatically", "Actions the app takes on its own while monitoring runs", "activity"))
        down_lists = [x["name"] for x in mailer.lists() if x.get("on_machine_down")]
        report_lists = [x["name"] for x in mailer.lists() if x.get("on_report")]
        st.toggle("Raise a planned work order when a machine is forecast to fail", key="au_plan",
                  help="When time to failure drops below the planning horizon, a work order is created with parts, technician and cost.")
        st.toggle("Start an 8D investigation when a machine stops", key="au_8d",
                  help="Runs now if no investigation is running, otherwise queued. Linked to the emergency work order.")
        st.toggle("Email the machine-down alert", key="au_notify",
                  help="Sent to recipient lists marked 'Alert when a machine goes down'.")
        st.caption(f"Alert lists: {', '.join(down_lists) or 'none yet (Settings › Email & recipients)'} · "
                   f"report lists: {', '.join(report_lists) or 'none'}")
        st.toggle("Complete repairs automatically after the estimated time", key="au_complete",
                  help="For unattended demos: an in-repair job completes when its estimated duration has passed.")
        st.toggle("Include a machine close to failure in each session", key="au_near",
                  help="If no machine is within a few hours of failure, one healthy machine develops a fast fault.")
        st.html('<div class="sx-label" style="margin-top:8px">Limits</div>')
        c1, c2, c3, c4 = st.columns(4)
        c1.number_input("Stop after (s) above critical", 3, 300, key="au_trip",
                        help="Protective stop: how long a signal may stay above its critical limit before the machine stops.")
        c2.number_input("Plan horizon (h)", 6.0, 720.0, step=6.0, key="au_plan_h", help="Plan maintenance below this time to failure.")
        c3.number_input("Urgent (h)", 1.0, 168.0, step=1.0, key="au_urgent_h", help="Schedule urgently below this time to failure.")
        c4.number_input("Critical (h)", 0.5, 48.0, step=0.5, key="au_crit_h", help="Machine shows as critical below this time to failure.")
        st.button("Save", type="primary", icon=":material/save:", key="au_save", on_click=_save_automation)
        st.caption("Actions apply to a running session at once; limits and horizons apply from the next monitoring start.")
    with right, K.card("opsstore"):
        stt = ops_store.store().status()
        where = f"Snowflake · {stt['schema']}" if stt["kind"] == "snowflake" else "Local files (snowflake_app/.ops)"
        st.html(K.card_title("Plant state storage", "Machine state, work orders, alerts, lists and the email log", "database",
                             K.pill("healthy" if not (stt["error"] or stt["write_error"]) else "warning",
                                    "Writing" if not stt["write_error"] else "Write failed", "xs")))
        st.html('<div class="sx-inset" style="padding:10px 12px;display:grid;gap:6px;font-size:12.5px">'
                f'<div class="sx-row"><span class="sx-subtle" style="width:110px">Stored in</span><b>{K.esc(where)}</b></div>'
                f'<div class="sx-row"><span class="sx-subtle" style="width:110px">Last write</span>{K.esc(K.ts(stt["last_write"]))}</div>'
                f'<div class="sx-row"><span class="sx-subtle" style="width:110px">Pending</span>{stt["pending"]}</div></div>')
        if stt["error"]:
            st.caption(f"Snowflake storage unavailable, using local files: {stt['error']}")
        if stt["write_error"]:
            st.caption(f"Last write failed (will retry): {stt['write_error']}")
        st.caption("Writes happen only on changes (a machine changes state, a work order moves) plus a health refresh every 5 minutes, "
                   "so the warehouse can suspend in between.")
    with right, K.card("reset"):
        st.html(K.card_title("Reset plant conditions", "Forget every machine's wear and down state", "wrench"))
        st.caption("Down machines come back, open work orders are cancelled, and the next session assigns a new fleet mix. "
                   "History (closed work orders, events, emails) is kept.")
        ok = st.checkbox("I understand", key="reset_ok", disabled=shell.monitor().active)
        st.button("Reset plant", icon=":material/restart_alt:", key="reset_btn", disabled=not ok or shell.monitor().active,
                  on_click=_reset_plant, help="Stop monitoring first" if shell.monitor().active else None)

# ---------------------------------------------------------------------- business
def _save_business():
    cfg["business"].update(margin_pct=float(ss.bz_margin), labour_usd_h=float(ss.bz_labour), crew=int(ss.bz_crew))
    persist("Business settings saved")


with tabs["business"]:
    bz = cfg["business"]
    for k, v in (("bz_margin", float(bz["margin_pct"])), ("bz_labour", float(bz["labour_usd_h"])), ("bz_crew", int(bz["crew"]))):
        ss.setdefault(k, v)
    left, right = st.columns([6, 6])
    with left, K.card("biz"):
        st.html(K.card_title("Cost of downtime", "Used for the business impact of every stop, work order and forecast", "dollar"))
        c1, c2, c3 = st.columns(3)
        c1.number_input("Contribution margin (%)", 0.0, 95.0, step=1.0, key="bz_margin",
                        help="Share of the selling price that is lost when a machine does not produce")
        c2.number_input("Labour rate ($/h)", 0.0, 500.0, step=5.0, key="bz_labour", help="Cost of an idle operator per hour")
        c3.number_input("Crew per machine", 0, 10, step=1, key="bz_crew", help="Operators standing idle while a machine is down")
        st.button("Save", type="primary", icon=":material/save:", key="bz_save", on_click=_save_business)
        st.caption("Cost per hour down = units per hour × average order price × margin + crew × labour rate. A stop also costs its "
                   "repair and the late penalties of customer orders it pushes past their due date.")
    with right, K.card("bizhow"):
        st.html(K.card_title("Where the numbers come from", None, "info") +
                '<div class="sx-muted" style="font-size:12.5px;display:grid;gap:8px">'
                '<div><b style="color:#1B1816">Order book</b>: PRODUCTION.ORDER_BOOK, about four weeks of open customer orders per machine, '
                "rolled forward every night by the ROLL_ORDER_BOOK task from each machine's order history and the product prices.</div>"
                '<div><b style="color:#1B1816">Capacity</b>: units per hour each machine made in PRODUCTION_ORDERS (machine type average '
                'for machines without history), over three shifts.</div>'
                '<div><b style="color:#1B1816">Lateness</b>: open orders run earliest due first at that rate; a stop is a window with no output. '
                'An order is late when it finishes after the end of its due date; penalties are 1–5 % of its value per day by priority.</div>'
                '<div><b style="color:#1B1816">OEE</b>: availability (maintenance downtime), performance (units made vs. planned) and '
                'quality (inspection pass rate) from the history tables.</div></div>')

# ---------------------------------------------------------------------- pipeline
with tabs["pipeline"]:
    left, right = st.columns([5, 7])
    with left, K.card("pipe"):
        st.html(K.card_title("Generation", "Applied to every agent call", "cpu"))
        with st.form("pipe_form", border=False):
            temp = st.slider("Temperature", 0.0, 1.0, float(cfg["pipeline"]["temperature"]), 0.05, help="Lower = more deterministic, more grounded output")
            mt = st.number_input("Max tokens per agent", 200, 4096, int(cfg["pipeline"]["max_tokens"]), 50)
            if st.form_submit_button("Save", type="primary", icon=":material/save:"):
                cfg["pipeline"].update(temperature=round(temp, 2), max_tokens=int(mt))
                persist("Pipeline settings saved")
    with right, K.card("agents"):
        st.html(K.card_title("Agents", "11 agents; the conditional edge after PdM decides whether D0–D8 run", "branch"))
        st.dataframe(pd.DataFrame([{"Step": s["code"], "Agent": s["title"], "Role": AGENTS[s["id"]][0].split(": ", 1)[-1],
                                    "Sections": len(AGENTS[s["id"]][1])} for s in STEPS]), hide_index=True)

# -------------------------------------------------------------------- appearance
with tabs["appearance"]:
    with K.card("appear"):
        st.html(K.card_title("Appearance & accessibility", "Signal Orange light theme. Status is always shown with icon, text and shape, never colour alone.", "activity"))
        with st.form("disp_form", border=False):
            c1, c2 = st.columns(2)
            refresh = c1.segmented_control("Live refresh interval", [0.5, 1.0, 2.0], default=float(cfg["display"]["refresh_s"]),
                                           format_func=lambda v: f"{v:g} s", help="How often live views redraw. Slower reduces motion and load.")
            follow = c2.toggle("Follow the highest-risk machine by default", value=bool(cfg["display"]["follow_risk"]))
            eng = st.toggle("Engineer view", value=bool(cfg["display"].get("engineer_view")),
                            help="Show the statistics behind the plain-language labels: anomaly score in σ, PSI, fit R², "
                                 "p-values and the technical detail of every event.")
            st.caption("Animations respect your operating system's reduce-motion setting.")
            if st.form_submit_button("Save", type="primary", icon=":material/save:"):
                cfg["display"].update(refresh_s=float(refresh or 1.0), follow_risk=bool(follow), engineer_view=bool(eng))
                ss.follow_risk = bool(follow)
                ss.pop("w_follow_risk", None)
                persist("Display settings saved")
