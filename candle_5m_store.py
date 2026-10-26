"""
candle_5m_store.py
【単一データ源泉 (SSoT)】5分足 OHLCV 永続ローカル時系列ストア ＆ 動的ダウンサンプリングマネージャー
BingX USDT無期限先物版 (PROJECT_RULES.md 準拠)

- 5分足を最小データ粒度とし、Data/candles_5m/candles_5m_{SYMBOL}.csv に永続保存
- 初回起動時: BingX REST API (/openApi/swap/v2/quote/klines) から過去分を取得保存 (不足時は OKX REST API で補完)
- 定期実行時 (8時間ごと/毎時): 最終タイムスタンプ以降の差分のみを取得して高速 Append
- 稼働継続に伴い、ローカルには 60日〜65日分 (最大18,720本＝約2ヶ月強) の高密度データが自然蓄積
- GCP・Oracle・AWS 等のクラウド環境で遮断のない OKX REST API (/api/v5/market/history-candles) による外部フォールバック
- 任意の足種 (15m, 30m, 1h, 1d) への数学的完全整合ダウンサンプリング (resample_from_5m) を提供
"""

from __future__ import annotations
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Any

import numpy as np
import pandas as pd
import requests

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "Data"
CANDLES_5M_DIR = DATA_DIR / "candles_5m"
BINGX_REST_URL = "https://open-api.bingx.com/openApi/swap/v2/quote/klines"
BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"
OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/history-candles"

# 固定選定20銘柄 + 地合い用BTC (PROJECT_RULES.md 準拠)
DEFAULT_TARGET_SYMBOLS = [
    "BTC-USDT",
    "HYPE-USDT", "SOL-USDT", "DOGE-USDT", "XRP-USDT", "SUI-USDT",
    "AVAX-USDT", "LINK-USDT", "ADA-USDT", "NEAR-USDT", "APT-USDT",
    "OP-USDT", "ARB-USDT", "TIA-USDT", "RENDER-USDT", "WIF-USDT",
    "FET-USDT", "TAO-USDT", "AAVE-USDT", "ENA-USDT", "ZEC-USDT"
]


def normalize_symbol_name(symbol: str) -> str:
    """銘柄名の正規化 (大文字化、ハイフン統一)"""
    s = str(symbol).strip().upper()
    if s.endswith("USDT") and "-" not in s:
        s = s[:-4] + "-USDT"
    elif not s.endswith("-USDT"):
        s = s + "-USDT"
    return s


def get_clean_symbol(symbol: str) -> str:
    """ティッカー名のみ取得 (例: BTC-USDT -> BTC)"""
    s = str(symbol).strip().upper()
    return s.replace("-USDT", "").replace("USDT", "")


def get_5m_csv_path(symbol: str) -> Path:
    """指定銘柄の 5分足 CSV ファイルパスを取得"""
    clean_sym = normalize_symbol_name(symbol)
    CANDLES_5M_DIR.mkdir(parents=True, exist_ok=True)
    return CANDLES_5M_DIR / f"candles_5m_{clean_sym}.csv"


