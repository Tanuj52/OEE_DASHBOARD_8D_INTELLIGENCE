"""
Signal Orange UI kit for the Streamlit app.

Colours, fonts and radii come from .streamlit/config.toml. This module adds the
pieces Streamlit has no native element for, mirroring the React app: white
cards on a warm-neutral canvas, status pills that never rely on colour alone
(icon + text + shape), KPI tiles with sparklines, and a consistent Altair look.
All dynamic text is HTML-escaped before it is rendered.
"""

import re
from html import escape
from urllib.parse import quote

import altair as alt
import streamlit as st

# Design tokens (light) - keep in sync with config.toml and the React CSS variables.
BG, SURFACE, S2, S3 = "#F8F7F5", "#FFFFFF", "#F5F4F2", "#ECEAE7"
LINE, LINE_STRONG = "#E5E2DE", "#D0CCC7"
FG, MUTED, SUBTLE = "#1B1816", "#57524D", "#78716B"
ACCENT, ACCENT_TEXT, ACCENT_FG, ACCENT_SOFT = "#F26B1D", "#B54111", "#FFFFFF", "#FFF1E8"
ACCENT_45 = "#F8B48C"   # accent at ~45% on white
NEUTRAL_SOFT = "#C9C4BE"

CSS = """
<style>
:root{--bg:#F8F7F5;--surface:#FFFFFF;--s2:#F5F4F2;--s3:#ECEAE7;--line:#E5E2DE;--line-strong:#D0CCC7;
--fg:#1B1816;--muted:#57524D;--subtle:#78716B;--accent:#F26B1D;--accent-text:#B54111;--accent-fg:#FFFFFF;--accent-soft:#FFF1E8;}
html,body{font-feature-settings:'cv11','ss01';-webkit-font-smoothing:antialiased}
[data-testid="stMainBlockContainer"]{padding-top:1.25rem;padding-bottom:6rem;max-width:1520px}
.st-key-sx-styles{display:none}
[data-testid="stHeader"]{background:transparent}
h1,h2,h3{letter-spacing:-0.015em}
.tabular{font-variant-numeric:tabular-nums}
::selection{background:rgba(242,107,29,.30)}

/* Cards: st.container(border=True, key="card_*") */
div[class*="st-key-card_"]{background:var(--surface);border:1px solid var(--line)!important;border-radius:14px!important;
  box-shadow:0 1px 0 0 rgba(229,226,222,.6),0 1px 3px 0 rgba(0,0,0,.05);padding:16px 18px 18px!important}
div[class*="st-key-cardaccent_"]{position:relative;overflow:hidden;background:var(--surface);border:1px solid rgba(242,107,29,.55)!important;border-radius:14px!important;
  box-shadow:0 1px 3px 0 rgba(0,0,0,.05);padding:14px 18px 14px 22px!important}
div[class*="st-key-cardaccent_"]::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--accent)}
div[class*="st-key-flat_"]{background:var(--s2);border:1px solid var(--line)!important;border-radius:10px!important;padding:12px 14px!important}

/* Buttons: white, semibold text on Signal Orange (deepens on hover) */
[data-testid="stBaseButton-primary"],[data-testid="stBaseButton-primaryFormSubmit"]{color:var(--accent-fg)!important;font-weight:600;border-color:var(--accent)!important}
[data-testid="stBaseButton-primary"] p,[data-testid="stBaseButton-primaryFormSubmit"] p,
[data-testid="stBaseButton-primary"] [data-testid="stIconMaterial"],[data-testid="stBaseButton-primaryFormSubmit"] [data-testid="stIconMaterial"]{color:var(--accent-fg)!important;font-weight:600}
[data-testid="stBaseButton-primary"]:hover,[data-testid="stBaseButton-primaryFormSubmit"]:hover{background:#DB5512!important;border-color:#DB5512!important}
[data-testid="stBaseButton-secondary"]{background:var(--surface)}
[data-testid="stBaseButton-secondary"] p,[data-testid="stBaseButton-tertiary"] p{font-weight:500}

/* Sidebar navigation: active link is wrapped in a keyed container (st-key-navon_*) */
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"]{padding-top:.25rem}
[data-testid="stPageLink-NavLink"]{border-radius:8px;position:relative}
[data-testid="stPageLink-NavLink"] span{font-size:13px;font-weight:500;color:var(--muted)!important}
[data-testid="stPageLink-NavLink"]:hover{background:var(--s2)}
div[class*="st-key-navon_"] [data-testid="stPageLink-NavLink"]{background:var(--s3)!important}
div[class*="st-key-navon_"] [data-testid="stPageLink-NavLink"] span{color:var(--fg)!important}
div[class*="st-key-navon_"] [data-testid="stPageLink-NavLink"] [data-testid="stIconMaterial"]{color:var(--accent-text)!important}
div[class*="st-key-navon_"] [data-testid="stPageLink-NavLink"]::before{content:"";position:absolute;left:0;top:7px;bottom:7px;width:2px;border-radius:2px;background:var(--accent)}
section[data-testid="stSidebar"] div[class*="st-key-nav"]{gap:0}

/* Tabs, expanders, metrics */
[data-baseweb="tab-list"]{gap:.25rem}
[data-baseweb="tab"] p{font-weight:500}
[data-testid="stExpander"] details{background:var(--surface);border-radius:10px}
[data-testid="stExpander"] summary p{font-weight:500}

/* ---------- HTML primitives ---------- */
.sx-label{font-size:11px;line-height:16px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--subtle)}
.sx-mono{font-family:'JetBrains Mono',ui-monospace,monospace}
.sx-muted{color:var(--muted)} .sx-subtle{color:var(--subtle)} .sx-accent{color:var(--accent-text)}
.sx-row{display:flex;align-items:center;gap:8px}
.sx-card{background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:0 1px 3px 0 rgba(0,0,0,.05)}
.sx-inset{background:var(--s2);border:1px solid var(--line);border-radius:10px}
.sx-title{display:flex;align-items:flex-start;gap:10px;margin:0 0 10px}
.sx-title .ico{color:var(--subtle);margin-top:2px}
.sx-title h3{font-size:14px;font-weight:600;margin:0;padding:0;color:var(--fg);line-height:20px}
.sx-title p{font-size:12px;color:var(--subtle);margin:1px 0 0;line-height:16px}

.sx-pill{display:inline-flex;align-items:center;gap:4px;white-space:nowrap;border-radius:6px;border:1px solid var(--line-strong);
  padding:1px 7px;font-size:11.5px;line-height:18px;font-weight:600;color:var(--muted);background:var(--surface)}
.sx-pill.xs{font-size:11px;padding:0 6px;line-height:17px}
.sx-pill svg{width:12px;height:12px;flex:none}
.sx-pill.crit{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}
.sx-pill.warn{background:var(--accent-soft);border-color:rgba(242,107,29,.45);color:var(--accent-text)}
.sx-pill.drift{background:transparent;border-style:dashed;border-color:rgba(242,107,29,.75);color:var(--accent-text)}
.sx-pill.crit .sx-dot,.sx-pill.crit .sx-dot.live::after{background:var(--accent-fg)}
.sx-pill.off{color:var(--subtle);border-color:var(--line)}
.sx-pill.solid{background:var(--fg);border-color:var(--fg);color:#fff}

.sx-chip{display:inline-flex;align-items:center;gap:7px;height:32px;padding:0 11px;border-radius:9px;border:1px solid var(--line);
  background:var(--surface);font-size:12px;color:var(--fg);white-space:nowrap}
.sx-chip.hot{background:var(--accent);border-color:var(--accent);color:var(--accent-fg);font-weight:600}
.sx-chip .k{color:var(--subtle)} .sx-chip.hot .k{color:rgba(255,255,255,.85)}
.sx-dot{position:relative;display:inline-block;width:8px;height:8px;border-radius:99px;background:var(--accent);flex:none}
.sx-dot.idle{background:var(--line-strong)}
.sx-dot.live::after{content:"";position:absolute;inset:0;border-radius:99px;background:var(--accent);animation:sx-ping 1.6s cubic-bezier(.2,.6,.3,1) infinite}
@keyframes sx-ping{0%{transform:scale(.8);opacity:.7}100%{transform:scale(2.4);opacity:0}}
@media (prefers-reduced-motion:reduce){.sx-dot.live::after,.sx-stripes{animation:none!important}}

.sx-topbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:2px 0 14px;margin-bottom:6px;border-bottom:1px solid var(--line)}
.sx-topbar .t{flex:1;min-width:240px}
.sx-topbar h1{font-size:19px;font-weight:600;margin:0;padding:0;line-height:26px;color:var(--fg)}
.sx-topbar p{font-size:12.5px;color:var(--subtle);margin:0}

.sx-kpi{position:relative;background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:14px 16px 12px;height:100%;
  box-shadow:0 1px 3px 0 rgba(0,0,0,.05);overflow:hidden}
.sx-kpi.emph{border-color:rgba(242,107,29,.55)}
.sx-kpi.emph::before{content:"";position:absolute;left:0;right:0;top:0;height:3px;background:var(--accent)}
.sx-kpi .top{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.sx-kpi .top svg{width:16px;height:16px;color:var(--subtle);flex:none}
.sx-kpi .v{font-size:28px;line-height:34px;font-weight:600;color:var(--fg);margin-top:6px;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.sx-kpi.emph .v{color:var(--accent-text)}
.sx-kpi .v small{font-size:13px;font-weight:500;color:var(--subtle);margin-left:4px;letter-spacing:0}
.sx-kpi .h{font-size:12px;color:var(--subtle);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sx-kpi img.spark{display:block;width:100%;height:34px;margin-top:8px}
.sx-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(138px,1fr));gap:14px}

.sx-banner{position:relative;display:flex;align-items:center;gap:14px;flex-wrap:wrap;background:var(--surface);border:1px solid rgba(242,107,29,.6);
  border-radius:14px;padding:14px 16px 14px 20px;overflow:hidden;box-shadow:0 1px 3px 0 rgba(0,0,0,.05)}
.sx-banner::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--accent)}
.sx-banner .ic{display:flex;align-items:center;justify-content:center;width:40px;height:40px;border-radius:10px;background:var(--accent);color:var(--accent-fg);flex:none}
.sx-banner .ic svg{width:20px;height:20px}
.sx-banner .tx{flex:1;min-width:260px}
.sx-banner .tx b{font-weight:600}

.sx-bar{height:8px;border-radius:99px;background:var(--s3);overflow:hidden}
.sx-bar>span{display:block;height:100%;border-radius:99px;background:var(--subtle)}
.sx-bar>span.a{background:var(--accent)}
.sx-stripes{background-image:repeating-linear-gradient(-45deg,rgba(255,255,255,.35) 0 6px,transparent 6px 12px)!important;background-size:200% 100%;animation:sx-shimmer 2.4s linear infinite}
@keyframes sx-shimmer{from{background-position:200% 0}to{background-position:-200% 0}}

.sx-table{width:100%;border-collapse:collapse;font-size:13px}
.sx-table th{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--subtle);font-weight:600;text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
.sx-table td{padding:8px 12px;border-bottom:1px solid rgba(229,226,222,.7);white-space:nowrap;color:var(--muted)}
.sx-table tr:last-child td{border-bottom:0}
.sx-table td.fg{color:var(--fg)}

/* Agent markdown output */
.st-key-agentout h3,[class*="st-key-agentout"] h3{font-size:12.5px!important;font-weight:600!important;text-transform:uppercase;letter-spacing:.06em;color:var(--accent-text)!important;margin:14px 0 6px!important;padding:0!important}
[class*="st-key-agentout"] table{font-size:12.5px}
[class*="st-key-agentout"] th{background:var(--s3);text-transform:capitalize}
[class*="st-key-agentout"] li::marker{color:var(--accent)}

/* Report page: jump bar, section badges, anchor offsets, reading size */
.sx-jump{display:flex;align-items:center;flex-wrap:wrap;gap:6px;padding:2px 2px 4px}
.sx-jump .sx-label{margin-right:4px}
.sx-jump a{display:inline-flex;align-items:center;height:26px;padding:0 10px;border-radius:7px;border:1px solid var(--line);
  background:var(--surface);font-family:'JetBrains Mono',ui-monospace,monospace;font-size:11.5px;font-weight:600;
  color:var(--accent-text)!important;text-decoration:none!important}
.sx-jump a:hover{border-color:rgba(242,107,29,.55);background:var(--accent-soft)}
[class*="st-key-agentout_rv"] p,[class*="st-key-agentout_rv"] li,[class*="st-key-agentout_hl"] p,[class*="st-key-agentout_hl"] li{font-size:14.5px}
[class*="st-key-agentout_rv"] table,[class*="st-key-agentout_hl"] table{font-size:13px}
.sx-code{display:inline-flex;align-items:center;justify-content:center;min-width:46px;height:34px;padding:0 8px;border-radius:9px;
  background:var(--accent);color:var(--accent-fg);font-weight:700;font-size:13px;flex:none}
.sx-anchor{position:relative;top:-84px;height:0;visibility:hidden}

/* Plant Assistant dock (ui/chat_dock.py): fixed bottom-right launcher and panel */
div.st-key-chatdock{position:fixed!important;right:22px;bottom:20px;z-index:999990;width:auto!important;max-width:calc(100vw - 44px)}
div.st-key-chatdock div.st-key-chat_launch button{border-radius:999px!important;padding:10px 18px!important;
  box-shadow:0 10px 28px rgba(242,107,29,.35),0 2px 6px rgba(27,24,22,.12)}
div.st-key-chatpanel{width:410px;max-width:calc(100vw - 44px);background:var(--surface);border:1px solid var(--line);
  border-radius:16px;box-shadow:0 22px 60px rgba(27,24,22,.20),0 3px 10px rgba(27,24,22,.08);padding:12px 14px 10px;gap:8px}
div.st-key-chatpanel [data-testid="stChatMessage"]{padding:6px 2px;background:transparent}
div.st-key-chatpanel [data-testid="stChatMessage"] p{font-size:13px;line-height:19px}
div.st-key-chatmsgs button p{font-size:12.5px;text-align:left}
.sx-spin{animation:sx-rot 1s linear infinite}
@keyframes sx-rot{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.sx-spin{animation:none}}
@media print{div.st-key-chatdock{display:none!important}}

@media print{section[data-testid="stSidebar"],[data-testid="stHeader"],.no-print{display:none!important}}
</style>
"""

