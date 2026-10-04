"""
Manufacturing 8D Intelligence: Streamlit edition.

Run locally:   cd snowflake_app && streamlit run streamlit_app.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st  # noqa: E402

from snowflake_app.ui import chat_dock, kit as K, shell  # noqa: E402

st.set_page_config(page_title="8D Intelligence", page_icon=":material/precision_manufacturing:",
                   layout="wide", initial_sidebar_state="expanded")
# The marker div keeps this st.html in the main container. Style-only HTML goes to Streamlit's
# event container, where a toast raised inside a refreshing fragment (Start repair, Schedule
# maintenance, ...) replaces it, and the whole app renders unstyled until the next full rerun.
with st.container(key="sx-styles"):
    st.html(K.CSS + K.ICON_CSS + '<div class="sx-style-anchor" aria-hidden="true"></div>')
shell.init_session()

pages = {r["path"]: st.Page(r["path"], title=r["title"], icon=r["icon"], default=r.get("default", False)) for r in shell.ROUTES}
current = st.navigation(list(pages.values()), position="hidden")
shell.follow_nav()
shell.sidebar(pages, current)
err = shell.data_unavailable()
if err and current.url_path != "settings":
    shell.data_error_page(err)  # every page reads the Snowflake tables; never show them from stale files
else:
    current.run()
chat_dock.mount()
shell.flush_notices()  # notices raised by page code after the top-of-run flush