def fetch_bingx_5m_candles(symbol: str, start_ms: int, end_ms: int, max_bars: int = 10080, timeout: int = 10) -> pd.DataFrame:
    """
    BingX REST API (/openApi/swap/v2/quote/klines) から 5分足データをページネーション取得
    1リクエストあたり最大1,000本
    """
    clean_sym = normalize_symbol_name(symbol)
    cols = ["timestamp", "open", "high", "low", "close", "volume", "quote_volume"]
    rows = []
    cur_end = end_ms
    consecutive_empty = 0

    while len(rows) < max_bars and consecutive_empty < 3:
        params = {
            "symbol": clean_sym,
            "interval": "5m",
            "limit": 1000,
            "endTime": str(cur_end),
        }
        if start_ms > 0:
            params["startTime"] = str(start_ms)

        try:
            resp = requests.get(BINGX_REST_URL, params=params, timeout=timeout)
            if resp.status_code == 429:
                time.sleep(2.0)
                continue
            if resp.status_code != 200:
                time.sleep(1.0)
                consecutive_empty += 1
                continue

            data = resp.json()
            if data.get("code") != 0:
                consecutive_empty += 1
                time.sleep(0.5)
                continue

            items = data.get("data", [])
            if not items:
                consecutive_empty += 1
                break

            consecutive_empty = 0
            # items は昇順 (古い -> 新しい)
            batch = []
            for item in items:
                t = int(item.get("time") or 0)
                if t < start_ms:
                    continue
                o = float(item.get("open") or 0.0)
                h = float(item.get("high") or 0.0)
                l = float(item.get("low") or 0.0)
                c = float(item.get("close") or 0.0)
                v = float(item.get("volume") or 0.0)
                qv = v * c
                batch.append([t, o, h, l, c, v, qv])

            if not batch:
                break

            # 過去方向へ遡るため、先頭（最古）のタイムスタンプを取得
            oldest_ts = int(items[0].get("time") or 0)
            rows = batch + rows

            if oldest_ts <= start_ms or oldest_ts >= cur_end or len(items) < 10:
                break
            cur_end = oldest_ts - 1
            time.sleep(0.08)  # レート制限保護

        except Exception:
            consecutive_empty += 1
            time.sleep(1.0)

    if rows:
        df = pd.DataFrame(rows, columns=cols)
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms", utc=True)
        df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        # 未確定足（直近5分以内）を除外
        now_utc = datetime.now(timezone.utc)
        if not df.empty and df["timestamp"].iloc[-1] + timedelta(minutes=5) > now_utc:
            df = df.iloc[:-1].reset_index(drop=True)
        return df

    return pd.DataFrame(columns=cols)


def fetch_okx_5m_candles(symbol: str, start_ms: int, end_ms: int, max_bars: int = 12000, timeout: int = 8) -> pd.DataFrame:
    """
    OKX REST API (/api/v5/market/history-candles) から 5分足データをページネーション取得
    - GCP / Oracle VPS / AWS いずれのクラウド環境でも遮断なく 65日以上前まで安定取得可能
    - instId: {TICKER}-USDT-SWAP (例: SOL-USDT-SWAP, BTC-USDT-SWAP, HYPE-USDT-SWAP)
    """
    raw_sym = get_clean_symbol(symbol)
    inst_id = f"{raw_sym}-USDT-SWAP"
    cols = ["timestamp", "open", "high", "low", "close", "volume", "quote_volume"]
    rows = []
    cur_after = str(end_ms + 300_000) if end_ms > 0 else ""
    consecutive_empty = 0

    while len(rows) < max_bars and consecutive_empty < 3:
        params = {
            "instId": inst_id,
            "bar": "5m",
            "limit": "100",
        }
        if cur_after:
            params["after"] = cur_after

        try:
            resp = requests.get(OKX_CANDLES_URL, params=params, timeout=timeout)
            if resp.status_code == 429:
                time.sleep(1.0)
                continue
            if resp.status_code != 200:
                consecutive_empty += 1
                time.sleep(0.4)
                continue

            data_json = resp.json()
            if data_json.get("code") != "0":
                consecutive_empty += 1
                time.sleep(0.4)
                continue

            items = data_json.get("data", [])
            if not items:
                consecutive_empty += 1
                break

            consecutive_empty = 0
            batch = []
            for item in items:
                t = int(item[0])
                if t < start_ms:
                    continue
                o = float(item[1])
                h = float(item[2])
                l = float(item[3])
                c = float(item[4])
                v = float(item[5])
                qv = float(item[7]) if len(item) > 7 else (v * c)
                batch.append([t, o, h, l, c, v, qv])

            if not batch:
                break

            rows = batch + rows
            oldest_ts = int(items[-1][0])
            if oldest_ts <= start_ms or len(items) < 10:
                break
            cur_after = str(oldest_ts)
            time.sleep(0.03)  # OKX レート制限保護
        except Exception:
            consecutive_empty += 1
            time.sleep(0.5)

    if rows:
        df = pd.DataFrame(rows, columns=cols)
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms", utc=True)
        df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        return df

    return pd.DataFrame(columns=cols)


