"""
EODHD INTRADAY VALIDATION — full rigor pass
================================================
Rebuilt version. The previous version showed two fixed-offset guesses
(AEST/AEDT) side by side for a human to eyeball — this version
actually resolves the correct Sydney local time per bar using real
historical DST rules (Python's zoneinfo), classifies bars as inside
or outside the normal 10:00-16:00 continuous session, excludes
incomplete trading days from any summary rather than silently using
them, and flags implausible same-day/overnight moves that could
indicate an unadjusted split or dividend.

Runs BHP.AU first as a canary (fails fast on plan/access issues before
spending calls on the other three). If it passes, runs the full check
— recent window, historical depth, and splits/dividends consistency —
for BHP, RIO, WHC, and PDN.

This is a data-verification tool only. Does not build, tune, or run
any part of Phase 3/V3.

SETUP: EODHD_API_TOKEN in this app's Streamlit Secrets (paid plan
required — confirmed the free tier returns HTTP 403 for this endpoint).
"""

import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

from eodhd_logic import (
    to_sydney_and_classify, day_completeness_report, outside_session_summary,
    flag_implausible_moves, MIN_CONTINUOUS_BARS_FOR_COMPLETE_DAY, IMPLAUSIBLE_MOVE_PCT,
)

st.set_page_config(page_title="EODHD Validation (full)", layout="wide")
st.title("EODHD Intraday Validation — Full Rigor Pass")
st.caption("Real Sydney-timezone DST handling, session classification, day-completeness exclusion, and implausible-move flagging.")

TICKERS = ["BHP.AU", "RIO.AU", "WHC.AU", "PDN.AU"]
BASE_URL = "https://eodhd.com/api/intraday/{ticker}"

try:
    API_TOKEN = st.secrets["EODHD_API_TOKEN"]
except Exception:
    st.error("No EODHD_API_TOKEN found in Streamlit secrets. Add it under this app's Settings -> Secrets, then reload.")
    st.stop()


def fetch_intraday(ticker, from_dt, to_dt, interval="5m"):
    params = {"api_token": API_TOKEN, "interval": interval, "fmt": "json",
              "from": int(from_dt.timestamp()), "to": int(to_dt.timestamp())}
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


def fetch_eod(ticker, from_date, to_date):
    url = f"https://eodhd.com/api/eod/{ticker}"
    params = {"api_token": API_TOKEN, "fmt": "json", "from": from_date, "to": to_date}
    try:
        resp = requests.get(url, params=params, timeout=20)
        if resp.status_code != 200:
            return False, resp.text[:500]
        return True, resp.json()
    except Exception as e:
        return False, str(e)


def check_splits_dividends_consistency(intraday_df, complete_dates, eod_data):
    """Compares each COMPLETE day's continuous-session close against the
    EOD (adjusted) series for the same date. Restricted to complete
    days and continuous-session bars only, so a thin/auction-contaminated
    day can't produce a false mismatch."""
    continuous = intraday_df[intraday_df["in_continuous_session"] &
                              intraday_df["sydney_date"].astype(str).isin(complete_dates)]
    if continuous.empty:
        return pd.DataFrame(), pd.DataFrame()
    daily_close = continuous.groupby("sydney_date")["close"].last().sort_index()
    intraday_return = daily_close.pct_change() * 100

    edf = pd.DataFrame(eod_data)
    edf["date"] = pd.to_datetime(edf["date"]).dt.date
    edf = edf.set_index("date").sort_index()
    eod_col = "adjusted_close" if "adjusted_close" in edf.columns else "close"
    eod_return = edf[eod_col].pct_change() * 100

    common = sorted(set(intraday_return.dropna().index) & set(eod_return.dropna().index))
    rows = [{"date": str(d), "intraday_return_pct": round(intraday_return.loc[d], 3),
             "eod_adjusted_return_pct": round(eod_return.loc[d], 3),
             "diff": round(intraday_return.loc[d] - eod_return.loc[d], 3)} for d in common]
    result_df = pd.DataFrame(rows)
    flagged = result_df[result_df["diff"].abs() > 1.0] if not result_df.empty else result_df
    return result_df, flagged


def full_ticker_report(ticker, recent_data, depth_data, eod_data):
    """Runs the complete rigor checklist for one ticker and returns a
    summary dict — this is what feeds the final safe/not-safe verdict."""
    summary = {"ticker": ticker}

    recent_df, err = to_sydney_and_classify(recent_data)
    if err:
        summary["error"] = err
        return summary
    recent_completeness, recent_complete_dates = day_completeness_report(recent_df)
    recent_outside = outside_session_summary(recent_df)
    recent_flags = flag_implausible_moves(recent_df, recent_complete_dates)

    depth_df, depth_err = to_sydney_and_classify(depth_data) if depth_data else (None, "no depth data")
    if depth_df is not None and not depth_err:
        depth_completeness, depth_complete_dates = day_completeness_report(depth_df)
        earliest_complete_date = min(depth_complete_dates) if depth_complete_dates else None
    else:
        depth_completeness, depth_complete_dates, earliest_complete_date = pd.DataFrame(), set(), None

    consistency_df, consistency_flagged = (pd.DataFrame(), pd.DataFrame())
    if eod_data:
        consistency_df, consistency_flagged = check_splits_dividends_consistency(recent_df, recent_complete_dates, eod_data)

    summary.update({
        "recent_completeness": recent_completeness,
        "recent_complete_dates": recent_complete_dates,
        "recent_outside_session": recent_outside,
        "recent_implausible_moves": recent_flags,
        "depth_completeness": depth_completeness,
        "earliest_complete_trading_day_in_depth_window": str(earliest_complete_date) if earliest_complete_date else "none found in window tested",
        "eod_consistency_full": consistency_df,
        "eod_consistency_flagged": consistency_flagged,
    })
    return summary


