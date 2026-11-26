"""Polymarket 相場センチメント 24h出来高 Top 20 1ヶ月ノーマライズ推移チャート＆24h急変ピックアップ

仕様:
1. Polymarket Gamma API から現在アクティブかつ未終了のイベントを 24時間出来高順に取得。
2. スポーツ・eスポーツ・エンタメ・ツイート数カウント等のノイズを除外し、
   相場センチメント関連カテゴリ（Crypto / Econ/Fin / Tech/AI / Geo / Politics）のみを抽出。
3. プレースホルダー・決着張り付き（1%未満 / 99%超）・履歴1日未満の超短期を除外し、
   各イベントの本命候補を Top 20 まで選出。
4. CLOB API (/prices-history) から直近1ヶ月の推移と「直近24時間の確率変化（pt）」を算出。
5. 開始時点=100%の比率ノーマライズチャートを 10銘柄×2枚（Top 1-10 / Top 11-20）で生成し、
   24時間確率急変ピックアップ Top 5 とともに Discord へ送信。
"""
from __future__ import annotations

import argparse
import json
import statistics
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
import requests

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config_loader import get_webhook_url

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

JST = timezone(timedelta(hours=9))
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"

PLACEHOLDER_PREFIXES = ("Candidate ", "Team ", "Company ", "Player ", "Country ")

# 相場センチメントと無関係なスポーツ・エンタメ・ツイート数カウント等を最優先除外
EXCLUDED_TAG_SLUGS = {
    "sports", "esports", "games", "nba", "nba-finals", "nfl", "nfl-all", "football",
    "basketball", "soccer", "tennis", "baseball", "mlb", "nhl", "hockey", "mma", "ufc",
    "boxing", "golf", "f1", "formula-1", "grand-prix", "cricket", "cbb", "cfb", "ncaa",
    "dota-2", "cs2", "counter-strike-2", "league-of-legends", "valorant",
    "champions-league", "ucl", "epl", "laliga", "la-liga", "mls",
    "tweet-markets", "music", "movies", "tv", "gaming", "video-games", "gta-vi",
    "awards", "weather", "chess", "poker", "math", "science",
}

# 相場センチメント5カテゴリの優先順ルール (pop-culture併記のTech案件を拾うためホワイトリスト併用)
CATEGORY_TAG_RULES: List[Tuple[str, set]] = [
    ("Crypto", {"crypto", "crypto-prices", "bitcoin", "ethereum", "solana", "altcoins", "defi", "stablecoins"}),
    ("Econ/Fin", {
        "economy", "finance", "fed", "fomc", "fed-rates", "economic-policy", "inflation",
        "recession", "macro", "commodities", "oil", "gold", "stocks", "ipo", "ipos",
        "business", "central-banks", "interest-rates", "currencies", "forex", "tariffs", "trade-war",
    }),
    ("Tech/AI", {"big-tech", "tech", "ai", "openai", "anthropic", "nvidia", "nvda", "claude"}),
    ("Geo", {"geopolitics", "middle-east", "military-strikes", "foreign-policy", "israel-x-iran", "blockade"}),
    ("Politics", {
        "politics", "us-election", "elections", "main-election", "world-elections",
        "global-elections", "french-election", "israel-election", "senate", "midterms", "president",
    }),
]

HIGH_CONTRAST_COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#17becf", "#bcbd22", "#34495e",
]
LINE_STYLES = ["-", "--", "-.", ":"]
VOL_ALERT_CATEGORIES = {"Econ/Fin", "Geo"}


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


def safe_mpl_label(text: str) -> str:
    """古い matplotlib で $...$ が数式解釈されるのを防止"""
    if "text.parse_math" in matplotlib.rcParams and not matplotlib.rcParams["text.parse_math"]:
        return text
    return text.replace("$", r"\$")


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


