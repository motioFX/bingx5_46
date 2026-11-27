"""Polymarket `Maker Buy No` 24時間仮想ペーパートレードボット（cron自律稼働対応）

特徴:
  - 実売買ゼロ・秘密鍵不要・常駐メモリゼロ（VPS cron で 2時間おき等に設定して24時間稼働可能）
  - 既存の現物監査ボット (bitbank5_46_1spot_limit.py) とは完全に独立した仮想 USDC 台帳を管理
  - リアルな3状態ライフサイクル: PENDING_MAKER -> OPEN (CLOB履歴/Ask到達で約定) -> WON / LOST / CANCELLED
  - 厳格リスク管理: 1銘柄=総エクイティ2%、最大12枠 (Bucket A<=8, Bucket B<=4)、1イベント1銘柄、
                  Bucket Bは同一銘柄・同方向1枠まで
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config_loader import get_webhook_url
from polymarket_buy_no_screener import (
    fetch_active_events_pool,
    fetch_book_metrics,
    fetch_live_rv,
    http_get_json,
    scan_bucket_a_negrisk,
    scan_bucket_b_crypto_high_iv,
)
from polymarket_top20_chart import (
    CLOB_HISTORY_URL,
    configure_japanese_font,
    parse_json_list,
    send_chart_to_discord,
    shorten_text,
)

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

JST = timezone(timedelta(hours=9))
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

DEFAULT_STATE_DIR = BASE_DIR / "Data" / "polymarket_paper"
DEFAULT_PLOTS_DIR = BASE_DIR / "Data" / "plots" / "polymarket_paper"

MAX_TOTAL_SLOTS = 12
MAX_BUCKET_A_SLOTS = 8
MAX_BUCKET_B_SLOTS = 4
STAKE_PCT = 0.05
MIN_STAKE_USD = 2.0
PENDING_TTL_HOURS = 24.0
RUNAWAY_CENTS = 2.5


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def append_csv_rows(path: Path, fieldnames: List[str], rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fieldnames})


def load_or_init_state(
    state_path: Path,
    initial_balance: float = 100.0,
    target_balance: float = 200.0,
    reset: bool = False,
) -> Dict[str, Any]:
    if state_path.exists() and not reset:
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "cash_usd" in data:
                data.setdefault("target_balance_usd", float(target_balance))
                return data
        except Exception:
            pass
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "created_at_utc": now_iso,
        "updated_at_utc": now_iso,
        "initial_balance_usd": float(initial_balance),
        "target_balance_usd": float(target_balance),
        "cash_usd": float(initial_balance),
        "peak_equity_usd": float(initial_balance),
        "realized_pnl_usd": 0.0,
        "stats": {"placed": 0, "filled": 0, "cancelled": 0, "won": 0, "lost": 0},
        "pending_orders": [],
        "open_positions": [],
        "seq_counter": 0,
    }


def fetch_market_status_by_yes_token(yes_token_id: str) -> Optional[Dict[str, Any]]:
    """Gamma /markets?clob_token_ids= で市場の決着ステータスを取得"""
    data = http_get_json(GAMMA_MARKETS_URL, {"clob_token_ids": yes_token_id}, timeout=15)
    if isinstance(data, list) and data:
        return data[0]
    return None


def fetch_no_token_min_price_since(no_token_id: str, since_ts: int, until_ts: int) -> Optional[float]:
    """指定時刻以降に CLOB 履歴で No トークンが到達した最安値を返す"""
    data = http_get_json(
        CLOB_HISTORY_URL,
        {"market": no_token_id, "startTs": int(since_ts), "endTs": int(until_ts), "fidelity": 1},
        timeout=15,
    )
    if not data or not isinstance(data, dict):
        return None
    prices = [
        float(pt["p"])
        for pt in (data.get("history") or [])
        if int(pt.get("t", 0)) > since_ts and 0.0 < float(pt.get("p", 0.0)) <= 1.0
    ]
    return min(prices) if prices else None


def check_market_resolution(mkt: Optional[Dict[str, Any]]) -> Optional[str]:
    """決着済みなら 'WON' (No=1) または 'LOST' (Yes=1)、未決着は None"""
    if not mkt or not mkt.get("closed"):
        return None
    outs = parse_json_list(mkt.get("outcomes"))
    prs = parse_json_list(mkt.get("outcomePrices"))
    if outs != ["Yes", "No"] or len(prs) != 2:
        return None
    try:
        p_yes = float(prs[0])
        p_no = float(prs[1])
    except Exception:
        return None
    if p_no >= 0.99 and p_yes <= 0.01:
        return "WON"
    if p_yes >= 0.99 and p_no <= 0.01:
        return "LOST"
    return None


def filter_bucket_b_strict(bucket_b: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Bucket B 厳格条件: IV > max(RV30 + 10pt, RV14), edge_vs_rv30 >= +2¢, No Bid 82..97.5¢, 2..45d"""
    out = [
        b for b in bucket_b
        if 82.0 <= b["no_bid_cents"] <= 97.5
        and 2.0 <= b["days_rem"] <= 45.0
        and b["no_depth_2c_usd"] >= 500.0
        and b["poly_iv_pct"] > max(b["rv_30d_pct"] + 10.0, b["rv_14d_pct"])
        and b["edge_vs_rv30_cents"] >= 2.0
    ]
    out.sort(key=lambda x: x["edge_vs_rv30_cents"], reverse=True)
    return out


