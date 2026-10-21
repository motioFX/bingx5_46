#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bingx_oi_collector.py
BingX USDT無期限先物 建玉（Open Interest）＆ ファンディングレート（Funding Rate）定期スナップショット蓄積モジュール
(PROJECT_RULES.md 第6項 準拠)

機能:
1. BingX Swap V2 API (premiumIndex, ticker, openInterest) から固定20銘柄 + BTC-USDT の最新 OI / FR / MarkPrice を取得。
2. Data/oi_store/{symbol}_oi.csv に時系列としてアペンド保存（重複排除・UTF-8）。
3. ローソク足マージ・分析時に蓄積済み実測 OI/FR 時系列を返却。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional
import pandas as pd
import requests

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "Data"
OI_STORE_DIR = DATA_DIR / "oi_store"
OI_STORE_DIR.mkdir(parents=True, exist_ok=True)

JST = timezone(timedelta(hours=9))
BINGX_BASE_URL = "https://open-api.bingx.com"

FIXED_TARGET_SYMBOLS: List[str] = [
    "BTC-USDT",
    "HYPE-USDT", "SOL-USDT", "DOGE-USDT", "XRP-USDT", "SUI-USDT",
    "AVAX-USDT", "LINK-USDT", "APT-USDT", "NEAR-USDT", "ARB-USDT",
    "UNI-USDT", "OP-USDT", "TIA-USDT", "RENDER-USDT", "INJ-USDT",
    "FET-USDT", "TAO-USDT", "AAVE-USDT", "ENA-USDT", "ZEC-USDT"
]


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
        # 万一 "DEMO" や単一文字列が渡された場合の型防御
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
            oi_usd = oi_val * mark_px if mark_px > 0 else oi_val

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
                    df["timestamp"] = df["timestamp"].astype(int)
                    cond = (df["timestamp"] >= start_ms) & (df["timestamp"] <= end_ms)
                    return df[cond].copy().sort_values("timestamp").reset_index(drop=True)
            except Exception:
                pass
    return pd.DataFrame()


if __name__ == "__main__":
    print("[BingX_OI] BingX OI & FR スナップショット収集開始...")
    cnt = collect_and_save_oi_snapshot()
    print(f"[BingX_OI] {cnt} 銘柄の建玉・FRスナップショットを Data/oi_store/ へ保存しました。")
