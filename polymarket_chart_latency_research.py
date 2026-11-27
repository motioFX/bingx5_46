"""チャート急変＆実現ボラティリティ → Polymarket（BTC/ETH価格市場）実践エッジ徹底検証

検証構成:
  1. CLOB fidelity=1 (1分足・直近7日) × Binance 1m 足の秒単位厳密整合 (+15秒ジッターの擬似ラグ排除)
  2. 真の1分足リード・ラグ曲線 (lag = -10m .. +10m, BH-FDR補正)
  3. 戦略A: BTC/ETH 1m/5m 急変ショック → Polymarket 出遅れスキャルピング (0m/+1m/+2m遅延, スプレッド控除後)
  4. 戦略B: チャート実現ボラ (7d/14d/30d RV) × ワンタッチ・バリアオプション理論価格 vs ライブCLOB板 & 7日乖離収束テスト
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
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
from scipy import optimize, stats
from statsmodels.stats.multitest import multipletests

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from polymarket_leadlag_research import (
    ARTIFACT_DIR,
    BINANCE_KLINES_URL,
    CLOB_HISTORY_URL,
    GAMMA_EVENTS_URL,
    configure_japanese_font,
    http_get_json,
    parse_json_list,
    shorten_text,
)
from polymarket_mispricing_research import fetch_book_metrics, parse_dt_utc

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

LAT_DIR = BASE_DIR / "Data" / "polymarket_research" / "chart_latency"
CLOB_1M_CACHE = LAT_DIR / "clob_1m_cache"
PLOTS_LAT_DIR = BASE_DIR / "Data" / "plots" / "polymarket_chart_latency"


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def one_touch_prob(
    spot: float,
    strike: float,
    sigma: float,
    t_years: float,
    direction: int,
    risk_neutral: bool = False,
) -> float:
    """幾何ブラウン運動に基づくワンタッチ・バリアオプション到達確率"""
    if spot <= 0 or strike <= 0:
        return np.nan
    if direction > 0 and spot >= strike:
        return 1.0
    if direction < 0 and spot <= strike:
        return 1.0
    if t_years <= 1e-6 or sigma <= 1e-6:
        return 0.0

    mu = (-0.5 * sigma * sigma) if risk_neutral else 0.0
    sd = sigma * math.sqrt(t_years)

    if direction > 0:
        # 上方バリア K > S0
        x = math.log(strike / spot)
        term1 = norm_cdf((-x + mu * t_years) / sd)
        exp_val = math.exp(min(50.0, (2.0 * mu * x) / (sigma * sigma)))
        term2 = exp_val * norm_cdf((-x - mu * t_years) / sd)
        return float(min(1.0, max(0.0, term1 + term2)))
    else:
        # 下方バリア K < S0
        b = math.log(spot / strike)
        term1 = norm_cdf((-b - mu * t_years) / sd)
        exp_val = math.exp(min(50.0, (-2.0 * mu * b) / (sigma * sigma)))
        term2 = exp_val * norm_cdf((-b + mu * t_years) / sd)
        return float(min(1.0, max(0.0, term1 + term2)))


def digital_above_prob(
    spot: float,
    strike: float,
    sigma: float,
    t_years: float,
    risk_neutral: bool = False,
) -> float:
    """満期時点上回り確率 (デジタルコール)"""
    if spot <= 0 or strike <= 0:
        return np.nan
    if t_years <= 1e-6:
        return 1.0 if spot >= strike else 0.0
    if sigma <= 1e-6:
        return 1.0 if spot >= strike else 0.0
    mu = (-0.5 * sigma * sigma) if risk_neutral else 0.0
    d2 = (math.log(spot / strike) + mu * t_years) / (sigma * math.sqrt(t_years))
    return float(min(1.0, max(0.0, norm_cdf(d2))))


def solve_implied_vol(
    market_prob: float,
    spot: float,
    strike: float,
    t_years: float,
    kind: str,
    direction: int,
) -> float:
    """Polymarket Mid価格から年率インプライド・ボラティリティ (IV) を二分法で逆算"""
    if not (0.01 <= market_prob <= 0.99) or t_years <= 1e-6:
        return np.nan

    def obj(sig: float) -> float:
        if kind == "hit":
            return one_touch_prob(spot, strike, sig, t_years, direction, risk_neutral=False) - market_prob
        return digital_above_prob(spot, strike, sig, t_years, risk_neutral=False) - market_prob

    try:
        f_lo, f_hi = obj(0.05), obj(3.50)
        if f_lo * f_hi > 0:
            return np.nan
        return float(optimize.brentq(obj, 0.05, 3.50, xtol=1e-4))
    except Exception:
        return np.nan


def parse_crypto_strike(event_title: str, sub_label: str) -> Optional[Dict[str, Any]]:
    """イベント名・サブタイトルから (asset, kind, strike, strike_dir) を抽出"""
    tl = event_title.lower()
    asset = "BTC" if "bitcoin" in tl else ("ETH" if "ethereum" in tl else None)
    if not asset:
        return None

    lbl = str(sub_label or "").strip()
    if "above" in tl:
        m = re.search(r"([\d,]+(?:\.\d+)?)", lbl)
        if not m:
            return None
        return {"asset": asset, "kind": "above", "strike": float(m.group(1).replace(",", "")), "strike_dir": 1}

    if "hit" in tl:
        m = re.search(r"([↑↓])\s*\$?([\d,]+(?:\.\d+)?)", lbl)
        if not m:
            return None
        return {
            "asset": asset,
            "kind": "hit",
            "strike": float(m.group(2).replace(",", "")),
            "strike_dir": 1 if m.group(1) == "↑" else -1,
        }
    return None


def select_active_crypto_strikes(max_tokens: int = 18, refresh: bool = False) -> List[Dict[str, Any]]:
    """現在アクティブな BTC/ETH 価格市場の争点ストライク（確率 6%〜94%）を選出"""
    LAT_DIR.mkdir(parents=True, exist_ok=True)
    cache_f = LAT_DIR / "selected_strikes.json"
    if cache_f.exists() and not refresh:
        cached = json.loads(cache_f.read_text(encoding="utf-8"))
        if isinstance(cached, list) and len(cached) >= 8:
            print(f"[Cache] 選定済みBTC/ETHストライク {len(cached)} 件を読み込みました。")
            return cached

    print("[Gamma API] アクティブな BTC / ETH 価格市場を探索中...")
    events = http_get_json(
        GAMMA_EVENTS_URL,
        {"active": "true", "closed": "false", "order": "volume24hr", "ascending": "false", "limit": 80},
        timeout=20,
    ) or []

    cands: List[Dict[str, Any]] = []
    for ev in events:
        title = str(ev.get("title") or "")
        tl = title.lower()
        if not (("bitcoin" in tl or "ethereum" in tl) and ("hit" in tl or "above" in tl)):
            continue
        end_dt = parse_dt_utc(ev.get("endDate"))
        if end_dt is None:
            continue

        for m in ev.get("markets") or []:
            if m.get("active") is not True or m.get("closed"):
                continue
            outs = parse_json_list(m.get("outcomes"))
            prs = parse_json_list(m.get("outcomePrices"))
            tids = parse_json_list(m.get("clobTokenIds"))
            if outs != ["Yes", "No"] or not prs or not tids:
                continue
            try:
                p_now = float(prs[0])
            except Exception:
                continue
            if not (0.06 <= p_now <= 0.94):
                continue

            sub_lbl = str(m.get("groupItemTitle") or m.get("question") or "")
            parsed = parse_crypto_strike(title, sub_lbl)
            if not parsed:
                continue

            m_end = parse_dt_utc(m.get("endDate")) or end_dt
            vol_m = float(m.get("volume") or 0.0)
            vol24_m = float(m.get("volume24hr") or 0.0)
            cands.append({
                "event_title": title,
                "sub_label": sub_lbl,
                "token_id": str(tids[0]),
                "end_ts": int(m_end.timestamp()),
                "end_iso": m_end.strftime("%Y-%m-%d %H:%M"),
                "gamma_prob": p_now,
                "volume": vol_m,
                "volume24hr": vol24_m,
                **parsed,
            })

    # 出来高＋24h出来高バランスで上位を選出（BTC・ETH両方バランス確保）
    cands.sort(key=lambda x: x["volume24hr"] + 0.05 * x["volume"], reverse=True)
    selected = cands[:max_tokens]
    cache_f.write_text(json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8")
    for s in selected:
        print(
            f"  + [{s['asset']}/{s['kind']:5s}] {shorten_text(s['event_title'], 36):36s} | "
            f"{s['sub_label']:10s} (K=${s['strike']:,.0f}, dir={s['strike_dir']:+d}, p={s['gamma_prob']*100:4.1f}%)"
        )
    return selected


def fetch_clob_1m_7d(token_id: str, end_ts: int, days: int = 7, refresh: bool = False) -> pd.DataFrame:
    """CLOB fidelity=1 (1分足) を 3.5日×2チャンクで安全取得"""
    CLOB_1M_CACHE.mkdir(parents=True, exist_ok=True)
    cf = CLOB_1M_CACHE / f"{token_id[:24]}_1m_{days}d.csv"
    if cf.exists() and not refresh:
        df = pd.read_csv(cf, encoding="utf-8")
        if not df.empty:
            return df

    start_ts = end_ts - days * 86400
    half_sec = int(3.5 * 86400)
    pts: Dict[int, float] = {}
    cur = start_ts
    while cur < end_ts:
        nxt = min(cur + half_sec, end_ts)
        data = http_get_json(
            CLOB_HISTORY_URL,
            {"market": token_id, "startTs": int(cur), "endTs": int(nxt), "fidelity": 1},
            timeout=15,
        )
        if data and isinstance(data, dict):
            for pt in data.get("history") or []:
                try:
                    t_s = int(pt["t"])
                    p_v = float(pt["p"])
                    if 0.0 <= p_v <= 1.0:
                        pts[t_s] = p_v
                except Exception:
                    continue
        cur = nxt
        time.sleep(0.08)

    if not pts:
        return pd.DataFrame(columns=["t", "p"])
    df = pd.DataFrame(sorted(pts.items()), columns=["t", "p"])
    df.to_csv(cf, index=False, encoding="utf-8")
    return df


def fetch_binance_1m_and_1h(symbol: str, now_ts: int, refresh: bool = False) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Binance 1m (直近7.5日) と 1h (直近35日) を取得し bar_close_ts 基準で返す"""
    LAT_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for interval, days, bar_sec in [("1m", 8, 60), ("1h", 36, 3600)]:
        cf = LAT_DIR / f"{symbol}_{interval}_recent.csv"
        if cf.exists() and not refresh:
            df_c = pd.read_csv(cf, encoding="utf-8")
            if len(df_c) > 500:
                df_c["bar_close_dt"] = pd.to_datetime(df_c["close_ts"], unit="s", utc=True)
                out[interval] = df_c.set_index("bar_close_dt").sort_index()
                continue

        print(f"  [Binance] {symbol} ({interval}) {days}日分を取得中...")
        cur_ms = (now_ts - days * 86400) * 1000
        end_ms = now_ts * 1000
        rows: Dict[int, Tuple[int, float, float, float, float]] = {}
        while cur_ms < end_ms:
            data = http_get_json(
                BINANCE_KLINES_URL,
                {"symbol": symbol, "interval": interval, "startTime": cur_ms, "endTime": end_ms, "limit": 1000},
                timeout=15,
            )
            if not data or not isinstance(data, list):
                break
            last_open = cur_ms
            for k in data:
                o_sec = int(k[0]) // 1000
                c_sec = o_sec + bar_sec
                assert int(k[6]) == c_sec * 1000 - 1
                rows[c_sec] = (o_sec, float(k[1]), float(k[2]), float(k[3]), float(k[4]))
                last_open = int(k[0])
            if len(data) < 1000:
                break
            cur_ms = last_open + bar_sec * 1000
            time.sleep(0.05)

        df_r = pd.DataFrame(
            [{"close_ts": c, "open_ts": v[0], "open": v[1], "high": v[2], "low": v[3], "close": v[4]} for c, v in sorted(rows.items())]
        )
        df_r.to_csv(cf, index=False, encoding="utf-8")
        df_r["bar_close_dt"] = pd.to_datetime(df_r["close_ts"], unit="s", utc=True)
        out[interval] = df_r.set_index("bar_close_dt").sort_index()
    return out["1m"], out["1h"]