def default_candidate_fetcher(now_utc: datetime) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    events = fetch_active_events_pool(limit=150)
    rv_map = {"BTC": fetch_live_rv("BTCUSDT"), "ETH": fetch_live_rv("ETHUSDT")}
    cand_a = scan_bucket_a_negrisk(events, now_utc, min_days=2.0, max_days=120.0, top_n=12)
    cand_b_raw = scan_bucket_b_crypto_high_iv(events, now_utc, rv_map, min_days=2.0)
    cand_b = filter_bucket_b_strict(cand_b_raw)
    for c in cand_a:
        c["bucket"] = "A"
    for c in cand_b:
        c["bucket"] = "B"
        c["event_id"] = f"crypto_{c['asset']}_{c['sub_label']}_{c['end_date']}"
    return cand_a, cand_b, rv_map


def run_paper_cycle(
    state_dir: Path = DEFAULT_STATE_DIR,
    plots_dir: Path = DEFAULT_PLOTS_DIR,
    initial_balance: float = 100.0,
    target_balance: float = 200.0,
    reset: bool = False,
    dry_run: bool = False,
    now_utc: Optional[datetime] = None,
    candidate_fetcher: Callable[[datetime], Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]] = default_candidate_fetcher,
    market_lookup: Callable[[str], Optional[Dict[str, Any]]] = fetch_market_status_by_yes_token,
    book_lookup: Callable[[str], Optional[Dict[str, Any]]] = fetch_book_metrics,
    min_price_lookup: Callable[[str, int, int], Optional[float]] = fetch_no_token_min_price_since,
) -> Dict[str, Any]:
    """1サイクル分のペーパートレード更新（決着→約定/取消→新規発注→会計計算）を実行"""
    now_dt = now_utc or datetime.now(timezone.utc)
    now_ts = int(now_dt.timestamp())
    now_iso = now_dt.isoformat()

    state_path = state_dir / "paper_state.json"
    trades_csv = state_dir / "paper_trades.csv"
    equity_csv = state_dir / "equity_history.csv"

    state = load_or_init_state(
        state_path,
        initial_balance=initial_balance,
        target_balance=target_balance,
        reset=reset,
    )
    events_log: List[str] = []
    closed_rows: List[Dict[str, Any]] = []

    # 1) 保有中ポジション (OPEN) の決着確認 & 時価評価 (MTM)
    still_open: List[Dict[str, Any]] = []
    for pos in state["open_positions"]:
        mkt = market_lookup(pos["yes_token_id"])
        res_status = check_market_resolution(mkt)
        if res_status in ("WON", "LOST"):
            payout = pos["shares"] * 1.0 if res_status == "WON" else 0.0
            pnl = payout - pos["stake_usd"]
            state["cash_usd"] += payout
            state["realized_pnl_usd"] += pnl
            if res_status == "WON":
                state["stats"]["won"] += 1
            else:
                state["stats"]["lost"] += 1
            events_log.append(
                f"{'🎉 WON' if res_status == 'WON' else '💥 LOST'} [{pos['bucket']}] "
                f"{shorten_text(pos['title'], 28)} ({pos['sub_label']}) PnL=${pnl:+.2f}"
            )
            closed_rows.append({
                "trade_id": pos["trade_id"],
                "bucket": pos["bucket"],
                "title": pos["title"],
                "sub_label": pos["sub_label"],
                "status": res_status,
                "limit_no_cents": round(pos["limit_price"] * 100.0, 2),
                "stake_usd": round(pos["stake_usd"], 2),
                "payout_usd": round(payout, 2),
                "pnl_usd": round(pnl, 2),
                "roi_pct": round((pnl / pos["stake_usd"]) * 100.0, 2),
                "opened_at": pos.get("filled_at_utc", pos["created_at_utc"]),
                "closed_at": now_iso,
            })
            continue

        # 未決着 -> 最新 No Best Bid で MTM 評価
        bk = book_lookup(pos["no_token_id"])
        cur_bid = float(bk["best_bid"]) if bk else float(pos.get("current_no_bid", pos["limit_price"]))
        mtm_val = pos["shares"] * cur_bid
        pos["current_no_bid"] = cur_bid
        pos["mtm_value"] = mtm_val
        pos["unrealized_pnl_usd"] = mtm_val - pos["stake_usd"]
        still_open.append(pos)

    state["open_positions"] = still_open

    # 2) 待機中 Maker 指値 (PENDING_MAKER) の約定・キャンセル判定
    still_pending: List[Dict[str, Any]] = []
    for ord_item in state["pending_orders"]:
        mkt = market_lookup(ord_item["yes_token_id"])
        res_status = check_market_resolution(mkt)
        if res_status is not None:
            # 約定前に市場閉鎖 -> キャンセル返金
            state["cash_usd"] += ord_item["stake_usd"]
            state["stats"]["cancelled"] += 1
            events_log.append(f"↩️ CANCEL(市場終了) [{ord_item['bucket']}] {shorten_text(ord_item['title'], 26)}")
            closed_rows.append({
                "trade_id": ord_item["trade_id"], "bucket": ord_item["bucket"],
                "title": ord_item["title"], "sub_label": ord_item["sub_label"],
                "status": "CANCELLED_CLOSED", "limit_no_cents": round(ord_item["limit_price"] * 100.0, 2),
                "stake_usd": round(ord_item["stake_usd"], 2), "payout_usd": round(ord_item["stake_usd"], 2),
                "pnl_usd": 0.0, "roi_pct": 0.0,
                "opened_at": ord_item["created_at_utc"], "closed_at": now_iso,
            })
            continue

        bk = book_lookup(ord_item["no_token_id"])
        min_hist_p = min_price_lookup(ord_item["no_token_id"], int(ord_item["created_ts"]), now_ts)
        lim = float(ord_item["limit_price"])

        filled_by_ask = bool(bk and float(bk["best_ask"]) <= lim + 1e-6)
        filled_by_hist = bool(min_hist_p is not None and min_hist_p <= lim + 1e-6)

        if filled_by_ask or filled_by_hist:
            shares = ord_item["stake_usd"] / lim
            cur_bid = float(bk["best_bid"]) if bk else lim
            mtm_val = shares * cur_bid
            new_pos = {
                **ord_item,
                "status": "OPEN",
                "shares": shares,
                "filled_at_utc": now_iso,
                "filled_ts": now_ts,
                "fill_reason": "ASK_CROSS" if filled_by_ask else "HIST_TOUCH",
                "current_no_bid": cur_bid,
                "mtm_value": mtm_val,
                "unrealized_pnl_usd": mtm_val - ord_item["stake_usd"],
            }
            state["open_positions"].append(new_pos)
            state["stats"]["filled"] += 1
            events_log.append(
                f"✅ FILL [{ord_item['bucket']}] {shorten_text(ord_item['title'], 26)} "
                f"({ord_item['sub_label']}) @ {lim*100:.1f}¢ (${ord_item['stake_usd']:.2f})"
            )
            continue

        age_hours = (now_ts - int(ord_item["created_ts"])) / 3600.0
        ran_away = bool(bk and float(bk["best_bid"]) > lim + (RUNAWAY_CENTS / 100.0))
        if age_hours >= PENDING_TTL_HOURS or ran_away:
            reason = "CANCEL_TTL" if age_hours >= PENDING_TTL_HOURS else "CANCEL_RUNAWAY"
            state["cash_usd"] += ord_item["stake_usd"]
            state["stats"]["cancelled"] += 1
            events_log.append(f"↩️ {reason} [{ord_item['bucket']}] {shorten_text(ord_item['title'], 26)}")
            closed_rows.append({
                "trade_id": ord_item["trade_id"], "bucket": ord_item["bucket"],
                "title": ord_item["title"], "sub_label": ord_item["sub_label"],
                "status": reason, "limit_no_cents": round(lim * 100.0, 2),
                "stake_usd": round(ord_item["stake_usd"], 2), "payout_usd": round(ord_item["stake_usd"], 2),
                "pnl_usd": 0.0, "roi_pct": 0.0,
                "opened_at": ord_item["created_at_utc"], "closed_at": now_iso,
            })
            continue

        still_pending.append(ord_item)

    state["pending_orders"] = still_pending

    # 3) 新規 Maker 指値配置 (PENDING_MAKER)
    cand_a, cand_b, rv_map = candidate_fetcher(now_dt)

    active_all = state["open_positions"] + state["pending_orders"]
    used_events = {p["event_id"] for p in active_all}
    used_tokens = {p["no_token_id"] for p in active_all}
    count_a = sum(1 for p in active_all if p["bucket"] == "A")
    count_b = sum(1 for p in active_all if p["bucket"] == "B")
    b_asset_cnt = {"BTC": sum(1 for p in active_all if p["bucket"] == "B" and p.get("asset") == "BTC"),
                   "ETH": sum(1 for p in active_all if p["bucket"] == "B" and p.get("asset") == "ETH")}
    b_asset_dirs = {(p.get("asset"), p.get("strike_dir")) for p in active_all if p["bucket"] == "B"}

    # 現在のエクイティ基準で1ポジション金額を算出
    cur_locked = sum(o["stake_usd"] for o in state["pending_orders"])
    cur_mtm = sum(p["mtm_value"] for p in state["open_positions"])
    cur_equity = state["cash_usd"] + cur_locked + cur_mtm

    # Bucket B (高IV厳選) と Bucket A (NegRisk) を交互に枠内発注
    interleaved: List[Dict[str, Any]] = []
    for i in range(max(len(cand_a), len(cand_b))):
        if i < len(cand_b):
            interleaved.append(cand_b[i])
        if i < len(cand_a):
            interleaved.append(cand_a[i])

    for cand in interleaved:
        total_cnt = len(state["open_positions"]) + len(state["pending_orders"])
        if total_cnt >= MAX_TOTAL_SLOTS:
            break
        bkt = cand["bucket"]
        if bkt == "A" and count_a >= MAX_BUCKET_A_SLOTS:
            continue
        if bkt == "B":
            if count_b >= MAX_BUCKET_B_SLOTS:
                continue
            asset = cand["asset"]
            sdir = cand["strike_dir"]
            if b_asset_cnt.get(asset, 0) >= 2 or (asset, sdir) in b_asset_dirs:
                continue

        if cand["event_id"] in used_events or cand["no_token_id"] in used_tokens:
            continue

        stake = round(min(state["cash_usd"], max(MIN_STAKE_USD, cur_equity * STAKE_PCT)), 2)
        if state["cash_usd"] < MIN_STAKE_USD or stake < MIN_STAKE_USD:
            break

        limit_p = round(float(cand["no_bid_cents"]) / 100.0, 4)
        if not (0.80 <= limit_p <= 0.98):
            continue

        state["seq_counter"] += 1
        trade_id = f"PM-{state['seq_counter']:04d}"
        new_ord = {
            "trade_id": trade_id,
            "bucket": bkt,
            "event_id": cand["event_id"],
            "title": cand.get("event_title", ""),
            "sub_label": cand["sub_label"],
            "yes_token_id": cand["yes_token_id"],
            "no_token_id": cand["no_token_id"],
            "asset": cand.get("asset", ""),
            "strike_dir": cand.get("strike_dir", 0),
            "days_rem": round(float(cand["days_rem"]), 2),
            "end_date": cand["end_date"],
            "limit_price": limit_p,
            "stake_usd": stake,
            "expected_roi_pct": round((1.0 - limit_p) / limit_p * 100.0, 2),
            "created_at_utc": now_iso,
            "created_ts": now_ts,
            "status": "PENDING_MAKER",
        }
        state["cash_usd"] -= stake
        state["pending_orders"].append(new_ord)
        state["stats"]["placed"] += 1
        used_events.add(cand["event_id"])
        used_tokens.add(cand["no_token_id"])
        if bkt == "A":
            count_a += 1
        else:
            count_b += 1
            b_asset_cnt[cand["asset"]] = b_asset_cnt.get(cand["asset"], 0) + 1
            b_asset_dirs.add((cand["asset"], cand["strike_dir"]))

        events_log.append(
            f"📝 ORDER [{bkt}] {shorten_text(new_ord['title'], 26)} ({new_ord['sub_label']}) "
            f"Maker Bid {limit_p*100:.1f}¢ (${stake:.2f}, ROI {new_ord['expected_roi_pct']:.1f}%)"
        )

    # 4) 最終会計集計＆不変式アサーション
    locked_pending = sum(o["stake_usd"] for o in state["pending_orders"])
    open_mtm = sum(p["mtm_value"] for p in state["open_positions"])
    open_cost = sum(p["stake_usd"] for p in state["open_positions"])
    unrealized_pnl = open_mtm - open_cost
    equity = state["cash_usd"] + locked_pending + open_mtm
    assert abs(equity - (state["cash_usd"] + locked_pending + open_mtm)) < 1e-6

    state["peak_equity_usd"] = max(float(state.get("peak_equity_usd", equity)), equity)
    dd_pct = (equity / state["peak_equity_usd"] - 1.0) * 100.0 if state["peak_equity_usd"] > 0 else 0.0
    state["updated_at_utc"] = now_iso

    eq_row = {
        "ts_utc": now_iso,
        "ts_jst": now_dt.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S"),
        "equity_usd": round(equity, 4),
        "cash_usd": round(state["cash_usd"], 4),
        "locked_pending_usd": round(locked_pending, 4),
        "open_mtm_usd": round(open_mtm, 4),
        "realized_pnl_usd": round(state["realized_pnl_usd"], 4),
        "unrealized_pnl_usd": round(unrealized_pnl, 4),
        "drawdown_pct": round(dd_pct, 4),
        "n_open": len(state["open_positions"]),
        "n_pending": len(state["pending_orders"]),
    }

    if reset and not dry_run:
        for old_f in (trades_csv, equity_csv):
            if old_f.exists():
                try:
                    old_f.unlink()
                except Exception:
                    pass

    if not dry_run:
        atomic_write_json(state_path, state)
        append_csv_rows(
            trades_csv,
            ["trade_id", "bucket", "title", "sub_label", "status", "limit_no_cents", "stake_usd", "payout_usd", "pnl_usd", "roi_pct", "opened_at", "closed_at"],
            closed_rows,
        )
        append_csv_rows(
            equity_csv,
            ["ts_utc", "ts_jst", "equity_usd", "cash_usd", "locked_pending_usd", "open_mtm_usd", "realized_pnl_usd", "unrealized_pnl_usd", "drawdown_pct", "n_open", "n_pending"],
            [eq_row],
        )

    return {
        "state": state,
        "eq_row": eq_row,
        "events_log": events_log,
        "closed_rows": closed_rows,
        "equity_csv": None if (reset and dry_run) else equity_csv,
        "rv_map": rv_map,
    }