def fetch_top_active_events(limit: int = 100) -> List[Dict[str, Any]]:
    """24時間出来高順で未終了のアクティブイベントを取得"""
    params = {
        "active": "true",
        "closed": "false",
        "order": "volume24hr",
        "ascending": "false",
        "limit": limit,
    }
    resp = requests.get(GAMMA_EVENTS_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def select_primary_market(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """イベント内からプレースホルダーや決着張り付きを除外し、本命アクティブ候補を1つ選出"""
    raw_markets = event.get("markets") or []
    candidates: List[Tuple[float, float, str, str]] = []

    for m in raw_markets:
        if m.get("active") is not True or m.get("closed"):
            continue
        vol_m = float(m.get("volume") or 0.0)
        if vol_m <= 0:
            continue

        name = (m.get("groupItemTitle") or m.get("question") or "").strip()
        if is_placeholder_name(name):
            continue

        outs = parse_json_list(m.get("outcomes"))
        prs = parse_json_list(m.get("outcomePrices"))
        tids = parse_json_list(m.get("clobTokenIds"))
        if not outs or not prs or not tids or len(prs) != len(tids):
            continue

        try:
            float_prices = [float(p) for p in prs]
        except (TypeError, ValueError):
            continue

        if outs == ["Yes", "No"]:
            side_idx = 0
            side_label = name
        else:
            side_idx = max(range(len(float_prices)), key=lambda idx: float_prices[idx])
            side_label = str(outs[side_idx]) if len(raw_markets) == 1 else f"{name} ({outs[side_idx]})"

        p_now = float_prices[side_idx]
        if 0.01 <= p_now <= 0.99:
            candidates.append((p_now, vol_m, str(tids[side_idx]), side_label))

    if not candidates:
        return None

    # 現在確率が最も高い本命候補（同率なら取引量順）を採用
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    p_now, vol_m, token_id, label = candidates[0]
    return {
        "token_id": token_id,
        "candidate_label": label,
        "current_prob": p_now,
        "market_volume": vol_m,
    }


def fetch_monthly_history(
    token_id: str,
    min_days: float = 1.0,
    min_points: int = 12,
    lookback_days: int = 30,
) -> Optional[List[Tuple[datetime, float]]]:
    """CLOB API から直近1ヶ月（1時間足相当）の価格推移を取得し、1日未満の超短期は除外"""
    params = {"market": token_id, "interval": "1m", "fidelity": 60}
    for attempt in range(3):
        try:
            resp = requests.get(CLOB_HISTORY_URL, params=params, timeout=15)
            if resp.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            if not resp.ok:
                return None
            raw_hist = resp.json().get("history") or []
            break
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    else:
        return None

    if len(raw_hist) < min_points:
        return None

    now_utc = datetime.now(timezone.utc)
    cutoff_ts = (now_utc - timedelta(days=lookback_days)).timestamp()

    points: List[Tuple[datetime, float]] = []
    for pt in raw_hist:
        try:
            t_sec = float(pt["t"])
            p_val = float(pt["p"])
            if t_sec >= cutoff_ts and p_val > 0.0:
                dt_jst = datetime.fromtimestamp(t_sec, tz=timezone.utc).astimezone(JST)
                points.append((dt_jst, p_val))
        except Exception:
            continue

    if len(points) < min_points:
        return None

    span_days = (points[-1][0] - points[0][0]).total_seconds() / 86400.0
    if span_days < min_days:
        return None

    return points


def classify_market_sentiment_event(event: Dict[str, Any]) -> Optional[str]:
    """相場センチメント対象イベントかを判定し、5カテゴリ名のいずれかを返す（対象外は None）"""
    tags = event.get("tags") or []
    slugs = {str(t.get("slug", "")).lower() for t in tags if isinstance(t, dict) and t.get("slug")}
    title_lower = str(event.get("title") or "").lower()

    if "tweets" in title_lower or (slugs & EXCLUDED_TAG_SLUGS):
        return None

    for cat_name, tag_set in CATEGORY_TAG_RULES:
        if slugs & tag_set:
            return cat_name
    return None


def compute_24h_prob_delta(history: List[Tuple[datetime, float]]) -> Tuple[Optional[float], Optional[float]]:
    """直近24時間に最も近い点からの確率変化 (24h前確率%, 変化pt) を算出"""
    if len(history) < 2:
        return None, None
    latest_dt, latest_p = history[-1]
    target_dt = latest_dt - timedelta(hours=24)
    ref_dt, ref_p = min(history[:-1], key=lambda item: abs((item[0] - target_dt).total_seconds()))
    if (latest_dt - ref_dt).total_seconds() < 12 * 3600:
        return None, None
    return ref_p * 100.0, (latest_p - ref_p) * 100.0


def detect_causal_vol_shocks(
    history: List[Tuple[datetime, float]],
    min_dp_pt: float = 3.0,
    min_sigma: float = 3.0,
    roll_window: int = 168,
    cooldown_hours: float = 6.0,
    recent_window_hours: float = 8.0,
) -> Dict[str, Any]:
    """完全因果的（過去窓のみ使用・先読みなし）に 1h 3σ確率急変ショックを検知する"""
    shock_indices: List[int] = []
    shock_details: List[Dict[str, Any]] = []
    prev_dps: List[float] = []
    last_shock_dt: Optional[datetime] = None

    for i in range(1, len(history)):
        prev_dt, prev_p = history[i - 1]
        cur_dt, cur_p = history[i]
        dt_hours = (cur_dt - prev_dt).total_seconds() / 3600.0
        if not (0.25 <= dt_hours <= 4.0):
            continue

        dp_pt = (cur_p - prev_p) * 100.0
        if len(prev_dps) >= 24:
            window_vals = prev_dps[-roll_window:]
            roll_std = statistics.stdev(window_vals)
            if roll_std > 1e-9 and abs(dp_pt) >= min_dp_pt:
                z_score = abs(dp_pt) / roll_std
                cooldown_ok = (
                    last_shock_dt is None
                    or (cur_dt - last_shock_dt).total_seconds() >= cooldown_hours * 3600.0
                )
                if z_score >= min_sigma and cooldown_ok:
                    shock_indices.append(i)
                    shock_details.append({
                        "idx": i,
                        "dt": cur_dt,
                        "dp_pt": dp_pt,
                        "z_score": z_score,
                        "prob_before_pct": prev_p * 100.0,
                        "prob_after_pct": cur_p * 100.0,
                    })
                    last_shock_dt = cur_dt

        prev_dps.append(dp_pt)

    recent_shock: Optional[Dict[str, Any]] = None
    if shock_details and history:
        latest_dt = history[-1][0]
        recent_cands = [
            sh for sh in shock_details
            if (latest_dt - sh["dt"]).total_seconds() <= recent_window_hours * 3600.0
        ]
        if recent_cands:
            recent_shock = max(recent_cands, key=lambda sh: abs(float(sh["dp_pt"])))

    return {
        "shock_indices": shock_indices,
        "shock_details": shock_details,
        "recent_shock": recent_shock,
    }


def collect_top_normalized_markets(
    top_n: int = 20,
    min_days: float = 1.0,
    lookback_days: int = 30,
    shock_window_hours: float = 8.0,
    shock_min_pt: float = 3.0,
    shock_min_sigma: float = 3.0,
) -> List[Dict[str, Any]]:
    """24h出来高順で相場センチメント条件を満たす上位 top_n 銘柄と1ヶ月推移を収集"""
    events = fetch_top_active_events(limit=150)
    results: List[Dict[str, Any]] = []

    for ev in events:
        category = classify_market_sentiment_event(ev)
        if not category:
            continue

        primary = select_primary_market(ev)
        if not primary:
            continue

        history = fetch_monthly_history(
            token_id=primary["token_id"],
            min_days=min_days,
            lookback_days=lookback_days,
        )
        if not history:
            continue

        base_price = history[0][1]
        if base_price <= 0.001:
            continue

        times = [dt for dt, _ in history]
        prices = [p for _, p in history]
        norm_values = [(p / base_price) * 100.0 for p in prices]

        last_norm = norm_values[-1]
        diff_pct = last_norm - 100.0
        span_days = (times[-1] - times[0]).total_seconds() / 86400.0
        prob_24h_ago_pct, delta_24h_pt = compute_24h_prob_delta(history)
        shock_info = detect_causal_vol_shocks(
            history,
            min_dp_pt=shock_min_pt,
            min_sigma=shock_min_sigma,
            recent_window_hours=shock_window_hours,
        )
        recent_shock = shock_info["recent_shock"]
        is_vol_alert = bool(category in VOL_ALERT_CATEGORIES and recent_shock is not None)

        results.append({
            "rank": len(results) + 1,
            "category": category,
            "title": str(ev.get("title") or "Unknown Event"),
            "slug": str(ev.get("slug") or ""),
            "volume24h": float(ev.get("volume24hr") or 0.0),
            "volume_total": float(ev.get("volume") or 0.0),
            "candidate": primary["candidate_label"],
            "base_prob_pct": base_price * 100.0,
            "curr_prob_pct": prices[-1] * 100.0,
            "prob_24h_ago_pct": prob_24h_ago_pct,
            "delta_24h_pt": delta_24h_pt,
            "norm_pct": last_norm,
            "diff_pct": diff_pct,
            "span_days": span_days,
            "times": times,
            "norm_values": norm_values,
            "shock_indices": shock_info["shock_indices"],
            "shock_details": shock_info["shock_details"],
            "recent_shock": recent_shock,
            "is_vol_alert": is_vol_alert,
        })

        if len(results) >= top_n:
            break
        time.sleep(0.08)

    return results


def render_normalized_chart(
    items: List[Dict[str, Any]],
    out_path: Path,
    rank_label: str,
    generated_at_jst: datetime,
) -> Optional[Path]:
    """指定された銘柄群（最大10銘柄）を Base=100% ノーマライズチャートとして描画・保存"""
    if not items:
        return None

    configure_japanese_font()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(15, 7.5))
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#fafafa")

    all_norms: List[float] = []
    has_any_shock = False

    for idx, item in enumerate(items):
        color = HIGH_CONTRAST_COLORS[idx % len(HIGH_CONTRAST_COLORS)]
        linestyle = LINE_STYLES[(idx // len(HIGH_CONTRAST_COLORS)) % len(LINE_STYLES)]

        sign = "+" if item["diff_pct"] >= 0 else ""
        ev_short = shorten_text(item["title"], 28)
        cand_short = shorten_text(item["candidate"], 16)
        label = safe_mpl_label(
            f"#{item['rank']:02d} [{item['category']}] {ev_short} [{cand_short}] "
            f"({item['base_prob_pct']:.1f}%→{item['curr_prob_pct']:.1f}% | {sign}{item['diff_pct']:.1f}%)"
        )
        # matplotlib 側の naive 表示用に tz を除いた JST 時刻を使う
        naive_times = [t.replace(tzinfo=None) for t in item["times"]]
        norms = item["norm_values"]
        all_norms.extend(norms)

        ax.plot(
            naive_times,
            norms,
            label=label,
            color=color,
            linestyle=linestyle,
            linewidth=2.0,
            alpha=0.92,
        )
        ax.scatter(
            [naive_times[-1]],
            [norms[-1]],
            color=color,
            s=22,
            zorder=5,
        )

        shock_idxs = item.get("shock_indices") or []
        if shock_idxs:
            has_any_shock = True
            recent_idx = item["recent_shock"]["idx"] if (item.get("is_vol_alert") and item.get("recent_shock")) else None
            normal_idxs = [si for si in shock_idxs if si != recent_idx and 0 <= si < len(naive_times)]
            if normal_idxs:
                ax.scatter(
                    [naive_times[si] for si in normal_idxs],
                    [norms[si] for si in normal_idxs],
                    marker="*",
                    s=115,
                    color=color,
                    edgecolors="#111111",
                    linewidths=0.5,
                    zorder=7,
                )
            if recent_idx is not None and 0 <= recent_idx < len(naive_times):
                ax.scatter(
                    [naive_times[recent_idx]],
                    [norms[recent_idx]],
                    marker="*",
                    s=185,
                    color=color,
                    edgecolors="#d62728",
                    linewidths=1.3,
                    zorder=8,
                )

    ax.axhline(100.0, color="#555555", linestyle="--", linewidth=1.2, alpha=0.75, label="基準ライン (100%)")
    if has_any_shock:
        ax.scatter(
            [], [],
            marker="*",
            s=115,
            color="#444444",
            edgecolors="#d62728",
            linewidths=0.9,
            label="★ = 1h 3σ急変ショック (|Δp|>=3pt & >=3σ)",
        )

    # 極端な低確率急騰スパイクで他の線が潰れないよう Y軸外れ値スマートクリップ
    if all_norms:
        sorted_vals = sorted(all_norms)
        ymax_raw = sorted_vals[-1]
        ymin_raw = sorted_vals[0]
        p96 = sorted_vals[int(0.96 * (len(sorted_vals) - 1))]
        if ymax_raw > max(260.0, p96 * 1.45):
            y_cap = max(220.0, min(ymax_raw * 1.05, p96 * 1.25))
            y_low = max(0.0, min(85.0, ymin_raw * 0.92))
            ax.set_ylim(y_low, y_cap)
            ax.text(
                0.012, 0.975,
                f"※ 一部急騰銘柄はY軸上限 ({y_cap:.0f}%, 実最大 {ymax_raw:.0f}%) 超のためクリップ表示",
                transform=ax.transAxes,
                ha="left", va="top",
                fontsize=8.2,
                color="#444444",
                bbox={"boxstyle": "round,pad=0.3", "facecolor": "#fff8e1", "edgecolor": "#e0c070", "alpha": 0.92},
            )
    ax.set_title(
        f"Polymarket 相場センチメント 24h出来高 {rank_label} — 直近1ヶ月推移 (開始=100%) [{generated_at_jst.strftime('%Y-%m-%d %H:%M JST')}]",
        fontsize=12.5,
        fontweight="bold",
        pad=14,
    )
    ax.set_xlabel("日時 (JST)", fontsize=10, labelpad=8)
    ax.set_ylabel("ノーマライズ指数 (開始時 = 100%)", fontsize=10, labelpad=8)
    ax.grid(True, which="both", linestyle=":", alpha=0.6)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=6, maxticks=12))
    fig.autofmt_xdate()

    ax.legend(
        title="順位 / イベント [本命候補] (開始→現在 | 変化率)",
        title_fontsize=9,
        fontsize=8.5,
        loc="upper left",
        bbox_to_anchor=(1.015, 1.0),
        frameon=True,
        facecolor="#ffffff",
        edgecolor="#cccccc",
    )

    fig.tight_layout()
    fig.savefig(out_path, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return out_path


def _format_24h_delta(delta_pt: Optional[float]) -> str:
    if delta_pt is None:
        return "24h:NA"
    sign = "+" if delta_pt >= 0 else ""
    return f"24h:{sign}{delta_pt:.1f}pt"


def build_discord_caption(
    items: List[Dict[str, Any]],
    rank_label: str,
    generated_at_jst: datetime,
    shock_items: Optional[List[Dict[str, Any]]] = None,
    vol_alerts: Optional[List[Dict[str, Any]]] = None,
    show_regime_banner: bool = False,
    shock_window_hours: float = 8.0,
) -> str:
    """各チャート画像に添える Discord サマリーテキスト（2000文字以内）を作成"""
    header = (
        f"📊 **【Polymarket 相場センチメント】 24h出来高 {rank_label} (1ヶ月ノーマライズ推移)**\n"
        f"取得時刻: `{generated_at_jst.strftime('%Y-%m-%d %H:%M JST')}` | "
        f"対象: `Crypto / 経済金融 / Tech・AI / 地政学 / 政治`"
    )
    lines = [header]

    if show_regime_banner:
        if vol_alerts:
            lines.append(
                f"🚨 **【ボラ警戒モード】FOMC/地政学 1h 3σショック検知 (直近{shock_window_hours:g}h)**\n"
                "┗ 今後1〜6h BTC(1.24x)/Gold(1.33x) 変動拡大注意・成行飛び乗り厳禁"
            )
            lines.append("```text")
            for a_item in vol_alerts[:3]:
                sh = a_item["recent_shock"]
                dp = float(sh["dp_pt"])
                sign = "+" if dp >= 0 else ""
                ev_s = shorten_text(a_item["title"], 24)
                dt_str = sh["dt"].strftime("%m/%d %H:%M")
                lines.append(
                    f"[{a_item['category']:<8}] {ev_s:<24} {sign}{dp:4.1f}pt ({sh['z_score']:3.1f}σ) @ {dt_str}"
                )
            lines.append("```")
        else:
            lines.append(
                f"🟢 **ボラ・レジーム: 平常** (FOMC/地政学 直近{shock_window_hours:g}hに 3σショックなし)"
            )

    lines.append("```text")
    for item in items:
        ev_s = shorten_text(item["title"], 24)
        cand_s = shorten_text(item["candidate"], 13)
        d24_str = _format_24h_delta(item.get("delta_24h_pt"))
        lines.append(
            f"#{item['rank']:02d} [{item['category']:<8}] {ev_s:<24} [{cand_s:<13}] "
            f"{item['curr_prob_pct']:4.1f}% ({d24_str}) | ${item['volume24h']:,.0f}"
        )
    lines.append("```")

    if shock_items:
        lines.append("⚡ **直近24時間 確率急変ピックアップ (全20銘柄中 Top 5)**")
        lines.append("```text")
        for s_item in shock_items:
            delta_pt = float(s_item["delta_24h_pt"])
            icon = "🔥" if delta_pt >= 0 else "🧊"
            sign = "+" if delta_pt >= 0 else ""
            ev_s = shorten_text(s_item["title"], 24)
            cand_s = shorten_text(s_item["candidate"], 13)
            p_24h = float(s_item["prob_24h_ago_pct"])
            p_now = float(s_item["curr_prob_pct"])
            lines.append(
                f"{icon} #{s_item['rank']:02d} [{s_item['category']:<8}] {ev_s:<24} [{cand_s:<13}] "
                f"{p_24h:4.1f}%->{p_now:4.1f}% ({sign}{delta_pt:.1f}pt)"
            )
        lines.append("```")

    text = "\n".join(lines)
    if len(text) > 1950:
        cut = text[:1940]
        last_nl = cut.rfind("\n")
        if last_nl > 0:
            cut = cut[:last_nl]
        if cut.count("```") % 2 == 1:
            cut += "\n```"
        text = cut
    return text


def send_chart_to_discord(webhook_url: str, image_path: Path, content: str) -> bool:
    """Discord Webhook へテキスト付きでチャート画像を送信"""
    for attempt in range(3):
        try:
            with image_path.open("rb") as fp:
                resp = requests.post(
                    webhook_url,
                    data={"content": content},
                    files={"file": (image_path.name, fp, "image/png")},
                    timeout=30,
                )
            if resp.status_code in (200, 204):
                return True
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 3.0))
                time.sleep(retry_after + 1.0)
                continue
            resp.raise_for_status()
        except Exception as exc:
            print(f"[Discord Error] 送信失敗 (試行 {attempt + 1}/3): {exc}")
            time.sleep(2.0 * (attempt + 1))
    return False


