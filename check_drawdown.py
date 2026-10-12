import pandas as pd
import numpy as np
from test_bot_portfolio_fast import load_data, run_realistic_bot_sim

mp_dict = load_data()

for init_b in [100.0, 200.0, 300.0, 500.0]:
    for sl in [0.085, 0.090]:
        res = run_realistic_bot_sim(
            mp_dict,
            initial_balance=init_b,
            margin_per_stage=15.0,
            leverage=10.0,
            max_active_positions=2,
            sl_pct=sl
        )
        eq = np.array(res["equity_curve"])
        min_eq = float(np.min(eq))
        print(f"Init: ${init_b:4.0f} | SL: {sl*100:4.1f}% | NetProfit: ${res['net_profit']:+7.1f} ({res['return_pct']:+6.1f}%) | MinEquity: ${min_eq:6.1f} | MaxDD: {res['max_dd_pct']:5.1f}% | Trades: {res['trades']} (SL: {res['sl_trades']})")
