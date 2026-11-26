"""Polymarket 確率急変後 1〜6時間リバーサル（フェード／逆張り）徹底アウトオブサンプル＆プラセボ検証

目的:
  第1弾 (polymarket_leadlag_research.py) で観測された「外生ショック後 1〜3h リバーサル」が、
  1. 真の未知アウトオブサンプル (Unseen OOS) イベント群でも再現するか
  2. 同一ショック重複を排除したポートフォリオ単位でも統計的に有意か (BH-FDR補正)
  3. 暗号資産単独（直前リターン同条件のランダム時刻）プラセボを上回る超過アルファがあるか
  4. 手数料 (0/4/10/20 bps) および約定遅延 (0m/+15m/+30m) に耐えるか
  を検証する。
"""
from __future__ import annotations

import argparse
import json
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
from scipy import stats
from statsmodels.stats.multitest import multipletests

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from polymarket_leadlag_research import (
    ARTIFACT_DIR,
    GAMMA_EVENTS_URL,
    SHORT_SYM,
    SKIP_TAGS,
    SYMBOLS,
    align_polymarket_and_crypto,
    choose_best_submarket,
    classify_session_utc,
    configure_japanese_font,
    detect_causal_shocks,
    fetch_binance_klines_intervals,
    fetch_clob_chunked,
    http_get_json,
    shorten_text,
)

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

DATA_V1_DIR = BASE_DIR / "Data" / "polymarket_research"
FADE_DIR = DATA_V1_DIR / "fade_v2"
PLOTS_FADE_DIR = BASE_DIR / "Data" / "plots" / "polymarket_fade"


def infer_direction_sign_v2(title: str, submarket_label: str, group: str) -> int:
    """拡張外生イベント向けの厳格な理論方向符号 (+1=Risk-On, -1=Risk-Off, 0=曖昧/除外)"""
    t = f"{title} || {submarket_label}".lower()
    sub_l = submarket_label.lower()

    if group == "FOMC/金利":
        if "decrease" in sub_l or "cut" in sub_l:
            return 1
        if "no change" in sub_l or "increase" in sub_l or "hike" in sub_l:
            return -1
        return 0

    if group == "地政学":
        # 和平・停戦・戦争終結・封鎖解除・外交会談 = Risk-On (+1)
        if any(k in t for k in [
            "ceasefire", "peace deal", "conflict ends", "end of iranian blockade",
            "end of military operations", "diplomatic meeting", "ends ukraine war",
        ]):
            return 1
        # 軍事攻撃・侵攻・空域閉鎖・原油急騰(↑) = Risk-Off (-1)
        # ※ regime fall / khamenei out 等は市場解釈が割れやすいため意図的に 0（除外）とする
        if any(k in t for k in [
            "regime fall", "khamenei out", "leader end of",
        ]):
            return 0
        if any(k in t for k in [
            "strikes", "enter iran", "invade", "military engagement",
            "closes its airspace", "crude oil", "wti",
        ]):
            if ("crude oil" in t or "wti" in t) and "↓" in submarket_label:
                return 1
            return -1
        return 0

    if group == "Tech/AI・マクロ":
        if any(k in t for k in [
            "recession", "bankruptcy", "bubble burst", "tariff", "shutdown", "banned",
        ]):
            return -1
        return 0

    return 0


def classify_exogenous_group_v2(event: Dict[str, Any]) -> Optional[str]:
    """外生マクロ・地政学・Techイベントのみを対象（内生・暗号資産価格系・選挙・スポーツは除外）"""
    tags = {str(t.get("slug", "")).lower() for t in (event.get("tags") or []) if isinstance(t, dict)}
    title = str(event.get("title") or "")
    title_l = title.lower()

    if tags & SKIP_TAGS:
        if not ({"tech", "ai", "nvidia", "tiktok"} & tags and "affair" not in title_l and "searched person" not in title_l):
            return None
    if any(bad in title_l for bad in [
        "tweets", "elon musk #", "wear a suit", "stranger things", "aliens exist",
        "affair", "searched person", "mrbeast", "bitcoin", "ethereum", "solana", "crypto",
        "math problem", "millennium prize", "rental price", "b200 average", "h100 average", "b300 average",
    ]):
        return None

    end_date = str(event.get("endDate") or "")
    if end_date and end_date[:10] < "2025-01-01":
        return None

    if "fed decision in" in title_l:
        return "FOMC/金利"

    if (tags & {"geopolitics", "iran", "middle-east", "ukraine-peace-deal", "diplomacy-ceasefire", "hormuz", "oil"}) or any(
        k in title_l for k in ["iran", "ceasefire", "ukraine", "crude oil", "venezuela military"]
    ):
        if not (tags & {"elections", "main-election", "world-elections", "global-elections"}):
            return "地政学"

    if (tags & {"gov-shutdown", "recession", "tariffs", "trade-war", "tiktok"}) or any(
        k in title_l for k in ["shutdown", "recession", "tariff", "tiktok banned", "ai bubble"]
    ):
        return "Tech/AI・マクロ"

    return None


