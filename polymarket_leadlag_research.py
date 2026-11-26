"""Polymarket × 暗号資産・マクロ相場 連動性＆先行性（リード・ラグ）徹底定量検証スクリプト

目的:
  Polymarket の高流動性マーケット（FOMC金利・地政学・Tech/AI・暗号資産固有）の確率推移が、
  BTC / ETH / SOL / RENDER / NEAR / PAXG(Gold) の価格・ボラティリティとどう連動し、
  本当に「先行指標（トレード可能なエッジ）」になり得るかを、
  1時間足(1h)・15分足(15m)の厳密なタイムスタンプ整合データで統計検証する。

重要仕様:
  - タイムスタンプ整合: Binance kline は bar_close_time = open_time + bar_delta を時刻Tとし、
    Polymarket CLOB は label='right', closed='right' で「T時点までに観測された最新確率」に整列する。
    (1本ズレによる擬似先行バイアスと先読みを完全排除)
  - 内生性分離: 「BTC価格到達系」等の内生イベントと、FOMC・地政学・Tech等の外生イベントを厳密に分離。
  - 多重検定補正: Benjamini-Hochberg FDR q値を算出し、偶然の偽陽性を制御。
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
from scipy import stats
from statsmodels.stats.multitest import multipletests
from statsmodels.tsa.stattools import grangercausalitytests

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "Data" / "polymarket_research"
CLOB_CACHE_DIR = DATA_DIR / "clob_cache"
BINANCE_CACHE_DIR = DATA_DIR / "binance_cache"
PLOTS_DIR = BASE_DIR / "Data" / "plots" / "polymarket_research"
ARTIFACT_DIR = Path(
    r"C:\Users\user\.gemini\antigravity\brain\6d94feb6-68be-4afe-a6c4-9d1afc68db67"
)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "RENDERUSDT", "NEARUSDT", "PAXGUSDT"]
SHORT_SYM = {s: s.replace("USDT", "") for s in SYMBOLS}
PLACEHOLDER_PREFIXES = ("Candidate ", "Team ", "Company ", "Player ", "Country ")

SKIP_TAGS = {
    "sports", "esports", "games", "nba", "nba-finals", "nfl", "nfl-all", "football",
    "basketball", "soccer", "tennis", "baseball", "mlb", "nhl", "hockey", "mma", "ufc",
    "boxing", "golf", "f1", "formula-1", "grand-prix", "cricket", "cbb", "cfb", "ncaa",
    "dota-2", "cs2", "counter-strike-2", "league-of-legends", "valorant",
    "champions-league", "ucl", "epl", "laliga", "la-liga", "mls", "superbowl", "fifa-world-cup",
    "tweet-markets", "tweets-markets", "music", "movies", "tv", "gaming", "video-games",
    "gta-vi", "awards", "weather", "chess", "poker", "poker-game", "eurovision",
    "celebrities", "pop-culture", "stranger-things", "time-poty",
}


def configure_japanese_font() -> None:
    """Windows / Ubuntu VPS の両環境で日本語フォントを設定"""
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


def parse_json_list(val: Any) -> List[Any]:
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return val if isinstance(val, list) else []


def is_placeholder_name(name: str) -> bool:
    s = (name or "").strip()
    if not s or s == "Other":
        return True
    return any(s.startswith(p) and len(s) <= len(p) + 2 for p in PLACEHOLDER_PREFIXES)


def shorten_text(text: str, max_len: int) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "…"


def http_get_json(url: str, params: Dict[str, Any], timeout: int = 20, retries: int = 4) -> Any:
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code == 429:
                wait_s = float(resp.headers.get("Retry-After", 1.5 * (attempt + 1)))
                time.sleep(wait_s)
                continue
            if resp.status_code == 400:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(1.0 * (attempt + 1))
    return None


def infer_direction_sign(title: str, submarket_label: str, group: str) -> int:
    """相場（リスク資産 BTC等）に対する確率上昇時の理論方向性をルールベース判定 (+1=RiskOn, -1=RiskOff, 0=不定)"""
    t = f"{title} || {submarket_label}".lower()

    if group == "FOMC/金利":
        if "decrease" in submarket_label.lower() or "cut" in submarket_label.lower():
            return 1
        if "no change" in submarket_label.lower() or "increase" in submarket_label.lower() or "hike" in submarket_label.lower():
            return -1
        if "fed chair" in title.lower():
            # ハト派/トランプ寄り候補等で一律でないため0
            return 0
        return 0

    if group == "地政学":
        if any(k in t for k in ["ceasefire", "peace deal", "conflict ends", "end of iranian blockade"]):
            return 1
        if any(k in t for k in ["strikes", "enter iran", "invade", "military engagement", "crude oil", "oil"]):
            return -1
        return 0

    if group == "暗号資産固有(内生)":
        if "microstrategy sells" in t or "sells any bitcoin" in t:
            return -1
        if "↓" in submarket_label or "below" in submarket_label.lower():
            return -1
        if "↑" in submarket_label or "above" in submarket_label.lower():
            return 1
        return 1

    if group == "Tech/AI・マクロ":
        if any(k in t for k in ["recession", "bankruptcy", "bubble burst", "tariff", "shutdown", "banned"]):
            return -1
        return 0

    return 0


def classify_event_group(event: Dict[str, Any]) -> Optional[str]:
    """イベントを 4つの研究グループに分類（スポーツ・エンタメ等は除外）"""
    tags = {str(t.get("slug", "")).lower() for t in (event.get("tags") or []) if isinstance(t, dict)}
    title = str(event.get("title") or "")
    title_l = title.lower()

    if tags & SKIP_TAGS:
        # ただし pop-culture と tech が併記されるケースはタイトルで精査
        if not ({"tech", "ai", "nvidia", "tiktok"} & tags and "affair" not in title_l and "searched person" not in title_l):
            return None
    if any(bad in title_l for bad in ["tweets", "elon musk #", "wear a suit", "stranger things", "aliens exist", "affair", "searched person", "mrbeast"]):
        return None
    if any(bad in title_l for bad in ["math problem", "millennium prize", "rental price", "b200 average", "h100 average", "b300 average"]):
        return None

    end_date = str(event.get("endDate") or "")
    if end_date and end_date[:10] < "2025-01-01":
        return None

    if "fed decision in" in title_l or "fed chair" in title_l:
        return "FOMC/金利"

    if (tags & {"geopolitics", "iran", "middle-east", "ukraine-peace-deal", "diplomacy-ceasefire", "hormuz"}) or "iran" in title_l or "ceasefire" in title_l:
        # 単なる地方選挙等を除く
        if not (tags & {"elections", "main-election", "world-elections", "global-elections"}):
            return "地政学"

    if "microstrategy" in title_l or ("bitcoin" in title_l and ("hit" in title_l or "above" in title_l)):
        if "daily" not in tags and "today" not in tags and "multi-strikes" not in tags:
            return "暗号資産固有(内生)"

    if (tags & {"tech", "ai", "nvidia", "anthropic", "openai", "gov-shutdown", "recession", "tariffs", "trade-war"}) or "nvidia ceo" in title_l or "tiktok banned" in title_l or "shutdown" in title_l or "ai bubble" in title_l:
        if "math" not in tags and "science" not in tags:
            return "Tech/AI・マクロ"

    return None


def fetch_daily_history(token_id: str) -> List[Tuple[int, float]]:
    data = http_get_json(CLOB_HISTORY_URL, {"market": token_id, "interval": "max", "fidelity": 1440}, timeout=15)
    if not data or not isinstance(data, dict):
        return []
    raw = data.get("history") or []
    pts: List[Tuple[int, float]] = []
    for item in raw:
        try:
            pts.append((int(item["t"]), float(item["p"])))
        except Exception:
            continue
    return sorted(pts, key=lambda x: x[0])


def choose_best_submarket(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """イベント内の上位出来高サブマーケットから、張り付きでない活発に動いた本命・争点マーケットを1つ選定"""
    raw_markets = event.get("markets") or []
    if not raw_markets:
        return None

    sorted_markets = sorted(raw_markets, key=lambda m: float(m.get("volume") or 0.0), reverse=True)
    candidates: List[Dict[str, Any]] = []

    for m in sorted_markets[:8]:
        vol_m = float(m.get("volume") or 0.0)
        if vol_m < 100_000:
            continue
        name = (m.get("groupItemTitle") or m.get("question") or "").strip()
        if is_placeholder_name(name):
            continue

        outs = parse_json_list(m.get("outcomes"))
        tids = parse_json_list(m.get("clobTokenIds"))
        if not outs or not tids:
            continue

        if outs == ["Yes", "No"]:
            tid = str(tids[0])
            label = name
        else:
            tid = str(tids[0])
            label = f"{name} ({outs[0]})" if len(raw_markets) > 1 else str(outs[0])

        daily = fetch_daily_history(tid)
        time.sleep(0.05)
        if len(daily) < 10:
            continue

        ps = np.array([p for _, p in daily], dtype=float)
        p_range = float(ps.max() - ps.min())
        p_std = float(ps.std())
        t_first, t_last = daily[0][0], daily[-1][0]
        if (t_last - t_first) < 5 * 86400:
            continue

        candidates.append({
            "token_id": tid,
            "submarket_label": label,
            "submarket_volume": vol_m,
            "daily_points": len(daily),
            "p_range": p_range,
            "p_std": p_std,
            "t_first": t_first,
            "t_last": t_last,
        })

    if not candidates:
        return None

    # 確率レンジ >= 0.15 かつ std >= 0.04 を満たす活発な候補を優先し、その中で出来高最大を選ぶ
    active_moving = [c for c in candidates if c["p_range"] >= 0.15 and c["p_std"] >= 0.04]
    if active_moving:
        active_moving.sort(key=lambda c: c["submarket_volume"], reverse=True)
        return active_moving[0]

    # フォールバック: レンジ >= 0.10 の中で出来高最大
    fallback = [c for c in candidates if c["p_range"] >= 0.10]
    if fallback:
        fallback.sort(key=lambda c: c["submarket_volume"], reverse=True)
        return fallback[0]

    return None


def select_research_events(refresh: bool = False) -> List[Dict[str, Any]]:
    """4グループからバランス良く計18〜22イベントを選出（キャッシュ対応）"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sel_cache = DATA_DIR / "selected_events.json"
    if sel_cache.exists() and not refresh:
        with sel_cache.open("r", encoding="utf-8") as f:
            cached = json.load(f)
        if isinstance(cached, list) and len(cached) >= 12:
            print(f"[Cache] 選定済みイベント {len(cached)} 件を読み込みました: {sel_cache}")
            return cached

    print("[Gamma API] アクティブ＆決着済みイベント候補を取得中...")
    active_evs = http_get_json(GAMMA_EVENTS_URL, {"active": "true", "closed": "false", "order": "volume24hr", "ascending": "false", "limit": 100}) or []
    closed_evs = http_get_json(GAMMA_EVENTS_URL, {"closed": "true", "order": "volume", "ascending": "false", "limit": 150}) or []

    # 重複除去しつつ統合
    seen_slugs = set()
    pool_by_group: Dict[str, List[Dict[str, Any]]] = {
        "FOMC/金利": [],
        "地政学": [],
        "Tech/AI・マクロ": [],
        "暗号資産固有(内生)": [],
    }
    quotas = {
        "FOMC/金利": 7,
        "地政学": 5,
        "Tech/AI・マクロ": 4,
        "暗号資産固有(内生)": 4,
    }

    # アクティブ案件を先頭に、次に決着済みの高出来高案件を評価
    combined = [(True, e) for e in active_evs] + [(False, e) for e in closed_evs]
    for is_active, ev in combined:
        slug = str(ev.get("slug") or "")
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)

        grp = classify_event_group(ev)
        if not grp:
            continue

        vol_total = float(ev.get("volume") or 0.0)
        vol24 = float(ev.get("volume24hr") or 0.0)
        if is_active and vol_total < 1_000_000 and vol24 < 100_000:
            continue
        if (not is_active) and vol_total < 20_000_000:
            continue

        pool_by_group[grp].append({**ev, "_is_active": is_active})

    selected: List[Dict[str, Any]] = []
    for grp, quota in quotas.items():
        # アクティブ最上位1-2件を優先し、残りを総出来高順で選定
        cands = pool_by_group[grp]
        act_cands = sorted([c for c in cands if c["_is_active"]], key=lambda x: float(x.get("volume24hr") or 0), reverse=True)
        cls_cands = sorted([c for c in cands if not c["_is_active"]], key=lambda x: float(x.get("volume") or 0), reverse=True)
        ordered = act_cands[:2] + cls_cands + act_cands[2:]

        picked = 0
        for ev in ordered:
            if picked >= quota:
                break
            sub = choose_best_submarket(ev)
            if not sub:
                continue
            title = str(ev.get("title") or "")
            dir_sign = infer_direction_sign(title, sub["submarket_label"], grp)
            rec = {
                "slug": str(ev.get("slug")),
                "title": title,
                "group": grp,
                "is_endogenous": (grp == "暗号資産固有(内生)"),
                "is_active": bool(ev["_is_active"]),
                "event_volume": float(ev.get("volume") or 0.0),
                "volume24hr": float(ev.get("volume24hr") or 0.0),
                "end_date": str(ev.get("endDate") or "")[:10],
                "direction_sign": dir_sign,
                **sub,
            }
            selected.append(rec)
            picked += 1
            print(
                f"  + [{grp}] {shorten_text(title, 42)} | sub={shorten_text(sub['submarket_label'], 22)} "
                f"| range={sub['p_range']*100:.0f}pt | dir={dir_sign:+d} | vol=${rec['event_volume']/1e6:.1f}M"
            )

    with sel_cache.open("w", encoding="utf-8") as f:
        json.dump(selected, f, ensure_ascii=False, indent=2)
    return selected


