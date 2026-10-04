import time
from datetime import datetime

import pandas as pd
import streamlit as st

from snowflake_app.core import parts as P
from snowflake_app.services import parts_ops
from snowflake_app.ui import kit as K, shell

PATH = "app_pages/spare_parts.py"
shell.topbar(PATH)
ss = st.session_state
mon = shell.monitor()
NOW = time.time()
OPEN = ("planned", "awaiting", "in_repair")
wos = mon.work_orders()
open_wos = [w for w in wos if w["status"] in OPEN]
outlook = mon.outlook()
plan = parts_ops.plan(outlook, open_wos, NOW)
MST, STK, INC, SUG = plan["master"], plan["stock"], plan["incoming"], plan["suggestions"]
POS = parts_ops.pos()
held = P.reservations(plan["txns"])
URGENCY = {"stock_out": ("crit", "Stock-out"), "forecast": ("warn", "Needed soon"), "below_reorder": ("off", "Below reorder point")}


# ------------------------------------------------------------------ callbacks
def _request(pid: str, qty: int, reason: str):
    po = parts_ops.create_po(pid, qty, reason, user=shell.current_user())
    shell.notify(f"Purchase request {po['po_id']}: {po['qty']} × {po['part_name']}", icon=":material/shopping_cart:")


def _request_all():
    n = 0
    for s in SUG:
        if not s["incoming"]:
            parts_ops.create_po(s["part_id"], s["qty"], _why(s), user=shell.current_user())
            n += 1
    shell.notify(f"{n} purchase requests raised" if n else "Every suggestion already has a request", icon=":material/shopping_cart:")


def _po(po_id: str, action: str):
    po = parts_ops.po_action(po_id, action, shell.current_user())
    if po:
        msg = {"approve": "approved", "order": f"ordered, expected {K.ts(po.get('expected_at'))}", "receive": "received into stock",
               "cancel": "cancelled"}[action]
        shell.notify(f"{po_id} {msg}", icon=":material/inventory_2:")


def _adjust():
    pid, qty = ss.get("adj_part"), int(ss.get("adj_qty") or 0)
    if pid and qty and parts_ops.adjust(pid, qty, ss.get("adj_note") or "Stock count", shell.current_user()):
        shell.notify(f"Stock of {parts_ops.by_id()[pid]['part_name']} adjusted by {qty:+d}", icon=":material/tune:")
        ss.adj_qty, ss.adj_note = 0, ""


def _why(s: dict) -> str:
    if s["urgency"] == "stock_out":
        jobs = ", ".join(w["wo_id"] for w in plan["shorts"].get(s["part_id"], [])) or "jobs"
        return f"Stock-out: {jobs} waiting" if plan["shorts"].get(s["part_id"]) else "Out of stock"
    if s["urgency"] == "forecast":
        return "Needed soon: " + ", ".join(f"{m['machine_id']} in {K.duration(m['rul_hours'])}" for m in plan["forecast"].get(s["part_id"], []))
    return f"Below reorder point ({s['projected']} free vs. {s['reorder_point']})"


def _recent(iso: str | None, days: float) -> bool:
    try:
        return bool(iso) and NOW - datetime.fromisoformat(iso).timestamp() < days * 86400
    except ValueError:
        return False


# ------------------------------------------------------------------ header
reserved_units = sum(s["reserved"] for s in STK.values())
open_pos = [p for p in POS if p["status"] in P.INCOMING]
late = [s for s in SUG if s["late_for"]]
tiles = [
    K.kpi("Stock value", K.usd(plan["value"]), "", f"{len(MST)} spare parts on the shelf", ico="box"),
    K.kpi("Parts to order", str(len(SUG)), "", f"{sum(1 for s in SUG if s['urgency'] == 'stock_out')} stock-outs", ico="package",
          emphasis=any(s["urgency"] == "stock_out" for s in SUG)),
    K.kpi("Held for jobs", str(reserved_units), "", f"{len(held)} open work orders", ico="wrench"),
    K.kpi("Open purchase orders", str(len(open_pos)), "", f"{sum(1 for p in open_pos if p['status'] == 'requested')} waiting for approval",
          ico="truck"),
    K.kpi("Won't arrive in time", str(len(late)), "", "Lead time ends after the job needs it", ico="clock", emphasis=bool(late)),
]
st.html(f'<section class="sx-kpis" aria-label="Spare parts indicators">{"".join(tiles)}</section>')

