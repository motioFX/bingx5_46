#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monthly_key_levels.py
先月（前月）の高値・安値・終値（PMH / PML / PMC）を日足・時系列データから自動集計・記憶（永続化）し、
ノーマライズチャート（起点1.0）上での反発位置・比率（Bounce Position）を提供するモジュール。
BingX USDT無期限先物版 (PROJECT_RULES.md 準拠)
"""

import os
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple
import pandas as pd

JST = timezone(pd.Timedelta(hours=9))


def normalize_sym(s: str) -> str:
    sym = str(s).strip().upper()
    if sym.endswith("USDT") and "-" not in sym:
        sym = sym[:-4] + "-USDT"
    elif not sym.endswith("-USDT"):
        sym = sym + "-USDT"
    return sym


def get_previous_month_range(now_dt: Optional[datetime] = None) -> Tuple[str, int, int]:
    """指定日時（デフォルト現在JST）から先月（前月）の年月文字列、開始日時、終了日時を取得"""
    dt = now_dt if now_dt is not None else datetime.now(JST)
    first_of_this_month = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_of_prev_month = first_of_this_month - pd.Timedelta(days=1)
    prev_year = last_of_prev_month.year
    prev_month = last_of_prev_month.month
    prev_month_str = f"{prev_year:04d}-{prev_month:02d}"
    return prev_month_str, prev_year, prev_month


def calculate_monthly_levels_for_symbol(
    symbol: str,
    data_dir: Path,
    prev_year: int,
    prev_month: int
) -> Optional[Dict[str, float]]:
    """
    指定銘柄の 5分足データ（candles_5m_{sym}.csv）等から先月の高値・安値・終値を算出
    """
    clean_sym = normalize_sym(symbol)
    ticker = clean_sym.replace("-USDT", "")

    candidate_files = [
        data_dir / f"candles_5m/candles_5m_{clean_sym}.csv",
        data_dir / f"candles_5m/candles_5m_{ticker}.csv",
        data_dir / f"daily_{clean_sym}.csv",
        data_dir / f"daily_{ticker}.csv",
        data_dir / f"merged_{clean_sym}.csv",
        data_dir / f"merged_{ticker}.csv",
    ]

    df_pm = None
    for c_file in candidate_files:
        if c_file.exists():
            try:
                df = pd.read_csv(c_file)
                if not df.empty and "timestamp" in df.columns:
                    df["ts"] = pd.to_datetime(df["timestamp"], utc=True)
                    sub = df[(df["ts"].dt.year == prev_year) & (df["ts"].dt.month == prev_month)]
                    if not sub.empty:
                        df_pm = sub
                        break
            except Exception:
                pass

    if df_pm is None or df_pm.empty:
        return None

    high_col = "high" if "high" in df_pm.columns else "close"
    low_col = "low" if "low" in df_pm.columns else "close"
    close_col = "close"

    pmh = float(df_pm[high_col].max())
    pml = float(df_pm[low_col].min())
    pmc = float(df_pm[close_col].iloc[-1])

    return {
        "prev_month_high": pmh,
        "prev_month_low": pml,
        "prev_month_close": pmc,
        "sample_count": len(df_pm)
    }


def get_or_update_monthly_key_levels(
    symbols: List[str],
    data_dir: Path,
    force_refresh: bool = False
) -> Dict[str, Any]:
    """
    全対象銘柄の先月キーレベル（PMH/PML/PMC）を取得・永続保存（Data/monthly_key_levels.json）。
    """
    levels_file = data_dir / "monthly_key_levels.json"
    prev_month_str, prev_year, prev_month = get_previous_month_range()

    cache_data = {}
    if levels_file.exists() and not force_refresh:
        try:
            with open(levels_file, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
            if cache_data.get("target_month") == prev_month_str and "levels" in cache_data:
                cached_symbols = set(cache_data["levels"].keys())
                norm_symbols = {normalize_sym(s) for s in symbols}
                if norm_symbols.issubset(cached_symbols):
                    return cache_data
        except Exception:
            cache_data = {}

    levels = cache_data.get("levels", {})

    for sym in symbols:
        ns = normalize_sym(sym)
        if ns in levels and not force_refresh:
            continue
        res = calculate_monthly_levels_for_symbol(ns, data_dir, prev_year, prev_month)
        if res:
            levels[ns] = res
            # ティッカー短縮キーも念のため登録
            levels[ns.replace("-USDT", "")] = res

    result = {
        "target_month": prev_month_str,
        "updated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
        "levels": levels
    }

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with open(levels_file, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[monthly_key_levels] 保存エラー: {e}")

    return result


def compute_bounce_metrics(
    symbol: str,
    base_price_30d: float,
    current_price: float,
    monthly_levels: Dict[str, Any]
) -> Dict[str, float]:
    """
    30dノーマライズ起点価格（base_price_30d = 1.0）および現在価格から、
    先月高安（PMH/PML）の正規化ライン位置および安値反発率（Bounce %）を算出。
    """
    ns = normalize_sym(symbol)
    ticker = ns.replace("-USDT", "")
    levels_dict = monthly_levels.get("levels", {})
    sym_levels = levels_dict.get(ns, levels_dict.get(ticker, {}))

    if not sym_levels:
        return {
            "pmh": base_price_30d,
            "pml": base_price_30d,
            "pmh_norm": 1.0,
            "pml_norm": 1.0,
            "bounce_from_pml_pct": 0.0,
            "range_pos_pct": 50.0
        }

    pmh = sym_levels.get("prev_month_high", base_price_30d)
    pml = sym_levels.get("prev_month_low", base_price_30d)

    pmh_norm = (pmh / base_price_30d) if base_price_30d > 0 else 1.0
    pml_norm = (pml / base_price_30d) if base_price_30d > 0 else 1.0

    bounce_pct = ((current_price - pml) / pml * 100.0) if pml > 0 else 0.0
    range_width = (pmh - pml)
    range_pos = ((current_price - pml) / range_width * 100.0) if range_width > 0 else 50.0

    return {
        "pmh": pmh,
        "pml": pml,
        "pmh_norm": pmh_norm,
        "pml_norm": pml_norm,
        "bounce_from_pml_pct": bounce_pct,
        "range_pos_pct": range_pos
    }
