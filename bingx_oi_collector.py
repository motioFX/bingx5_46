#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bingx_oi_collector.py
BingX USDT無期限先物 建玉（Open Interest）＆ ファンディングレート（Funding Rate）定期スナップショット蓄積モジュール
(PROJECT_RULES.md 第6項 準拠)

機能:
1. BingX Swap V2 API (premiumIndex, ticker, openInterest) から固定20銘柄 + BTC-USDT の最新 OI / FR / MarkPrice を取得。
2. Data/oi_store/{symbol}_oi.csv に時系列としてアペンド保存（重複排除・UTF-8）。
3. 蓄積前の過去分（直近35日分）は全クラウド・日本人環境互換の OKX 公開API（1時間OI・8時間FR履歴）を最優先で境界比率スケーリング結合し、
   ローソク足マージ・分析・バックテスト・実運用に 100% 同一の OI変化率 (oi_chg_4h) と FR (fundingRate) を提供する。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import numpy as np
import pandas as pd
import requests

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "Data"
OI_STORE_DIR = DATA_DIR / "oi_store"
OI_STORE_DIR.mkdir(parents=True, exist_ok=True)

JST = timezone(timedelta(hours=9))
BINGX_BASE_URL = "https://open-api.bingx.com"
BYBIT_BASE_URL = "https://api.bybit.com"

FIXED_TARGET_SYMBOLS: List[str] = [
    "BTC-USDT",
    "HYPE-USDT", "SOL-USDT", "DOGE-USDT", "XRP-USDT", "SUI-USDT",
    "AVAX-USDT", "LINK-USDT", "APT-USDT", "NEAR-USDT", "ARB-USDT",
    "UNI-USDT", "OP-USDT", "TIA-USDT", "RENDER-USDT", "INJ-USDT",
    "FET-USDT", "TAO-USDT", "AAVE-USDT", "ENA-USDT", "ZEC-USDT"
]

# インメモリキャッシュ: symbol -> (cached_epoch_sec, df_oi_fr)
_OI_FR_MEM_CACHE: Dict[str, Tuple[float, pd.DataFrame]] = {}


def normalize_sym(s: str) -> str:
    sym = str(s).strip().upper()
    if sym.endswith("USDT") and "-" not in sym:
        sym = sym[:-4] + "-USDT"
    elif not sym.endswith("-USDT"):
        sym = sym + "-USDT"
    return sym


def cleanup_invalid_oi_files() -> None:
    """1文字のティッカーなど誤って生成された不正な CSV ファイルをクリーンアップ"""
    if not OI_STORE_DIR.exists():
        return
    for f in OI_STORE_DIR.glob("*_oi.csv"):
        name_part = f.name.replace("_oi.csv", "").replace("-USDT", "").strip()
        if len(name_part) <= 1:
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass


def fetch_bingx_oi_fr_snapshot_sync(
    symbols: Optional[List[str]] = None,
    timeout: int = 10
) -> Dict[str, Dict[str, float]]:
    """指定銘柄群（デフォルト: 固定20銘柄 + BTC-USDT）の最新 OI / FR / MarkPrice スナップショットを取得"""
    if isinstance(symbols, str):
        clean_s = symbols.strip().upper()
        if clean_s in ("DEMO", "REAL", "AIR", "MOCK", "LIVE") or len(clean_s) <= 4:
            symbols = None
        else:
            symbols = [clean_s]

    valid_syms = symbols if symbols is not None else FIXED_TARGET_SYMBOLS
    target_syms = [normalize_sym(s) for s in valid_syms if len(str(s).strip()) >= 2]
    results: Dict[str, Dict[str, float]] = {}

    # 1. 全銘柄の premiumIndex (markPrice, lastFundingRate) を一括取得
    fr_map: Dict[str, Dict[str, float]] = {}
    try:
        resp_fr = requests.get(f"{BINGX_BASE_URL}/openApi/swap/v2/quote/premiumIndex", timeout=timeout)
        if resp_fr.status_code == 200:
            data_fr = resp_fr.json().get("data", [])
            if isinstance(data_fr, list):
                for item in data_fr:
                    sym = normalize_sym(item.get("symbol", ""))
                    fr_map[sym] = {
                        "markPrice": float(item.get("markPrice") or 0.0),
                        "fundingRate": float(item.get("lastFundingRate") or 0.0),
                    }
    except Exception as e:
        print(f"[BingX_OI Warning] premiumIndex fetch error: {e}")

    # 2. 各対象銘柄の openInterest を取得
    for sym in target_syms:
        try:
            oi_val = 0.0
            resp_oi = requests.get(
                f"{BINGX_BASE_URL}/openApi/swap/v2/quote/openInterest",
                params={"symbol": sym},
                timeout=5
            )
            if resp_oi.status_code == 200:
                oi_json = resp_oi.json()
                if oi_json.get("code") == 0:
                    oi_val = float(oi_json.get("data", {}).get("openInterest") or 0.0)

            info = fr_map.get(sym, {})
            mark_px = float(info.get("markPrice", 0.0))
            fr_val = float(info.get("fundingRate", 0.0))
            # BingX API の openInterest はすでに USDT 建て想定元本 (Notional USD) で返却されるため二重乗算しない
            oi_usd = oi_val

            results[sym] = {
                "openInterest": oi_val,
                "openInterestVal": oi_usd,
                "markPrice": mark_px,
                "fundingRate": fr_val,
            }
        except Exception:
            continue

    return results


