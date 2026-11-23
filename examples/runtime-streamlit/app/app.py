"""A Streamlit app that runs on dapp-runtime:python-3.12: no Dockerfile, no image build."""

import os
import platform

import humanize  # not in the runtime image: comes from requirements.txt at start
import pandas as pd
import streamlit as st

st.set_page_config(page_title="dapp runtime demo", layout="centered")
st.title("Runs from source on DataOS (v2, edited)")
st.write("This app was deployed with `runtime: python-3.12` and `code: ./app`: no Dockerfile, no image build.")
c1, c2, c3 = st.columns(3)
c1.metric("Python", platform.python_version())
c2.metric("Code version", os.environ.get("DAPP_CODE_VERSION", "local"))
c3.metric("Streamlit", st.__version__)

rows = st.slider("Rows", 5, 50, 12)
df = pd.DataFrame({"day": pd.date_range("2026-11-01", periods=rows), "orders": [(i * 37) % 101 + 20 for i in range(rows)]})
st.line_chart(df, x="day", y="orders")
st.caption(f"{humanize.intcomma(int(df['orders'].sum()))} orders in total")
