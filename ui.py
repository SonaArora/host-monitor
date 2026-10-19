"""
Basic status UI: shows the latest processed metrics for every host and
refreshes every 5 seconds. Run with: streamlit run ui.py
"""

import json
import os
from datetime import datetime, timezone

import pandas as pd
import redis
import streamlit as st

from alert import CONFIG_PATH, load_config

r = redis.Redis(
    host=os.environ.get("REDIS_HOST", "localhost"),
    port=os.environ.get("REDIS_PORT", 6379),
    decode_responses=True,
)
# same thresholds as alert.py, so the page and the alerts agree
rules = load_config(CONFIG_PATH).get("alerts", {})

COLORS = {
    "OK": "#c8e6c9",
    "WARNING": "#fff3b0",
    "CRITICAL": "#ffcdd2",
    "STALE": "#e0e0e0",
}
# table column -> metric name in alert_config.json
COLUMNS = {
    "cpu %": "cpu_percent",
    "memory %": "memory_percent",
    "swap %": "swap_percent",
    "worst disk %": "disk_percent",
    "max temp °C": "temperature_c",
    "gpu %": "gpu_utilization_percent",
}

st.set_page_config(page_title="Host status", layout="wide")
st.title("Host status")


def level(value, metric):
    rule = rules.get(metric, {})
    if value is None:
        return "OK"
    if value > rule.get("critical", 1000):
        return "CRITICAL"
    if value > rule.get("warning", 1000):
        return "WARNING"
    return "OK"


def host_status(row, age):
    # no message for 30s: collector or consumer has stopped
    if age > 30:
        return "STALE"
    levels = []
    for column, metric in COLUMNS.items():
        levels.append(level(row[column], metric))
    if "CRITICAL" in levels:
        return "CRITICAL"
    if "WARNING" in levels:
        return "WARNING"
    return "OK"


def status_color(status):
    return f"background-color: {COLORS[status]}"


def cell_color(value, metric):
    # only color the cells that cross a threshold, so the cause is easy to see
    name = level(value, metric)
    if name == "OK":
        return ""
    return f"background-color: {COLORS[name]}"


@st.fragment(run_every=5)
def status():
    rows = []
    now = datetime.now(timezone.utc)
    for stream in r.scan_iter("processed_metrics_*"):
        latest = r.xrevrange(stream, count=1)
        if not latest:
            continue
        data = json.loads(latest[0][1]["metrics"])
        m = data["metrics"]
        age = (now - datetime.fromisoformat(data["timestamp"])).total_seconds()
        row = {
            "status": "",
            "host": data["hostname"],
            "last seen (s)": int(age),
            "cpu %": m.get("cpu_percent"),
            "memory %": m.get("memory_percent"),
            "swap %": m.get("swap_percent"),
            "load 1m": m.get("loadavg_1m"),
            "worst disk %": max(m.get("disk_percent", {}).values(), default=None),
            "max temp °C": max(m.get("temperature_c", {}).values(), default=None),
            "gpu %": m.get("gpu_utilization_percent"),
        }
        row["status"] = host_status(row, age)
        rows.append(row)

    if not rows:
        st.info("No hosts yet: start collector.py and consumer.py.")
        return

    df = pd.DataFrame(rows)
    counts = df["status"].value_counts()
    for col, name in zip(st.columns(4), ("OK", "WARNING", "CRITICAL", "STALE")):
        col.metric(name, counts.get(name, 0))

    styled = df.style.map(status_color, subset=["status"])
    for column, metric in COLUMNS.items():
        styled = styled.map(cell_color, subset=[column], metric=metric)
    st.dataframe(styled.format(precision=1), hide_index=True)
    st.caption(f"Updated {now:%H:%M:%S} UTC, refreshes every 5 seconds.")


status()