def save_oi_snapshot(oi_data: Dict[str, Dict[str, float]], target_ts_ms: Optional[int] = None) -> int:
    """取得したスナップショットを Data/oi_store/{symbol}_oi.csv にアペンド保存"""
    if not oi_data:
        return 0

    now_utc = datetime.now(timezone.utc)
    if target_ts_ms is None:
        target_ts_ms = int(now_utc.timestamp() * 1000)
    dt_jst_str = datetime.fromtimestamp(target_ts_ms / 1000, tz=timezone.utc).astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")

    OI_STORE_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_invalid_oi_files()
    saved_count = 0

    for sym, item in oi_data.items():
        try:
            clean_sym = normalize_sym(sym)
            ticker = clean_sym.replace("-USDT", "")
            for fname in (f"{clean_sym}_oi.csv", f"{ticker}_oi.csv"):
                csv_path = OI_STORE_DIR / fname
                row = {
                    "timestamp": target_ts_ms,
                    "datetime_jst": dt_jst_str,
                    "openInterest": item.get("openInterest", 0.0),
                    "openInterestVal": item.get("openInterestVal", 0.0),
                    "markPrice": item.get("markPrice", 0.0),
                    "fundingRate": item.get("fundingRate", 0.0),
                }

                if csv_path.exists():
                    df = pd.read_csv(csv_path, encoding="utf-8")
                    new_df = pd.DataFrame([row])
                    df = pd.concat([df, new_df], ignore_index=True)
                else:
                    df = pd.DataFrame([row])

                df = df.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp").reset_index(drop=True)
                df.to_csv(csv_path, index=False, encoding="utf-8")
            _OI_FR_MEM_CACHE.pop(clean_sym, None)
            saved_count += 1
        except Exception:
            continue

    return saved_count


def collect_and_save_oi_snapshot(symbols: Optional[List[str]] = None, target_ts_ms: Optional[int] = None) -> int:
    """対象全銘柄の最新 OI/FR スナップショットを取得して Data/oi_store/ に保存"""
    data = fetch_bingx_oi_fr_snapshot_sync(symbols=symbols)
    if not data:
        return 0
    return save_oi_snapshot(data, target_ts_ms=target_ts_ms)


def load_bingx_oi_history(symbol: str, start_ms: int = 0, end_ms: int = 9999999999999) -> pd.DataFrame:
    """指定銘柄の蓄積済み BingX OI/FR 時系列を読み出す"""
    clean_sym = normalize_sym(symbol)
    ticker = clean_sym.replace("-USDT", "")
    for fname in (f"{clean_sym}_oi.csv", f"{ticker}_oi.csv"):
        csv_path = OI_STORE_DIR / fname
        if csv_path.exists():
            try:
                df = pd.read_csv(csv_path, encoding="utf-8")
                if not df.empty and "timestamp" in df.columns:
                    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce").fillna(0).astype(np.int64)
                    cond = (df["timestamp"] >= start_ms) & (df["timestamp"] <= end_ms)
                    return df[cond].copy().sort_values("timestamp").reset_index(drop=True)
            except Exception:
                pass
    return pd.DataFrame()


OKX_BASE_URL = "https://www.okx.com"
OKX_OI_HISTORY_URL = "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history"
OKX_FR_HISTORY_URL = "https://www.okx.com/api/v5/public/funding-rate-history"


