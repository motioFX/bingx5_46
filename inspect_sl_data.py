import pickle

with open("Data/plots/sl_scan_results.pkl", "rb") as f:
    res = pickle.load(f)

for sl_val in [3.0, 5.0, 8.0, 9.0, 10.0, 12.0]:
    data = res[sl_val]
    print(f"SL {sl_val:4.1f}% | Net Return: {data['total_return_pct']:6.2f}% | WinRate: {data['win_rate']:5.1f}% | Trades: {data['total_trades']:4d} | Stopped: {data['stopped_out']:3d} | MaxDD: {data['max_dd_pct']:5.2f}%")

print("\n=== SL 9.0% Symbols ===")
opt = res[9.0]
for sym, d in sorted(opt["symbols"].items(), key=lambda x: x[1]["pnl_pct"], reverse=True):
    print(f"{sym:12s} | PnL: {d['pnl_pct']:6.2f}% | WinRate: {d['win_rate']:5.1f}% | Trades: {d['trades']:3d} | Stopped: {d['stopped']:2d}")
