# -*- coding: utf-8 -*-
"""
bingx5_46 データレイク管理モジュール (`Data/lake/bingx_lake.db`)

設計原則:
1. SQLite (WALモード + 外部キー制約) を唯一の正本 (SSoT) とし、追記型・無欠損で運用する。
2. すべての時刻は UTC エポック秒 (INTEGER) を基本とし、確定足のみを格納・参照する
   (`ts_utc + tf_sec <= data_cutoff_utc`)。
3. 5分足 (`5m`) から `15m`, `1h`, `4h` を決定的リサンプリングし、全4時間軸を常時保持する。
4. OI/FR は過去側 `merge_asof(direction="backward")` でのみ結合し、未来値の逆補間を禁止する。
5. `backtest_runs` -> `backtest_cells` -> `backtest_simulated_trades` -> `daily_selections` -> `live_trades`
   の完全な追跡性と再現性（オフラインレイク単体再現）を保証する。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

from symbol_universe import get_universe_records, normalize_symbol

BASE_DIR = Path(__file__).resolve().parent
LAKE_DIR = BASE_DIR / "Data" / "lake"
LAKE_EXPORTS_DIR = LAKE_DIR / "exports"
DEFAULT_LAKE_DB_PATH = LAKE_DIR / "bingx_lake.db"

TIMEFRAME_SECONDS: Dict[str, int] = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
}

SUPPORTED_TIMEFRAMES: Tuple[str, ...] = ("5m", "15m", "1h", "4h")


def utc_now_sec() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def sec_to_iso(ts_sec: int) -> str:
    return datetime.fromtimestamp(int(ts_sec), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_utc_sec_series(ts_series: pd.Series) -> pd.Series:
    """pandas 2.x の datetime 単位違いや sec/ms/us/ns 数値混在に左右されず UTC エポック秒整数へ正規化する。"""
    if len(ts_series) == 0:
        return pd.Series([], index=ts_series.index, dtype="int64")

    def _scale_numeric(num_s: pd.Series) -> pd.Series:
        vals = num_s.to_numpy(dtype=np.float64)
        scaled = np.where(
            vals >= 1e17,
            vals // 1_000_000_000,
            np.where(
                vals >= 1e14,
                vals // 1_000_000,
                np.where(vals >= 1e11, vals // 1_000, vals),
            ),
        )
        out = pd.Series(-1, index=num_s.index, dtype="int64")
        valid = np.isfinite(vals)
        out.iloc[valid] = scaled[valid].astype(np.int64)
        return out

    if pd.api.types.is_numeric_dtype(ts_series):
        return _scale_numeric(pd.to_numeric(ts_series, errors="coerce"))

    as_num = pd.to_numeric(ts_series, errors="coerce")
    if as_num.notna().all():
        return _scale_numeric(as_num)

    dt = pd.to_datetime(ts_series, utc=True, errors="coerce")
    valid = dt.notna()
    out = pd.Series(-1, index=ts_series.index, dtype="int64")
    if valid.any():
        ns = dt.loc[valid].dt.as_unit("ns").astype("int64")
        out.loc[valid] = (ns // 1_000_000_000).astype("int64")
    return out


def get_git_sha_short() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(BASE_DIR),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        ).strip()
        return out or "unknown"
    except Exception:
        return "unknown"


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS universe_symbols (
    symbol TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    is_tradable INTEGER NOT NULL,
    updated_at_utc INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS market_candles (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts_utc INTEGER NOT NULL,
    ts_iso TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    quote_volume REAL NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (symbol, timeframe, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_candles_sym_tf_ts ON market_candles(symbol, timeframe, ts_utc);
CREATE INDEX IF NOT EXISTS idx_candles_tf_ts ON market_candles(timeframe, ts_utc);

CREATE TABLE IF NOT EXISTS market_oi_fr (
    symbol TEXT NOT NULL,
    ts_utc INTEGER NOT NULL,
    ts_iso TEXT NOT NULL,
    open_interest REAL,
    open_interest_val REAL,
    mark_price REAL,
    funding_rate REAL,
    source TEXT NOT NULL,
    PRIMARY KEY (symbol, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_oi_sym_ts ON market_oi_fr(symbol, ts_utc);

CREATE TABLE IF NOT EXISTS regime_snapshots (
    cutoff_utc INTEGER PRIMARY KEY,
    cutoff_iso TEXT NOT NULL,
    overall_regime TEXT NOT NULL,
    regime_pattern TEXT NOT NULL,
    strength_tier TEXT NOT NULL,
    net_score REAL NOT NULL,
    mean_buy_votes REAL NOT NULL,
    mean_sell_votes REAL NOT NULL,
    allowed_strategies_json TEXT NOT NULL,
    recommended_symbols_json TEXT NOT NULL,
    excluded_symbols_json TEXT NOT NULL,
    whale_snapshot_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY,
    run_at_utc INTEGER NOT NULL,
    data_cutoff_utc INTEGER NOT NULL,
    opt_start_utc INTEGER NOT NULL,
    opt_end_utc INTEGER NOT NULL,
    eval_start_utc INTEGER NOT NULL,
    eval_end_utc INTEGER NOT NULL,
    embargo_bars INTEGER NOT NULL DEFAULT 0,
    regime TEXT NOT NULL,
    strength_tier TEXT NOT NULL,
    allowed_strategies_json TEXT NOT NULL,
    fee_maker REAL NOT NULL,
    fee_taker REAL NOT NULL,
    code_git_sha TEXT NOT NULL,
    config_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_cells (
    cell_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    params_json TEXT NOT NULL,
    opt_trades INTEGER NOT NULL,
    opt_win_rate REAL NOT NULL,
    opt_net_pnl REAL NOT NULL,
    opt_ev REAL NOT NULL,
    opt_pf REAL NOT NULL,
    opt_max_dd REAL NOT NULL,
    eval_trades INTEGER NOT NULL,
    eval_wins INTEGER NOT NULL,
    eval_win_rate REAL NOT NULL,
    eval_gross_pnl REAL NOT NULL,
    eval_total_fees REAL NOT NULL,
    eval_net_pnl REAL NOT NULL,
    eval_ev REAL NOT NULL,
    eval_pf REAL NOT NULL,
    eval_max_dd REAL NOT NULL,
    eval_grade TEXT NOT NULL,
    eval_ext_stats_json TEXT NOT NULL,
    is_opt_best INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cells_run ON backtest_cells(run_id);
CREATE INDEX IF NOT EXISTS idx_cells_lookup ON backtest_cells(run_id, symbol, strategy, timeframe);

CREATE TABLE IF NOT EXISTS backtest_simulated_trades (
    sim_trade_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    cell_id TEXT NOT NULL REFERENCES backtest_cells(cell_id) ON DELETE CASCADE,
    window_type TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    signal_ts_utc INTEGER NOT NULL,
    order_ts_utc INTEGER NOT NULL,
    order_px REAL NOT NULL,
    fill_ts_utc INTEGER,
    fill_px REAL,
    cancel_ts_utc INTEGER,
    cancel_reason TEXT,
    exit_ts_utc INTEGER,
    exit_px REAL,
    exit_reason TEXT,
    qty REAL NOT NULL,
    pos_cost_usdt REAL NOT NULL,
    gross_pnl REAL NOT NULL,
    entry_fee_usdt REAL NOT NULL,
    exit_fee_usdt REAL NOT NULL,
    net_pnl REAL NOT NULL,
    ext_pct REAL,
    ext_atr REAL,
    va_upper REAL,
    va_lower REAL,
    range_high REAL,
    range_low REAL
);
CREATE INDEX IF NOT EXISTS idx_simtrades_cell ON backtest_simulated_trades(cell_id);
CREATE INDEX IF NOT EXISTS idx_simtrades_run ON backtest_simulated_trades(run_id);

CREATE TABLE IF NOT EXISTS daily_selections (
    selection_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    selected_at_utc INTEGER NOT NULL,
    trade_date_jst TEXT NOT NULL,
    regime TEXT NOT NULL,
    winner_symbol TEXT,
    winner_strategy TEXT,
    winner_side TEXT,
    winner_timeframe TEXT,
    winner_params_json TEXT,
    winner_cell_id TEXT REFERENCES backtest_cells(cell_id),
    eval_trades INTEGER,
    eval_ev REAL,
    eval_net_pnl REAL,
    eval_pf REAL,
    eval_win_rate REAL,
    eval_grade TEXT,
    status TEXT NOT NULL,
    reason_text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_selections_date ON daily_selections(trade_date_jst, selected_at_utc);
CREATE INDEX IF NOT EXISTS idx_selections_run ON daily_selections(run_id);

CREATE TABLE IF NOT EXISTS live_trades (
    trade_id TEXT PRIMARY KEY,
    selection_id TEXT REFERENCES daily_selections(selection_id),
    run_id TEXT REFERENCES backtest_runs(run_id),
    trade_date_jst TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    signal_ts_utc INTEGER,
    entry_ts_utc INTEGER NOT NULL,
    entry_px REAL NOT NULL,
    qty REAL NOT NULL,
    notional_usdt REAL NOT NULL,
    exit_ts_utc INTEGER,
    exit_px REAL,
    exit_reason TEXT,
    gross_pnl_usdt REAL,
    fee_usdt REAL,
    net_pnl_usdt REAL,
    ext_pct_at_entry REAL,
    ext_atr_at_entry REAL,
    meta_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_live_trades_sel ON live_trades(selection_id);
CREATE INDEX IF NOT EXISTS idx_live_trades_date ON live_trades(trade_date_jst);
"""