# ------------------------------------------------------------------ suggestions
left, right = st.columns([7, 5])
with left, K.card("sp_sugg", accent=bool(SUG)):
    st.html(K.card_title("What to order", "Free stock after reservations, open requests and parts machines heading for failure will need",
                         "package", f'<span class="sx-pill off xs tabular">{len(SUG)}</span>'))
    if not SUG:
        st.html('<div class="sx-subtle" style="font-size:12.5px;padding:6px 0">Nothing to order: every part is above its reorder point '
                'and covers the jobs and forecasts.</div>')
    for s in SUG:
        v, label = URGENCY[s["urgency"]]
        flag = (f'<div style="font-size:12px;color:#B54111;margin-top:3px">{K.icon("clock", 12)} Lead time {s["lead_time_days"]} d: '
                f'arrives after {", ".join(w["wo_id"] for w in s["late_for"])} needs it. Expedite or use a substitute.</div>'
                if s["late_for"] else "")
        c1, c2 = st.columns([5, 2], vertical_alignment="center")
        c1.html(f'<div class="sx-row" style="gap:6px;flex-wrap:wrap"><span class="sx-pill {v} xs">{K.esc(label)}</span>'
                f'<b style="font-size:13.5px">{K.esc(s["part_name"])}</b><span class="sx-mono sx-subtle" style="font-size:11px">{K.esc(s["part_id"])}</span></div>'
                f'<div class="sx-muted" style="font-size:12.5px;margin-top:2px">{K.esc(_why(s))}</div>'
                f'<div class="sx-subtle" style="font-size:12px;margin-top:2px">Order {s["qty"]} from {K.esc(s["vendor_name"] or "—")} · '
                f'{K.usd(s["qty"] * s["unit_cost_usd"])} · {s["lead_time_days"]} d lead time · arrives {K.esc(K.ts(s["arrives_at"]))}'
                + (f' · {s["incoming"]} already requested' if s["incoming"] else "") + f'</div>{flag}')
        c2.button("Request", key=f"sp_req_{s['part_id']}", icon=":material/add_shopping_cart:", type="primary" if not s["incoming"] else "secondary",
                  on_click=_request, args=(s["part_id"], s["qty"], _why(s)), width="stretch")
    if SUG:
        st.button("Request all", key="sp_req_all", icon=":material/shopping_cart:", on_click=_request_all,
                  help="One purchase request per suggestion that has none yet")

with right, K.card("sp_held"):
    st.html(K.card_title("Held for work orders", "Parts reserved when a job was raised; issued or released when it closes", "wrench"))
    if not held:
        st.html('<div class="sx-subtle" style="font-size:12.5px">No parts are held right now.</div>')
    cov = P.coverage(plan["txns"], STK)
    names = {p["part_id"]: p["part_name"] for p in MST}
    wo_by = {w["wo_id"]: w for w in wos}
    for wo_id, items in held.items():
        w = wo_by.get(wo_id) or {}
        lines = "".join(f'<li style="list-style:none;font-size:12.5px;color:{"#57524D" if cov.get((wo_id, pid), True) else "#B54111"}">'
                        f'{q} × {K.esc(names.get(pid, pid))} · {"on the shelf" if cov.get((wo_id, pid), True) else "short"}</li>'
                        for pid, q in items.items())
        st.html(f'<div style="border:1px solid #E5E2DE;border-radius:10px;padding:8px 10px;margin-bottom:6px">'
                f'<div class="sx-row" style="gap:6px"><span class="sx-mono" style="font-size:12px;font-weight:600">{K.esc(wo_id)}</span>'
                f'{K.pill(w.get("status") or "pending", size="xs")}<span class="sx-muted" style="font-size:12px">{K.esc(w.get("machine_id") or "")}</span></div>'
                f'<ul style="margin:4px 0 0;padding:0">{lines}</ul></div>')

