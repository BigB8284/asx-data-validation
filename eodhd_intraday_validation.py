"""
EODHD INTRADAY VALIDATION — free tier only
================================================
Tests whether EODHD's free API key (20 calls/day, no credit card,
sign up at eodhd.com) can actually retrieve 5-minute ASX intraday
data at all, and if so, what it looks like — before spending a
cent on a paid plan.

Runs BHP.AU FIRST, alone. If that fails outright (likely a plan-gating
rejection, not a data problem), it STOPS and reports the exact error
rather than burning quota testing the other three tickers uselessly.
If BHP.AU works, it proceeds to RIO.AU, WHC.AU, PDN.AU.

This is a data-verification tool only. It does not build, tune, or
run any part of Phase 3 — just confirms what's actually available.

SETUP:
1. Sign up free at https://eodhd.com (no credit card needed)
2. Copy your API token
3. In this app's Streamlit Cloud settings -> Secrets, add:
   EODHD_API_TOKEN = "your_token_here"
"""

import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

st.set_page_config(page_title="EODHD Intraday Validation", layout="centered")
st.title("EODHD Intraday Validation (Free Tier)")
st.caption("Tests real ASX 5-minute data access before any purchase. Fails fast to protect your daily call quota.")

TICKERS = ["BHP.AU", "RIO.AU", "WHC.AU", "PDN.AU"]
BASE_URL = "https://eodhd.com/api/intraday/{ticker}"

try:
    API_TOKEN = st.secrets["EODHD_API_TOKEN"]
except Exception:
    st.error("No EODHD_API_TOKEN found in Streamlit secrets. Add it under this app's Settings -> Secrets, then reload.")
    st.stop()


def fetch_intraday(ticker, from_dt, to_dt, interval="5m"):
    """One request. Returns (success, data_or_error, status_code)."""
    params = {
        "api_token": API_TOKEN,
        "interval": interval,
        "fmt": "json",
        "from": int(from_dt.timestamp()),
        "to": int(to_dt.timestamp()),
    }
    try:
        resp = requests.get(BASE_URL.format(ticker=ticker), params=params, timeout=20)
        if resp.status_code != 200:
            return False, resp.text[:500], resp.status_code
        data = resp.json()
        if not data:
            return False, "Empty response — no bars returned for this window.", resp.status_code
        return True, data, resp.status_code
    except Exception as e:
        return False, str(e), None


def analyze_bars(ticker, data):
    """Bar completeness, timestamp/session-boundary evidence, per-day counts."""
    df = pd.DataFrame(data)
    if "timestamp" in df.columns:
        df["utc_dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    elif "datetime" in df.columns:
        df["utc_dt"] = pd.to_datetime(df["datetime"], utc=True)
    else:
        return {"error": f"Unrecognized response columns: {list(df.columns)}"}

    # AEST is UTC+10 (no DST) / AEDT is UTC+11 (DST, roughly Oct-Apr) —
    # shown both ways rather than assumed, so the actual offset in this
    # response can be checked against the calendar date.
    df["aest_naive"] = df["utc_dt"] + pd.Timedelta(hours=10)
    df["aedt_naive"] = df["utc_dt"] + pd.Timedelta(hours=11)
    df["date"] = df["utc_dt"].dt.date

    bars_per_day = df.groupby("date").size()
    first_bar = df.iloc[0]
    last_bar = df.iloc[-1]

    return {
        "n_bars": len(df),
        "date_range": f"{df['date'].min()} to {df['date'].max()}",
        "n_trading_days": df["date"].nunique(),
        "bars_per_day_min_max_median": (int(bars_per_day.min()), int(bars_per_day.max()), int(bars_per_day.median())),
        "first_bar_utc": str(first_bar["utc_dt"]),
        "first_bar_aest_guess": str(first_bar["aest_naive"]),
        "first_bar_aedt_guess": str(first_bar["aedt_naive"]),
        "last_bar_utc": str(last_bar["utc_dt"]),
        "last_bar_aest_guess": str(last_bar["aest_naive"]),
        "sample_raw_row": data[0],
        "sample_columns": list(df.columns),
        "raw_close_first_5": df["close"].head(5).tolist() if "close" in df.columns else None,
    }


if st.button("Run validation (starts with BHP.AU only)", type="primary", use_container_width=True):
    calls_used = 0

    st.subheader("Step 1: BHP.AU — recent window (canary test)")
    recent_to = datetime.now(timezone.utc)
    recent_from = recent_to - timedelta(days=10)
    with st.spinner("Testing BHP.AU recent data access..."):
        ok, result, status = fetch_intraday("BHP.AU", recent_from, recent_to)
    calls_used += 1

    if not ok:
        st.error(f"BHP.AU FAILED (HTTP {status}). Stopping here to protect your quota — this looks like a plan/access issue, not worth spending more calls testing the other tickers until this is resolved.")
        st.code(str(result))
        st.info(f"API calls used this run: {calls_used}")
        st.stop()

    analysis = analyze_bars("BHP.AU", result)
    if "error" in analysis:
        st.error(f"BHP.AU returned data but in an unexpected format: {analysis['error']}")
        st.json(result[0] if result else {})
        st.stop()

    st.success("BHP.AU recent data: SUCCESS")
    st.json(analysis)

    st.divider()
    st.subheader("Step 2: BHP.AU — historical depth check (Oct 2020)")
    depth_from = datetime(2020, 10, 1, tzinfo=timezone.utc)
    depth_to = datetime(2020, 10, 15, tzinfo=timezone.utc)
    with st.spinner("Testing BHP.AU historical depth..."):
        ok2, result2, status2 = fetch_intraday("BHP.AU", depth_from, depth_to)
    calls_used += 1

    if ok2:
        depth_analysis = analyze_bars("BHP.AU", result2)
        st.success(f"BHP.AU Oct 2020 data: SUCCESS — {depth_analysis.get('n_bars', 0)} bars found")
        st.json(depth_analysis)
    else:
        st.warning(f"BHP.AU Oct 2020 window FAILED (HTTP {status2}) — depth may not actually reach Oct 2020 for this ticker, or this specific window has an issue.")
        st.code(str(result2))

    st.divider()
    st.subheader("Step 3: RIO.AU, WHC.AU, PDN.AU — recent window each")
    for ticker in ["RIO.AU", "WHC.AU", "PDN.AU"]:
        with st.spinner(f"Testing {ticker}..."):
            ok3, result3, status3 = fetch_intraday(ticker, recent_from, recent_to)
        calls_used += 1
        if ok3:
            a = analyze_bars(ticker, result3)
            st.success(f"{ticker}: SUCCESS — {a.get('n_bars', 0)} bars, {a.get('n_trading_days', 0)} trading days")
            with st.expander(f"{ticker} full detail"):
                st.json(a)
        else:
            st.error(f"{ticker}: FAILED (HTTP {status3})")
            st.code(str(result3))

    st.divider()
    st.info(f"Total API calls used this run: {calls_used} (out of your daily quota — check eodhd.com account page for what's left today).")
    st.caption(
        "Splits/dividends note: this test does NOT yet confirm whether intraday bars are adjusted for corporate "
        "actions — that needs a follow-up check against a known dividend/split date, which wasn't run here to "
        "conserve quota. Flag this as still open if the rest of this test passes and a purchase is being considered."
    )
else:
    st.info("Tap to run. This starts with a single BHP.AU call and stops immediately if that fails, to protect today's quota.")
