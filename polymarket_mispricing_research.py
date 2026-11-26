"""Polymarket 予測市場 構造的歪み・ミスプライシング徹底検証スクリプト

4本柱の検証:
  1. 大規模キャリブレーション ＆ Yes-Bias 検証 (2025 IS vs 2026 OOS, 単独二択 vs 多択NegRisk, クラスター補正, カテゴリ別)
  2. Favourite-Longshot Bias ＆ 高確率ボンディング (Buy No vs Buy Yes: ROI, APR, ケリー比率, テールリスク, ドローダウン)
  3. ライブ CLOB 板裁定スキャン (NegRisk ΣBid/ΣMid/ΣAsk, 価格ストライク・期限ラダー単調性違反)
  4. ライブ CLOB 流動性・スプレッド・スリッページ地図 ($100 / $1,000 / $10,000 成行コスト vs 理論エッジ)
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
from scipy import stats
from statsmodels.stats.multitest import multipletests
from statsmodels.stats.proportion import proportion_confint

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from polymarket_leadlag_research import (
    ARTIFACT_DIR,
    CLOB_HISTORY_URL,
    GAMMA_EVENTS_URL,
    configure_japanese_font,
    parse_json_list,
    shorten_text,
)

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"

MIS_DIR = BASE_DIR / "Data" / "polymarket_research" / "mispricing"
DAILY_CACHE_DIR = MIS_DIR / "clob_daily_cache"
PLOTS_MIS_DIR = BASE_DIR / "Data" / "plots" / "polymarket_mispricing"


def thread_safe_get(url: str, params: Dict[str, Any], timeout: int = 15, retries: int = 4) -> Any:
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After", 1.2 * (attempt + 1))))
                continue
            if resp.status_code in (400, 404):
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(0.5 * (attempt + 1))
    return None


def parse_dt_utc(val: Any) -> Optional[datetime]:
    if not val:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        # Handle formats like "2026-04-05 06:15:11+00" or ISO8601 "2026-04-05T06:15:11Z"
        s_norm = s.replace("Z", "+00:00")
        if re.search(r"[+-]\d{2}$", s_norm):
            s_norm += ":00"
        dt = datetime.fromisoformat(s_norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            return None


def classify_market_category(question: str, event_title: str) -> str:
    txt = f"{question} {event_title}".lower()
    if any(k in txt for k in [
        "nba", "nfl", "mlb", "nhl", "ufc", "soccer", "world cup", "champions league", "premier league",
        "super bowl", "finals", "stanley cup", "playoffs", "championship", "grand prix", "f1",
        "tennis", "wimbledon", "golf", "masters", "boxing", "counter-strike", "dota", "valorant",
        "league of legends", "esports", "mvp", "ballon d'or", "la liga", "serie a", "bundesliga",
    ]):
        return "Sports"
    if any(k in txt for k in [
        "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto", "xrp", "dogecoin",
        "microstrategy", "fdv", "airdrop", "token", "binance", "coinbase", "polymarket",
    ]):
        return "Crypto"
    if any(k in txt for k in [
        "fed ", "fomc", "bps", "interest rate", "inflation", "cpi", "gdp", "recession",
        "unemployment", "tariff", "crude oil", "wti", "gold", "s&p", "nasdaq", "ipo", "nvidia",
        "openai", "anthropic", "ai ", "tesla", "apple",
    ]):
        return "Econ/Tech"
    if any(k in txt for k in [
        "iran", "israel", "ukraine", "russia", "china", "taiwan", "gaza", "hezbollah",
        "ceasefire", "strike", "invade", "military", "nato", "blockade", "hormuz", "venezuela",
    ]):
        return "Geopolitics"
    if any(k in txt for k in [
        "election", "president", "prime minister", "mayor", "senate", "house", "congress",
        "trump", "biden", "nominee", "governor", "parliament", "shutdown", "cabinet",
    ]):
        return "Politics"
    return "Other"


def fetch_resolved_binary_markets(target_per_year: int = 350, refresh: bool = False) -> pd.DataFrame:
    """Gamma /markets から 2025年(IS) と 2026年(OOS) の決着済み高流動性バイナリ市場を収集"""
    MIS_DIR.mkdir(parents=True, exist_ok=True)
    cache_csv = MIS_DIR / "resolved_markets_pool.csv"
    if cache_csv.exists() and not refresh:
        df = pd.read_csv(cache_csv, encoding="utf-8")
        if len(df) >= 400:
            print(f"[Cache] 決着済み市場メタデータ {len(df)} 件を読み込みました。")
            return df

    print("[Gamma /markets] 決着済みバイナリ市場 (2025-2026, Vol>=$100k) を収集しています...")
    all_rows: List[Dict[str, Any]] = []
    seen_ids = set()

    for page in range(32):  # offset 0..3100
        offset = page * 100
        data = thread_safe_get(
            GAMMA_MARKETS_URL,
            {"closed": "true", "order": "volumeNum", "ascending": "false", "limit": 100, "offset": offset},
            timeout=20,
        )
        if not data or not isinstance(data, list):
            break
        for m in data:
            mid = str(m.get("id") or "")
            if not mid or mid in seen_ids:
                continue
            seen_ids.add(mid)

            vol = float(m.get("volumeNum") or m.get("volume") or 0.0)
            if vol < 100_000.0:
                continue

            outs = parse_json_list(m.get("outcomes"))
            prs = parse_json_list(m.get("outcomePrices"))
            tids = parse_json_list(m.get("clobTokenIds"))
            if outs != ["Yes", "No"] or len(prs) != 2 or not tids:
                continue
            try:
                p_yes_final = float(prs[0])
                p_no_final = float(prs[1])
            except Exception:
                continue
            if p_yes_final >= 0.99 and p_no_final <= 0.01:
                y_val = 1
            elif p_yes_final <= 0.01 and p_no_final >= 0.99:
                y_val = 0
            else:
                continue

            dt_close = parse_dt_utc(m.get("closedTime")) or parse_dt_utc(m.get("endDate"))
            if dt_close is None or dt_close.year < 2025 or dt_close.year > 2026:
                continue

            evs = m.get("events") or []
            parent_id = str(evs[0].get("id")) if evs and isinstance(evs[0], dict) and evs[0].get("id") else f"mkt_{mid}"
            ev_title = str(evs[0].get("title") or "") if evs and isinstance(evs[0], dict) else ""
            question = str(m.get("question") or "")
            cat = classify_market_category(question, ev_title)

            all_rows.append({
                "market_id": mid,
                "yes_token_id": str(tids[0]),
                "question": question,
                "event_id": parent_id,
                "event_title": ev_title,
                "is_neg_risk": bool(m.get("negRisk")),
                "category": cat,
                "volume": vol,
                "y": y_val,
                "close_ts": int(dt_close.timestamp()),
                "close_date": dt_close.strftime("%Y-%m-%d"),
                "year": dt_close.year,
            })

        # 十分なプールが集まったら早期終了
        n25 = sum(1 for r in all_rows if r["year"] == 2025)
        n26 = sum(1 for r in all_rows if r["year"] == 2026)
        if n25 >= target_per_year and n26 >= target_per_year:
            break
        time.sleep(0.08)

    df_all = pd.DataFrame(all_rows)
    if df_all.empty:
        return df_all

    # 2025年と2026年からバランス良く上位を抽出
    df_2025 = df_all[df_all["year"] == 2025].sort_values("volume", ascending=False).head(target_per_year)
    df_2026 = df_all[df_all["year"] == 2026].sort_values("volume", ascending=False).head(target_per_year)
    df_sel = pd.concat([df_2025, df_2026], ignore_index=True).sort_values("volume", ascending=False).reset_index(drop=True)
    df_sel.to_csv(cache_csv, index=False, encoding="utf-8")
    print(f"  抽出完了: 計 {len(df_sel)} 市場 (2025年={len(df_2025)}, 2026年={len(df_2026)})")
    return df_sel


def fetch_single_daily_history(token_id: str, refresh: bool = False) -> Tuple[str, List[Tuple[int, float]]]:
    DAILY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cf = DAILY_CACHE_DIR / f"{token_id[:28]}.json"
    if cf.exists() and not refresh:
        try:
            raw = json.loads(cf.read_text(encoding="utf-8"))
            return token_id, [(int(x[0]), float(x[1])) for x in raw]
        except Exception:
            pass

    data = thread_safe_get(CLOB_HISTORY_URL, {"market": token_id, "interval": "max", "fidelity": 1440}, timeout=15)
    pts: List[Tuple[int, float]] = []
    if data and isinstance(data, dict):
        for pt in data.get("history") or []:
            try:
                pts.append((int(pt["t"]), float(pt["p"])))
            except Exception:
                continue
    pts.sort(key=lambda x: x[0])
    try:
        cf.write_text(json.dumps(pts), encoding="utf-8")
    except Exception:
        pass
    return token_id, pts


def build_calibration_snapshots(df_markets: pd.DataFrame, refresh: bool = False) -> pd.DataFrame:
    """全選定市場の日足履歴を取得し、T-14d / T-7d / T-3d / T-1d スナップショットを生成"""
    snap_cache = MIS_DIR / "calibration_snapshots.csv"
    if snap_cache.exists() and not refresh:
        df_s = pd.read_csv(snap_cache, encoding="utf-8")
        if len(df_s) >= 300:
            print(f"[Cache] スナップショット {len(df_s)} 市場を読み込みました。")
            return df_s

    tokens = df_markets["yes_token_id"].tolist()
    print(f"[CLOB Daily] {len(tokens)} 市場の日足履歴を並列取得中...")
    hist_map: Dict[str, List[Tuple[int, float]]] = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_single_daily_history, tid, refresh): tid for tid in tokens}
        done_cnt = 0
        for fut in as_completed(futs):
            tid, pts = fut.result()
            hist_map[tid] = pts
            done_cnt += 1
            if done_cnt % 150 == 0:
                print(f"  進捗: {done_cnt} / {len(tokens)}")

    horizons = [14, 7, 3, 1]
    records: List[Dict[str, Any]] = []

    for row in df_markets.itertuples(index=False):
        pts = hist_map.get(row.yes_token_id, [])
        if len(pts) < 4:
            continue
        ts_arr = np.array([x[0] for x in pts], dtype=np.int64)
        p_arr = np.array([x[1] for x in pts], dtype=float)

        # 真の決着アンカー: closedTime と最終日足観測時刻の早い方（先読み完全防止）
        t_anchor = min(int(row.close_ts), int(ts_arr[-1]))
        # もし終了間際にすでに100%確定張り付きになっていた場合は、完全決着前の有効市場のみを見るため close_ts 基準で T-h を引く
        rec: Dict[str, Any] = {
            "market_id": row.market_id,
            "yes_token_id": row.yes_token_id,
            "question": row.question,
            "event_id": row.event_id,
            "event_title": row.event_title,
            "is_neg_risk": bool(row.is_neg_risk),
            "category": row.category,
            "volume": float(row.volume),
            "y": int(row.y),
            "close_ts": int(row.close_ts),
            "t_anchor": int(t_anchor),
            "close_date": row.close_date,
            "year": int(row.year),
            "n_daily_pts": len(pts),
        }

        has_any = False
        for h in horizons:
            target_ts = t_anchor - h * 86400
            valid_idx = np.where((ts_arr <= target_ts) & (ts_arr >= target_ts - 4 * 86400))[0]
            if len(valid_idx) > 0:
                p_h = float(p_arr[valid_idx[-1]])
                if 0.0 <= p_h <= 1.0:
                    rec[f"p_{h}d"] = p_h
                    has_any = True
                else:
                    rec[f"p_{h}d"] = np.nan
            else:
                rec[f"p_{h}d"] = np.nan
        if has_any:
            records.append(rec)

    df_snap = pd.DataFrame(records)
    df_snap.to_csv(snap_cache, index=False, encoding="utf-8")
    print(f"  スナップショット構築完了: {len(df_snap)} 市場")
    return df_snap


def analyze_calibration_and_flb(df_snap: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """Pillar 1 (キャリブレーション・クラスター補正・2025 vs 2026) & Pillar 2 (Buy No vs Buy Yes)"""
    bins = [0.0, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 1.0001]
    bin_labels = ["0-5%", "5-10%", "10-20%", "20-30%", "30-40%", "40-50%", "50-60%", "60-70%", "70-80%", "80-90%", "90-95%", "95-100%"]

    calib_rows: List[Dict[str, Any]] = []
    for h in [14, 7, 3, 1]:
        col = f"p_{h}d"
        sub = df_snap.dropna(subset=[col]).copy()
        sub["bin"] = pd.cut(sub[col], bins=bins, labels=bin_labels, right=False)

        for scope, s_df in [
            ("All", sub),
            ("2025_IS", sub[sub["year"] == 2025]),
            ("2026_OOS", sub[sub["year"] == 2026]),
            ("Standalone", sub[~sub["is_neg_risk"]]),
            ("Multi_NegRisk", sub[sub["is_neg_risk"]]),
        ]:
            if s_df.empty:
                continue
            for bl in bin_labels:
                bdf = s_df[s_df["bin"] == bl]
                n = len(bdf)
                if n < 3:
                    continue
                p_mean = float(bdf[col].mean())
                y_mean = float(bdf["y"].mean())
                k_wins = int(bdf["y"].sum())
                ci_lo, ci_hi = proportion_confint(k_wins, n, method="wilson")
                p_bin = float(stats.binomtest(k_wins, n, min(max(p_mean, 1e-4), 1 - 1e-4)).pvalue)
                calib_rows.append({
                    "horizon_d": h, "scope": scope, "bin": bl, "n": n,
                    "p_mean": p_mean, "y_mean": y_mean, "bias_y_minus_p": y_mean - p_mean,
                    "ci_low": float(ci_lo), "ci_high": float(ci_hi), "p_val": p_bin,
                })

    df_calib = pd.DataFrame(calib_rows)
    if not df_calib.empty:
        m_all = (df_calib["scope"] == "All") & (df_calib["horizon_d"].isin([7, 3]))
        df_calib["q_fdr"] = np.nan
        if m_all.sum() > 0:
            _, qv, _, _ = multipletests(df_calib.loc[m_all, "p_val"].values, method="fdr_bh")
            df_calib.loc[m_all, "q_fdr"] = qv

    # 全体指標 & クラスター補正 Yes-Bias
    summary_rows: List[Dict[str, Any]] = []
    for h in [14, 7, 3, 1]:
        col = f"p_{h}d"
        sub = df_snap.dropna(subset=[col]).copy()
        for scope, s_df in [
            ("All", sub),
            ("2025_IS", sub[sub["year"] == 2025]),
            ("2026_OOS", sub[sub["year"] == 2026]),
            ("Standalone", sub[~sub["is_neg_risk"]]),
            ("Multi_NegRisk", sub[sub["is_neg_risk"]]),
        ] + [(f"Cat_{c}", sub[sub["category"] == c]) for c in sorted(sub["category"].unique())]:
            if len(s_df) < 10:
                continue
            diffs = (s_df["y"] - s_df[col]).values
            brier = float(np.mean((s_df[col] - s_df["y"]) ** 2))
            # イベント単位クラスター補正平均 (同一イベント内の市場群を1観測に圧縮)
            ev_grp = s_df.assign(diff=diffs).groupby("event_id")["diff"].mean().values
            _, p_raw = stats.ttest_1samp(diffs, 0.0)
            _, p_clust = stats.ttest_1samp(ev_grp, 0.0) if len(ev_grp) >= 5 else (0.0, 1.0)
            summary_rows.append({
                "horizon_d": h, "scope": scope, "n_markets": len(s_df), "n_events": len(ev_grp),
                "mean_p": float(s_df[col].mean()), "mean_y": float(s_df["y"].mean()),
                "yes_bias_pt": float(diffs.mean() * 100.0),
                "cluster_bias_pt": float(ev_grp.mean() * 100.0),
                "p_raw": float(p_raw), "p_cluster": float(p_clust),
                "brier_score": brier,
            })
    df_bias_summary = pd.DataFrame(summary_rows)

    # Pillar 2: Buy No vs Buy Yes in High-Win-Probability Bands (using T-7d & T-3d)
    win_bands = [
        ("75-80%", 0.75, 0.80),
        ("80-85%", 0.80, 0.85),
        ("85-90%", 0.85, 0.90),
        ("90-95%", 0.90, 0.95),
        ("95-98%", 0.95, 0.98),
        ("98-99.5%", 0.98, 0.995),
    ]
    flb_rows: List[Dict[str, Any]] = []
    for h in [14, 7, 3, 1]:
        col = f"p_{h}d"
        sub = df_snap.dropna(subset=[col]).copy()
        for scope, s_df in [
            ("All", sub),
            ("2025_IS", sub[sub["year"] == 2025]),
            ("2026_OOS", sub[sub["year"] == 2026]),
            ("Multi_NegRisk", sub[sub["is_neg_risk"]]),
            ("Standalone", sub[~sub["is_neg_risk"]]),
        ]:
            for bname, blo, bhi in win_bands:
                # 1) Buy No: p_yes in [1 - bhi, 1 - blo) -> cost_no = 1 - p_yes in (blo, bhi]
                m_no = s_df[(s_df[col] > (1.0 - bhi)) & (s_df[col] <= (1.0 - blo))]
                # 2) Buy Yes: p_yes in [blo, bhi) -> cost_yes = p_yes
                m_yes = s_df[(s_df[col] >= blo) & (s_df[col] < bhi)]

                for side, m_df in [("Buy_No", m_no), ("Buy_Yes", m_yes)]:
                    n = len(m_df)
                    if n < 5:
                        continue
                    if side == "Buy_No":
                        cost = 1.0 - m_df[col].values
                        win = (1 - m_df["y"].values).astype(float)
                    else:
                        cost = m_df[col].values
                        win = m_df["y"].values.astype(float)

                    roi = (win - cost) / cost * 100.0
                    mean_roi = float(roi.mean())
                    apr = mean_roi * (365.0 / h)
                    win_rate = float(win.mean())
                    mean_cost = float(cost.mean())
                    upset_rate = 1.0 - win_rate
                    # 1回の敗北を取り戻すのに必要な連勝数
                    wins_to_recover = mean_cost / max(1.0 - mean_cost, 1e-6)
                    # Binary Kelly f* = (p*b - q)/b = (p - cost)/(1 - cost)
                    kelly_f = max(0.0, (win_rate - mean_cost) / max(1.0 - mean_cost, 1e-6))
                    _, p_val = stats.ttest_1samp(roi, 0.0) if n >= 5 and np.std(roi, ddof=1) > 0 else (0.0, 1.0)
                    flb_rows.append({
                        "horizon_d": h, "scope": scope, "side": side, "win_band": bname,
                        "n": n, "mean_entry_cents": mean_cost * 100.0,
                        "expected_win_pct": mean_cost * 100.0, "actual_win_pct": win_rate * 100.0,
                        "upset_pct": upset_rate * 100.0,
                        "mean_roi_pct": mean_roi, "apr_pct": apr,
                        "wins_to_recover": wins_to_recover, "kelly_pct": kelly_f * 100.0,
                        "p_val": float(p_val),
                    })

    df_flb = pd.DataFrame(flb_rows)
    if not df_flb.empty:
        m_fdr = (df_flb["scope"] == "All") & (df_flb["horizon_d"].isin([7, 3]))
        df_flb["q_fdr"] = np.nan
        if m_fdr.sum() > 0:
            _, qv, _, _ = multipletests(df_flb.loc[m_fdr, "p_val"].values, method="fdr_bh")
            df_flb.loc[m_fdr, "q_fdr"] = qv

    return {"df_calib": df_calib, "df_bias_summary": df_bias_summary, "df_flb": df_flb}


def fetch_book_metrics(token_id: str) -> Optional[Dict[str, Any]]:
    """CLOB /book から最良気配・スプレッド・厚み・成行スリッページを正確に算出"""
    bk = thread_safe_get(CLOB_BOOK_URL, {"token_id": token_id}, timeout=12)
    if not bk or not isinstance(bk, dict):
        return None
    bids_raw = bk.get("bids") or []
    asks_raw = bk.get("asks") or []
    bids = sorted([(float(b["price"]), float(b["size"])) for b in bids_raw if float(b.get("size", 0)) > 0], key=lambda x: x[0], reverse=True)
    asks = sorted([(float(a["price"]), float(a["size"])) for a in asks_raw if float(a.get("size", 0)) > 0], key=lambda x: x[0])
    if not bids or not asks:
        return None

    best_bid, best_bid_sz = bids[0]
    best_ask, best_ask_sz = asks[0]
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return None

    mid = 0.5 * (best_bid + best_ask)
    spread_cents = (best_ask - best_bid) * 100.0
    top_bid_usd = best_bid * best_bid_sz
    top_ask_usd = best_ask * best_ask_sz
    depth_2c_bid = sum(p * s for p, s in bids if p >= mid - 0.02)
    depth_2c_ask = sum(p * s for p, s in asks if p <= mid + 0.02)

    def calc_buy_slip_cents(target_usd: float) -> float:
        rem_usd = target_usd
        shares = 0.0
        for p, sz in asks:
            lvl_usd = p * sz
            if lvl_usd >= rem_usd:
                shares += rem_usd / p
                rem_usd = 0.0
                break
            shares += sz
            rem_usd -= lvl_usd
        if rem_usd > 1e-6 or shares <= 0:
            return np.nan
        vwap = target_usd / shares
        return (vwap - mid) * 100.0

    return {
        "token_id": token_id,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_cents": spread_cents,
        "top_depth_usd": min(top_bid_usd, top_ask_usd),
        "depth_2c_usd": depth_2c_bid + depth_2c_ask,
        "slip_100_cents": calc_buy_slip_cents(100.0),
        "slip_1000_cents": calc_buy_slip_cents(1000.0),
        "slip_10000_cents": calc_buy_slip_cents(10000.0),
    }


def parse_strike_from_title(name: str) -> Optional[Tuple[str, float]]:
    """'↑ 150,000' or '↓ 55,000' or '↑ $160' -> (dir, value)"""
    s = str(name or "").strip()
    m = re.search(r"([↑↓])\s*\$?([\d,]+(?:\.\d+)?)", s)
    if not m:
        return None
    val = float(m.group(2).replace(",", ""))
    return m.group(1), val


def parse_date_from_title(name: str, ref_dt: Optional[datetime] = None) -> Optional[datetime]:
    s = str(name or "").strip()
    now_ref = ref_dt or datetime.now(timezone.utc).replace(tzinfo=None)
    for fmt in ["%B %d, %Y", "%B %d", "%b %d, %Y", "%b %d"]:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=now_ref.year)
                # 年跨ぎ期限 (例: 現在11月で "January 31" なら翌年)
                if (now_ref - dt).days > 45:
                    dt = dt.replace(year=now_ref.year + 1)
            return dt
        except Exception:
            continue
    return None


def scan_live_clob_structure(refresh: bool = False) -> Dict[str, Any]:
    """Pillar 3 & 4: ライブ NegRisk裁定・ストライク/期限ラダー単調性・板流動性地図"""
    live_cache = MIS_DIR / "live_orderbook_scan.json"
    if live_cache.exists() and not refresh:
        cached = json.loads(live_cache.read_text(encoding="utf-8"))
        if cached.get("books"):
            print(f"[Cache] ライブCLOB板スキャン ({len(cached['books'])} トークン) を読み込みました。")
            return cached

    print("[Live CLOB] アクティブ上位100イベントから NegRisk・ラダー・板データをスキャン中...")
    events = thread_safe_get(GAMMA_EVENTS_URL, {"active": "true", "closed": "false", "order": "volume24hr", "ascending": "false", "limit": 100}, timeout=20) or []

    negrisk_targets: List[Dict[str, Any]] = []
    ladder_targets: List[Dict[str, Any]] = []
    token_meta: Dict[str, Dict[str, Any]] = {}

    for ev in events:
        title = str(ev.get("title") or "")
        all_ms = ev.get("markets") or []
        ms = [m for m in all_ms if m.get("active") is True and not m.get("closed")]
        if not ms:
            continue

        is_nr = bool(ev.get("negRisk"))
        is_hit = ("what price will" in title.lower() or "what will wti" in title.lower()) and len(ms) >= 4
        date_cnt = sum(1 for m in ms if parse_date_from_title(m.get("groupItemTitle") or "") is not None)
        is_deadline_ladder = ("by" in title.lower() or "through" in title.lower()) and date_cnt >= 3 and not is_nr

        # NegRiskは全アクティブ候補(<=35択)を完全に揃えられるイベントのみ裁定対象にする
        want_nr = is_nr and 2 <= len(all_ms) <= 35 and len(ms) == len(all_ms) and len(negrisk_targets) < 12
        want_lad = ((is_hit and len(ladder_targets) < 6) or (is_deadline_ladder and len(ladder_targets) < 10))
        chosen_ms = ms if (want_nr or want_lad) else sorted(ms, key=lambda x: float(x.get("volume") or 0), reverse=True)[:2]

        ev_tokens = []
        for m in chosen_ms:
            outs = parse_json_list(m.get("outcomes"))
            tids = parse_json_list(m.get("clobTokenIds"))
            if outs != ["Yes", "No"] or not tids:
                continue
            tid = str(tids[0])
            sub_lbl = str(m.get("groupItemTitle") or m.get("question") or "")
            token_meta[tid] = {
                "event_title": title,
                "sub_label": sub_lbl,
                "volume": float(m.get("volume") or 0.0),
                "is_neg_risk": is_nr,
            }
            ev_tokens.append((tid, sub_lbl))

        if want_nr and len(ev_tokens) == len(all_ms):
            negrisk_targets.append({"title": title, "volume24h": float(ev.get("volume24hr") or 0), "tokens": ev_tokens})
        if want_lad and len(ev_tokens) >= 4:
            ladder_targets.append({
                "title": title,
                "kind": "strike" if is_hit else "deadline",
                "tokens": ev_tokens,
            })

    all_tids = list(token_meta.keys())[:360]
    print(f"  板取得対象トークン数: {len(all_tids)} (NegRisk={len(negrisk_targets)}イベント, Ladder={len(ladder_targets)}イベント)")

    books_map: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_book_metrics, tid): tid for tid in all_tids}
        for fut in as_completed(futs):
            res_b = fut.result()
            if res_b:
                tid = res_b["token_id"]
                books_map[tid] = {**res_b, **token_meta.get(tid, {})}

    # 1) NegRisk 合計確率分析
    negrisk_summary: List[Dict[str, Any]] = []
    for nr in negrisk_targets:
        bks = [books_map[tid] for tid, _ in nr["tokens"] if tid in books_map]
        if len(bks) < len(nr["tokens"]) * 0.8:  # 8割以上取得できたイベントのみ
            continue
        sum_bid = sum(b["best_bid"] for b in bks)
        sum_mid = sum(b["mid"] for b in bks)
        sum_ask = sum(b["best_ask"] for b in bks)
        negrisk_summary.append({
            "title": nr["title"],
            "n_outcomes": len(bks),
            "sum_bid": sum_bid,
            "sum_mid": sum_mid,
            "sum_ask": sum_ask,
            "long_arb_edge_cents": (1.0 - sum_ask) * 100.0,
            "short_arb_edge_cents": (sum_bid - 1.0) * 100.0,
            "overround_mid_pct": (sum_mid - 1.0) * 100.0,
        })

    # 2) ストライク・期限ラダー単調性チェック
    ladder_violations: List[Dict[str, Any]] = []
    ladder_curves: List[Dict[str, Any]] = []
    for lad in ladder_targets:
        if lad["kind"] == "strike":
            for dir_char in ["↑", "↓"]:
                pts = []
                for tid, lbl in lad["tokens"]:
                    if tid not in books_map:
                        continue
                    parsed = parse_strike_from_title(lbl)
                    if parsed and parsed[0] == dir_char:
                        pts.append((parsed[1], lbl, books_map[tid]))
                # 上昇(↑)はK大ほど難(単調減少)、下落(↓)はK小ほど難(K大で単調増加)
                pts.sort(key=lambda x: x[0])
                if len(pts) < 3:
                    continue
                ladder_curves.append({
                    "title": f"{lad['title']} ({dir_char})",
                    "kind": f"strike_{dir_char}",
                    "points": [{"x": str(p[0]), "label": p[1], "bid": p[2]["best_bid"], "mid": p[2]["mid"], "ask": p[2]["best_ask"]} for p in pts],
                })
                for i in range(len(pts)):
                    for j in range(i + 1, len(pts)):
                        k_i, lbl_i, bi = pts[i]
                        k_j, lbl_j, bj = pts[j]
                        # ↑: i<j なのに mid_j > mid_i なら違反
                        # ↓: i<j なのに mid_i > mid_j なら違反
                        if dir_char == "↑":
                            mid_viol = bj["mid"] - bi["mid"]
                            exec_viol = bj["best_bid"] - bi["best_ask"]
                            pair_desc = f"{lbl_j} ({bj['mid']*100:.1f}¢) > {lbl_i} ({bi['mid']*100:.1f}¢)"
                        else:
                            mid_viol = bi["mid"] - bj["mid"]
                            exec_viol = bi["best_bid"] - bj["best_ask"]
                            pair_desc = f"{lbl_i} ({bi['mid']*100:.1f}¢) > {lbl_j} ({bj['mid']*100:.1f}¢)"
                        if mid_viol > 0.005:
                            ladder_violations.append({
                                "title": lad["title"], "kind": f"strike_{dir_char}",
                                "pair": pair_desc, "mid_viol_cents": mid_viol * 100.0,
                                "exec_arb_cents": exec_viol * 100.0,
                            })
        else:
            pts_d = []
            for tid, lbl in lad["tokens"]:
                if tid not in books_map:
                    continue
                dt = parse_date_from_title(lbl)
                if dt:
                    pts_d.append((dt, lbl, books_map[tid]))
            pts_d.sort(key=lambda x: x[0])
            if len(pts_d) >= 3:
                ladder_curves.append({
                    "title": lad["title"],
                    "kind": "deadline",
                    "points": [{"x": p[0].strftime("%m/%d"), "label": p[1], "bid": p[2]["best_bid"], "mid": p[2]["mid"], "ask": p[2]["best_ask"]} for p in pts_d],
                })
                # 期限が後のほうが累積確率は高いはず (D_i < D_j => p_i <= p_j)
                for i in range(len(pts_d)):
                    for j in range(i + 1, len(pts_d)):
                        _, lbl_i, bi = pts_d[i]
                        _, lbl_j, bj = pts_d[j]
                        mid_viol = bi["mid"] - bj["mid"]
                        exec_viol = bi["best_bid"] - bj["best_ask"]
                        if mid_viol > 0.005:
                            ladder_violations.append({
                                "title": lad["title"], "kind": "deadline",
                                "pair": f"{lbl_i} ({bi['mid']*100:.1f}¢) > {lbl_j} ({bj['mid']*100:.1f}¢)",
                                "mid_viol_cents": mid_viol * 100.0,
                                "exec_arb_cents": exec_viol * 100.0,
                            })

    out = {
        "books": list(books_map.values()),
        "negrisk_summary": negrisk_summary,
        "ladder_violations": ladder_violations,
        "ladder_curves": ladder_curves,
    }
    live_cache.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def generate_mispricing_charts(
    df_snap: pd.DataFrame,
    p12: Dict[str, pd.DataFrame],
    live: Dict[str, Any],
) -> List[Path]:
    configure_japanese_font()
    PLOTS_MIS_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    saved: List[Path] = []

    df_calib = p12["df_calib"]
    df_bias = p12["df_bias_summary"]
    df_flb = p12["df_flb"]
    df_books = pd.DataFrame(live["books"])

    # ==========================================================
    # Chart 1: Calibration Curve & 2025(IS) vs 2026(OOS) Bias
    # ==========================================================
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6.5))
    ax1.plot([0, 100], [0, 100], "k--", linewidth=1.2, label="完全較正ライン (y = p)")
    for h, col, mk in [(7, "#1f77b4", "o"), (3, "#ff7f0e", "s")]:
        c_sub = df_calib[(df_calib["scope"] == "All") & (df_calib["horizon_d"] == h)]
        xs = c_sub["p_mean"].values * 100.0
        ys = c_sub["y_mean"].values * 100.0
        ylo = np.maximum(0.0, ys - c_sub["ci_low"].values * 100.0)
        yhi = np.maximum(0.0, c_sub["ci_high"].values * 100.0 - ys)
        ax1.errorbar(xs, ys, yerr=[ylo, yhi], fmt=f"-{mk}", color=col, capsize=3, linewidth=2.0, label=f"決着 {h}日前 (T-{h}d, n={int(c_sub['n'].sum())})")
    ax1.set_title("Polymarket 確率キャリブレーション曲線 (Wilson 95%CI)", fontsize=11.5, fontweight="bold")
    ax1.set_xlabel("市場織り込み Yes 確率 (%)", fontsize=9.5)
    ax1.set_ylabel("実際の Yes 実現率 (%)", fontsize=9.5)
    ax1.set_xlim(-2, 102)
    ax1.set_ylim(-2, 102)
    ax1.grid(True, linestyle=":", alpha=0.6)
    ax1.legend(fontsize=8.8, loc="upper left")

    # 右パネル: 2025 IS vs 2026 OOS の誤差 (y - p, pt) @ T-7d
    c25 = df_calib[(df_calib["scope"] == "2025_IS") & (df_calib["horizon_d"] == 7)].set_index("bin")
    c26 = df_calib[(df_calib["scope"] == "2026_OOS") & (df_calib["horizon_d"] == 7)].set_index("bin")
    all_bins = [b for b in ["0-5%", "5-10%", "10-20%", "20-30%", "30-40%", "40-50%", "50-60%", "60-70%", "70-80%", "80-90%", "90-95%", "95-100%"] if b in c25.index or b in c26.index]
    xb = np.arange(len(all_bins))
    wb = 0.36
    v25 = [float(c25.loc[b, "bias_y_minus_p"] * 100.0) if b in c25.index else 0.0 for b in all_bins]
    v26 = [float(c26.loc[b, "bias_y_minus_p"] * 100.0) if b in c26.index else 0.0 for b in all_bins]
    ax2.bar(xb - wb / 2, v25, width=wb, label="2025年 (IS)", color="#1f77b4", alpha=0.88)
    ax2.bar(xb + wb / 2, v26, width=wb, label="2026年 (OOS)", color="#ff7f0e", alpha=0.88)
    ax2.axhline(0, color="#111111", linewidth=1.0)
    ax2.set_xticks(xb)
    ax2.set_xticklabels(all_bins, rotation=35, ha="right", fontsize=8.5)
    ax2.set_title("価格帯別 キャリブレーション誤差 (実現率 - 市場確率, T-7d)\nマイナス = Yes過大評価(Buy No有利) / プラス = Yes過小評価", fontsize=11.0, fontweight="bold")
    ax2.set_ylabel("較正誤差 (pt)", fontsize=9.5)
    ax2.grid(True, axis="y", linestyle=":", alpha=0.6)
    ax2.legend(fontsize=8.8)

    fig.tight_layout()
    p1 = PLOTS_MIS_DIR / "mis_01_calibration_and_yes_bias.png"
    fig.savefig(p1, dpi=150, bbox_inches="tight")
    plt.close(fig)
    saved.append(p1)

    # ==========================================================
    # Chart 2: Buy No vs Buy Yes (FLB & Tail-Harvesting, ROI & APR)
    # ==========================================================
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6.2))
    bands = ["75-80%", "80-85%", "85-90%", "90-95%", "95-98%", "98-99.5%"]
    flb7 = df_flb[(df_flb["horizon_d"] == 7) & (df_flb["scope"] == "All")]
    xb = np.arange(len(bands))
    wb = 0.35
    y_no = [float(flb7[(flb7["side"] == "Buy_No") & (flb7["win_band"] == b)]["mean_roi_pct"].mean()) for b in bands]
    y_yes = [float(flb7[(flb7["side"] == "Buy_Yes") & (flb7["win_band"] == b)]["mean_roi_pct"].mean()) for b in bands]
    ax1.bar(xb - wb / 2, np.nan_to_num(y_no, nan=0.0), width=wb, label="Buy No (大穴Yes売り)", color="#2ca02c", alpha=0.88)
    ax1.bar(xb + wb / 2, np.nan_to_num(y_yes, nan=0.0), width=wb, label="Buy Yes (本命Yes買い)", color="#d62728", alpha=0.88)
    ax1.axhline(0, color="#111111", linewidth=1.0)
    ax1.set_xticks(xb)
    ax1.set_xticklabels(bands, fontsize=9.5)
    ax1.set_title("勝率帯別 1トレード平均ROI: Buy No vs Buy Yes (T-7d, グロス)", fontsize=11.3, fontweight="bold")
    ax1.set_xlabel("エントリー時の暗黙勝率帯", fontsize=9.5)
    ax1.set_ylabel("平均 ROI (%)", fontsize=9.5)
    ax1.grid(True, axis="y", linestyle=":", alpha=0.6)
    ax1.legend(fontsize=8.8)

    # 右パネル: Buy No 2025(IS) vs 2026(OOS) @ T-7d
    f25 = df_flb[(df_flb["horizon_d"] == 7) & (df_flb["scope"] == "2025_IS") & (df_flb["side"] == "Buy_No")].set_index("win_band")
    f26 = df_flb[(df_flb["horizon_d"] == 7) & (df_flb["scope"] == "2026_OOS") & (df_flb["side"] == "Buy_No")].set_index("win_band")
    r25 = [float(f25.loc[b, "mean_roi_pct"]) if b in f25.index else 0.0 for b in bands]
    r26 = [float(f26.loc[b, "mean_roi_pct"]) if b in f26.index else 0.0 for b in bands]
    ax2.bar(xb - wb / 2, r25, width=wb, label="Buy No: 2025年 (IS)", color="#1f77b4", alpha=0.88)
    ax2.bar(xb + wb / 2, r26, width=wb, label="Buy No: 2026年 (OOS)", color="#ff7f0e", alpha=0.88)
    ax2.axhline(0, color="#111111", linewidth=1.0)
    ax2.set_xticks(xb)
    ax2.set_xticklabels(bands, fontsize=9.5)
    ax2.set_title("Buy No (大穴Yes売り) の年度別再現性: 2025(IS) vs 2026(OOS) @ T-7d", fontsize=11.3, fontweight="bold")
    ax2.set_xlabel("エントリー時の No 暗黙勝率帯", fontsize=9.5)
    ax2.set_ylabel("平均 ROI (%)", fontsize=9.5)
    ax2.grid(True, axis="y", linestyle=":", alpha=0.6)
    ax2.legend(fontsize=8.8)

    fig.tight_layout()
    p2 = PLOTS_MIS_DIR / "mis_02_buy_no_vs_buy_yes_flb.png"
    fig.savefig(p2, dpi=150, bbox_inches="tight")
    plt.close(fig)
    saved.append(p2)

    # ==========================================================
    # Chart 3: Category Yes-Bias & Equity Curve / Drawdown (2025->2026)
    # ==========================================================
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6.2))
    cat_sub = df_bias[(df_bias["horizon_d"] == 7) & (df_bias["scope"].str.startswith("Cat_"))].sort_values("yes_bias_pt")
    if not cat_sub.empty:
        cnames = [s.replace("Cat_", "") + f"\n(n={n})" for s, n in zip(cat_sub["scope"], cat_sub["n_markets"])]
        cols = ["#2ca02c" if v < 0 else "#d62728" for v in cat_sub["yes_bias_pt"]]
        ax1.barh(range(len(cnames)), cat_sub["yes_bias_pt"].values, color=cols, alpha=0.85)
        ax1.axvline(0, color="#111111", linewidth=1.0)
        ax1.set_yticks(range(len(cnames)))
        ax1.set_yticklabels(cnames, fontsize=9.5)
        ax1.set_title("カテゴリ別 Yes-Bias (実現率 - 市場確率, T-7d)\n左(緑)=Yes過大評価 / 右(赤)=Yes過小評価", fontsize=11.0, fontweight="bold")
        ax1.set_xlabel("平均較正誤差 (pt)", fontsize=9.5)
        ax1.grid(True, axis="x", linestyle=":", alpha=0.6)

    # 右パネル: Buy No (勝率80-98%, 即ち p_yes in 2-20%) の構造別 (多択NegRisk vs 単独二択) 時系列累積ROI
    sub7 = df_snap.dropna(subset=["p_7d"]).sort_values("close_ts").copy()
    sub_no_nr = sub7[(sub7["is_neg_risk"]) & (sub7["p_7d"] >= 0.02) & (sub7["p_7d"] <= 0.20)].copy()
    sub_no_st = sub7[(~sub7["is_neg_risk"]) & (sub7["p_7d"] >= 0.02) & (sub7["p_7d"] <= 0.20)].copy()
    sub_yes = sub7[(sub7["p_7d"] >= 0.80) & (sub7["p_7d"] <= 0.98)].copy()

    if not sub_no_nr.empty:
        c_nr = 1.0 - sub_no_nr["p_7d"].values
        w_nr = (1 - sub_no_nr["y"].values).astype(float)
        roi_nr = (w_nr - c_nr) / c_nr * 100.0
        c_nr_t = np.minimum(0.995, c_nr + 0.010)
        roi_nr_t = (w_nr - c_nr_t) / c_nr_t * 100.0
        ax2.plot(np.arange(1, len(roi_nr) + 1), np.cumsum(roi_nr), color="#2ca02c", linewidth=2.2, label=f"Buy No 多択NegRisk (Maker 0¢, n={len(roi_nr)})")
        ax2.plot(np.arange(1, len(roi_nr_t) + 1), np.cumsum(roi_nr_t), color="#1f77b4", linestyle="--", linewidth=1.7, label="Buy No 多択NegRisk (+1.0¢コスト)")
    if not sub_no_st.empty:
        c_st = 1.0 - sub_no_st["p_7d"].values
        w_st = (1 - sub_no_st["y"].values).astype(float)
        roi_st = (w_st - c_st) / c_st * 100.0
        ax2.plot(np.arange(1, len(roi_st) + 1), np.cumsum(roi_st), color="#d62728", linewidth=2.0, label=f"Buy No 単独二択 (地政学等の急変被弾, n={len(roi_st)})")
    if not sub_yes.empty:
        c_yes = sub_yes["p_7d"].values
        roi_yes = (sub_yes["y"].values - c_yes) / c_yes * 100.0
        ax2.plot(np.arange(1, len(roi_yes) + 1), np.cumsum(roi_yes), color="#ff7f0e", linewidth=1.8, label=f"Buy Yes 本命 (80-98%, n={len(roi_yes)})")

    ax2.axhline(0, color="#111111", linewidth=0.9)
    ax2.set_title("高勝率(80-98%)戦略 構造別・累積ROI推移 (T-7d: 多択NegRisk vs 単独二択)", fontsize=10.8, fontweight="bold")
    ax2.set_xlabel("トレード順 (時系列)", fontsize=9.5)
    ax2.set_ylabel("単利累積 ROI (%)", fontsize=9.5)
    ax2.grid(True, linestyle=":", alpha=0.6)
    ax2.legend(fontsize=8.0, loc="best")

    fig.tight_layout()
    p3 = PLOTS_MIS_DIR / "mis_03_category_mispricing_and_drawdown.png"
    fig.savefig(p3, dpi=150, bbox_inches="tight")
    plt.close(fig)
    saved.append(p3)

    # ==========================================================
    # Chart 4: Live NegRisk Arbitrage & Strike/Deadline Ladder Curves
    # ==========================================================
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6.2))
    nr_list = live.get("negrisk_summary") or []
    if nr_list:
        nr_df = pd.DataFrame(nr_list).sort_values("sum_mid")
        yn = np.arange(len(nr_df))
        ax1.hlines(yn, nr_df["sum_bid"] * 100.0, nr_df["sum_ask"] * 100.0, color="#607d8b", linewidth=3.0, alpha=0.8, label="ΣBestBid 〜 ΣBestAsk レンジ")
        ax1.scatter(nr_df["sum_mid"] * 100.0, yn, color="#1f77b4", s=45, zorder=5, label="ΣMid (%)")
        ax1.axvline(100.0, color="#d62728", linestyle="--", linewidth=1.4, label="理論値 100% (無裁定ライン)")
        ax1.set_yticks(yn)
        ax1.set_yticklabels([f"{shorten_text(t, 28)} ({k}択)" for t, k in zip(nr_df["title"], nr_df["n_outcomes"])], fontsize=8.5)
        ax1.set_title("ライブ NegRisk 排他イベント合計確率 (ΣBid / ΣMid / ΣAsk)", fontsize=11.0, fontweight="bold")
        ax1.set_xlabel("全候補 Yes 合計価格 (%)", fontsize=9.5)
        ax1.grid(True, axis="x", linestyle=":", alpha=0.6)
        ax1.legend(fontsize=8.3, loc="best")

    curves = live.get("ladder_curves") or []
    # 最初の代表的な価格ストライク上昇(↑)ラダーと期限ラダーを表示
    strike_curves = [c for c in curves if c["kind"] == "strike_↑"][:2]
    deadline_curves = [c for c in curves if c["kind"] == "deadline"][:2]
    for c in (strike_curves + deadline_curves)[:3]:
        pts = c["points"]
        xs = np.arange(len(pts))
        mids = [p["mid"] * 100.0 for p in pts]
        ax2.plot(xs, mids, marker="o", linewidth=2.0, label=shorten_text(c["title"], 34))
    ax2.set_title("ライブ ストライク / 期限ラダー確率曲線 (単調性チェック)", fontsize=11.0, fontweight="bold")
    ax2.set_xlabel("ラダー順序インデックス (昇順)", fontsize=9.5)
    ax2.set_ylabel("Mid 確率 (%)", fontsize=9.5)
    ax2.grid(True, linestyle=":", alpha=0.6)
    ax2.legend(fontsize=8.0, loc="best")

    fig.tight_layout()
    p4 = PLOTS_MIS_DIR / "mis_04_arbitrage_and_monotonicity.png"
    fig.savefig(p4, dpi=150, bbox_inches="tight")
    plt.close(fig)
    saved.append(p4)

    # ==========================================================
    # Chart 5: CLOB Liquidity, Spread & Slippage Map
    # ==========================================================
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6.2))
    if not df_books.empty:
        pbins = [0.0, 0.10, 0.30, 0.70, 0.90, 1.0]
        plbls = ["1-10¢", "10-30¢", "30-70¢", "70-90¢", "90-99¢"]
        df_books["p_band"] = pd.cut(df_books["mid"], bins=pbins, labels=plbls)
        grp_b = df_books.groupby("p_band", observed=False)
        xb = np.arange(len(plbls))
        wb = 0.22
        med_spr = grp_b["spread_cents"].median().reindex(plbls).fillna(0)
        med_s100 = grp_b["slip_100_cents"].median().reindex(plbls).fillna(0)
        med_s1k = grp_b["slip_1000_cents"].median().reindex(plbls).fillna(0)
        med_s10k = grp_b["slip_10000_cents"].median().reindex(plbls).fillna(0)

        ax1.bar(xb - 1.5 * wb, med_spr.values, width=wb, label="Bid-Ask スプレッド中央値", color="#607d8b")
        ax1.bar(xb - 0.5 * wb, med_s100.values, width=wb, label="$100 成行スリップ (対Mid)", color="#2ca02c")
        ax1.bar(xb + 0.5 * wb, med_s1k.values, width=wb, label="$1,000 成行スリップ (対Mid)", color="#ff7f0e")
        ax1.bar(xb + 1.5 * wb, med_s10k.values, width=wb, label="$10,000 成行スリップ (対Mid)", color="#d62728")
        ax1.set_xticks(xb)
        ax1.set_xticklabels(plbls, fontsize=9.5)
        ax1.set_title(f"ライブ CLOB 価格帯別 スプレッド＆注文サイズ別スリッページ (n={len(df_books)})", fontsize=11.0, fontweight="bold")
        ax1.set_ylabel("執行コスト (¢ / シェア)", fontsize=9.5)
        ax1.grid(True, axis="y", linestyle=":", alpha=0.6)
        ax1.legend(fontsize=8.0, loc="best")

        # 右パネル: 理論エッジ vs 実測執行コスト (価格帯別 Mid±2¢ 板厚み)
        med_depth = grp_b["depth_2c_usd"].median().reindex(plbls).fillna(0)
        mean_depth = grp_b["depth_2c_usd"].mean().reindex(plbls).fillna(0)
        ax2.bar(xb - 0.18, med_depth.values, width=0.35, label="Mid±2¢ 板厚み 中央値 ($)", color="#1f77b4")
        ax2.bar(xb + 0.18, mean_depth.values, width=0.35, label="Mid±2¢ 板厚み 平均値 ($)", color="#90caf9")
        ax2.set_yscale("log")
        ax2.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda y, _: f"${y:,.0f}"))
        ax2.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax2.set_xticks(xb)
        ax2.set_xticklabels(plbls, fontsize=9.5)
        ax2.set_title("価格帯別 CLOB 板厚み (Mid±2¢ 以内 USD, 対数軸)", fontsize=11.0, fontweight="bold")
        ax2.set_ylabel("板厚み (USD, 対数スケール)", fontsize=9.5)
        ax2.grid(True, axis="y", linestyle=":", alpha=0.6)
        ax2.legend(fontsize=8.5, loc="best")

    fig.tight_layout()
    p5 = PLOTS_MIS_DIR / "mis_05_clob_liquidity_and_spread_map.png"
    fig.savefig(p5, dpi=150, bbox_inches="tight")
    plt.close(fig)
    saved.append(p5)

    for p in saved:
        shutil.copy2(p, ARTIFACT_DIR / p.name)
    return saved


def save_and_print_mispricing_summary(
    df_snap: pd.DataFrame,
    p12: Dict[str, pd.DataFrame],
    live: Dict[str, Any],
    charts: List[Path],
) -> None:
    MIS_DIR.mkdir(parents=True, exist_ok=True)
    p12["df_calib"].to_csv(MIS_DIR / "calibration_bins.csv", index=False, encoding="utf-8")
    p12["df_bias_summary"].to_csv(MIS_DIR / "yes_bias_summary.csv", index=False, encoding="utf-8")
    p12["df_flb"].to_csv(MIS_DIR / "flb_buy_no_vs_yes.csv", index=False, encoding="utf-8")
    pd.DataFrame(live["books"]).to_csv(MIS_DIR / "live_books.csv", index=False, encoding="utf-8")
    pd.DataFrame(live["negrisk_summary"]).to_csv(MIS_DIR / "live_negrisk.csv", index=False, encoding="utf-8")
    pd.DataFrame(live["ladder_violations"]).to_csv(MIS_DIR / "live_ladder_violations.csv", index=False, encoding="utf-8")

    print("\n" + "=" * 78)
    print("Polymarket 予測市場 構造的歪み・ミスプライシング分析 サマリー")
    print("=" * 78)
    print(f"有効スナップショット市場数: {len(df_snap)} (2025={int((df_snap['year']==2025).sum())}, 2026={int((df_snap['year']==2026).sum())})")

    print("\n--- 1. Yes-Bias & クラスター補正サマリー (T-7d & T-3d) ---")
    b_sub = p12["df_bias_summary"][p12["df_bias_summary"]["horizon_d"].isin([7, 3])]
    print(b_sub.to_string(index=False))

    print("\n--- 2. Buy No vs Buy Yes 高勝率帯比較 (T-7d, All) ---")
    flb7 = p12["df_flb"][(p12["df_flb"]["horizon_d"] == 7) & (p12["df_flb"]["scope"] == "All")]
    print(flb7[["side", "win_band", "n", "mean_entry_cents", "actual_win_pct", "upset_pct", "mean_roi_pct", "apr_pct", "wins_to_recover", "kelly_pct", "p_val", "q_fdr"]].to_string(index=False))

    print("\n--- 3. ライブ NegRisk 合計確率 ---")
    if live["negrisk_summary"]:
        print(pd.DataFrame(live["negrisk_summary"]).to_string(index=False))
    print(f"\n--- 4. ライブ ラダー単調性違反件数: {len(live['ladder_violations'])} 件 ---")
    if live["ladder_violations"]:
        print(pd.DataFrame(live["ladder_violations"]).head(10).to_string(index=False))

    print("\n--- 5. ライブ CLOB 板スプレッド＆スリッページ統計 ---")
    df_b = pd.DataFrame(live["books"])
    if not df_b.empty:
        df_b["p_band"] = pd.cut(df_b["mid"], bins=[0, 0.1, 0.3, 0.7, 0.9, 1.0], labels=["1-10c", "10-30c", "30-70c", "70-90c", "90-99c"])
        print(df_b.groupby("p_band", observed=False)[["spread_cents", "top_depth_usd", "depth_2c_usd", "slip_100_cents", "slip_1000_cents", "slip_10000_cents"]].median().round(2).to_string())

    print("\n生成チャート:")
    for p in charts:
        print(f"  - {p} ({p.stat().st_size:,} bytes)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket 予測市場 構造的歪み・ミスプライシング分析")
    parser.add_argument("--refresh", action="store_true", help="キャッシュ無視で再取得")
    parser.add_argument("--per-year", type=int, default=350, help="2025/2026 各年の取得上限")
    args = parser.parse_args()

    df_mkts = fetch_resolved_binary_markets(target_per_year=args.per_year, refresh=args.refresh)
    if df_mkts.empty:
        print("❌ 決着済み市場を取得できませんでした。")
        return 1

    df_snap = build_calibration_snapshots(df_mkts, refresh=args.refresh)
    p12 = analyze_calibration_and_flb(df_snap)
    live = scan_live_clob_structure(refresh=args.refresh)
    charts = generate_mispricing_charts(df_snap, p12, live)
    save_and_print_mispricing_summary(df_snap, p12, live, charts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
