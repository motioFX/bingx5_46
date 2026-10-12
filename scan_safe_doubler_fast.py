#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
import time
import pandas as pd
import numpy as np
from test_bot_portfolio_fast import load_data, run_realistic_bot_sim

mp_dict = load_data()

print("================================================================================", flush=True)
print(" 【資金管理・ナンピン・証拠金スキャン: 原資2倍 & 破綻ゼロの黄金比率探索】", flush=True)
print("================================================================================", flush=True)

test_grid = [
    # (init_b, margin, stages, nanpin_d, sl)
    # 1. 証拠金 $5 (レバ10倍で$50/注文)
    (100.0, 5.0, 3, 0.020, 0.085),
    (100.0, 5.0, 4, 0.020, 0.085),
    (100.0, 5.0, 5, 0.020, 0.085),
    (100.0, 5.0, 3, 0.025, 0.085),
    (100.0, 5.0, 4, 0.025, 0.085),
    (100.0, 5.0, 5, 0.025, 0.085),
    (100.0, 5.0, 4, 0.020, 0.090),
    (100.0, 5.0, 4, 0.025, 0.090),

    # 2. 証拠金 $7.5 (レバ10倍で$75/注文)
    (100.0, 7.5, 3, 0.020, 0.085),
    (100.0, 7.5, 4, 0.020, 0.085),
    (100.0, 7.5, 3, 0.025, 0.085),
    (100.0, 7.5, 4, 0.025, 0.085),
    (100.0, 7.5, 4, 0.020, 0.090),
    (100.0, 7.5, 4, 0.025, 0.090),

    # 3. 証拠金 $10 (レバ10倍で$100/注文)
    (100.0, 10.0, 3, 0.020, 0.085),
    (100.0, 10.0, 3, 0.025, 0.085),
    (100.0, 10.0, 4, 0.025, 0.085),
    (150.0, 10.0, 4, 0.020, 0.085),
    (150.0, 10.0, 4, 0.025, 0.085),
    (150.0, 10.0, 4, 0.020, 0.090),
    (150.0, 10.0, 4, 0.025, 0.090),

    # 4. 証拠金 $15 (ボット初期設定)
    (150.0, 15.0, 3, 0.025, 0.085),
    (200.0, 15.0, 3, 0.025, 0.085),
    (200.0, 15.0, 4, 0.025, 0.085),
    (200.0, 15.0, 4, 0.025, 0.090),
]

results = []

for init_b, margin, stages, nanpin_d, sl in test_grid:
    res = run_realistic_bot_sim(
        mp_dict,
        initial_balance=init_b,
        margin_per_stage=margin,
        leverage=10.0,
        max_active_positions=2,
        target_ratio=0.60,
        min_va_width=0.015,
        nanpin_drop=nanpin_d,
        sl_pct=sl,
        total_max_stages=stages
    )
    eq = np.array(res["equity_curve"])
    min_eq = float(np.min(eq))
    results.append({
        "init_b": init_b,
        "margin": margin,
        "stages": stages,
        "nanpin_d": nanpin_d * 100,
        "sl": sl * 100,
        "profit": res["net_profit"],
        "return_pct": res["return_pct"],
        "min_eq": min_eq,
        "max_dd": res["max_dd_pct"],
        "wr": res["win_rate"],
        "trades": res["trades"],
        "sl_count": res["sl_trades"]
    })
    status = "OK" if min_eq > 0 else "BURST"
    print(f"[{status:5s}] 原資:${init_b:3.0f} | 証拠金:${margin:4.1f} | 回数:{stages}回 | 難平:{nanpin_d*100:3.1f}% | SL:{sl*100:4.1f}% | 利回り:{res['return_pct']:+6.1f}% (${res['net_profit']:+5.1f}) | 最小残高:${min_eq:5.1f} | DD:{res['max_dd_pct']:4.1f}% | 勝率:{res['win_rate']:4.1f}%", flush=True)

df_res = pd.DataFrame(results)
safe_doublers = df_res[(df_res["min_eq"] > 0) & (df_res["return_pct"] >= 100.0)]

print(f"\n安全に原資2倍 (利回り >= 100% & 破綻ゼロ) を達成した設定数: {len(safe_doublers)} 通り\n")
if len(safe_doublers) > 0:
    print("=== 【安全に原資2倍を達成した黄金設定一覧】(利回り順) ===")
    for idx, r in safe_doublers.sort_values(by="return_pct", ascending=False).iterrows():
        print(f"★ 原資:${r['init_b']:3.0f} | 1回証拠金:${r['margin']:4.1f} (建玉${r['margin']*10:3.0f}) | 最大{r['stages']}回 | 難平幅:{r['nanpin_d']:3.1f}% | SL:{r['sl']:4.1f}% | 利回り:{r['return_pct']:+6.1f}% (+${r['profit']:5.1f}) | 最小残高:${r['min_eq']:5.1f} | MaxDD:{r['max_dd']:4.1f}% | 勝率:{r['wr']:4.1f}%")
