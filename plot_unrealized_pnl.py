"""保有暗号資産 含み損益推移チャート生成スクリプト (BTC 1年分 / RENDER・NEAR 購入当初〜現在)

仕様:
1. Bitbank 長期保有 BTC (0.1434 BTC / 確定簿価 12,794,387 円):
   - Bitbank Public API から直近 1 年分の日足・9/27以降の1時間足＋現在価格を取得し、含み損益推移を算出。
2. Bitbank 現物 RENDER & Binance Japan 現物 NEAR:
   - Private API (約定履歴全件) から購入日時・数量・取得単価を時系列で復元し、
     API 取得不可環境（IP制限等）に備えて Data/trade_history_cache.json へ自動キャッシュ。
   - 購入当初 (2026-09-27) からの 1 時間足価格推移と突き合わせ、各時点の累積数量・投資元本・評価額・含み損益を厳密に計算。
3. 2 枚の 2×2 ダッシュボード PNG を Data/plots/ に保存:
   - unrealized_pnl_history_*.png : 含み損益 (円 / %) の推移 (BTC 1年 / RENDER / NEAR / 3銘柄合計)
   - portfolio_value_vs_cost_*.png: 評価額 vs 投資元本 (階段コスト) の比較推移
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import ccxt
import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import requests

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from bitbank5_46_2api import api_bitbank
from config_loader import BITBANK_PUBLIC_URL, get_webhook_url, load_binance_api_keys
from long_term_btc_tracker import KNOWN_ENTRY_PRICE, LONG_TERM_PROTECTED_BTC

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

JST = timezone(timedelta(hours=9))
TRADE_CACHE_PATH = BASE_DIR / "Data" / "trade_history_cache.json"


def configure_japanese_font() -> None:
    """Windows / Ubuntu VPS の両環境で日本語が豆腐化しないようフォントを設定"""
    candidate_paths = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
        "/usr/share/fonts/opentype/ipafont-mincho/ipam.ttf",
    ]
    for fp in candidate_paths:
        if Path(fp).exists():
            try:
                fm.fontManager.addfont(fp)
            except Exception:
                pass

    matplotlib.rcParams["font.sans-serif"] = [
        "Meiryo", "Yu Gothic", "MS Gothic",
        "Noto Sans CJK JP", "IPAGothic", "IPAMincho",
        "DejaVu Sans", "sans-serif",
    ]
    matplotlib.rcParams["axes.unicode_minus"] = False
    if "text.parse_math" in matplotlib.rcParams:
        matplotlib.rcParams["text.parse_math"] = False


def load_trade_cache() -> Dict[str, List[Dict[str, Any]]]:
    if not TRADE_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(TRADE_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_trade_cache(cache: Dict[str, List[Dict[str, Any]]]) -> None:
    try:
        TRADE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TRADE_CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        print(f"[Cache Warning] 保存失敗: {exc}")


async def fetch_bitbank_trades(pair: str = "render_jpy") -> List[Dict[str, Any]]:
    """Bitbank 約定履歴を取得（失敗時はキャッシュへフォールバック）"""
    cache = load_trade_cache()
    try:
        api = api_bitbank(pair, coin="JPY", mode="live")
        res = await api.bitbank._request_private("GET", f"/v1/user/spot/trade_history?pair={pair}&count=1000")
        if res.get("success") == 1 and "data" in res and "trades" in res["data"]:
            raw_trades = res["data"]["trades"]
            normalized = [
                {
                    "ts_ms": int(t.get("executed_at", 0)),
                    "side": str(t.get("side", "")).lower(),
                    "qty": float(t.get("amount", 0.0)),
                    "price": float(t.get("price", 0.0)),
                }
                for t in raw_trades
            ]
            normalized.sort(key=lambda x: x["ts_ms"])
            if normalized:
                cache[f"bitbank_{pair}"] = normalized
                save_trade_cache(cache)
                return normalized
    except Exception as exc:
        print(f"[Bitbank Trade History Warning] {pair}: {exc}")

    return cache.get(f"bitbank_{pair}", [])


def fetch_binance_near_trades() -> List[Dict[str, Any]]:
    """Binance Japan NEAR/JPY 約定履歴を取得（実受取数量ベース、失敗時はキャッシュ使用）"""
    cache = load_trade_cache()
    keys = load_binance_api_keys()
    if keys and len(keys) >= 2 and keys[0]:
        try:
            exchange = ccxt.binance({
                "apiKey": keys[0],
                "secret": keys[1],
                "enableRateLimit": True,
                "options": {
                    "defaultType": "spot",
                    "adjustForTimeDifference": True,
                    "recvWindow": 60000,
                },
            })
            try:
                exchange.load_time_difference()
            except Exception:
                pass
            raw_trades = exchange.fetch_my_trades("NEAR/JPY", limit=1000)
            normalized = []
            for t in raw_trades:
                gross_amt = float(t.get("amount", 0.0))
                fee = t.get("fee") or {}
                # NEAR建てで手数料が控除された場合は実残高増加分を反映
                fee_near = float(fee.get("cost", 0.0)) if str(fee.get("currency", "")).upper() == "NEAR" else 0.0
                net_amt = max(0.0, gross_amt - fee_near)
                normalized.append({
                    "ts_ms": int(t.get("timestamp", 0)),
                    "side": str(t.get("side", "")).lower(),
                    "qty": net_amt,
                    "price": float(t.get("price", 0.0)),
                })
            normalized.sort(key=lambda x: x["ts_ms"])
            if normalized:
                cache["binance_NEARJPY"] = normalized
                save_trade_cache(cache)
                return normalized
        except Exception as exc:
            print(f"[Binance Trade History Warning] NEAR/JPY: {exc}")

    return cache.get("binance_NEARJPY", [])


def fetch_bitbank_ticker_last(pair: str) -> Optional[float]:
    try:
        r = requests.get(f"{BITBANK_PUBLIC_URL}/{pair}/ticker", timeout=5).json()
        if r.get("success") == 1:
            return float(r.get("data", {}).get("last", 0.0))
    except Exception:
        pass
    return None


def fetch_bitbank_daily_candles(pair: str, days: int = 365) -> pd.DataFrame:
    """Bitbank Public API から指定日数分の日足を取得し最新 ticker を追記"""
    now_jst = datetime.now(JST)
    years = sorted({(now_jst - timedelta(days=days)).year, now_jst.year})
    rows: List[List[Any]] = []
    for y in years:
        try:
            r = requests.get(f"{BITBANK_PUBLIC_URL}/{pair}/candlestick/1day/{y}", timeout=12).json()
            if r.get("success") == 1:
                ohlcv = (r.get("data") or {}).get("candlestick", [{}])[0].get("ohlcv", [])
                rows.extend(ohlcv)
        except Exception as exc:
            print(f"[Candle Warning] {pair} {y}: {exc}")

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume", "ts"])
    df["close"] = df["close"].astype(float)
    df["dt"] = pd.to_datetime(df["ts"].astype(float), unit="ms", utc=True).dt.tz_convert(JST)
    cutoff = now_jst - timedelta(days=days)
    df = df[df["dt"] >= cutoff].drop_duplicates(subset=["ts"]).sort_values("dt").reset_index(drop=True)

    last_px = fetch_bitbank_ticker_last(pair)
    if last_px and last_px > 0:
        df = pd.concat([df, pd.DataFrame([{"close": last_px, "dt": now_jst}])], ignore_index=True)

    return df[["dt", "close"]]


def fetch_bitbank_hourly_candles(pair: str, start_dt_jst: datetime) -> pd.DataFrame:
    """Bitbank Public API から開始日以降の 1 時間足を取得し最新 ticker を追記"""
    now_jst = datetime.now(JST)
    cur_date = (start_dt_jst.astimezone(timezone.utc) - timedelta(days=1)).date()
    end_date = now_jst.astimezone(timezone.utc).date()

    rows: List[List[Any]] = []
    while cur_date <= end_date:
        d_str = cur_date.strftime("%Y%m%d")
        try:
            r = requests.get(f"{BITBANK_PUBLIC_URL}/{pair}/candlestick/1hour/{d_str}", timeout=10)
            if r.ok:
                data = r.json()
                if data.get("success") == 1:
                    ohlcv = (data.get("data") or {}).get("candlestick", [{}])[0].get("ohlcv", [])
                    rows.extend(ohlcv)
        except Exception:
            pass
        cur_date += timedelta(days=1)
        time.sleep(0.03)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume", "ts"])
    df["close"] = df["close"].astype(float)
    df["dt"] = pd.to_datetime(df["ts"].astype(float), unit="ms", utc=True).dt.tz_convert(JST)
    df = df[df["dt"] >= start_dt_jst].drop_duplicates(subset=["ts"]).sort_values("dt").reset_index(drop=True)

    last_px = fetch_bitbank_ticker_last(pair)
    if last_px and last_px > 0:
        df = pd.concat([df, pd.DataFrame([{"close": last_px, "dt": now_jst}])], ignore_index=True)

    return df[["dt", "close"]]


def fetch_binance_hourly_candles(symbol: str, start_dt_jst: datetime) -> pd.DataFrame:
    """Binance Public API から開始日時以降の 1 時間足を取得し最新 ticker を追記"""
    now_jst = datetime.now(JST)
    start_ms = int(start_dt_jst.timestamp() * 1000)
    end_ms = int(now_jst.timestamp() * 1000)

    rows: List[List[Any]] = []
    while start_ms < end_ms:
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": symbol, "interval": "1h", "startTime": start_ms, "limit": 1000},
                timeout=12,
            )
            if not r.ok:
                break
            chunk = r.json()
            if not chunk:
                break
            rows.extend(chunk)
            start_ms = int(chunk[-1][0]) + 3_600_000
            if len(chunk) < 1000:
                break
        except Exception:
            break
        time.sleep(0.05)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "close", "v", "ct", "qv", "nt", "tbb", "tbq", "ig"])
    df["close"] = df["close"].astype(float)
    df["dt"] = pd.to_datetime(df["ts"].astype(float), unit="ms", utc=True).dt.tz_convert(JST)
    df = df[df["dt"] >= start_dt_jst].drop_duplicates(subset=["ts"]).sort_values("dt").reset_index(drop=True)

    try:
        t_resp = requests.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": symbol}, timeout=5).json()
        last_px = float(t_resp.get("price", 0.0))
        if last_px > 0:
            df = pd.concat([df, pd.DataFrame([{"close": last_px, "dt": now_jst}])], ignore_index=True)
    except Exception:
        pass

    return df[["dt", "close"]]


def build_position_pnl_series(
    candles_df: pd.DataFrame,
    trades: List[Dict[str, Any]],
) -> pd.DataFrame:
    """約定履歴を各ローソク足時刻までに累積適用し、保有数・投資元本・評価額・含み損益を時系列算出"""
    if candles_df.empty or not trades:
        return pd.DataFrame()

    sorted_trades = sorted(trades, key=lambda x: x["ts_ms"])
    trade_idx = 0
    n_trades = len(sorted_trades)

    cur_qty = 0.0
    cur_cost = 0.0
    records = []

    for _, row in candles_df.iterrows():
        dt_jst = row["dt"]
        ts_ms = int(dt_jst.timestamp() * 1000)
        close_px = float(row["close"])

        while trade_idx < n_trades and sorted_trades[trade_idx]["ts_ms"] <= ts_ms:
            tr = sorted_trades[trade_idx]
            amt = tr["qty"]
            px = tr["price"]
            if tr["side"] == "buy":
                cur_qty += amt
                cur_cost += amt * px
            elif tr["side"] == "sell" and cur_qty > 0:
                avg = cur_cost / cur_qty
                sell_amt = min(amt, cur_qty)
                cur_qty -= sell_amt
                cur_cost = cur_qty * avg
            trade_idx += 1

        if cur_qty <= 0:
            continue

        valuation = cur_qty * close_px
        pnl = valuation - cur_cost
        pnl_pct = (pnl / cur_cost * 100.0) if cur_cost > 0 else 0.0
        records.append({
            "dt": dt_jst,
            "price": close_px,
            "qty": cur_qty,
            "cost": cur_cost,
            "valuation": valuation,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
        })

    return pd.DataFrame(records)


def build_fixed_btc_series(candles_df: pd.DataFrame) -> pd.DataFrame:
    """長期保有 BTC (0.1434 BTC / 確定簿価 12,794,387 円) の含み損益推移を算出"""
    if candles_df.empty:
        return pd.DataFrame()
    df = candles_df.copy()
    cost = LONG_TERM_PROTECTED_BTC * KNOWN_ENTRY_PRICE
    df["price"] = df["close"]
    df["qty"] = LONG_TERM_PROTECTED_BTC
    df["cost"] = cost
    df["valuation"] = df["price"] * LONG_TERM_PROTECTED_BTC
    df["pnl"] = df["valuation"] - cost
    df["pnl_pct"] = (df["pnl"] / cost) * 100.0
    return df[["dt", "price", "qty", "cost", "valuation", "pnl", "pnl_pct"]]


def build_combined_series(
    btc_1h_df: pd.DataFrame,
    render_df: pd.DataFrame,
    near_df: pd.DataFrame,
) -> pd.DataFrame:
    """全3銘柄を保有開始以降 (2026-09-27 〜 現在) の共通 1 時間軸で統合"""
    dfs = {"btc": btc_1h_df, "render": render_df, "near": near_df}
    min_dt = min(d["dt"].min() for d in dfs.values() if not d.empty)
    max_dt = max(d["dt"].max() for d in dfs.values() if not d.empty)

    base_index = pd.date_range(start=min_dt.floor("h"), end=max_dt.ceil("h"), freq="1h", tz=JST)
    grid = pd.DataFrame({"dt": base_index})

    for key, src in dfs.items():
        if src.empty:
            grid[f"{key}_val"] = 0.0
            grid[f"{key}_cost"] = 0.0
            grid[f"{key}_pnl"] = 0.0
            continue
        sub = src[["dt", "valuation", "cost", "pnl"]].copy()
        sub = sub.sort_values("dt")
        merged = pd.merge_asof(grid[["dt"]], sub, on="dt", direction="backward")
        # 購入開始前のみ 0 埋め
        grid[f"{key}_val"] = merged["valuation"].fillna(0.0)
        grid[f"{key}_cost"] = merged["cost"].fillna(0.0)
        grid[f"{key}_pnl"] = merged["pnl"].fillna(0.0)

    # 最終点は各シリーズの最新スナップショット値を反映
    last_idx = grid.index[-1]
    for key, src in dfs.items():
        if not src.empty:
            grid.loc[last_idx, f"{key}_val"] = float(src["valuation"].iloc[-1])
            grid.loc[last_idx, f"{key}_cost"] = float(src["cost"].iloc[-1])
            grid.loc[last_idx, f"{key}_pnl"] = float(src["pnl"].iloc[-1])

    grid["valuation"] = grid["btc_val"] + grid["render_val"] + grid["near_val"]
    grid["cost"] = grid["btc_cost"] + grid["render_cost"] + grid["near_cost"]
    grid["pnl"] = grid["btc_pnl"] + grid["render_pnl"] + grid["near_pnl"]
    grid["pnl_pct"] = np.where(grid["cost"] > 0, (grid["pnl"] / grid["cost"]) * 100.0, 0.0)
    return grid


def _style_dark_axis(ax: plt.Axes, date_fmt: str) -> None:
    ax.set_facecolor("#111827")
    ax.grid(True, linestyle=":", alpha=0.35, color="#64748b")
    ax.tick_params(colors="#cbd5e1", labelsize=9)
    ax.yaxis.set_major_formatter(mticker.StrMethodFormatter("{x:,.0f}"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter(date_fmt))


def _to_naive_jst_series(dt_series: pd.Series) -> pd.Series:
    s = pd.to_datetime(dt_series)
    if s.dt.tz is not None:
        return s.dt.tz_convert(JST).dt.tz_localize(None)
    return s


def _apply_y_padding(ax: plt.Axes, values: Sequence[float], include_zero: bool = False) -> None:
    arr = np.asarray(list(values), dtype=float)
    if arr.size == 0:
        return
    y_min = float(np.nanmin(arr))
    y_max = float(np.nanmax(arr))
    if include_zero:
        y_min = min(y_min, 0.0)
        y_max = max(y_max, 0.0)
    span = max(y_max - y_min, 1000.0)
    ax.set_ylim(y_min - span * 0.16, y_max + span * 0.18)


def _annotate_extrema(ax: plt.Axes, df: pd.DataFrame, dts: pd.Series, col: str) -> None:
    if df.empty:
        return
    idx_max = df[col].idxmax()
    idx_min = df[col].idxmin()
    n_len = len(df)
    for idx, color, va, dy in [
        (idx_max, "#34d399", "bottom", 6),
        (idx_min, "#f87171", "top", -8),
    ]:
        pos = df.index.get_loc(idx)
        ha = "left" if pos < n_len * 0.12 else ("right" if pos > n_len * 0.88 else "center")
        val = float(df.loc[idx, col])
        dt_val = dts.loc[idx]
        dt_lbl = df.loc[idx, "dt"].strftime("%m/%d")
        ax.scatter([dt_val], [val], color=color, s=30, zorder=6)
        ax.annotate(
            f"{dt_lbl}: {val:+,.0f}円",
            xy=(dt_val, val),
            xytext=(0, dy),
            textcoords="offset points",
            ha=ha,
            va=va,
            fontsize=8,
            fontweight="bold",
            color=color,
            bbox=dict(boxstyle="round,pad=0.2", facecolor="#0f172a", edgecolor=color, alpha=0.85, lw=0.8),
        )


def plot_pnl_dashboard(
    btc_1y: pd.DataFrame,
    render_df: pd.DataFrame,
    near_df: pd.DataFrame,
    combined_df: pd.DataFrame,
    trade_dates: Dict[str, List[datetime]],
    out_path: Path,
    now_jst: datetime,
) -> Path:
    """2×2 含み損益 (円 / %) 推移ダッシュボードを生成"""
    configure_japanese_font()
    fig, axes = plt.subplots(2, 2, figsize=(18, 11), dpi=150)
    fig.patch.set_facecolor("#0f172a")

    ren_qty = f"{render_df['qty'].iloc[-1]:,.1f}枚" if not render_df.empty else ""
    near_qty = f"{near_df['qty'].iloc[-1]:,.1f}枚" if not near_df.empty else ""
    panels = [
        (axes[0, 0], btc_1y, f"BTC/JPY (長期保有 1年推移 / {LONG_TERM_PROTECTED_BTC:.4f} BTC)", "%Y-%m", []),
        (axes[0, 1], render_df, f"RENDER/JPY (購入当初 9/27〜現在 / {ren_qty})", "%m/%d", trade_dates.get("render", [])),
        (axes[1, 0], near_df, f"NEAR/JPY (購入当初 9/27〜現在 / {near_qty})", "%m/%d", trade_dates.get("near", [])),
        (axes[1, 1], combined_df, "全3銘柄 合計含み損益推移 (9/27〜現在 / BTC+RENDER+NEAR)", "%m/%d", []),
    ]

    for ax, df, title, date_fmt, buy_dts in panels:
        _style_dark_axis(ax, date_fmt)
        if df.empty:
            ax.set_title(f"{title} (データなし)", color="#f8fafc", fontsize=11, fontweight="bold")
            continue

        dts = _to_naive_jst_series(df["dt"])
        pnls = df["pnl"].values
        ax.axhline(0.0, color="#94a3b8", linestyle="--", linewidth=1.2, alpha=0.75)

        if "btc_pnl" in df.columns:
            # 合計パネルには個別寄与も薄く表示
            ax.plot(dts, df["btc_pnl"], color="#f59e0b", linewidth=1.2, linestyle="--", alpha=0.75, label="BTC")
            ax.plot(dts, df["render_pnl"], color="#a855f7", linewidth=1.2, linestyle="--", alpha=0.75, label="RENDER")
            ax.plot(dts, df["near_pnl"], color="#06b6d4", linewidth=1.2, linestyle="--", alpha=0.75, label="NEAR")
            ax.plot(dts, pnls, color="#f8fafc", linewidth=2.4, label="合計含み損益", zorder=5)
            ax.fill_between(dts, 0.0, pnls, where=(pnls >= 0), interpolate=True, color="#10b981", alpha=0.22)
            ax.fill_between(dts, 0.0, pnls, where=(pnls < 0), interpolate=True, color="#ef4444", alpha=0.25)
            ax.legend(loc="lower left", fontsize=8.5, facecolor="#1e293b", edgecolor="#475569", labelcolor="#f8fafc", ncol=4)
        else:
            ax.plot(dts, pnls, color="#38bdf8", linewidth=2.0, zorder=4)
            ax.fill_between(dts, 0.0, pnls, where=(pnls >= 0), interpolate=True, color="#10b981", alpha=0.25, label="含み益ゾーン")
            ax.fill_between(dts, 0.0, pnls, where=(pnls < 0), interpolate=True, color="#ef4444", alpha=0.28, label="含み損ゾーン")
            # 購入・買増ポイントの縦線
            for idx_b, bdt in enumerate(buy_dts):
                bdt_naive = bdt.astimezone(JST).replace(tzinfo=None)
                ax.axvline(bdt_naive, color="#fbbf24", linestyle=":", linewidth=1.0, alpha=0.55,
                           label="購入/買増タイミング" if idx_b == 0 else None)
            ax.legend(loc="lower left", fontsize=8.5, facecolor="#1e293b", edgecolor="#475569", labelcolor="#f8fafc")

        all_vals = list(pnls)
        if "btc_pnl" in df.columns:
            all_vals.extend(df["btc_pnl"].tolist() + df["render_pnl"].tolist() + df["near_pnl"].tolist())
        _apply_y_padding(ax, all_vals, include_zero=True)
        _annotate_extrema(ax, df, dts, "pnl")

        last_row = df.iloc[-1]
        cur_pnl = float(last_row["pnl"])
        cur_pct = float(last_row["pnl_pct"])
        cur_val = float(last_row["valuation"])
        cur_cost = float(last_row["cost"])
        max_pnl = float(df["pnl"].max())
        min_pnl = float(df["pnl"].min())

        sub = (
            f"現在含み損益: {cur_pnl:+,.0f}円 ({cur_pct:+.2f}%) | 評価額: {cur_val:,.0f}円 / 元本: {cur_cost:,.0f}円\n"
            f"期間レンジ: 最大 {max_pnl:+,.0f}円 〜 最小 {min_pnl:+,.0f}円"
        )
        ax.set_title(f"{title}\n{sub}", color="#f8fafc", fontsize=10.5, fontweight="bold", pad=8)
        ax.set_ylabel("含み損益 (JPY)", color="#cbd5e1", fontsize=9.5)

    fig.suptitle(
        f"保有暗号資産 含み損益推移ダッシュボード [{now_jst.strftime('%Y-%m-%d %H:%M JST')}]",
        color="#f8fafc",
        fontsize=15,
        fontweight="bold",
        y=0.98,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.955])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)
    return out_path


def plot_value_vs_cost_dashboard(
    btc_1y: pd.DataFrame,
    render_df: pd.DataFrame,
    near_df: pd.DataFrame,
    combined_df: pd.DataFrame,
    out_path: Path,
    now_jst: datetime,
) -> Path:
    """2×2 評価額 vs 投資元本 (取得コスト) 比較ダッシュボードを生成"""
    configure_japanese_font()
    fig, axes = plt.subplots(2, 2, figsize=(18, 11), dpi=150)
    fig.patch.set_facecolor("#0f172a")

    panels = [
        (axes[0, 0], btc_1y, f"BTC/JPY (1年推移 / {LONG_TERM_PROTECTED_BTC:.4f} BTC)", "%Y-%m"),
        (axes[0, 1], render_df, "RENDER/JPY (購入当初 9/27〜現在)", "%m/%d"),
        (axes[1, 0], near_df, "NEAR/JPY (購入当初 9/27〜現在)", "%m/%d"),
        (axes[1, 1], combined_df, "全3銘柄 合計評価額 vs 投資元本 (9/27〜現在)", "%m/%d"),
    ]

    for ax, df, title, date_fmt in panels:
        _style_dark_axis(ax, date_fmt)
        if df.empty:
            continue

        dts = _to_naive_jst_series(df["dt"])
        vals = df["valuation"].values
        costs = df["cost"].values

        ax.plot(dts, vals, color="#38bdf8", linewidth=2.1, label="時価評価額 (JPY)", zorder=5)
        ax.step(dts, costs, where="post", color="#fbbf24", linestyle="--", linewidth=1.8, label="累計投資元本 (JPY)", zorder=4)
        ax.fill_between(dts, costs, vals, where=(vals >= costs), interpolate=True, color="#10b981", alpha=0.22, label="含み益")
        ax.fill_between(dts, costs, vals, where=(vals < costs), interpolate=True, color="#ef4444", alpha=0.25, label="含み損")

        _apply_y_padding(ax, list(vals) + list(costs), include_zero=False)
        ax.legend(loc="lower left", fontsize=8.5, facecolor="#1e293b", edgecolor="#475569", labelcolor="#f8fafc")

        last_row = df.iloc[-1]
        cur_val = float(last_row["valuation"])
        cur_cost = float(last_row["cost"])
        cur_pnl = float(last_row["pnl"])
        cur_pct = float(last_row["pnl_pct"])
        sub = f"時価評価額: {cur_val:,.0f}円 vs 元本: {cur_cost:,.0f}円 | 差額: {cur_pnl:+,.0f}円 ({cur_pct:+.2f}%)"
        ax.set_title(f"{title}\n{sub}", color="#f8fafc", fontsize=10.5, fontweight="bold", pad=8)
        ax.set_ylabel("金額 (JPY)", color="#cbd5e1", fontsize=9.5)

    fig.suptitle(
        f"保有暗号資産 評価額 vs 投資元本 比較ダッシュボード [{now_jst.strftime('%Y-%m-%d %H:%M JST')}]",
        color="#f8fafc",
        fontsize=15,
        fontweight="bold",
        y=0.98,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.955])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)
    return out_path


def send_to_discord(webhook_url: str, paths: List[Path], summary_text: str) -> None:
    for idx, p in enumerate(paths):
        content = summary_text if idx == 0 else f"📊 **【保有暗号資産 評価額 vs 投資元本】** `{p.name}`"
        try:
            with p.open("rb") as fp:
                resp = requests.post(
                    webhook_url,
                    data={"content": content},
                    files={"file": (p.name, fp, "image/png")},
                    timeout=30,
                )
            if resp.status_code in (200, 204):
                print(f"✅ Discord 送信成功: {p.name}")
            else:
                print(f"⚠️ Discord 送信警告 ({resp.status_code}): {p.name}")
        except Exception as exc:
            print(f"❌ Discord 送信エラー ({p.name}): {exc}")
        time.sleep(1.5)


async def run(send_discord_flag: bool = False, channel: Optional[str] = None) -> Tuple[Path, Path]:
    now_jst = datetime.now(JST)
    ts_tag = now_jst.strftime("%Y%m%d_%H%M%S")
    plots_dir = BASE_DIR / "Data" / "plots"

    print(f"[{now_jst.strftime('%Y-%m-%d %H:%M:%S JST')}] 約定履歴と過去ローソク足データを取得中...")
    render_trades = await fetch_bitbank_trades("render_jpy")
    near_trades = fetch_binance_near_trades()

    if not render_trades:
        raise RuntimeError("RENDER 約定履歴が取得できませんでした（キャッシュも空）。")
    if not near_trades:
        raise RuntimeError("NEAR 約定履歴が取得できませんでした（VPS で実行するかキャッシュを確認してください）。")

    render_start = datetime.fromtimestamp(render_trades[0]["ts_ms"] / 1000.0, tz=timezone.utc).astimezone(JST) - timedelta(hours=1)
    near_start = datetime.fromtimestamp(near_trades[0]["ts_ms"] / 1000.0, tz=timezone.utc).astimezone(JST) - timedelta(hours=1)
    common_start = min(render_start, near_start)

    btc_1y_candles = fetch_bitbank_daily_candles("btc_jpy", days=365)
    btc_1h_candles = fetch_bitbank_hourly_candles("btc_jpy", common_start)
    render_candles = fetch_bitbank_hourly_candles("render_jpy", render_start)
    near_candles = fetch_binance_hourly_candles("NEARJPY", near_start)

    btc_1y_df = build_fixed_btc_series(btc_1y_candles)
    btc_1h_df = build_fixed_btc_series(btc_1h_candles)
    render_df = build_position_pnl_series(render_candles, render_trades)
    near_df = build_position_pnl_series(near_candles, near_trades)
    combined_df = build_combined_series(btc_1h_df, render_df, near_df)

    # 買い増し節目 (1時間以上間隔の空いた購入バッチを抽出)
    trade_dates: Dict[str, List[datetime]] = {"render": [], "near": []}
    for key, trs in [("render", render_trades), ("near", near_trades)]:
        last_dt: Optional[datetime] = None
        for t in trs:
            dt = datetime.fromtimestamp(t["ts_ms"] / 1000.0, tz=timezone.utc).astimezone(JST)
            if last_dt is None or (dt - last_dt).total_seconds() >= 3600:
                trade_dates[key].append(dt)
                last_dt = dt

    pnl_path = plots_dir / f"unrealized_pnl_history_{ts_tag}.png"
    val_path = plots_dir / f"portfolio_value_vs_cost_{ts_tag}.png"

    plot_pnl_dashboard(btc_1y_df, render_df, near_df, combined_df, trade_dates, pnl_path, now_jst)
    plot_value_vs_cost_dashboard(btc_1y_df, render_df, near_df, combined_df, val_path, now_jst)

    print(f"📈 含み損益推移チャート保存: {pnl_path}")
    print(f"📈 評価額vs元本チャート保存: {val_path}")

    for name, df in [("BTC(1y)", btc_1y_df), ("RENDER", render_df), ("NEAR", near_df), ("TOTAL(9/27~)", combined_df)]:
        if not df.empty:
            r = df.iloc[-1]
            print(
                f"  [{name:12s}] 評価額={r['valuation']:>10,.0f}円 | 元本={r['cost']:>10,.0f}円 | "
                f"現在損益={r['pnl']:>+10,.0f}円 ({r['pnl_pct']:>+6.2f}%) | "
                f"最大={df['pnl'].max():>+10,.0f}円 / 最小={df['pnl'].min():>+10,.0f}円"
            )

    if send_discord_flag:
        webhook_url = get_webhook_url(channel)
        if webhook_url:
            tot = combined_df.iloc[-1]
            summary = (
                f"📉 **【保有暗号資産 含み損益推移チャート】** ({now_jst.strftime('%Y-%m-%d %H:%M JST')})\n"
                f"• 合計評価額: `{tot['valuation']:,.0f} 円` / 合計元本: `{tot['cost']:,.0f} 円`\n"
                f"• 合計含み損益: `{tot['pnl']:+,.0f} 円 ({tot['pnl_pct']:+.2f}%)`"
            )
            send_to_discord(webhook_url, [pnl_path, val_path], summary)

    return pnl_path, val_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="保有暗号資産 含み損推移チャート生成")
    parser.add_argument("--discord", action="store_true", help="Discord へ画像を送信")
    parser.add_argument("--channel", type=str, default=None, help="送信先Webhook名")
    args = parser.parse_args()
    asyncio.run(run(send_discord_flag=args.discord, channel=args.channel))
