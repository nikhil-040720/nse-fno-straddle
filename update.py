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


def get_standard_monthly_expiries(trade_date, number=1):
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
    required = ["FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp", "ClsPric"]
    for col in required:
        if col not in df.columns:
            raise Exception(f"Required NSE column missing: {col}")

    df["TckrSymb"] = df["TckrSymb"].astype(str).str.strip().str.upper()
    df["FinInstrmTp"] = df["FinInstrmTp"].astype(str).str.strip().str.upper()
    df["OptnTp"] = df["OptnTp"].astype(str).str.strip().str.upper()
    df["XpryDt"] = pd.to_datetime(df["XpryDt"], errors="coerce")
    df["StrkPric"] = pd.to_numeric(df["StrkPric"], errors="coerce")
    df["ClsPric"] = pd.to_numeric(df["ClsPric"], errors="coerce")
    return df


def calculate_for_symbol(df, symbol, trade_date):
    sym = df[df["TckrSymb"] == symbol].copy()
    if sym.empty:
        return []

    futures = sym[sym["FinInstrmTp"].isin(["IDF", "STF"])].copy()
    options = sym[sym["FinInstrmTp"].isin(["IDO", "STO"])].copy()

    futures = futures[futures["ClsPric"].notna()]
    options = options[options["ClsPric"].notna()]

    if futures.empty or options.empty:
        return []

    standard_expiries = get_standard_monthly_expiries(trade_date, number=1)
    if not standard_expiries:
        return []

    expiry = standard_expiries[0]
    fut = futures[futures["XpryDt"] == expiry].copy()
    opt_exp = options[options["XpryDt"] == expiry].copy()

    if fut.empty or opt_exp.empty:
        return []

    future_close = float(fut.iloc[0]["ClsPric"])
    strikes = sorted(opt_exp["StrkPric"].dropna().unique())
    if not strikes:
        return []

    atm = min(strikes, key=lambda x: abs(x - future_close))
    ce = opt_exp[(opt_exp["StrkPric"] == atm) & (opt_exp["OptnTp"] == "CE")]
    pe = opt_exp[(opt_exp["StrkPric"] == atm) & (opt_exp["OptnTp"] == "PE")]

    if ce.empty or pe.empty:
        return []

    ce_close = float(ce.iloc[0]["ClsPric"])
    pe_close = float(pe.iloc[0]["ClsPric"])
    straddle = ce_close + pe_close
    straddle_pct = (straddle / future_close * 100) if future_close else None

    return [{
        "Trade Date": trade_date.strftime("%Y-%m-%d"),
        "Symbol": symbol,
        "Expiry Bucket": "CURRENT",
        "Expiry": expiry.strftime("%Y-%m-%d"),
        "Futures Close": future_close,
        "ATM Strike": atm,
        "CE Close": ce_close,
        "PE Close": pe_close,
        "Straddle": straddle,
        "Straddle %": straddle_pct,
    }]


def process_date(trade_date):
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
    for symbol in futures_symbols:
        if symbol in SYMBOLS_TO_EXCLUDE:
            continue
        results.extend(calculate_for_symbol(df, symbol, trade_date))

    for index_symbol in ["NIFTY", "BANKNIFTY"]:
        results.extend(calculate_for_symbol(df, index_symbol, trade_date))

    return results


def load_cycle_data(active_expiry_str, cycle_start_date_str):
    if not REPO_FILE.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(REPO_FILE)
        if df.empty or "Trade Date" not in df.columns:
            return pd.DataFrame()
        df["Trade Date"] = df["Trade Date"].astype(str)
        df["Expiry"] = df["Expiry"].astype(str)
        return df[
            (df["Expiry"] == active_expiry_str)
            & (df["Trade Date"] >= cycle_start_date_str)
        ]
    except Exception:
        return pd.DataFrame()


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

    # Identify the active expiry contract based on today
    expiries = get_standard_monthly_expiries(today, number=1)
    if not expiries:
        print("Could not find standard expiry.")
        return

    active_expiry_dt = expiries[0].date()
    active_expiry_str = active_expiry_dt.strftime("%Y-%m-%d")

    # Start cutoff: exactly 5 trading days prior to previous month expiry
    cycle_start_date = get_cycle_start_cutoff(active_expiry_dt)
    cycle_start_date_str = cycle_start_date.strftime("%Y-%m-%d")

    print(f"Active Expiry: {active_expiry_str}")
    print(f"Cycle Start Date (5 days before prev expiry): {cycle_start_date_str}")

    # Load existing records for this cycle
    existing_df = load_cycle_data(active_expiry_str, cycle_start_date_str)
    already_downloaded_dates = set()
    if not existing_df.empty:
        already_downloaded_dates = set(existing_df["Trade Date"].unique())

    # Build calendar list from cycle_start_date to today
    all_dates_in_cycle = []
    curr = cycle_start_date
    while curr <= today:
        if is_trading_day(curr):
            all_dates_in_cycle.append(curr)
        curr += timedelta(days=1)

    new_rows = []
    for d in all_dates_in_cycle:
        d_str = d.strftime("%Y-%m-%d")
        if d_str in already_downloaded_dates:
            continue

        day_rows = process_date(d)
        if day_rows:
            new_rows.extend(day_rows)

    if not new_rows and existing_df.empty:
        print("No data available.")
        return

    new_df = pd.DataFrame(new_rows)
    if existing_df.empty:
        final_df = new_df
    elif new_df.empty:
        final_df = existing_df
    else:
        final_df = pd.concat([existing_df, new_df], ignore_index=True)

    final_df = final_df.drop_duplicates(
        subset=["Trade Date", "Symbol"],
        keep="last",
    )

    save_cycle_data(final_df)
    print(f"Saved total {len(final_df)} rows across {final_df['Trade Date'].nunique()} trading dates.")


if __name__ == "__main__":
    main()