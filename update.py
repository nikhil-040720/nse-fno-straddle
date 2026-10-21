import calendar
from datetime import datetime, timedelta
import io
from pathlib import Path
import zipfile
import pandas as pd
import requests

# Save exclusively to current_month.csv
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

    while d.weekday() != 1:  # Move to Tuesday
        d -= timedelta(days=1)

    while not is_trading_day(d):
        d -= timedelta(days=1)

    return pd.Timestamp(d)


def get_standard_monthly_expiries(trade_date, number=3):
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
        session.get("https://www.nseindia.com/", timeout=20)
    except Exception:
        pass

    response = session.get(url, timeout=60)
    if response.status_code != 200 or not response.content.startswith(b"PK"):
        return None

    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        csv_files = [x for x in z.namelist() if x.lower().endswith(".csv")]
        if not csv_files:
            return None

        with z.open(csv_files[0]) as f:
            return pd.read_csv(f, low_memory=False)


def clean_data(df, trade_date):
    df.columns = [str(c).strip() for c in df.columns]

    required = [
        "FinInstrmTp",
        "TckrSymb",
        "XpryDt",
        "StrkPric",
        "OptnTp",
        "ClsPric",
    ]
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

    standard_expiries = get_standard_monthly_expiries(trade_date, 3)
    results = []

    buckets = [
        ("CURRENT", standard_expiries[0]),
        ("NEAR", standard_expiries[1]),
        ("FAR", standard_expiries[2]),
    ]

    for bucket, expiry in buckets:
        fut = futures[futures["XpryDt"] == expiry].copy()
        opt_exp = options[options["XpryDt"] == expiry].copy()

        if fut.empty or opt_exp.empty:
            continue

        future_close = float(fut.iloc[0]["ClsPric"])
        strikes = sorted(opt_exp["StrkPric"].dropna().unique())

        if not strikes:
            continue

        atm = min(strikes, key=lambda x: abs(x - future_close))

        ce = opt_exp[(opt_exp["StrkPric"] == atm) & (opt_exp["OptnTp"] == "CE")]
        pe = opt_exp[(opt_exp["StrkPric"] == atm) & (opt_exp["OptnTp"] == "PE")]

        if ce.empty or pe.empty:
            continue

        ce_close = float(ce.iloc[0]["ClsPric"])
        pe_close = float(pe.iloc[0]["ClsPric"])
        straddle = ce_close + pe_close
        straddle_pct = (straddle / future_close * 100) if future_close else None

        results.append({
            "Trade Date": trade_date.strftime("%Y-%m-%d"),
            "Symbol": symbol,
            "Expiry Bucket": bucket,
            "Expiry": expiry.strftime("%Y-%m-%d"),
            "Futures Close": future_close,
            "ATM Strike": atm,
            "CE Close": ce_close,
            "PE Close": pe_close,
            "Straddle": straddle,
            "Straddle %": straddle_pct,
        })

    return results


def process_date(trade_date):
    print(f"Downloading NSE data for {trade_date:%Y-%m-%d}")
    df = download_nse(trade_date)
    if df is None:
        print(f"No NSE file available for {trade_date:%Y-%m-%d}")
        return []

    df = clean_data(df, trade_date)

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
        rows = calculate_for_symbol(df, symbol, trade_date)
        results.extend(rows)

    for index_symbol in ["NIFTY", "BANKNIFTY"]:
        rows = calculate_for_symbol(df, index_symbol, trade_date)
        results.extend(rows)

    return results


def load_current_month_data(active_month_str):
    if not REPO_FILE.exists():
        return pd.DataFrame()

    try:
        df = pd.read_csv(REPO_FILE)
        if df.empty or "Trade Date" not in df.columns:
            return pd.DataFrame()

        # Only retain entries matching the active month (YYYY-MM)
        df["Trade Date"] = df["Trade Date"].astype(str)
        df = df[df["Trade Date"].str.startswith(active_month_str)]
        return df
    except Exception:
        return pd.DataFrame()


def save_current_month_data(df):
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
    df = df.sort_values(["Trade Date", "Symbol", "Expiry Bucket"])
    df.to_csv(REPO_FILE, index=False)


def main():
    today = datetime.now().date()

    dates_to_try = [
        today,
        today - timedelta(days=1),
        today - timedelta(days=2),
        today - timedelta(days=3),
        today - timedelta(days=4),
    ]

    new_rows = []
    actual_trade_date = None

    for d in dates_to_try:
        rows = process_date(d)
        if rows:
            new_rows.extend(rows)
            actual_trade_date = d
            break

    if not new_rows or actual_trade_date is None:
        print("No data found.")
        return

    # Check active month of the fetched trade date
    active_month_str = actual_trade_date.strftime("%Y-%m")
    new_df = pd.DataFrame(new_rows)

    # Load existing data, keeping ONLY the current month
    old_df = load_current_month_data(active_month_str)

    if old_df.empty:
        final_df = new_df
    else:
        final_df = pd.concat([old_df, new_df], ignore_index=True)
        final_df = final_df.drop_duplicates(
            subset=["Trade Date", "Symbol", "Expiry Bucket"],
            keep="last",
        )

    save_current_month_data(final_df)
    print(f"Saved {len(new_df)} rows for {actual_trade_date}.")
    print(f"Total rows in {REPO_FILE.name}: {len(final_df)}")


if __name__ == "__main__":
    main()