def build_cohorts(max_oos: int = 24, refresh: bool = False) -> List[Dict[str, Any]]:
    """第1弾の Discovery (12件) を固定しつつ、未知 OOS イベントを新規抽出"""
    FADE_DIR.mkdir(parents=True, exist_ok=True)
    cohort_cache = FADE_DIR / "cohorts_events.json"
    if cohort_cache.exists() and not refresh:
        with cohort_cache.open("r", encoding="utf-8") as f:
            cached = json.load(f)
        if isinstance(cached, list) and len(cached) >= 20:
            print(f"[Cache] 拡張コホート {len(cached)} イベントを読み込みました。")
            return cached

    # 1) Discovery Cohort = 第1弾で direction_sign != 0 だった外生12件
    v1_path = DATA_V1_DIR / "selected_events.json"
    with v1_path.open("r", encoding="utf-8") as f:
        v1_events = json.load(f)

    discovery: List[Dict[str, Any]] = []
    discovery_slugs = set()
    for ev in v1_events:
        if not ev.get("is_endogenous") and int(ev.get("direction_sign", 0)) != 0:
            rec = dict(ev)
            rec["cohort"] = "Discovery"
            discovery.append(rec)
            discovery_slugs.add(rec["slug"])

    print(f"[Discovery] 固定コホート: {len(discovery)} イベント")

    # 2) Unseen OOS Cohort = 第1弾に含まれない新規イベントを Gamma API から抽出
    print("[Gamma API] 未知 OOS コホート候補を検索中 (closed offset 0..360 + active)...")
    raw_pool: List[Tuple[bool, Dict[str, Any]]] = []
    act = http_get_json(GAMMA_EVENTS_URL, {"active": "true", "closed": "false", "order": "volume24hr", "ascending": "false", "limit": 100}) or []
    raw_pool.extend([(True, e) for e in act])
    for offset in [0, 120, 240]:
        cls_page = http_get_json(GAMMA_EVENTS_URL, {"closed": "true", "order": "volume", "ascending": "false", "limit": 120, "offset": offset}) or []
        raw_pool.extend([(False, e) for e in cls_page])
        time.sleep(0.1)

    seen = set(discovery_slugs)
    candidates: List[Tuple[float, str, bool, Dict[str, Any]]] = []
    for is_act, ev in raw_pool:
        slug = str(ev.get("slug") or "")
        if not slug or slug in seen:
            continue
        seen.add(slug)

        grp = classify_exogenous_group_v2(ev)
        if not grp:
            continue
        vol = float(ev.get("volume") or 0.0)
        if vol < 10_000_000:
            continue
        candidates.append((vol, grp, is_act, ev))

    candidates.sort(key=lambda x: x[0], reverse=True)
    oos_events: List[Dict[str, Any]] = []
    for vol, grp, is_act, ev in candidates:
        if len(oos_events) >= max_oos:
            break
        sub = choose_best_submarket(ev)
        if not sub:
            continue
        title = str(ev.get("title") or "")
        dir_sign = infer_direction_sign_v2(title, sub["submarket_label"], grp)
        if dir_sign == 0:
            continue
        rec = {
            "slug": str(ev.get("slug")),
            "title": title,
            "group": grp,
            "is_endogenous": False,
            "is_active": is_act,
            "event_volume": vol,
            "volume24hr": float(ev.get("volume24hr") or 0.0),
            "end_date": str(ev.get("endDate") or "")[:10],
            "direction_sign": dir_sign,
            "cohort": "Unseen_OOS",
            **sub,
        }
        oos_events.append(rec)
        print(
            f"  + [OOS/{grp}] {shorten_text(title, 42):42s} | sub={shorten_text(sub['submarket_label'], 18):18s} "
            f"| dir={dir_sign:+d} | vol=${vol/1e6:5.1f}M"
        )

    combined = discovery + oos_events
    with cohort_cache.open("w", encoding="utf-8") as f:
        json.dump(combined, f, ensure_ascii=False, indent=2)
    print(f"[Cohort Ready] Discovery={len(discovery)}, Unseen_OOS={len(oos_events)}, Total={len(combined)}")
    return combined


def deduplicate_shocks_portfolio(df_shocks: pd.DataFrame, cooldown_hours: float = 3.0) -> pd.DataFrame:
    """同時刻〜3時間以内に重複発生したショックを最大 |dp| の1トレードへ集約"""
    if df_shocks.empty:
        return df_shocks.copy()
    df_s = df_shocks.sort_values(["shock_dt", "abs_dp_pt"], ascending=[True, False]).reset_index(drop=True)
    kept_indices: List[int] = []
    last_kept_dt: Optional[pd.Timestamp] = None
    for idx, row in df_s.iterrows():
        cur_dt = row["shock_dt"]
        if last_kept_dt is None or (cur_dt - last_kept_dt).total_seconds() >= cooldown_hours * 3600.0:
            kept_indices.append(idx)
            last_kept_dt = cur_dt
    return df_s.loc[kept_indices].reset_index(drop=True)


def compute_stats_row(vals: np.ndarray, label: str, condition: str, asset: str, horizon: int, fee_bps: float = 0.0) -> Dict[str, Any]:
    """1配列から統計指標（平均・中央値・勝率・t/Wilcoxon・95%CI・PF・Sharpe）を計算"""
    net = vals - (fee_bps / 100.0)
    n = len(net)
    if n < 3:
        return {
            "label": label, "condition": condition, "asset": asset, "horizon_h": horizon,
            "fee_bps": fee_bps, "n": n, "mean_pct": np.nan, "median_pct": np.nan,
            "win_pct": np.nan, "t_stat": np.nan, "p_t": np.nan, "p_wilcoxon": np.nan,
            "ci_low": np.nan, "ci_high": np.nan, "pf": np.nan, "sharpe_trade": np.nan,
        }
    mean_v = float(np.mean(net))
    med_v = float(np.median(net))
    win_v = float((net > 0).mean() * 100.0)
    std_v = float(np.std(net, ddof=1))
    if std_v > 1e-12:
        t_stat, p_t = stats.ttest_1samp(net, 0.0)
        se = std_v / np.sqrt(n)
        ci_low, ci_high = mean_v - 1.96 * se, mean_v + 1.96 * se
        sharpe = mean_v / std_v
    else:
        t_stat, p_t, ci_low, ci_high, sharpe = 0.0, 1.0, mean_v, mean_v, 0.0
    try:
        _, p_wil = stats.wilcoxon(net)
    except Exception:
        p_wil = 1.0
    pos_s = float(net[net > 0].sum())
    neg_s = float(abs(net[net < 0].sum()))
    pf = pos_s / max(neg_s, 1e-6)
    return {
        "label": label, "condition": condition, "asset": asset, "horizon_h": horizon,
        "fee_bps": fee_bps, "n": int(n), "mean_pct": mean_v, "median_pct": med_v,
        "win_pct": win_v, "t_stat": float(t_stat), "p_t": float(p_t), "p_wilcoxon": float(p_wil),
        "ci_low": float(ci_low), "ci_high": float(ci_high), "pf": float(pf), "sharpe_trade": float(sharpe),
    }


