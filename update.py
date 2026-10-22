import calendar
from datetime import datetime, timedelta
import io
from pathlib import Path
import zipfile
import pandas as pd
import requests

REPO_FILE = Path("current_month.csv")

NSE_URL = (
    "https://nsearchives.nseindia.com/content/fo/"
    "BhavCopy_NSE_FO_0_0_0_{date}_F_0000.csv.zip"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/140 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}

SYMBOLS_TO_EXCLUDE = {
    "FINNIFTY",
    "MIDCPNIFTY",
    "NIFTYNXT50",
}

# NSE F&O holidays for 2026
NSE_HOLIDAYS_2026 = {
    "2026-01-26",
    "2026-03-03",
    "2026-03-26",
    "2026-03-31",
    "2026-04-03",
    "2026-04-14",
    "2026-05-01",
    "2026-05-28",
    "2026-06-26",
    "2026-09-14",
    "2026-10-02",
    "2026-10-20",
    "2026-11-10",
    "2026-11-24",
    "2026-12-25",
}


def is_trading_day(d):
    if d.weekday() >= 5:
        return False
    if d.strftime("%Y-%m-%d") in NSE_HOLIDAYS_2026:
        return False
    return True


def get_weekly_expiry(trade_date):
    days_until_tuesday = (1 - trade_date.weekday()) % 7
    tuesday = trade_date + timedelta(days=days_until_tuesday)

    d = tuesday
    while not is_trading_day(d):
        d -= timedelta(days=1)

    if d < trade_date:
        next_tuesday = tuesday + timedelta(days=7)
        d = next_tuesday
        while not is_trading_day(d):
            d -= timedelta(days=1)

    return pd.Timestamp(d)


def monthly_expiry(year, month):
    last_day = calendar.monthrange(year, month)[1]
    d = datetime(year, month, last_day).date()

    while d.weekday() != 1:  # Tuesday
        d -= timedelta(days=1)

    while not is_trading_day(d):
        d -= timedelta(days=1)

    return pd.Timestamp(d)


def get_previous_expiry(year, month):
    prev_m = 12 if month == 1 else month - 1
    prev_y = year - 1 if month == 1 else year
    return monthly_expiry(prev_y, prev_m)


def get_cycle_start_cutoff(active_expiry_date):
    """
    Finds the previous month's expiry and rolls backward
    5 trading days before it to find the start of the tracking cycle.
    """
    prev_expiry = get_previous_expiry(active_expiry_date.year, active_expiry_date.month).date()
    cur = prev_expiry - timedelta(days=1)
    trading_days_counted = 0
    while trading_days_counted < 5:
        if is_trading_day(cur):
            trading_days_counted += 1
            if trading_days_counted == 5:
                break
        cur -= timedelta(days=1)
    return cur


def get_target_cycle_expiry(today):
    """
    Determines if we should track current month or roll to NEXT month.
    If today is within 5 trading days before current monthly expiry (or past it),
    we roll forward to track the NEXT month's expiry (November).
    """
    expiries = []
    year = today.year
    month = today.month

    for i in range(0, 8):
        total_month = month - 1 + i
        y = year + total_month // 12
        m = total_month % 12 + 1
        exp = monthly_expiry(y, m)
        if exp.date() >= today:
            expiries.append(exp)
        if len(expiries) >= 2:
            break

    curr_exp = expiries[0].date()
    next_exp = expiries[1] if len(expiries) > 1 else expiries[0]

    # Check cutoff (5 trading days before curr_exp)
    cur = curr_exp - timedelta(days=1)
    trading_days_counted = 0
    while trading_days_counted < 5:
        if is_trading_day(cur):
            trading_days_counted += 1
            if trading_days_counted == 5:
                break
        cur -= timedelta(days=1)

    cutoff_for_next_cycle = cur

    # If today has reached 5 trading days before October expiry, switch to November!
    if today >= cutoff_for_next_cycle:
        return next_exp  # November Expiry
    return expiries[0]   # October Expiry


def download_nse(trade_date):
    date_text = trade_date.strftime("%Y%m%d")
    url = NSE_URL.format(date=date_text)

    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        session.get("https://www.nseindia.com/", timeout=15)
    except Exception:
        pass

    response = session.get(url, timeout=30)
    if response.status_code != 200 or not response.content.startswith(b"PK"):
        return None

    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        csv_files = [x for x in z.namelist() if x.lower().endswith(".csv")]
        if not csv_files:
            return None
        with z.open(csv_files[0]) as f:
            return pd.read_csv(f, low_memory=False)


def clean_data(df):
    df.columns = [str(c).strip() for c in df.columns]

    if "ClsPric" in df.columns:
        df["OptionPrice"] = pd.to_numeric(df["ClsPric"], errors="coerce")
    elif "SttlmPric" in df.columns:
        df["OptionPrice"] = pd.to_numeric(df["SttlmPric"], errors="coerce")
    else:
        df["OptionPrice"] = 0.0

    if "SttlmPric" in df.columns:
        df["OptionPrice"] = df["OptionPrice"].replace(0, pd.NA).fillna(pd.to_numeric(df["SttlmPric"], errors="coerce"))

    df["TckrSymb"] = df["TckrSymb"].astype(str).str.strip().str.upper()
    df["FinInstrmTp"] = df["FinInstrmTp"].astype(str).str.strip().str.upper()
    df["OptnTp"] = df["OptnTp"].astype(str).str.strip().str.upper()
    df["XpryDt"] = pd.to_datetime(df["XpryDt"], errors="coerce")
    df["StrkPric"] = pd.to_numeric(df["StrkPric"], errors="coerce")
    df["ClsPric"] = pd.to_numeric(df["ClsPric"], errors="coerce")
    return df


