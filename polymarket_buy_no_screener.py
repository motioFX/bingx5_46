"""Polymarket `Maker Buy No` 割高ロングショット・スクリーナー（ローカル監視・抽出ツール）

設計根拠 (これまでの定量検証結果に基づく):
  - Bucket A: 多択 NegRisk (>=4択) の割高ロングショット (Yes 3〜18% → No 82〜97¢) を Maker 指値で狙う。
              単独二択および地政学・突発ニュース市場はテールリスク被弾回避のため厳格除外。
              相関リスク抑制のため 1イベントにつき最大1銘柄のみ採用。
  - Bucket B: BTC / ETH の遠方価格ターゲット (Yes 2〜25%) のうち、Polymarket IV がチャート実現ボラ
              (RV_30d / RV_14d) を上回る割高ターゲットを抽出（Theta と恐怖プレミアムを味方にする）。
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config_loader import get_webhook_url
from polymarket_chart_latency_research import (
    digital_above_prob,
    one_touch_prob,
    parse_crypto_strike,
    solve_implied_vol,
)
from polymarket_leadlag_research import (
    BINANCE_KLINES_URL,
    GAMMA_EVENTS_URL,
    configure_japanese_font,
    http_get_json,
    is_placeholder_name,
    parse_json_list,
    shorten_text,
)
from polymarket_mispricing_research import fetch_book_metrics, parse_dt_utc
from polymarket_top20_chart import send_chart_to_discord

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

JST = timezone(timedelta(hours=9))
SCREENER_DIR = BASE_DIR / "Data" / "polymarket_research" / "screener"
PLOTS_SCR_DIR = BASE_DIR / "Data" / "plots" / "polymarket_screener"

# 第3弾検証で「単独/地政学ニュースの Buy No は危険」と判明したため厳格除外
EXCLUDED_GEO_NEWS_KEYWORDS = {
    "geopolitics", "iran", "israel", "ukraine", "russia", "war", "ceasefire",
    "strike", "strikes", "invade", "invasion", "blockade", "military", "hormuz",
    "shutdown", "resign", "affair", "cabinet", "tariff", "tariffs", "regime",
    "khamenei", "maduro", "putin", "zelenskyy", "hezbollah", "gaza", "taiwan",
    "venezuela", "china invade", "out of power",
    "tweets", "tweet-markets", "tweets-markets",
}


def is_excluded_geo_or_news(title: str, tag_slugs: set[str]) -> bool:
    tl = title.lower()
    if tag_slugs & EXCLUDED_GEO_NEWS_KEYWORDS:
        return True
    return any(kw in tl for kw in EXCLUDED_GEO_NEWS_KEYWORDS)


def fetch_live_rv(symbol: str) -> Dict[str, float]:
    """Binance 1h 最新35日分から現在価格と 7d/14d/30d 年率実現ボラを取得"""
    data = http_get_json(BINANCE_KLINES_URL, {"symbol": symbol, "interval": "1h", "limit": 800}, timeout=15) or []
    if len(data) < 200:
        return {"spot": np.nan, "rv_7d": np.nan, "rv_14d": np.nan, "rv_30d": np.nan}
    closes = np.array([float(k[4]) for k in data], dtype=float)
    rets = np.diff(np.log(closes))
    ann = math.sqrt(24.0 * 365.0)
    return {
        "spot": float(closes[-1]),
        "rv_7d": float(np.std(rets[-7 * 24 :], ddof=1) * ann),
        "rv_14d": float(np.std(rets[-14 * 24 :], ddof=1) * ann),
        "rv_30d": float(np.std(rets[-min(len(rets), 30 * 24) :], ddof=1) * ann),
    }


def fetch_active_events_pool(limit: int = 150) -> List[Dict[str, Any]]:
    data = http_get_json(
        GAMMA_EVENTS_URL,
        {"active": "true", "closed": "false", "order": "volume24hr", "ascending": "false", "limit": limit},
        timeout=20,
    )
    return data if isinstance(data, list) else []


def scan_bucket_a_negrisk(
    events: List[Dict[str, Any]],
    now_utc: datetime,
    min_days: float = 2.0,
    max_days: float = 120.0,
    top_n: int = 10,
) -> List[Dict[str, Any]]:
    """Bucket A: 多択 NegRisk (>=4択, 非地政学) の割高ロングショット Buy No 候補抽出"""
    pre_cands: List[Dict[str, Any]] = []

    for ev in events:
        if not ev.get("negRisk"):
            continue
        title = str(ev.get("title") or "")
        tags = {str(t.get("slug", "")).lower() for t in (ev.get("tags") or []) if isinstance(t, dict)}
        if is_excluded_geo_or_news(title, tags):
            continue

        ms = [m for m in (ev.get("markets") or []) if m.get("active") is True and not m.get("closed")]
        if len(ms) < 4:
            continue

        ev_end = parse_dt_utc(ev.get("endDate"))
        for m in ms:
            sub_lbl = str(m.get("groupItemTitle") or m.get("question") or "").strip()
            if is_placeholder_name(sub_lbl):
                continue
            outs = parse_json_list(m.get("outcomes"))
            prs = parse_json_list(m.get("outcomePrices"))
            tids = parse_json_list(m.get("clobTokenIds"))
            if outs != ["Yes", "No"] or len(prs) != 2 or len(tids) < 2:
                continue
            try:
                p_yes = float(prs[0])
            except Exception:
                continue
            if not (0.03 <= p_yes <= 0.18):
                continue

            vol_m = float(m.get("volume") or 0.0)
            vol24_m = float(m.get("volume24hr") or 0.0)
            if vol_m < 100_000.0 and vol24_m < 10_000.0:
                continue

            m_end = parse_dt_utc(m.get("endDate")) or ev_end
            if m_end is None:
                continue
            days_rem = (m_end - now_utc).total_seconds() / 86400.0
            if not (min_days <= days_rem <= max_days):
                continue

            pre_cands.append({
                "event_id": str(ev.get("id") or title),
                "event_title": title,
                "n_outcomes": len(ms),
                "sub_label": sub_lbl,
                "yes_token_id": str(tids[0]),
                "no_token_id": str(tids[1]),
                "gamma_yes_pct": p_yes * 100.0,
                "days_rem": days_rem,
                "end_date": m_end.strftime("%Y-%m-%d"),
                "volume": vol_m,
                "volume24hr": vol24_m,
                "tags": sorted(tags)[:4],
            })

    if not pre_cands:
        return []

    # 上位流動性候補の No トークン実測板を並列取得 (最大80トークンまで絞って高速取得)
    pre_cands.sort(key=lambda x: x["volume24hr"] + 0.05 * x["volume"], reverse=True)
    probe_cands = pre_cands[:80]
    books_no: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_book_metrics, c["no_token_id"]): c["no_token_id"] for c in probe_cands}
        for fut in as_completed(futs):
            ntid = futs[fut]
            bk = fut.result()
            if bk:
                books_no[ntid] = bk

    filtered: List[Dict[str, Any]] = []
    for c in probe_cands:
        bk = books_no.get(c["no_token_id"])
        if not bk:
            continue
        # No 板自身の流動性＆スプレッド検証
        if bk["depth_2c_usd"] < 1000.0 or bk["spread_cents"] > 3.0:
            continue
        # Maker 指値 = No Best Bid (成行ではなく最良買い板に並べる想定)
        maker_no = float(bk["best_bid"])
        if not (0.82 <= maker_no <= 0.97):
            continue
        roi_pct = (1.0 - maker_no) / maker_no * 100.0
        apr_pct = roi_pct * (365.0 / max(c["days_rem"], 1.0))
        wins_to_rec = maker_no / max(1.0 - maker_no, 1e-6)
        # スコア: APR と 板厚みのバランス (極端なテールに偏らないよう 1-maker_no も加味)
        score = min(apr_pct, 400.0) * math.log10(max(bk["depth_2c_usd"], 10.0)) * (1.0 - maker_no) * 10.0

        filtered.append({
            **c,
            "no_bid_cents": maker_no * 100.0,
            "no_ask_cents": float(bk["best_ask"]) * 100.0,
            "no_spread_cents": float(bk["spread_cents"]),
            "no_depth_2c_usd": float(bk["depth_2c_usd"]),
            "no_top_depth_usd": float(bk["top_depth_usd"]),
            "roi_pct": roi_pct,
            "apr_pct": apr_pct,
            "wins_to_recover": wins_to_rec,
            "score": score,
        })

    # 1イベントにつき最大1銘柄のみ採用（相関リスク排除）
    best_per_event: Dict[str, Dict[str, Any]] = {}
    for item in filtered:
        eid = item["event_id"]
        if eid not in best_per_event or item["score"] > best_per_event[eid]["score"]:
            best_per_event[eid] = item

    ranked = sorted(best_per_event.values(), key=lambda x: x["score"], reverse=True)
    return ranked[:top_n]


def scan_bucket_b_crypto_high_iv(
    events: List[Dict[str, Any]],
    now_utc: datetime,
    rv_map: Dict[str, Dict[str, float]],
    min_days: float = 2.0,
) -> List[Dict[str, Any]]:
    """Bucket B: BTC / ETH 遠方ターゲット (Yes 2〜25%) のうち高 IV 割高ターゲット Buy No 抽出"""
    cands: List[Dict[str, Any]] = []
    now_ts = now_utc.timestamp()

    for ev in events:
        title = str(ev.get("title") or "")
        tl = title.lower()
        if not (("bitcoin" in tl or "ethereum" in tl) and ("hit" in tl or "above" in tl)):
            continue
        ev_end = parse_dt_utc(ev.get("endDate"))
        for m in ev.get("markets") or []:
            if m.get("active") is not True or m.get("closed"):
                continue
            outs = parse_json_list(m.get("outcomes"))
            prs = parse_json_list(m.get("outcomePrices"))
            tids = parse_json_list(m.get("clobTokenIds"))
            if outs != ["Yes", "No"] or len(prs) != 2 or len(tids) < 2:
                continue
            try:
                p_yes = float(prs[0])
            except Exception:
                continue
            if not (0.02 <= p_yes <= 0.25):
                continue

            sub_lbl = str(m.get("groupItemTitle") or m.get("question") or "").strip()
            parsed = parse_crypto_strike(title, sub_lbl)
            if not parsed:
                continue
            m_end = parse_dt_utc(m.get("endDate")) or ev_end
            if m_end is None:
                continue
            days_rem = (m_end.timestamp() - now_ts) / 86400.0
            if days_rem < min_days:
                continue
            vol_m = float(m.get("volume") or 0.0)
            if vol_m < 25_000.0 and float(m.get("volume24hr") or 0.0) < 5_000.0:
                continue

            cands.append({
                "event_title": title,
                "sub_label": sub_lbl,
                "yes_token_id": str(tids[0]),
                "no_token_id": str(tids[1]),
                "days_rem": days_rem,
                "end_date": m_end.strftime("%Y-%m-%d"),
                "volume": vol_m,
                "volume24hr": float(m.get("volume24hr") or 0.0),
                **parsed,
            })

    if not cands:
        return []

    books_no: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_book_metrics, c["no_token_id"]): c["no_token_id"] for c in cands}
        for fut in as_completed(futs):
            ntid = futs[fut]
            bk = fut.result()
            if bk:
                books_no[ntid] = bk

    results: List[Dict[str, Any]] = []
    for c in cands:
        bk = books_no.get(c["no_token_id"])
        if not bk or bk["depth_2c_usd"] < 300.0:
            continue
        asset = c["asset"]
        rv_info = rv_map[asset]
        spot = rv_info["spot"]
        if np.isnan(spot):
            continue

        t_years = c["days_rem"] / 365.0
        # No の mid から Yes 暗黙 Mid を算出して IV 逆算
        no_bid = float(bk["best_bid"])
        no_mid = float(bk["mid"])
        yes_mid = 1.0 - no_mid

        if c["kind"] == "hit":
            fair_yes_14d = one_touch_prob(spot, c["strike"], rv_info["rv_14d"], t_years, c["strike_dir"], risk_neutral=False)
            fair_yes_30d = one_touch_prob(spot, c["strike"], rv_info["rv_30d"], t_years, c["strike_dir"], risk_neutral=False)
        else:
            fair_yes_14d = digital_above_prob(spot, c["strike"], rv_info["rv_14d"], t_years, risk_neutral=False)
            fair_yes_30d = digital_above_prob(spot, c["strike"], rv_info["rv_30d"], t_years, risk_neutral=False)

        iv_poly = solve_implied_vol(yes_mid, spot, c["strike"], t_years, c["kind"], c["strike_dir"])
        if np.isnan(iv_poly):
            continue

        fair_no_30d = 1.0 - fair_yes_30d
        fair_no_14d = 1.0 - fair_yes_14d
        iv_minus_rv30 = (iv_poly - rv_info["rv_30d"]) * 100.0
        iv_minus_rv14 = (iv_poly - rv_info["rv_14d"]) * 100.0
        edge_vs_rv30_c = (fair_no_30d - no_bid) * 100.0
        edge_vs_rv14_c = (fair_no_14d - no_bid) * 100.0
        roi_pct = (1.0 - no_bid) / no_bid * 100.0
        apr_pct = roi_pct * (365.0 / max(c["days_rem"], 1.0))

        results.append({
            **c,
            "spot": spot,
            "distance_pct": (c["strike"] / spot - 1.0) * 100.0,
            "no_bid_cents": no_bid * 100.0,
            "no_ask_cents": float(bk["best_ask"]) * 100.0,
            "no_spread_cents": float(bk["spread_cents"]),
            "no_depth_2c_usd": float(bk["depth_2c_usd"]),
            "fair_no_30d_cents": fair_no_30d * 100.0,
            "fair_no_14d_cents": fair_no_14d * 100.0,
            "rv_30d_pct": rv_info["rv_30d"] * 100.0,
            "rv_14d_pct": rv_info["rv_14d"] * 100.0,
            "poly_iv_pct": iv_poly * 100.0,
            "iv_minus_rv30_pct": iv_minus_rv30,
            "iv_minus_rv14_pct": iv_minus_rv14,
            "edge_vs_rv30_cents": edge_vs_rv30_c,
            "edge_vs_rv14_cents": edge_vs_rv14_c,
            "roi_pct": roi_pct,
            "apr_pct": apr_pct,
            "wins_to_recover": no_bid / max(1.0 - no_bid, 1e-6),
        })

    # IV が RV_30d より高い順（割高ターゲット順）にソート
    results.sort(key=lambda x: x["iv_minus_rv30_pct"], reverse=True)
    return results


def render_screener_dashboard(
    bucket_a: List[Dict[str, Any]],
    bucket_b: List[Dict[str, Any]],
    rv_map: Dict[str, Dict[str, float]],
    now_jst: datetime,
    out_path: Path,
) -> Optional[Path]:
    configure_japanese_font()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16.5, 6.8))
    fig.patch.set_facecolor("#ffffff")

    # 左パネル: Bucket A (多択 NegRisk 割高ロングショット Buy No)
    if bucket_a:
        df_a = pd.DataFrame(bucket_a)
        sizes = np.clip(np.sqrt(df_a["no_depth_2c_usd"].values) * 2.2, 50, 450)
        sc = ax1.scatter(
            df_a["no_bid_cents"], df_a["apr_pct"].clip(upper=500.0),
            s=sizes, c=df_a["days_rem"], cmap="viridis", alpha=0.85, edgecolors="#111111", linewidth=0.9,
        )
        cbar = fig.colorbar(sc, ax=ax1, fraction=0.04, pad=0.02)
        cbar.set_label("残存日数 (日)", fontsize=9.0)
        x_min, x_max = float(df_a["no_bid_cents"].min()), float(df_a["no_bid_cents"].max())
        y_vals = df_a["apr_pct"].clip(upper=500.0)
        y_min, y_max = float(y_vals.min()), float(y_vals.max())
        ax1.set_xlim(x_min - 1.2, x_max + 1.8)
        ax1.set_ylim(max(0.0, y_min - 30.0), y_max + 55.0)
        for _, r in df_a.iterrows():
            lbl = f"{shorten_text(r['event_title'], 18)} [{shorten_text(r['sub_label'], 12)}]\n({r['roi_pct']:.1f}% | {r['wins_to_recover']:.0f}勝要)"
            is_right_edge = r["no_bid_cents"] > (x_min + x_max) * 0.50
            ax1.annotate(
                lbl,
                (r["no_bid_cents"], min(r["apr_pct"], 500.0)),
                textcoords="offset points",
                xytext=(-8 if is_right_edge else 8, 8),
                ha="right" if is_right_edge else "left",
                fontsize=7.5,
                color="#222222",
            )
        ax1.set_title(f"Bucket A: 多択NegRisk Maker Buy No 候補 (n={len(df_a)}, 1イベント1銘柄)", fontsize=11.0, fontweight="bold")
        ax1.set_xlabel("No Maker 指値価格 (¢, Best Bid基準)", fontsize=9.5)
        ax1.set_ylabel("年率換算利回り APR (%, 上限500%表示)", fontsize=9.5)
        ax1.grid(True, linestyle=":", alpha=0.6)
    else:
        ax1.text(0.5, 0.5, "該当候補なし", ha="center", va="center", fontsize=12)
        ax1.set_title("Bucket A: 多択NegRisk Maker Buy No 候補", fontsize=11.0, fontweight="bold")

    # 右パネル: Bucket B (BTC/ETH 遠方ターゲット IV vs 14d/30d RV)
    if bucket_b:
        df_b = pd.DataFrame(bucket_b[:12]).iloc[::-1].reset_index(drop=True)
        yb = np.arange(len(df_b))
        cols = ["#2ca02c" if v > 0 else "#90a4ae" for v in df_b["iv_minus_rv30_pct"]]
        ax2.barh(yb, df_b["poly_iv_pct"], color=cols, alpha=0.85, label="Polymarket IV (%)")
        ax2.scatter(df_b["rv_30d_pct"], yb, color="#1f77b4", marker="D", s=45, zorder=5, label="チャート 30d RV (%)")
        ax2.scatter(df_b["rv_14d_pct"], yb, color="#d62728", marker="^", s=50, zorder=6, label="チャート 14d RV (%)")
        ax2.set_yticks(yb)
        ax2.set_yticklabels([
            f"[{r['asset']}] {r['sub_label']} ({r['days_rem']:.0f}d, No:{r['no_bid_cents']:.1f}¢)"
            for _, r in df_b.iterrows()
        ], fontsize=8.2)
        ax2.set_title("Bucket B: BTC/ETH 遠方ターゲット IV vs チャート実現ボラ (緑=IV>30d RV)", fontsize=10.8, fontweight="bold")
        ax2.set_xlabel("年率ボラティリティ (%)", fontsize=9.5)
        ax2.grid(True, axis="x", linestyle=":", alpha=0.6)
        ax2.legend(fontsize=8.2, loc="lower right")
    else:
        ax2.text(0.5, 0.5, "該当候補なし", ha="center", va="center", fontsize=12)
        ax2.set_title("Bucket B: BTC/ETH 高IV遠方ターゲット", fontsize=11.0, fontweight="bold")

    fig.suptitle(
        f"Polymarket Maker Buy No 割高ロングショット・スクリーナー [{now_jst.strftime('%Y-%m-%d %H:%M JST')}]",
        fontsize=12.5, fontweight="bold", y=1.01,
    )
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket Maker Buy No 割高ロングショット・スクリーナー")
    parser.add_argument("--min-days", type=float, default=2.0, help="最小残存日数 (デフォルト: 2.0)")
    parser.add_argument("--max-days", type=float, default=120.0, help="Bucket A 最大残存日数 (デフォルト: 120.0)")
    parser.add_argument("--top-a", type=int, default=10, help="Bucket A 上位抽出件数 (デフォルト: 10)")
    parser.add_argument("--test", action="store_true", help="test4_test チャンネルへサマリー送信")
    args = parser.parse_args()

    now_utc = datetime.now(timezone.utc)
    now_jst = now_utc.astimezone(JST)
    tag = now_jst.strftime("%Y%m%d_%H%M%S")

    print(f"[{now_jst.strftime('%Y-%m-%d %H:%M:%S JST')}] Polymarket Maker Buy No スクリーナーを実行中...")
    events = fetch_active_events_pool(limit=150)
    rv_map = {
        "BTC": fetch_live_rv("BTCUSDT"),
        "ETH": fetch_live_rv("ETHUSDT"),
    }
    print(
        f"  Spot/RV: BTC=${rv_map['BTC']['spot']:,.1f} (14d={rv_map['BTC']['rv_14d']*100:.1f}%, 30d={rv_map['BTC']['rv_30d']*100:.1f}%) | "
        f"ETH=${rv_map['ETH']['spot']:,.1f} (14d={rv_map['ETH']['rv_14d']*100:.1f}%, 30d={rv_map['ETH']['rv_30d']*100:.1f}%)"
    )

    bucket_a = scan_bucket_a_negrisk(events, now_utc, min_days=args.min_days, max_days=args.max_days, top_n=args.top_a)
    bucket_b = scan_bucket_b_crypto_high_iv(events, now_utc, rv_map, min_days=args.min_days)

    SCREENER_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(bucket_a).to_csv(SCREENER_DIR / f"bucket_a_negrisk_{tag}.csv", index=False, encoding="utf-8")
    pd.DataFrame(bucket_b).to_csv(SCREENER_DIR / f"bucket_b_crypto_iv_{tag}.csv", index=False, encoding="utf-8")

    print(f"\n=== Bucket A: 多択 NegRisk 非地政学 Maker Buy No 上位 {len(bucket_a)} 件 (1イベント1銘柄) ===")
    if bucket_a:
        df_a = pd.DataFrame(bucket_a)
        cols_a = ["event_title", "sub_label", "n_outcomes", "days_rem", "no_bid_cents", "no_spread_cents", "no_depth_2c_usd", "roi_pct", "apr_pct", "wins_to_recover"]
        df_a_show = df_a[cols_a].copy()
        df_a_show["event_title"] = df_a_show["event_title"].map(lambda s: shorten_text(s, 32))
        df_a_show["sub_label"] = df_a_show["sub_label"].map(lambda s: shorten_text(s, 18))
        print(df_a_show.round(2).to_string(index=False))
    else:
        print("  (該当候補なし)")

    print(f"\n=== Bucket B: BTC/ETH 遠方ターゲット IV vs チャートRV (全 {len(bucket_b)} 件 / 上位表示) ===")
    if bucket_b:
        df_b = pd.DataFrame(bucket_b)
        cols_b = ["asset", "sub_label", "days_rem", "distance_pct", "no_bid_cents", "fair_no_30d_cents", "poly_iv_pct", "rv_30d_pct", "iv_minus_rv30_pct", "edge_vs_rv30_cents", "roi_pct", "wins_to_recover"]
        print(df_b[cols_b].round(2).to_string(index=False))
    else:
        print("  (該当候補なし)")

    chart_path = PLOTS_SCR_DIR / f"buy_no_screener_{tag}.png"
    render_screener_dashboard(bucket_a, bucket_b, rv_map, now_jst, chart_path)
    print(f"\n📈 ダッシュボード生成完了: {chart_path} ({chart_path.stat().st_size:,} bytes)")

    if args.test:
        wh = get_webhook_url("test4_test")
        if wh:
            lines = [f"🎯 **Polymarket Maker Buy No スクリーナー** (`{now_jst.strftime('%Y-%m-%d %H:%M JST')}`)", "```text"]
            for idx, r in enumerate(bucket_a[:6], 1):
                lines.append(
                    f"A{idx} {shorten_text(r['event_title'], 22)} [{shorten_text(r['sub_label'], 12)}] "
                    f"No:{r['no_bid_cents']:.1f}c ROI:{r['roi_pct']:.1f}% ({r['days_rem']:.0f}d)"
                )
            for idx, r in enumerate([b for b in bucket_b if b["iv_minus_rv30_pct"] > 0][:4], 1):
                lines.append(
                    f"B{idx} {r['asset']} {r['sub_label']} No:{r['no_bid_cents']:.1f}c "
                    f"IV:{r['poly_iv_pct']:.0f}%>RV30:{r['rv_30d_pct']:.0f}% (+{r['edge_vs_rv30_cents']:.1f}c)"
                )
            lines.append("```")
            ok = send_chart_to_discord(wh, chart_path, "\n".join(lines)[:1950])
            print(f"{'✅' if ok else '❌'} test4_test 送信結果: {ok}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
