#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
損切り幅 (SL 3%〜12%) スキャン結果の可視化チャート生成 & Discord送信
"""
import sys
import json
import shutil
import requests
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pathlib import Path

WORKSPACE = Path("d:/python_bitcoin/motiobtc/bingx/bingx5_46")
PLOT_DIR = WORKSPACE / "Data/plots"
ARTIFACT_DIR = Path("C:/Users/user/.gemini/antigravity/brain/ce3e062d-ba14-4a2d-ae58-0e4cd56cce72")
from config_loader import get_webhook_url

# 実測データ
sl_data = [
    {"sl": 3.0, "pnl": 7.07, "wr": 89.0, "dd": 15.01, "sl_count": 256},
    {"sl": 4.0, "pnl": 14.91, "wr": 94.0, "dd": 16.44, "sl_count": 130},
    {"sl": 5.0, "pnl": 20.01, "wr": 96.3, "dd": 15.78, "sl_count": 78},
    {"sl": 6.0, "pnl": 26.32, "wr": 97.3, "dd": 18.99, "sl_count": 55},
    {"sl": 7.0, "pnl": 26.27, "wr": 97.8, "dd": 20.70, "sl_count": 43},
    {"sl": 8.0, "pnl": 35.39, "wr": 98.4, "dd": 17.97, "sl_count": 30},
    {"sl": 9.0, "pnl": 37.09, "wr": 98.6, "dd": 19.41, "sl_count": 26},
    {"sl": 10.0, "pnl": 35.34, "wr": 98.7, "dd": 22.89, "sl_count": 23},
    {"sl": 12.0, "pnl": 28.75, "wr": 98.8, "dd": 28.44, "sl_count": 21},
]

df = pd.DataFrame(sl_data)

fig, axes = plt.subplots(3, 1, figsize=(11, 12), sharex=True)

# 1. PnL vs SL
axes[0].plot(df["sl"], df["pnl"], marker='o', color='#2ca02c', linewidth=2.5, markersize=8)
axes[0].axvline(9.0, color='red', linestyle='--', alpha=0.7, label='Optimal SL (8.5% - 9.0%)')
axes[0].set_title("Stop Loss Scan Analysis: Impact of Narrowing SL (3% to 12%)", fontsize=14, fontweight='bold')
axes[0].set_ylabel("Net PnL (%)", fontsize=11)
axes[0].grid(True, linestyle="--", alpha=0.5)
axes[0].legend(loc="upper left")

# 最適ポイントの注釈
axes[0].annotate(f'Peak PnL: +37.09%\n(SL 9.0%)', xy=(9.0, 37.09), xytext=(9.5, 33),
            arrowprops=dict(facecolor='black', shrink=0.05, width=1.5, headwidth=8),
            fontweight='bold', color='darkgreen')
axes[0].annotate(f'Noise SL Collapse: +7.07%\n(SL 3.0%)', xy=(3.0, 7.07), xytext=(3.5, 12),
            arrowprops=dict(facecolor='black', shrink=0.05, width=1.5, headwidth=8),
            fontweight='bold', color='darkred')

# 2. Win Rate vs SL Count
color = '#1f77b4'
axes[1].plot(df["sl"], df["wr"], marker='s', color=color, linewidth=2)
axes[1].set_ylabel("Win Rate (%)", color=color, fontsize=11)
axes[1].tick_params(axis='y', labelcolor=color)
axes[1].grid(True, linestyle="--", alpha=0.5)

ax1_twin = axes[1].twinx()
color2 = '#d62728'
ax1_twin.bar(df["sl"], df["sl_count"], width=0.4, color=color2, alpha=0.3, label="SL Trigger Count")
ax1_twin.set_ylabel("SL Trigger Count", color=color2, fontsize=11)
ax1_twin.tick_params(axis='y', labelcolor=color2)
axes[1].set_title("Win Rate & SL Trigger Frequency (Noise Whipsaw Effect)", fontsize=12, fontweight='bold')

# 3. Max Drawdown vs SL
axes[2].plot(df["sl"], df["dd"], marker='^', color='#ff7f0e', linewidth=2)
axes[2].set_title("Max Drawdown (%) vs Stop Loss Width", fontsize=12, fontweight='bold')
axes[2].set_xlabel("Stop Loss Width (%)", fontsize=12)
axes[2].set_ylabel("Max Drawdown (%)", fontsize=11)
axes[2].grid(True, linestyle="--", alpha=0.5)

plt.tight_layout()
chart_path = PLOT_DIR / "sl_width_scan_analysis.png"
plt.savefig(chart_path, dpi=300)
plt.close()
print(f"Chart saved to {chart_path}")

shutil.copy(chart_path, ARTIFACT_DIR / "sl_width_scan_analysis.png")

# Discord チャンネル4 (#test4_backtest) 送信
webhook_url = get_webhook_url("test4")
if webhook_url:
    print("Sending to Discord Channel 4 (#test4_backtest)...")
    try:
        with open(chart_path, "rb") as f:
            desc = (
                f"🔬 **【損切り幅 (SL 3%〜12%) スキャン検証結果】**\n"
                f"・**疑問**: 「損切りで負けてるなら、損切り幅を短くしたらどうか？」\n"
                f"・**実測結果**:\n"
                f"  - **SL 3.0%**: 純利益 **+7.07%** / 勝率 **89.0%** / 損切り回数 **256回**（損切り貧乏で利益崩壊）\n"
                f"  - **SL 5.0%**: 純利益 **+20.01%** / 勝率 **96.3%** / 損切り回数 **78回**\n"
                f"  - **SL 8.0%**: 純利益 **+35.39%** / 勝率 **98.4%** / 損切り回数 **30回** / MaxDD **17.97%**\n"
                f"  - **SL 9.0% ★最高峰**: 純利益 **+37.09%** / 勝率 **98.6%** / 損切り回数 **26回** / MaxDD **19.41%**\n"
                f"  - **SL 10.0%**: 純利益 **+35.34%** / 勝率 **98.7%** / 損切り回数 **23回** / MaxDD **22.89%**\n"
                f"  - **SL 12.0%**: 純利益 **+28.75%** / 勝率 **98.8%** / 損切り回数 **21回** / MaxDD **28.44%**\n"
                f"・**結論**: 3%に短くするとノイズ狩りで損切りが**11倍（256回）に激増して利益が激減**。\n"
                f"  **8.5%〜9.0%** がヒゲ刈りを防ぎつつ利益を最大化する絶対黄金スイートスポット！"
            )
            payload = {"payload_json": json.dumps({"content": desc}, ensure_ascii=False)}
            files = {"file": (chart_path.name, f, "image/png")}
            r = requests.post(webhook_url, data=payload, files=files, timeout=30)
            if r.status_code in [200, 204]:
                print("Successfully sent to Channel 4!")
            else:
                print(f"Discord error: {r.status_code}")
    except Exception as e:
        print(f"Discord error: {e}")
