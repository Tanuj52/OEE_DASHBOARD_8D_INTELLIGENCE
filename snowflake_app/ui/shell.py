"""
App shell: per-session services, grouped sidebar navigation, live top bar.
"""

import copy

import streamlit as st

from snowflake_app.services import automation, capa_ops, datasource, ops_store, parts_ops
from snowflake_app.services import settings as S
from snowflake_app.services.live import LiveMonitor
from snowflake_app.services.llm import SnowflakePool, bound_complete
from snowflake_app.services.runs import RunManager
from snowflake_app.ui import kit as K

ROUTES = [
    {"path": "app_pages/command_center.py", "title": "Command Center", "icon": ":material/dashboard:", "group": "Operate",
     "description": "Plant-wide quality, risk and cost at a glance", "default": True},
    {"path": "app_pages/live_monitoring.py", "title": "Live Monitoring", "icon": ":material/monitor_heart:", "group": "Operate",
     "description": "Streaming telemetry, early-warning model and time to failure"},
    {"path": "app_pages/maintenance.py", "title": "Maintenance", "icon": ":material/build:", "group": "Operate",
     "description": "Work orders from early warnings and protective stops, repair and return to service"},
    {"path": "app_pages/spare_parts.py", "title": "Spare parts", "icon": ":material/inventory_2:", "group": "Operate",
     "description": "Stock, parts held for work orders, what to order and purchase requests"},
    {"path": "app_pages/business_impact.py", "title": "Business impact", "icon": ":material/payments:", "group": "Operate",
     "description": "Customer orders at risk, the cost of downtime, the best maintenance window and OEE"},
    {"path": "app_pages/investigations.py", "title": "8D Investigations", "icon": ":material/account_tree:", "group": "Investigate",
     "description": "Multi-agent root-cause pipeline powered by Snowflake AI_COMPLETE"},
    {"path": "app_pages/corrective_actions.py", "title": "Corrective actions", "icon": ":material/task:", "group": "Investigate",
     "description": "Every action from the 8D reports: owners, due dates, progress and whether the fix worked"},
    {"path": "app_pages/traceability.py", "title": "Lot Traceability", "icon": ":material/lan:", "group": "Investigate",
     "description": "Trace defects back to suppliers and forward to customers"},
    {"path": "app_pages/quality.py", "title": "Quality & SPC", "icon": ":material/query_stats:", "group": "Investigate",
     "description": "Process capability, control charts and cost of poor quality"},
    {"path": "app_pages/reports.py", "title": "Reports", "icon": ":material/description:", "group": "Output",
     "description": "Every 8D report from the agent pipeline: search, open, download and share"},
    {"path": "app_pages/report_view.py", "title": "8D Report", "icon": ":material/article:", "group": "Output",
     "description": "Full 8D report with PDF, Word and email delivery", "hidden": True, "parent": "app_pages/reports.py"},
    {"path": "app_pages/settings.py", "title": "Settings", "icon": ":material/settings:", "group": "System",
     "description": "Snowflake connection, report email and preferences"},
]
GROUPS = ["Operate", "Investigate", "Output", "System"]
ROUTE = {r["path"]: r for r in ROUTES}


@st.cache_resource
def run_manager() -> RunManager:
    rm = RunManager()
    rm.on_complete = automation.on_report_complete
    return rm


@st.cache_resource
def _live_monitor() -> LiveMonitor:
    """One stream per server process (like the FastAPI backend), so a browser refresh does not lose it."""
    mon = LiveMonitor()
    rm = run_manager()
    mon.hooks["trip"] = lambda m, wo, ev, ctx: automation.on_trip(rm, m, wo, ev, ctx)
    return mon


def init_session():
    ss = st.session_state
    if S.IN_SNOWFLAKE and SnowflakePool.embedded is None:
        try:
            SnowflakePool.embedded = st.connection("snowflake").raw_connection
        except Exception:
            pass
    _bind_database()
    if "settings" not in ss:
        ss.settings = S.load()
    ss.setdefault("selected_run", None)
    ss.setdefault("trace_defect", None)
    ss.setdefault("focus_machine", None)
    apply_data_source()


@st.cache_resource
def _deployed_database() -> str | None:
    """Inside Snowflake the app's objects live in the database the Streamlit app is deployed in."""
    try:
        return SnowflakePool.query({"mode": "embedded"}, "SELECT CURRENT_DATABASE()")[0][0]
    except Exception:
        return None


def _bind_database():
    if S.IN_SNOWFLAKE and SnowflakePool.embedded is not None:
        S.set_database(_deployed_database())
    elif S.hosted():
        S.set_database(S.hosted().get("database"))


@st.cache_resource
def _plant_source() -> dict:
    """Where the plant data and operations state live: one choice for the whole server process.
    The data layer and the ops store are shared by every viewer, so a per-session choice would
    flip them back and forth between viewers."""
    return {}