def _fetch_okx_historical_oi_fr(symbol: str, days: int = 35) -> pd.DataFrame:
    """
    PROJECT_RULES.md 第6項準拠 (OKX最優先仕様):
    BingX 自前蓄積前の過去分（直近35日分）を全クラウド・日本人環境互換の
    OKX 公開 API (Rubik Open Interest & Funding Rate 履歴) から取得。
    """
    clean_sym = normalize_sym(symbol)  # e.g. "BTC-USDT"
    inst_id = f"{clean_sym}-SWAP"       # e.g. "BTC-USDT-SWAP"
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (days * 24 * 3600 * 1000)

    # 1. OKX ヒストリカル OI (1時間足) 取得
    oi_rows = []
    cur_end_ms = now_ms
    headers = {"User-Agent": "Mozilla/5.0"}

    for _ in range(10):  # 最大10ページ (1000時間 = 約41日分)
        params = {"instId": inst_id, "period": "1H", "end": str(cur_end_ms)}
        try:
            resp = requests.get(OKX_OI_HISTORY_URL, params=params, headers=headers, timeout=6)
            if resp.status_code != 200:
                break
            r_json = resp.json()
            if r_json.get("code") != "0":
                break
            lst = r_json.get("data", [])
            if not lst:
                break

            min_ts = cur_end_ms
            for item in lst:
                if len(item) >= 4:
                    t = int(item[0])
                    # item[3] が USD 建て OI 想定元本
                    oi_usd = float(item[3])
                    if start_ms <= t <= now_ms:
                        oi_rows.append((t, oi_usd))
                    if t < min_ts:
                        min_ts = t

            if min_ts <= start_ms or min_ts >= cur_end_ms:
                break
            cur_end_ms = min_ts - 1000
            time.sleep(0.04)  # レート制限保護
        except Exception:
            break

    # 2. OKX ヒストリカル Funding Rate (8時間足) 取得
    fr_rows = []
    cur_after = ""
    for _ in range(3):  # 最大3ページ (300件 = 2400時間 = 100日分)
        fr_params: Dict[str, Any] = {"instId": inst_id, "limit": 100}
        if cur_after:
            fr_params["after"] = cur_after
        try:
            resp_fr = requests.get(OKX_FR_HISTORY_URL, params=fr_params, headers=headers, timeout=6)
            if resp_fr.status_code == 200:
                r_fr = resp_fr.json()
                if r_fr.get("code") == "0":
                    data = r_fr.get("data", [])
                    if not data:
                        break
                    for x in data:
                        ft = int(x["fundingTime"])
                        fr_rows.append((ft, float(x["fundingRate"])))
                    cur_after = str(data[-1]["fundingTime"])
                    if int(cur_after) <= start_ms:
                        break
            time.sleep(0.04)
        except Exception:
            break

    if oi_rows:
        df_oi = pd.DataFrame(oi_rows, columns=["timestamp", "openInterest"]).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
        if fr_rows:
            df_fr = pd.DataFrame(fr_rows, columns=["timestamp", "fundingRate"]).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
            df_merged = pd.merge_asof(df_oi, df_fr, on="timestamp", direction="backward")
            df_merged["fundingRate"] = df_merged["fundingRate"].ffill().bfill().fillna(0.0001)
        else:
            df_merged = df_oi
            df_merged["fundingRate"] = 0.0001
        return df_merged

    # OKX に上場がない銘柄 (HYPEなど) のみ Bybit フォールバック試行
    return _fetch_bybit_historical_oi_fr_fallback(symbol, days)


def _fetch_bybit_historical_oi_fr_fallback(symbol: str, days: int = 35) -> pd.DataFrame:
    """OKX未上場銘柄用のフォールバック (Bybit V5)"""
    clean_sym = normalize_sym(symbol)
    b_sym = clean_sym.replace("-USDT", "USDT")
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - (days * 24 * 3600 * 1000)

    oi_rows = []
    cursor = ""
    for _ in range(5):
        params: Dict[str, Any] = {"category": "linear", "symbol": b_sym, "intervalTime": "1h", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = requests.get(f"{BYBIT_BASE_URL}/v5/market/open-interest", params=params, timeout=6)
            if resp.status_code != 200:
                break
            r_json = resp.json()
            lst = r_json.get("result", {}).get("list", [])
            if not lst:
                break
            for item in lst:
                oi_rows.append((int(item["timestamp"]), float(item["openInterest"])))
            if int(lst[-1]["timestamp"]) <= start_ms:
                break
            cursor = r_json.get("result", {}).get("nextPageCursor", "")
            if not cursor:
                break
        except Exception:
            break

    if not oi_rows:
        return pd.DataFrame()

    df_oi = pd.DataFrame(oi_rows, columns=["timestamp", "openInterest"]).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)

    fr_rows = []
    try:
        resp_fr = requests.get(
            f"{BYBIT_BASE_URL}/v5/market/funding/history",
            params={"category": "linear", "symbol": b_sym, "limit": 200},
            timeout=6
        )
        if resp_fr.status_code == 200:
            for x in resp_fr.json().get("result", {}).get("list", []):
                fr_rows.append((int(x["fundingRateTimestamp"]), float(x["fundingRate"])))
    except Exception:
        pass

    if fr_rows:
        df_fr = pd.DataFrame(fr_rows, columns=["timestamp", "fundingRate"]).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
        df_merged = pd.merge_asof(df_oi, df_fr, on="timestamp", direction="backward")
        df_merged["fundingRate"] = df_merged["fundingRate"].ffill().bfill().fillna(0.0001)
    else:
        df_merged = df_oi
        df_merged["fundingRate"] = 0.0001

    return df_merged