if st.button("Run full validation (starts with BHP.AU only)", type="primary", use_container_width=True):
    calls_used = 0
    recent_to = datetime.now(timezone.utc)
    recent_from = recent_to - timedelta(days=10)
    depth_from = datetime(2020, 10, 1, tzinfo=timezone.utc)
    depth_to = datetime(2020, 10, 20, tzinfo=timezone.utc)

    st.subheader("Canary: BHP.AU recent window")
    with st.spinner("Testing BHP.AU access..."):
        ok, result, status = fetch_intraday("BHP.AU", recent_from, recent_to)
    calls_used += 1
    if not ok:
        st.error(f"BHP.AU FAILED (HTTP {status}). Stopping to protect quota.")
        st.code(str(result))
        st.stop()
    st.success("BHP.AU access confirmed. Proceeding with full check on all four tickers.")

    all_summaries = {}
    for ticker in TICKERS:
        with st.spinner(f"Full check: {ticker}..."):
            ok_r, recent_data, _ = fetch_intraday(ticker, recent_from, recent_to) if ticker != "BHP.AU" else (True, result, status)
            calls_used += 1 if ticker != "BHP.AU" else 0
            ok_d, depth_data, _ = fetch_intraday(ticker, depth_from, depth_to)
            calls_used += 1
            from_str, to_str = recent_from.strftime("%Y-%m-%d"), recent_to.strftime("%Y-%m-%d")
            ok_e, eod_data = fetch_eod(ticker, from_str, to_str)
            calls_used += 1

        if not ok_r:
            st.error(f"{ticker}: recent-window fetch FAILED — skipping full check for this ticker.")
            continue

        summary = full_ticker_report(ticker, recent_data, depth_data if ok_d else None, eod_data if ok_e else None)
        all_summaries[ticker] = summary

        with st.expander(f"{ticker} — full detail", expanded=(ticker == "BHP.AU")):
            if "error" in summary:
                st.error(summary["error"])
                continue
            st.markdown("**Recent-window day completeness**")
            st.dataframe(summary["recent_completeness"], use_container_width=True, hide_index=True)
            st.markdown(f"**Bars outside the 10:00-16:00 continuous session** (auction/other): {summary['recent_outside_session']['n_outside_bars']}")
            if summary["recent_outside_session"]["n_outside_bars"] > 0:
                st.caption(f"Affected dates: {summary['recent_outside_session']['dates_affected']}, example times: {summary['recent_outside_session']['example_times']}")
            st.markdown(f"**Earliest complete trading day found in Oct 2020 depth window:** {summary['earliest_complete_trading_day_in_depth_window']}")
            st.markdown("**Implausible same-day/overnight moves (>{:.0f}%, complete days only)**".format(IMPLAUSIBLE_MOVE_PCT))
            if summary["recent_implausible_moves"].empty:
                st.write("None flagged in this window.")
            else:
                st.dataframe(summary["recent_implausible_moves"], use_container_width=True, hide_index=True)
            st.markdown("**Intraday vs EOD-adjusted return consistency (splits/dividends check)**")
            if summary["eod_consistency_flagged"].empty and not summary["eod_consistency_full"].empty:
                st.success(f"Matches closely on all {len(summary['eod_consistency_full'])} complete days checked.")
            elif not summary["eod_consistency_flagged"].empty:
                st.warning("Mismatch found:")
                st.dataframe(summary["eod_consistency_flagged"], use_container_width=True, hide_index=True)
            else:
                st.write("Not enough overlapping complete days to compare in this window.")

    st.divider()
    st.subheader("Final summary — safe-to-use verdict per ticker")
    st.caption(f"Total API calls used this run: {calls_used}")
    verdict_rows = []
    for ticker, s in all_summaries.items():
        if "error" in s:
            verdict_rows.append({"ticker": ticker, "verdict": "FAILED", "note": s["error"]})
            continue
        n_incomplete = len(s["recent_completeness"][s["recent_completeness"]["status"] != "complete"])
        n_flags = len(s["recent_implausible_moves"])
        n_mismatch = len(s["eod_consistency_flagged"])
        issues = []
        if n_incomplete > 0:
            issues.append(f"{n_incomplete} incomplete day(s) in recent window")
        if n_flags > 0:
            issues.append(f"{n_flags} implausible move(s) flagged")
        if n_mismatch > 0:
            issues.append(f"{n_mismatch} intraday/EOD mismatch(es)")
        verdict = "Usable, no issues found" if not issues else "Usable with caveats: " + "; ".join(issues)
        verdict_rows.append({"ticker": ticker, "verdict": verdict,
                            "earliest_complete_day_found": s["earliest_complete_trading_day_in_depth_window"]})
    st.dataframe(pd.DataFrame(verdict_rows), use_container_width=True, hide_index=True)
    st.caption(
        "Material limitation, stated plainly: the historical-depth check above only tested a 20-day window in "
        "October 2020, not the full 5.75-year history — it confirms data EXISTS at the claimed start, not that "
        "every day since then is complete. A full depth/completeness sweep across the entire history is a "
        "separate, larger check, not run here."
    )
else:
    st.info("Tap to run. Starts with a single BHP.AU call and stops immediately if that fails.")