# Lucide icon paths (24x24, stroke). Decorative icons are aria-hidden; meaning is always also in text.
ICONS = {
    "alert": '<circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/>',
    "triangle": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    "waves": '<path d="M2 6c.6.5 1.2 1 2.5 1C7 7 7 5 9.5 5c2.6 0 2.4 2 5 2 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1"/><path d="M2 12c.6.5 1.2 1 2.5 1 2.5 0 2.5-2 5-2 2.6 0 2.4 2 5 2 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1"/><path d="M2 18c.6.5 1.2 1 2.5 1 2.5 0 2.5-2 5-2 2.6 0 2.4 2 5 2 2.5 0 2.5-2 5-2 1.3 0 1.9.5 2.5 1"/>',
    "check": '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    "minus": '<circle cx="12" cy="12" r="10"/><path d="M8 12h8"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    "loader": '<path d="M21 12a9 9 0 1 1-6.219-8.56"/>',
    "xcircle": '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>',
    "shield": '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
    "factory": '<path d="M2 20a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8l-7 5V8l-7 5V4a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2Z"/><path d="M17 18h1"/><path d="M12 18h1"/><path d="M7 18h1"/>',
    "activity": '<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/>',
    "package": '<path d="m7.5 4.27 9 5.15"/><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
    "gauge": '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
    "percent": '<line x1="19" x2="5" y1="5" y2="19"/><circle cx="6.5" cy="6.5" r="2.5"/><circle cx="17.5" cy="17.5" r="2.5"/>',
    "dollar": '<circle cx="12" cy="12" r="10"/><path d="M16 8h-6a2 2 0 1 0 0 4h4a2 2 0 1 1 0 4H8"/><path d="M12 18V6"/>',
    "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "truck": '<path d="M14 18V6a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v11a1 1 0 0 0 1 1h2"/><path d="M15 18H9"/><path d="M19 18h2a1 1 0 0 0 1-1v-3.65a1 1 0 0 0-.22-.624l-3.48-4.35A1 1 0 0 0 17.52 8H14"/><circle cx="17" cy="18" r="2"/><circle cx="7" cy="18" r="2"/>',
    "branch": '<line x1="6" x2="6" y1="3" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
    "wrench": '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "database": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5V19A9 3 0 0 0 21 19V5"/><path d="M3 12A9 3 0 0 0 21 12"/>',
    "cpu": '<rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/>',
    "arrow": '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
    "layers": '<path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>',
    "pause": '<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
    "trend": '<polyline points="22 17 13.5 8.5 8.5 13.5 2 7"/><polyline points="16 17 22 17 22 11"/>',
    "list": '<path d="M3 12h.01"/><path d="M3 18h.01"/><path d="M3 6h.01"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M8 6h13"/>',
    "file": '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>'
            '<path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
    "mail": '<rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>',
    "box": '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
    "user": '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    "diamond": '<path d="M2.7 10.3a2.41 2.41 0 0 0 0 3.41l7.59 7.59a2.41 2.41 0 0 0 3.41 0l7.59-7.59a2.41 2.41 0 0 0 0-3.41l-7.59-7.59a2.41 2.41 0 0 0-3.41 0Z"/>',
}