def fetch_bybit_5m_candles(symbol: str, start_ms: int, end_ms: int, max_bars: int = 5000) -> pd.DataFrame:
    """Bybit V5 Kline API よりフォールバック取得"""
    raw_sym = get_clean_symbol(symbol)
    b_sym = f"{raw_sym}USDT"
    cols = ["timestamp", "open", "high", "low", "close", "volume", "quote_volume"]
    rows = []
    cur_end = end_ms
    consecutive_empty = 0

    while len(rows) < max_bars and consecutive_empty < 2:
        cur_start = max(start_ms, cur_end - (1000 * 5 * 60 * 1000))
        params = {
            "category": "linear",
            "symbol": b_sym,
            "interval": "5",
            "start": str(cur_start),
            "end": str(cur_end),
            "limit": 1000,
        }
        try:
            resp = requests.get(BYBIT_KLINE_URL, params=params, timeout=8)
            if resp.status_code != 200:
                break
            res_data = resp.json()
            if res_data.get("retCode") != 0:
                break
            data = res_data.get("result", {}).get("list", [])
            if not data:
                break
            for item in data:
                t_ms = int(item[0])
                o = float(item[1])
                h = float(item[2])
                l = float(item[3])
                c = float(item[4])
                v = float(item[5])
                qv = float(item[6]) if len(item) > 6 else (v * c)
                rows.append([t_ms, o, h, l, c, v, qv])

            oldest_ts = int(data[-1][0])
            if oldest_ts <= start_ms or oldest_ts >= cur_end:
                break
            cur_end = oldest_ts - 1
            time.sleep(0.04)
        except Exception:
            break

    if rows:
        df = pd.DataFrame(rows, columns=cols)
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(float), unit="ms", utc=True)
        df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        return df

    return pd.DataFrame(columns=cols)


def load_5m_candles(symbol: str) -> pd.DataFrame:
    """ローカルの 5分足 CSV からデータを読み込む (存在しない場合は空DF)"""
    csv_path = get_5m_csv_path(symbol)
    if not csv_path.exists():
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "quote_volume"])
    try:
        df = pd.read_csv(csv_path)
        if not df.empty and "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            df = df.sort_values("timestamp").reset_index(drop=True)
            return df
    except Exception as e:
        print(f"[5M Store Error] {symbol} load failed: {e}")
    return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "quote_volume"])