def run_latency_and_option_research(refresh: bool = False) -> Dict[str, Any]:
    now_ts = int(time.time())
    strikes = select_active_crypto_strikes(max_tokens=18, refresh=refresh)

    print("\n[Step 1] Binance 1m (8d) / 1h (36d) と CLOB 1m (7d) ＋ ライブ板を取得中...")
    btc_1m, btc_1h = fetch_binance_1m_and_1h("BTCUSDT", now_ts, refresh=refresh)
    eth_1m, eth_1h = fetch_binance_1m_and_1h("ETHUSDT", now_ts, refresh=refresh)
    spot_1m = {"BTC": btc_1m, "ETH": eth_1m}
    spot_1h = {"BTC": btc_1h, "ETH": eth_1h}

    # 実現ボラティリティ (7d, 14d, 30d) の系列と最新値を計算
    rv_series: Dict[str, pd.DataFrame] = {}
    rv_now: Dict[str, Dict[str, float]] = {}
    for asset, df1h in spot_1h.items():
        r1h = np.log(df1h["close"] / df1h["close"].shift(1))
        ann = math.sqrt(24.0 * 365.0)
        df_rv = pd.DataFrame(index=df1h.index)
        df_rv["close"] = df1h["close"]
        df_rv["rv_7d"] = r1h.rolling(7 * 24).std() * ann
        df_rv["rv_14d"] = r1h.rolling(14 * 24).std() * ann
        df_rv["rv_30d"] = r1h.rolling(30 * 24).std() * ann
        rv_series[asset] = df_rv
        rv_now[asset] = {
            "spot": float(df1h["close"].iloc[-1]),
            "rv_7d": float(df_rv["rv_7d"].iloc[-1]),
            "rv_14d": float(df_rv["rv_14d"].iloc[-1]),
            "rv_30d": float(df_rv["rv_30d"].iloc[-1]),
        }
        print(
            f"  {asset}: Spot=${rv_now[asset]['spot']:,.1f} | "
            f"RV 7d={rv_now[asset]['rv_7d']*100:.1f}%, 14d={rv_now[asset]['rv_14d']*100:.1f}%, 30d={rv_now[asset]['rv_30d']*100:.1f}%"
        )

    # CLOB 1m と ライブ板 を取得
    clob_1m_map: Dict[str, pd.DataFrame] = {}
    books_map: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs_hist = {ex.submit(fetch_clob_1m_7d, s["token_id"], now_ts, 7, refresh): s["token_id"] for s in strikes}
        futs_bk = {ex.submit(fetch_book_metrics, s["token_id"]): s["token_id"] for s in strikes}
        for fut in as_completed(futs_hist):
            tid = futs_hist[fut]
            clob_1m_map[tid] = fut.result()
        for fut in as_completed(futs_bk):
            tid = futs_bk[fut]
            bk = fut.result()
            if bk:
                books_map[tid] = bk

    # --- 検証 1: 秒単位厳密整合 1分足リード・ラグ CCF & 旧15mリサンプル比較 ---
    print("\n[Step 2] 秒単位厳密整合による真の1分足リード・ラグ分析を実行中...")
    all_jitters: List[float] = []
    ccf_1m_rows: List[Dict[str, Any]] = []
    ccf_15m_old_rows: List[Dict[str, Any]] = []
    aligned_1m_map: Dict[str, pd.DataFrame] = {}

    for s in strikes:
        tid = s["token_id"]
        df_raw = clob_1m_map.get(tid, pd.DataFrame())
        if len(df_raw) < 300:
            continue

        # 秒単位ジッター計測 & 最近傍分へスナップ (max 30s)
        t_raw = df_raw["t"].values.astype(np.int64)
        m_snap = (np.round(t_raw / 60.0) * 60).astype(np.int64)
        jitters = t_raw - m_snap
        all_jitters.extend(jitters.tolist())

        df_snap = pd.DataFrame({"close_ts": m_snap, "p": df_raw["p"].values})
        df_snap = df_snap.groupby("close_ts", as_index=False)["p"].last().sort_values("close_ts")
        df_snap["dt"] = pd.to_datetime(df_snap["close_ts"], unit="s", utc=True)
        p_1m = df_snap.set_index("dt")["p"]

        cdf1m = spot_1m[s["asset"]]
        idx_common = cdf1m.loc[p_1m.index.min() : p_1m.index.max()].index
        if len(idx_common) < 300:
            continue

        p_al = p_1m.reindex(idx_common).ffill(limit=2)
        df_al = pd.DataFrame(index=idx_common)
        df_al["prob"] = p_al
        df_al["dp_cents"] = df_al["prob"].diff() * 100.0
        df_al["close"] = cdf1m["close"].reindex(idx_common)
        df_al["ret_1m"] = np.log(df_al["close"] / df_al["close"].shift(1)) * 100.0
        df_al["ret_5m"] = np.log(df_al["close"] / df_al["close"].shift(5)) * 100.0
        df_al = df_al.dropna(subset=["prob", "dp_cents", "ret_1m"])
        if len(df_al) < 300:
            continue
        aligned_1m_map[tid] = df_al

        # 方向一致 dp (strike_dir = +1 / -1)
        eff_dp = df_al["dp_cents"] * s["strike_dir"]
        for lag in range(-10, 11):
            # lag = -k: BTC が k分先行 -> Corr(eff_dp[t], ret_1m[t+lag])
            j = pd.concat([eff_dp, df_al["ret_1m"].shift(-lag)], axis=1).dropna()
            if len(j) < 100 or j.iloc[:, 0].std() == 0 or j.iloc[:, 1].std() == 0:
                continue
            r_v, p_v = stats.pearsonr(j.iloc[:, 0].values, j.iloc[:, 1].values)
            ccf_1m_rows.append({
                "token_id": tid, "asset": s["asset"], "event_title": s["event_title"],
                "sub_label": s["sub_label"], "lag_min": lag, "corr": float(r_v), "p_val": float(p_v), "n": len(j),
            })

        # 比較用: 旧 15m right-closed リサンプル (ジッター繰り越しあり)
        p_raw_ts = pd.Series(df_raw["p"].values, index=pd.to_datetime(df_raw["t"], unit="s", utc=True))
        p_old15 = p_raw_ts.resample("15min", label="right", closed="right").last().ffill(limit=2)
        c_old15 = cdf1m["close"].resample("15min", label="right", closed="right").last()
        j_old = pd.DataFrame({
            "dp": p_old15.diff() * 100.0 * s["strike_dir"],
            "ret": np.log(c_old15 / c_old15.shift(1)) * 100.0,
        }).dropna()
        for lag15 in range(-4, 5):
            jj = pd.concat([j_old["dp"], j_old["ret"].shift(-lag15)], axis=1).dropna()
            if len(jj) >= 40 and jj.iloc[:, 0].std() > 0 and jj.iloc[:, 1].std() > 0:
                rv15, _ = stats.pearsonr(jj.iloc[:, 0].values, jj.iloc[:, 1].values)
                ccf_15m_old_rows.append({"token_id": tid, "lag_15m": lag15, "corr": float(rv15)})

    df_ccf_1m = pd.DataFrame(ccf_1m_rows)
    if not df_ccf_1m.empty:
        _, qv, _, _ = multipletests(df_ccf_1m["p_val"].values, method="fdr_bh")
        df_ccf_1m["q_fdr"] = qv
    df_ccf_15m_old = pd.DataFrame(ccf_15m_old_rows)

    # --- 検証 2: 戦略A — BTC/ETH 1m/5m 急変ショック → Polymarket 出遅れスキャルピング ---
    print("[Step 3] 戦略A: BTC/ETH 1m・5m急変イベントスタディ＆スキャルピング検証中...")
    spike_rows: List[Dict[str, Any]] = []
    spike_curves: List[Dict[str, Any]] = []

    for shock_type, ret_col, thresh_pct, cd_mins in [
        ("1m急変(|r1m|>=0.35%)", "ret_1m", 0.35, 10),
        ("5m急変(|r5m|>=0.70%)", "ret_5m", 0.70, 20),
    ]:
        for s in strikes:
            tid = s["token_id"]
            if tid not in aligned_1m_map:
                continue
            df_al = aligned_1m_map[tid]
            bk = books_map.get(tid, {})
            live_spread = float(bk.get("spread_cents", 1.0)) if bk else 1.0
            slip100 = float(bk.get("slip_100_cents", 0.5)) if bk else 0.5

            last_pos = -cd_mins - 1
            for pos in range(6, len(df_al) - 31):
                r_val = float(df_al[ret_col].iloc[pos])
                if abs(r_val) < thresh_pct or (pos - last_pos) <= cd_mins:
                    continue
                last_pos = pos

                # 期待する Polymarket の同方向符号 (+1なら確率上昇, -1なら下落)
                exp_dir = float(np.sign(r_val) * s["strike_dir"])
                # t=-1 の確率を基準に t-5 .. t+30 の累積変化(¢)を計測
                p_pre1 = float(df_al["prob"].iloc[pos - 1]) * 100.0
                # すでに96%超または4%未満に張り付いていた場合はスキップ
                if p_pre1 < 4.0 or p_pre1 > 96.0:
                    continue

                p_win = df_al["prob"].iloc[pos - 5 : pos + 31].values * 100.0
                if len(p_win) != 36:
                    continue
                # 相対時刻 -5..+30 の方向一致累積オッズ変化(¢) (t=-1 を 0 とする)
                cum_dir_cents = (p_win - p_pre1) * exp_dir

                for rel_idx, rel_m in enumerate(range(-5, 31)):
                    spike_curves.append({
                        "shock_type": shock_type, "asset": s["asset"], "rel_m": rel_m,
                        "cum_dir_cents": float(cum_dir_cents[rel_idx]),
                    })

                # 事後トレードリターン(¢): Entry = T+delay_m, Exit = T+exit_m
                # idx 4 = t-1, idx 5 = t=0, idx 6 = t+1, ... idx 5+m = t+m
                p_t0 = float(p_win[5])
                p_t1 = float(p_win[6])
                p_t2 = float(p_win[7])
                p_t5 = float(p_win[10])
                p_t10 = float(p_win[15])
                p_t15 = float(p_win[20])

                spike_rows.append({
                    "shock_type": shock_type, "token_id": tid, "asset": s["asset"],
                    "sub_label": s["sub_label"], "shock_dt": df_al.index[pos].isoformat(),
                    "ret_shock_pct": r_val, "exp_dir": exp_dir, "spread_cents": live_spread,
                    "slip_100_cents": slip100,
                    "sim_0m_cents": float((p_t0 - p_pre1) * exp_dir),
                    "drift_0_to_5m_cents": float((p_t5 - p_t0) * exp_dir),
                    "drift_0_to_10m_cents": float((p_t10 - p_t0) * exp_dir),
                    "drift_0_to_15m_cents": float((p_t15 - p_t0) * exp_dir),
                    "drift_1_to_10m_cents": float((p_t10 - p_t1) * exp_dir),
                    "drift_2_to_10m_cents": float((p_t10 - p_t2) * exp_dir),
                })

    df_spikes = pd.DataFrame(spike_rows)
    df_spike_curves = pd.DataFrame(spike_curves)

    # --- 検証 3: 戦略B — ワンタッチ・バリアオプション理論価格 vs ライブCLOB & 7日平均回帰 ---
    print("[Step 4] 戦略B: チャート実現ボラ理論価格 vs Polymarket IV・7日乖離収束テスト中...")
    option_rows: List[Dict[str, Any]] = []
    fv_backtest_rows: List[Dict[str, Any]] = []

    for s in strikes:
        tid = s["token_id"]
        asset = s["asset"]
        rv_info = rv_now[asset]
        spot = rv_info["spot"]
        t_rem_years = max(0.0, (s["end_ts"] - now_ts) / (365.0 * 86400.0))

        bk = books_map.get(tid, {})
        bid = float(bk["best_bid"]) if bk else s["gamma_prob"]
        ask = float(bk["best_ask"]) if bk else s["gamma_prob"]
        mid = float(bk["mid"]) if bk else s["gamma_prob"]
        spread_c = (ask - bid) * 100.0

        def calc_fair(sp: float, sig: float, ty: float, rn: bool) -> float:
            if s["kind"] == "hit":
                return one_touch_prob(sp, s["strike"], sig, ty, s["strike_dir"], risk_neutral=rn)
            return digital_above_prob(sp, s["strike"], sig, ty, risk_neutral=rn)

        f_7d = calc_fair(spot, rv_info["rv_7d"], t_rem_years, False)
        f_14d = calc_fair(spot, rv_info["rv_14d"], t_rem_years, False)
        f_30d = calc_fair(spot, rv_info["rv_30d"], t_rem_years, False)
        f_14d_rn = calc_fair(spot, rv_info["rv_14d"], t_rem_years, True)
        iv_poly = solve_implied_vol(mid, spot, s["strike"], t_rem_years, s["kind"], s["strike_dir"])

        # Takerで即座に買える理論エッジ (買いなら Fair - Ask、売りなら Bid - Fair)
        buy_yes_edge_c = (f_14d - ask) * 100.0
        buy_no_edge_c = (bid - f_14d) * 100.0
        maker_edge_c = (f_14d - mid) * 100.0

        option_rows.append({
            "asset": asset, "kind": s["kind"], "event_title": s["event_title"],
            "sub_label": s["sub_label"], "strike": s["strike"], "strike_dir": s["strike_dir"],
            "days_rem": t_rem_years * 365.0,
            "spot": spot, "bid_cents": bid * 100.0, "mid_cents": mid * 100.0, "ask_cents": ask * 100.0,
            "spread_cents": spread_c,
            "fair_7d_cents": f_7d * 100.0, "fair_14d_cents": f_14d * 100.0,
            "fair_30d_cents": f_30d * 100.0, "fair_14d_rn_cents": f_14d_rn * 100.0,
            "rv_14d_pct": rv_info["rv_14d"] * 100.0,
            "poly_iv_pct": iv_poly * 100.0 if pd.notna(iv_poly) else np.nan,
            "iv_minus_rv_pct": (iv_poly - rv_info["rv_14d"]) * 100.0 if pd.notna(iv_poly) else np.nan,
            "maker_edge_cents": maker_edge_c,
            "best_taker_edge_cents": max(buy_yes_edge_c, buy_no_edge_c),
            "best_taker_side": "Buy Yes" if buy_yes_edge_c >= buy_no_edge_c else "Buy No",
        })

        # 過去7日間のローリング 1h 乖離収束テスト
        if tid in aligned_1m_map:
            df_al_1h = aligned_1m_map[tid]["prob"].resample("1h", label="right", closed="right").last().dropna()
            rv_df = rv_series[asset].reindex(df_al_1h.index).ffill()
            if len(df_al_1h) >= 36:
                times_h = df_al_1h.index.tolist()
                p_vals = df_al_1h.values * 100.0
                last_sig_i = -7
                for i in range(0, len(times_h) - 24):
                    if i - last_sig_i < 6:
                        continue
                    dt_i = times_h[i]
                    sp_i = float(rv_df["close"].iloc[i])
                    sig_i = float(rv_df["rv_14d"].iloc[i])
                    t_rem_i = max(0.0, (s["end_ts"] - int(dt_i.timestamp())) / (365.0 * 86400.0))
                    if np.isnan(sp_i) or np.isnan(sig_i) or t_rem_i <= 1e-4:
                        continue
                    fair_i = calc_fair(sp_i, sig_i, t_rem_i, False) * 100.0
                    mkt_i = float(p_vals[i])
                    dev_i = mkt_i - fair_i  # +なら割高(Buy No), -なら割安(Buy Yes)
                    if abs(dev_i) < 3.0 or mkt_i < 5.0 or mkt_i > 95.0:
                        continue
                    last_sig_i = i
                    side_sign = -1.0 if dev_i > 0 else 1.0  # +1=Buy Yes, -1=Buy No
                    entry_cost = mkt_i if side_sign > 0 else (100.0 - mkt_i)
                    rec_fv = {
                        "asset": asset, "sub_label": s["sub_label"], "dt": dt_i.isoformat(),
                        "side": "Buy Yes" if side_sign > 0 else "Buy No",
                        "dev_cents": dev_i, "entry_cost_cents": entry_cost,
                    }
                    for h in [3, 6, 12, 24]:
                        pnl_c = (float(p_vals[i + h]) - mkt_i) * side_sign
                        rec_fv[f"pnl_{h}h_cents"] = pnl_c
                        rec_fv[f"roi_{h}h_pct"] = pnl_c / max(entry_cost, 1.0) * 100.0
                    fv_backtest_rows.append(rec_fv)

    df_options = pd.DataFrame(option_rows)
    df_fv_bt = pd.DataFrame(fv_backtest_rows)

    return {
        "strikes": strikes,
        "jitters": np.array(all_jitters, dtype=float),
        "df_ccf_1m": df_ccf_1m,
        "df_ccf_15m_old": df_ccf_15m_old,
        "df_spikes": df_spikes,
        "df_spike_curves": df_spike_curves,
        "df_options": df_options,
        "df_fv_bt": df_fv_bt,
        "rv_now": rv_now,
    }


