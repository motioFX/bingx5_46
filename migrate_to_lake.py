# -*- coding: utf-8 -*-
"""
既存ローカルデータ（candles_5m, binance_candles_5m_4y, oi_store, regime_history, real_trades_history）
を Data/lake/bingx_lake.db へ無損失で統合・移行し、過去不足分（1000PEPE/WIF・直近ギャップ等）と
全4時間軸（5m/15m/1h/4h）を確立するマイグレーションスクリプト。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from candle_5m_store import fetch_bingx_5m_candles, fetch_bybit_5m_candles, fetch_okx_5m_candles
from lake_store import BASE_DIR, LakeStore, sec_to_iso, to_utc_sec_series, utc_now_sec
from symbol_universe import LAKE_UNIVERSE_SYMBOLS, normalize_symbol, to_base_ticker

DATA_DIR = BASE_DIR / "Data"
JST = timezone(timedelta(hours=9))


def _parse_semicolon_list(val: Any) -> List[str]:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return []
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return []
    if s.startswith("["):
        try:
            loaded = json.loads(s)
            if isinstance(loaded, list):
                return [str(x).strip() for x in loaded if str(x).strip()]
        except Exception:
            pass
    return [p.strip() for p in s.replace(",", ";").split(";") if p.strip()]


def _normalize_external_scale(df_ext: pd.DataFrame, df_ref: pd.DataFrame) -> pd.DataFrame:
    """外部足 (OKX/Bybit/Binance) と BingX 足の重複境界で価格・出来高スケール (例: 1000PEPE vs PEPE) を自動整合する。"""
    if df_ext is None or df_ext.empty or df_ref is None or df_ref.empty:
        return df_ext
    ext = df_ext.copy()
    ref = df_ref.copy()
    ext["_ts"] = to_utc_sec_series(ext["timestamp"])
    ref["_ts"] = to_utc_sec_series(ref["timestamp"])
    joined = pd.merge(ext[["_ts", "close", "volume"]], ref[["_ts", "close", "volume"]], on="_ts", suffixes=("_ext", "_ref"))
    if joined.empty:
        # 時間重複がない場合も、近接境界 (24時間以内) で桁違い (10x以上) だけは補正
        ref_med = float(ref["close"].head(50).median())
        ext_med = float(ext["close"].tail(50).median())
        if ref_med > 0 and ext_med > 0:
            raw_ratio = ref_med / ext_med
            if raw_ratio > 5.0 or raw_ratio < 0.2:
                # 10の累乗近似（例: 1000.0）
                pow10 = round(np.log10(raw_ratio))
                scale = float(10 ** pow10)
                for c in ("open", "high", "low", "close"):
                    ext[c] = pd.to_numeric(ext[c], errors="coerce") * scale
                ext["volume"] = pd.to_numeric(ext["volume"], errors="coerce") / scale
        return ext.drop(columns=["_ts"], errors="ignore")

    r_px_list = joined["close_ref"].astype(float) / joined["close_ext"].astype(float).replace(0, np.nan)
    r_px = float(r_px_list.dropna().median())
    if np.isfinite(r_px) and r_px > 0 and abs(r_px - 1.0) > 0.015:
        for c in ("open", "high", "low", "close"):
            ext[c] = pd.to_numeric(ext[c], errors="coerce") * r_px
        ext["volume"] = pd.to_numeric(ext["volume"], errors="coerce") / r_px
    return ext.drop(columns=["_ts"], errors="ignore")


def migrate_candles_for_symbol(
    lake: LakeStore,
    symbol: str,
    history_days: int = 210,
    sync_live: bool = True,
    now_sec: Optional[int] = None,
) -> Dict[str, Any]:
    norm_sym = normalize_symbol(symbol)
    ticker = to_base_ticker(norm_sym)
    cutoff = int(now_sec) if now_sec is not None else utc_now_sec()
    target_start_sec = cutoff - (history_days + 10) * 86400

    # 1. Data/binance_candles_5m_4y/{TICKER}USDT_5m.csv
    b4y_path = DATA_DIR / "binance_candles_5m_4y" / f"{ticker}USDT_5m.csv"
    df_b4y = pd.DataFrame()
    if b4y_path.exists():
        try:
            df_b4y = pd.read_csv(b4y_path, encoding="utf-8")
            if not df_b4y.empty:
                df_b4y["_ts"] = to_utc_sec_series(df_b4y["timestamp"])
                df_b4y = df_b4y[df_b4y["_ts"] >= target_start_sec].drop(columns=["_ts"]).reset_index(drop=True)
        except Exception as e:
            print(f"[Migrate][{norm_sym}] binance_4y 読込警告: {e}")

    # 2. Data/candles_5m/candles_5m_{SYM}.csv
    c5m_path = DATA_DIR / "candles_5m" / f"candles_5m_{norm_sym}.csv"
    df_c5m = pd.DataFrame()
    if c5m_path.exists():
        try:
            df_c5m = pd.read_csv(c5m_path, encoding="utf-8")
        except Exception as e:
            print(f"[Migrate][{norm_sym}] candles_5m 読込警告: {e}")

    if not df_b4y.empty:
        if not df_c5m.empty:
            df_b4y = _normalize_external_scale(df_b4y, df_c5m)
        lake.upsert_candles(norm_sym, "5m", df_b4y, source="binance_4y", cutoff_utc=cutoff)

    if not df_c5m.empty:
        lake.upsert_candles(norm_sym, "5m", df_c5m, source="bingx_csv", cutoff_utc=cutoff)

    # 3. オンライン補完: (a) 過去不足 (history_days 未満) の OKX/Bybit バックフィル, (b) 最新までの BingX 差分追記
    if sync_live:
        stats = lake.get_candle_stats(norm_sym, "5m")
        min_ts = stats["min_ts_utc"]
        max_ts = stats["max_ts_utc"]
        ref_df = df_c5m if not df_c5m.empty else lake.load_candles(norm_sym, "5m", cutoff_utc=cutoff, limit=500)

        # (a) 過去側不足分（例: 1000PEPE-USDT, WIF-USDT は binance_4y 不在のため OKX/Bybit から取得）
        if min_ts is None or min_ts > (target_start_sec + 7 * 86400):
            back_end_ms = (min_ts * 1000) if min_ts is not None else (cutoff * 1000)
            back_start_ms = target_start_sec * 1000
            try:
                df_okx = fetch_okx_5m_candles(norm_sym, start_ms=back_start_ms, end_ms=back_end_ms)
                if not df_okx.empty:
                    df_okx = _normalize_external_scale(df_okx, ref_df)
                    lake.upsert_candles(norm_sym, "5m", df_okx, source="okx_backfill", cutoff_utc=cutoff)
            except Exception as e:
                print(f"[Migrate][{norm_sym}] OKX 過去補完警告: {e}")

            stats_after_okx = lake.get_candle_stats(norm_sym, "5m")
            if stats_after_okx["min_ts_utc"] is None or stats_after_okx["min_ts_utc"] > (target_start_sec + 14 * 86400):
                try:
                    by_end_ms = (stats_after_okx["min_ts_utc"] * 1000) if stats_after_okx["min_ts_utc"] else (cutoff * 1000)
                    df_bybit = fetch_bybit_5m_candles(norm_sym, start_ms=back_start_ms, end_ms=by_end_ms)
                    if not df_bybit.empty:
                        df_bybit = _normalize_external_scale(df_bybit, ref_df)
                        lake.upsert_candles(norm_sym, "5m", df_bybit, source="bybit_backfill", cutoff_utc=cutoff)
                except Exception as e:
                    print(f"[Migrate][{norm_sym}] Bybit 過去補完警告: {e}")

        # (b) 最新ギャップ補完 (例: 11/08 -> 現在まで BingX から差分取得)
        stats_curr = lake.get_candle_stats(norm_sym, "5m")
        cur_max = stats_curr["max_ts_utc"]
        fwd_start_ms = ((cur_max - 3600) * 1000) if cur_max else (cutoff - 65 * 86400) * 1000
        try:
            df_bx = fetch_bingx_5m_candles(norm_sym, start_ms=fwd_start_ms, end_ms=cutoff * 1000)
            if not df_bx.empty:
                lake.upsert_candles(norm_sym, "5m", df_bx, source="bingx", cutoff_utc=cutoff)
        except Exception as e:
            print(f"[Migrate][{norm_sym}] BingX 最新同期警告: {e}")

    # 4. 5m確定足から 15m / 1h / 4h を完全再構築
    tf_counts = lake.rebuild_higher_timeframes_from_5m(norm_sym, cutoff_utc=cutoff)
    final_stats = {tf: lake.get_candle_stats(norm_sym, tf) for tf in ("5m", "15m", "1h", "4h")}
    return {"symbol": norm_sym, "rebuilt_counts": tf_counts, "stats": final_stats}


def migrate_oi_store(lake: LakeStore, sync_live: bool = True, cutoff_utc: Optional[int] = None) -> int:
    oi_dir = DATA_DIR / "oi_store"
    cutoff = int(cutoff_utc) if cutoff_utc is not None else utc_now_sec()
    total_rows = 0
    allowed = set(LAKE_UNIVERSE_SYMBOLS)

    if oi_dir.exists():
        # {SYM}-USDT_oi.csv を後勝ちにするため短い形式を先に処理
        files = sorted(oi_dir.glob("*_oi.csv"), key=lambda p: (1 if "-USDT_oi.csv" in p.name else 0, p.name))
        for csv_path in files:
            raw_sym = csv_path.stem[:-3] if csv_path.stem.endswith("_oi") else csv_path.stem
            norm_sym = normalize_symbol(raw_sym)
            if norm_sym not in allowed:
                continue
            try:
                df = pd.read_csv(csv_path, encoding="utf-8")
                if df.empty or "timestamp" not in df.columns:
                    continue
                df_norm = pd.DataFrame({
                    "ts_utc": to_utc_sec_series(df["timestamp"]),
                    "open_interest": pd.to_numeric(df.get("openInterest"), errors="coerce"),
                    "open_interest_val": pd.to_numeric(df.get("openInterestVal"), errors="coerce"),
                    "mark_price": pd.to_numeric(df.get("markPrice"), errors="coerce"),
                    "funding_rate": pd.to_numeric(df.get("fundingRate"), errors="coerce"),
                })
                src = "bingx_oi_csv" if "-USDT_oi.csv" in csv_path.name else "legacy_oi_csv"
                total_rows += lake.upsert_oi_fr(norm_sym, df_norm, source=src, cutoff_utc=cutoff)
            except Exception as e:
                print(f"[Migrate][OI] {csv_path.name} 取込警告: {e}")

    if sync_live:
        try:
            from bingx_oi_collector import collect_and_save_oi_snapshot, sync_oi_fr_history_for_symbol
            collect_and_save_oi_snapshot(symbols=LAKE_UNIVERSE_SYMBOLS)
            for sym in LAKE_UNIVERSE_SYMBOLS:
                try:
                    sync_oi_fr_history_for_symbol(sym, target_days=35)
                except Exception:
                    continue
        except Exception as e:
            print(f"[Migrate][OI] オンラインOI補完警告: {e}")

    return total_rows


def migrate_regime_history(lake: LakeStore) -> int:
    path = DATA_DIR / "regime_history.csv"
    if not path.exists():
        return 0
    df = pd.read_csv(path, encoding="utf-8")
    if df.empty:
        return 0
    ts_sec = to_utc_sec_series(df["timestamp"])
    cnt = 0
    for idx, row in df.iterrows():
        t = int(ts_sec.iloc[idx])
        if t <= 0:
            continue
        rec = {
            "cutoff_utc": t,
            "cutoff_iso": sec_to_iso(t),
            "overall_regime": str(row.get("overall_regime", "NEUTRAL")),
            "regime_pattern": str(row.get("regime_pattern", "")),
            "strength_tier": str(row.get("strength_tier", "")),
            "net_score": float(row.get("net_score", 0.0) or 0.0),
            "mean_buy_votes": float(row.get("mean_buy_votes", 0.0) or 0.0),
            "mean_sell_votes": float(row.get("mean_sell_votes", 0.0) or 0.0),
            "allowed_strategies": _parse_semicolon_list(row.get("allowed_strategies")),
            "recommended_symbols": _parse_semicolon_list(row.get("recommended_symbols")),
            "excluded_symbols": _parse_semicolon_list(row.get("excluded_symbols")),
            "whale_snapshot": {"source": "regime_history.csv"},
        }
        lake.upsert_regime_snapshot(rec)
        cnt += 1
    return cnt


def migrate_live_trades(lake: LakeStore) -> int:
    path_json = DATA_DIR / "real_trades_history.json"
    trades: List[Dict[str, Any]] = []
    if path_json.exists():
        try:
            with open(path_json, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                trades = loaded
        except Exception as e:
            print(f"[Migrate][Trades] JSON読込警告: {e}")

    if not trades:
        return 0

    open_entries: Dict[str, Dict[str, Any]] = {}
    cnt = 0
    for ev in trades:
        sym = normalize_symbol(str(ev.get("symbol", "")))
        action = str(ev.get("action", "")).upper()
        side = str(ev.get("side", "LONG")).lower()
        ts_dt = pd.to_datetime(ev.get("timestamp"), utc=True, errors="coerce")
        if pd.isna(ts_dt):
            continue
        ts_sec = int(ts_dt.timestamp())
        px = float(ev.get("price", 0.0) or 0.0)
        qty = float(ev.get("qty", 0.0) or 0.0)
        pnl = float(ev.get("pnl", 0.0) or 0.0)
        note = str(ev.get("note", ""))

        if action == "ENTRY":
            open_entries[sym] = {
                "entry_ts_utc": ts_sec,
                "entry_px": px,
                "qty": qty,
                "side": side,
                "note": note,
            }
        elif action == "CLOSE":
            ent = open_entries.pop(sym, None)
            entry_ts = int(ent["entry_ts_utc"]) if ent else ts_sec
            entry_px = float(ent["entry_px"]) if ent else px
            trade_qty = float(ent["qty"]) if ent and ent["qty"] > 0 else qty
            notional = round(entry_px * trade_qty, 4) if entry_px > 0 else round(px * trade_qty, 4)
            dt_jst = datetime.fromtimestamp(entry_ts, tz=timezone.utc).astimezone(JST).strftime("%Y-%m-%d")
            raw_key = f"{sym}|{entry_ts}|{ts_sec}|{side}|{px:.8f}"
            tid = "legacy_" + hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:16]
            lake.record_live_trade(
                {
                    "trade_id": tid,
                    "selection_id": None,
                    "run_id": None,
                    "trade_date_jst": dt_jst,
                    "symbol": sym,
                    "strategy": "legacy",
                    "side": side,
                    "timeframe": "unknown",
                    "signal_ts_utc": None,
                    "entry_ts_utc": entry_ts,
                    "entry_px": entry_px,
                    "qty": trade_qty,
                    "notional_usdt": notional,
                    "exit_ts_utc": ts_sec,
                    "exit_px": px,
                    "exit_reason": note,
                    "gross_pnl_usdt": None,
                    "fee_usdt": None,
                    "net_pnl_usdt": pnl,
                    "ext_pct_at_entry": None,
                    "ext_atr_at_entry": None,
                    "meta": {"legacy_import": True, "note": note},
                }
            )
            cnt += 1
    return cnt


def run_migration(
    history_days: int = 210,
    sync_live: bool = True,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    t0 = time.time()
    lake = LakeStore(db_path=db_path)
    now_sec = utc_now_sec()
    print(f"=== [Lake Migration] DB: {lake.db_path} | history_days={history_days} | sync_live={sync_live} ===")

    candle_summaries = []
    for sym in LAKE_UNIVERSE_SYMBOLS:
        res = migrate_candles_for_symbol(lake, sym, history_days=history_days, sync_live=sync_live, now_sec=now_sec)
        s5 = res["stats"]["5m"]
        s15 = res["stats"]["15m"]
        s1h = res["stats"]["1h"]
        s4h = res["stats"]["4h"]
        print(
            f"  - {sym:13s} | 5m={s5['count']:6d} ({str(s5['min_iso'])[:10]}..{str(s5['max_iso'])[:10]}) "
            f"| 15m={s15['count']:5d} | 1h={s1h['count']:4d} | 4h={s4h['count']:4d}"
        )
        candle_summaries.append(res)

    oi_count = migrate_oi_store(lake, sync_live=sync_live, cutoff_utc=now_sec)
    regime_count = migrate_regime_history(lake)
    live_count = migrate_live_trades(lake)
    exported = lake.export_audit_csvs()

    elapsed = time.time() - t0
    print(
        f"=== [Lake Migration 完了] ({elapsed:.1f}s) | OI取込: {oi_count}行 | 地合い履歴: {regime_count}件 | 実トレード履歴: {live_count}件 ==="
    )
    return {
        "db_path": str(lake.db_path),
        "symbols_count": len(candle_summaries),
        "oi_upserted": oi_count,
        "regime_snapshots": regime_count,
        "live_trades_imported": live_count,
        "exports": exported,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="bingx5_46 Data Lake Migration Tool")
    parser.add_argument("--history-days", type=int, default=210, help="最低保持・取込目標日数 (デフォルト: 210)")
    parser.add_argument("--offline", action="store_true", help="外部API通信を行わずローカルファイルのみを取り込む")
    parser.add_argument("--db-path", type=str, default=None, help="テスト用DBパス上書き")
    args = parser.parse_args()

    db_p = Path(args.db_path) if args.db_path else None
    run_migration(history_days=args.history_days, sync_live=(not args.offline), db_path=db_p)


if __name__ == "__main__":
    main()