def run_matched_placebo(
    df_dedup: pd.DataFrame,
    crypto_1h: Dict[str, pd.DataFrame],
    asset: str = "BTC",
    horizon_h: int = 3,
    n_boot: int = 500,
    seed: int = 42,
) -> Dict[str, Any]:
    """同一(年月・時間帯) かつ 直前4hリターン符号一致のランダム時刻から500回ブートストラップ抽出して超過アルファを検定"""
    sym = f"{asset}USDT"
    cdf = crypto_1h[sym].copy()
    cdf["ret_1h"] = np.log(cdf["close"] / cdf["close"].shift(1)) * 100.0
    cdf["pre4"] = cdf["ret_1h"].rolling(4).sum().shift(1)  # t-4..t-1
    cdf["fut_h"] = cdf["ret_1h"].rolling(horizon_h).sum().shift(-horizon_h)  # t+1..t+H
    cdf["ym"] = cdf.index.strftime("%Y-%m")
    cdf["sess"] = [classify_session_utc(ts) for ts in cdf.index]
    cdf["pre_sign"] = np.sign(cdf["pre4"].fillna(0.0))
    valid_pool = cdf.dropna(subset=["pre4", "fut_h"])

    # 各ショックに対応する候補プールを事前インデックス化
    sub = df_dedup.dropna(subset=[f"{asset}_fade_post{horizon_h}", "BTC_dir_pre4"]).copy()
    if len(sub) < 10:
        return {"asset": asset, "horizon_h": horizon_h, "n": len(sub), "actual_mean": np.nan, "placebo_mean": np.nan, "excess_alpha": np.nan, "p_placebo": np.nan}

    actual_vals = sub[f"{asset}_fade_post{horizon_h}"].values
    actual_mean = float(np.mean(actual_vals))

    # 高速化: グループごとの fut_h 配列を辞書化
    grouped = {k: g["fut_h"].values for k, g in valid_pool.groupby(["ym", "sess", "pre_sign"])}
    grouped_fb = {k: g["fut_h"].values for k, g in valid_pool.groupby(["sess", "pre_sign"])}

    pools: List[np.ndarray] = []
    fade_dirs: List[float] = []
    for r in sub.itertuples(index=False):
        ts = r.shock_dt
        ym = ts.strftime("%Y-%m")
        sess = r.session
        # 実際の fade_dir (-sign(dp * dir_sign)) と同じ方向にランダムエントリーした場合の符号
        f_dir = float(r.fade_dir)
        # ショック前4時間の該当銘柄の実際の動きと同じ符号でマッチ
        pre_s = float(np.sign(getattr(r, f"{asset}_raw_pre4")))
        arr = grouped.get((ym, sess, pre_s))
        if arr is None or len(arr) < 15:
            arr = grouped_fb.get((sess, pre_s), valid_pool["fut_h"].values)
        pools.append(arr)
        fade_dirs.append(f_dir)

    rng = np.random.default_rng(seed)
    boot_means = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        s_sum = 0.0
        for arr, f_dir in zip(pools, fade_dirs):
            pick = arr[rng.integers(0, len(arr))]
            s_sum += f_dir * pick
        boot_means[b] = s_sum / len(pools)

    placebo_mean = float(boot_means.mean())
    placebo_sd = float(boot_means.std(ddof=1))
    excess_alpha = actual_mean - placebo_mean
    # 片側経験p値: プラセボ平均が実際以上になる割合
    p_emp = float((boot_means >= actual_mean).mean())
    return {
        "asset": asset,
        "horizon_h": horizon_h,
        "n": int(len(sub)),
        "actual_mean": actual_mean,
        "placebo_mean": placebo_mean,
        "placebo_sd": placebo_sd,
        "excess_alpha": excess_alpha,
        "p_placebo": p_emp,
        "boot_dist": boot_means.tolist(),
    }