def sync_symbol_5m_candles(symbol: str, target_days: int = 65) -> pd.DataFrame:
    """
    指定銘柄の 5分足データを同期・差分追加 (Append) する
    - 既存ファイルがある場合: 最終足の時刻以降のみ API から取得して差分マージ
    - 過去分不足時 (65日分未満): BingX / OKX (クラウド完全互換) から過去補完
    - 最大保持件数: target_days * 288本 (デフォルト 65日分 = 約18,720本＝2ヶ月強) をローリング保持
    """
    csv_path = get_5m_csv_path(symbol)
    now_ms = int(time.time() * 1000)
    max_history_bars = target_days * 288

    existing_df = load_5m_candles(symbol)

    if not existing_df.empty:
        last_dt = existing_df["timestamp"].iloc[-1]
        last_ms = int(last_dt.timestamp() * 1000)

        # 差分追加 (最終足以降)
        if now_ms - last_ms >= 300_000:
            start_ms = last_ms + 1
            new_df = fetch_bingx_5m_candles(symbol, start_ms=start_ms, end_ms=now_ms)
            if not new_df.empty:
                existing_df = pd.concat([existing_df, new_df], ignore_index=True)
                existing_df = existing_df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

        # 過去分が不足している場合 (60日分 = 17,280本未満)、さらに過去方向を取得
        required_bars = int(target_days * 288 * 0.90)
        if len(existing_df) < required_bars:
            target_start_ms = now_ms - (target_days * 24 * 3600 * 1000)
            oldest_ms = int(existing_df["timestamp"].iloc[0].timestamp() * 1000)
            if oldest_ms > target_start_ms + 3600_000:
                # 1. BingX で遡れるだけ遡る
                past_df = fetch_bingx_5m_candles(symbol, start_ms=target_start_ms, end_ms=oldest_ms - 1)
                # 2. BingX で不足した分 (45日前〜65日前) は OKX (GCP/全クラウド互換) で完全補完
                if past_df.empty or len(past_df) < 50:
                    past_df = fetch_okx_5m_candles(symbol, start_ms=target_start_ms, end_ms=oldest_ms - 1)
                elif int(past_df["timestamp"].iloc[0].timestamp() * 1000) > target_start_ms + 3600_000:
                    bingx_oldest_ms = int(past_df["timestamp"].iloc[0].timestamp() * 1000)
                    okx_past_df = fetch_okx_5m_candles(symbol, start_ms=target_start_ms, end_ms=bingx_oldest_ms - 1)
                    if not okx_past_df.empty:
                        past_df = pd.concat([okx_past_df, past_df], ignore_index=True).drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
                # 3. 最終予備 (Bybit)
                if past_df.empty or len(past_df) < 50:
                    past_df = fetch_bybit_5m_candles(symbol, start_ms=target_start_ms, end_ms=oldest_ms - 1)

                if not past_df.empty:
                    existing_df = pd.concat([past_df, existing_df], ignore_index=True)
                    existing_df = existing_df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

        # ディスク上の SSoT ストアは常に最低 65 日分 (18,720 本) を保持し、短期差分同期で過去データが切り詰められるのを防止
        disk_max_bars = max(target_days, 65) * 288
        if len(existing_df) > disk_max_bars:
            existing_df = existing_df.tail(disk_max_bars).reset_index(drop=True)
        existing_df.to_csv(csv_path, index=False)
        if len(existing_df) > max_history_bars:
            return existing_df.tail(max_history_bars).reset_index(drop=True)
        return existing_df
    else:
        # 初回ブートストラップ: 過去 target_days 日分を取得
        start_ms = now_ms - (target_days * 24 * 3600 * 1000)
        df_init = fetch_bingx_5m_candles(symbol, start_ms=start_ms, end_ms=now_ms)
        if df_init.empty or len(df_init) < int(target_days * 288 * 0.5):
            # OKX で補完
            df_okx = fetch_okx_5m_candles(symbol, start_ms=start_ms, end_ms=now_ms)
            if not df_okx.empty:
                df_init = pd.concat([df_okx, df_init], ignore_index=True).drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
            if df_init.empty or len(df_init) < int(target_days * 288 * 0.5):
                df_bybit = fetch_bybit_5m_candles(symbol, start_ms=start_ms, end_ms=now_ms)
                if not df_bybit.empty:
                    df_init = pd.concat([df_bybit, df_init], ignore_index=True).drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

        if not df_init.empty:
            if len(df_init) > max_history_bars:
                df_init = df_init.tail(max_history_bars).reset_index(drop=True)
            df_init.to_csv(csv_path, index=False)
            return df_init

        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "quote_volume"])