def _svg(paths: str, stroke: str = "black") -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{stroke}" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round">{paths}</svg>')


# st.html sanitises inline <svg>, so icons are CSS masks over currentColor (they inherit text colour).
ICON_CSS = "<style>.sx-i{display:inline-block;flex:none;vertical-align:-2px;background:currentColor;" \
    "-webkit-mask:var(--m) center/contain no-repeat;mask:var(--m) center/contain no-repeat}" + "".join(
        f".sx-i.i-{k}{{--m:url(\"data:image/svg+xml,{quote(_svg(v), safe='')}\")}}" for k, v in ICONS.items()) + "</style>"


def icon(name: str, size: int = 16, cls: str = "") -> str:
    return f'<span class="sx-i i-{name} {cls}" style="width:{size}px;height:{size}px" aria-hidden="true"></span>'


def esc(v) -> str:
    return escape("" if v is None else str(v))


def md(text: str | None) -> str:
    """LLM markdown for st.markdown: escape `$` so currency is not typeset as LaTeX."""
    return re.sub(r"(?<!\\)\$", r"\\$", text or "")


# status -> (css variant, icon, default label)
STATUS = {
    "critical": ("crit", "alert", "Critical"), "Critical": ("crit", "alert", "Critical"),
    "high": ("warn", "triangle", "High"), "warning": ("warn", "triangle", "Warning"), "Major": ("warn", "triangle", "Major"),
    "drift": ("drift", "waves", "Drift"), "degradation": ("warn", "triangle", "Degrading"),
    "medium": ("ok", "info", "Medium"), "Minor": ("ok", "info", "Minor"),
    "healthy": ("ok", "check", "Healthy"), "low": ("ok", "check", "OK"), "ok": ("ok", "check", "OK"),
    "learning": ("off", "loader", "Learning"), "unmonitored": ("off", "minus", "No sensors"),
    "completed": ("ok", "check", "Completed"), "running": ("warn", "loader", "Running"), "thinking": ("warn", "loader", "Reasoning"),
    "failed": ("crit", "xcircle", "Failed"), "cancelled": ("off", "minus", "Cancelled"), "skipped": ("off", "minus", "Skipped"),
    "pending": ("off", "minus", "Pending"), "queued": ("off", "clock", "Queued"), "done": ("ok", "check", "Done"),
    "Open": ("warn", "triangle", "Containment open"), "Closed": ("ok", "check", "Closed"),
    # machine lifecycle and work orders
    "down": ("crit", "wrench", "Down"), "tripped": ("crit", "wrench", "Down"), "stopped": ("warn", "wrench", "Stopped"),
    "in_repair": ("warn", "wrench", "In repair"), "commissioning": ("drift", "loader", "Commissioning"),
    "awaiting": ("crit", "clock", "Awaiting repair"), "planned": ("off", "clock", "Planned"), "closed": ("ok", "check", "Closed"),
    "plan": ("warn", "wrench", "Plan maintenance"), "urgent": ("crit", "wrench", "Maintenance due"),
    "P1": ("crit", "alert", "Emergency"), "P2": ("warn", "triangle", "Urgent"), "P3": ("off", "clock", "Routine"),
}