def generate_latency_charts(res: Dict[str, Any]) -> List[Path]:
    configure_japanese_font()
    PLOTS_LAT_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    saved: List[Path] = []

    # ==========================================================
    # Chart 1: True 1m Latency CCF & +15s Jitter Artifact Check
    # ==========================================================
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6.2))
    df1 = res["df_ccf_1m"]
    if not df1.empty:
        grp_mean = df1.groupby("lag_min")["corr"].mean()
        grp_se = df1.groupby("lag_min")["corr"].sem().fillna(0)
        xs = grp_mean.index.values
        ax1.plot(xs, grp_mean.values, marker="o", linewidth=2.3, color="#1f77b4", label=f"秒単位厳密整合 1m CCF (全{df1['token_id'].nunique()}銘柄平均)")
        ax1.fill_between(xs, grp_mean.values - 1.96 * grp_se.values, grp_mean.values + 1.96 * grp_se.values, color="#1f77b4", alpha=0.16)
        # 最強個別銘柄も重ねる
        best_tid = df1.loc[df1["corr"].idxmax(), "token_id"]
        sub_b = df1[df1["token_id"] == best_tid].sort_values("lag_min")
        ax1.plot(sub_b["lag_min"], sub_b["corr"], linestyle="--", marker="s", markersize=4, color="#d62728", alpha=0.85,
                 label=f"最大反応銘柄: {sub_b['asset'].iloc[0]} {sub_b['sub_label'].iloc[0]}")
        ax1.axvline(0, color="#111111", linestyle=":", linewidth=1.3)
        ax1.axhline(0, color="#666666", linewidth=0.9)
        ax1.set_title("真の1分足 リード・ラグ CCF 曲線 (lag -10分 .. +10分)", fontsize=11.5, fontweight="bold")
        ax1.set_xlabel("ラグ (分)  ← BTC/ETH先行 (-10m)  |  同時(0m)  |  Polymarket先行 (+10m) →", fontsize=9.5)
        ax1.set_ylabel("相関係数 r (方向一致)", fontsize=9.5)
        ax1.grid(True, linestyle=":", alpha=0.6)
        ax1.legend(fontsize=8.3, loc="best")

    jit = res["jitters"]
    if len(jit) > 0:
        ax2.hist(jit, bins=np.arange(-30, 32, 2), color="#ff9800", edgecolor="#333333", alpha=0.82)
        ax2.axvline(0, color="#111111", linestyle="--", linewidth=1.2, label="正分 (00秒)")
        ax2.axvline(float(np.median(jit)), color="#d62728", linewidth=2.0, label=f"中央値 {np.median(jit):+.1f}秒 (CLOB記録遅延)")
        ax2.set_title("CLOB API タイムスタンプ記録ジッター分布 (正分からのズレ)", fontsize=11.5, fontweight="bold")
        ax2.set_xlabel("正分からの秒差 (秒)", fontsize=9.5)
        ax2.set_ylabel("観測点数", fontsize=9.5)
        ax2.grid(True, axis="y", linestyle=":", alpha=0.6)
        ax2.legend(fontsize=8.8)

    fig.tight_layout()
    p1 = PLOTS_LAT_DIR / "lat_01_true_1m_latency_profile.png"
    fig.savefig(p1, dpi=150, bbox_inches="tight")
    plt.close(fig)
    saved.append(p1)

    # ==========================================================
    # Chart 2: BTC/ETH 1m & 5m Spike Event Study on Polymarket Odds
    # ==========================================================
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6.2))
    df_sc = res["df_spike_curves"]
    if not df_sc.empty:
        for stype, col in [("1m急変(|r1m|>=0.35%)", "#1f77b4"), ("5m急変(|r5m|>=0.70%)", "#2ca02c")]:
            sub = df_sc[df_sc["shock_type"] == stype]
            if sub.empty:
                continue
            piv_m = sub.groupby("rel_m")["cum_dir_cents"].mean()
            piv_se = sub.groupby("rel_m")["cum_dir_cents"].sem().fillna(0)
            ax1.plot(piv_m.index, piv_m.values, linewidth=2.2, marker="o", markersize=3.5, color=col, label=f"{stype}")
            ax1.fill_between(piv_m.index, piv_m.values - 1.96 * piv_se.values, piv_m.values + 1.96 * piv_se.values, color=col, alpha=0.14)
        ax1.axvline(0, color="#d62728", linestyle="--", linewidth=1.2, label="BTC/ETH 急変確定足 (t=0m)")
        ax1.axvspan(0.5, 30.5, color="#e3f2fd", alpha=0.35, label="事後トレード可能区間 (+1m..+30m)")
        ax1.axhline(0, color="#111111", linewidth=0.9)
        ax1.set_title("BTC/ETH 急変ショック前後の Polymarket 累積確率変動 (t=-1m基準)", fontsize=11.2, fontweight="bold")
        ax1.set_xlabel("急変確定足からの経過時間 (分)", fontsize=9.5)
        ax1.set_ylabel("方向一致 確率変化 (¢)", fontsize=9.5)
        ax1.grid(True, linestyle=":", alpha=0.6)
        ax1.legend(fontsize=8.2, loc="best")

    df_sp = res["df_spikes"]
    if not df_sp.empty:
        # 遅延別純獲得幅 (vs スプレッド1.0¢)
        modes = [
            ("同時反映\n(t=-1→0m)", "sim_0m_cents"),
            ("0分遅延\n(T0→T+5m)", "drift_0_to_5m_cents"),
            ("0分遅延\n(T0→T+10m)", "drift_0_to_10m_cents"),
            ("0分遅延\n(T0→T+15m)", "drift_0_to_15m_cents"),
            ("+1分遅延\n(T+1→T+10m)", "drift_1_to_10m_cents"),
            ("+2分遅延\n(T+2→T+10m)", "drift_2_to_10m_cents"),
        ]
        xm = np.arange(len(modes))
        wm = 0.35
        for idx, (stype, col) in enumerate([("1m急変(|r1m|>=0.35%)", "#1f77b4"), ("5m急変(|r5m|>=0.70%)", "#2ca02c")]):
            sub_s = df_sp[df_sp["shock_type"] == stype]
            vals = [float(sub_s[c].mean()) if len(sub_s) else 0.0 for _, c in modes]
            ax2.bar(xm + (idx - 0.5) * wm, vals, width=wm, label=f"{stype} (n={len(sub_s)})", color=col, alpha=0.88)
        ax2.axhline(0, color="#111111", linewidth=0.9)
        ax2.axhline(1.0, color="#d62728", linestyle="--", linewidth=1.3, label="実測ライブスプレッド中央値 (1.0¢)")
        ax2.set_xticks(xm)
        ax2.set_xticklabels([m[0] for m in modes], fontsize=8.5)
        ax2.set_title("戦略A: 約定遅延別の獲得値幅 (¢) vs スプレッド障壁", fontsize=11.2, fontweight="bold")
        ax2.set_ylabel("平均獲得幅 (¢ / シェア, コスト前)", fontsize=9.5)
        ax2.grid(True, axis="y", linestyle=":", alpha=0.6)
        ax2.legend(fontsize=8.2, loc="best")

    fig.tight_layout()
    p2 = PLOTS_LAT_DIR / "lat_02_btc_spike_event_study_1m.png"
    fig.savefig(p2, dpi=150, bbox_inches="tight")
    plt.close(fig)
    saved.append(p2)

    # ==========================================================
    # Chart 3: Option Fair Value (RV) vs Live Polymarket Odds & IV Smile
    # ==========================================================
    df_opt = res["df_options"]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16.5, 6.5))
    if not df_opt.empty:
        df_o = df_opt.sort_values("maker_edge_cents").reset_index(drop=True)
        yo = np.arange(len(df_o))
        ax1.hlines(yo, df_o["bid_cents"], df_o["ask_cents"], color="#555555", linewidth=3.5, alpha=0.8, label="ライブ Bid〜Ask レンジ")
        ax1.scatter(df_o["mid_cents"], yo, color="#111111", s=30, zorder=5, label="ライブ Mid")
        ax1.scatter(df_o["fair_7d_cents"], yo, color="#ff7f0e", marker="^", s=50, zorder=6, label="理論確率 (7d RV)")
        ax1.scatter(df_o["fair_14d_cents"], yo, color="#1f77b4", marker="D", s=50, zorder=7, label="理論確率 (14d RV)")
        ax1.scatter(df_o["fair_30d_cents"], yo, color="#2ca02c", marker="s", s=42, zorder=6, label="理論確率 (30d RV)")
        ax1.set_yticks(yo)
        ax1.set_yticklabels([f"[{r['asset']}] {shorten_text(r['event_title'], 22)} | {r['sub_label']}" for _, r in df_o.iterrows()], fontsize=8.0)
        ax1.set_title("戦略B: ライブ市場価格 (Bid/Ask) vs バリア理論確率 (7d/14d/30d RV)", fontsize=11.0, fontweight="bold")
        ax1.set_xlabel("確率 / 価格 (¢)", fontsize=9.5)
        ax1.grid(True, axis="x", linestyle=":", alpha=0.6)
        ax1.legend(fontsize=8.0, loc="best")

        # 右パネル: IV vs RV (ストライク乖離別)
        valid_iv = df_opt.dropna(subset=["poly_iv_pct"]).copy()
        if not valid_iv.empty:
            valid_iv["moneyness_pct"] = (valid_iv["strike"] / valid_iv["spot"] - 1.0) * 100.0
            for asset, col, mk in [("BTC", "#1f77b4", "o"), ("ETH", "#ff7f0e", "^")]:
                sub_a = valid_iv[valid_iv["asset"] == asset]
                if not sub_a.empty:
                    ax2.scatter(sub_a["moneyness_pct"], sub_a["poly_iv_pct"], s=70, color=col, marker=mk, label=f"{asset} Polymarket IV (%)")
                    ax2.axhline(res["rv_now"][asset]["rv_14d"] * 100.0, color=col, linestyle="--", linewidth=1.3,
                                label=f"{asset} 14d 実現ボラ RV ({res['rv_now'][asset]['rv_14d']*100:.1f}%)")
            ax2.axvline(0, color="#333333", linestyle=":", linewidth=1.1)
            ax2.set_title("Polymarket インプライド・ボラ (IV) vs チャート実現ボラ (14d RV)", fontsize=11.0, fontweight="bold")
            ax2.set_xlabel("目標価格の現在価格からの乖離 (%)", fontsize=9.5)
            ax2.set_ylabel("年率ボラティリティ (%)", fontsize=9.5)
            ax2.grid(True, linestyle=":", alpha=0.6)
            ax2.legend(fontsize=8.2, loc="best")

    fig.tight_layout()
    p3 = PLOTS_LAT_DIR / "lat_03_option_fair_value_vs_live_odds.png"
    fig.savefig(p3, dpi=150, bbox_inches="tight")
    plt.close(fig)
    saved.append(p3)

    # ==========================================================
    # Chart 4: Fair Value Divergence Convergence & Strategy Comparison
    # ==========================================================
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6.2))
    df_fv = res["df_fv_bt"]
    if not df_fv.empty:
        hs = [3, 6, 12, 24]
        xf = np.arange(len(hs))
        wf = 0.26
        for idx, (side_lbl, sub_df, col) in enumerate([
            ("全シグナル", df_fv, "#1f77b4"),
            ("割高売り (Buy No)", df_fv[df_fv["side"] == "Buy No"], "#2ca02c"),
            ("割安買い (Buy Yes)", df_fv[df_fv["side"] == "Buy Yes"], "#ff7f0e"),
        ]):
            means = [float(sub_df[f"pnl_{h}h_cents"].mean()) if len(sub_df) else 0.0 for h in hs]
            ax1.bar(xf + (idx - 1) * wf, means, width=wf, label=f"{side_lbl} (n={len(sub_df)})", color=col, alpha=0.88)
        ax1.axhline(0, color="#111111", linewidth=0.9)
        ax1.axhline(1.0, color="#d62728", linestyle="--", linewidth=1.2, label="Taker 往復コスト目安 (1.0¢)")
        ax1.set_xticks(xf)
        ax1.set_xticklabels([f"{h}h後" for h in hs], fontsize=9.5)
        ax1.set_title("戦略B 過去7日検証: 理論価格乖離(|Mkt-Fair|>=3¢) 後の値幅収束 (¢)", fontsize=11.0, fontweight="bold")
        ax1.set_ylabel("平均獲得値幅 (¢ / シェア, コスト前)", fontsize=9.5)
        ax1.grid(True, axis="y", linestyle=":", alpha=0.6)
        ax1.legend(fontsize=8.3, loc="best")

        # 右パネル: ROI(%) 累積推移 (Maker 0¢ vs Taker 1¢)
        df_fv_s = df_fv.sort_values("dt").reset_index(drop=True)
        xr = np.arange(1, len(df_fv_s) + 1)
        roi_maker = df_fv_s["roi_12h_pct"].values
        roi_taker = ((df_fv_s["pnl_12h_cents"] - 1.0) / df_fv_s["entry_cost_cents"].clip(lower=1.0) * 100.0).values
        ax2.plot(xr, np.cumsum(roi_maker), color="#2ca02c", linewidth=2.2, label=f"戦略B 12h収束 (Maker 0¢, n={len(xr)})")
        ax2.plot(xr, np.cumsum(roi_taker), color="#1f77b4", linestyle="--", linewidth=1.8, label="戦略B 12h収束 (Taker 1.0¢控除後)")
        ax2.axhline(0, color="#111111", linewidth=0.9)
        ax2.set_title("戦略B 理論価格乖離トレード (12h保有) 単利累積 ROI 推移 (過去7日間)", fontsize=11.0, fontweight="bold")
        ax2.set_xlabel("シグナル番号 (時系列)", fontsize=9.5)
        ax2.set_ylabel("単利累積 ROI (%)", fontsize=9.5)
        ax2.grid(True, linestyle=":", alpha=0.6)
        ax2.legend(fontsize=8.3, loc="best")

    fig.tight_layout()
    p4 = PLOTS_LAT_DIR / "lat_04_fair_value_divergence_backtest.png"
    fig.savefig(p4, dpi=150, bbox_inches="tight")
    plt.close(fig)
    saved.append(p4)

    for p in saved:
        shutil.copy2(p, ARTIFACT_DIR / p.name)
    return saved


