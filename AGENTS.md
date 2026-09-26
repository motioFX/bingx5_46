# bitbank5_46 AI Agent Rules & Guidelines

## 1. Deployment & Code Synchronization Rule
- **Never directly overwrite/SCP code files (.py) from Windows to VPS.**
- Always follow the Git-based deployment workflow:
  1. Make and test code changes locally on Windows.
  2. Commit and push to GitHub (`git add`, `git commit`, `git push origin bitbank`).
  3. SSH into the VPS and pull the latest code (`cd /home/ubuntu/bitbank5_46 && git pull origin bitbank`).
  4. Safely verify/restart the bot process on the VPS (`bitbank5_46_1spot_limit.py --loop`).

## 2. Bitbank Spot Trading Rules (Spot / JPY / LONG ONLY)
- **Spot Trading Only (No Shorting / No Margin)**:
  - Bitbank is a spot exchange. Strategies must be **LONG ONLY** (Buy to enter, Sell to exit). No short selling.
  - Leverage is fixed at **1.0x** (`LEVERAGE_FACTOR = 1.0`). Edge (funding rate, interest) is not considered.
- **Maker Limit Orders & Quantization**:
  - Long entries must prioritize Maker limit orders at **`best_bid`**.
  - Unfilled orders must be safely cancelled before relocating or replacing.
  - Price and lot sizes must strictly conform to Bitbank asset specifications (`sz_decimals`, `price_place`).

## 3. Air Mode & Long-Term BTC Asset Protection Rule
- **Air Mode Operation**:
  - Since Bitbank does not provide a demo account, the bot connects to the live API (`BITBANK_IS_LIVE = True`) to fetch real market/orderbook data and balances, while routing execution through virtual simulation (`BITBANK_IS_AIR = True`, labeled as "エアトレード").
- **Long-Term BTC Asset Isolation**:
  - Existing long-term spot BTC holdings in the account (e.g. 0.1434 BTC) must be completely isolated and protected from bot trading/liquidation.
  - Display and notify the status (holding amount, average entry price, current price, valuation, profit/loss, total account equity) at startup and on every periodic screening cycle.
- **Active Orders Tracking**:
  - Active limit orders (e.g. RENDER build orders) must be tracked and reported at startup and on periodic cycles.

## 4. Execution Sequence & Periodic Timing Rule
- **Startup Sequence**:
  - 1. Bitbank ASCII Art banner & account settings display (Order Mode: "エアトレード").
  - 2. 120-day historical data sync for all 47 pairs & 2-part ZIP generation (`past`, `recent`) sent to Discord.
  - 3. Normalized return charts (30D, 10D, 5D) generated and sent to Discord.
  - 4. Long-term BTC status report to terminal & Discord.
  - 5. Active limit orders status report (RENDER etc.) to terminal & Discord.
  - 6. Enter 1H candle execution loop.
- **Periodic Screening (Every 8 Hours at 01:00, 09:00, 17:00 JST)**:
  - Re-run historical sync, 2-part ZIP upload, normalized charts, BTC status, and active orders report.
  - **Do NOT show the ASCII Art banner during periodic screening cycles** (banner is startup-only).

## 5. Data Retention & Disk Capacity Management Rule
- **Always keep only the latest data files and auto-cleanup old files.**
- Historical ZIP archives (`Data/bitbank_all_symbols_past_*.zip`, `recent_*.zip`) and plot charts (`Data/plots/*.png`) must be automatically pruned after 24 hours of retention (max 1 day) while keeping the latest 1 set safe.
- Single master CSV (`historical_all_symbols_merged.csv`) and candle caches (`historical_candles/*.csv`) must always be cleanly updated in place without accumulating dated redundant CSV files.
- Execution logs (`bot_output.log`) must be rotated with a 15MB cap to permanently prevent VPS disk space exhaustion.

## 6. Cross-Platform Compatibility (Windows & Linux)
- Ensure all Python scripts run cleanly from Windows Command Prompt (`cmd.exe`) and PowerShell without unhandled exceptions, encoding crashes, or OS-specific path issues.
- All file IO operations must explicitly declare `encoding="utf-8"`.
- Use `pathlib.Path` for cross-platform path handling.
- External APIs and background tasks must be wrapped in fail-safe exception blocks (`except (Exception, BaseException):`) so that server downtimes (HTTP 503, timeouts) never terminate the main trading bot process.

## 7. Implementation Plan & Walkthrough Rule
- ALWAYS write the Implementation Plan and Walkthrough artifacts entirely in Japanese.