# ------------------------------------------------------------------ purchase orders
with K.card("sp_pos"):
    st.html(K.card_title("Purchase orders", "Requested → approved → ordered → received. Receiving puts the parts on the shelf.", "truck"))
    lanes = [("requested", "Requested"), ("approved", "Approved"), ("ordered", "Ordered"), ("received", "Received, last 14 days")]
    cols = st.columns(4)
    for col, (status, title) in zip(cols, lanes):
        items = [p for p in POS if p["status"] == status and (status != "received" or _recent(p.get("received_at"), 14))]
        with col:
            st.html(f'<div class="sx-label" style="margin-bottom:4px">{K.esc(title)} · {len(items)}</div>')
            if not items:
                st.html('<div class="sx-subtle" style="font-size:12px">None</div>')
            for p in items[:8]:
                eta = f' · expected {K.esc(K.ts(p["expected_at"]))}' if status == "ordered" and p.get("expected_at") else ""
                st.html(f'<div style="border:1px solid #E5E2DE;border-radius:10px;padding:8px 10px;background:#fff">'
                        f'<div class="sx-mono" style="font-size:11.5px;color:#57524D">{K.esc(p["po_id"])}</div>'
                        f'<div style="font-size:13px;font-weight:600">{p["qty"]} × {K.esc(p["part_name"])}</div>'
                        f'<div class="sx-subtle" style="font-size:11.5px">{K.esc(p.get("vendor_name") or "")} · {K.usd(p["qty"] * float(p["unit_cost_usd"]))}{eta}</div>'
                        + (f'<div class="sx-muted" style="font-size:11.5px">{K.esc(p.get("reason") or "")}</div>' if p.get("reason") else "") + "</div>")
                with st.container(horizontal=True, gap="small"):
                    if status == "requested":
                        st.button("Approve", key=f"po_ap_{p['po_id']}", type="primary", on_click=_po, args=(p["po_id"], "approve"))
                    elif status == "approved":
                        st.button("Place order", key=f"po_or_{p['po_id']}", type="primary", on_click=_po, args=(p["po_id"], "order"))
                    elif status == "ordered":
                        st.button("Receive", key=f"po_rc_{p['po_id']}", type="primary", on_click=_po, args=(p["po_id"], "receive"))
                    if status in P.INCOMING:
                        st.button("Cancel", key=f"po_cx_{p['po_id']}", type="tertiary", on_click=_po, args=(p["po_id"], "cancel"))

# ------------------------------------------------------------------ stock
with K.card("sp_stock"):
    st.html(K.card_title("Stock", "Opening balance plus every receipt, issue, adjustment and reservation", "box"))
    sugg = {s["part_id"]: s for s in SUG}
    df = pd.DataFrame([{
        "Part": p["part_name"], "ID": p["part_id"], "Category": p["category"], "On hand": STK[p["part_id"]]["on_hand"],
        "Held": STK[p["part_id"]]["reserved"], "Free": STK[p["part_id"]]["available"],
        "On order": (INC.get(p["part_id"]) or {}).get("qty", 0), "Reorder at": int(p["reorder_point"]),
        "Status": URGENCY[sugg[p["part_id"]]["urgency"]][1] if p["part_id"] in sugg else "OK",
        "Lead time (d)": p["lead_time_days"], "Vendor": p["vendor_name"], "Unit cost": float(p["unit_cost_usd"]),
        "Value": max(STK[p["part_id"]]["on_hand"], 0) * float(p["unit_cost_usd"]), "Fits": p["machine_compatibility"],
    } for p in MST])
    st.dataframe(df, hide_index=True, width="stretch", height=min(38 + 35 * len(df), 560),
                 column_config={"Unit cost": st.column_config.NumberColumn(format="$%.2f"),
                                "Value": st.column_config.NumberColumn(format="$%.0f")})
    with st.popover("Adjust stock", icon=":material/tune:"):
        st.selectbox("Part", [p["part_id"] for p in MST], key="adj_part",
                     format_func=lambda i: f"{i} · {next(p['part_name'] for p in MST if p['part_id'] == i)}")
        st.number_input("Change (+ found, − missing)", -100, 100, 0, key="adj_qty")
        st.text_input("Reason", key="adj_note", placeholder="Cycle count, damaged, found")
        st.button("Record adjustment", icon=":material/save:", on_click=_adjust, type="primary")

with K.card("sp_ledger"):
    rows = list(reversed(plan["txns"]))
    st.html(K.card_title("Ledger", f"{len(rows)} stock movements, newest first", "list"))
    if rows:
        names = {p["part_id"]: p["part_name"] for p in MST}
        st.dataframe(pd.DataFrame([{"When": K.ts(t.get("ts")), "Kind": t["kind"].capitalize(), "Part": names.get(t["part_id"], t["part_id"]),
                                    "Qty": t["qty"], "Work order": t.get("wo_id") or "", "Purchase order": t.get("po_id") or "",
                                    "By": t.get("user_name") or "", "Note": t.get("note") or ""} for t in rows[:300]]),
                     hide_index=True, width="stretch", height=min(38 + 35 * len(rows), 380))
    else:
        st.caption("Movements appear here as work orders reserve and use parts and purchase orders are received.")