def pill(status: str, label: str | None = None, size: str = "") -> str:
    variant, ico, default = STATUS.get(status, ("ok", "info", str(status).title()))
    return f'<span class="sx-pill {variant} {size}">{icon(ico, 12)}{esc(label or default)}</span>'


def dot(live: bool = True) -> str:
    return f'<span class="sx-dot {"live" if live else "idle"}" aria-hidden="true"></span>'


def html(markup: str):
    st.html(markup)


def card_title(title: str, subtitle: str | None = None, ico: str | None = None, right: str = "") -> str:
    sub = f"<p>{esc(subtitle)}</p>" if subtitle else ""
    i = f'<span class="ico">{icon(ico, 16)}</span>' if ico else ""
    r = f'<div style="margin-left:auto">{right}</div>' if right else ""
    return f'<div class="sx-title">{i}<div style="min-width:0"><h3>{esc(title)}</h3>{sub}</div>{r}</div>'


def card(key: str, accent: bool = False):
    """White card container (bordered, keyed for styling)."""
    return st.container(border=True, key=f"{'cardaccent' if accent else 'card'}_{key}")


def flat(key: str):
    return st.container(border=True, key=f"flat_{key}")


def sparkline(values, width: int = 160, height: int = 34, fill: bool = True, color: str = ACCENT) -> str:
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    pts = [(i * (width - 6) / (n - 1) + 3, height - 4 - (v - lo) / span * (height - 9)) for i, v in enumerate(vals)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f'<polygon points="3,{height} {line} {width - 3},{height}" fill="{color}" fill-opacity="0.10"/>' if fill else ""
    lx, ly = pts[-1]
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" preserveAspectRatio="none">{area}'
           f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="1.6" stroke-linejoin="round"/>'
           f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.4" fill="{color}"/></svg>')
    return (f'<img class="spark" style="display:block;width:100%;height:{height}px" '
            f'src="data:image/svg+xml,{quote(svg, safe="")}" alt="Trend, latest {vals[-1]:g}"/>')


def kpi(label: str, value: str, unit: str = "", hint: str = "", spark=None, ico: str | None = None, emphasis: bool = False) -> str:
    u = f"<small>{esc(unit)}</small>" if unit else ""
    return (f'<div class="sx-kpi{" emph" if emphasis else ""}" role="group" aria-label="{esc(label)}: {esc(value)} {esc(unit)}">'
            f'<div class="top"><span class="sx-label">{esc(label)}</span>{icon(ico) if ico else ""}</div>'
            f'<div class="v">{esc(value)}{u}</div><div class="h" title="{esc(hint)}">{esc(hint)}</div>'
            f'{sparkline(spark) if spark is not None else ""}</div>')


def bar(pct: float, accent: bool = False, striped: bool = False, label: str = "Progress") -> str:
    pct = max(0.0, min(100.0, float(pct or 0)))
    cls = ("a" if accent else "") + (" sx-stripes" if striped else "")
    return (f'<div class="sx-bar" role="progressbar" aria-label="{esc(label)}" aria-valuenow="{pct:.0f}" '
            f'aria-valuemin="0" aria-valuemax="100"><span class="{cls}" style="width:{pct:.1f}%"></span></div>')


def table(columns: list[tuple[str, str]], rows: list[dict], fmt: dict | None = None) -> str:
    """Small HTML table. fmt maps column key -> callable(row) returning trusted HTML."""
    fmt = fmt or {}
    head = "".join(f'<th scope="col">{esc(label)}</th>' for _, label in columns)
    body = "".join("<tr>" + "".join(
        f"<td>{fmt[k](r) if k in fmt else esc(r.get(k))}</td>" for k, _ in columns) + "</tr>" for r in rows)
    return f'<div style="overflow-x:auto"><table class="sx-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def num(v, d: int = 0) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{d}f}"
    except (TypeError, ValueError):
        return str(v)


def count(n, word: str, plural: str | None = None) -> str:
    """"1 order", "3 orders"."""
    n = int(n or 0)
    return f"{n} {word if n == 1 else plural or word + 's'}"


def usd(v) -> str:
    return "—" if v is None else f"${float(v):,.0f}"


def hours(h) -> str:
    """Duration given in hours, shown in the largest sensible unit (s, min, h, d)."""
    if h is None:
        return "—"
    h = float(h)
    if h * 60 < 1.5:
        return f"{max(h, 0) * 3600:.0f} s"
    if h < 1.5:
        return f"{h * 60:.0f} min"
    if h < 48:
        return f"{h:.0f} h"
    return f"{h / 24:.1f} d"


def seconds(s) -> str:
    """Elapsed time such as 4m 05s or 1h 12m."""
    if s is None:
        return "—"
    s = int(max(float(s), 0))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    if s < 172800:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def duration(h) -> str:
    """Plain-language duration for a forecast in hours: 28 minutes, 37 hours, 2.4 days."""
    if h is None:
        return "—"
    h = max(float(h), 0.0)
    if h * 60 < 1.5:
        return "under a minute"
    if h < 1.5:
        return f"{h * 60:.0f} minutes"
    if h < 48:
        return f"{h:.0f} hours"
    return f"{h / 24:.1f} days"


def by_when(h) -> str:
    """Clock time h hours from now: 14:05 today, Tue 09:00, or Oct 14."""
    from datetime import datetime, timedelta
    if h is None:
        return "—"
    t = datetime.now() + timedelta(hours=float(h))
    if t.date() == datetime.now().date():
        return f"{t:%H:%M} today"
    if float(h) < 24 * 6:
        return f"{t:%a %H:%M}"
    return f"{t:%b %-d}"


# Plain-language readings of the model's statistics (the numbers stay available in Engineer view).
def unusual(score, thr) -> tuple[str, str]:
    """(label, pill status) for the anomaly score against its limit."""
    if score is None or not thr:
        return "—", "learning"
    r = float(score) / float(thr)
    if r < 1:
        return "Normal", "healthy"
    if r < 1.5:
        return "Elevated", "drift"
    if r < 2.5:
        return "High", "warning"
    return "Severe", "critical"


def deviation(z) -> str:
    """How far one reading is from the machine's normal."""
    if z is None:
        return ""
    a = abs(float(z))
    side = "above" if z > 0 else "below"
    if a < 2:
        return "normal"
    if a < 3:
        return f"slightly {side} normal"
    if a < 5:
        return f"{side} normal"
    return f"well {side} normal"


def shift(psi, crit) -> str:
    """Behaviour change of one signal vs. the learned normal (from PSI)."""
    if psi is None or not crit:
        return ""
    if psi >= crit:
        return "Large"
    if psi >= crit * 0.5:
        return "Small"
    return ""


GLOSSARY = [
    ("Health", "How far each signal is from its critical limit, 100% = at its normal level, 0% = at the limit."),
    ("Unusual behaviour", "How different the machine looks from the normal pattern it learned, across all six signals together. "
                          "It rises before any single limit is crossed. Engineer view shows it as a score in σ (standard deviations)."),
    ("Behaviour change", "A signal has settled somewhere new. The model then checks the history: a steady trend means wear; "
                         "a one-off step (new material lot, new setting) is a process change and the model re-learns normal. "
                         "Engineer view shows PSI, the population stability index."),
    ("Time to failure", "When the worsening signal is forecast to reach its critical limit, with a likely range. Also called "
                        "remaining useful life (RUL). It is refitted every minute and counts down in between."),
    ("Protective stop", "A machine that stays above a critical limit stops automatically to prevent damage, raises an "
                        "emergency work order and stays down until a repair is completed."),
    ("Commissioning", "After a repair the machine runs again while the model learns its new normal; alerts stay off until then."),
]


def engineer() -> bool:
    return bool((st.session_state.get("settings") or {}).get("display", {}).get("engineer_view"))


LIFECYCLE = {"running": "Running", "commissioning": "Commissioning: learning the new normal",
             "tripped": "Stopped automatically, awaiting maintenance", "stopped": "Taken offline for maintenance",
             "in_repair": "In repair"}


def ts(iso: str | None) -> str:
    """Timestamp: clock time for today's samples, date and time otherwise."""
    if not iso:
        return "—"
    from datetime import datetime
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if t.second or len(iso) > 16:
        now = datetime.now(t.tzinfo) if t.tzinfo else datetime.now()
        return t.strftime("%H:%M:%S") if t.date() == now.date() else t.strftime("%b %-d, %H:%M:%S")
    return t.strftime("%b %-d, %I:%M %p")


sim_time = ts


SENSOR_META = {
    "vibration_mm_s": ("Vibration", "mm/s", 2), "temp_c": ("Temperature", "°C", 1), "pressure_bar": ("Pressure", "bar", 2),
    "power_kw": ("Power", "kW", 1), "coolant_flow_lpm": ("Coolant flow", "L/min", 2), "spindle_rpm": ("Spindle speed", "rpm", 0),
}


def sensor_label(s: str) -> str:
    return SENSOR_META.get(s, (s, "", 2))[0]


def sensor_unit(s: str) -> str:
    return SENSOR_META.get(s, (s, "", 2))[1]


def sensor_val(s: str, v) -> str:
    return num(v, SENSOR_META.get(s, (s, "", 2))[2])


# ---------------------------------------------------------------------------
# Altair
# ---------------------------------------------------------------------------

def style(chart: alt.TopLevelMixin, height: int = 240) -> alt.TopLevelMixin:
    """Apply the Signal Orange chart look (call on the top-level chart, after layering)."""
    return (chart.properties(height=height, background="transparent")
            .configure(font="Inter")
            .configure_view(stroke=None)
            .configure_axis(labelColor=SUBTLE, titleColor=SUBTLE, labelFontSize=11, titleFontSize=11, titleFontWeight=500,
                            gridColor=LINE, gridDash=[3, 3], domain=False, tickColor=LINE, tickSize=4, labelPadding=4)
            .configure_axisX(grid=False)
            .configure_legend(labelColor=MUTED, titleColor=SUBTLE, labelFontSize=11, titleFontSize=11, symbolSize=80, orient="bottom")
            .configure_title(color=FG, fontSize=12, fontWeight=600, anchor="start"))


def chart(c: alt.TopLevelMixin, height: int = 240, key: str | None = None):
    st.altair_chart(style(c, height), theme=None, key=key, width="stretch")