def set_plant_source(data: dict, ops: dict | None = None):
    """Called by Settings when an operator changes the data source; every session follows on its next rerun."""
    ps = _plant_source()
    ps["data"] = copy.deepcopy(data)
    if ops is not None:
        ps["ops"] = copy.deepcopy(ops)


def apply_data_source() -> dict:
    """Point the shared data layer at the configured source; cached page data is dropped when it changes."""
    ss = st.session_state
    ps = _plant_source()
    ps.setdefault("data", copy.deepcopy(ss.settings["data"]))
    ps.setdefault("ops", copy.deepcopy(ss.settings["ops"]))
    ss.settings["data"], ss.settings["ops"] = copy.deepcopy(ps["data"]), copy.deepcopy(ps["ops"])
    res = datasource.apply(ss.settings)
    ss.data_source = res
    if res["changed"]:
        st.cache_data.clear()
    store = ops_store.store()
    ss.ops_store = store.configure(ss.settings)
    if ss.get("_shared_from") != store.config_key:  # plant-wide settings saved by anyone, once per backend
        rec = store.get("app_settings", "plant")
        S.apply_shared(ss.settings, (rec or {}).get("values"))
        ss._shared_from = store.config_key
    if not res.get("error"):
        _bootstrap(store.config_key)
    return res


_booted: set = set()


def _bootstrap(key):
    """Once per operations backend: track the actions of reports written before this version, give open work
    orders their part reservations, and run the effectiveness check (idempotent; safe on every instance)."""
    if key in _booted:
        return
    _booted.add(key)
    try:
        run_manager().import_legacy()  # reports saved as files by earlier versions, before their actions are read
        capa_ops.backfill()
        wos = monitor().work_orders()
        parts_ops.ensure_reservations([w for w in wos if w["status"] in ("planned", "awaiting", "in_repair")])
        capa_ops.check_effectiveness(wos)
    except Exception:
        _booted.discard(key)  # plant tables not readable yet; try again on the next run


def current_user() -> str:
    """Who is acting, for audit trails: the Snowflake user inside Snowflake, the OS user locally."""
    try:
        u = st.user.get("user_name") or st.user.get("email")
        if u:
            return str(u)
    except Exception:
        pass
    if S.hosted():  # public deployment: visitors are anonymous
        return str(S.hosted().get("app_user") or "Plant user")
    import getpass
    try:
        return getpass.getuser()
    except Exception:
        return "Plant user"


def data_unavailable() -> str | None:
    """The reason the Snowflake tables cannot be read, if they cannot (pages need them)."""
    ds = st.session_state.get("data_source") or {}
    return ds.get("error") if ds.get("kind") != "snowflake" and ds.get("wanted") == "snowflake" else None


def _retry_data():
    datasource.retry()
    st.cache_data.clear()


def data_error_page(err: str):
    st.html(f'<header class="sx-topbar"><div class="t"><h1>Plant data unavailable</h1>'
            f'<p>The app reads every table from Snowflake and cannot reach them right now.</p></div></header>')
    with K.card("dataerr"):
        ds = st.session_state.get("data_source") or {}
        st.error(f"Could not read **{ds.get('schema')}**: {err}", icon=":material/error:")
        st.markdown("Check that the tables exist (`python sql/setup.py --connection <name>`), that this role can "
                    "read them, and that a warehouse is available. Settings lets you pick another connection or schema.")
        with st.container(horizontal=True):
            st.button("Retry", icon=":material/refresh:", type="primary", on_click=_retry_data)
            st.page_link("app_pages/settings.py", label="Open Settings", icon=":material/settings:")


def monitor() -> LiveMonitor:
    return _live_monitor()


def settings() -> dict:
    return st.session_state.settings


def active_provider() -> dict:
    return S.describe_active(settings())


def save_settings() -> bool:
    """Save locally (outside Snowflake) and share the plant-wide part through the operations store."""
    cfg = settings()
    S.save(cfg)
    store = ops_store.store()
    try:
        user = st.user.get("user_name") or st.user.get("email")
    except Exception:
        user = None
    store.put("app_settings", {"name": "plant", "values": S.shared_part(cfg), "updated_at": ops_store.now_iso(),
                               "updated_by": user})
    return store.sink is not None and (store.sink.kind == "snowflake" or not S.IN_SNOWFLAKE)


def start_investigation(source: str, machine_id: str | None = None):
    """Launch the 8D pipeline in the background. Returns (run, error)."""
    snap = S.snapshot(settings())
    provider = S.describe_active(snap)
    trigger = {"source": source, "machine_id": machine_id}
    try:
        if source == "live":
            trigger = monitor().escalation_trigger(machine_id)
        run = run_manager().start(trigger, bound_complete(snap), provider, context={"settings": snap, "reason": "manual"})
    except (RuntimeError, KeyError) as e:
        return None, str(e)
    st.session_state.selected_run = run.id
    return run, None