def fetch_clob_chunked(
    token_id: str,
    start_ts: int,
    end_ts: int,
    fidelity: int,
    chunk_days: int,
    refresh: bool = False,
) -> pd.Series:
    """CLOB API の14日上限を回避するため chunk_days 単位で分割取得し、UTC Timestamp->prob Series を返す"""
    CLOB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CLOB_CACHE_DIR / f"{token_id[:24]}_f{fidelity}_{start_ts}_{end_ts}.csv"
    if cache_file.exists() and not refresh:
        df = pd.read_csv(cache_file, encoding="utf-8")
        if not df.empty:
            idx = pd.to_datetime(df["t"], unit="s", utc=True)
            return pd.Series(df["p"].values, index=idx, dtype=float).sort_index()

    chunk_sec = chunk_days * 86400
    all_pts: Dict[int, float] = {}
    cur = start_ts
    while cur < end_ts:
        nxt = min(cur + chunk_sec, end_ts)
        data = http_get_json(
            CLOB_HISTORY_URL,
            {"market": token_id, "startTs": int(cur), "endTs": int(nxt), "fidelity": int(fidelity)},
            timeout=15,
        )
        if data and isinstance(data, dict):
            for pt in data.get("history") or []:
                try:
                    t_s = int(pt["t"])
                    p_v = float(pt["p"])
                    if 0.0 <= p_v <= 1.0:
                        all_pts[t_s] = p_v
                except Exception:
                    continue
        cur = nxt
        time.sleep(0.10)

    if not all_pts:
        return pd.Series(dtype=float)

    items = sorted(all_pts.items(), key=lambda x: x[0])
    df_out = pd.DataFrame(items, columns=["t", "p"])
    df_out.to_csv(cache_file, index=False, encoding="utf-8")
    idx = pd.to_datetime(df_out["t"], unit="s", utc=True)
    return pd.Series(df_out["p"].values, index=idx, dtype=float).sort_index()


def merge_intervals(intervals: List[Tuple[int, int]], pad_sec: int = 2 * 86400) -> List[Tuple[int, int]]:
    """重複・近接する時間区間をマージして Binance API 取得回数を最小化"""
    if not intervals:
        return []
    padded = sorted([(max(0, a - pad_sec), b + pad_sec) for a, b in intervals])
    merged = [padded[0]]
    for s, e in padded[1:]:
        ps, pe = merged[-1]
        if s <= pe + 86400:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return merged