def sync_oi_fr_history_for_symbol(symbol: str, target_days: int = 35) -> pd.DataFrame:
    """
    指定銘柄の OI / FR 時系列を取得・補完し、4時間OI変化率 (oi_chg_4h) を付与して返す。
    - BingX 自前蓄積データ (Data/oi_store/{symbol}_oi.csv) が存在する期間は 100% BingX 純正データを最優先採用。
    - 過去不足分 (30日未満) は OKX 公開API (Rubik OI・FR履歴) を最優先で境界比率スケーリング結合し CSV に永続化。
    """
    clean_sym = normalize_sym(symbol)
    now_sec = time.time()
    cached = _OI_FR_MEM_CACHE.get(clean_sym)
    if cached is not None and (now_sec - cached[0]) < 900 and not cached[1].empty:
        return cached[1]

    df_bingx = load_bingx_oi_history(clean_sym)
    now_ms = int(now_sec * 1000)
    req_start_ms = now_ms - (target_days * 24 * 3600 * 1000)

    need_backfill = True
    if not df_bingx.empty and "timestamp" in df_bingx.columns:
        oldest_ms = int(df_bingx["timestamp"].iloc[0])
        if oldest_ms <= req_start_ms + (48 * 3600 * 1000) and len(df_bingx) >= 600:
            need_backfill = False

    if need_backfill:
        df_ext = _fetch_okx_historical_oi_fr(clean_sym, days=target_days)
        if not df_ext.empty:
            if not df_bingx.empty and "openInterest" in df_bingx.columns:
                # 境界時刻での建玉スケール比率を合わせて段差を完全排除
                earliest_bx_ts = int(df_bingx["timestamp"].iloc[0])
                earliest_bx_oi = float(df_bingx["openInterest"].iloc[0])
                df_ext_before = df_ext[df_ext["timestamp"] < earliest_bx_ts].copy()
                if not df_ext_before.empty and earliest_bx_oi > 0:
                    ref_ext_oi = float(df_ext_before["openInterest"].iloc[-1])
                    if ref_ext_oi > 0:
                        scale_factor = earliest_bx_oi / ref_ext_oi
                        df_ext_before["openInterest"] = df_ext_before["openInterest"] * scale_factor
                    df_ext_before["openInterestVal"] = df_ext_before["openInterest"]
                    df_ext_before["markPrice"] = 0.0
                    df_ext_before["datetime_jst"] = pd.to_datetime(df_ext_before["timestamp"], unit="ms", utc=True).dt.tz_convert(JST).dt.strftime("%Y-%m-%d %H:%M:%S")
                    cols = ["timestamp", "datetime_jst", "openInterest", "openInterestVal", "markPrice", "fundingRate"]
                    for c in cols:
                        if c not in df_bingx.columns:
                            df_bingx[c] = 0.0
                    df_combined = pd.concat([df_ext_before[cols], df_bingx[cols]], ignore_index=True)
                    df_combined = df_combined.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp").reset_index(drop=True)
                    df_bingx = df_combined
            else:
                df_ext["openInterestVal"] = df_ext["openInterest"]
                df_ext["markPrice"] = 0.0
                df_ext["datetime_jst"] = pd.to_datetime(df_ext["timestamp"], unit="ms", utc=True).dt.tz_convert(JST).dt.strftime("%Y-%m-%d %H:%M:%S")
                cols = ["timestamp", "datetime_jst", "openInterest", "openInterestVal", "markPrice", "fundingRate"]
                df_bingx = df_ext[cols].copy()

            # 補完済み時系列を oi_store に保存
            try:
                ticker = clean_sym.replace("-USDT", "")
                for fname in (f"{clean_sym}_oi.csv", f"{ticker}_oi.csv"):
                    df_bingx.to_csv(OI_STORE_DIR / fname, index=False, encoding="utf-8")
            except Exception:
                pass

    if df_bingx.empty:
        return pd.DataFrame()

    df_out = df_bingx.sort_values("timestamp").reset_index(drop=True)
    # 4時間前 (timestamp - 4h) の OI と比較して oi_chg_4h (%) を正確に算出 (時間間隔が不均一でも正確に4時間前を参照)
    df_ref = df_out[["timestamp", "openInterest"]].copy().rename(columns={"timestamp": "ref_ts", "openInterest": "oi_4h_ago"})
    df_out["target_4h_ts"] = df_out["timestamp"] - (4 * 3600 * 1000)
    df_merged_4h = pd.merge_asof(
        df_out.sort_values("target_4h_ts"),
        df_ref.sort_values("ref_ts"),
        left_on="target_4h_ts",
        right_on="ref_ts",
        direction="backward"
    ).sort_values("timestamp").reset_index(drop=True)

    oi_now = df_merged_4h["openInterest"].astype(float)
    oi_prev = df_merged_4h["oi_4h_ago"].astype(float)
    df_merged_4h["oi_chg_4h"] = np.where(oi_prev > 0, (oi_now - oi_prev) / oi_prev * 100.0, 0.0)
    df_merged_4h["fundingRate"] = pd.to_numeric(df_merged_4h["fundingRate"], errors="coerce").fillna(0.0001)

    res_df = df_merged_4h[["timestamp", "openInterest", "oi_chg_4h", "fundingRate"]].copy()
    _OI_FR_MEM_CACHE[clean_sym] = (now_sec, res_df)
    return res_df


