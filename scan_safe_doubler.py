import time
import pandas as pd
import numpy as np
from test_bot_portfolio_fast import load_data, run_realistic_bot_sim

mp_dict = load_data()

print("=" * 80)
print(" 【資金管理・ナンピン・証拠金スキャン: 原資2倍 & 破綻ゼロの黄金比率探索】")
print("=" * 80)

results = []

# パラメータグリッド
for init_b in [100.0, 150.0, 200.0]:
    for margin in [3.0, 5.0, 8.0, 10.0]: # 1回あたりの証拠金
        for max_stages in [3, 4, 5]: # 最大ナンピン回数
            for nanpin_d in [0.020, 0.025, 0.030]: # ナンピン幅
                for sl in [0.085, 0.090]:
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
                        total_max_stages=max_stages
                    )
                    eq = np.array(res["equity_curve"])
                    min_eq = float(np.min(eq))
                    
                    # 破綻した（MinEquity <= 0）ものは除外またはフラグ
                    results.append({
                        "init_b": init_b,
                        "margin": margin,
                        "stages": max_stages,
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

df_res = pd.DataFrame(results)

# 破綻ゼロ (min_eq > 0) かつ 原資2倍 (return_pct >= 100%) をフィルタ
safe_doublers = df_res[(df_res["min_eq"] > 0) & (df_res["return_pct"] >= 100.0)]

print(f"\n全試行数: {len(df_res)} 通り")
print(f"安全に原資2倍 (利回り >= 100% & 破綻ゼロ) を達成した設定数: {len(safe_doublers)} 通り\n")

if len(safe_doublers) > 0:
    print("=== 【原資2倍達成 TOP 15 設定】(利回り順) ===")
    top15 = safe_doublers.sort_values(by="return_pct", ascending=False).head(15)
    for idx, r in top15.iterrows():
        print(f"原資: ${r['init_b']:3.0f} | 証拠金: ${r['margin']:4.1f} | 回数: {r['stages']}回 | ナンピン: {r['nanpin_d']:3.1f}% | SL: {r['sl']:4.1f}% | 利回り: {r['return_pct']:+6.1f}% (利益: ${r['profit']:+5.1f}) | 最小残高: ${r['min_eq']:5.1f} | MaxDD: {r['max_dd']:4.1f}% | 勝率: {r['wr']:4.1f}% (SL: {r['sl_count']}回)")
else:
    print("条件を満たす設定がありませんでした。TOP 10 を表示:")
    print(df_res.sort_values(by="return_pct", ascending=False).head(10))