def cleanup_old_charts(plots_dir: Path, max_age_hours: float = 72.0, keep_latest: int = 4) -> None:
    """古い Polymarket チャート画像を自動削除（最新 keep_latest 枚は保持）"""
    if not plots_dir.exists():
        return
    cutoff_ts = time.time() - (max_age_hours * 3600.0)
    files = sorted(plots_dir.glob("polymarket_top*_1m_*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files[keep_latest:]:
        try:
            if f.stat().st_mtime < cutoff_ts:
                f.unlink()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket 24h出来高 Top 20 ノーマライズチャート配信")
    parser.add_argument("--top-n", type=int, default=20, help="取得銘柄数（デフォルト: 20）")
    parser.add_argument("--days", type=int, default=30, help="集計対象日数（デフォルト: 30日）")
    parser.add_argument("--min-days", type=float, default=1.0, help="除外する超短期閾値日数（デフォルト: 1.0日）")
    parser.add_argument("--shock-window-hours", type=float, default=8.0, help="ボラ警戒バナー判定窓(時間、デフォルト: 8.0)")
    parser.add_argument("--shock-min-pt", type=float, default=3.0, help="1h急変ショック最小確率差(pt、デフォルト: 3.0)")
    parser.add_argument("--shock-min-sigma", type=float, default=3.0, help="1h急変ショック最小Zスコア(σ、デフォルト: 3.0)")
    parser.add_argument("--channel", type=str, default=None, help="送信先Webhook名 (例: real1_bitbank, test4_test)")
    parser.add_argument("--test", action="store_true", help="test4_test チャンネルへ強制送信")
    parser.add_argument("--dry-run", action="store_true", help="画像生成のみ行い Discord 送信しない")
    args = parser.parse_args()

    now_jst = datetime.now(JST)
    date_tag = now_jst.strftime("%Y%m%d_%H%M%S")
    plots_dir = BASE_DIR / "Data" / "plots"

    print(f"[{now_jst.strftime('%Y-%m-%d %H:%M:%S JST')}] Polymarket 相場センチメント 24h出来高 Top {args.top_n} を取得中...")
    items = collect_top_normalized_markets(
        top_n=args.top_n,
        min_days=args.min_days,
        lookback_days=args.days,
        shock_window_hours=args.shock_window_hours,
        shock_min_pt=args.shock_min_pt,
        shock_min_sigma=args.shock_min_sigma,
    )
    if not items:
        print("❌ 有効なマーケットデータを取得できませんでした。")
        return 1

    shock_items = sorted(
        [it for it in items if it.get("delta_24h_pt") is not None],
        key=lambda it: abs(float(it["delta_24h_pt"])),
        reverse=True,
    )[:5]
    vol_alerts = sorted(
        [it for it in items if it.get("is_vol_alert") and it.get("recent_shock")],
        key=lambda it: abs(float(it["recent_shock"]["dp_pt"])),
        reverse=True,
    )

    print(f"✅ 取得完了: {len(items)} 銘柄 (3σショック延べ {sum(len(it.get('shock_indices') or []) for it in items)} 回)")
    if vol_alerts:
        print(f"🚨 ボラ警戒モード発動 (直近{args.shock_window_hours:g}h / {len(vol_alerts)}件):")
        for a_it in vol_alerts:
            sh = a_it["recent_shock"]
            sign = "+" if sh["dp_pt"] >= 0 else ""
            print(f"  * [{a_it['category']}] {a_it['title']} -> {sign}{sh['dp_pt']:.1f}pt ({sh['z_score']:.1f}σ) @ {sh['dt'].strftime('%m/%d %H:%M JST')}")
    else:
        print(f"🟢 ボラ・レジーム: 平常 (FOMC/地政学 直近{args.shock_window_hours:g}hに 3σショックなし)")

    for item in items:
        sign = "+" if item["diff_pct"] >= 0 else ""
        d24 = _format_24h_delta(item.get("delta_24h_pt"))
        n_sh = len(item.get("shock_indices") or [])
        print(
            f"  #{item['rank']:02d} [{item['category']:<8}] 24h=${item['volume24h']:>10,.0f} ({item['span_days']:4.1f}d, ★{n_sh}) | "
            f"{item['base_prob_pct']:5.1f}%->{item['curr_prob_pct']:5.1f}% ({sign}{item['diff_pct']:6.1f}% | {d24}) | "
            f"{item['title']} [{item['candidate']}]"
        )

    if shock_items:
        print("\n⚡ 直近24時間 確率急変ピックアップ (Top 5):")
        for s_item in shock_items:
            dp = float(s_item["delta_24h_pt"])
            sign = "+" if dp >= 0 else ""
            print(
                f"  #{s_item['rank']:02d} [{s_item['category']:<8}] "
                f"{s_item['prob_24h_ago_pct']:5.1f}%->{s_item['curr_prob_pct']:5.1f}% ({sign}{dp:.1f}pt) | "
                f"{s_item['title']} [{s_item['candidate']}]"
            )

    # 10銘柄ずつ 2分割してチャート生成 (Top 1-10 / Top 11-20)
    groups = [
        ("Top 1-10", items[:10], plots_dir / f"polymarket_top1_10_1m_{date_tag}.png"),
        ("Top 11-20", items[10:20], plots_dir / f"polymarket_top11_20_1m_{date_tag}.png"),
    ]

    generated: List[Tuple[str, List[Dict[str, Any]], Path]] = []
    for rank_label, sub_items, out_path in groups:
        if not sub_items:
            continue
        rendered = render_normalized_chart(sub_items, out_path, rank_label, now_jst)
        if rendered:
            print(f"📈 チャート生成完了: {rendered}")
            generated.append((rank_label, sub_items, rendered))

    if args.dry_run:
        for idx, (rank_label, sub_items, _) in enumerate(generated):
            cap = build_discord_caption(
                sub_items,
                rank_label,
                now_jst,
                shock_items=shock_items if idx == 0 else None,
                vol_alerts=vol_alerts if idx == 0 else None,
                show_regime_banner=(idx == 0),
                shock_window_hours=args.shock_window_hours,
            )
            print(f"📝 [{rank_label}] キャプション文字数: {len(cap)} 文字")
            if idx == 0:
                print("--- [Top 1-10 キャプションプレビュー] ---")
                print(cap)
                print("---------------------------------------")
        print("📌 [--dry-run] Discord 送信をスキップしました。")
        cleanup_old_charts(plots_dir)
        return 0

    channel_name = "test4_test" if args.test else args.channel
    webhook_url = get_webhook_url(channel_name)
    if not webhook_url:
        print(f"❌ Discord Webhook URL が見つかりません (channel={channel_name or 'auto'})")
        return 1

    all_ok = True
    for idx, (rank_label, sub_items, chart_path) in enumerate(generated):
        caption = build_discord_caption(
            sub_items,
            rank_label,
            now_jst,
            shock_items=shock_items if idx == 0 else None,
            vol_alerts=vol_alerts if idx == 0 else None,
            show_regime_banner=(idx == 0),
            shock_window_hours=args.shock_window_hours,
        )
        ok = send_chart_to_discord(webhook_url, chart_path, caption)
        if ok:
            print(f"✅ Discord 送信成功 ({rank_label}): {chart_path.name}")
        else:
            print(f"❌ Discord 送信失敗 ({rank_label}): {chart_path.name}")
            all_ok = False
        if idx + 1 < len(generated):
            time.sleep(2.0)

    cleanup_old_charts(plots_dir)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
