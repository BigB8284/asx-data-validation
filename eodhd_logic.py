"""Pure logic functions for the EODHD validator, split out so they can
be unit tested with synthetic data before going anywhere near the
live API or Streamlit."""

import pandas as pd
from zoneinfo import ZoneInfo

SYDNEY = ZoneInfo("Australia/Sydney")
SESSION_START = pd.Timestamp("10:00:00").time()
SESSION_END = pd.Timestamp("16:00:00").time()
MIN_CONTINUOUS_BARS_FOR_COMPLETE_DAY = 60  # expected ~72 for a full 10:00-16:00 session; buffer allows for a legitimate early close without over-flagging
IMPLAUSIBLE_MOVE_PCT = 15.0  # a same-day range or overnight gap bigger than this gets flagged for manual corporate-action check


def to_sydney_and_classify(data):
    """Converts raw API bars to real Australia/Sydney local time using
    actual historical DST rules (zoneinfo), and classifies each bar as
    inside or outside the normal 10:00-16:00 continuous session. This
    replaces any fixed UTC offset guess."""
    df = pd.DataFrame(data)
    if "timestamp" in df.columns:
        df["utc_dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    elif "datetime" in df.columns:
        df["utc_dt"] = pd.to_datetime(df["datetime"], utc=True)
    else:
        return None, f"Unrecognized response columns: {list(df.columns)}"

    df["sydney_dt"] = df["utc_dt"].dt.tz_convert(SYDNEY)
    df["sydney_date"] = df["sydney_dt"].dt.date
    df["sydney_time"] = df["sydney_dt"].dt.time
    df["in_continuous_session"] = df["sydney_time"].apply(lambda t: SESSION_START <= t < SESSION_END)
    return df, None


def day_completeness_report(df):
    """Per calendar day (Sydney local date): how many CONTINUOUS-session
    bars were found, and whether that day should be treated as complete
    or excluded. Days below the threshold are flagged, not silently
    included in any downstream calculation."""
    continuous = df[df["in_continuous_session"]]
    per_day = continuous.groupby("sydney_date").size()
    rows = []
    for date, n in per_day.items():
        rows.append({"date": str(date), "n_continuous_bars": int(n),
                     "status": "complete" if n >= MIN_CONTINUOUS_BARS_FOR_COMPLETE_DAY else "INCOMPLETE - excluded"})
    report = pd.DataFrame(rows).sort_values("date")
    complete_dates = set(report[report["status"] == "complete"]["date"])
    return report, complete_dates


def outside_session_summary(df):
    """How many bars fall outside the normal continuous session (pre-open
    auction, closing auction, or anything else) — surfaced explicitly
    rather than silently mixed into 'the day's data'."""
    outside = df[~df["in_continuous_session"]]
    if outside.empty:
        return {"n_outside_bars": 0, "dates_affected": []}
    per_day = outside.groupby("sydney_date").size()
    return {
        "n_outside_bars": len(outside),
        "dates_affected": [str(d) for d in per_day.index],
        "example_times": sorted(set(str(t) for t in outside["sydney_time"].head(10))),
    }


def flag_implausible_moves(df, complete_dates):
    """Using ONLY complete, continuous-session bars: flags any day whose
    intraday high/low range, or overnight gap from the prior complete
    day's last continuous close, exceeds a sanity threshold. This is a
    manual-check flag, not a definitive split/dividend diagnosis — real
    corporate actions, real volatility, and data artifacts can all
    produce a flag here, so each one needs a look, not an assumption."""
    continuous = df[df["in_continuous_session"] & df["sydney_date"].astype(str).isin(complete_dates)].copy()
    if continuous.empty:
        return pd.DataFrame()
    daily = continuous.groupby("sydney_date").agg(
        day_high=("high", "max"), day_low=("low", "min"),
        day_open=("open", "first"), day_close=("close", "last"),
    ).sort_index()
    daily["intraday_range_pct"] = (daily["day_high"] - daily["day_low"]) / daily["day_low"] * 100
    daily["prior_close"] = daily["day_close"].shift(1)
    daily["overnight_gap_pct"] = (daily["day_open"] - daily["prior_close"]) / daily["prior_close"] * 100

    flagged = daily[(daily["intraday_range_pct"] > IMPLAUSIBLE_MOVE_PCT) |
                     (daily["overnight_gap_pct"].abs() > IMPLAUSIBLE_MOVE_PCT)]
    return flagged.reset_index().rename(columns={"sydney_date": "date"})