def calculate_for_symbol(df, symbol, trade_date, target_expiry_ts):
    sym = df[df["TckrSymb"] == symbol].copy()
    if sym.empty:
        return []

    futures = sym[sym["FinInstrmTp"].isin(["IDF", "STF"])].copy()
    options = sym[sym["FinInstrmTp"].isin(["IDO", "STO"])].copy()

    if futures.empty or options.empty:
        return []

    if symbol == "NIFTY":
        opt_expiry = get_weekly_expiry(trade_date)
    else:
        opt_expiry = target_expiry_ts

    fut = futures[futures["XpryDt"] == target_expiry_ts].copy()
    if fut.empty:
        fut = futures.sort_values("XpryDt").head(1)

    opt_exp = options[options["XpryDt"] == opt_expiry].copy()
    if opt_exp.empty:
        available = options[options["XpryDt"] >= pd.Timestamp(trade_date)]["XpryDt"].sort_values()
        if not available.empty:
            opt_exp = options[options["XpryDt"] == available.iloc[0]].copy()
            opt_expiry = available.iloc[0]
        else:
            return []

    if fut.empty or opt_exp.empty:
        return []

    future_close = float(fut.iloc[0]["ClsPric"])
    strikes = sorted(opt_exp["StrkPric"].dropna().unique())
    if not strikes:
        return []

    atm_strike = min(strikes, key=lambda x: abs(x - future_close))

    ce = opt_exp[(opt_exp["StrkPric"] == atm_strike) & (opt_exp["OptnTp"] == "CE")]
    pe = opt_exp[(opt_exp["StrkPric"] == atm_strike) & (opt_exp["OptnTp"] == "PE")]

    if ce.empty or pe.empty:
        return []

    ce_price = float(ce.iloc[0]["OptionPrice"])
    pe_price = float(pe.iloc[0]["OptionPrice"])
    straddle = ce_price + pe_price
    straddle_pct = (straddle / future_close * 100) if future_close else None

    return [{
        "Trade Date": trade_date.strftime("%Y-%m-%d"),
        "Symbol": symbol,
        "Expiry Bucket": "CURRENT",
        "Expiry": opt_expiry.strftime("%Y-%m-%d"),
        "Futures Close": future_close,
        "ATM Strike": atm_strike,
        "CE Close": ce_price,
        "PE Close": pe_price,
        "Straddle": straddle,
        "Straddle %": straddle_pct,
    }]


def process_date(trade_date, target_expiry_ts):
    print(f"Downloading NSE data for {trade_date:%Y-%m-%d} (Expiry: {target_expiry_ts:%Y-%m-%d})...")
    df = download_nse(trade_date)
    if df is None:
        print(f"No file available for {trade_date:%Y-%m-%d}")
        return []

    df = clean_data(df)
    futures_symbols = sorted(
        df.loc[df["FinInstrmTp"].isin(["IDF", "STF"]), "TckrSymb"]
        .dropna()
        .unique()
        .tolist()
    )

    results = []
    for symbol in futures_symbols:
        if symbol in SYMBOLS_TO_EXCLUDE:
            continue
        results.extend(calculate_for_symbol(df, symbol, trade_date, target_expiry_ts))

    for index_symbol in ["NIFTY", "BANKNIFTY"]:
        results.extend(calculate_for_symbol(df, index_symbol, trade_date, target_expiry_ts))

    return results


def save_cycle_data(df):
    columns = [
        "Trade Date",
        "Symbol",
        "Expiry Bucket",
        "Expiry",
        "Futures Close",
        "ATM Strike",
        "CE Close",
        "PE Close",
        "Straddle",
        "Straddle %",
    ]
    df = df[columns].copy()
    df = df.sort_values(["Trade Date", "Symbol"])
    df.to_csv(REPO_FILE, index=False)


def main():
    today = datetime.now().date()

    # Automatically switches to November contract once within 5 trading days of Oct expiry
    target_expiry_ts = get_target_cycle_expiry(today)
    target_expiry_dt = target_expiry_ts.date()

    # Start date = 5 trading days before previous expiry (October 27)
    cycle_start_date = get_cycle_start_cutoff(target_expiry_dt)

    print(f"Target Active Expiry: {target_expiry_dt}")
    print(f"Cycle Start Date: {cycle_start_date}")

    all_dates_in_cycle = []
    curr = cycle_start_date
    while curr <= today:
        if is_trading_day(curr):
            all_dates_in_cycle.append(curr)
        curr += timedelta(days=1)

    all_cycle_rows = []
    for d in all_dates_in_cycle:
        day_rows = process_date(d, target_expiry_ts)
        if day_rows:
            all_cycle_rows.extend(day_rows)

    if not all_cycle_rows:
        print("No data available.")
        return

    final_df = pd.DataFrame(all_cycle_rows)
    final_df = final_df.drop_duplicates(
        subset=["Trade Date", "Symbol"],
        keep="last",
    )

    save_cycle_data(final_df)
    print(f"Successfully processed {len(final_df)} rows for {target_expiry_dt} cycle across {final_df['Trade Date'].nunique()} trading dates.")


if __name__ == "__main__":
    main()