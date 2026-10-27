<div align="center">

# ⚡ NSE F&O ATM Straddle Engine

**Automated End-of-Day (EOD) ATM Straddle Ingestion, Multi-Month Transition & Volatility Analytics Pipeline**

[![NSE Bhavcopy Ingestion](https://img.shields.io/badge/Data-NSE%20Official%20Bhavcopy-blue?style=for-the-badge&logo=databricks)](https://www.nseindia.com/)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![GitHub Actions](https://img.shields.io/badge/Automation-GitHub%20Actions-2088FF?style=for-the-badge&logo=github-actions&logoColor=white)](https://github.com/features/actions)
[![Live Google Sheet](https://img.shields.io/badge/Live%20Dashboard-STD%20Sheet-34A853?style=for-the-badge&logo=googlesheets&logoColor=white)](https://docs.google.com/spreadsheets/d/1mSSFov0MsGOTHdd2fWaTVVWcglkluV5qd91XTtzTk3g/edit)

---

<p align="center">
  A quantitative pipeline designed to compute settlement-based At-The-Money (ATM) straddle pricing across the entire NSE F&O universe. Features dual-contract rollover tracking, dynamic ticker harmonization, and an interactive Google Sheets dashboard with momentum streak analytics.
</p>

[📊 **Open Live Straddle Sheet**](https://docs.google.com/spreadsheets/d/1mSSFov0MsGOTHdd2fWaTVVWcglkluV5qd91XTtzTk3g/edit)

</div>

---

## 📌 Architecture Overview

```mermaid
flowchart LR
    A["NSE Bhavcopy ZIP"] -->|Fetch EOD Archives| B("update.py Engine")
    B -->|Calculate ATM Straddles| C["current_month.csv"]
    C -->|Commit & Push| D["GitHub Repository"]
    D -->|Daily Trigger / 8:30 PM IST| E["Google Apps Script"]
    E -->|Dual-Routing Engine| F["STD Sheet Dashboard"]
    F -->|Raw Records| G[("Data Hub (data)")]
    F -->|Expiring Cycle| H["OCT 26 Archive Tab"]
    F -->|Upcoming Cycle| I["STRADDLE Live Tab"]