def fetch_binance_klines_intervals(
    symbol: str,
    interval: str,
    intervals: List[Tuple[int, int]],
    refresh: bool = False,
) -> pd.DataFrame:
    """Binance Spot API から指定区間群の klines を取得し、厳密な bar_close_time インデックスで返す"""
    BINANCE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = BINANCE_CACHE_DIR / f"{symbol}_{interval}.csv"

    bar_sec = 3600 if interval == "1h" else 900
    now_sec = int(time.time())
    merged_iv = merge_intervals([(s, min(e, now_sec)) for s, e in intervals])

    existing_df = pd.DataFrame()
    if cache_file.exists() and not refresh:
        existing_df = pd.read_csv(cache_file, encoding="utf-8")
        if not existing_df.empty:
            existing_df["close_ts"] = existing_df["close_ts"].astype(int)

    existing_ts = set(existing_df["close_ts"].tolist()) if not existing_df.empty else set()
    rows_map: Dict[int, Tuple[int, float, float, float, float, float]] = {}
    if not existing_df.empty:
        for r in existing_df.itertuples(index=False):
            rows_map[int(r.close_ts)] = (int(r.open_ts), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume))

    need_fetch = False
    for s_sec, e_sec in merged_iv:
        # 始点と終点のカバレッジ確認
        probe_s = ((s_sec // bar_sec) + 2) * bar_sec
        probe_e = ((e_sec // bar_sec) - 2) * bar_sec
        if probe_s not in existing_ts or probe_e not in existing_ts:
            need_fetch = True
            break

    if need_fetch or refresh:
        print(f"  [Binance] {symbol} ({interval}) 取得中...")
        for s_sec, e_sec in merged_iv:
            cur_ms = int(s_sec * 1000)
            end_ms = int(e_sec * 1000)
            while cur_ms < end_ms:
                data = http_get_json(
                    BINANCE_KLINES_URL,
                    {"symbol": symbol, "interval": interval, "startTime": cur_ms, "endTime": end_ms, "limit": 1000},
                    timeout=15,
                )
                if not data or not isinstance(data, list):
                    break
                last_open_ms = cur_ms
                for k in data:
                    open_ms = int(k[0])
                    kline_close_ms = int(k[6])
                    open_sec = open_ms // 1000
                    bar_close_sec = open_sec + bar_sec
                    # 厳密整合チェック: kline close_time (ms) == bar_close_sec*1000 - 1
                    assert kline_close_ms == bar_close_sec * 1000 - 1, (
                        f"Timestamp mismatch for {symbol} {interval}: open={open_ms}, close={kline_close_ms}"
                    )
                    rows_map[bar_close_sec] = (
                        open_sec,
                        float(k[1]),
                        float(k[2]),
                        float(k[3]),
                        float(k[4]),
                        float(k[5]),
                    )
                    last_open_ms = open_ms
                if len(data) < 1000:
                    break
                cur_ms = last_open_ms + bar_sec * 1000
                time.sleep(0.06)

        if rows_map:
            recs = [
                {"close_ts": cts, "open_ts": v[0], "open": v[1], "high": v[2], "low": v[3], "close": v[4], "volume": v[5]}
                for cts, v in sorted(rows_map.items())
            ]
            out_df = pd.DataFrame(recs)
            out_df.to_csv(cache_file, index=False, encoding="utf-8")
        else:
            out_df = pd.DataFrame(columns=["close_ts", "open_ts", "open", "high", "low", "close", "volume"])
    else:
        out_df = existing_df.sort_values("close_ts").reset_index(drop=True)

    if out_df.empty:
        return out_df

    out_df["bar_close_dt"] = pd.to_datetime(out_df["close_ts"], unit="s", utc=True)
    out_df = out_df.set_index("bar_close_dt").sort_index()
    # 自己整合チェック: close_ts - open_ts == bar_sec
    assert ((out_df["close_ts"] - out_df["open_ts"]) == bar_sec).all(), "bar_close_time 整合エラー"
    return out_df


def align_polymarket_and_crypto(
    poly_raw: pd.Series,
    crypto_dfs: Dict[str, pd.DataFrame],
    freq: str,
) -> pd.DataFrame:
    """Polymarket時系列を 'T時点までに観測された最新確率' として厳密整列し、対数リターンと結合"""
    if poly_raw.empty or "BTCUSDT" not in crypto_dfs or crypto_dfs["BTCUSDT"].empty:
        return pd.DataFrame()

    # T時点の足に (T-delta, T] 内の最後の約定/スナップショットを割り当てる (label="right", closed="right")
    p_res = poly_raw.resample(freq, label="right", closed="right").last()
    # 先読み禁止: ffill(limit=2) のみ許可、bfill は絶対に使わない
    p_res = p_res.ffill(limit=2)

    btc_df = crypto_dfs["BTCUSDT"]
    # Polymarket 観測範囲に重なる Binance bar_close_dt インデックスへ厳密 reindex
    t_min, t_max = p_res.dropna().index.min(), p_res.dropna().index.max()
    if pd.isna(t_min) or pd.isna(t_max):
        return pd.DataFrame()

    target_idx = btc_df.loc[t_min:t_max].index
    if len(target_idx) < 30:
        return pd.DataFrame()

    p_aligned = p_res.reindex(target_idx).ffill(limit=2)
    df = pd.DataFrame(index=target_idx)
    df["prob"] = p_aligned
    df["dp"] = df["prob"].diff() * 100.0  # pt単位

    p_clip = df["prob"].clip(0.005, 0.995)
    df["dlogit"] = np.log(p_clip / (1.0 - p_clip)).diff()

    for sym, cdf in crypto_dfs.items():
        short = SHORT_SYM[sym]
        c_sub = cdf["close"].reindex(target_idx)
        df[f"close_{short}"] = c_sub
        df[f"ret_{short}"] = np.log(c_sub / c_sub.shift(1)) * 100.0  # %単位

    df = df.dropna(subset=["prob", "dp", "ret_BTC"])
    return df


def compute_ccf_series(
    x: pd.Series,
    y: pd.Series,
    max_lag: int,
) -> Dict[int, Tuple[float, float, int]]:
    """
    lag = +k (k>0): Corr(x[t], y[t+k]) -> x が y に k期先行 (Polymarket Leads)
    lag = 0       : Corr(x[t], y[t])   -> 同時反応
    lag = -k (k<0): Corr(x[t], y[t+k]) -> y が x に |k|期先行 (Crypto Leads)
    """
    out: Dict[int, Tuple[float, float, int]] = {}
    for lag in range(-max_lag, max_lag + 1):
        # y.shift(-lag) は時刻 t に対して y[t + lag] を配置する
        joined = pd.concat([x, y.shift(-lag)], axis=1).dropna()
        n = len(joined)
        if n < 30 or joined.iloc[:, 0].std() == 0 or joined.iloc[:, 1].std() == 0:
            out[lag] = (np.nan, np.nan, n)
            continue
        r_val, p_val = stats.pearsonr(joined.iloc[:, 0].values, joined.iloc[:, 1].values)
        out[lag] = (float(r_val), float(p_val), int(n))
    return out


def safe_granger_min_p(target: pd.Series, predictor: pd.Series, maxlag: int) -> Tuple[float, Optional[int]]:
    """predictor -> target の Granger因果性検定（最小p値と対応ラグを返す）"""
    df_g = pd.concat([target, predictor], axis=1).dropna()
    if len(df_g) < maxlag * 5 + 20 or df_g.iloc[:, 0].std() == 0 or df_g.iloc[:, 1].std() == 0:
        return 1.0, None
    try:
        res = grangercausalitytests(df_g.values, maxlag=maxlag, verbose=False)
        best_p = 1.0
        best_lag = None
        for lag_k, dct in res.items():
            p_val = float(dct[0]["ssr_ftest"][1])
            if p_val < best_p:
                best_p = p_val
                best_lag = int(lag_k)
        return best_p, best_lag
    except Exception:
        return 1.0, None


def detect_causal_shocks(
    dp: pd.Series,
    min_abs_dp: float,
    roll_window: int,
    sigma_mult: float,
    cooldown_bars: int,
) -> List[pd.Timestamp]:
    """完全因果的（過去窓のみ使用・.shift(1)）な確率急変ショック検出"""
    if len(dp) < 40:
        return []
    # .shift(1) により現在バーのショックが自身のローリング標準偏差を膨らませない
    roll_std = dp.rolling(window=roll_window, min_periods=24).std().shift(1)
    # 初期立ち上がり等で過度に小さい場合は min_abs_dp で下限ガード
    thresh = np.maximum(min_abs_dp, sigma_mult * roll_std.fillna(dp.expanding(min_periods=12).std().shift(1)))
    is_shock = dp.abs() >= thresh

    shocks: List[pd.Timestamp] = []
    last_idx = -cooldown_bars - 1
    idx_list = dp.index.tolist()
    for i, ts in enumerate(idx_list):
        if bool(is_shock.iloc[i]) and (i - last_idx) > cooldown_bars:
            shocks.append(ts)
            last_idx = i
    return shocks


def classify_session_utc(ts: pd.Timestamp) -> str:
    """UTC時刻から 米国市場時間 / 平日時間外 / 週末(土日) に分類"""
    if ts.dayofweek >= 5:
        return "週末(土日UTC)"
    # 米国株式通常セッション (13:30 - 21:00 UTC 概算)
    t_mins = ts.hour * 60 + ts.minute
    if 13 * 60 + 30 <= t_mins <= 21 * 60:
        return "米国市場時間"
    return "平日時間外"


def run_analysis(events: List[Dict[str, Any]], refresh: bool = False) -> Dict[str, Any]:
    """4本柱すべての定量検証を実行"""
    now_ts = int(time.time())
    # 1) Polymarket CLOB 1h / 15m 取得
    print("\n[Step 1] Polymarket CLOB 1h / 15m チャンク履歴を取得中...")
    poly_1h_map: Dict[str, pd.Series] = {}
    poly_15m_map: Dict[str, pd.Series] = {}
    intervals_1h: List[Tuple[int, int]] = []
    intervals_15m: List[Tuple[int, int]] = []

    for ev in events:
        slug = ev["slug"]
        t_first = int(ev["t_first"])
        t_last = min(int(ev["t_last"]), now_ts)
        s_1h = max(t_first, t_last - 60 * 86400)
        s_15m = max(t_first, t_last - 28 * 86400)

        ser_1h = fetch_clob_chunked(ev["token_id"], s_1h, t_last, fidelity=60, chunk_days=12, refresh=refresh)
        ser_15m = fetch_clob_chunked(ev["token_id"], s_15m, t_last, fidelity=15, chunk_days=10, refresh=refresh)
        if len(ser_1h) >= 40:
            poly_1h_map[slug] = ser_1h
            intervals_1h.append((s_1h, t_last))
        if len(ser_15m) >= 80:
            poly_15m_map[slug] = ser_15m
            intervals_15m.append((s_15m, t_last))
        print(f"  {shorten_text(ev['title'], 38):38s} -> 1h:{len(ser_1h):4d}pts, 15m:{len(ser_15m):4d}pts")

    # 2) Binance 6銘柄 1h / 15m 取得
    print("\n[Step 2] Binance Spot 6銘柄 (1h / 15m) を取得・整合中...")
    crypto_1h: Dict[str, pd.DataFrame] = {}
    crypto_15m: Dict[str, pd.DataFrame] = {}
    for sym in SYMBOLS:
        crypto_1h[sym] = fetch_binance_klines_intervals(sym, "1h", intervals_1h, refresh=refresh)
        crypto_15m[sym] = fetch_binance_klines_intervals(sym, "15m", intervals_15m, refresh=refresh)
        print(f"  {sym:10s} -> 1h:{len(crypto_1h[sym]):5d} bars, 15m:{len(crypto_15m[sym]):5d} bars")

    # 3) 整列データフレーム生成 & 検証 A/B/C/D 実行
    print("\n[Step 3] 厳密タイムスタンプ整合＆4本柱定量検証を実行中...")
    aligned_1h: Dict[str, pd.DataFrame] = {}
    aligned_15m: Dict[str, pd.DataFrame] = {}
    valid_events: List[Dict[str, Any]] = []

    for ev in events:
        slug = ev["slug"]
        df1h = align_polymarket_and_crypto(poly_1h_map.get(slug, pd.Series(dtype=float)), crypto_1h, "1h")
        df15m = align_polymarket_and_crypto(poly_15m_map.get(slug, pd.Series(dtype=float)), crypto_15m, "15min")
        if len(df1h) < 48:
            continue
        aligned_1h[slug] = df1h
        if len(df15m) >= 96:
            aligned_15m[slug] = df15m
        valid_events.append(ev)

    # --- 検証 A & B: 1h / 15m CCF (方向・ボラ) & 双方向 Granger ---
    ccf_1h_records: List[Dict[str, Any]] = []
    ccf_1h_vol_records: List[Dict[str, Any]] = []
    ccf_15m_records: List[Dict[str, Any]] = []
    granger_records: List[Dict[str, Any]] = []

    for ev in valid_events:
        slug = ev["slug"]
        df1h = aligned_1h[slug]
        dir_sign = ev["direction_sign"]

        # 方向一致符号（direction_sign==0のときはそのまま生dpを使い、絶対値・ボラと個別符号の両方確認）
        eff_dp = df1h["dp"] * (dir_sign if dir_sign != 0 else 1)

        # 全6銘柄との1h CCF
        for sym in SYMBOLS:
            short = SHORT_SYM[sym]
            ret_col = f"ret_{short}"
            ccf_dir = compute_ccf_series(eff_dp, df1h[ret_col], max_lag=12)
            ccf_vol = compute_ccf_series(df1h["dp"].abs(), df1h[ret_col].abs(), max_lag=12)
            for lag, (r_val, p_val, n_obs) in ccf_dir.items():
                ccf_1h_records.append({
                    "slug": slug, "title": ev["title"], "group": ev["group"],
                    "is_endogenous": ev["is_endogenous"], "direction_sign": dir_sign,
                    "asset": short, "lag": lag, "corr": r_val, "p_val": p_val, "n": n_obs,
                })
            for lag, (r_val, p_val, n_obs) in ccf_vol.items():
                ccf_1h_vol_records.append({
                    "slug": slug, "title": ev["title"], "group": ev["group"],
                    "is_endogenous": ev["is_endogenous"],
                    "asset": short, "lag": lag, "corr": r_val, "p_val": p_val, "n": n_obs,
                })

        # 1h Granger vs BTC
        p_poly_to_btc, lag_p2b = safe_granger_min_p(df1h["ret_BTC"], df1h["dp"], maxlag=6)
        p_btc_to_poly, lag_b2p = safe_granger_min_p(df1h["dp"], df1h["ret_BTC"], maxlag=6)
        # 同時相関 lag=0
        sim_r, sim_p, sim_n = compute_ccf_series(eff_dp, df1h["ret_BTC"], max_lag=0)[0]

        # 15m CCF & Granger vs BTC
        p_poly_to_btc_15m, p_btc_to_poly_15m = np.nan, np.nan
        if slug in aligned_15m:
            df15 = aligned_15m[slug]
            eff_dp15 = df15["dp"] * (dir_sign if dir_sign != 0 else 1)
            ccf_15 = compute_ccf_series(eff_dp15, df15["ret_BTC"], max_lag=16)
            for lag, (r_val, p_val, n_obs) in ccf_15.items():
                ccf_15m_records.append({
                    "slug": slug, "title": ev["title"], "group": ev["group"],
                    "is_endogenous": ev["is_endogenous"], "direction_sign": dir_sign,
                    "asset": "BTC", "lag": lag, "corr": r_val, "p_val": p_val, "n": n_obs,
                })
            p_poly_to_btc_15m, _ = safe_granger_min_p(df15["ret_BTC"], df15["dp"], maxlag=8)
            p_btc_to_poly_15m, _ = safe_granger_min_p(df15["dp"], df15["ret_BTC"], maxlag=8)

        granger_records.append({
            "slug": slug, "title": ev["title"], "group": ev["group"],
            "is_endogenous": ev["is_endogenous"], "direction_sign": dir_sign,
            "n_1h": len(df1h), "n_15m": len(aligned_15m.get(slug, [])),
            "sim_r_1h": sim_r, "sim_p_1h": sim_p,
            "p_poly_to_btc_1h": p_poly_to_btc, "lag_p2b_1h": lag_p2b,
            "p_btc_to_poly_1h": p_btc_to_poly, "lag_b2p_1h": lag_b2p,
            "p_poly_to_btc_15m": p_poly_to_btc_15m,
            "p_btc_to_poly_15m": p_btc_to_poly_15m,
        })

    df_ccf_1h = pd.DataFrame(ccf_1h_records)
    df_ccf_1h_vol = pd.DataFrame(ccf_1h_vol_records)
    df_ccf_15m = pd.DataFrame(ccf_15m_records)
    df_granger = pd.DataFrame(granger_records)

    # BH-FDR 多重検定補正
    for df_c in [df_ccf_1h, df_ccf_1h_vol, df_ccf_15m]:
        if not df_c.empty:
            valid_mask = df_c["p_val"].notna()
            df_c["q_val"] = np.nan
            if valid_mask.sum() > 0:
                _, q_vals, _, _ = multipletests(df_c.loc[valid_mask, "p_val"].values, method="fdr_bh")
                df_c.loc[valid_mask, "q_val"] = q_vals

    # Granger 5分類判定 (BH-FDR補正 on 1h p-values)
    if not df_granger.empty:
        _, q_p2b, _, _ = multipletests(df_granger["p_poly_to_btc_1h"].fillna(1.0).values, method="fdr_bh")
        _, q_b2p, _, _ = multipletests(df_granger["p_btc_to_poly_1h"].fillna(1.0).values, method="fdr_bh")
        _, q_sim, _, _ = multipletests(df_granger["sim_p_1h"].fillna(1.0).values, method="fdr_bh")
        df_granger["q_poly_to_btc_1h"] = q_p2b
        df_granger["q_btc_to_poly_1h"] = q_b2p
        df_granger["q_sim_1h"] = q_sim

        cats = []
        for row in df_granger.itertuples(index=False):
            p_lead = row.q_poly_to_btc_1h < 0.05
            c_lead = row.q_btc_to_poly_1h < 0.05
            sim_sig = row.q_sim_1h < 0.05
            if p_lead and c_lead:
                cats.append("Bidirectional")
            elif p_lead:
                cats.append("Polymarket Leads")
            elif c_lead:
                cats.append("Crypto Leads")
            elif sim_sig:
                cats.append("Simultaneous Only")
            else:
                cats.append("No Significant Link")
        df_granger["classification"] = cats

    # --- 検証 C: イベントスタディ（1h ショック前後 t-6..t+24） ---
    shock_events_1h: List[Dict[str, Any]] = []
    shock_windows_1h: List[Dict[str, Any]] = []
    shock_windows_15m: List[Dict[str, Any]] = []

    for ev in valid_events:
        slug = ev["slug"]
        df1h = aligned_1h[slug]
        dir_sign = ev["direction_sign"]
        shocks_ts = detect_causal_shocks(df1h["dp"], min_abs_dp=3.0, roll_window=168, sigma_mult=3.0, cooldown_bars=6)

        # 平常時ボラティリティ基準（イベント期間全体の平均 |ret|）
        base_abs_ret = {SHORT_SYM[s]: float(df1h[f"ret_{SHORT_SYM[s]}"].abs().mean()) for s in SYMBOLS}

        for ts in shocks_ts:
            pos = df1h.index.get_loc(ts)
            if pos < 6 or pos + 24 >= len(df1h):
                continue
            dp_val = float(df1h["dp"].iloc[pos])
            # 方向一致乗数: direction_sign != 0 のときのみ方向リターン算出
            align_mult = np.sign(dp_val) * dir_sign if dir_sign != 0 else np.nan
            session = classify_session_utc(ts)

            win_rec: Dict[str, Any] = {
                "slug": slug, "title": ev["title"], "group": ev["group"],
                "is_endogenous": ev["is_endogenous"], "direction_sign": dir_sign,
                "shock_ts": ts.isoformat(), "dp_pt": dp_val, "abs_dp_pt": abs(dp_val),
                "session": session, "align_mult": align_mult,
            }

            # 各銘柄の Pre(t-4..t-1), Sim(t=0), Post(t+1..t+1,+3,+6,+12,+24) 累積リターン・ボラ倍率
            for sym in SYMBOLS:
                short = SHORT_SYM[sym]
                rets = df1h[f"ret_{short}"].iloc[pos - 6 : pos + 25].values
                if len(rets) != 31 or np.isnan(rets).any():
                    continue
                b_abs = max(base_abs_ret[short], 1e-6)
                raw_pre4 = float(rets[2:6].sum())     # t-4..t-1
                raw_sim = float(rets[6])            # t=0
                raw_post1 = float(rets[7])          # t+1
                raw_post3 = float(rets[7:10].sum()) # t+1..t+3
                raw_post6 = float(rets[7:13].sum()) # t+1..t+6
                raw_post12 = float(rets[7:19].sum())# t+1..t+12
                raw_post24 = float(rets[7:31].sum())# t+1..t+24

                win_rec[f"{short}_sim_abs_ratio"] = abs(raw_sim) / b_abs
                win_rec[f"{short}_post6_abs_ratio"] = float(np.abs(rets[7:13]).mean()) / b_abs
                win_rec[f"{short}_raw_sim"] = raw_sim
                win_rec[f"{short}_raw_post6"] = raw_post6

                if not np.isnan(align_mult):
                    win_rec[f"{short}_dir_pre4"] = align_mult * raw_pre4
                    win_rec[f"{short}_dir_sim"] = align_mult * raw_sim
                    win_rec[f"{short}_dir_post1"] = align_mult * raw_post1
                    win_rec[f"{short}_dir_post3"] = align_mult * raw_post3
                    win_rec[f"{short}_dir_post6"] = align_mult * raw_post6
                    win_rec[f"{short}_dir_post12"] = align_mult * raw_post12
                    win_rec[f"{short}_dir_post24"] = align_mult * raw_post24

                if short == "BTC":
                    # 相対時刻 -6..+24 の軌跡（t=-1 基準で累積、方向付き & ボラ倍率）
                    for k_idx, rel_k in enumerate(range(-6, 25)):
                        shock_windows_1h.append({
                            "slug": slug, "group": ev["group"], "is_endogenous": ev["is_endogenous"],
                            "shock_ts": ts.isoformat(), "session": session, "rel_k": rel_k,
                            "btc_raw_ret": float(rets[k_idx]),
                            "btc_abs_ratio": abs(float(rets[k_idx])) / b_abs,
                            "btc_dir_ret": float(align_mult * rets[k_idx]) if not np.isnan(align_mult) else np.nan,
                        })

            shock_events_1h.append(win_rec)

        # 15m ショック検出 (t-8..t+16 = -2h..+4h)
        if slug in aligned_15m:
            df15 = aligned_15m[slug]
            shocks_15 = detect_causal_shocks(df15["dp"], min_abs_dp=2.0, roll_window=288, sigma_mult=3.0, cooldown_bars=8)
            b_abs15 = max(float(df15["ret_BTC"].abs().mean()), 1e-6)
            for ts in shocks_15:
                pos = df15.index.get_loc(ts)
                if pos < 8 or pos + 16 >= len(df15):
                    continue
                dp_val = float(df15["dp"].iloc[pos])
                align_mult = np.sign(dp_val) * dir_sign if dir_sign != 0 else np.nan
                rets15 = df15["ret_BTC"].iloc[pos - 8 : pos + 17].values
                if len(rets15) != 25 or np.isnan(rets15).any():
                    continue
                session = classify_session_utc(ts)
                for k_idx, rel_k in enumerate(range(-8, 17)):
                    shock_windows_15m.append({
                        "slug": slug, "group": ev["group"], "is_endogenous": ev["is_endogenous"],
                        "shock_ts": ts.isoformat(), "session": session, "rel_k": rel_k,
                        "btc_abs_ratio": abs(float(rets15[k_idx])) / b_abs15,
                        "btc_dir_ret": float(align_mult * rets15[k_idx]) if not np.isnan(align_mult) else np.nan,
                    })

    df_shocks = pd.DataFrame(shock_events_1h)
    df_win_1h = pd.DataFrame(shock_windows_1h)
    df_win_15m = pd.DataFrame(shock_windows_15m)

    # --- 検証 D-1: 週末/平日時間外 vs 米国市場時間 (15m CCF) ---
    session_ccf_rows: List[Dict[str, Any]] = []
    for ev in valid_events:
        slug = ev["slug"]
        if slug not in aligned_15m:
            continue
        df15 = aligned_15m[slug]
        dir_sign = ev["direction_sign"]
        eff_dp15 = df15["dp"] * (dir_sign if dir_sign != 0 else 1)
        sessions = pd.Series([classify_session_utc(ts) for ts in df15.index], index=df15.index)

        for sess_name in ["米国市場時間", "平日時間外", "週末(土日UTC)"]:
            mask = sessions == sess_name
            for lag in [-2, -1, 0, 1, 2, 4]:
                joined = pd.concat([eff_dp15, df15["ret_BTC"].shift(-lag)], axis=1)
                sub = joined.loc[mask].dropna()
                if len(sub) < 40 or sub.iloc[:, 0].std() == 0 or sub.iloc[:, 1].std() == 0:
                    continue
                r_val, p_val = stats.pearsonr(sub.iloc[:, 0].values, sub.iloc[:, 1].values)
                r_vol, _ = stats.pearsonr(sub.iloc[:, 0].abs().values, sub.iloc[:, 1].abs().values)
                session_ccf_rows.append({
                    "slug": slug, "group": ev["group"], "is_endogenous": ev["is_endogenous"],
                    "session": sess_name, "lag": lag, "corr": float(r_val), "vol_corr": float(r_vol),
                    "p_val": float(p_val), "n": len(sub),
                })
    df_sess_ccf = pd.DataFrame(session_ccf_rows)

    # --- 検証 D-3: 簡易ルール・バックテスト (外生イベント × 方向明確 shocks on BTC) ---
    backtest_summary: List[Dict[str, Any]] = []
    if not df_shocks.empty:
        exo_dir = df_shocks[(~df_shocks["is_endogenous"]) & (df_shocks["BTC_dir_post1"].notna())].copy()
        exo_dir["shock_dt"] = pd.to_datetime(exo_dir["shock_ts"])
        exo_dir = exo_dir.sort_values("shock_dt").reset_index(drop=True)
        n_tot = len(exo_dir)
        mid_dt = exo_dir["shock_dt"].iloc[n_tot // 2] if n_tot >= 4 else None

        for horizon in [1, 3, 6, 12, 24]:
            col = f"BTC_dir_post{horizon}"
            vals = exo_dir[col].dropna().values
            if len(vals) < 5:
                continue
            fee_bps = 0.10  # 往復 0.10% (10bps)
            vals_net = vals - fee_bps
            mean_gross = float(vals.mean())
            mean_net = float(vals_net.mean())
            win_gross = float((vals > 0).mean() * 100.0)
            win_net = float((vals_net > 0).mean() * 100.0)
            t_stat, p_t = stats.ttest_1samp(vals_net, 0.0)
            try:
                _, p_wil = stats.wilcoxon(vals_net)
            except Exception:
                p_wil = 1.0
            pos_sum = float(vals_net[vals_net > 0].sum())
            neg_sum = float(abs(vals_net[vals_net < 0].sum()))
            pf_net = pos_sum / max(neg_sum, 1e-6)
            sharpe_trade = float(vals_net.mean() / max(vals_net.std(ddof=1), 1e-6))

            is_mean, oos_mean = np.nan, np.nan
            if mid_dt is not None:
                is_vals = exo_dir.loc[exo_dir["shock_dt"] < mid_dt, col].dropna().values - fee_bps
                oos_vals = exo_dir.loc[exo_dir["shock_dt"] >= mid_dt, col].dropna().values - fee_bps
                is_mean = float(is_vals.mean()) if len(is_vals) > 0 else np.nan
                oos_mean = float(oos_vals.mean()) if len(oos_vals) > 0 else np.nan

            backtest_summary.append({
                "horizon_h": horizon, "n_trades": len(vals),
                "mean_gross_pct": mean_gross, "mean_net_pct": mean_net,
                "win_gross_pct": win_gross, "win_net_pct": win_net,
                "t_stat_net": float(t_stat), "p_t_net": float(p_t), "p_wilcoxon_net": float(p_wil),
                "pf_net": pf_net, "sharpe_per_trade_net": sharpe_trade,
                "is_mean_net_pct": is_mean, "oos_mean_net_pct": oos_mean,
            })
    df_bt = pd.DataFrame(backtest_summary)

    return {
        "events": valid_events,
        "aligned_1h": aligned_1h,
        "aligned_15m": aligned_15m,
        "df_ccf_1h": df_ccf_1h,
        "df_ccf_1h_vol": df_ccf_1h_vol,
        "df_ccf_15m": df_ccf_15m,
        "df_granger": df_granger,
        "df_shocks": df_shocks,
        "df_win_1h": df_win_1h,
        "df_win_15m": df_win_15m,
        "df_sess_ccf": df_sess_ccf,
        "df_bt": df_bt,
    }


def generate_charts(res: Dict[str, Any]) -> List[Path]:
    """検証結果を5枚の高解像度日本語チャートに描画し、PLOTS_DIR と ARTIFACT_DIR に保存"""
    configure_japanese_font()
    PLOTS_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    saved_paths: List[Path] = []

    events = res["events"]
    ev_map = {e["slug"]: e for e in events}
    df_ccf_1h = res["df_ccf_1h"]
    df_ccf_15m = res["df_ccf_15m"]
    df_win_1h = res["df_win_1h"]
    df_shocks = res["df_shocks"]
    df_sess_ccf = res["df_sess_ccf"]
    df_bt = res["df_bt"]

    # ==========================================================
    # Chart 1: 1h CCF Heatmap vs BTC (Exogenous vs Endogenous)
    # ==========================================================
    btc_ccf = df_ccf_1h[df_ccf_1h["asset"] == "BTC"].copy()
    if not btc_ccf.empty:
        # Sort: Exogenous groups first, then Endogenous
        ordered_evs = sorted(events, key=lambda e: (e["is_endogenous"], e["group"], -e["event_volume"]))
        slug_order = [e["slug"] for e in ordered_evs if e["slug"] in btc_ccf["slug"].unique()]
        piv_r = btc_ccf.pivot(index="slug", columns="lag", values="corr").reindex(slug_order)
        piv_q = btc_ccf.pivot(index="slug", columns="lag", values="q_val").reindex(slug_order)

        fig, ax = plt.subplots(figsize=(16, max(7.5, 0.44 * len(slug_order) + 2.5)))
        vmax = max(0.25, float(np.nanpercentile(np.abs(piv_r.values), 98)))
        im = ax.imshow(piv_r.values, cmap="RdBu_r", vmin=-vmax, vmax=vmax, aspect="auto")

        lags = piv_r.columns.tolist()
        ax.set_xticks(range(len(lags)))
        ax.set_xticklabels([f"{l:+d}" if l != 0 else "0\n(同時)" for l in lags], fontsize=8.5)

        ylabels = []
        exo_count = 0
        for s in slug_order:
            e = ev_map[s]
            if not e["is_endogenous"]:
                exo_count += 1
            tag = "【内生】" if e["is_endogenous"] else f"[{e['group']}]"
            d_str = f"({e['direction_sign']:+d})" if e["direction_sign"] != 0 else "(0)"
            ylabels.append(f"{tag} {shorten_text(e['title'], 34)} {d_str}")
        ax.set_yticks(range(len(slug_order)))
        ax.set_yticklabels(ylabels, fontsize=8.5)

        # Annotate FDR q<0.05 cells with * and lag=0 vertical line
        for i in range(len(slug_order)):
            for j in range(len(lags)):
                rv = piv_r.iloc[i, j]
                qv = piv_q.iloc[i, j]
                if pd.notna(qv) and qv < 0.05:
                    txt_color = "white" if abs(rv) > vmax * 0.55 else "black"
                    ax.text(j, i, f"{rv:+.2f}*", ha="center", va="center", fontsize=7.5, fontweight="bold", color=txt_color)

        zero_idx = lags.index(0)
        ax.axvline(zero_idx - 0.5, color="#222222", linestyle="--", linewidth=1.2, alpha=0.8)
        ax.axvline(zero_idx + 0.5, color="#222222", linestyle="--", linewidth=1.2, alpha=0.8)
        if 0 < exo_count < len(slug_order):
            ax.axhline(exo_count - 0.5, color="#111111", linestyle="-", linewidth=2.0)

        cbar = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.02)
        cbar.set_label("相関係数 r (dp vs BTCリターン)", fontsize=9.5)

        ax.set_title(
            "1時間足 リード・ラグ相互相関 (CCF): Polymarket確率変化 vs BTCリターン\n"
            "負ラグ(-k)=BTC先行 | 0=同時反応 | 正ラグ(+k)=Polymarket先行  (*はBH-FDR q<0.05有意)",
            fontsize=12.5, fontweight="bold", pad=14,
        )
        ax.set_xlabel("ラグ (時間)  ← BTCが先行   |   同時(0)   |   Polymarketが先行 →", fontsize=10.5, labelpad=8)
        fig.tight_layout()
        p1 = PLOTS_DIR / "01_leadlag_ccf_heatmap_1h.png"
        fig.savefig(p1, dpi=150, bbox_inches="tight")
        plt.close(fig)
        saved_paths.append(p1)

    # ==========================================================
    # Chart 2: 15m Lead-Lag Curve & Weekend/Session Split
    # ==========================================================
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6.5))
    if not df_ccf_15m.empty:
        for is_endo, label, color, ls in [
            (False, "外生イベント平均 (FOMC・地政学・Tech)", "#1f77b4", "-"),
            (True, "内生イベント平均 (BTC価格到達系)", "#d62728", "--"),
        ]:
            sub = df_ccf_15m[df_ccf_15m["is_endogenous"] == is_endo]
            if sub.empty:
                continue
            grp_mean = sub.groupby("lag")["corr"].mean()
            grp_se = sub.groupby("lag")["corr"].sem().fillna(0)
            xs = grp_mean.index.values * 15  # 分単位
            ax1.plot(xs, grp_mean.values, label=label, color=color, linestyle=ls, linewidth=2.3, marker="o", markersize=4)
            ax1.fill_between(xs, grp_mean.values - 1.96 * grp_se.values, grp_mean.values + 1.96 * grp_se.values, color=color, alpha=0.14)

        # ボラ相関（全外生）も追加
        ax1.axvline(0, color="#333333", linestyle=":", linewidth=1.3)
        ax1.axhline(0, color="#666666", linestyle="-", linewidth=0.9, alpha=0.6)
        ax1.set_title("15分足 リード・ラグ曲線 (±4時間: 外生 vs 内生)", fontsize=11.5, fontweight="bold")
        ax1.set_xlabel("ラグ (分)  ← BTC先行 (-240m)  |  0  |  Polymarket先行 (+240m) →", fontsize=9.5)
        ax1.set_ylabel("平均相関係数 r", fontsize=9.5)
        ax1.grid(True, linestyle=":", alpha=0.6)
        ax1.legend(fontsize=8.5, loc="best")

    if not df_sess_ccf.empty:
        exo_sess = df_sess_ccf[~df_sess_ccf["is_endogenous"]]
        sess_order = ["米国市場時間", "平日時間外", "週末(土日UTC)"]
        target_lags = [-1, 0, 1]
        lag_labels = { -1: "lag -15m (BTC先行)", 0: "lag 0m (同時)", 1: "lag +15m (Poly先行)" }
        x = np.arange(len(sess_order))
        w = 0.25
        colors_s = ["#7f7f7f", "#2ca02c", "#ff7f0e"]
        for idx, lg in enumerate(target_lags):
            vals = []
            errs = []
            for sname in sess_order:
                s_vals = exo_sess[(exo_sess["session"] == sname) & (exo_sess["lag"] == lg)]["corr"].dropna()
                vals.append(float(s_vals.mean()) if len(s_vals) else 0.0)
                errs.append(float(s_vals.sem()) if len(s_vals) > 1 else 0.0)
            ax2.bar(x + (idx - 1) * w, vals, width=w, yerr=errs, capsize=3, label=lag_labels[lg], color=colors_s[idx], alpha=0.88)

        ax2.axhline(0, color="#444444", linewidth=0.9)
        ax2.set_xticks(x)
        ax2.set_xticklabels(sess_order, fontsize=9.5)
        ax2.set_title("時間帯別（米国時間 vs 時間外 vs 週末）15分足相関\n【外生イベントのみ】", fontsize=11.5, fontweight="bold")
        ax2.set_ylabel("平均相関係数 r (±1SE)", fontsize=9.5)
        ax2.grid(True, axis="y", linestyle=":", alpha=0.6)
        ax2.legend(fontsize=8.5, loc="best")

    fig.tight_layout()
    p2 = PLOTS_DIR / "02_leadlag_15m_and_weekend_split.png"
    fig.savefig(p2, dpi=150, bbox_inches="tight")
    plt.close(fig)
    saved_paths.append(p2)

    # ==========================================================
    # Chart 3: Event Study (Shock Pre/Sim/Post Cumulative Drift & Volatility)
    # ==========================================================
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6.5))
    if not df_win_1h.empty:
        # 累積リターン（t=-1 を 0 基準にして、t=0の同時ジャンプと t+1..t+24の事後ドリフトを視覚化）
        exo_dir_w = df_win_1h[(~df_win_1h["is_endogenous"]) & (df_win_1h["btc_dir_ret"].notna())].copy()
        for grp_name, sub_df, col in [
            ("FOMC/金利 (方向付き)", exo_dir_w[exo_dir_w["group"] == "FOMC/金利"], "#1f77b4"),
            ("地政学 (方向付き)", exo_dir_w[exo_dir_w["group"] == "地政学"], "#2ca02c"),
            ("外生合計 (FOMC+地政学)", exo_dir_w, "#111111"),
        ]:
            if sub_df.empty:
                continue
            piv = sub_df.pivot_table(index="shock_ts", columns="rel_k", values="btc_dir_ret", aggfunc="mean")
            if piv.empty:
                continue
            # 累積和を t=-1 (=0) 基準にシフト
            cum = piv.cumsum(axis=1)
            if -1 in cum.columns:
                cum = cum.sub(cum[-1], axis=0)
            mean_c = cum.mean(axis=0)
            se_c = cum.sem(axis=0).fillna(0)
            ax1.plot(mean_c.index, mean_c.values, label=f"{grp_name} (n={len(piv)})", color=col, linewidth=2.2 if col == "#111111" else 1.8)
            if col == "#111111":
                ax1.fill_between(mean_c.index, mean_c.values - 1.96 * se_c.values, mean_c.values + 1.96 * se_c.values, color=col, alpha=0.12)

        ax1.axvspan(-0.5, 0.5, color="#ffe0b2", alpha=0.55, label="同時反応バー (t=0)")
        ax1.axvspan(0.5, 24.5, color="#e3f2fd", alpha=0.35, label="純粋事後トレード可能区間 (t+1..+24h)")
        ax1.axvline(0, color="#d62728", linestyle="--", linewidth=1.2)
        ax1.axhline(0, color="#555555", linewidth=0.9)
        ax1.set_title("確率急変ショック前後の BTC 累積方向一致リターン (1h足, 95%CI)", fontsize=11.5, fontweight="bold")
        ax1.set_xlabel("ショック発生からの経過時間 (時間, t=-1基準=0%)", fontsize=9.5)
        ax1.set_ylabel("累積方向一致リターン (%)", fontsize=9.5)
        ax1.grid(True, linestyle=":", alpha=0.6)
        ax1.legend(fontsize=8.3, loc="best")

        # 右パネル: ボラティリティ倍率 |r| / 平常時|r| (全外生イベント)
        exo_all_w = df_win_1h[~df_win_1h["is_endogenous"]]
        vol_piv = exo_all_w.pivot_table(index="shock_ts", columns="rel_k", values="btc_abs_ratio", aggfunc="mean")
        if not vol_piv.empty:
            v_mean = vol_piv.mean(axis=0)
            v_se = vol_piv.sem(axis=0).fillna(0)
            ax2.bar(v_mean.index, v_mean.values, color=["#d62728" if k == 0 else ("#ff9800" if 1 <= k <= 3 else "#607d8b") for k in v_mean.index], alpha=0.85)
            ax2.errorbar(v_mean.index, v_mean.values, yerr=1.96 * v_se.values, fmt="none", ecolor="#222222", elinewidth=0.9, capsize=2)
            ax2.axhline(1.0, color="#111111", linestyle="--", linewidth=1.2, label="平常時平均ボラ = 1.0x")
            ax2.set_title(f"外生ショック前後の BTC ボラティリティ倍率推移 (n={len(vol_piv)})", fontsize=11.5, fontweight="bold")
            ax2.set_xlabel("ショック発生からの経過時間 (時間)", fontsize=9.5)
            ax2.set_ylabel("|BTC リターン| / 平常時平均 (倍)", fontsize=9.5)
            ax2.grid(True, axis="y", linestyle=":", alpha=0.6)
            ax2.legend(fontsize=8.5, loc="best")

    fig.tight_layout()
    p3 = PLOTS_DIR / "03_event_study_post_shock_drift.png"
    fig.savefig(p3, dpi=150, bbox_inches="tight")
    plt.close(fig)
    saved_paths.append(p3)

    # ==========================================================
    # Chart 4: Case Studies Overlay (Top 4 Representative Events)
    # ==========================================================
    # FOMC / 地政学 / Tech / 内生 から1件ずつ最もインパクトの大きいイベントを抽出
    picked_cases: List[Dict[str, Any]] = []
    for grp_target in ["FOMC/金利", "地政学", "Tech/AI・マクロ", "暗号資産固有(内生)"]:
        cands = [e for e in events if e["group"] == grp_target and e["slug"] in res["aligned_1h"]]
        if cands:
            cands.sort(key=lambda e: res["aligned_1h"][e["slug"]]["dp"].abs().max(), reverse=True)
            picked_cases.append(cands[0])

    if picked_cases:
        fig, axes = plt.subplots(2, 2, figsize=(16, 10))
        axes_flat = axes.flatten()
        for idx, ev in enumerate(picked_cases[:4]):
            ax_l = axes_flat[idx]
            df1h = res["aligned_1h"][ev["slug"]]
            # 最大ショック前後 ±5日間を切り出して見やすく表示
            peak_ts = df1h["dp"].abs().idxmax()
            w_start = peak_ts - pd.Timedelta(days=6)
            w_end = peak_ts + pd.Timedelta(days=6)
            sub = df1h.loc[w_start:w_end]
            if len(sub) < 24:
                sub = df1h.iloc[-min(len(df1h), 240):]

            times_naive = [t.tz_convert(timezone(timedelta(hours=9))).tz_localize(None) for t in sub.index]
            l1 = ax_l.plot(times_naive, sub["prob"].values * 100.0, color="#1f77b4", linewidth=2.1, label="Polymarket確率 (%)")
            ax_l.set_ylabel("Polymarket 確率 (%)", color="#1f77b4", fontsize=9.5)
            ax_l.tick_params(axis="y", labelcolor="#1f77b4")

            ax_r = ax_l.twinx()
            l2 = ax_r.plot(times_naive, sub["close_BTC"].values, color="#ff7f0e", linewidth=1.8, alpha=0.9, label="BTC/USDT ($)")
            ax_r.set_ylabel("BTC 価格 (USDT)", color="#ff7f0e", fontsize=9.5)
            ax_r.tick_params(axis="y", labelcolor="#ff7f0e")

            # 地政学のときは Gold (PAXG) も軽く重ねる（正規化ではなく参考ラインはタイトルで注記）
            pk_naive = peak_ts.tz_convert(timezone(timedelta(hours=9))).tz_localize(None)
            ax_l.axvline(pk_naive, color="#d62728", linestyle="--", linewidth=1.2, alpha=0.8)

            ax_l.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
            ax_l.grid(True, linestyle=":", alpha=0.5)
            tag = "【内生】" if ev["is_endogenous"] else f"[{ev['group']}]"
            ax_l.set_title(
                f"{tag} {shorten_text(ev['title'], 38)}\n対象: {shorten_text(ev['submarket_label'], 30)} (赤破線=最大急変点)",
                fontsize=10.5, fontweight="bold",
            )
            lines = l1 + l2
            labels = [ln.get_label() for ln in lines]
            ax_l.legend(lines, labels, loc="upper left", fontsize=8.0)

        fig.suptitle("代表ケーススタディ: 最大急変局面前後の Polymarket 確率 vs BTC 価格推移 (JST)", fontsize=13, fontweight="bold", y=1.01)
        fig.tight_layout()
        p4 = PLOTS_DIR / "04_case_studies_overlay.png"
        fig.savefig(p4, dpi=150, bbox_inches="tight")
        plt.close(fig)
        saved_paths.append(p4)

    # ==========================================================
    # Chart 5: Cross-Asset Sensitivity & Backtest Performance
    # ==========================================================
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6.5))
    if not df_shocks.empty:
        exo_s = df_shocks[~df_shocks["is_endogenous"]]
        exo_dir_s = exo_s[exo_s["BTC_dir_sim"].notna()]
        asset_names = [SHORT_SYM[s] for s in SYMBOLS]
        sim_means, post6_means, vol_ratios = [], [], []
        for a in asset_names:
            sim_means.append(float(exo_dir_s[f"{a}_dir_sim"].mean()) if f"{a}_dir_sim" in exo_dir_s else 0.0)
            post6_means.append(float(exo_dir_s[f"{a}_dir_post6"].mean()) if f"{a}_dir_post6" in exo_dir_s else 0.0)
            vol_ratios.append(float(exo_s[f"{a}_sim_abs_ratio"].mean()) if f"{a}_sim_abs_ratio" in exo_s else 1.0)

        x = np.arange(len(asset_names))
        w = 0.35
        ax1.bar(x - w / 2, sim_means, width=w, label="同時バー方向リターン (t=0, %)", color="#1f77b4", alpha=0.88)
        ax1.bar(x + w / 2, post6_means, width=w, label="事後6時間ドリフト (t+1..+6h, %)", color="#2ca02c", alpha=0.88)
        ax1.axhline(0, color="#333333", linewidth=0.9)
        ax1.set_xticks(x)
        ax1.set_xticklabels(asset_names, fontsize=10, fontweight="bold")
        ax1.set_ylabel("平均方向一致リターン (%)", fontsize=9.5)
        ax1.set_title("銘柄別ショック感応度（保有 BTC/RENDER/NEAR vs ETH/SOL/PAXG）", fontsize=11.2, fontweight="bold")
        ax1.grid(True, axis="y", linestyle=":", alpha=0.6)

        ax1_r = ax1.twinx()
        ax1_r.plot(x, vol_ratios, color="#d62728", marker="D", linewidth=2.0, label="同時ボラ倍率 (右軸, 倍)")
        ax1_r.set_ylabel("同時ボラ拡大倍率 (平常時=1.0x)", color="#d62728", fontsize=9.5)
        ax1_r.tick_params(axis="y", labelcolor="#d62728")
        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax1_r.get_legend_handles_labels()
        ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper right", fontsize=8.0)

    if not df_bt.empty:
        x_b = np.arange(len(df_bt))
        w_b = 0.28
        ax2.bar(x_b - w_b, df_bt["mean_gross_pct"], width=w_b, label="平均リターン (手数料0bps)", color="#64b5f6")
        ax2.bar(x_b, df_bt["mean_net_pct"], width=w_b, label="平均リターン (手数料10bps控除後)", color="#1565c0")
        ax2.bar(x_b + w_b, df_bt["oos_mean_net_pct"].fillna(0), width=w_b, label="後半OOS期間 (手数料10bps控除後)", color="#ff9800")
        ax2.axhline(0, color="#111111", linewidth=1.0)
        ax2.set_xticks(x_b)
        ax2.set_xticklabels([f"{int(h)}h保有\n(勝率{w:.0f}%)" for h, w in zip(df_bt["horizon_h"], df_bt["win_net_pct"])], fontsize=9.0)
        ax2.set_title(f"ゼロ先読み事後トレード検証（外生ショック確定足終値でBTCエントリー, n={int(df_bt['n_trades'].iloc[0])}）", fontsize=11.0, fontweight="bold")
        ax2.set_ylabel("1トレード平均リターン (%)", fontsize=9.5)
        ax2.grid(True, axis="y", linestyle=":", alpha=0.6)
        ax2.legend(fontsize=8.3, loc="best")

    fig.tight_layout()
    p5 = PLOTS_DIR / "05_cross_asset_sensitivity_and_backtest.png"
    fig.savefig(p5, dpi=150, bbox_inches="tight")
    plt.close(fig)
    saved_paths.append(p5)

    for p in saved_paths:
        shutil.copy2(p, ARTIFACT_DIR / p.name)
    return saved_paths


