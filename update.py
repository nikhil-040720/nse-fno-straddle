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


def get_standard_monthly_expiries(trade_date, number=2):
    year = trade_date.year
    month = trade_date.month
    expiries = []

    for i in range(0, 8):
        total_month = month - 1 + i
        y = year + total_month // 12
        m = total_month % 12 + 1
        expiry = monthly_expiry(y, m)

        if expiry.date() >= trade_date:
            expiries.append(expiry)

        if len(expiries) >= number:
            break

    return expiries[:number]


def get_cycle_start_cutoff(active_expiry_date):
    """5 trading days before the previous monthly expiry."""
    prev_m = 12 if active_expiry_date.month == 1 else active_expiry_date.month - 1
    prev_y = active_expiry_date.year - 1 if active_expiry_date.month == 1 else active_expiry_date.year
    prev_expiry = monthly_expiry(prev_y, prev_m).date()

    cur = prev_expiry - timedelta(days=1)
    trading_days_counted = 0
    while trading_days_counted < 5:
        if is_trading_day(cur):
            trading_days_counted += 1
            if trading_days_counted == 5:
                break
        cur -= timedelta(days=1)
    return cur


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

SYMBOL_RENAMES = {
    "ICICIPRULI": "ICICILIFE",
}
def clean_data(df):
    df.columns = [str(c).strip() for c in df.columns]
    
    # Normalize ticker symbols & apply official NSE ticker migration
    df["TckrSymb"] = df["TckrSymb"].astype(str).str.strip().str.upper()
    df["TckrSymb"] = df["TckrSymb"].replace(SYMBOL_RENAMES)

    # Explicitly prioritize SttlmPric (SETTLE_PR), fallback to ClsPric
    settle = pd.to_numeric(df["SttlmPric"], errors="coerce") if "SttlmPric" in df.columns else pd.Series(index=df.index, dtype=float)
    close = pd.to_numeric(df["ClsPric"], errors="coerce") if "ClsPric" in df.columns else pd.Series(index=df.index, dtype=float)

    # Replace 0 or NaN with the fallback value
    settle = settle.replace(0, pd.NA)
    close = close.replace(0, pd.NA)

    # Primary price is Settlement; fallback is Close
    df["CleanPrice"] = settle.combine_first(close).fillna(0.0)

    
    df["FinInstrmTp"] = df["FinInstrmTp"].astype(str).str.strip().str.upper()
    df["OptnTp"] = df["OptnTp"].astype(str).str.strip().str.upper()
    df["XpryDt"] = pd.to_datetime(df["XpryDt"], errors="coerce")
    df["StrkPric"] = pd.to_numeric(df["StrkPric"], errors="coerce")
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

    # Futures contract matching target expiry
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

    # Use CleanPrice (Settlement Price) for the Futures reference
    future_ref_price = float(fut.iloc[0]["CleanPrice"])
    if future_ref_price == 0.0:
        return []

    strikes = sorted(opt_exp["StrkPric"].dropna().unique())
    if not strikes:
        return []

    # Strike closest to settlement future price
    atm_strike = min(strikes, key=lambda x: abs(x - future_ref_price))

    ce = opt_exp[(opt_exp["StrkPric"] == atm_strike) & (opt_exp["OptnTp"] == "CE")]
    pe = opt_exp[(opt_exp["StrkPric"] == atm_strike) & (opt_exp["OptnTp"] == "PE")]

    if ce.empty or pe.empty:
        return []

    # Use CleanPrice (Settlement Price) for both Call and Put
    ce_price = float(ce.iloc[0]["CleanPrice"])
    pe_price = float(pe.iloc[0]["CleanPrice"])
    straddle = ce_price + pe_price
    straddle_pct = (straddle / future_ref_price * 100) if future_ref_price else None

    return [{
        "Trade Date": trade_date.strftime("%Y-%m-%d"),
        "Symbol": symbol,
        "Expiry Bucket": "CURRENT",
        "Expiry": opt_expiry.strftime("%Y-%m-%d"),
        "Futures Close": future_ref_price,
        "ATM Strike": atm_strike,
        "CE Close": ce_price,
        "PE Close": pe_price,
        "Straddle": straddle,
        "Straddle %": straddle_pct,
    }]


def process_date(trade_date, target_expiries):
    print(f"Downloading NSE data for {trade_date:%Y-%m-%d}...")
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
    for target_expiry_ts in target_expiries:
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
    df = df.sort_values(["Trade Date", "Symbol", "Expiry"])
    df.to_csv(REPO_FILE, index=False)


def main():
    today = datetime.now().date()

    # Get active standard monthly expiries (Current and Next)
    expiries = get_standard_monthly_expiries(today, number=2)
    if not expiries:
        print("Could not find standard expiries.")
        return

    curr_exp_dt = expiries[0].date()
    next_exp_dt = expiries[1].date() if len(expiries) > 1 else None

    # Overlap trigger: 5 trading days before curr_exp_dt
    cur = curr_exp_dt - timedelta(days=1)
    trading_days_counted = 0
    while trading_days_counted < 5:
        if is_trading_day(cur):
            trading_days_counted += 1
            if trading_days_counted == 5:
                break
        cur -= timedelta(days=1)
    overlap_start_date = cur

    # Earliest date to track for current cycle
    earliest_cycle_start = get_cycle_start_cutoff(curr_exp_dt)

    print(f"Current Monthly Expiry: {curr_exp_dt}")
    if next_exp_dt:
        print(f"Next Monthly Expiry: {next_exp_dt}")
    print(f"Rollover Start Date (5 days before current expiry): {overlap_start_date}")

    all_dates = []
    curr = earliest_cycle_start
    while curr <= today:
        if is_trading_day(curr):
            all_dates.append(curr)
        curr += timedelta(days=1)

    all_rows = []
    for d in all_dates:
        target_expiries_for_date = []

        # 1. Fetch current monthly contract if trade date <= current expiry
        if d <= curr_exp_dt:
            target_expiries_for_date.append(expiries[0])

        # 2. Fetch next monthly contract if trade date >= overlap_start_date
        if next_exp_dt and d >= overlap_start_date:
            target_expiries_for_date.append(expiries[1])

        if target_expiries_for_date:
            day_rows = process_date(d, target_expiries_for_date)
            if day_rows:
                all_rows.extend(day_rows)

    if not all_rows:
        print("No data available.")
        return

    final_df = pd.DataFrame(all_rows)
    final_df = final_df.drop_duplicates(
        subset=["Trade Date", "Symbol", "Expiry"],
        keep="last",
    )

    save_cycle_data(final_df)
    print(f"Successfully processed {len(final_df)} rows across {final_df['Trade Date'].nunique()} trading dates.")


if __name__ == "__main__":
    main()