def sync_all_target_symbols_5m(symbols: Optional[List[str]] = None, target_days: int = 65, lookback_days: Optional[int] = None) -> Dict[str, pd.DataFrame]:
    """全監視対象銘柄の 5分足データを一括同期・差分最新化"""
    effective_days = lookback_days if lookback_days is not None else target_days
    target_symbols = symbols or DEFAULT_TARGET_SYMBOLS
    results = {}
    print(f"[5M Store] 全 {len(target_symbols)} 銘柄の 5分足時系列ストアを同期中...")
    t0 = time.time()
    for sym in target_symbols:
        df = sync_symbol_5m_candles(sym, target_days=effective_days)
        results[sym] = df
        time.sleep(0.04)
    elapsed = time.time() - t0
    total_bars = sum(len(df) for df in results.values())
    print(f"[5M Store] 同期完了 ({elapsed:.2f}秒 / 総ローソク足数: {total_bars:,}本)")
    return results


def resample_from_5m(df_5m: pd.DataFrame, interval_minutes: int = 5, rule: Optional[str] = None) -> pd.DataFrame:
    """
    単一源泉の 5分足データから任意の時間足へ数学的完全整合ダウンサンプリング (リサンプリング)
    - 5m: そのまま返却
    - 15m: 15分足 (3本集約)
    - 30m: 30分足 (6本集約)
    - 60m (1H): 1時間足 (12本集約)
    - 240m (4H): 4時間足 (48本集約)
    - 1440m (1D): 日足 (288本集約)
    """
    if rule is not None and interval_minutes == 5:
        r_up = str(rule).upper()
        rule_min_map = {"15MIN": 15, "30MIN": 30, "1H": 60, "60MIN": 60, "4H": 240, "1D": 1440}
        interval_minutes = rule_min_map.get(r_up, 15)

    if df_5m is None or df_5m.empty or int(interval_minutes) <= 5:
        return df_5m.copy() if df_5m is not None else pd.DataFrame()

    df_copy = df_5m.copy()
    if not pd.api.types.is_datetime64_any_dtype(df_copy["timestamp"]):
        df_copy["timestamp"] = pd.to_datetime(df_copy["timestamp"], utc=True)
    df_copy = df_copy.sort_values("timestamp").reset_index(drop=True)

    resample_rule = f"{int(interval_minutes)}min"
    df_indexed = df_copy.set_index("timestamp")

    agg_dict = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    if "quote_volume" in df_indexed.columns:
        agg_dict["quote_volume"] = "sum"
    for col in ("openInterest", "oi_chg_4h", "fundingRate"):
        if col in df_indexed.columns:
            agg_dict[col] = "last"

    resampled = df_indexed.resample(resample_rule, closed="left", label="left").agg(agg_dict)
    resampled = resampled.dropna(subset=["open", "close"]).reset_index()
    return resampled


def get_resampled_candles(symbol: str, interval: str = "5m") -> pd.DataFrame:
    """
    銘柄と希望の時間足 ('5m', '15m', '30m', '1h', '4h', '1d') を指定して
    ローカル 5分足ストアから即時リサンプリングデータを取得
    """
    inv_map = {
        "5M": 5, "5MIN": 5, "5": 5,
        "15M": 15, "15MIN": 15, "15": 15,
        "30M": 30, "30MIN": 30, "30": 30,
        "1H": 60, "60M": 60, "60": 60,
        "2H": 120, "120M": 120, "120": 120,
        "4H": 240, "240M": 240, "240": 240,
        "1D": 1440, "D": 1440, "1440M": 1440,
    }
    target_min = inv_map.get(str(interval).upper(), 5)
    df_5m = load_5m_candles(symbol)
    if df_5m.empty:
        df_5m = sync_symbol_5m_candles(symbol)
    return resample_from_5m(df_5m, target_min)


if __name__ == "__main__":
    print("Testing candle_5m_store.py on BTC-USDT...")
    df = sync_symbol_5m_candles("BTC-USDT", target_days=5)
    print(f"BTC-USDT 5m candles: {len(df)} rows")
    if not df.empty:
        print("Latest 3 rows:")
        print(df.tail(3))
        df_1h = resample_from_5m(df, 60)
        print(f"Resampled 1h candles: {len(df_1h)} rows")
