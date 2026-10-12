import pandas as pd
import numpy as np
from test_drawdown_solutions import test_config

print("=== 【下落の凹みを消し去り、原資2倍を安全に達成するパラメータ探索】 ===")

# max_stages=4, nanpin_drop=0.030, use_trend_filter=True, cooldown=24h
for init_b in [100.0, 150.0, 200.0]:
    for margin in [5.0, 7.5, 10.0]:
        for stages in [3, 4]:
            for np_d in [0.025, 0.030]:
                res = test_config(
                    initial_balance=init_b,
                    fixed_margin=margin,
                    max_stages=stages,
                    nanpin_drop=np_d,
                    sl_pct=0.090,
                    use_trend_filter=True, # 24h EMA トレンドフィルター
                    cooldown_bars=288      # 24h クールダウン
                )
                status = "EXCELLENT" if (res["min_eq"] >= init_b * 0.7 and res["return_pct"] >= 100.0) else (
                    "GOOD" if (res["min_eq"] >= init_b * 0.5 and res["return_pct"] >= 80.0) else "OTHER"
                )
                if status in ["EXCELLENT", "GOOD"]:
                    print(f"[{status:9s}] 原資:${init_b:3.0f} | 証拠金:${margin:4.1f} | 回数:{stages} | 難平:{np_d*100:3.1f}% | 利回り:{res['return_pct']:+6.1f}% (+${res['profit']:5.1f}) | 最小残高:${res['min_eq']:5.1f} | MaxDD:{res['max_dd']:4.1f}% | SL:{res['sl_trades']}回")
