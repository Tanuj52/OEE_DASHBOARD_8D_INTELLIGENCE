import pandas as pd
import streamlit as st

from snowflake_app.core import data as D
from snowflake_app.ui import kit as K, shell

PATH = "app_pages/traceability.py"
shell.topbar(PATH)
ss = st.session_state


@st.cache_data(ttl=900)
def defects() -> pd.DataFrame:
    d = D.defects_enriched()[["defect_id", "defect_type", "severity", "machine_id", "timestamp", "containment_status"]].copy()
    d["date"] = d["timestamp"].dt.strftime("%Y-%m-%d")
    return d.drop(columns="timestamp").reset_index(drop=True)


@st.cache_data(ttl=900)
def trace(defect_id: str) -> dict:
    return D.traceability(defect_id)


df = defects()
left, right = st.columns([4, 9])

with left, K.card("defects"):
    st.html(K.card_title("Defects", f"{len(df)} recorded · select one to trace", "alert"))
    q = st.text_input("Search defects", placeholder="ID, machine, type…", key="tr_q", label_visibility="collapsed",
                      icon=":material/search:")
    view = df
    if q:
        ql = q.lower()
        view = df[df.apply(lambda r: ql in " ".join(map(str, r.values)).lower(), axis=1)].reset_index(drop=True)
    if ss.trace_defect is None and len(df):
        ss.trace_defect = df.iloc[0]["defect_id"]
    ev = st.dataframe(view[["defect_id", "defect_type", "machine_id", "date", "severity"]], hide_index=True, height=640,
                      on_select="rerun", selection_mode="single-row", key=f"tr_list_{q}",
                      column_config={"defect_id": st.column_config.TextColumn("Defect", width="small"), "defect_type": "Type",
                                     "machine_id": "Machine", "date": "Date", "severity": "Severity"})
    if ev and ev.selection.rows:
        ss.trace_defect = view.iloc[ev.selection.rows[0]]["defect_id"]

