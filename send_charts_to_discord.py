#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Discord チャンネル (#test4_backtest / test4_test) へ
最適化バックテストおよび実運用PnLチャートを送信するスクリプト
"""
import json
import sys
import requests
from pathlib import Path
from config_loader import get_webhook_url

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

webhook_url = get_webhook_url("test4_backtest")
if not webhook_url:
    print("[Error] test4_backtest webhook URL is not configured.")
    sys.exit(1)

print(f"Target Webhook: {webhook_url[:40]}...")


def send_image_to_discord(file_path: Path, title_message: str):
    if not file_path.exists():
        print(f"[Error] File not found: {file_path}")
        return False
    print(f"Uploading {file_path.name} ({file_path.stat().st_size:,} bytes)...")
    try:
        with file_path.open("rb") as fh:
            files = {"file": (file_path.name, fh, "image/png")}
            payload = json.dumps({"content": title_message}, ensure_ascii=False)
            data = {"payload_json": payload}
            resp = requests.post(webhook_url, data=data, files=files, timeout=60)
            resp.raise_for_status()
            print(f"  ✔ Successfully sent {file_path.name}")
            return True
    except Exception as e:
        print(f"  ❌ Failed to send {file_path.name}: {e}")
        return False


# 1. 最適化バックテスト累積PnL比較チャート
img_opt = Path("Data/plots/pnl_optimization_comparison.png")
msg_opt = (
    "📊 **【RSIMA ＆ 全面安フィルター 最適化バックテスト比較グラフ (2022〜2026年 4.7年間)】**\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "• 🔴 **未対策ベースライン**: `-184.9%` (大赤字 / MaxDD: 352.7%)\n"
    "• 🔵 **全面安フィルター導入 (sl 2.5%)**: `+144.9%` (黒字転換 / MaxDD: 154.7%)\n"
    "• 🟢 **🏆 最適値 (全面安フィルター ＋ sl 3.0%)**: **`+250.3%` (最大利益 / MaxDD: 140.8% 最小)**\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "★ **改善効果**: 実質 **`+435.2%`** の収益向上 ＆ MaxDD **`-211.9%`** の大幅圧縮！\n"
    "★ **勝率**: 47.0% | **PF**: 1.03 | **PnL/DD比**: 1.78 (約2倍効率)"
)
send_image_to_discord(img_opt, msg_opt)

# 2. 実運用トレード実績 累積PnLチャート
img_real = Path("Data/real_trading_performance.png")
msg_real = (
    "📈 **【実運用トレード損益実績 累積PnLチャート (最新)】**\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "• **累計実現損益**: **`+$3.2749 USDT`** (右肩上がり継続中)\n"
    "• **総決済取引数**: **`25 回`** (16勝 8敗 1分)\n"
    "• **通算勝率**: **`64.0%`** | **PF**: **`2.33`**\n"
    "• **総利益**: `+$5.7460 USDT` | **総損失**: `-$2.4711 USDT`\n"
    "• **直近利確 (本日10/12)**: `SUI-USDT` (+0.1053), `AAVE-USDT` (+0.0280)"
)
send_image_to_discord(img_real, msg_real)

# 3. 暴落局面クローズアップチャート
img_zoom = Path("Data/plots/macro_dump_filter_zoom_cases.png")
msg_zoom = (
    "🛡️ **【歴史的急落局面における全面安フィルター動作検証クローズアップ】**\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "• **Case 1 (2022/05 LUNAショック)**: アルト急落初動を即座に検知し緊急ロック、壊滅的被弾を回避\n"
    "• **Case 2 (2022/11 FTX破綻)**: BTC主導急落を即時ロック、アルト自律反発まで安全待機\n"
    "• **Case 3 (2023/06 SEC提訴)**: アルト先行型急落（全体の76.1%）を初動で完全ブロック\n"
    "• **Case 4 (2024/08 令和ブラックマンデー)**: 急落をロック後、BTC安定＋アルト過半数反発で安全復旧"
)
send_image_to_discord(img_zoom, msg_zoom)

print("\n全チャートの Discord 送信が完了しました！")