def enrich_candles_with_oi_fr(df_candles: pd.DataFrame, symbol: str, auto_sync: bool = True) -> pd.DataFrame:
    """
    任意のローソク足 DataFrame (5m, 15m, 30m, 60m) に timestamp 基準 (merge_asof backward) で
    openInterest, oi_chg_4h (%), fundingRate (8h率) を結合して返す。
    """
    if df_candles is None or df_candles.empty or not symbol:
        return df_candles
    if "oi_chg_4h" in df_candles.columns and "fundingRate" in df_candles.columns:
        return df_candles

    try:
        clean_sym = normalize_sym(symbol)
        cached = _OI_FR_MEM_CACHE.get(clean_sym)
        if not auto_sync and cached is not None and not cached[1].empty:
            df_oifr = cached[1]
        else:
            df_oifr = sync_oi_fr_history_for_symbol(symbol, target_days=35)
        if df_oifr.empty:
            out = df_candles.copy()
            out["oi_chg_4h"] = 0.0
            out["fundingRate"] = 0.0001
            return out

        out = df_candles.copy()
        ts_col = out["timestamp"]
        if pd.api.types.is_numeric_dtype(ts_col):
            out["_ts_ms"] = ts_col.astype(np.int64)
        else:
            out["_ts_ms"] = pd.to_datetime(ts_col, utc=True).astype("int64") // 10**6

        out = out.sort_values("_ts_ms").reset_index(drop=True)
        oifr_sorted = df_oifr.sort_values("timestamp").reset_index(drop=True)

        merged = pd.merge_asof(
            out,
            oifr_sorted,
            left_on="_ts_ms",
            right_on="timestamp",
            direction="backward",
            suffixes=("", "_oifr")
        )
        merged["oi_chg_4h"] = pd.to_numeric(merged["oi_chg_4h"], errors="coerce").fillna(0.0)
        merged["fundingRate"] = pd.to_numeric(merged["fundingRate"], errors="coerce").fillna(0.0001)
        if "openInterest" in merged.columns:
            merged["openInterest"] = pd.to_numeric(merged["openInterest"], errors="coerce").ffill().fillna(0.0)

        drop_cols = [c for c in ("_ts_ms", "timestamp_oifr") if c in merged.columns]
        if drop_cols:
            merged = merged.drop(columns=drop_cols)
        return merged
    except Exception:
        out = df_candles.copy()
        if "oi_chg_4h" not in out.columns:
            out["oi_chg_4h"] = 0.0
        if "fundingRate" not in out.columns:
            out["fundingRate"] = 0.0001
        return out


if __name__ == "__main__":
    print("[BingX_OI] BingX OI & FR スナップショット収集開始...")
    cnt = collect_and_save_oi_snapshot()
    print(f"[BingX_OI] {cnt} 銘柄の建玉・FRスナップショットを Data/oi_store/ へ保存しました。")
