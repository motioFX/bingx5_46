"""Polymarket 24h出来高 Top 20 銘柄 1ヶ月ノーマライズ推移チャート生成＆Discord通知スクリプト

仕様:
1. Polymarket Gamma API から現在アクティブかつ未終了のイベントを 24時間出来高順に取得。
2. プレースホルダー市場や決着張り付き（1%未満 / 99%超）、履歴1日未満の超短期イベントを除外し、
   各イベントの本命候補（現在確率最大のアクティブ選択肢）を Top 20 まで選出。
3. Polymarket CLOB API (/prices-history) から直近1ヶ月（interval=1m, fidelity=60）の推移を取得。
4. 各銘柄の開始時点価格を 100% として比率ノーマライズし、見やすさ重視で 10銘柄×2枚（Top 1-10 / Top 11-20）のチャート画像を生成。
5. Discord Webhook（VPSでは real1_bitbank、Windowsテスト時や --test 指定時は test4_test）へ送信。
"""
from __future__ import annotations

import argparse
import json
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
HIGH_CONTRAST_COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#17becf", "#bcbd22", "#34495e",
]
LINE_STYLES = ["-", "--", "-.", ":"]


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


def collect_top_normalized_markets(
    top_n: int = 20,
    min_days: float = 1.0,
    lookback_days: int = 30,
) -> List[Dict[str, Any]]:
    """24h出来高順でフィルタ条件を満たす上位 top_n 銘柄と1ヶ月推移を収集"""
    events = fetch_top_active_events(limit=100)
    results: List[Dict[str, Any]] = []

    for ev in events:
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

        results.append({
            "rank": len(results) + 1,
            "title": str(ev.get("title") or "Unknown Event"),
            "slug": str(ev.get("slug") or ""),
            "volume24h": float(ev.get("volume24hr") or 0.0),
            "volume_total": float(ev.get("volume") or 0.0),
            "candidate": primary["candidate_label"],
            "base_prob_pct": base_price * 100.0,
            "curr_prob_pct": prices[-1] * 100.0,
            "norm_pct": last_norm,
            "diff_pct": diff_pct,
            "span_days": span_days,
            "times": times,
            "norm_values": norm_values,
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

    for idx, item in enumerate(items):
        color = HIGH_CONTRAST_COLORS[idx % len(HIGH_CONTRAST_COLORS)]
        linestyle = LINE_STYLES[(idx // len(HIGH_CONTRAST_COLORS)) % len(LINE_STYLES)]

        sign = "+" if item["diff_pct"] >= 0 else ""
        ev_short = shorten_text(item["title"], 32)
        cand_short = shorten_text(item["candidate"], 18)
        label = safe_mpl_label(
            f"#{item['rank']:02d} {ev_short} [{cand_short}] "
            f"({item['base_prob_pct']:.1f}%→{item['curr_prob_pct']:.1f}% | {sign}{item['diff_pct']:.1f}%)"
        )
        # matplotlib 側の naive 表示用に tz を除いた JST 時刻を使う
        naive_times = [t.replace(tzinfo=None) for t in item["times"]]
        ax.plot(
            naive_times,
            item["norm_values"],
            label=label,
            color=color,
            linestyle=linestyle,
            linewidth=2.0,
            alpha=0.92,
        )
        ax.scatter(
            [naive_times[-1]],
            [item["norm_values"][-1]],
            color=color,
            s=22,
            zorder=5,
        )

    ax.axhline(100.0, color="#555555", linestyle="--", linewidth=1.2, alpha=0.75, label="基準ライン (100%)")
    ax.set_title(
        f"Polymarket 24h出来高 {rank_label} — 直近1ヶ月 ノーマライズ推移 (開始=100%) [{generated_at_jst.strftime('%Y-%m-%d %H:%M JST')}]",
        fontsize=13,
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


def build_discord_caption(items: List[Dict[str, Any]], rank_label: str, generated_at_jst: datetime) -> str:
    """各チャート画像に添える Discord サマリーテキスト（2000文字以内）を作成"""
    header = (
        f"📊 **【Polymarket 日次トレンド】 24h出来高 {rank_label} (1ヶ月ノーマライズ推移)**\n"
        f"取得時刻: `{generated_at_jst.strftime('%Y-%m-%d %H:%M JST')}` | 基準: 各銘柄の開始時点=100%"
    )
    lines = [header, "```text"]
    for item in items:
        sign = "+" if item["diff_pct"] >= 0 else ""
        ev_s = shorten_text(item["title"], 30)
        cand_s = shorten_text(item["candidate"], 15)
        lines.append(
            f"#{item['rank']:02d} {ev_s:<30} [{cand_s:<15}] "
            f"{item['base_prob_pct']:4.1f}%->{item['curr_prob_pct']:4.1f}% ({sign}{item['diff_pct']:5.1f}%) "
            f"| 24h:${item['volume24h']:,.0f}"
        )
    lines.append("```")
    text = "\n".join(lines)
    if len(text) > 1950:
        text = text[:1940] + "\n```"
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
    parser.add_argument("--channel", type=str, default=None, help="送信先Webhook名 (例: real1_bitbank, test4_test)")
    parser.add_argument("--test", action="store_true", help="test4_test チャンネルへ強制送信")
    parser.add_argument("--dry-run", action="store_true", help="画像生成のみ行い Discord 送信しない")
    args = parser.parse_args()

    now_jst = datetime.now(JST)
    date_tag = now_jst.strftime("%Y%m%d_%H%M%S")
    plots_dir = BASE_DIR / "Data" / "plots"

    print(f"[{now_jst.strftime('%Y-%m-%d %H:%M:%S JST')}] Polymarket 24h出来高 Top {args.top_n} のデータを取得中...")
    items = collect_top_normalized_markets(
        top_n=args.top_n,
        min_days=args.min_days,
        lookback_days=args.days,
    )
    if not items:
        print("❌ 有効なマーケットデータを取得できませんでした。")
        return 1

    print(f"✅ 取得完了: {len(items)} 銘柄")
    for item in items:
        sign = "+" if item["diff_pct"] >= 0 else ""
        print(
            f"  #{item['rank']:02d} 24h=${item['volume24h']:>10,.0f} ({item['span_days']:4.1f}d) | "
            f"{item['base_prob_pct']:5.1f}%->{item['curr_prob_pct']:5.1f}% ({sign}{item['diff_pct']:6.1f}%) | "
            f"{item['title']} [{item['candidate']}]"
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
        caption = build_discord_caption(sub_items, rank_label, now_jst)
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
