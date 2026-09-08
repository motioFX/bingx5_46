from __future__ import annotations
"""Rebuild merged hourly datasets for top-volume BingX symbols.
Generates selection scores, normalized charts, and merged CSVs.
"""
import argparse
import asyncio
import math
import shutil
import sys
import time
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
import json
import subprocess

import matplotlib
import numpy as np
import pandas as pd
import pybotters
import requests
from config_loader import get_webhook_url

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

matplotlib.use("Agg")
import matplotlib.pyplot as plt

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

REST_API_URL = {
    "bingx": "https://open-api.bingx.com",
    "bingx_demo": "https://open-api-vst.bingx.com",
    "coinbase": "https://api.exchange.coinbase.com",
}

JST = timezone(timedelta(hours=9))
_now_jst = datetime.now(JST).replace(minute=0, second=0, microsecond=0)
DEFAULT_START_JST = _now_jst - timedelta(days=30)
DEFAULT_START_STR = DEFAULT_START_JST.isoformat()

GRANULARITY_MS = {
    "1H": 60 * 60 * 1000,
    "2H": 2 * 60 * 60 * 1000,
    "4H": 4 * 60 * 60 * 1000,
    "6H": 6 * 60 * 60 * 1000,
    "12H": 12 * 60 * 60 * 1000,
    "1D": 24 * 60 * 60 * 1000,
}

NormalizedWindow = Tuple[str, Optional[timedelta], Optional[int]]

NORMALIZED_WINDOWS: Sequence[NormalizedWindow] = (
    ("30d", timedelta(days=30), 30 * 24),
    ("10d", timedelta(days=10), 10 * 24),
    ("5d", timedelta(days=5), 5 * 24),
)

DEFAULT_PRODUCT_TYPE = "PERP"


class send_discord:
    def __init__(self) -> None:
        self.real3_webhook = get_webhook_url("real3_bngx")
        self.test4_webhook = get_webhook_url("test4_backtest")
        self.bingx_webhook = self.real3_webhook
        self.win32_webhook = self.test4_webhook
        self.default_webhook = self.real3_webhook
        self.webhook_url = self.real3_webhook or self.test4_webhook

    def _get_target_webhooks(self, text: str = "") -> list[str]:
        # すべての通知を real3_bngx に集約
        target = self.real3_webhook or self.test4_webhook
        return [target] if target else []

    def send_message(self, content: str) -> None:
        webhooks = self._get_target_webhooks(content)
        for url in webhooks:
            try:
                requests.post(url, json={"content": content}, timeout=10).raise_for_status()
            except requests.RequestException:
                pass

    def send_file(self, file_path: Path, description: str = "") -> None:
        webhooks = self._get_target_webhooks(description)
        for url in webhooks:
            try:
                if not file_path.exists():
                    continue
                with file_path.open("rb") as fh:
                    files = {"file": (file_path.name, fh)}
                    data = {"content": description} if description else {}
                    requests.post(url, data=data, files=files, timeout=30).raise_for_status()
            except requests.RequestException as e:
                print(f"[Discord Error] Failed to send file {file_path.name}: {e}")