# Buttons inside auto-refreshing fragments must act through on_click callbacks: a click's
# one-shot trigger can be consumed by another fragment's timed rerun before the owning code
# reads it. Callbacks always run, but may not navigate, so navigation is deferred via goto()
# and executed by follow_nav() at the top of the app script and of every fragment.

def goto(path: str, params: dict | None = None):
    st.session_state.nav_to = path
    st.session_state.nav_params = params


def notify(message: str, icon: str | None = None):
    """Toast from a callback. Elements created inside a callback during a fragment rerun are written
    at the top of the app (Streamlit warns about this) and displaced the app stylesheet; queue the
    toast instead and show it from the next script or fragment body (flush_notices)."""
    st.session_state.setdefault("_notices", []).append((message, icon))


def flush_notices():
    for message, icon in st.session_state.pop("_notices", None) or []:
        st.toast(message, icon=icon)


def follow_nav():
    flush_notices()
    path = st.session_state.pop("nav_to", None)
    params = st.session_state.pop("nav_params", None)
    if path:
        st.switch_page(path, query_params=params)


def open_report(run_id: str):
    """on_click callback: open one report on its own page (deep-linkable as ?id=...)."""
    st.session_state.report_open = run_id
    goto("app_pages/report_view.py", {"id": run_id})


def synced(widget, label, *args, state: str, after=None, **kw):
    """Widget bound to a plain session key. Widget-owned keys can be dropped between fragment
    reruns, so the app state lives in `state` and the widget is re-seeded from it each run."""
    ss = st.session_state
    wkey = f"w_{state}"
    if ss.get(wkey) is None:
        ss[wkey] = ss[state]

    def _sync():
        v = ss.get(wkey)
        if v is None:  # segmented control deselected: keep the current choice
            ss[wkey] = ss[state]
            return
        ss[state] = v
        if after:
            after()

    return widget(label, *args, key=wkey, on_change=_sync, **kw)


def launch_investigation(source: str, machine_id: str | None = None):
    """on_click callback: start a run and open the Investigations page."""
    _, err = start_investigation(source, machine_id)
    if err:
        notify(err, icon=":material/error:")
    else:
        goto("app_pages/investigations.py")


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

LOGO = """
<div style="display:flex;align-items:center;gap:10px;padding:14px 4px 6px">
  <div style="width:34px;height:34px;border-radius:9px;background:#F26B1D;color:#FFFFFF;display:flex;align-items:center;justify-content:center;
     font-weight:800;font-size:13px;letter-spacing:-.02em;box-shadow:0 0 0 1px rgba(242,107,29,.55),0 0 22px -6px rgba(242,107,29,.55)">8D</div>
  <div style="line-height:1.2;min-width:0">
    <div style="font-size:14px;font-weight:600;color:#1B1816">8D Intelligence</div>
    <div style="font-size:11px;color:#78716B">Manufacturing Quality Command</div>
  </div>
</div>
"""


def sidebar(pages: dict, current):
    s = monitor().summary()
    rm = run_manager()
    running = rm.active()
    with st.sidebar:
        st.html(LOGO)
        for g in GROUPS:
            st.html(f'<div class="sx-label" style="padding:0 10px;margin:12px 0 2px">{g}</div>')
            for r in ROUTES:
                if r["group"] != g or r.get("hidden"):
                    continue
                label = r["title"]
                if r["path"] == "app_pages/live_monitoring.py" and s.get("active"):
                    label += "  :orange-badge[Paused]" if s["paused"] else "  :orange-badge[Live]"
                if r["path"] == "app_pages/investigations.py" and running:
                    label += "  :orange-badge[Running]"
                if r["path"] == "app_pages/maintenance.py":
                    n_open = sum(1 for w in ops_store.store().all("work_orders") if w.get("status") in ("planned", "awaiting", "in_repair", "commissioning"))
                    if n_open:
                        label += f"  :orange-badge[{n_open}]"
                if r["path"] == "app_pages/corrective_actions.py":
                    n = _overdue_actions()
                    if n:
                        label += f"  :orange-badge[{n} overdue]"
                if r["path"] == "app_pages/spare_parts.py":
                    n = parts_to_order()
                    if n:
                        label += f"  :orange-badge[{n} to order]"
                slug = r["path"].split("/")[-1][:-3]
                on = pages[r["path"]] is current or any(pages[c["path"]] is current for c in ROUTES if c.get("parent") == r["path"])
                with st.container(key=f"{'navon' if on else 'navoff'}_{slug}"):
                    st.page_link(pages[r["path"]], label=label, icon=r["icon"])
        st.space("medium")
        st.fragment(_sidebar_status, run_every=ticking())()


