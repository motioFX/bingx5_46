import pandas as pd
import numpy as np
from test_bot_portfolio_fast import load_data, run_realistic_bot_sim

mp_dict = load_data()

print("=== 原資 $200〜$400 における高安全性 & 原資2倍達成検証 ===")
for init_b in [200.0, 300.0, 400.0]:
    for margin in [5.0, 7.5, 10.0]:
        for stages in [3, 4]:
            res = run_realistic_bot_sim(
                mp_dict,
                initial_balance=init_b,
                margin_per_stage=margin,
                leverage=10.0,
                max_active_positions=2,
                target_ratio=0.60,
                min_va_width=0.015,
                nanpin_drop=0.025,
                sl_pct=0.090,
                total_max_stages=stages
            )
            eq = np.array(res["equity_curve"])
            min_eq = float(np.min(eq))
            status = "SAFE" if min_eq > init_b * 0.3 else ("RISKY" if min_eq > 0 else "BURST")
            print(f"[{status:5s}] Init: ${init_b:3.0f} | Margin: ${margin:4.1f} | Stages: {stages} | Return: {res['return_pct']:+6.1f}% (+${res['net_profit']:5.1f}) | MinEq: ${min_eq:5.1f} | MaxDD: {res['max_dd_pct']:4.1f}% | Trades: {res['trades']} (SL: {res['sl_trades']})")