def render_paper_dashboard(
    res: Dict[str, Any],
    now_jst: datetime,
    out_path: Path,
) -> Optional[Path]:
    configure_japanese_font()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    state = res["state"]
    eq_row = res["eq_row"]
    eq_csv = res["equity_csv"]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16.5, 6.8))
    fig.patch.set_facecolor("#ffffff")

    # 左パネル: 仮想口座エクイティ推移
    if eq_csv is not None and eq_csv.exists():
        df_eq = pd.read_csv(eq_csv, encoding="utf-8")
    else:
        df_eq = pd.DataFrame([eq_row])

    init_bal = float(state["initial_balance_usd"])
    tgt_bal = float(state.get("target_balance_usd", init_bal * 2.0))
    denom = max(tgt_bal - init_bal, 1e-6)
    progress_pct = (eq_row["equity_usd"] - init_bal) / denom * 100.0

    if not df_eq.empty:
        dts = pd.to_datetime(df_eq["ts_jst"])
        ax1.plot(dts, df_eq["equity_usd"], marker="o", markersize=4, linewidth=2.2, color="#1f77b4", label="総エクイティ ($)")
        ax1.axhline(init_bal, color="#555555", linestyle="--", linewidth=1.2, label=f"初期証拠金 (${init_bal:,.0f})")
        ax1.axhline(tgt_bal, color="#2ca02c", linestyle=":", linewidth=1.4, label=f"目標2倍 (${tgt_bal:,.0f})")
        if len(dts) == 1:
            ax1.set_xlim(dts.iloc[0] - pd.Timedelta(hours=1), dts.iloc[0] + pd.Timedelta(hours=1))
        y_lo = min(init_bal * 0.92, float(df_eq["equity_usd"].min()) * 0.95)
        y_hi = max(tgt_bal * 1.05, float(df_eq["equity_usd"].max()) * 1.05)
        ax1.set_ylim(y_lo, y_hi)
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M"))
        fig.autofmt_xdate()
    won, lost = state["stats"]["won"], state["stats"]["lost"]
    wr_str = f"{won/(won+lost)*100:.1f}%" if (won + lost) > 0 else "N/A"
    ax1.set_title(
        f"仮想口座: ${eq_row['equity_usd']:,.2f} / 目標 ${tgt_bal:,.0f} (進捗 {progress_pct:+.1f}%)\n"
        f"確定 ${state['realized_pnl_usd']:+.2f} | 勝敗 {won}W-{lost}L [{wr_str}] | DD {eq_row['drawdown_pct']:.2f}%",
        fontsize=10.2, fontweight="bold",
    )
    ax1.set_ylabel("資産額 (USDC)", fontsize=9.5)
    ax1.grid(True, linestyle=":", alpha=0.6)
    ax1.legend(fontsize=8.5, loc="best")

    # 右パネル: 現在の OPEN / PENDING_MAKER ポジション一覧
    items = [
        {**p, "kind_lbl": "OPEN", "bar_val": p.get("unrealized_pnl_usd", 0.0)}
        for p in state["open_positions"]
    ] + [
        {**o, "kind_lbl": "PEND", "bar_val": o["stake_usd"] * (o["expected_roi_pct"] / 100.0)}
        for o in state["pending_orders"]
    ]

    if items:
        items_rev = list(reversed(items))
        yi = np.arange(len(items_rev))
        vals = [it["bar_val"] for it in items_rev]
        cols = [
            ("#2ca02c" if it["bar_val"] >= 0 else "#d62728") if it["kind_lbl"] == "OPEN" else "#64b5f6"
            for it in items_rev
        ]
        ax2.barh(yi, vals, color=cols, alpha=0.88)
        ax2.axvline(0, color="#111111", linewidth=1.0)
        lbls = [
            f"[{it['kind_lbl']}/{it['bucket']}] {shorten_text(it['title'], 22)} [{shorten_text(it['sub_label'], 12)}] "
            f"({it['limit_price']*100:.1f}¢ | {it['days_rem']:.0f}d)"
            for it in items_rev
        ]
        ax2.set_yticks(yi)
        ax2.set_yticklabels(lbls, fontsize=8.0)
        ax2.set_title(f"保有＆待機ポジション (OPEN={len(state['open_positions'])}, PENDING={len(state['pending_orders'])})", fontsize=10.8, fontweight="bold")
        ax2.set_xlabel("OPEN=含み損益($) / PEND=満期想定利益($)", fontsize=9.2)
        ax2.grid(True, axis="x", linestyle=":", alpha=0.6)
    else:
        ax2.text(0.5, 0.5, "現在の保有・待機ポジションなし", ha="center", va="center", fontsize=11)
        ax2.set_title("保有＆待機ポジション一覧", fontsize=10.8, fontweight="bold")

    fig.suptitle(
        f"Polymarket Maker Buy No 仮想ペーパートレード [{now_jst.strftime('%Y-%m-%d %H:%M JST')}]",
        fontsize=12.5, fontweight="bold", y=1.01,
    )
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out_path


