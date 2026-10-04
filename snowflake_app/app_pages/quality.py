import altair as alt
import pandas as pd
import streamlit as st

from snowflake_app.core import data as D
from snowflake_app.tools.copq_calculator import calculate_copq
from snowflake_app.ui import kit as K, shell

PATH = "app_pages/quality.py"
shell.topbar(PATH)
ss = st.session_state


@st.cache_data(ttl=900)
def characteristics() -> list[str]:
    return D.characteristics()


@st.cache_data(ttl=900)
def machines() -> list[str]:
    return sorted(D.table("production_orders")["machine_id"].dropna().unique().tolist())


@st.cache_data(ttl=900)
def spc(char: str, mid: str | None) -> dict:
    return D.spc(char, mid)


view = st.segmented_control("Quality views", ["spc", "copq"], default="spc", key="q_view", label_visibility="collapsed",
                            format_func=lambda v: ":material/query_stats: Statistical process control" if v == "spc" else ":material/payments: Cost of poor quality")


def spc_view():
    chars = characteristics()
    with st.container(horizontal=True, vertical_alignment="bottom"):
        char = st.selectbox("Characteristic", chars, index=chars.index("Outer Diameter") if "Outer Diameter" in chars else 0, key="q_char", width=240)
        mid = st.selectbox("Machine", [None] + sorted(set(machines())), key="q_mach", width=260,
                           format_func=lambda m: "All machines" if m is None else m)
    res = spc(char, mid)
    if "error" in res:
        with K.card("nodata"):
            st.html(K.card_title("Not enough data", res["error"], "info"))
        return
    cap = res["capability"]
    cc = res["control_chart"]
    ind, mr = cc["individuals"], cc["moving_range"]
    viol = res["nelson_violations"]

    verdict = cap["verdict"]
    good = cap["cpk"] is not None and cap["cpk"] >= 1.33
    st.html('<div class="sx-kpis">' + "".join([
        K.kpi("Cpk", K.num(cap["cpk"], 2), hint=verdict, ico="gauge", emphasis=not good),
        K.kpi("Cp", K.num(cap["cp"], 2), hint="Potential capability", ico="gauge"),
        K.kpi("Est. PPM defective", K.num(cap["ppm_estimate"]), hint=f"n = {cap['sample_size']}", ico="percent", emphasis=(cap["ppm_estimate"] or 0) > 1000),
        K.kpi("Mean / std dev", f"{cap['mean']:.4f}", hint=f"σ {cap['std']:.4f} · nominal {res['nominal']:g}", ico="trend"),
        K.kpi("Nelson violations", str(len(viol)), hint="Out of control" if res["out_of_control"] else "In control", ico="alert", emphasis=bool(viol)),
    ]) + "</div>")

    pts = pd.DataFrame(res["points"])
    pts["kind"] = pts.apply(lambda r: "Spec fail" if r["fail"] else ("Rule 1 violation" if r["violation"] else "In control"), axis=1)
    lines = pd.DataFrame([{"y": ind["ucl"], "l": f"UCL {ind['ucl']:.4f}", "k": "control"}, {"y": ind["center_line"], "l": f"CL {ind['center_line']:.4f}", "k": "center"},
                          {"y": ind["lcl"], "l": f"LCL {ind['lcl']:.4f}", "k": "control"}, {"y": cap["usl"], "l": f"USL {cap['usl']:g}", "k": "spec"},
                          {"y": cap["lsl"], "l": f"LSL {cap['lsl']:g}", "k": "spec"}])
    x = alt.X("i:Q", title="Sample", axis=alt.Axis(tickMinStep=1))
    y = alt.Y("v:Q", title=char, scale=alt.Scale(zero=False))
    rule = alt.Chart(lines).mark_rule().encode(
        y="y:Q", color=alt.Color("k:N", scale=alt.Scale(domain=["control", "center", "spec"], range=[K.ACCENT, K.MUTED, K.FG]), legend=None),
        strokeDash=alt.StrokeDash("k:N", scale=alt.Scale(domain=["control", "center", "spec"], range=[[5, 4], [1, 0], [2, 3]]), legend=None))
    lbl = alt.Chart(lines).mark_text(align="left", dx=6, color=K.MUTED, fontSize=10).encode(y="y:Q", x=alt.value(0), text="l:N")
    ln = alt.Chart(pts).mark_line(color=K.NEUTRAL_SOFT, strokeWidth=1).encode(x=x, y=y)
    dots = alt.Chart(pts).mark_point(filled=True, size=26).encode(
        x=x, y=y, shape=alt.Shape("kind:N", scale=alt.Scale(domain=["In control", "Rule 1 violation", "Spec fail"], range=["circle", "diamond", "triangle-up"]),
                                  legend=alt.Legend(title=None, orient="top")),
        color=alt.Color("kind:N", scale=alt.Scale(domain=["In control", "Rule 1 violation", "Spec fail"], range=[K.MUTED, K.ACCENT, K.ACCENT_TEXT]), legend=None),
        size=alt.condition("datum.kind != 'In control'", alt.value(60), alt.value(18)),
        tooltip=[alt.Tooltip("i:Q", title="Sample"), alt.Tooltip("t:N", title="Time"), alt.Tooltip("v:Q", title="Value", format=".4f"),
                 "inspection_id", "machine_id", "kind"])

    left, right = st.columns([8, 4])
    with left:
        with K.card("ichart"):
            st.html(K.card_title("Individuals chart", "Control limits (process voice) vs. specification limits (customer voice)", "trend"))
            K.chart(alt.layer(rule, ln, dots, lbl), 290)
        with K.card("mr"):
            st.html(K.card_title("Moving range", "Short-term variation between consecutive parts", "activity"))
            mdf = pd.DataFrame({"i": range(1, len(mr["values"]) + 1), "mr": mr["values"]})
            m1 = alt.Chart(mdf).mark_line(color=K.MUTED, strokeWidth=1).encode(x=alt.X("i:Q", title="Sample"), y=alt.Y("mr:Q", title="MR"))
            m2 = alt.Chart(pd.DataFrame([{"y": mr["ucl"]}])).mark_rule(color=K.ACCENT, strokeDash=[5, 4]).encode(y="y:Q")
            m3 = alt.Chart(pd.DataFrame([{"y": mr["center_line"]}])).mark_rule(color=K.MUTED).encode(y="y:Q")
            K.chart(alt.layer(m1, m3, m2), 150)
    with right:
        with K.card("verdict", accent=not good):
            st.html(K.card_title("Capability verdict", f"{char} · {mid or 'all machines'}", "gauge", K.pill("healthy" if good else "warning", "Capable" if good else "Not capable", "xs")) +
                    f'<div style="font-size:13px" class="sx-muted">{K.esc(verdict)}. Cpk {K.num(cap["cpk"], 3)} against a 1.33 target; '
                    f'{"the process meets" if good else "the process does not meet"} the customer specification with margin.</div>')
        with K.card("hist"):
            st.html(K.card_title("Distribution vs. specification", "Histogram of measurements", "layers"))
            h = pd.DataFrame(res["histogram"])
            hb = alt.Chart(h).mark_bar(color=K.NEUTRAL_SOFT, cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(
                x=alt.X("x:Q", title=char, scale=alt.Scale(zero=False)), y=alt.Y("count:Q", title="Count"), tooltip=["x", "count"])
            spec = pd.DataFrame([{"x": cap["lsl"], "l": "LSL"}, {"x": cap["usl"], "l": "USL"}, {"x": cap["mean"], "l": "mean"}])
            sr = alt.Chart(spec).mark_rule(strokeWidth=1.5).encode(
                x="x:Q", color=alt.Color("l:N", scale=alt.Scale(domain=["LSL", "USL", "mean"], range=[K.ACCENT, K.ACCENT, K.FG]), legend=None),
                strokeDash=alt.StrokeDash("l:N", scale=alt.Scale(domain=["LSL", "USL", "mean"], range=[[1, 0], [1, 0], [4, 3]]), legend=None))
            st_ = alt.Chart(spec).mark_text(dy=-4, baseline="bottom", color=K.MUTED, fontSize=10).encode(x="x:Q", y=alt.value(0), text="l:N")
            K.chart(alt.layer(hb, sr, st_), 200)
        with K.card("viol"):
            st.html(K.card_title("Nelson violations", f"{len(viol)} detected", "alert"))
            if viol:
                rows = [{"Rule": v["rule"], "Sample": v["index"], "Description": v["description"]} for v in viol]
                st.dataframe(pd.DataFrame(rows), hide_index=True, height=min(36 * len(rows) + 40, 280))
            else:
                st.caption("No rule violations: the process is in statistical control.")


COPQ_FIELDS = [
    ("Internal failure", [("scrap_units", "Scrapped units", 135), ("unit_cost", "Unit cost ($)", 25.0), ("rework_hours", "Rework hours", 13.5),
                          ("sorting_hours", "Sorting hours", 16.0), ("downtime_hours", "Downtime hours", 12.0)]),
    ("External failure", [("warranty_claims", "Warranty claims", 4), ("returns_units", "Returned units", 25),
                          ("expedited_shipping_cost", "Expedited freight ($)", 1200.0), ("customer_penalty", "Customer penalties ($)", 0.0)]),
    ("Investigation", [("investigation_hours", "Engineering hours", 40.0)]),
]


def copq_view():
    left, right = st.columns([5, 7])
    vals = {}
    with left, K.card("copqin"):
        st.html(K.card_title("Inputs", "Defaults reflect the plant's 27 recorded defects; adjust to model a scenario", "dollar"))
        for group, fields in COPQ_FIELDS:
            st.html(f'<div class="sx-label" style="margin-top:6px">{group}</div>')
            cols = st.columns(2)
            for i, (k, label, v) in enumerate(fields):
                vals[k] = cols[i % 2].number_input(label, min_value=0.0, value=float(v), step=1.0 if isinstance(v, int) else 0.5, key=f"copq_{k}")
    for k in ("scrap_units", "warranty_claims", "returns_units"):
        vals[k] = int(vals[k])
    res = calculate_copq(**vals)
    with right:
        st.html('<div class="sx-kpis">' + "".join([
            K.kpi("Total COPQ", K.usd(res["total_copq"]), ico="dollar", emphasis=True),
            K.kpi("Internal", K.usd(res["internal_failure"]["subtotal"]), hint=f"{res['breakdown_pct']['internal']}%"),
            K.kpi("External", K.usd(res["external_failure"]["subtotal"]), hint=f"{res['breakdown_pct']['external']}%"),
            K.kpi("Investigation", K.usd(res["investigation"]["cost"]), hint=f"{res['breakdown_pct']['investigation']}%"),
        ]) + "</div>")
        rows = [("Scrap", res["internal_failure"]["scrap"], "Internal"), ("Rework", res["internal_failure"]["rework"], "Internal"),
                ("Sorting", res["internal_failure"]["sorting"], "Internal"), ("Downtime", res["internal_failure"]["downtime"], "Internal"),
                ("Warranty", res["external_failure"]["warranty"], "External"), ("Returns", res["external_failure"]["returns"], "External"),
                ("Freight", res["external_failure"]["expedited_shipping"], "External"), ("Penalty", res["external_failure"]["customer_penalty"], "External"),
                ("Engineering", res["investigation"]["cost"], "Investigation")]
        rows.sort(key=lambda r: -r[1])
        mx = rows[0][1] or 1
        items = "".join(
            f'<li style="list-style:none;display:grid;grid-template-columns:120px 1fr 90px;gap:12px;align-items:center;font-size:12.5px">'
            f'<span>{n} <span class="sx-subtle" style="font-size:11px">{g}</span></span>'
            f'<div class="sx-bar" style="height:10px"><span style="width:{v / mx * 100:.1f}%;background:{"#F26B1D" if i == 0 else ("#F8B48C" if g == "External" else "#78716B")}"></span></div>'
            f'<span class="tabular" style="text-align:right;font-weight:500">{K.usd(v)}</span></li>' for i, (n, v, g) in enumerate(rows))
        with K.card("drivers"):
            st.html(K.card_title("Cost drivers", "Largest first", "layers") + f'<ul style="margin:0;padding:0;display:grid;gap:10px">{items}</ul>'
                    '<div class="sx-subtle" style="font-size:12px;margin-top:14px">Rates: rework $75/h, sorting $45/h, downtime $500/h, warranty $200/claim, '
                    'returns $50/unit, engineering $120/h.</div>')


if (view or "spc") == "spc":
    spc_view()
else:
    copq_view()
