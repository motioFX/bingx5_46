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

## 3. Air Mode & Oracle No.1 Monitoring Operation
- **Air Mode Operation (オラクルNo.1 監視運用)**:
  - オラクルNo.1ボットは相場監視・シグナル通知に専念するため、実発注は行わずエアトレード（`BITBANK_IS_AIR = True`）として動作させる。
  - Live API（`BITBANK_IS_LIVE = True`）から本番レート・板情報・実残高を取得しつつ、発注処理はペーパートレードとして仮想執行。
  - ロングエントリー（LONG ENTRY）および利確・手仕舞い（LONG CLOSE）のタイミングで、Discordへ明瞭なシグナル通知および決済トレードチャートを送信する。
- **Cross-Exchange Portfolio Tracking (毎時ポジション監査)**:
  - `portfolio_tracker.py` と連携し、**Bitbank（現物 RENDER, BTC 等）** および **Binance Japan（現物 NEAR 等）** の購入済み暗号資産を毎時間サイクルごとに自動監査。
  - 保有数量、移動加重平均建値、現在市場価格、評価額、含み損益、未約定指値注文（拘束JPY）を、ボット起動時・定期選定時・毎時ループにてログおよびDiscordへ自動報告する。
- **Long-Term BTC Asset Protection**:
  - 口座内の長期保有現物BTC（0.1434 BTC等）は、ボットの売買から完全に隔離・保護する。

## 4. Execution Sequence & Periodic Timing Rule
- **Architecture Characteristic (1H Candle & REST API Only)**:
  - 5分足の監視ループや WebSocket 常時接続は使用せず、完全な **「1時間足（1h）確定足・HTTP REST API方式」** で動作する。
  - 毎時00分05秒に1回だけ数十秒稼働し、残りの時間はスリープ待機するため、常時接続切断トラブルがなくVPSのCPU負荷が極めて低く安定する。
- **Startup Sequence**:
  - 1. Bitbank ASCII Art banner & account settings display (Order Mode: "エアトレード").
  - 2. 120-day historical data sync (4ヶ月分 / 2分割ZIP) ＆ 30d/10d/5d normalized return charts sent to Discord (Bitbank 47銘柄 ＆ Binance Japan 27銘柄).
  - 3. Cross-exchange portfolio & active limit orders audit report (Bitbank RENDER/BTC & Binance Japan NEAR) to terminal & Discord.
  - 4. Enter 1H candle execution loop.
- **Periodic Screening (Every 8 Hours at 01:00, 09:00, 17:00 JST)**:
  - Re-run historical sync (Bitbank ＆ Binance Japan 4ヶ月分2分割ZIP), normalized charts, and cross-exchange portfolio audit.
  - **Do NOT show the ASCII Art banner during periodic screening cycles** (banner is startup-only).

## 5. Dual-Exchange 120-Day 2-Part ZIP & Rate Limit / CPU Safety
- **Bitbank 47 Symbols Sync (`download_historical_candles.py`)**:
  - 47 JPY pairs × 120 days -> Part 1 (past 60d) & Part 2 (recent 60d) ZIPs uploaded to Discord.
- **Binance Japan 27 Symbols Sync (`download_binance_candles.py`)**:
  - 27 JPY spot pairs × 120 days -> Part 1 (past 60d) & Part 2 (recent 60d) ZIPs uploaded to Discord.
- **Rate Limit & CPU Safety Guard**:
  - サーバー負荷およびVPSのCPUクレジット消費を徹底抑制するため、各銘柄間に `0.3秒`、ページネーション間に `0.05秒` のウェイトを挿入し、逐次（シングルスレッド）かつ低負荷ZIP圧縮で丁寧に取得する。

## 6. Data Retention & Disk Capacity Management Rule
- **Always keep only the latest data files and auto-cleanup old files.**
- Historical ZIP archives (`Data/bitbank_all_symbols_past_*.zip`, `recent_*.zip`, `Data/binance_japan_all_symbols_past_*.zip`, `recent_*.zip`) and plot charts (`Data/plots/*.png`) must be automatically pruned after 24 hours of retention (max 1 day) while keeping the latest 1 set safe.
- Single master CSVs (`historical_all_symbols_merged.csv`, `binance_japan_all_symbols_merged.csv`) and candle caches must always be cleanly updated in place without accumulating dated redundant CSV files.
- Execution logs (`bot_output.log`) must be rotated with a 15MB cap to permanently prevent VPS disk space exhaustion.

## 7. Cross-Platform Compatibility (Windows & Linux)
- Ensure all Python scripts run cleanly from Windows Command Prompt (`cmd.exe`) and PowerShell without unhandled exceptions, encoding crashes, or OS-specific path issues.
- All file IO operations must explicitly declare `encoding="utf-8"`.
- Use `pathlib.Path` for cross-platform path handling.
- External APIs and background tasks must be wrapped in fail-safe exception blocks (`except (Exception, BaseException):`) so that server downtimes (HTTP 503, timeouts) never terminate the main trading bot process.

## 8. Implementation Plan & Walkthrough Rule
- ALWAYS write the Implementation Plan and Walkthrough artifacts entirely in Japanese.