def save_and_print_summary(res: Dict[str, Any], chart_paths: List[Path]) -> None:
    """分析結果を JSON / CSV に保存し、主要数値をコンソール出力"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    res["df_ccf_1h"].to_csv(DATA_DIR / "ccf_1h_directional.csv", index=False, encoding="utf-8")
    res["df_ccf_1h_vol"].to_csv(DATA_DIR / "ccf_1h_volatility.csv", index=False, encoding="utf-8")
    res["df_ccf_15m"].to_csv(DATA_DIR / "ccf_15m_directional.csv", index=False, encoding="utf-8")
    res["df_granger"].to_csv(DATA_DIR / "granger_causality.csv", index=False, encoding="utf-8")
    res["df_shocks"].to_csv(DATA_DIR / "shock_events_1h.csv", index=False, encoding="utf-8")
    res["df_sess_ccf"].to_csv(DATA_DIR / "session_split_ccf_15m.csv", index=False, encoding="utf-8")
    res["df_bt"].to_csv(DATA_DIR / "backtest_summary.csv", index=False, encoding="utf-8")

    # 要約統計計算
    df_ccf = res["df_ccf_1h"]
    btc_exo = df_ccf[(df_ccf["asset"] == "BTC") & (~df_ccf["is_endogenous"])]
    btc_endo = df_ccf[(df_ccf["asset"] == "BTC") & (df_ccf["is_endogenous"])]
    btc_vol_exo = res["df_ccf_1h_vol"][(res["df_ccf_1h_vol"]["asset"] == "BTC") & (~res["df_ccf_1h_vol"]["is_endogenous"])]

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "event_count_total": len(res["events"]),
        "event_count_exo": sum(1 for e in res["events"] if not e["is_endogenous"]),
        "event_count_endo": sum(1 for e in res["events"] if e["is_endogenous"]),
        "total_1h_bars_aligned": sum(len(df) for df in res["aligned_1h"].values()),
        "total_15m_bars_aligned": sum(len(df) for df in res["aligned_15m"].values()),
        "fdr_sig_1h_btc_exo": {
            "poly_leads_lag_gt_0": int(((btc_exo["lag"] > 0) & (btc_exo["q_val"] < 0.05)).sum()),
            "simultaneous_lag_0": int(((btc_exo["lag"] == 0) & (btc_exo["q_val"] < 0.05)).sum()),
            "crypto_leads_lag_lt_0": int(((btc_exo["lag"] < 0) & (btc_exo["q_val"] < 0.05)).sum()),
        },
        "fdr_sig_1h_btc_endo": {
            "poly_leads_lag_gt_0": int(((btc_endo["lag"] > 0) & (btc_endo["q_val"] < 0.05)).sum()),
            "simultaneous_lag_0": int(((btc_endo["lag"] == 0) & (btc_endo["q_val"] < 0.05)).sum()),
            "crypto_leads_lag_lt_0": int(((btc_endo["lag"] < 0) & (btc_endo["q_val"] < 0.05)).sum()),
        },
        "fdr_sig_1h_vol_exo": {
            "poly_leads_lag_gt_0": int(((btc_vol_exo["lag"] > 0) & (btc_vol_exo["q_val"] < 0.05)).sum()),
            "simultaneous_lag_0": int(((btc_vol_exo["lag"] == 0) & (btc_vol_exo["q_val"] < 0.05)).sum()),
            "crypto_leads_lag_lt_0": int(((btc_vol_exo["lag"] < 0) & (btc_vol_exo["q_val"] < 0.05)).sum()),
        },
        "granger_counts": res["df_granger"]["classification"].value_counts().to_dict() if not res["df_granger"].empty else {},
        "shock_count_1h_total": len(res["df_shocks"]),
        "shock_count_1h_exo": int((~res["df_shocks"]["is_endogenous"]).sum()) if not res["df_shocks"].empty else 0,
        "charts": [str(p) for p in chart_paths],
    }
    with (DATA_DIR / "research_summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 72)
    print("Polymarket × 暗号資産・マクロ相場 徹底検証 サマリー")
    print("=" * 72)
    print(f"有効イベント数: {summary['event_count_total']} (外生={summary['event_count_exo']}, 内生={summary['event_count_endo']})")
    print(f"整列バー総数  : 1h={summary['total_1h_bars_aligned']:,} bars, 15m={summary['total_15m_bars_aligned']:,} bars")
    print(f"1hショック数  : 全{summary['shock_count_1h_total']}件 (外生={summary['shock_count_1h_exo']}件)")
    print(f"1h CCF FDR有意数 (外生 vs BTC方向) : Poly先行={summary['fdr_sig_1h_btc_exo']['poly_leads_lag_gt_0']}, 同時={summary['fdr_sig_1h_btc_exo']['simultaneous_lag_0']}, BTC先行={summary['fdr_sig_1h_btc_exo']['crypto_leads_lag_lt_0']}")
    print(f"1h CCF FDR有意数 (内生 vs BTC方向) : Poly先行={summary['fdr_sig_1h_btc_endo']['poly_leads_lag_gt_0']}, 同時={summary['fdr_sig_1h_btc_endo']['simultaneous_lag_0']}, BTC先行={summary['fdr_sig_1h_btc_endo']['crypto_leads_lag_lt_0']}")
    print(f"1h CCF FDR有意数 (外生 vs BTCボラ) : Poly先行={summary['fdr_sig_1h_vol_exo']['poly_leads_lag_gt_0']}, 同時={summary['fdr_sig_1h_vol_exo']['simultaneous_lag_0']}, BTC先行={summary['fdr_sig_1h_vol_exo']['crypto_leads_lag_lt_0']}")
    print(f"Granger 5分類 : {summary['granger_counts']}")
    if not res["df_bt"].empty:
        print("\n--- 簡易事後バックテスト (外生ショック確定足終値エントリー -> BTC, 手数料10bps) ---")
        print(res["df_bt"].to_string(index=False))
    print("\n生成チャート:")
    for p in chart_paths:
        print(f"  - {p} ({p.stat().st_size:,} bytes)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket × 暗号資産 連動性＆先行性徹底検証")
    parser.add_argument("--refresh", action="store_true", help="キャッシュを無視してAPIから再取得する")
    args = parser.parse_args()

    events = select_research_events(refresh=args.refresh)
    if len(events) < 6:
        print("❌ 有効イベントが不足しています。")
        return 1

    res = run_analysis(events, refresh=args.refresh)
    chart_paths = generate_charts(res)
    save_and_print_summary(res, chart_paths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