def to_ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def from_ms_jst(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(JST)


def log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    msg_str = f"[{stamp}] {message}"
    try:
        print(msg_str)
    except Exception:
        sys.stdout.buffer.write((msg_str + "\n").encode("utf-8", errors="replace"))
        sys.stdout.flush()


def log_fetch_result(label: str, rows: Sequence[Any]) -> None:
    count = len(rows) if hasattr(rows, "__len__") else 0
    if count == 0:
        log(f"{label}: no data returned (skip)")
    else:
        log(f"{label}: {count} rows")


def ensure_columns(df: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    for col in columns:
        if col not in df.columns:
            df[col] = np.nan
    return df


def normalize_symbol(symbol: str) -> str:
    sym = str(symbol).strip().upper()
    if "_" in sym:
        sym = sym.replace("_", "-")
    if sym.endswith("-USDT"):
        return sym
    if sym.endswith("USDT") and "-" not in sym:
        return f"{sym[:-4]}-USDT"
    if sym.endswith("-USDC"):
        return sym
    if sym.endswith("USDC") and "-" not in sym:
        return f"{sym[:-4]}-USDC"
    if "-" not in sym:
        return f"{sym}-USDT"
    return sym


def fetch_bingx_tickers(product_type: str = "SWAP", mode: str = "demo", max_retries: int = 5) -> List[dict]:
    base_url = REST_API_URL["bingx_demo"] if mode in ("paper", "demo", "testnet") else REST_API_URL["bingx"]
    url = f"{base_url}/openApi/swap/v2/quote/ticker"
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code != 200:
                time.sleep(1.0)
                continue
            data = resp.json()
            if data.get("code") == 0:
                ticker_list = data.get("data", [])
                result = []
                for item in ticker_list:
                    sym = normalize_symbol(item.get("symbol", ""))
                    if not sym.endswith("-USDT"):
                        continue
                    last_pr = float(item.get("lastPrice") or 0.0)
                    open_pr = float(item.get("openPrice") or 0.0)
                    vol = float(item.get("quoteVolume") or 0.0)
                    change_24h_pct = float(item.get("priceChangePercent") or 0.0)
                    
                    # クジラ大口流入
                    whale_file = Path(__file__).resolve().parent / "Data" / "whale_market_state.json"
                    whale_net_usd = 0.0
                    whale_long_r = 0.5
                    if whale_file.exists():
                        try:
                            with open(whale_file, "r", encoding="utf-8") as wf:
                                w_data = json.load(wf)
                                coins_sent = w_data.get("coins_sentiment", {})
                                raw_coin = sym.replace("-USDT", "")
                                if raw_coin in coins_sent:
                                    whale_net_usd = float(coins_sent[raw_coin].get("net_val_usd", 0.0))
                                    whale_long_r = float(coins_sent[raw_coin].get("long_ratio", 0.5))
                        except Exception:
                            pass

                    whale_impact_ratio = (whale_net_usd / vol * 100.0) if vol > 0 else 0.0

                    result.append({
                        "symbol": sym,
                        "lastPr": last_pr,
                        "prevDayPx": open_pr,
                        "change_24h_pct": change_24h_pct,
                        "usdtVolume": vol,
                        "openInterestCoins": 0.0,
                        "openInterestVal": 0.0,
                        "funding": 0.0,
                        "funding_rate_pct": 0.0,
                        "funding_annual_pct": 0.0,
                        "premium": 0.0,
                        "whale_net_val_usd": whale_net_usd,
                        "whale_long_ratio": whale_long_r,
                        "whale_impact_ratio": whale_impact_ratio,
                    })
                return result
        except Exception as e:
            log(f"Failed to fetch BingX tickers (attempt {attempt+1}): {e}")
            time.sleep(2.0)
    return []


def fetch_demo_available_symbols(product_type: str = "SWAP") -> set:
    tickers = fetch_bingx_tickers()
    symbols = {t["symbol"] for t in tickers if t.get("symbol")}
    return symbols if symbols else {"BTC-USDT", "ETH-USDT", "SOL-USDT"}


async def fetch_bingx_ohlcv(
    symbol: str,
    product_type: str,
    granularity: str,
    start_ms: int,
    end_ms: int,
    mode: str = "demo",
) -> List[List[Any]]:
    base_url = REST_API_URL["bingx_demo"] if mode in ("paper", "demo", "testnet") else REST_API_URL["bingx"]
    url = f"{base_url}/openApi/swap/v2/quote/klines"
    interval_map = {"1H": "1h", "2H": "2h", "4H": "4h", "1D": "1d"}
    bx_interval = interval_map.get(granularity, "1h")
    clean_sym = normalize_symbol(symbol)
    
    rows = []
    max_retries = 3
    curr_start = start_ms
    step_ms = 1400 * 3600 * 1000  # 1400 hours (~58 days per page)
    
    import aiohttp
    async with aiohttp.ClientSession() as session:
        while curr_start < end_ms:
            curr_end = min(curr_start + step_ms, end_ms)
            params = {
                "symbol": clean_sym,
                "interval": bx_interval,
                "startTime": str(curr_start),
                "endTime": str(curr_end),
                "limit": "1440"
            }
            success = False
            for attempt in range(max_retries):
                try:
                    async with session.get(url, params=params, timeout=15) as res:
                        if res.status == 200:
                            data = await res.json()
                            if data.get("code") == 0:
                                items = data.get("data", [])
                                if isinstance(items, list) and len(items) > 0:
                                    for item in items:
                                        t = int(item.get("time") or 0)
                                        o = float(item.get("open") or 0.0)
                                        h = float(item.get("high") or 0.0)
                                        l = float(item.get("low") or 0.0)
                                        c = float(item.get("close") or 0.0)
                                        v = float(item.get("volume") or 0.0)
                                        rows.append([t, o, h, l, c, v, v * c])
                                    success = True
                                    last_t = int(items[-1].get("time") or 0)
                                    curr_start = last_t + 1
                                    break
                                else:
                                    success = True
                                    curr_start = curr_end + 1
                                    break
                except Exception as e:
                    if attempt == max_retries - 1:
                        log(f"BingX OHLCV fetch error for {clean_sym} (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1 and not success:
                    await asyncio.sleep(1.0)
            if not success or curr_start >= curr_end:
                curr_start = curr_end + 1

    seen = set()
    unique_rows = []
    for r in rows:
        if r[0] not in seen:
            seen.add(r[0])
            unique_rows.append(r)
    unique_rows.sort(key=lambda x: x[0])
    return unique_rows


async def fetch_coinbase_candles_1h(start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
    url = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
    rows: List[Dict[str, Any]] = []
    step_ms = 300 * 60 * 60 * 1000
    cur_end = end_ms
    headers = {"User-Agent": "Mozilla/5.0"}
    async with pybotters.Client(headers=headers) as client:
        while cur_end > start_ms:
            cur_start = max(cur_end - step_ms, start_ms)
            start_iso = datetime.fromtimestamp(cur_start / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            end_iso = datetime.fromtimestamp(cur_end / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            params = {
                "granularity": "3600",
                "start": start_iso,
                "end": end_iso
            }
            max_retries = 3
            success = False
            for attempt in range(max_retries):
                try:
                    resp = await client.get(url, params=params)
                    data = await resp.json()
                    if isinstance(data, list):
                        success = True
                        break
                    await asyncio.sleep(1.0)
                except Exception:
                    await asyncio.sleep(1.0)
            
            if not success:
                cur_end = cur_start - 1
                continue
                
            for it in data:
                ts_sec = int(it[0])
                rows.append({
                    "timestamp": from_ms_jst(ts_sec * 1000),
                    "coinbase_close": float(it[4])
                })
            cur_end = cur_start - 1
            await asyncio.sleep(0.1)
    rows.sort(key=lambda d: d["timestamp"])
    return rows


async def fetch_bingx_funding_history(
    symbol: str,
    start_ms: int,
    end_ms: int,
    mode: str = "demo",
) -> List[Dict[str, Any]]:
    base_url = REST_API_URL["bingx_demo"] if mode in ("paper", "demo", "testnet") else REST_API_URL["bingx"]
    url = f"{base_url}/openApi/swap/v2/quote/fundingRate"
    clean_sym = normalize_symbol(symbol)
    results = []
    try:
        resp = requests.get(f"{url}?symbol={clean_sym}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == 0:
                fr_info = data.get("data", {})
                if isinstance(fr_info, dict):
                    fr = float(fr_info.get("fundingRate") or 0.0)
                    t_ms = int(fr_info.get("fundingTime") or time.time() * 1000)
                    dt_jst = from_ms_jst(t_ms).replace(minute=0, second=0, microsecond=0)
                    results.append({
                        "timestamp": dt_jst,
                        "fundingRate": fr,
                        "fundingRate_1h_pct": fr * 100.0,
                        "fundingRate_annual_pct": fr * 3.0 * 365.0 * 100.0,
                        "premium": 0.0,
                    })
    except Exception as e:
        log(f"BingX funding rate fetch error for {clean_sym}: {e}")
    return results


async def build_merged_dataset(
    symbol: str,
    start_utc: datetime,
    end_utc: datetime,
    *,
    out_dir: Path,
    product_type: str = "PERP",
    granularity: str = "1H",
    rank: Optional[int] = None,
    premium_dict: Optional[Dict[datetime, float]] = None,
    ticker_info: Optional[Dict[str, Any]] = None,
) -> Tuple[pd.DataFrame, Path]:
    start_ms = to_ms(start_utc)
    end_ms = to_ms(end_utc)
    log(f"Start unified fetch for {symbol} {start_utc.isoformat()} -> {end_utc.isoformat()}")

    # 1. OHLCV フェッチ
    ohlcv_rows = await fetch_bingx_ohlcv(symbol, product_type, granularity, start_ms, end_ms)

    if not ohlcv_rows:
        raise RuntimeError(f"No OHLCV data returned for {symbol}.")

    price_records = []
    for it in ohlcv_rows:
        ts = int(it[0])
        price_records.append(
            {
                "timestamp": from_ms_jst(ts),
                "open": float(it[1]),
                "high": float(it[2]),
                "low": float(it[3]),
                "close": float(it[4]),
                "volume": float(it[5]) if len(it) > 5 else 0.0,
                "qv": float(it[6]) if len(it) > 6 else 0.0,
            }
        )
    df_price = (
        pd.DataFrame.from_records(price_records)
        .drop_duplicates(subset=["timestamp"])
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

    df = df_price.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    # 2. Funding History (FR & Premium) フェッチ & マージ
    funding_rows = await fetch_bingx_funding_history(symbol, start_ms, end_ms)
    if funding_rows:
        df_funding = (
            pd.DataFrame(funding_rows)
            .drop_duplicates(subset=["timestamp"])
            .sort_values("timestamp")
            .reset_index(drop=True)
        )
        df_funding["timestamp"] = pd.to_datetime(df_funding["timestamp"])
        df = pd.merge(df, df_funding, on="timestamp", how="left")
    else:
        df["fundingRate"] = 0.0
        df["fundingRate_1h_pct"] = 0.0
        df["fundingRate_annual_pct"] = 0.0
        df["premium"] = 0.0

    df["fundingRate"] = df["fundingRate"].ffill().bfill().fillna(0.0)
    df["fundingRate_1h_pct"] = df["fundingRate"] * 100.0
    df["fundingRate_annual_pct"] = df["fundingRate"] * 24.0 * 365.0 * 100.0
    df["premium"] = df.get("premium", pd.Series(0.0, index=df.index)).ffill().bfill().fillna(0.0)

    # 3. Open Interest (建玉)
    oi_coins = float(ticker_info.get("openInterestCoins", 0.0)) if ticker_info else 0.0
    df["openInterest"] = oi_coins
    df["openInterestVal"] = df["openInterest"] * df["close"]
    df["symbol"] = symbol

    df = df.sort_values("timestamp").reset_index(drop=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"merged_{symbol}.csv"
    df.to_csv(out_file, index=False, encoding="utf-8-sig")
    log(f"Saved merged dataset for {symbol} to {out_file} ({len(df)} rows)")

    return df, out_file


def check_normalized_peak_timing(df: pd.DataFrame) -> Tuple[bool, List[str]]:
    """
    30d (720h), 10d (240h), 5d (120h) の各期間において、
    前半に高値（ピーク）を付けて後半にかけて下落・低迷しているかを判定する。
    """
    windows = [
        ("5d", 5 * 24),    # 120h
        ("10d", 10 * 24),  # 240h
        ("30d", 30 * 24),  # 720h
    ]
    reasons = []
    is_bad = False

    for w_name, w_hours in windows:
        if len(df) < 24:
            continue
        sub = df.tail(w_hours).reset_index(drop=True)
        if len(sub) < 24:
            continue

        c_series = sub["close"].astype(float)
        base_c = c_series.iloc[0]
        if base_c <= 0:
            continue
        norm_c = c_series / base_c
        final_norm = float(norm_c.iloc[-1])

        # ピーク位置の特定
        peak_idx = int(norm_c.argmax())
        peak_ratio = peak_idx / len(norm_c)
        peak_val = float(norm_c.max())

        # 前半・後半の高値
        half = len(sub) // 2
        first_half_max = float(sub["high"].iloc[:half].max()) if "high" in sub.columns else float(c_series.iloc[:half].max())
        second_half_max = float(sub["high"].iloc[half:].max()) if "high" in sub.columns else float(c_series.iloc[half:].max())
        curr_c = float(c_series.iloc[-1])

        # 判定条件:
        # 1. 5d または 10d で、ピークが前半 (< 50%) にあり、直近がピークから10%以上下落かつマイナス (final_norm < 1.0)
        # 2. または、前半高値に対して後半高値が大きく切り下がっており (second_half_max < first_half_max * 0.93)、直近価格も高値から下落
        if w_name in ("5d", "10d"):
            if peak_ratio < 0.50 and (final_norm < peak_val * 0.90) and (final_norm < 1.0 or final_norm < peak_val * 0.80):
                is_bad = True
                reasons.append(f"{w_name}前半高値(peak={peak_ratio:.2f}, max={peak_val:.2f}x->{final_norm:.2f}x)")
            elif second_half_max < first_half_max * 0.93 and curr_c < first_half_max * 0.90:
                is_bad = True
                reasons.append(f"{w_name}高値切り下げ(前半max={first_half_max:.4f}, 後半max={second_half_max:.4f})")
        elif w_name == "30d":
            # 30d でピークが最序盤 (< 30%) にあり、直近が大きく沈んでいる場合
            if peak_ratio < 0.30 and final_norm < 0.90 and (final_norm < peak_val * 0.75):
                is_bad = True
                reasons.append(f"30d長期衰退(peak={peak_ratio:.2f}, max={peak_val:.2f}x->{final_norm:.2f}x)")

    return is_bad, reasons


def generate_normalized_charts(all_dfs: List[pd.DataFrame], out_dir: Path, window_name: str, hours_limit: int) -> Tuple[Optional[Path], float, str]:
    """指定期間 (30d / 10d / 5d) の上位10銘柄の正規化比較チャートを作成し、地合い（Market State）を判定 (パフォーマンス順ランキング凡例)"""
    if not all_dfs:
        return None, 1.0, "NEUTRAL"

    # 上位10銘柄に絞り込み
    dfs_top10 = all_dfs[:10]

    # 各銘柄の正規化パフォーマンスを収集
    plot_items = []
    for df in dfs_top10:
        if df.empty or "close" not in df.columns or "symbol" not in df.columns:
            continue
        sym = df["symbol"].iloc[0]
        df_sliced = df.tail(hours_limit).reset_index(drop=True)
        if len(df_sliced) < 2:
            continue

        close_series = df_sliced["close"].astype(float)
        base_price = close_series.iloc[0]
        if base_price == 0:
            continue
            
        norm_series = close_series / base_price
        final_norm = float(norm_series.iloc[-1])
        ts_series = pd.to_datetime(df_sliced["timestamp"])
        plot_items.append({
            "symbol": sym,
            "ts": ts_series,
            "norm": norm_series,
            "final_norm": final_norm
        })

    if not plot_items:
        return None, 1.0, "NEUTRAL"

    # 期間パフォーマンス（final_norm）の降順（ランキング順）にソート
    plot_items.sort(key=lambda x: x["final_norm"], reverse=True)

    norm_performances = [item["final_norm"] for item in plot_items]
    mean_norm = float(np.mean(norm_performances)) if norm_performances else 1.0
    market_state = "LONG" if mean_norm >= 1.0 else "SHORT"

    fig, ax = plt.subplots(figsize=(10.5, 5.2))
    
    # ランキング順にプロット & 凡例ラベルを設定
    for rank, item in enumerate(plot_items, 1):
        sym = item["symbol"]
        fn = item["final_norm"]
        pct_change = (fn - 1.0) * 100.0
        sign_str = "+" if pct_change >= 0 else ""
        label = f"{rank:>2}. {sym:<8} ({fn:.2f}x | {sign_str}{pct_change:.1f}%)"
        ax.plot(item["ts"], item["norm"], label=label, linewidth=2)

    ax.axhline(1.0, color="black", linestyle="--", linewidth=1.2, alpha=0.6, zorder=1)
    ax.set_title(f"BingX Top 10 Normalized Performance [{window_name}] (State: {market_state})", fontsize=12, pad=12)
    ax.set_xlabel("Time (JST)", fontsize=10)
    ax.set_ylabel("Normalized Price (Base = 1.0)", fontsize=10)
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), borderaxespad=0., fontsize=9)
    ax.grid(True, linestyle=':', alpha=0.4)
    plt.tight_layout()

    out_file = out_dir / f"normalized_chart_{window_name}.png"
    plt.savefig(out_file, dpi=150, bbox_inches='tight')
    plt.close(fig)

    log(f"Normalized chart Top 10 [{window_name}] saved to {out_file} | Mean Norm: {mean_norm:.4f} -> Market State: {market_state}")
    return out_file, mean_norm, market_state


def export_top10_gainers_csv(
    top10_gainers: List[dict],
    prioritized_candidates: List[dict],
    all_dfs: List[pd.DataFrame],
    out_dir: Path,
    discord: Optional[send_discord] = None,
    send_to_discord: bool = True,
) -> Tuple[Path, Path, Path]:
    """
    24時間上昇率 Top 10 銘柄の分析用データを集計し、
    1. Data/top10_24h_gainers_latest.csv (最新スナップショット)
    2. Data/top10_24h_gainers_history.csv (時系列蓄積ログ)
    3. Data/top10_gainers_snapshots/top10_gainers_YYYYMMDD_HHMMSS.csv (個別保存)
    に保存し、DiscordへCSVファイルをアップロードする。
    """
    now_jst = datetime.now(JST)
    now_utc = datetime.now(timezone.utc)
    ts_jst_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")
    ts_utc_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    file_ts_str = now_jst.strftime("%Y%m%d_%H%M%S")

    # symbol -> priority rank map
    priority_map = {t["symbol"]: idx + 1 for idx, t in enumerate(prioritized_candidates)}

    # symbol -> candle DataFrame map
    df_map = {}
    if all_dfs:
        for d in all_dfs:
            if isinstance(d, pd.DataFrame) and not d.empty and "symbol" in d.columns:
                df_map[d["symbol"].iloc[0]] = d

    records = []
    for gain_rank, t in enumerate(top10_gainers, 1):
        sym = t.get("symbol", "")
        last_pr = float(t.get("lastPr", 0.0))
        prev_pr = float(t.get("prevDayPx", 0.0))
        chg_24h = float(t.get("change_24h_pct", 0.0))
        vol_24h = float(t.get("usdtVolume", 0.0))
        oi_val = float(t.get("openInterestVal", 0.0))
        oi_coins = float(t.get("openInterestCoins", 0.0))
        funding = float(t.get("funding", 0.0))
        fr_1h_pct = float(t.get("funding_rate_pct", funding * 100.0))
        fr_ann_pct = float(t.get("funding_annual_pct", funding * 24.0 * 365.0 * 100.0))
        w_usd = float(t.get("whale_net_val_usd", 0.0))
        w_fmt = t.get("whale_net_val_fmt", "$0")
        w_long_r = float(t.get("whale_long_ratio", 0.5))
        w_impact = float(t.get("whale_impact_ratio", 0.0))
        w_tier = int(t.get("tier", 2))
        w_status = t.get("whale_status", "NEUTRAL")
        w_tag = t.get("whale_tag", "⚪ クジラ中立")
        p_rank = priority_map.get(sym, gain_rank)

        # キャンドルデータからの短期リターン・ボラティリティ算出
        ret_1h_pct = 0.0
        ret_4h_pct = 0.0
        vol_24h_pct = 0.0
        vol_surge_ratio = 1.0

        if sym in df_map:
            df_sym = df_map[sym]
            if len(df_sym) >= 2:
                c_now = float(df_sym["close"].iloc[-1])
                c_1h = float(df_sym["close"].iloc[-2])
                ret_1h_pct = ((c_now - c_1h) / c_1h * 100.0) if c_1h > 0 else 0.0

            if len(df_sym) >= 5:
                c_now = float(df_sym["close"].iloc[-1])
                c_4h = float(df_sym["close"].iloc[-5])
                ret_4h_pct = ((c_now - c_4h) / c_4h * 100.0) if c_4h > 0 else 0.0

            if len(df_sym) >= 24:
                df_last24 = df_sym.tail(24)
                h_max = float(df_last24["high"].max())
                l_min = float(df_last24["low"].min())
                vol_24h_pct = ((h_max - l_min) / l_min * 100.0) if l_min > 0 else 0.0
                mean_v = float(df_last24["volume"].mean())
                curr_v = float(df_last24["volume"].iloc[-1])
                vol_surge_ratio = (curr_v / mean_v) if mean_v > 0 else 1.0

        records.append({
            "timestamp_jst": ts_jst_str,
            "timestamp_utc": ts_utc_str,
            "gain_rank": gain_rank,
            "priority_rank": p_rank,
            "symbol": sym,
            "change_24h_pct": round(chg_24h, 2),
            "current_price": last_pr,
            "prev_day_price": prev_pr,
            "volume_24h_usd": round(vol_24h, 2),
            "open_interest_usd": round(oi_val, 2),
            "open_interest_coins": round(oi_coins, 2),
            "funding_rate_1h_pct": round(fr_1h_pct, 4),
            "funding_rate_annual_pct": round(fr_ann_pct, 2),
            "whale_tier": w_tier,
            "whale_status": w_status,
            "whale_tag": w_tag,
            "whale_net_usd": round(w_usd, 2),
            "whale_net_fmt": w_fmt,
            "whale_long_ratio": round(w_long_r, 4),
            "whale_impact_pct": round(w_impact, 2),
            "return_1h_pct": round(ret_1h_pct, 2),
            "return_4h_pct": round(ret_4h_pct, 2),
            "volatility_24h_pct": round(vol_24h_pct, 2),
            "volume_surge_ratio": round(vol_surge_ratio, 2),
        })

    df_top10 = pd.DataFrame(records)

    # 1. Data/top10_24h_gainers_latest.csv の保存
    latest_file = out_dir / "top10_24h_gainers_latest.csv"
    df_top10.to_csv(latest_file, index=False, encoding="utf-8-sig")
    log(f"Saved latest Top 10 gainers to {latest_file}")

    # 2. Data/top10_gainers_snapshots/top10_gainers_YYYYMMDD_HHMMSS.csv の保存
    snapshot_dir = out_dir / "top10_gainers_snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_file = snapshot_dir / f"top10_gainers_{file_ts_str}.csv"
    df_top10.to_csv(snapshot_file, index=False, encoding="utf-8-sig")
    log(f"Saved snapshot Top 10 gainers to {snapshot_file}")

    # 3. Data/top10_24h_gainers_history.csv への時系列追記蓄積
    history_file = out_dir / "top10_24h_gainers_history.csv"
    if history_file.exists():
        try:
            df_hist = pd.read_csv(history_file)
            # 同一日時・同一銘柄の重複を除外して結合
            df_hist = df_hist[~((df_hist["timestamp_jst"] == ts_jst_str) & (df_hist["symbol"].isin(df_top10["symbol"])))]
            df_combined = pd.concat([df_hist, df_top10], ignore_index=True)
            df_combined.to_csv(history_file, index=False, encoding="utf-8-sig")
        except Exception as e:
            log(f"[Warning] Failed to append to history CSV: {e}")
            df_top10.to_csv(history_file, index=False, encoding="utf-8-sig")
    else:
        df_top10.to_csv(history_file, index=False, encoding="utf-8-sig")
    log(f"Appended Top 10 gainers to historical archive: {history_file}")

    # 4. Discord へ CSV ファイルをアップロード
    if send_to_discord and discord:
        try:
            top_sym = df_top10.iloc[0]['symbol'] if not df_top10.empty else "N/A"
            top_chg = df_top10.iloc[0]['change_24h_pct'] if not df_top10.empty else 0.0
            prio_sym = prioritized_candidates[0]['symbol'] if prioritized_candidates else "N/A"
            prio_tier = prioritized_candidates[0].get('tier', 2) if prioritized_candidates else 2
            
            upload_desc = (
                f"📊 **[24h 上昇率 Top 10 データ集計 CSV]** ({ts_jst_str} JST)\n"
                f"• 24h上昇率1位: **{top_sym}** ({top_chg:+.2f}%)\n"
                f"• クジラ優先1位: **{prio_sym}** (Tier {prio_tier})\n"
                f"検証・分析用の詳細データ (OI, FR, 大口流入, 短期リターン等) を添付しました。"
            )
            discord.send_file(snapshot_file, upload_desc)
            log(f"Uploaded {snapshot_file.name} to Discord successfully.")
        except Exception as d_err:
            log(f"[Warning] Failed to upload Top 10 CSV to Discord: {d_err}")

    return latest_file, snapshot_file, history_file


def breakout_precursor_engine(
    df: pd.DataFrame,
    bb_window: int = 20,
    bb_mult: float = 2.0,
    gk_window: int = 24,
    tension_window: int = 24,
    regime_window: int = 168,
    clv_window: int = 12
) -> Optional[pd.DataFrame]:
    """1時間足データからブレイクアウト前兆スコア・特徴量を算出"""
    data = df.copy()
    if len(data) < 30:
        return None

    # 1. Garman-Klass Volatility & Bollinger Bands
    log_hl = np.log(data["high"] / np.where(data["low"] <= 0, 1e-9, data["low"]))
    log_co = np.log(data["close"] / np.where(data["open"] <= 0, 1e-9, data["open"]))
    gk_var = 0.5 * (log_hl ** 2) - (2 * np.log(2) - 1) * (log_co ** 2)
    data["gk_vol"] = np.sqrt(np.maximum(gk_var.rolling(gk_window).mean(), 1e-12))

    data["bb_mid"] = data["close"].rolling(bb_window).mean()
    data["bb_std"] = data["close"].rolling(bb_window).std()
    data["bb_upper"] = data["bb_mid"] + bb_mult * data["bb_std"]
    data["bb_lower"] = data["bb_mid"] - bb_mult * data["bb_std"]
    data["bb_width"] = (data["bb_upper"] - data["bb_lower"]) / (data["bb_mid"] + 1e-9)
    data["pct_b"] = (data["close"] - data["bb_lower"]) / ((data["bb_upper"] - data["bb_lower"]) + 1e-9)

    actual_regime = min(len(data), regime_window)
    if actual_regime < 24:
        return None

    data["gk_vol_rank"] = data["gk_vol"].rolling(actual_regime).apply(
        lambda x: pd.Series(x).rank(pct=True).iloc[-1] if len(x) == actual_regime else np.nan, raw=False
    )
    data["bb_width_rank"] = data["bb_width"].rolling(actual_regime).apply(
        lambda x: pd.Series(x).rank(pct=True).iloc[-1] if len(x) == actual_regime else np.nan, raw=False
    )

    data["is_compressed"] = (data["gk_vol_rank"] <= 0.20) | (data["bb_width_rank"] <= 0.20)
    data["was_compressed_6h"] = data["is_compressed"].rolling(6).max() == 1

    # 2. 建玉(OI) / 出来高過密テンション指標 (Tension)
    tr1 = data["high"] - data["low"]
    tr2 = (data["high"] - data["close"].shift(1)).abs()
    tr3 = (data["low"] - data["close"].shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    data["atr_pct"] = tr.rolling(tension_window).mean() / (data["close"] + 1e-9)

    oi_col = next((c for c in ["openInterest", "open_interest", "oi"] if c in data.columns), None)
    if oi_col is not None and data[oi_col].nunique() > 1:
        oi_change = data[oi_col].diff(tension_window)
        data["tension"] = oi_change / (data["atr_pct"] * data[oi_col].shift(tension_window) + 1e-9)
    else:
        data["tension"] = (data["volume"].rolling(tension_window).mean()) / (data["atr_pct"] * data["close"] + 1e-9)

    data["tension_rank"] = data["tension"].rolling(actual_regime).apply(
        lambda x: pd.Series(x).rank(pct=True).iloc[-1] if len(x) == actual_regime else np.nan, raw=False
    )
    data["is_fuel_loaded"] = data["tension_rank"] >= 0.70

    # 3. レンジ端受容 & 張り付き (Clinging)
    high_24 = data["high"].rolling(24).max()
    low_24 = data["low"].rolling(24).min()
    data["range_pos_24"] = (data["close"] - low_24) / ((high_24 - low_24) + 1e-9)
    data["clinging_high"] = (data["range_pos_24"] >= 0.65).rolling(4).sum() >= 2
    data["clinging_low"] = (data["range_pos_24"] <= 0.35).rolling(4).sum() >= 2

    # 4. 足内プライスアクション (CLV) & Funding Rate
    hl = data["high"] - data["low"]
    clv = ((data["close"] - data["low"]) - (data["high"] - data["close"])) / np.where(hl == 0, 1e-9, hl)
    data["clv_sum"] = clv.rolling(clv_window).sum()

    fr_col = next((c for c in ["fundingRate_annual_pct", "fundingRate", "funding_rate"] if c in data.columns), None)
    if fr_col is not None:
        data["fr_short_bias"] = data[fr_col] < 0
    else:
        data["fr_short_bias"] = False

    # 5. ブレイクアウト前兆総合シグナル
    data["precursor_long"] = data["was_compressed_6h"] & data["clinging_high"] & (data["pct_b"] >= 0.60)
    data["precursor_short"] = data["was_compressed_6h"] & data["clinging_low"] & (data["pct_b"] <= 0.40)

    return data


async def scan_precursor_candidates(
    tickers: List[Dict[str, Any]],
    whale_net_flows: Dict[str, Dict[str, Any]],
    min_vol_usd: float = 1000000.0,
    top_n: int = 10
) -> List[Dict[str, Any]]:
    """BingX全Swap(無期限先物)銘柄をスキャンし、前兆スコア順に上位候補を選出"""
    bx_url = f"{REST_API_URL['bingx']}/openApi/swap/v2/quote/klines"
    now_utc = datetime.now(timezone.utc)
    end_ts = int(now_utc.timestamp() * 1000)
    start_ts = int((now_utc - timedelta(days=8)).timestamp() * 1000)

    # 1. BTC除外、非暗号資産（株式トークン等）除外および出来高フィルタ
    candidates_to_scan = []
    for t in tickers:
        sym = t.get("symbol", "")
        clean_base = sym.replace("-USDT", "").replace("USDT", "").upper()
        if clean_base == "BTC" or "NCSK" in sym.upper() or "2USD" in sym.upper():
            continue
        vol_24h = float(t.get("usdtVolume", 0.0))
        px = float(t.get("lastPr", 0.0))
        if vol_24h < min_vol_usd or px <= 0:
            continue
        candidates_to_scan.append(t)

    # 出来高降順にソートし、流動性上位80銘柄をスキャン対象とする（高速かつ高流動性銘柄に特化）
    candidates_to_scan.sort(key=lambda x: float(x.get("usdtVolume", 0.0)), reverse=True)
    scan_pool = candidates_to_scan[:80]

    print(f"\n[Precursor Engine] Scanning {len(scan_pool)} high-volume BingX symbols for compression, tension, FR & precursor signals...")

    import aiohttp
    sem = asyncio.Semaphore(15)  # 同時リクエスト数制限

    async def fetch_candle_for_sym(session, t):
        sym = t.get("symbol", "")
        params = {
            "symbol": sym,
            "interval": "1h",
            "startTime": str(start_ts),
            "endTime": str(end_ts),
            "limit": "200"
        }
        async with sem:
            for attempt in range(3):
                try:
                    async with session.get(bx_url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("code") == 0:
                                items = data.get("data", [])
                                if isinstance(items, list) and len(items) >= 30:
                                    return t, items
                        await asyncio.sleep(0.2)
                except Exception:
                    await asyncio.sleep(0.2)
        return t, None

    async with aiohttp.ClientSession() as session:
        tasks = [fetch_candle_for_sym(session, t) for t in scan_pool]
        results = await asyncio.gather(*tasks)

    scored_candidates = []
    for t, items in results:
        if not items:
            continue
        sym = t.get("symbol", "")
        clean_sym = sym.replace("-USDT", "").replace("USDT", "").replace("USDC", "")
        w_info = whale_net_flows.get(clean_sym) or whale_net_flows.get(sym) or {}
        w_usd = float(w_info.get("net_val_usd", 0.0))
        w_fmt = w_info.get("net_val_fmt", "$0")
        w_ratio = float(w_info.get("long_ratio", 0.5))

        try:
            df = pd.DataFrame(items)
            for c in ["open", "high", "low", "close", "volume"]:
                if c in df.columns:
                    df[c] = df[c].astype(float)
            if "time" in df.columns:
                df["timestamp"] = pd.to_datetime(df["time"].astype(int), unit="ms")
                df = df.sort_values("timestamp").reset_index(drop=True)

            fr_annual = float(t.get("funding_annual_pct", 0.0))
            if fr_annual == 0.0 and "fundingRate" in t:
                fr_annual = float(t.get("fundingRate", 0.0)) * 100 * 365 * 24
            df["fundingRate"] = fr_annual

            # -------------------------------------------------------------
            # 【下落トレンド・暴落銘柄除外フィルター (LONG ONLY 厳格除外)】
            # -------------------------------------------------------------
            curr_close = df["close"].iloc[-1]
            ret_24h = (curr_close - df["close"].iloc[-24]) / df["close"].iloc[-24] if len(df) >= 24 else 0.0
            ret_3d = (curr_close - df["close"].iloc[-72]) / df["close"].iloc[-72] if len(df) >= 72 else ret_24h
            ret_5d = (curr_close - df["close"].iloc[-120]) / df["close"].iloc[-120] if len(df) >= 120 else ret_3d
            
            sma50_series = df["close"].rolling(50, min_periods=20).mean()
            sma50 = sma50_series.iloc[-1] if not sma50_series.empty else curr_close
            is_below_sma50 = (curr_close < sma50) if not pd.isna(sma50) else False

            # 下落トレンド判定:
            # 1. 5日間で -10% 以下 または 3日間で -7% 以下の大幅急落 (暴落ナイフ)
            # 2. 24時間騰落率が -5% 以下
            # 3. 24時間騰落率がマイナス かつ 中期SMA50を明確に下回っている
            if ret_5d < -0.10 or ret_3d < -0.07 or ret_24h < -0.05 or (ret_24h < -0.02 and is_below_sma50):
                # 下落トレンドのためロング選定から完全除外
                continue

            # 4. 【チャート前半高値除外フィルター】
            # 直近3日(72h)または5日(120h)において前半に高値を付け、後半の高値が大きく切り下がっている下落形状を除外
            is_peak_in_first_half = False
            for check_w in [72, 120]:
                if len(df) >= check_w:
                    sub_w = df.tail(check_w).reset_index(drop=True)
                    half = check_w // 2
                    first_max = sub_w["high"].iloc[:half].max()
                    second_max = sub_w["high"].iloc[half:].max()
                    # 前半高値に対して後半高値が7%以上切り下がり、現在値も高値から10%以上下落している形状
                    if second_max < first_max * 0.93 and curr_close < first_max * 0.90:
                        is_peak_in_first_half = True
                        break
            if is_peak_in_first_half:
                continue

            df_res = breakout_precursor_engine(df)
            if df_res is None or df_res.empty:
                continue

            last = df_res.iloc[-1]
            gk_r = float(last.get("gk_vol_rank", 0.5)) if not pd.isna(last.get("gk_vol_rank")) else 0.5
            bb_r = float(last.get("bb_width_rank", 0.5)) if not pd.isna(last.get("bb_width_rank")) else 0.5
            t_r = float(last.get("tension_rank", 0.5)) if not pd.isna(last.get("tension_rank")) else 0.5
            r_pos = float(last.get("range_pos_24", 0.5)) if not pd.isna(last.get("range_pos_24")) else 0.5
            pct_b = float(last.get("pct_b", 0.5)) if not pd.isna(last.get("pct_b")) else 0.5
            clv_s = float(last.get("clv_sum", 0.0)) if not pd.isna(last.get("clv_sum")) else 0.0

            # 総合前兆スコア (Precursor Score) 計算
            score = 0.0
            # 1. 圧縮度 (下位30%以下で加点)
            if gk_r <= 0.30: score += (0.30 - gk_r) * 600
            if bb_r <= 0.30: score += (0.30 - bb_r) * 400

            # 2. プレカーサー完全点灯ボーナス
            if last.get("precursor_long", False): score += 300

            # 3. OI / 出来高テンション
            if t_r >= 0.70: score += (t_r - 0.70) * 400

            # 4. FRマイナス (ショート踏み上げ燃料: 価格が崩れていない場合のみ加点)
            if fr_annual < 0:
                if ret_24h >= 0 or ret_3d >= 0:
                    score += min(350, 150 + abs(fr_annual) * 8)
                else:
                    score += 50  # 下落基調でのFRマイナスは踏み上げ期待が薄いため低加点
            elif fr_annual <= 5.0:
                score += 50

            # 5. 出来高急増 (Volume Surge)
            vol_mean_20 = df["volume"].tail(20).mean()
            curr_vol = df["volume"].iloc[-1]
            vol_surge_ratio = (curr_vol / (vol_mean_20 + 1e-9)) if vol_mean_20 > 0 else 1.0
            if vol_surge_ratio >= 1.3:
                score += min(150, (vol_surge_ratio - 1.0) * 100)

            # 6. 高値張り付き & CLV
            if r_pos >= 0.60: score += (r_pos - 0.60) * 300
            if pct_b >= 0.60: score += (pct_b - 0.60) * 200
            if clv_s > 0: score += min(100, clv_s * 15)

            # 7. クジラ純流入
            if w_usd > 0: score += 100

            # Tier 分類
            if w_usd > 0:
                tier = 1
                w_status = "BUY"
                w_tag = "🟢 クジラ買い越し"
            elif w_usd == 0:
                tier = 2
                w_status = "NEUTRAL"
                w_tag = "⚪ クジラ中立"
            else:
                tier = 3
                w_status = "SELL"
                w_tag = "🔴 クジラ売り越し"

            item = dict(t)
            item.update({
                "score": round(score, 2),
                "tier": tier,
                "whale_status": w_status,
                "whale_tag": w_tag,
                "whale_net_val_usd": w_usd,
                "whale_net_val_fmt": w_fmt,
                "whale_long_ratio": w_ratio,
                "gk_vol_rank": round(gk_r, 3),
                "bb_width_rank": round(bb_r, 3),
                "tension_rank": round(t_r, 3),
                "range_pos_24": round(r_pos, 3),
                "pct_b": round(pct_b, 3),
                "clv_sum": round(clv_s, 2),
                "fr_annual_pct": round(fr_annual, 2),
                "precursor_long": bool(last.get("precursor_long", False)),
                "is_compressed": bool(last.get("is_compressed", False)),
            })
            scored_candidates.append(item)

        except Exception as scan_err:
            continue

    # クジラ適格性（ロング専用: Tier 1 買い越し > Tier 2 中立 > Tier 3 売り越し）と前兆スコアでソート
    # Tier 1 & 2 を最優先とし、その中で前兆スコア降順
    scored_candidates = sorted(
        scored_candidates,
        key=lambda x: (
            0 if x.get("tier") in (1, 2) else 1,
            -float(x.get("score", 0.0))
        )
    )
    return scored_candidates[:top_n]


async def main():
    parser = argparse.ArgumentParser(description="Build mixed datasets with Precursor Engine")
    parser.add_argument("--start", type=str, default=DEFAULT_START_STR, help="ISO start timestamp")
    parser.add_argument("--top-n", type=int, default=15, help="Number of alt symbols to select")
    parser.add_argument("--no-chart-send", action="store_true", help="Do not send charts to Discord")
    args = parser.parse_args()

    # 0. クジラ分析スクリプトを自動更新実行
    whale_script = Path(__file__).resolve().parent / "fetch_whale_sentiment.py"
    if whale_script.exists():
        try:
            print("[Whale Engine] Updating whale sentiment via fetch_whale_sentiment.py...")
            subprocess.run([sys.executable, str(whale_script)], check=True, timeout=60)
        except Exception as w_err:
            print(f"[Whale Warning] Failed to run fetch_whale_sentiment.py: {w_err}")

    # 1. 銘柄情報の取得 & クジラ流入データの紐付け
    print("\n[Ranking Engine] Fetching BingX Tickers & Smart Metrics...")
    tickers = fetch_bingx_tickers()
    if not tickers:
        print("[Error] Failed to fetch tickers from BingX API.")
        return

    whale_json_file = Path(__file__).resolve().parent / "Data" / "whale_market_state.json"
    whale_net_flows = {}
    if whale_json_file.exists():
        try:
            with open(whale_json_file, "r", encoding="utf-8") as f:
                w_data = json.load(f)
                coins_sem = w_data.get("coins_sentiment", {})
                for c_name, c_info in coins_sem.items():
                    whale_net_flows[c_name] = {
                        "net_val_usd": c_info.get("net_val_usd", 0.0),
                        "net_val_fmt": c_info.get("net_val_formatted", "$0"),
                        "long_ratio": c_info.get("long_ratio", 0.5)
                    }
        except Exception as e:
            print(f"[Warning] Failed to load whale_market_state.json: {e}")

    # 2. ブレイクアウト前兆エンジン（GKボラ圧縮・OIテンション・高値張り付き・FRバイアス）による選定
    prioritized_candidates = await scan_precursor_candidates(tickers, whale_net_flows, min_vol_usd=1000000.0, top_n=args.top_n)

    out_dir = Path(__file__).resolve().parent / "Data"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 3. symbol_selection_scores.csv の保存 (前兆スコア優先ランキング)
    df_scores = pd.DataFrame(prioritized_candidates)
    df_scores["priority_rank"] = df_scores.index + 1
    scores_path = out_dir / "symbol_selection_scores.csv"
    df_scores.to_csv(scores_path, index=False)

    tier1_cnt = sum(1 for c in prioritized_candidates if c.get("tier") == 1)
    tier2_cnt = sum(1 for c in prioritized_candidates if c.get("tier") == 2)
    tier3_cnt = sum(1 for c in prioritized_candidates if c.get("tier") == 3)
    log(f"Saved precursor selection scores to {scores_path} (Top: {', '.join([c['symbol'] for c in prioritized_candidates[:5]])} | Tier1: {tier1_cnt}, Tier2: {tier2_cnt}, Tier3: {tier3_cnt})")

    # 4. 選定候補 Top 10 銘柄の OHLCV & Funding データ取得 (過去30日分+アルファ: 32日間)・ファイル保存
    end_utc = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start_utc = end_utc - timedelta(days=32)

    target_symbols = [t["symbol"] for t in prioritized_candidates]
    btc_sym = "BTC-USDT" if any("-USDT" in str(s) for s in target_symbols) else "BTC"
    if btc_sym not in target_symbols and "BTC" not in target_symbols:
        target_symbols.insert(0, btc_sym)

    ticker_map = {t.get("symbol"): t for t in tickers}

    print(f"\n[Download Engine] Downloading 30-day+ OHLCV, Funding Rate & OI for {len(target_symbols)} symbols: {target_symbols}...")

    all_dfs = []
    for rank, sym in enumerate(target_symbols, 1):
        try:
            print(f"   Downloading {sym} (OHLCV + Funding Rate)...")
            t_info = ticker_map.get(sym) or {}
            df, file_path = await build_merged_dataset(
                sym,
                start_utc,
                end_utc,
                out_dir=out_dir,
                rank=rank,
                ticker_info=t_info,
            )
            all_dfs.append(df)
            await asyncio.sleep(0.1)
        except Exception as e:
            print(f"   [Warning] Error fetching {sym}: {e}")
            await asyncio.sleep(0.1)

    # 3. チャート作成および解析処理
    if all_dfs:
        df_merged_all = pd.concat(all_dfs, ignore_index=True)
        merged_all_path = out_dir / "historical_all_symbols_merged.csv"
        df_merged_all.to_csv(merged_all_path, index=False, encoding="utf-8-sig")
        print(f"\n[Success] Merged 30-day dataset successfully saved to: {merged_all_path} ({len(df_merged_all)} rows)")

        # 日付スタンプ付きCSVおよびZIPアーカイブの作成
        start_tag = start_utc.strftime("%Y%m%d")
        end_tag = end_utc.strftime("%Y%m%d")
        dated_csv_name = f"{start_tag}_{end_tag}_all_symbols_merged.csv"
        dated_csv_path = out_dir / dated_csv_name
        df_merged_all.to_csv(dated_csv_path, index=False, encoding="utf-8-sig")

        # ==============================================================================
        # 【必須ルール】正規化（normalize）処理の前に、選定全銘柄+BTCの過去32日分データを
        # 1回すべてダウンロード完了し、必ずまとめてZIPファイル化してDiscordへ送信する
        # ==============================================================================
        zip_name = f"{start_tag}_{end_tag}_all_symbols_merged.zip"
        zip_path = out_dir / zip_name
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # 1. 統合マージドCSV
            zf.write(dated_csv_path, arcname=dated_csv_name)
            # 2. 銘柄別個別マージドCSV（バックテスト・検証用）
            for sym in target_symbols:
                ind_csv = out_dir / f"merged_{sym}.csv"
                if ind_csv.exists():
                    zf.write(ind_csv, arcname=f"individual/merged_{sym}.csv")

        master_zip_path = out_dir / "historical_all_symbols_merged.zip"
        with zipfile.ZipFile(master_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(merged_all_path, arcname="historical_all_symbols_merged.csv")

        discord = send_discord()
        if not args.no_chart_send and zip_path.exists():
            zip_desc = (
                f"📦 **【選定銘柄 32日分1HマージドデータZIP】** ({start_tag} -> {end_tag})\n"
                f"選定上位{len(target_symbols)}銘柄（+BTC）の1時間足OHLCV・出来高・FR・OI統合データセット"
            )
            discord.send_file(zip_path, zip_desc)

        # ==============================================================================
        # 【30日・10日・5日 ノーマライズチャート前半高値除外フィルター】
        # チャートの前半に高値を付けて後半下落している銘柄を選定ランキングから除外
        # ==============================================================================
        filtered_dfs = []
        excluded_symbols = set()

        for d in all_dfs:
            sym_name = d["symbol"].iloc[0]
            clean_name = sym_name.upper().replace("-USDT", "").replace("USDT", "").replace("USDC", "")
            if clean_name == "BTC":
                filtered_dfs.append(d)
                continue

            is_bad, bad_reasons = check_normalized_peak_timing(d)
            if is_bad:
                reason_str = ", ".join(bad_reasons)
                print(f"   [Normalized Peak Drop] {sym_name}: {reason_str} -> 前半高値下落型のため除外します。")
                excluded_symbols.add(sym_name)
            else:
                filtered_dfs.append(d)

        # チャート作成・選定用データセットを健全銘柄に更新
        plot_dfs = filtered_dfs

        # prioritized_candidates および symbol_selection_scores.csv の更新
        if excluded_symbols:
            prioritized_candidates = [c for c in prioritized_candidates if c["symbol"] not in excluded_symbols]
            df_scores = pd.DataFrame(prioritized_candidates)
            df_scores["priority_rank"] = df_scores.index + 1
            df_scores.to_csv(scores_path, index=False)
            print(f"   [Scores Updated] 前半高値除外銘柄 ({', '.join(excluded_symbols)}) を除去し、ランキングを再構築しました。(残り {len(prioritized_candidates)} 銘柄)")

        # ① 30d, 10d, 5d の 3 期間で正規化比較チャート生成 & 地合い判定
        windows = [
            ("30d", 30 * 24),
            ("10d", 10 * 24),
            ("5d", 5 * 24),
        ]

        discord = send_discord()
        window_states = []

        ts_jst_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        summary_text = (
            f"🚀 **【BingX ブレイクアウト前兆選定 & 多期間分析レポート】** ({ts_jst_str} JST)\n"
            f"========================================\n"
        )
        summary_text += f"\n📊 **[1. マルチタイムフレーム地合い判定]**\n"

        for win_label, win_hours in windows:
            chart_file, mean_norm, market_state = generate_normalized_charts(plot_dfs, out_dir, win_label, win_hours)
            final_st = "long_only" if market_state == "LONG" else "short_only"
            state_icon = "🟢" if market_state == "LONG" else "🔴"
            print(f"   └ [{win_label} Window] Mean Norm: {mean_norm:.4f} -> Market State: {market_state} ({final_st})")
            summary_text += f"• **[{win_label.upper()}]**: {state_icon} `{market_state}` (平均騰落: `{mean_norm:.4f}`)\n"
            window_states.append((win_label, market_state, mean_norm))

            if not args.no_chart_send and chart_file and chart_file.exists():
                discord.send_file(chart_file, f"Normalized Performance [{win_label}] Top 10 ({market_state})")

        # ② 前兆スコア Top 10
        print("\n==================================================================================")
        print(" [Step 2: Precursor Score Top 10 & Whale Inflow Priority Ranking]")
        print("==================================================================================")
        summary_text += f"\n👑 **[2. ブレイクアウト前兆 Top 10 銘柄]**\n"
        for rank, t in enumerate(prioritized_candidates, 1):
            sym = t.get("symbol", "")
            price = float(t.get("lastPr", 0))
            score_val = float(t.get("score", 0))
            w_fmt = t.get("whale_net_val_fmt", "$0")
            tag = t.get("whale_tag", "⚪ クジラ中立")
            tier = t.get("tier", 2)
            gk_r = float(t.get("gk_vol_rank", 0.5))
            fr_pct = float(t.get("fr_annual_pct", 0.0))
            line_str = f" #{rank:2d} [Tier {tier}] | {sym:8s} | Score: {score_val:5.1f} | GK_Vol: {gk_r:.2f} | FR: {fr_pct:+6.1f}%/年 | Whale: {w_fmt} ({tag})"
            log(line_str)
            summary_text += f"`#{rank:02d}` [T{tier}] **{sym:8s}** | Score: `{score_val:.1f}` | GK: `{gk_r:.2f}` | FR: `{fr_pct:+.1f}%` | Whale: `{w_fmt}`\n"
        print("----------------------------------------------------------------------------------")

        summary_text += f"========================================"
        if not args.no_chart_send:
            discord.send_message(summary_text)

        # market_state.json の保存
        last_market_state = window_states[-1][1] if window_states else "LONG"
        last_mean_norm = window_states[-1][2] if window_states else 1.0
        final_state = "long_only" if last_market_state == "LONG" else "short_only"
        state_file = out_dir / "market_state.json"
        state_data = {
            "market_state": final_state,
            "mean_norm": last_mean_norm,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state_data, f, indent=2)
        log(f"Saved market state ({final_state}) to {state_file}")

    print("\n==================================================")
    print("  Smart Selection & Multi-Window Analysis Completed!")
    print("==================================================")

if __name__ == "__main__":
    asyncio.run(main())