def save_and_print_latency_summary(res: Dict[str, Any], charts: List[Path]) -> None:
    LAT_DIR.mkdir(parents=True, exist_ok=True)
    res["df_ccf_1m"].to_csv(LAT_DIR / "ccf_1m_true_latency.csv", index=False, encoding="utf-8")
    res["df_ccf_15m_old"].to_csv(LAT_DIR / "ccf_15m_old_comparison.csv", index=False, encoding="utf-8")
    res["df_spikes"].to_csv(LAT_DIR / "spike_scalping_events.csv", index=False, encoding="utf-8")
    res["df_options"].to_csv(LAT_DIR / "option_fair_value_live.csv", index=False, encoding="utf-8")
    res["df_fv_bt"].to_csv(LAT_DIR / "fair_value_backtest_7d.csv", index=False, encoding="utf-8")

    print("\n" + "=" * 78)
    print("チャート急変＆実現ボラティリティ → Polymarket 実践エッジ検証 サマリー")
    print("=" * 78)
    jit = res["jitters"]
    print(f"対象ストライク数: {len(res['strikes'])} | CLOB記録ジッター: 中央値={np.median(jit):+.1f}s, 平均={np.mean(jit):+.1f}s")

    df1 = res["df_ccf_1m"]
    if not df1.empty:
        print("\n--- 1. 真の1分足リード・ラグ CCF 平均 (lag -5m..+3m) & FDR q<0.05件数 ---")
        sub_l = df1[df1["lag_min"].between(-5, 3)].groupby("lag_min").agg(
            mean_corr=("corr", "mean"),
            max_corr=("corr", "max"),
            fdr_sig_cnt=("q_fdr", lambda s: int((s < 0.05).sum())),
        )
        print(sub_l.round(4).to_string())

    df15 = res["df_ccf_15m_old"]
    if not df15.empty:
        print("\n--- 参考: 旧 15m right-closed リサンプル CCF 平均 ---")
        print(df15.groupby("lag_15m")["corr"].mean().round(4).to_string())

    df_sp = res["df_spikes"]
    if not df_sp.empty:
        print("\n--- 2. 戦略A: BTC/ETH 急変スキャルピング (平均 ¢ 獲得幅 / 勝率) ---")
        for stype, g in df_sp.groupby("shock_type"):
            print(f"\n[{stype}] n={len(g)}, 実測スプレッド中央値={g['spread_cents'].median():.2f}¢")
            for col in ["sim_0m_cents", "drift_0_to_5m_cents", "drift_0_to_10m_cents", "drift_0_to_15m_cents", "drift_1_to_10m_cents", "drift_2_to_10m_cents"]:
                v = g[col].dropna().values
                t_s, p_v = stats.ttest_1samp(v, 0.0) if len(v) >= 5 else (0.0, 1.0)
                print(f"  {col:22s}: mean={v.mean():+.3f}¢ | win={(v>0).mean()*100:5.1f}% | net_after_1c={(v-1.0).mean():+.3f}¢ | p={p_v:.4f}")

    df_o = res["df_options"]
    if not df_o.empty:
        print("\n--- 3. 戦略B: ライブ理論確率 (14d RV) vs Polymarket (上位ギャップ順) ---")
        cols_show = ["asset", "sub_label", "days_rem", "bid_cents", "mid_cents", "ask_cents", "fair_14d_cents", "poly_iv_pct", "iv_minus_rv_pct", "best_taker_side", "best_taker_edge_cents"]
        print(df_o.sort_values("best_taker_edge_cents", ascending=False)[cols_show].round(2).to_string(index=False))

    df_fv = res["df_fv_bt"]
    if not df_fv.empty:
        print("\n--- 4. 戦略B 過去7日ローリング乖離収束テスト (|Mkt - Fair| >= 3¢) ---")
        for lbl, sub in [("ALL", df_fv), ("Buy No(割高売り)", df_fv[df_fv["side"]=="Buy No"]), ("Buy Yes(割安買い)", df_fv[df_fv["side"]=="Buy Yes"])]:
            if sub.empty:
                continue
            print(f"  [{lbl}] n={len(sub)}")
            for h in [3, 6, 12, 24]:
                v_c = sub[f"pnl_{h}h_cents"].values
                v_r = sub[f"roi_{h}h_pct"].values
                _, p_v = stats.ttest_1samp(v_c, 0.0) if len(v_c) >= 5 else (0.0, 1.0)
                print(f"    {h:2d}h: mean_pnl={v_c.mean():+.2f}¢ (net1c={(v_c-1.0).mean():+.2f}¢) | ROI={v_r.mean():+.2f}% | win={(v_c>0).mean()*100:5.1f}% | p={p_v:.4f}")

    print("\n生成チャート:")
    for p in charts:
        print(f"  - {p} ({p.stat().st_size:,} bytes)")


def main() -> int:
    parser = argparse.ArgumentParser(description="チャート急変＆RV理論価格 → Polymarket 実践エッジ検証")
    parser.add_argument("--refresh", action="store_true", help="キャッシュ無視で再取得")
    args = parser.parse_args()

    res = run_latency_and_option_research(refresh=args.refresh)
    charts = generate_latency_charts(res)
    save_and_print_latency_summary(res, charts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
