#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
record_regime_history.py
過去の5分足時系列ストア（Data/candles_5m/）に基づき、
過去の各日（日次/一定間隔）における地合い判定結果と選定・除外銘柄の履歴を算出・永続化するスクリプト。
出力: Data/regime_history.csv
"""

import os
import sys
import csv
from pathlib import Path
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

# プロジェクトルート
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "Data"
CANDLES_DIR = DATA_DIR / "candles_5m"
HISTORY_FILE = DATA_DIR / "regime_history.csv"

# モジュール読み込み
sys.path.insert(0, str(BASE_DIR))
from bingx5_46_3logic import (
    evaluate_market_regime_3window,
    check_normalized_peak_timing,
    check_normalized_trough_timing
)
from candle_5m_store import (
    load_5m_candles,
    resample_from_5m,
    normalize_symbol_name,
    get_clean_symbol
)
from bingx5_46_4mix_candle_Merged_Alt10 import ALL_SYMBOLS

CSV_HEADERS = [
    "timestamp",
    "overall_regime",
    "regime_pattern",
    "strength_tier",
    "net_score",
    "mean_buy_votes",
    "mean_sell_votes",
    "allowed_strategies",
    "recommended_symbols",
    "excluded_symbols",
    "excluded_count",
    "recommended_count"
]


def evaluate_at_timestamp(all_5m_dfs: dict, cut_ts: pd.Timestamp) -> dict:
    """指定時点 cut_ts までのデータのみを用いて地合い判定と銘柄選定をシミュレート"""
    symbol_1h_dict = {}
    all_1h_dfs = []

    for sym, df_5m in all_5m_dfs.items():
        if df_5m.empty:
            continue
        # cut_ts 以前のデータのみを抽出
        sub_5m = df_5m[df_5m["timestamp"] <= cut_ts].copy()
        if len(sub_5m) < 288 * 5:  # 最低5日分以上
            continue
        
        df_1h = resample_from_5m(sub_5m, 60)
        df_1h["symbol"] = sym
        clean_sym = normalize_symbol_name(sym)
        all_1h_dfs.append(df_1h)
        symbol_1h_dict[clean_sym] = df_1h

    if len(symbol_1h_dict) < 15:
        return None

    # 地合い判定
    regime_res = evaluate_market_regime_3window(symbol_1h_dict)
    overall_reg = regime_res.get("overall_regime", "NEUTRAL")
    rec_syms = list(regime_res.get("recommended_symbols", []))
    reg_strength = regime_res.get("regime_strength", {})

    # 前半高安監査
    excluded = set()
    for d in all_1h_dfs:
        sym_name = d["symbol"].iloc[0]
        ticker = get_clean_symbol(sym_name)
        if ticker == "BTC":
            continue

        if overall_reg == "BUY":
            is_bad, _ = check_normalized_peak_timing(d)
            if is_bad:
                excluded.add(sym_name)
        elif overall_reg == "SELL":
            is_bad, _ = check_normalized_trough_timing(d)
            if is_bad:
                excluded.add(sym_name)

    final_recommended = [s for s in rec_syms if s not in excluded and get_clean_symbol(s) not in excluded]

    return {
        "timestamp": cut_ts.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "overall_regime": overall_reg,
        "regime_pattern": regime_res.get("regime_pattern", ""),
        "strength_tier": reg_strength.get("tier", "NORMAL"),
        "net_score": f"{reg_strength.get('net_score', 0.0):+.3f}",
        "mean_buy_votes": f"{reg_strength.get('mean_buy_votes', 0.0):.1f}",
        "mean_sell_votes": f"{reg_strength.get('mean_sell_votes', 0.0):.1f}",
        "allowed_strategies": ";".join(regime_res.get("allowed_strategies", [])),
        "recommended_symbols": ";".join(final_recommended),
        "excluded_symbols": ";".join(sorted(list(excluded))),
        "excluded_count": len(excluded),
        "recommended_count": len(final_recommended)
    }


def generate_regime_history(days: int = 45, step_hours: int = 24):
    """過去指定日数分の地合い履歴を生成・記録"""
    print(f"📦 全固定銘柄の 5分足ストアをロード中...")
    all_5m_dfs = {}
    for sym in ALL_SYMBOLS:
        df = load_5m_candles(sym)
        if not df.empty:
            if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            all_5m_dfs[sym] = df

    if not all_5m_dfs or "BTC-USDT" not in all_5m_dfs:
        print("❌ BTC-USDT または 5分足データが不足しています。")
        return

    btc_df = all_5m_dfs["BTC-USDT"]
    latest_ts = btc_df["timestamp"].max()
    start_ts = latest_ts - timedelta(days=days)
    
    # 評価時点リスト生成（正時 00:00 UTC）
    cur_ts = start_ts.floor("D")
    eval_timestamps = []
    while cur_ts <= latest_ts:
        eval_timestamps.append(cur_ts)
        cur_ts += timedelta(hours=step_hours)

    print(f"📊 過去 {len(eval_timestamps)} 時点 ({start_ts.strftime('%Y-%m-%d')} 〜 {latest_ts.strftime('%Y-%m-%d')}) の地合い・銘柄判定を遡及計算中...")

    records = []
    for idx, ts in enumerate(eval_timestamps, 1):
        res = evaluate_at_timestamp(all_5m_dfs, ts)
        if res:
            records.append(res)
            print(f"   [{idx}/{len(eval_timestamps)}] {res['timestamp'][:10]} | 地合い: {res['overall_regime']:7s} ({res['regime_pattern']:7s} / {res['strength_tier']:16s}) | 買{res['mean_buy_votes']} vs 売{res['mean_sell_votes']} | 推奨: {res['recommended_count']}銘柄 | 除外: {res['excluded_count']}銘柄")

    # CSV書き込み
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for r in records:
            writer.writerow(r)

    print(f"\n✅ 過去データ地合い・銘柄選定履歴の保存完了: {HISTORY_FILE} (計 {len(records)} 件)")


def append_current_regime_to_history(state_data: dict):
    """最新の地合い判定結果を regime_history.csv にアペンド追記"""
    try:
        file_exists = HISTORY_FILE.exists()
        reg_strength = state_data.get("regime_strength", {})
        rec_syms = state_data.get("recommended_symbols", [])
        exc_syms = state_data.get("excluded_symbols", [])

        row = {
            "timestamp": state_data.get("timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")),
            "overall_regime": state_data.get("overall_regime", "NEUTRAL"),
            "regime_pattern": state_data.get("regime_pattern", ""),
            "strength_tier": reg_strength.get("tier", "NORMAL"),
            "net_score": f"{reg_strength.get('net_score', 0.0):+.3f}",
            "mean_buy_votes": f"{reg_strength.get('mean_buy_votes', 0.0):.1f}",
            "mean_sell_votes": f"{reg_strength.get('mean_sell_votes', 0.0):.1f}",
            "allowed_strategies": ";".join(state_data.get("allowed_strategies", [])),
            "recommended_symbols": ";".join(rec_syms),
            "excluded_symbols": ";".join(exc_syms),
            "excluded_count": len(exc_syms),
            "recommended_count": len(rec_syms)
        }

        with open(HISTORY_FILE, "a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
        print(f"[Regime History] {row['timestamp']} の地合い結果を {HISTORY_FILE} に正常アペンドしました。")
    except Exception as e:
        print(f"[Regime History Error] 履歴アペンド失敗: {e}")


if __name__ == "__main__":
    days = 45
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            pass
    generate_regime_history(days=days, step_hours=24)