class LakeStore:
    """bingx5_46 SQLite データレイクの統合管理クラス。"""

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_LAKE_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def _conn(self, write: bool = True):
        conn = sqlite3.connect(str(self.db_path), timeout=60.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            yield conn
            if write:
                conn.commit()
        except Exception:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._conn(write=True) as conn:
            conn.executescript(_SCHEMA_SQL)
        self.sync_universe_symbols()

    def sync_universe_symbols(self) -> None:
        now_sec = utc_now_sec()
        rows = [
            (str(r["symbol"]), str(r["role"]), int(r["is_tradable"]), now_sec)
            for r in get_universe_records()
        ]
        with self._conn(write=True) as conn:
            conn.executemany(
                """
                INSERT INTO universe_symbols (symbol, role, is_tradable, updated_at_utc)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    role=excluded.role,
                    is_tradable=excluded.is_tradable,
                    updated_at_utc=excluded.updated_at_utc
                """,
                rows,
            )

    # ------------------------------------------------------------------
    # Candles (5m / 15m / 1h / 4h) - strictly closed bars & append-only
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_candle_df(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        work = df.copy()
        if "ts_utc" not in work.columns:
            if "timestamp" in work.columns:
                work["ts_utc"] = to_utc_sec_series(work["timestamp"])
            elif "open_time" in work.columns:
                ot = pd.to_numeric(work["open_time"], errors="coerce")
                # ms -> sec if >= 1e12
                work["ts_utc"] = np.where(ot > 1e12, ot // 1000, ot).astype("int64")
            else:
                raise ValueError("DataFrame には timestamp または ts_utc カラムが必要です。")
        else:
            work["ts_utc"] = pd.to_numeric(work["ts_utc"], errors="coerce")

        for col in ("open", "high", "low", "close", "volume"):
            work[col] = pd.to_numeric(work[col], errors="coerce")
        if "quote_volume" not in work.columns:
            work["quote_volume"] = work["close"] * work["volume"]
        else:
            work["quote_volume"] = pd.to_numeric(work["quote_volume"], errors="coerce")
            work["quote_volume"] = work["quote_volume"].fillna(work["close"] * work["volume"])

        work = work.dropna(subset=["ts_utc", "open", "high", "low", "close", "volume"]).copy()
        work["ts_utc"] = work["ts_utc"].astype("int64")
        work = work[work["ts_utc"] > 0].drop_duplicates(subset=["ts_utc"], keep="last").sort_values("ts_utc")
        return work

    def upsert_candles(
        self,
        symbol: str,
        timeframe: str,
        df: pd.DataFrame,
        source: str = "bingx",
        cutoff_utc: Optional[int] = None,
        prefer_higher_priority: bool = True,
    ) -> int:
        """確定足のみを追記・更新する。`cutoff_utc` 未指定時は現在時刻を上限とする。"""
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        norm_sym = normalize_symbol(symbol)
        work = self._normalize_candle_df(df)
        if work.empty:
            return 0

        tf_sec = TIMEFRAME_SECONDS[timeframe]
        max_close_utc = int(cutoff_utc) if cutoff_utc is not None else utc_now_sec()
        # 未確定足を書き込み段階で構造的に遮断する
        work = work[(work["ts_utc"] + tf_sec) <= max_close_utc]
        if work.empty:
            return 0

        rows = [
            (
                norm_sym,
                timeframe,
                int(r.ts_utc),
                sec_to_iso(int(r.ts_utc)),
                float(r.open),
                float(r.high),
                float(r.low),
                float(r.close),
                float(r.volume),
                float(r.quote_volume),
                str(source),
            )
            for r in work.itertuples(index=False)
        ]

        sql = (
            """
            INSERT INTO market_candles
                (symbol, timeframe, ts_utc, ts_iso, open, high, low, close, volume, quote_volume, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, timeframe, ts_utc) DO UPDATE SET
                ts_iso=excluded.ts_iso,
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume,
                quote_volume=excluded.quote_volume,
                source=excluded.source
            WHERE market_candles.source != 'bingx' OR excluded.source = 'bingx'
            """
            if prefer_higher_priority
            else """
            INSERT INTO market_candles
                (symbol, timeframe, ts_utc, ts_iso, open, high, low, close, volume, quote_volume, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, timeframe, ts_utc) DO UPDATE SET
                ts_iso=excluded.ts_iso,
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume,
                quote_volume=excluded.quote_volume,
                source=excluded.source
            """
        )

        with self._conn(write=True) as conn:
            conn.executemany(sql, rows)
        return len(rows)

    def rebuild_higher_timeframes_from_5m(
        self,
        symbol: str,
        cutoff_utc: Optional[int] = None,
        since_ts_utc: Optional[int] = None,
        timeframes: Sequence[str] = ("15m", "1h", "4h"),
    ) -> Dict[str, int]:
        """確定済み 5m 足から上位足 (15m / 1h / 4h) を決定的に生成・格納する。"""
        norm_sym = normalize_symbol(symbol)
        eff_cutoff = int(cutoff_utc) if cutoff_utc is not None else utc_now_sec()
        df_5m = self.load_candles(norm_sym, "5m", cutoff_utc=eff_cutoff, start_utc=since_ts_utc)
        counts: Dict[str, int] = {tf: 0 for tf in timeframes}
        if df_5m.empty:
            return counts

        # 5m 最新確定足より先は生成禁止
        latest_5m_close = int(df_5m["ts_utc"].max()) + 300
        strict_close_cap = min(eff_cutoff, latest_5m_close)

        ts_arr = df_5m["ts_utc"].to_numpy(dtype=np.int64)
        open_arr = df_5m["open"].to_numpy(dtype=np.float64)
        high_arr = df_5m["high"].to_numpy(dtype=np.float64)
        low_arr = df_5m["low"].to_numpy(dtype=np.float64)
        close_arr = df_5m["close"].to_numpy(dtype=np.float64)
        vol_arr = df_5m["volume"].to_numpy(dtype=np.float64)
        qvol_arr = df_5m["quote_volume"].to_numpy(dtype=np.float64)

        for tf in timeframes:
            if tf == "5m":
                continue
            tf_sec = TIMEFRAME_SECONDS[tf]
            buckets = (ts_arr // tf_sec) * tf_sec
            tmp = pd.DataFrame(
                {
                    "ts_utc": buckets,
                    "open": open_arr,
                    "high": high_arr,
                    "low": low_arr,
                    "close": close_arr,
                    "volume": vol_arr,
                    "quote_volume": qvol_arr,
                }
            )
            agg = tmp.groupby("ts_utc", sort=True, as_index=False).agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
                quote_volume=("quote_volume", "sum"),
            )
            agg = agg[(agg["ts_utc"] + tf_sec) <= strict_close_cap]
            counts[tf] = self.upsert_candles(
                norm_sym,
                tf,
                agg,
                source="derived_5m",
                cutoff_utc=strict_close_cap,
                prefer_higher_priority=False,
            )
        return counts

    def load_candles(
        self,
        symbol: str,
        timeframe: str,
        cutoff_utc: Optional[int] = None,
        start_utc: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """`ts_utc + tf_sec <= cutoff_utc` を厳守して確定足のみを取得する。"""
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        norm_sym = normalize_symbol(symbol)
        tf_sec = TIMEFRAME_SECONDS[timeframe]

        clauses = ["symbol = ?", "timeframe = ?"]
        params: List[Any] = [norm_sym, timeframe]
        if start_utc is not None:
            clauses.append("ts_utc >= ?")
            params.append(int(start_utc))
        if cutoff_utc is not None:
            clauses.append("(ts_utc + ?) <= ?")
            params.extend([tf_sec, int(cutoff_utc)])

        where_sql = " AND ".join(clauses)
        if limit is not None:
            sql = (
                f"SELECT ts_utc, ts_iso, open, high, low, close, volume, quote_volume, source "
                f"FROM (SELECT * FROM market_candles WHERE {where_sql} ORDER BY ts_utc DESC LIMIT ?) "
                f"ORDER BY ts_utc ASC"
            )
            params.append(int(limit))
        else:
            sql = (
                f"SELECT ts_utc, ts_iso, open, high, low, close, volume, quote_volume, source "
                f"FROM market_candles WHERE {where_sql} ORDER BY ts_utc ASC"
            )

        with self._conn(write=False) as conn:
            df = pd.read_sql_query(sql, conn, params=params)
        if df.empty:
            return pd.DataFrame(
                columns=["timestamp", "ts_utc", "open", "high", "low", "close", "volume", "quote_volume", "source"]
            )
        df["ts_utc"] = df["ts_utc"].astype("int64")
        df["timestamp"] = pd.to_datetime(df["ts_utc"], unit="s", utc=True)
        cols = ["timestamp", "ts_utc", "open", "high", "low", "close", "volume", "quote_volume", "source"]
        return df[cols].reset_index(drop=True)

    def get_candle_stats(self, symbol: Optional[str] = None, timeframe: str = "5m") -> Dict[str, Any]:
        norm_sym = normalize_symbol(symbol) if symbol else None
        with self._conn(write=False) as conn:
            if norm_sym:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt, MIN(ts_utc) AS min_ts, MAX(ts_utc) AS max_ts "
                    "FROM market_candles WHERE symbol = ? AND timeframe = ?",
                    (norm_sym, timeframe),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt, MIN(ts_utc) AS min_ts, MAX(ts_utc) AS max_ts "
                    "FROM market_candles WHERE timeframe = ?",
                    (timeframe,),
                ).fetchone()
        cnt = int(row["cnt"] or 0)
        min_ts = int(row["min_ts"]) if row["min_ts"] is not None else None
        max_ts = int(row["max_ts"]) if row["max_ts"] is not None else None
        return {
            "count": cnt,
            "min_ts_utc": min_ts,
            "max_ts_utc": max_ts,
            "min_iso": sec_to_iso(min_ts) if min_ts else None,
            "max_iso": sec_to_iso(max_ts) if max_ts else None,
        }

    # ------------------------------------------------------------------
    # OI & Funding Rate
    # ------------------------------------------------------------------

    def upsert_oi_fr(
        self,
        symbol: str,
        df: pd.DataFrame,
        source: str = "bingx_oi",
        cutoff_utc: Optional[int] = None,
    ) -> int:
        if df is None or df.empty:
            return 0
        norm_sym = normalize_symbol(symbol)
        work = df.copy()
        if "ts_utc" not in work.columns:
            if "timestamp" in work.columns:
                work["ts_utc"] = to_utc_sec_series(work["timestamp"])
            else:
                raise ValueError("OI DataFrame には timestamp または ts_utc が必要です。")
        else:
            work["ts_utc"] = pd.to_numeric(work["ts_utc"], errors="coerce")

        max_utc = int(cutoff_utc) if cutoff_utc is not None else utc_now_sec()
        work = work.dropna(subset=["ts_utc"]).copy()
        work["ts_utc"] = work["ts_utc"].astype("int64")
        work = work[(work["ts_utc"] > 0) & (work["ts_utc"] <= max_utc)]
        work = work.drop_duplicates(subset=["ts_utc"], keep="last").sort_values("ts_utc")
        if work.empty:
            return 0

        for c in ("open_interest", "open_interest_val", "mark_price", "funding_rate"):
            if c not in work.columns:
                work[c] = np.nan
            else:
                work[c] = pd.to_numeric(work[c], errors="coerce")

        def _f_or_none(v: Any) -> Optional[float]:
            if v is None or (isinstance(v, float) and np.isnan(v)):
                return None
            return float(v)

        rows = [
            (
                norm_sym,
                int(r.ts_utc),
                sec_to_iso(int(r.ts_utc)),
                _f_or_none(r.open_interest),
                _f_or_none(r.open_interest_val),
                _f_or_none(r.mark_price),
                _f_or_none(r.funding_rate),
                str(source),
            )
            for r in work.itertuples(index=False)
        ]

        with self._conn(write=True) as conn:
            conn.executemany(
                """
                INSERT INTO market_oi_fr
                    (symbol, ts_utc, ts_iso, open_interest, open_interest_val, mark_price, funding_rate, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, ts_utc) DO UPDATE SET
                    ts_iso=excluded.ts_iso,
                    open_interest=COALESCE(excluded.open_interest, market_oi_fr.open_interest),
                    open_interest_val=COALESCE(excluded.open_interest_val, market_oi_fr.open_interest_val),
                    mark_price=COALESCE(excluded.mark_price, market_oi_fr.mark_price),
                    funding_rate=COALESCE(excluded.funding_rate, market_oi_fr.funding_rate),
                    source=excluded.source
                """,
                rows,
            )
        return len(rows)

    def load_oi_fr(
        self,
        symbol: str,
        cutoff_utc: Optional[int] = None,
        start_utc: Optional[int] = None,
    ) -> pd.DataFrame:
        norm_sym = normalize_symbol(symbol)
        clauses = ["symbol = ?"]
        params: List[Any] = [norm_sym]
        if start_utc is not None:
            clauses.append("ts_utc >= ?")
            params.append(int(start_utc))
        if cutoff_utc is not None:
            clauses.append("ts_utc <= ?")
            params.append(int(cutoff_utc))

        sql = (
            f"SELECT ts_utc, ts_iso, open_interest, open_interest_val, mark_price, funding_rate, source "
            f"FROM market_oi_fr WHERE {' AND '.join(clauses)} ORDER BY ts_utc ASC"
        )
        with self._conn(write=False) as conn:
            df = pd.read_sql_query(sql, conn, params=params)
        if df.empty:
            return pd.DataFrame(
                columns=["timestamp", "ts_utc", "open_interest", "open_interest_val", "mark_price", "funding_rate", "source"]
            )
        df["ts_utc"] = df["ts_utc"].astype("int64")
        df["timestamp"] = pd.to_datetime(df["ts_utc"], unit="s", utc=True)
        return df

    @staticmethod
    def attach_oi_fr_causal(
        df_candles: pd.DataFrame,
        timeframe: str,
        df_oi: pd.DataFrame,
        cutoff_utc: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        厳密に因果的 (direction='backward') な OI / 4h OI変化率 / FR の結合を行う。
        各足の確定時刻 `bar_close_utc = ts_utc + tf_sec` 時点以前のみを参照し、未来値のリークを禁止する。
        """
        out = df_candles.copy()
        if out.empty:
            out["open_interest"] = np.nan
            out["funding_rate"] = np.nan
            out["oi_chg_4h"] = np.nan
            return out

        tf_sec = TIMEFRAME_SECONDS[timeframe]
        if "ts_utc" not in out.columns:
            out["ts_utc"] = to_utc_sec_series(out["timestamp"])
        out["ts_utc"] = out["ts_utc"].astype("int64")
        out["_bar_close_utc"] = out["ts_utc"] + tf_sec
        if cutoff_utc is not None:
            out["_bar_close_utc"] = np.minimum(out["_bar_close_utc"], int(cutoff_utc))

        if df_oi is None or df_oi.empty:
            out["open_interest"] = np.nan
            out["funding_rate"] = np.nan
            out["oi_chg_4h"] = np.nan
            return out.drop(columns=["_bar_close_utc"])

        oi = df_oi.copy()
        if "ts_utc" not in oi.columns:
            oi["ts_utc"] = to_utc_sec_series(oi["timestamp"])
        oi["ts_utc"] = oi["ts_utc"].astype("int64")
        if cutoff_utc is not None:
            oi = oi[oi["ts_utc"] <= int(cutoff_utc)]
        oi = oi.dropna(subset=["ts_utc"]).sort_values("ts_utc").drop_duplicates(subset=["ts_utc"], keep="last")
        if oi.empty:
            out["open_interest"] = np.nan
            out["funding_rate"] = np.nan
            out["oi_chg_4h"] = np.nan
            return out.drop(columns=["_bar_close_utc"])

        oi_curr = oi[["ts_utc", "open_interest", "funding_rate"]].rename(columns={"ts_utc": "_oi_ts"})
        merged = pd.merge_asof(
            out.sort_values("_bar_close_utc"),
            oi_curr.sort_values("_oi_ts"),
            left_on="_bar_close_utc",
            right_on="_oi_ts",
            direction="backward",
            tolerance=7200,
        )

        merged["_target_prev_ts"] = merged["_bar_close_utc"] - 14400
        oi_prev = oi[["ts_utc", "open_interest"]].rename(
            columns={"ts_utc": "_oi_prev_ts", "open_interest": "_oi_4h_ago"}
        )
        merged = pd.merge_asof(
            merged.sort_values("_target_prev_ts"),
            oi_prev.sort_values("_oi_prev_ts"),
            left_on="_target_prev_ts",
            right_on="_oi_prev_ts",
            direction="backward",
            tolerance=7200,
        )

        merged = merged.sort_values("ts_utc").reset_index(drop=True)
        oi_now = pd.to_numeric(merged["open_interest"], errors="coerce")
        oi_ago = pd.to_numeric(merged["_oi_4h_ago"], errors="coerce")
        merged["oi_chg_4h"] = np.where((oi_ago > 0) & oi_now.notna(), (oi_now - oi_ago) / oi_ago * 100.0, np.nan)
        merged["funding_rate"] = pd.to_numeric(merged["funding_rate"], errors="coerce")
        return merged.drop(
            columns=["_bar_close_utc", "_oi_ts", "_target_prev_ts", "_oi_prev_ts", "_oi_4h_ago"],
            errors="ignore",
        )

    # ------------------------------------------------------------------
    # Regime Snapshots, Backtest Runs / Cells / SimTrades / Selections / Live Trades
    # ------------------------------------------------------------------

    def upsert_regime_snapshot(self, record: Dict[str, Any]) -> None:
        cutoff_utc = int(record["cutoff_utc"])
        cutoff_iso = str(record.get("cutoff_iso") or sec_to_iso(cutoff_utc))
        row = (
            cutoff_utc,
            cutoff_iso,
            str(record["overall_regime"]),
            str(record["regime_pattern"]),
            str(record["strength_tier"]),
            float(record["net_score"]),
            float(record["mean_buy_votes"]),
            float(record["mean_sell_votes"]),
            json.dumps(record.get("allowed_strategies", []), ensure_ascii=False),
            json.dumps(record.get("recommended_symbols", []), ensure_ascii=False),
            json.dumps(record.get("excluded_symbols", {}), ensure_ascii=False),
            json.dumps(record.get("whale_snapshot", {}), ensure_ascii=False),
        )
        with self._conn(write=True) as conn:
            conn.execute(
                """
                INSERT INTO regime_snapshots (
                    cutoff_utc, cutoff_iso, overall_regime, regime_pattern, strength_tier,
                    net_score, mean_buy_votes, mean_sell_votes,
                    allowed_strategies_json, recommended_symbols_json,
                    excluded_symbols_json, whale_snapshot_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cutoff_utc) DO UPDATE SET
                    cutoff_iso=excluded.cutoff_iso,
                    overall_regime=excluded.overall_regime,
                    regime_pattern=excluded.regime_pattern,
                    strength_tier=excluded.strength_tier,
                    net_score=excluded.net_score,
                    mean_buy_votes=excluded.mean_buy_votes,
                    mean_sell_votes=excluded.mean_sell_votes,
                    allowed_strategies_json=excluded.allowed_strategies_json,
                    recommended_symbols_json=excluded.recommended_symbols_json,
                    excluded_symbols_json=excluded.excluded_symbols_json,
                    whale_snapshot_json=excluded.whale_snapshot_json
                """,
                row,
            )

    def save_backtest_bundle(
        self,
        run_row: Dict[str, Any],
        cell_rows: Sequence[Dict[str, Any]],
        sim_trade_rows: Sequence[Dict[str, Any]],
        selection_row: Optional[Dict[str, Any]] = None,
    ) -> None:
        """1回のウォークフォワード検証結果と勝者選定を単一トランザクションで原子保存する。"""
        with self._conn(write=True) as conn:
            conn.execute(
                """
                INSERT INTO backtest_runs (
                    run_id, run_at_utc, data_cutoff_utc,
                    opt_start_utc, opt_end_utc, eval_start_utc, eval_end_utc, embargo_bars,
                    regime, strength_tier, allowed_strategies_json,
                    fee_maker, fee_taker, code_git_sha, config_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    run_at_utc=excluded.run_at_utc,
                    data_cutoff_utc=excluded.data_cutoff_utc,
                    opt_start_utc=excluded.opt_start_utc,
                    opt_end_utc=excluded.opt_end_utc,
                    eval_start_utc=excluded.eval_start_utc,
                    eval_end_utc=excluded.eval_end_utc,
                    embargo_bars=excluded.embargo_bars,
                    regime=excluded.regime,
                    strength_tier=excluded.strength_tier,
                    allowed_strategies_json=excluded.allowed_strategies_json,
                    fee_maker=excluded.fee_maker,
                    fee_taker=excluded.fee_taker,
                    code_git_sha=excluded.code_git_sha,
                    config_json=excluded.config_json
                """,
                (
                    str(run_row["run_id"]),
                    int(run_row["run_at_utc"]),
                    int(run_row["data_cutoff_utc"]),
                    int(run_row["opt_start_utc"]),
                    int(run_row["opt_end_utc"]),
                    int(run_row["eval_start_utc"]),
                    int(run_row["eval_end_utc"]),
                    int(run_row.get("embargo_bars", 0)),
                    str(run_row["regime"]),
                    str(run_row["strength_tier"]),
                    str(run_row["allowed_strategies_json"]),
                    float(run_row["fee_maker"]),
                    float(run_row["fee_taker"]),
                    str(run_row.get("code_git_sha", "unknown")),
                    str(run_row["config_json"]),
                ),
            )

            if cell_rows:
                conn.executemany(
                    """
                    INSERT INTO backtest_cells (
                        cell_id, run_id, symbol, strategy, side, timeframe, params_json,
                        opt_trades, opt_win_rate, opt_net_pnl, opt_ev, opt_pf, opt_max_dd,
                        eval_trades, eval_wins, eval_win_rate, eval_gross_pnl, eval_total_fees,
                        eval_net_pnl, eval_ev, eval_pf, eval_max_dd, eval_grade,
                        eval_ext_stats_json, is_opt_best
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cell_id) DO UPDATE SET
                        opt_trades=excluded.opt_trades,
                        opt_win_rate=excluded.opt_win_rate,
                        opt_net_pnl=excluded.opt_net_pnl,
                        opt_ev=excluded.opt_ev,
                        opt_pf=excluded.opt_pf,
                        opt_max_dd=excluded.opt_max_dd,
                        eval_trades=excluded.eval_trades,
                        eval_wins=excluded.eval_wins,
                        eval_win_rate=excluded.eval_win_rate,
                        eval_gross_pnl=excluded.eval_gross_pnl,
                        eval_total_fees=excluded.eval_total_fees,
                        eval_net_pnl=excluded.eval_net_pnl,
                        eval_ev=excluded.eval_ev,
                        eval_pf=excluded.eval_pf,
                        eval_max_dd=excluded.eval_max_dd,
                        eval_grade=excluded.eval_grade,
                        eval_ext_stats_json=excluded.eval_ext_stats_json,
                        is_opt_best=excluded.is_opt_best
                    """,
                    [
                        (
                            str(c["cell_id"]),
                            str(c["run_id"]),
                            str(c["symbol"]),
                            str(c["strategy"]),
                            str(c["side"]),
                            str(c["timeframe"]),
                            str(c["params_json"]),
                            int(c["opt_trades"]),
                            float(c["opt_win_rate"]),
                            float(c["opt_net_pnl"]),
                            float(c["opt_ev"]),
                            float(c["opt_pf"]),
                            float(c["opt_max_dd"]),
                            int(c["eval_trades"]),
                            int(c["eval_wins"]),
                            float(c["eval_win_rate"]),
                            float(c["eval_gross_pnl"]),
                            float(c["eval_total_fees"]),
                            float(c["eval_net_pnl"]),
                            float(c["eval_ev"]),
                            float(c["eval_pf"]),
                            float(c["eval_max_dd"]),
                            str(c["eval_grade"]),
                            str(c["eval_ext_stats_json"]),
                            int(c["is_opt_best"]),
                        )
                        for c in cell_rows
                    ],
                )

            if sim_trade_rows:
                conn.executemany(
                    """
                    INSERT INTO backtest_simulated_trades (
                        sim_trade_id, run_id, cell_id, window_type, symbol, strategy, side, timeframe,
                        signal_ts_utc, order_ts_utc, order_px,
                        fill_ts_utc, fill_px, cancel_ts_utc, cancel_reason,
                        exit_ts_utc, exit_px, exit_reason,
                        qty, pos_cost_usdt, gross_pnl, entry_fee_usdt, exit_fee_usdt, net_pnl,
                        ext_pct, ext_atr, va_upper, va_lower, range_high, range_low
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sim_trade_id) DO NOTHING
                    """,
                    [
                        (
                            str(t["sim_trade_id"]),
                            str(t["run_id"]),
                            str(t["cell_id"]),
                            str(t["window_type"]),
                            str(t["symbol"]),
                            str(t["strategy"]),
                            str(t["side"]),
                            str(t["timeframe"]),
                            int(t["signal_ts_utc"]),
                            int(t["order_ts_utc"]),
                            float(t["order_px"]),
                            t.get("fill_ts_utc"),
                            t.get("fill_px"),
                            t.get("cancel_ts_utc"),
                            t.get("cancel_reason"),
                            t.get("exit_ts_utc"),
                            t.get("exit_px"),
                            t.get("exit_reason"),
                            float(t["qty"]),
                            float(t["pos_cost_usdt"]),
                            float(t["gross_pnl"]),
                            float(t["entry_fee_usdt"]),
                            float(t["exit_fee_usdt"]),
                            float(t["net_pnl"]),
                            t.get("ext_pct"),
                            t.get("ext_atr"),
                            t.get("va_upper"),
                            t.get("va_lower"),
                            t.get("range_high"),
                            t.get("range_low"),
                        )
                        for t in sim_trade_rows
                    ],
                )

            if selection_row is not None:
                conn.execute(
                    """
                    INSERT INTO daily_selections (
                        selection_id, run_id, selected_at_utc, trade_date_jst, regime,
                        winner_symbol, winner_strategy, winner_side, winner_timeframe,
                        winner_params_json, winner_cell_id,
                        eval_trades, eval_ev, eval_net_pnl, eval_pf, eval_win_rate, eval_grade,
                        status, reason_text
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(selection_id) DO UPDATE SET
                        status=excluded.status,
                        reason_text=excluded.reason_text
                    """,
                    (
                        str(selection_row["selection_id"]),
                        str(selection_row["run_id"]),
                        int(selection_row["selected_at_utc"]),
                        str(selection_row["trade_date_jst"]),
                        str(selection_row["regime"]),
                        selection_row.get("winner_symbol"),
                        selection_row.get("winner_strategy"),
                        selection_row.get("winner_side"),
                        selection_row.get("winner_timeframe"),
                        selection_row.get("winner_params_json"),
                        selection_row.get("winner_cell_id"),
                        selection_row.get("eval_trades"),
                        selection_row.get("eval_ev"),
                        selection_row.get("eval_net_pnl"),
                        selection_row.get("eval_pf"),
                        selection_row.get("eval_win_rate"),
                        selection_row.get("eval_grade"),
                        str(selection_row["status"]),
                        str(selection_row["reason_text"]),
                    ),
                )

    def get_run_record(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._conn(write=False) as conn:
            row = conn.execute("SELECT * FROM backtest_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def get_cells_for_run(self, run_id: str) -> List[Dict[str, Any]]:
        with self._conn(write=False) as conn:
            rows = conn.execute(
                "SELECT * FROM backtest_cells WHERE run_id = ? ORDER BY symbol, strategy, timeframe, cell_id",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_selection_for_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._conn(write=False) as conn:
            row = conn.execute(
                "SELECT * FROM daily_selections WHERE run_id = ? ORDER BY selected_at_utc DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_selection_status(self, selection_id: str, status: str, reason_suffix: Optional[str] = None) -> None:
        if not selection_id:
            return
        with self._conn(write=True) as conn:
            if reason_suffix:
                conn.execute(
                    "UPDATE daily_selections SET status = ?, reason_text = reason_text || ' | ' || ? WHERE selection_id = ?",
                    (status, reason_suffix, selection_id),
                )
            else:
                conn.execute(
                    "UPDATE daily_selections SET status = ? WHERE selection_id = ?",
                    (status, selection_id),
                )

    def record_live_trade(self, trade: Dict[str, Any]) -> None:
        """実デモトレード結果を `live_trades` へ書き戻し、紐づく `daily_selections` を EXECUTED に更新する。"""
        with self._conn(write=True) as conn:
            conn.execute(
                """
                INSERT INTO live_trades (
                    trade_id, selection_id, run_id, trade_date_jst,
                    symbol, strategy, side, timeframe, signal_ts_utc,
                    entry_ts_utc, entry_px, qty, notional_usdt,
                    exit_ts_utc, exit_px, exit_reason,
                    gross_pnl_usdt, fee_usdt, net_pnl_usdt,
                    ext_pct_at_entry, ext_atr_at_entry, meta_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    exit_ts_utc=COALESCE(excluded.exit_ts_utc, live_trades.exit_ts_utc),
                    exit_px=COALESCE(excluded.exit_px, live_trades.exit_px),
                    exit_reason=COALESCE(excluded.exit_reason, live_trades.exit_reason),
                    gross_pnl_usdt=COALESCE(excluded.gross_pnl_usdt, live_trades.gross_pnl_usdt),
                    fee_usdt=COALESCE(excluded.fee_usdt, live_trades.fee_usdt),
                    net_pnl_usdt=COALESCE(excluded.net_pnl_usdt, live_trades.net_pnl_usdt),
                    meta_json=excluded.meta_json
                """,
                (
                    str(trade["trade_id"]),
                    trade.get("selection_id"),
                    trade.get("run_id"),
                    str(trade["trade_date_jst"]),
                    normalize_symbol(str(trade["symbol"])),
                    str(trade["strategy"]),
                    str(trade["side"]),
                    str(trade["timeframe"]),
                    trade.get("signal_ts_utc"),
                    int(trade["entry_ts_utc"]),
                    float(trade["entry_px"]),
                    float(trade["qty"]),
                    float(trade["notional_usdt"]),
                    trade.get("exit_ts_utc"),
                    trade.get("exit_px"),
                    trade.get("exit_reason"),
                    trade.get("gross_pnl_usdt"),
                    trade.get("fee_usdt"),
                    trade.get("net_pnl_usdt"),
                    trade.get("ext_pct_at_entry"),
                    trade.get("ext_atr_at_entry"),
                    json.dumps(trade.get("meta", {}), ensure_ascii=False),
                ),
            )
            sel_id = trade.get("selection_id")
            if sel_id and trade.get("exit_ts_utc") is not None:
                conn.execute(
                    "UPDATE daily_selections SET status = 'EXECUTED' WHERE selection_id = ?",
                    (str(sel_id),),
                )

    def export_audit_csvs(self, out_dir: Optional[Path] = None) -> Dict[str, str]:
        """外部検収・可視化用の読み取り専用 CSV スナップショットを `Data/lake/exports/` へ出力する。"""
        target = Path(out_dir) if out_dir is not None else LAKE_EXPORTS_DIR
        target.mkdir(parents=True, exist_ok=True)
        tables = [
            "universe_symbols",
            "regime_snapshots",
            "backtest_runs",
            "backtest_cells",
            "daily_selections",
            "live_trades",
        ]
        written: Dict[str, str] = {}
        with self._conn(write=False) as conn:
            for tbl in tables:
                df = pd.read_sql_query(f"SELECT * FROM {tbl}", conn)
                path = target / f"{tbl}.csv"
                df.to_csv(path, index=False, encoding="utf-8")
                written[tbl] = str(path)
        return written