did = ss.trace_defect
with right:
    if not did:
        st.info("No defects recorded.", icon=":material/info:")
        st.stop()
    t = trace(did)
    if "error" in t:
        st.error(t["error"], icon=":material/error:")
        st.stop()
    bt = t["backward_trace"]
    d = bt["defect"]
    po = bt.get("production_order") or {}
    imp = t["impact_summary"]
    acts = imp.get("containment_actions_needed", {})
    with K.card("head"):
        st.html(f'<div class="sx-row" style="justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px">'
                f'<div><div class="sx-label">Defect under trace</div><div style="font-size:17px;font-weight:600;margin-top:2px">'
                f'<span class="sx-mono">{K.esc(d["defect_id"])}</span> · {K.esc(d["defect_type"])}</div>'
                f'<div class="sx-muted" style="font-size:13px;margin-top:3px">{K.esc(d.get("description"))}</div></div>'
                f'<div class="sx-row">{K.pill(d["severity"])}{K.pill(d.get("containment_status", "Open"))}</div></div>')

    st.html('<div class="sx-kpis">' + "".join([
        K.kpi("Orders to quarantine", K.num(imp["total_orders_affected"]), ico="layers"),
        K.kpi("Shipments affected", K.num(imp["total_shipments_affected"]), hint=f"{len(acts.get('recall_shipments', []))} to recall", ico="truck"),
        K.kpi("Customers to notify", K.num(imp["total_customers_affected"]), ico="users", emphasis=imp["total_customers_affected"] > 0),
        K.kpi("Units at risk", K.num(imp["total_units_at_risk"]), ico="box"),
    ]) + "</div>")

    # ------------------------------------------------------------- trace map
    lots = bt.get("material_lots", [])
    suppliers = {}
    for l in lots:
        s = l.get("supplier") or {}
        suppliers.setdefault(l["supplier_id"], s)
    orders, ships, customers = {}, [], {}
    for f in t.get("forward_traces", []):
        for o in f.get("affected_orders", []):
            orders.setdefault(o["order_id"], o)
        for s in f.get("affected_shipments", []):
            ships.append(s)
            c = s.get("customer") or {}
            cid = s["customer_id"]
            customers.setdefault(cid, {"name": c.get("name", cid), "shipments": 0, "units": 0})
            customers[cid]["shipments"] += 1
            customers[cid]["units"] += int(s.get("qty") or 0)

    def node(top, main, sub, hot=False):
        return (f'<div style="border:1px solid {"#F26B1D" if hot else "#E5E2DE"};background:#fff;border-radius:9px;padding:7px 9px;min-width:0">'
                f'<div class="sx-mono sx-subtle" style="font-size:10.5px">{K.esc(top)}</div>'
                f'<div style="font-size:12.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{K.esc(main)}</div>'
                f'<div class="sx-subtle" style="font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{K.esc(sub)}</div></div>')

    def column(title, ico, items, hot=False, more=0):
        extra = f'<div class="sx-subtle" style="font-size:11.5px;padding:2px 2px 0">+{more} more</div>' if more else ""
        return (f'<div style="border:1px solid {"#F26B1D" if hot else "#E5E2DE"};background:{"#FFF8F3" if hot else "#F5F4F2"};border-radius:12px;padding:10px;min-width:0">'
                f'<div class="sx-row sx-label" style="justify-content:space-between;margin-bottom:8px"><span class="sx-row" style="gap:5px">{K.icon(ico, 13)}{K.esc(title)}</span>'
                f'<span style="background:#ECEAE7;border-radius:5px;padding:0 6px;color:#57524D">{len(items) + more}</span></div>'
                f'<div style="display:grid;gap:6px">{"".join(items)}{extra}</div></div>')

    arrow = f'<div style="display:flex;align-items:center;justify-content:center;color:#F26B1D">{K.icon("arrow", 14)}</div>'
    order_items = [node(o["order_id"], o["product_id"], f'{o["machine_id"]} · {o["qty_produced"]} units') for o in list(orders.values())[:6]]
    cols = [
        column("Suppliers", "truck", [node(sid, s.get("name", sid), ", ".join(filter(None, [s.get("certification_level"), f"rating {s.get('rating')}" if s.get("rating") else None])))
                                      for sid, s in suppliers.items()]),
        column("Material lots", "box", [node(l["lot_id"], l["material"], f'CoA {l["coa_status"]} · {l["spec_grade"]}', l["coa_status"] != "PASS") for l in lots]),
        column("Origin order", "factory", [node(po.get("order_id"), po.get("product_id"), f'{po.get("machine_id")} · {po.get("qty_produced")} units', True)], hot=True),
        column("Affected orders", "layers", order_items, more=max(len(orders) - 6, 0)),
        column("Customers", "users", [node(cid, c["name"], f'{c["shipments"]} shipment{"s" if c["shipments"] != 1 else ""} · {c["units"]:,} units') for cid, c in customers.items()]),
    ]
    grid = arrow.join(cols)
    with K.card("map"):
        st.html(K.card_title("Trace map", "Backward to the material source, forward to every order and customer that consumed the same lots", "branch") +
                f'<div style="overflow-x:auto"><div style="display:grid;grid-template-columns:minmax(130px,1fr) 16px minmax(130px,1fr) 16px minmax(130px,1fr) 16px minmax(130px,1fr) 16px minmax(130px,1fr);'
                f'gap:0;align-items:stretch;min-width:760px">{grid}</div></div>')

    c1, c2 = st.columns(2)
    with c1, K.card("contain"):
        steps = [("Quarantine " + str(imp["total_orders_affected"]) + " orders", ", ".join(acts.get("quarantine_orders", [])[:8]) + ("…" if len(acts.get("quarantine_orders", [])) > 8 else "")),
                 ("Recall " + str(len(acts.get("recall_shipments", []))) + " shipments", ", ".join(acts.get("recall_shipments", [])) or "None"),
                 ("Notify " + str(len(acts.get("notify_customers", []))) + " customers", ", ".join(acts.get("notify_customers", [])) or "None"),
                 ("Hold material lots", ", ".join(l["lot_id"] for l in lots) or "None"),
                 ("Verify containment", f"100% inspect {d['defect_type'].lower()} on {po.get('machine_id')} output until D4 closes")]
        items = "".join(f'<li style="list-style:none;display:grid;grid-template-columns:24px 1fr;gap:10px">'
                        f'<span style="width:22px;height:22px;border-radius:99px;border:1.5px solid #F26B1D;color:#B54111;font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center">{i}</span>'
                        f'<div><div style="font-weight:600;font-size:13px">{K.esc(a)}</div><div class="sx-subtle sx-mono" style="font-size:11.5px;word-break:break-word">{K.esc(b)}</div></div></li>'
                        for i, (a, b) in enumerate(steps, 1))
        st.html(K.card_title("Containment checklist", "Generated from the trace", "shield") + f'<ol style="margin:0;padding:0;display:grid;gap:12px">{items}</ol>')
    with c2, K.card("ships"):
        st.html(K.card_title("Shipments", f"{len(ships)} shipment{'s' if len(ships) != 1 else ''}", "truck"))
        if ships:
            sdf = pd.DataFrame([{"Shipment": s["shipment_id"], "Order": s["order_id"], "Customer": (s.get("customer") or {}).get("name", s["customer_id"]),
                                 "Date": s["ship_date"], "Qty": int(s["qty"]), "Status": s["tracking_status"]} for s in ships])
            st.dataframe(sdf, hide_index=True)
        else:
            st.caption("No shipments consumed the affected lots.")