def run_fade_verification(events: List[Dict[str, Any]], refresh: bool = False) -> Dict[str, Any]:
    now_ts = int(time.time())
    print("\n[Step 1] 全コホートの Polymarket CLOB 1h 履歴を取得中...")
    poly_1h_map: Dict[str, pd.Series] = {}
    intervals_1h: List[Tuple[int, int]] = []

    for ev in events:
        slug = ev["slug"]
        t_first = int(ev["t_first"])
        t_last = min(int(ev["t_last"]), now_ts)
        s_1h = max(t_first, t_last - 60 * 86400)
        ser_1h = fetch_clob_chunked(ev["token_id"], s_1h, t_last, fidelity=60, chunk_days=12, refresh=refresh)
        if len(ser_1h) >= 48:
            poly_1h_map[slug] = ser_1h
            intervals_1h.append((s_1h, t_last))

    print(f"  有効 CLOB 1h 取得イベント数: {len(poly_1h_map)} / {len(events)}")

    print("\n[Step 2] Binance Spot 6銘柄 (1h) を取得中...")
    crypto_1h: Dict[str, pd.DataFrame] = {}
    for sym in SYMBOLS:
        crypto_1h[sym] = fetch_binance_klines_intervals(sym, "1h", intervals_1h, refresh=refresh)

    print("\n[Step 3] ショック検出＆リバーサル(フェード)抽出中...")
    shock_rows: List[Dict[str, Any]] = []
    valid_events: List[Dict[str, Any]] = []
    horizons = [1, 2, 3, 4, 6, 12]

    for ev in events:
        slug = ev["slug"]
        if slug not in poly_1h_map:
            continue
        df1h = align_polymarket_and_crypto(poly_1h_map[slug], crypto_1h, "1h")
        if len(df1h) < 48:
            continue
        valid_events.append(ev)

        dir_sign = int(ev["direction_sign"])
        shocks_ts = detect_causal_shocks(df1h["dp"], min_abs_dp=3.0, roll_window=168, sigma_mult=3.0, cooldown_bars=6)

        for ts in shocks_ts:
            pos = df1h.index.get_loc(ts)
            if pos < 6 or pos + 12 >= len(df1h):
                continue
            dp_val = float(df1h["dp"].iloc[pos])
            p_prev = float(df1h["prob"].iloc[pos - 1])
            p_now = float(df1h["prob"].iloc[pos])
            # ショックのRisk-On/Off符号 (+1ならRisk-Onショック, -1ならRisk-Offショック)
            align_mult = float(np.sign(dp_val) * dir_sign)
            # フェード方向 = ショックと逆 (+1=Buy Dip, -1=Sell Rally)
            fade_dir = -align_mult

            rec: Dict[str, Any] = {
                "slug": slug,
                "title": ev["title"],
                "group": ev["group"],
                "cohort": ev["cohort"],
                "event_volume": float(ev["event_volume"]),
                "direction_sign": dir_sign,
                "shock_ts": ts.isoformat(),
                "shock_dt": ts,
                "dp_pt": dp_val,
                "abs_dp_pt": abs(dp_val),
                "p_prev": p_prev,
                "p_now": p_now,
                "is_non_settlement": bool(0.10 <= p_prev <= 0.90 and 0.05 <= p_now <= 0.95),
                "session": classify_session_utc(ts),
                "align_mult": align_mult,
                "fade_dir": fade_dir,
            }

            ok_assets = True
            for sym in SYMBOLS:
                short = SHORT_SYM[sym]
                rets = df1h[f"ret_{short}"].iloc[pos - 4 : pos + 13].values  # len=17: idx 0..3=pre4, 4=t0, 5..16=post1..12
                if len(rets) != 17 or np.isnan(rets).any():
                    ok_assets = False
                    break
                raw_pre4 = float(rets[0:4].sum())
                raw_sim = float(rets[4])
                rec[f"{short}_raw_pre4"] = raw_pre4
                rec[f"{short}_dir_pre4"] = align_mult * raw_pre4
                rec[f"{short}_dir_sim"] = align_mult * raw_sim
                for h in horizons:
                    raw_post_h = float(rets[5 : 5 + h].sum())
                    rec[f"{short}_fade_post{h}"] = fade_dir * raw_post_h

            if ok_assets:
                shock_rows.append(rec)

    df_shocks = pd.DataFrame(shock_rows).sort_values("shock_dt").reset_index(drop=True)
    df_dedup_all = deduplicate_shocks_portfolio(df_shocks, cooldown_hours=3.0)
    df_dedup_disc = deduplicate_shocks_portfolio(df_shocks[df_shocks["cohort"] == "Discovery"], cooldown_hours=3.0)
    df_dedup_oos = deduplicate_shocks_portfolio(df_shocks[df_shocks["cohort"] == "Unseen_OOS"], cooldown_hours=3.0)

    print(
        f"  生ショック総数={len(df_shocks)} (Discovery={int((df_shocks['cohort']=='Discovery').sum())}, "
        f"Unseen_OOS={int((df_shocks['cohort']=='Unseen_OOS').sum())})"
    )
    print(
        f"  重複排除後独立数={len(df_dedup_all)} (Discovery={len(df_dedup_disc)}, Unseen_OOS={len(df_dedup_oos)})"
    )

    # --- Step 4: 約定遅延 (0m / +15m / +30m) テスト用 Binance 15m 取得 ---
    print("\n[Step 4] 約定遅延検証用 Binance 15m データを取得中...")
    iv_15m = [(int(dt.timestamp()) - 3600, int(dt.timestamp()) + 8 * 3600) for dt in df_dedup_all["shock_dt"]]
    crypto_15m: Dict[str, pd.DataFrame] = {}
    for sym in ["BTCUSDT", "RENDERUSDT", "NEARUSDT"]:
        crypto_15m[sym] = fetch_binance_klines_intervals(sym, "15m", iv_15m, refresh=refresh)

    delay_rows: List[Dict[str, Any]] = []
    for row in df_dedup_all.itertuples(index=False):
        t0 = row.shock_dt
        f_dir = float(row.fade_dir)
        for sym in ["BTCUSDT", "RENDERUSDT", "NEARUSDT"]:
            short = SHORT_SYM[sym]
            cdf = crypto_15m[sym]
            for delay_m in [0, 15, 30]:
                t_entry = t0 + pd.Timedelta(minutes=delay_m)
                for h in [3, 6]:
                    t_exit = t0 + pd.Timedelta(hours=h)
                    if t_entry in cdf.index and t_exit in cdf.index:
                        p_in = float(cdf.loc[t_entry, "close"])
                        p_out = float(cdf.loc[t_exit, "close"])
                        ret_pct = np.log(p_out / p_in) * 100.0 * f_dir
                        delay_rows.append({
                            "cohort": row.cohort, "shock_ts": row.shock_ts,
                            "asset": short, "delay_min": delay_m, "horizon_h": h,
                            "fade_ret_pct": float(ret_pct),
                        })
    df_delay = pd.DataFrame(delay_rows)

    # --- Step 5: 事前登録条件 (C0..C4) 一括評価 & BH-FDR ---
    print("\n[Step 5] 事前登録条件 C0..C4 の統計評価＆プラセボ検証を実行中...")
    cond_rows: List[Dict[str, Any]] = []
    mid_dt = df_dedup_all["shock_dt"].iloc[len(df_dedup_all) // 2] if len(df_dedup_all) >= 6 else None

    datasets = [
        ("全統合(重複排除)", df_dedup_all),
        ("Discovery(重複排除)", df_dedup_disc),
        ("Unseen_OOS(重複排除)", df_dedup_oos),
    ]
    if mid_dt is not None:
        datasets.append(("時系列前半IS", df_dedup_all[df_dedup_all["shock_dt"] < mid_dt]))
        datasets.append(("時系列後半OOS", df_dedup_all[df_dedup_all["shock_dt"] >= mid_dt]))

    # C0: 全銘柄 × 全保有期間
    for ds_name, ds_df in datasets:
        for sym in SYMBOLS:
            a = SHORT_SYM[sym]
            for h in horizons:
                vals = ds_df[f"{a}_fade_post{h}"].dropna().values
                cond_rows.append(compute_stats_row(vals, ds_name, "C0_Baseline", a, h, fee_bps=0.0))

    # C1..C4 条件フィルタ (BTC / RENDER / NEAR / PAXG の 3h & 6h)
    def get_filters(df_in: pd.DataFrame) -> List[Tuple[str, pd.Series]]:
        return [
            ("C1_事前織り込み(Pre4>+0.3%)", df_in["BTC_dir_pre4"] > 0.3),
            ("C1_事前横ばい(|Pre4|<=0.3%)", df_in["BTC_dir_pre4"].abs() <= 0.3),
            ("C1_事前逆行(Pre4<-0.3%)", df_in["BTC_dir_pre4"] < -0.3),
            ("C2_大幅ショック(|dp|>=5pt)", df_in["abs_dp_pt"] >= 5.0),
            ("C2_中規模ショック(3-5pt)", (df_in["abs_dp_pt"] >= 3.0) & (df_in["abs_dp_pt"] < 5.0)),
            ("C2_非決着再評価(10-90%)", df_in["is_non_settlement"]),
            ("C3_メガ流動性(>=$150M)", df_in["event_volume"] >= 150_000_000),
            ("C3_FOMC/金利のみ", df_in["group"] == "FOMC/金利"),
            ("C3_地政学のみ", df_in["group"] == "地政学"),
            ("C4_Long-Fadeのみ(現物押目)", df_in["fade_dir"] > 0),
            ("C4_Short-Fadeのみ(FOMO回避)", df_in["fade_dir"] < 0),
            ("C4_米国市場時間", df_in["session"] == "米国市場時間"),
            ("C4_時間外+週末", df_in["session"] != "米国市場時間"),
        ]

    for ds_name, ds_df in [("全統合(重複排除)", df_dedup_all), ("Discovery(重複排除)", df_dedup_disc), ("Unseen_OOS(重複排除)", df_dedup_oos)]:
        for cname, mask in get_filters(ds_df):
            sub_f = ds_df[mask]
            for a in ["BTC", "RENDER", "NEAR", "PAXG"]:
                for h in [3, 6]:
                    vals = sub_f[f"{a}_fade_post{h}"].dropna().values
                    cond_rows.append(compute_stats_row(vals, ds_name, cname, a, h, fee_bps=0.0))

    df_cond = pd.DataFrame(cond_rows)
    # BH-FDR補正（全統合データセットかつ n>=10 の主要検定に対して）
    mask_fdr = (df_cond["label"] == "全統合(重複排除)") & (df_cond["n"] >= 10) & df_cond["p_t"].notna()
    df_cond["q_fdr"] = np.nan
    if mask_fdr.sum() > 0:
        _, qv, _, _ = multipletests(df_cond.loc[mask_fdr, "p_t"].values, method="fdr_bh")
        df_cond.loc[mask_fdr, "q_fdr"] = qv

    # --- Step 6: マッチド・暗号資産単独プラセボ (500回ブートストラップ) ---
    placebo_results: List[Dict[str, Any]] = []
    for a in ["BTC", "ETH", "SOL", "RENDER", "NEAR", "PAXG"]:
        for h in [3, 6]:
            pl_all = run_matched_placebo(df_dedup_all, crypto_1h, asset=a, horizon_h=h, n_boot=500, seed=42)
            pl_all["subset"] = "All_Dedup"
            placebo_results.append(pl_all)

            pl_long = run_matched_placebo(df_dedup_all[df_dedup_all["fade_dir"] > 0], crypto_1h, asset=a, horizon_h=h, n_boot=500, seed=43)
            pl_long["subset"] = "Long_Fade_Only"
            placebo_results.append(pl_long)

    return {
        "events": valid_events,
        "df_shocks": df_shocks,
        "df_dedup_all": df_dedup_all,
        "df_dedup_disc": df_dedup_disc,
        "df_dedup_oos": df_dedup_oos,
        "df_delay": df_delay,
        "df_cond": df_cond,
        "placebo_results": placebo_results,
    }


def generate_fade_charts(res: Dict[str, Any]) -> List[Path]:
    """5枚の高解像度日本語チャートを生成"""
    configure_japanese_font()
    PLOTS_FADE_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    saved: List[Path] = []

    df_cond = res["df_cond"]
    df_dedup_all = res["df_dedup_all"]
    df_dedup_disc = res["df_dedup_disc"]
    df_dedup_oos = res["df_dedup_oos"]
    df_delay = res["df_delay"]

    # ==========================================================
    # Chart 1: Discovery vs Unseen OOS (Horizons & 6 Assets)
    # ==========================================================
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6.2))
    c0 = df_cond[df_cond["condition"] == "C0_Baseline"]
    horizons = [1, 2, 3, 4, 6, 12]
    x = np.arange(len(horizons))
    w = 0.25
    for idx, (lbl, col, title_lbl) in enumerate([
        ("Discovery(重複排除)", "#1f77b4", f"Discovery (n={len(df_dedup_disc)})"),
        ("Unseen_OOS(重複排除)", "#ff7f0e", f"未知 OOS (n={len(df_dedup_oos)})"),
        ("全統合(重複排除)", "#2ca02c", f"全統合 (n={len(df_dedup_all)})"),
    ]):
        sub = c0[(c0["label"] == lbl) & (c0["asset"] == "BTC")].set_index("horizon_h").reindex(horizons)
        ax1.bar(x + (idx - 1) * w, sub["mean_pct"].values, width=w, label=title_lbl, color=col, alpha=0.88)
    ax1.axhline(0, color="#111111", linewidth=1.0)
    ax1.axhline(0.04, color="#666666", linestyle="--", linewidth=1.0, label="Maker往復コスト (4bps)")
    ax1.axhline(0.10, color="#d62728", linestyle=":", linewidth=1.1, label="標準往復コスト (10bps)")
    ax1.set_xticks(x)
    ax1.set_xticklabels([f"{h}h" for h in horizons], fontsize=9.5)
    ax1.set_title("BTC フェード平均リターン (Discovery vs 未知OOS: 保有期間別)", fontsize=11.5, fontweight="bold")
    ax1.set_ylabel("平均グロス・リターン (%)", fontsize=9.5)
    ax1.grid(True, axis="y", linestyle=":", alpha=0.6)
    ax1.legend(fontsize=8.3, loc="best")

    assets = [SHORT_SYM[s] for s in SYMBOLS]
    xa = np.arange(len(assets))
    for idx, (lbl, col, title_lbl) in enumerate([
        ("Discovery(重複排除)", "#1f77b4", "Discovery (3h)"),
        ("Unseen_OOS(重複排除)", "#ff7f0e", "未知 OOS (3h)"),
        ("全統合(重複排除)", "#2ca02c", "全統合 (6h)"),
    ]):
        h_target = 6 if "6h" in title_lbl else 3
        sub = c0[(c0["label"] == lbl) & (c0["horizon_h"] == h_target)].set_index("asset").reindex(assets)
        ax2.bar(xa + (idx - 1) * w, sub["mean_pct"].values, width=w, label=title_lbl, color=col, alpha=0.88)
    ax2.axhline(0, color="#111111", linewidth=1.0)
    ax2.axhline(0.10, color="#d62728", linestyle=":", linewidth=1.1, label="10bps損益分岐ライン")
    ax2.set_xticks(xa)
    ax2.set_xticklabels(assets, fontsize=10, fontweight="bold")
    ax2.set_title("6銘柄別 フェード平均リターン (Discovery vs 未知OOS)", fontsize=11.5, fontweight="bold")
    ax2.set_ylabel("平均グロス・リターン (%)", fontsize=9.5)
    ax2.grid(True, axis="y", linestyle=":", alpha=0.6)
    ax2.legend(fontsize=8.3, loc="best")

    fig.tight_layout()
    p1 = PLOTS_FADE_DIR / "fade_01_discovery_vs_unseen_oos.png"
    fig.savefig(p1, dpi=150, bbox_inches="tight")
    plt.close(fig)
    saved.append(p1)

    # ==========================================================
    # Chart 2: Pre-Registered Conditions (Discovery vs Unseen OOS, 3h & 6h)
    # ==========================================================
    cond_order = [
        "C0_Baseline",
        "C4_Long-Fadeのみ(現物押目)",
        "C4_Short-Fadeのみ(FOMO回避)",
        "C1_事前織り込み(Pre4>+0.3%)",
        "C1_事前横ばい(|Pre4|<=0.3%)",
        "C1_事前逆行(Pre4<-0.3%)",
        "C2_大幅ショック(|dp|>=5pt)",
        "C2_非決着再評価(10-90%)",
        "C3_メガ流動性(>=$150M)",
        "C3_FOMC/金利のみ",
        "C3_地政学のみ",
        "C4_米国市場時間",
        "C4_時間外+週末",
    ]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16.5, 7.5), sharey=True)
    y = np.arange(len(cond_order))
    bh = 0.36
    for ax, asset_t, h_t, tstr in [
        (ax1, "BTC", 3, "BTC 3時間フェード (条件別 Discovery vs 未知OOS)"),
        (ax2, "RENDER", 6, "RENDER 6時間フェード (条件別 Discovery vs 未知OOS)"),
    ]:
        disc_s = df_cond[(df_cond["label"] == "Discovery(重複排除)") & (df_cond["asset"] == asset_t) & (df_cond["horizon_h"] == h_t)].set_index("condition").reindex(cond_order)
        oos_s = df_cond[(df_cond["label"] == "Unseen_OOS(重複排除)") & (df_cond["asset"] == asset_t) & (df_cond["horizon_h"] == h_t)].set_index("condition").reindex(cond_order)
        ax.barh(y - bh / 2, disc_s["mean_pct"].fillna(0).values, height=bh, label="Discovery", color="#1f77b4", alpha=0.88)
        ax.barh(y + bh / 2, oos_s["mean_pct"].fillna(0).values, height=bh, label="未知 OOS", color="#ff7f0e", alpha=0.88)
        ax.axvline(0, color="#111111", linewidth=1.0)
        ax.axvline(0.10, color="#d62728", linestyle=":", linewidth=1.0, label="10bpsコスト")
        ax.set_yticks(y)
        ax.set_yticklabels([c.replace("C0_", "").replace("C1_", "").replace("C2_", "").replace("C3_", "").replace("C4_", "") for c in cond_order], fontsize=9.0)
        ax.invert_yaxis()
        ax.set_xlabel("平均グロス・リターン (%)", fontsize=9.5)
        ax.set_title(tstr, fontsize=11.0, fontweight="bold")
        ax.grid(True, axis="x", linestyle=":", alpha=0.6)
        ax.legend(fontsize=8.3, loc="lower right")

    fig.tight_layout()
    p2 = PLOTS_FADE_DIR / "fade_02_long_vs_short_and_conditions.png"
    fig.savefig(p2, dpi=150, bbox_inches="tight")
    plt.close(fig)
    saved.append(p2)

    # ==========================================================
    # Chart 3: Matched Crypto-Only Placebo & Execution Delay / Cost
    # ==========================================================
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6.2))
    pl_all = [p for p in res["placebo_results"] if p["subset"] == "All_Dedup" and p["horizon_h"] == 3]
    if pl_all:
        names = [p["asset"] for p in pl_all]
        act_v = [p["actual_mean"] for p in pl_all]
        pl_v = [p["placebo_mean"] for p in pl_all]
        pl_err = [1.96 * p["placebo_sd"] for p in pl_all]
        xp = np.arange(len(names))
        wp = 0.35
        ax1.bar(xp - wp / 2, act_v, width=wp, label="Polymarket フェード実績 (3h)", color="#1565c0")
        ax1.bar(xp + wp / 2, pl_v, width=wp, yerr=pl_err, capsize=3, label="暗号資産単独プラセボ (500回±95%範囲)", color="#9e9e9e", alpha=0.85)
        for i, p in enumerate(pl_all):
            ax1.text(i, max(act_v[i], pl_v[i]) + 0.03, f"p={p['p_placebo']:.2f}", ha="center", fontsize=8.0)
        ax1.axhline(0, color="#111111", linewidth=0.9)
        ax1.set_xticks(xp)
        ax1.set_xticklabels(names, fontsize=10, fontweight="bold")
        ax1.set_title("マッチド暗号資産単独プラセボ比較 (3時間フェード・全統合)", fontsize=11.3, fontweight="bold")
        ax1.set_ylabel("平均リターン (%)", fontsize=9.5)
        ax1.grid(True, axis="y", linestyle=":", alpha=0.6)
        ax1.legend(fontsize=8.3, loc="best")

    if not df_delay.empty:
        delays = [0, 15, 30]
        xd = np.arange(len(delays))
        wd = 0.25
        for idx, (a, h, col) in enumerate([("BTC", 3, "#1f77b4"), ("RENDER", 6, "#2ca02c"), ("NEAR", 6, "#ff7f0e")]):
            sub_d = df_delay[(df_delay["asset"] == a) & (df_delay["horizon_h"] == h)].groupby("delay_min")["fade_ret_pct"].mean().reindex(delays)
            ax2.plot(delays, sub_d.values, marker="o", linewidth=2.2, label=f"{a} ({h}h基準)", color=col)
        ax2.axhline(0.0, color="#111111", linewidth=1.0, label="0bps (現物タイミング価値)")
        ax2.axhline(0.04, color="#666666", linestyle="--", linewidth=1.0, label="4bps (Maker往復)")
        ax2.axhline(0.10, color="#d62728", linestyle=":", linewidth=1.1, label="10bps (標準往復)")
        ax2.axhline(0.20, color="#880e4f", linestyle="-.", linewidth=1.0, label="20bps (Taker+スリップ)")
        ax2.set_xticks(delays)
        ax2.set_xticklabels(["0分\n(確定足終値)", "+15分遅延", "+30分遅延"], fontsize=9.5)
        ax2.set_title("約定遅延 (0/+15/+30m) と往復コスト水準の損益分岐", fontsize=11.3, fontweight="bold")
        ax2.set_ylabel("平均グロス・リターン (%)", fontsize=9.5)
        ax2.grid(True, linestyle=":", alpha=0.6)
        ax2.legend(fontsize=8.0, loc="best")

    fig.tight_layout()
    p3 = PLOTS_FADE_DIR / "fade_03_placebo_alpha_and_delay.png"
    fig.savefig(p3, dpi=150, bbox_inches="tight")
    plt.close(fig)
    saved.append(p3)

    # ==========================================================
    # Chart 4: Cumulative Equity Curves (Deduplicated Portfolio, IS vs OOS)
    # ==========================================================
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6.0))
    df_eq = df_dedup_all.sort_values("shock_dt").reset_index(drop=True)
    if not df_eq.empty:
        x_tr = np.arange(1, len(df_eq) + 1)
        is_oos_split = int((df_eq["cohort"] == "Discovery").sum())
        # BTC 3h (0bps, 4bps, 10bps)
        for fee, ls, lbl in [(0.0, "-", "0bps (グロス)"), (0.04, "--", "4bps (Maker)"), (0.10, ":", "10bps (標準)")]:
            ax1.plot(x_tr, (df_eq["BTC_fade_post3"] - fee).cumsum(), linestyle=ls, linewidth=2.0, label=f"BTC 3h {lbl}")
        ax1.plot(x_tr, df_eq["NEAR_fade_post3"].cumsum(), color="#9467bd", linewidth=1.7, alpha=0.85, label="NEAR 3h (0bps)")
        ax1.plot(x_tr, df_eq["RENDER_fade_post6"].cumsum(), color="#2ca02c", linewidth=1.7, alpha=0.85, label="RENDER 6h (0bps)")
        ax1.axhline(0, color="#111111", linewidth=0.9)
        ax1.set_title(f"重複排除ポートフォリオ累積損益 (全期間時系列順 n={len(df_eq)})", fontsize=11.3, fontweight="bold")
        ax1.set_xlabel("独立ショック発生回数 (時系列順)", fontsize=9.5)
        ax1.set_ylabel("単利累積リターン (%)", fontsize=9.5)
        ax1.grid(True, linestyle=":", alpha=0.6)
        ax1.legend(fontsize=8.0, loc="best")

        # 右パネル: Long-Only (現物実行可能) vs Short-Only 累積 (BTC 3h & RENDER/NEAR平均 6h)
        long_df = df_eq[df_eq["fade_dir"] > 0].reset_index(drop=True)
        short_df = df_eq[df_eq["fade_dir"] < 0].reset_index(drop=True)
        if not long_df.empty:
            alt_long = 0.5 * (long_df["RENDER_fade_post6"] + long_df["NEAR_fade_post6"])
            ax2.plot(np.arange(1, len(long_df) + 1), long_df["BTC_fade_post3"].cumsum(), label=f"Long-Fade BTC 3h (0bps, n={len(long_df)})", color="#1f77b4", linewidth=2.1)
            ax2.plot(np.arange(1, len(long_df) + 1), (long_df["BTC_fade_post3"] - 0.10).cumsum(), label="Long-Fade BTC 3h (10bps)", color="#1f77b4", linestyle="--", linewidth=1.6)
            ax2.plot(np.arange(1, len(long_df) + 1), alt_long.cumsum(), label="Long-Fade RENDER+NEAR 6h (0bps)", color="#2ca02c", linewidth=2.0)
        if not short_df.empty:
            ax2.plot(np.arange(1, len(short_df) + 1), short_df["BTC_fade_post3"].cumsum(), label=f"Short-Fade BTC 3h (0bps, n={len(short_df)})", color="#d62728", linewidth=1.9)
        ax2.axhline(0, color="#111111", linewidth=0.9)
        ax2.set_title("Long-Fade（現物押目買い）vs Short-Fade（FOMO回避/売り）累積推移", fontsize=11.3, fontweight="bold")
        ax2.set_xlabel("トレード番号", fontsize=9.5)
        ax2.set_ylabel("単利累積リターン (%)", fontsize=9.5)
        ax2.grid(True, linestyle=":", alpha=0.6)
        ax2.legend(fontsize=8.0, loc="best")

    fig.tight_layout()
    p4 = PLOTS_FADE_DIR / "fade_04_equity_curves_oos.png"
    fig.savefig(p4, dpi=150, bbox_inches="tight")
    plt.close(fig)
    saved.append(p4)

    # ==========================================================
    # Chart 5: Category x Asset Net 4bps Heatmap & Shock Size Scatter
    # ==========================================================
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6.0))
    cats = ["FOMC/金利", "地政学", "Tech/AI・マクロ"]
    mat = np.zeros((len(cats), len(assets)))
    cnt_cat = []
    for i, c in enumerate(cats):
        sub_c = df_dedup_all[df_dedup_all["group"] == c]
        cnt_cat.append(len(sub_c))
        for j, a in enumerate(assets):
            mat[i, j] = float((sub_c[f"{a}_fade_post3"] - 0.04).mean()) if len(sub_c) else 0.0

    vmax = max(0.25, float(np.nanmax(np.abs(mat))))
    im = ax1.imshow(mat, cmap="RdYlGn", vmin=-vmax, vmax=vmax, aspect="auto")
    ax1.set_xticks(range(len(assets)))
    ax1.set_xticklabels(assets, fontsize=10, fontweight="bold")
    ax1.set_yticks(range(len(cats)))
    ax1.set_yticklabels([f"{c}\n(n={n})" for c, n in zip(cats, cnt_cat)], fontsize=9.5)
    for i in range(len(cats)):
        for j in range(len(assets)):
            ax1.text(j, i, f"{mat[i, j]:+.3f}%", ha="center", va="center", fontsize=9.0, fontweight="bold", color="#111111")
    fig.colorbar(im, ax=ax1, fraction=0.04, pad=0.03, label="3hフェード平均 (Maker 4bps控除後 %)")
    ax1.set_title("カテゴリ × 6銘柄 3hフェード平均リターン (4bps控除後)", fontsize=11.3, fontweight="bold")

    ax2.scatter(df_dedup_disc["abs_dp_pt"], df_dedup_disc["BTC_fade_post3"], alpha=0.65, color="#1f77b4", label="Discovery")
    ax2.scatter(df_dedup_oos["abs_dp_pt"], df_dedup_oos["BTC_fade_post3"], alpha=0.65, color="#ff7f0e", marker="^", label="未知 OOS")
    ax2.axhline(0, color="#111111", linewidth=1.0)
    ax2.axhline(0.10, color="#d62728", linestyle=":", linewidth=1.0, label="10bpsコスト")
    ax2.set_title("ショック強度 |dp| (pt) vs BTC 3hフェードリターン", fontsize=11.3, fontweight="bold")
    ax2.set_xlabel("Polymarket 1時間確率変化 |dp| (pt)", fontsize=9.5)
    ax2.set_ylabel("BTC 3h フェード・グロス・リターン (%)", fontsize=9.5)
    ax2.grid(True, linestyle=":", alpha=0.6)
    ax2.legend(fontsize=8.5, loc="best")

    fig.tight_layout()
    p5 = PLOTS_FADE_DIR / "fade_05_category_and_asset_heatmap.png"
    fig.savefig(p5, dpi=150, bbox_inches="tight")
    plt.close(fig)
    saved.append(p5)

    for p in saved:
        shutil.copy2(p, ARTIFACT_DIR / p.name)
    return saved