def _overdue_actions() -> int:
    import time

    from snowflake_app.core import capa as C
    now = time.time()
    return sum(1 for a in ops_store.store().all("capa_actions") if C.is_overdue(a, now))


def parts_to_order() -> int:
    """Parts at or below the reorder point after reservations and open purchase requests (cheap: no forecast)."""
    from snowflake_app.core import parts as P
    try:
        mst = parts_ops.master()
    except Exception:
        return 0
    stk, inc = P.stock(mst, parts_ops.txns()), P.incoming(parts_ops.pos())
    return sum(1 for p in mst if stk[p["part_id"]]["available"] < 0 or
               stk[p["part_id"]]["available"] + (inc.get(p["part_id"]) or {}).get("qty", 0) <= int(p["reorder_point"]))


def ticking(every: float = 2) -> float | None:
    """Refresh interval for the shell fragments: only tick while something is live, so static
    pages have no timed reruns competing with forms and buttons."""
    return every if monitor().active or run_manager().active() else None


def _sidebar_status():
    follow_nav()
    mon = monitor()
    mon.touch()  # heartbeat keeps this session's stream alive while the tab is open
    s = mon.summary()
    prov = active_provider()
    if s.get("active"):
        state = "Paused" if s["paused"] else ("Learning" if s["phase"] == "learning" else "Live")
    else:
        state = "Off"
    live = s.get("active") and not s.get("paused")
    ico = "database" if prov["provider"] == "snowflake" else "cpu"
    ds = st.session_state.get("data_source") or {}
    ds_label, ds_ok = ("Snowflake", True) if ds.get("kind") == "snowflake" else ("Local files", not ds.get("error"))
    st.html(f"""
    <div class="sx-inset" style="padding:10px 11px;font-size:12px;display:grid;gap:8px">
      <div class="sx-row">{K.dot(live) if s.get('active') else K.dot(False)}<span class="sx-muted">Monitoring</span>
        <span style="margin-left:auto;font-weight:600;color:#1B1816">{state}</span></div>
      <div class="sx-row"><span class="sx-subtle">{K.icon('layers', 14)}</span><span class="sx-muted">Plant data</span>
        <span style="margin-left:auto;font-weight:600;color:{'#1B1816' if ds_ok else '#B54111'}" title="{K.esc(ds.get('schema') or '')}">{ds_label}</span></div>
      <div class="sx-row"><span class="sx-subtle">{K.icon(ico, 14)}</span><span class="sx-muted" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{K.esc(prov['label'])}</span>
        <span style="margin-left:auto;font-weight:600;color:{'#1B1816' if prov['configured'] else '#B54111'}">{'Ready' if prov['configured'] else 'Setup'}</span></div>
    </div>""")


# ---------------------------------------------------------------------------
# Top bar
# ---------------------------------------------------------------------------

def topbar(path: str):
    r = ROUTE[path]
    _topbar_live = st.fragment(_topbar, run_every=ticking())
    _topbar_live(r["title"], r["description"])


def _topbar(title: str, description: str):
    follow_nav()
    s = monitor().summary()
    prov = active_provider()
    chips = []
    if s.get("active"):
        crit = len(s["critical"])
        if crit:
            chips.append(f'<span class="sx-chip hot" role="status">{K.icon("alert", 14)}{crit} critical'
                         f'<span class="k">{K.icon("clock", 12)} {K.ts(s["sim_time"])}</span></span>')
        else:
            txt = "Paused" if s["paused"] else (f"Learning {s['training_progress']:.0f}%" if s["phase"] == "learning" else "Live")
            chips.append(f'<span class="sx-chip" role="status">{K.dot(not s["paused"])}<b style="font-weight:600">{txt}</b>'
                         f'<span class="k">{K.icon("clock", 12)} {K.ts(s["sim_time"])}</span></span>')
    ico = "database" if prov["provider"] == "snowflake" else "cpu"
    chips.append(f'<span class="sx-chip" aria-label="AI provider {K.esc(prov["label"])}, {"ready" if prov["configured"] else "needs setup"}">'
                 f'<span class="k">{K.icon(ico, 14)}</span><b style="font-weight:600">{K.esc(prov["label"].replace(" AI_COMPLETE", ""))}</b>'
                 f'<span class="k sx-mono">{K.esc(prov["model"])}</span>'
                 f'<span class="sx-dot {"idle" if prov["configured"] else ""}" style="width:6px;height:6px;{"background:#57524D" if prov["configured"] else ""}"></span></span>')
    st.html(f'<header class="sx-topbar"><div class="t"><h1>{K.esc(title)}</h1><p>{K.esc(description)}</p></div>'
            f'<div class="sx-row" style="gap:8px;flex-wrap:wrap">{"".join(chips)}</div></header>')