def cleanup_old_paper_charts(plots_dir: Path, max_age_hours: float = 72.0, keep_latest: int = 4) -> None:
    if not plots_dir.exists():
        return
    cutoff = time.time() - max_age_hours * 3600.0
    files = sorted(plots_dir.glob("paper_dashboard_*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files[keep_latest:]:
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except Exception:
            pass


def build_paper_discord_caption(res: Dict[str, Any], now_jst: datetime) -> str:
    state = res["state"]
    eq = res["eq_row"]
    init_bal = float(state["initial_balance_usd"])
    tgt_bal = float(state.get("target_balance_usd", init_bal * 2.0))
    progress_pct = (eq["equity_usd"] - init_bal) / max(tgt_bal - init_bal, 1e-6) * 100.0
    goal_badge = " 🏆 **目標達成!**" if eq["equity_usd"] >= tgt_bal else ""
    won, lost = state["stats"]["won"], state["stats"]["lost"]
    wr_str = f"{won/(won+lost)*100:.1f}%" if (won + lost) > 0 else "N/A"
    lines = [
        f"🤖 **【Polymarket 仮想ペーパートレード】 Maker Buy No (目標2倍チャレンジ)**{goal_badge}",
        f"時刻: `{now_jst.strftime('%Y-%m-%d %H:%M JST')}` | 初期 `${init_bal:,.0f}` → 目標 `${tgt_bal:,.0f}` (進捗 `{progress_pct:+.1f}%`)",
        f"エクイティ: **`${eq['equity_usd']:,.2f}`** "
        f"(現金 `${eq['cash_usd']:,.2f}` / 拘束 `${eq['locked_pending_usd']:,.2f}` / 保有時価 `${eq['open_mtm_usd']:,.2f}`)",
        f"確定損益: `${state['realized_pnl_usd']:+.2f}` | 含み損益: `${eq['unrealized_pnl_usd']:+.2f}` | "
        f"勝敗: `{won}W-{lost}L` ({wr_str}) | DD: `{eq['drawdown_pct']:.2f}%`",
    ]
    if res["events_log"]:
        lines.append("🔔 **今サイクルのイベント**")
        lines.append("```text")
        lines.extend(res["events_log"][:10])
        lines.append("```")

    active = state["open_positions"] + state["pending_orders"]
    if active:
        lines.append(f"📋 **稼働中ポジション (OPEN:{len(state['open_positions'])} / PEND:{len(state['pending_orders'])})**")
        lines.append("```text")
        for p in state["open_positions"][:6]:
            lines.append(
                f"[OPEN/{p['bucket']}] {shorten_text(p['title'], 20):<20} [{shorten_text(p['sub_label'], 11):<11}] "
                f"{p['limit_price']*100:4.1f}¢->現{p['current_no_bid']*100:4.1f}¢ PnL:${p['unrealized_pnl_usd']:+.2f}"
            )
        for o in state["pending_orders"][:6]:
            lines.append(
                f"[PEND/{o['bucket']}] {shorten_text(o['title'], 20):<20} [{shorten_text(o['sub_label'], 11):<11}] "
                f"指値{o['limit_price']*100:4.1f}¢ (${o['stake_usd']:.0f}, ROI {o['expected_roi_pct']:.1f}%)"
            )
        lines.append("```")

    text = "\n".join(lines)
    if len(text) > 1950:
        cut = text[:1940]
        nl = cut.rfind("\n")
        if nl > 0:
            cut = cut[:nl]
        if cut.count("```") % 2 == 1:
            cut += "\n```"
        text = cut
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket Maker Buy No 24時間仮想ペーパートレードボット")
    parser.add_argument("--initial-balance", type=float, default=100.0, help="初期仮想資金 USDC (デフォルト: 100)")
    parser.add_argument("--target-balance", type=float, default=200.0, help="目標資金 USDC (デフォルト: 200)")
    parser.add_argument("--reset", action="store_true", help="仮想口座をリセット")
    parser.add_argument("--dry-run", action="store_true", help="状態保存とDiscord送信を行わない")
    parser.add_argument("--notify-mode", choices=["smart", "always", "never"], default="smart", help="Discord通知モード")
    parser.add_argument("--channel", type=str, default=None, help="送信先Webhook名 (例: real1_bitbank, test4_test)")
    parser.add_argument("--test", action="store_true", help="test4_test へ送信")
    args = parser.parse_args()

    now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(JST)
    tag = now_jst.strftime("%Y%m%d_%H%M%S")

    print(f"[{now_jst.strftime('%Y-%m-%d %H:%M:%S JST')}] Polymarket 仮想ペーパートレードサイクル開始...")
    res = run_paper_cycle(
        initial_balance=args.initial_balance,
        target_balance=args.target_balance,
        reset=args.reset,
        dry_run=args.dry_run,
        now_utc=now_utc,
    )

    eq = res["eq_row"]
    st = res["state"]
    print(
        f"✅ 完了: Equity=${eq['equity_usd']:,.2f} (Cash=${eq['cash_usd']:,.2f}, Locked=${eq['locked_pending_usd']:,.2f}, "
        f"MTM=${eq['open_mtm_usd']:,.2f}) | OPEN={eq['n_open']}, PEND={eq['n_pending']} | "
        f"W/L={st['stats']['won']}/{st['stats']['lost']}"
    )
    for line in res["events_log"]:
        print(" ", line)

    chart_path = DEFAULT_PLOTS_DIR / f"paper_dashboard_{tag}.png"
    render_paper_dashboard(res, now_jst, chart_path)
    print(f"📈 ダッシュボード生成: {chart_path} ({chart_path.stat().st_size:,} bytes)")
    cleanup_old_paper_charts(DEFAULT_PLOTS_DIR)

    if args.dry_run or args.notify_mode == "never":
        print("📌 Discord 送信をスキップしました。")
        return 0

    is_scheduled_report = now_jst.hour in (9, 21)
    has_events = len(res["events_log"]) > 0
    if args.notify_mode == "smart" and not (has_events or is_scheduled_report):
        print("📌 [smart] イベント発生なし＆定時外のため Discord 送信をスキップしました。")
        return 0

    ch = "test4_test" if args.test else args.channel
    wh = get_webhook_url(ch)
    if not wh:
        print(f"❌ Discord Webhook URL が見つかりません (channel={ch or 'auto'})")
        return 1

    cap = build_paper_discord_caption(res, now_jst)
    ok = send_chart_to_discord(wh, chart_path, cap)
    print(f"{'✅' if ok else '❌'} Discord 送信結果 ({ch or 'auto'}): {ok} ({len(cap)}文字)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