def save_and_print_fade_summary(res: Dict[str, Any], chart_paths: List[Path]) -> None:
    FADE_DIR.mkdir(parents=True, exist_ok=True)
    res["df_shocks"].to_csv(FADE_DIR / "all_shocks_raw.csv", index=False, encoding="utf-8")
    res["df_dedup_all"].to_csv(FADE_DIR / "dedup_shocks_all.csv", index=False, encoding="utf-8")
    res["df_delay"].to_csv(FADE_DIR / "execution_delay_15m.csv", index=False, encoding="utf-8")
    res["df_cond"].to_csv(FADE_DIR / "conditions_summary.csv", index=False, encoding="utf-8")

    # boot_dist を除いたプラセボ要約を保存
    pl_clean = [{k: v for k, v in p.items() if k != "boot_dist"} for p in res["placebo_results"]]
    pd.DataFrame(pl_clean).to_csv(FADE_DIR / "placebo_summary.csv", index=False, encoding="utf-8")

    print("\n" + "=" * 78)
    print("Polymarket リバーサル(フェード) 未知OOS＆プラセボ検証 サマリー")
    print("=" * 78)
    print(f"有効イベント: {len(res['events'])} (Discovery={sum(1 for e in res['events'] if e['cohort']=='Discovery')}, "
          f"Unseen_OOS={sum(1 for e in res['events'] if e['cohort']=='Unseen_OOS')})")
    print(f"独立ショック数 (重複排除後): 全統合={len(res['df_dedup_all'])} (Discovery={len(res['df_dedup_disc'])}, Unseen_OOS={len(res['df_dedup_oos'])})")

    c0 = res["df_cond"][(res["df_cond"]["condition"] == "C0_Baseline") & (res["df_cond"]["asset"] == "BTC")]
    print("\n--- C0 Baseline BTC (0bps グロス) ---")
    print(c0[["label", "horizon_h", "n", "mean_pct", "win_pct", "p_t", "p_wilcoxon", "q_fdr", "pf"]].to_string(index=False))

    print("\n--- Top 条件 (全統合, n>=15, 3h/6h, p_t昇順) ---")
    sub_top = res["df_cond"][(res["df_cond"]["label"] == "全統合(重複排除)") & (res["df_cond"]["n"] >= 15)].sort_values("p_t")
    print(sub_top[["condition", "asset", "horizon_h", "n", "mean_pct", "win_pct", "p_t", "q_fdr", "pf"]].head(15).to_string(index=False))

    print("\n--- マッチド暗号資産単独プラセボ比較 (All_Dedup) ---")
    print(pd.DataFrame([p for p in pl_clean if p["subset"] == "All_Dedup"])[["asset", "horizon_h", "n", "actual_mean", "placebo_mean", "excess_alpha", "p_placebo"]].to_string(index=False))

    print("\n--- 約定遅延 (15m検証) ---")
    if not res["df_delay"].empty:
        print(res["df_delay"].groupby(["asset", "horizon_h", "delay_min"])["fade_ret_pct"].agg(["count", "mean"]).round(4).to_string())

    print("\n生成チャート:")
    for p in chart_paths:
        print(f"  - {p} ({p.stat().st_size:,} bytes)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket 急変後リバーサル 未知OOS＆プラセボ検証")
    parser.add_argument("--refresh", action="store_true", help="キャッシュ無視で再取得")
    parser.add_argument("--max-oos", type=int, default=24, help="新規 OOS イベント上限")
    args = parser.parse_args()

    events = build_cohorts(max_oos=args.max_oos, refresh=args.refresh)
    res = run_fade_verification(events, refresh=args.refresh)
    charts = generate_fade_charts(res)
    save_and_print_fade_summary(res, charts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
