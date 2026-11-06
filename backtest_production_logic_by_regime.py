"""本番ロジック整合バックテスト (全6戦略: VAH_BREAK / VAL_BREAK / VAH_POC / VAL_POC / RSIMA / RSIMA_SHORT)
本番 bingx5_46_3logic.py の VP算出 / シミュレータ(simulate_*_fast, build_rsima3*_chart_df) /
Volumeフィルター(build_entry_precision_filter) をそのまま使い、地合い(30d/10d/5d)別に成績を集計する。
"""
import sys, glob, os, time
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
import bingx5_46_3logic as L
from scratch.verify_numba_vp import make_mp_fast

L.time.sleep = lambda x: None
L.make_market_profile_dual_va = make_mp_fast

MP, BINS, STEP, TR = int(os.environ.get("MP", 300)), 50, 8, float(os.environ.get("TR", 0.70))
SIZE = 15.0
STRATS = {  # name: (sim_fn, direction, is_breakout)
    "VAH_BREAK": (L.simulate_vah_break_fast, "long", True),
    "VAL_BREAK": (L.simulate_val_break_fast, "short", True),
    "VAH_POC":   (L.simulate_vah_poc_fast, "short", False),
    "VAL_POC":   (L.simulate_val_poc_fast, "long", False),
}

reg = pd.read_csv("Data/market_regime_5m_from_2023.csv")
reg["dt"] = pd.to_datetime(reg["dt"], utc=True)
reg["code"] = (reg.state_30d.str[0] + "," + reg.state_10d.str[0] + "," + reg.state_5d.str[0])
reg = reg.set_index("dt")
t0 = reg.index[0]

rows = []
files = sorted(glob.glob("Data/binance_candles_5m_4y/*.pkl"))
for f in files:
    sym = os.path.basename(f).replace("_5m.pkl", "").replace("USDT", "")
    if sym in ("BTC", "ETH"):
        continue
    df = pd.read_pickle(f)
    df["dt"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df[df.dt >= t0].drop_duplicates("dt").reset_index(drop=True)
    if len(df) < 5000:
        continue
    ts = time.time()
    mp = make_mp_fast(df, period=MP, num_bins=BINS, step=STEP, use_decay=True)
    c = df.close.astype(float).values; h = df.high.astype(float).values; l = df.low.astype(float).values
    code = reg["code"].reindex(df.dt, method="ffill").values
    for name, (fn, side, brk) in STRATS.items():
        flt = L.build_entry_precision_filter(df, side=side, is_vp=True, is_breakout=brk)
        sim = fn(c, h, l, mp["val_70"], mp["vah_70"], mp["poc"], flt, initial_equity=SIZE, max_trades=1,
                 cooldown_bars=288, timeout_bars=48, target_ratio=TR, record_chart=True)
        curve = np.asarray(sim["pnl_curve"], float)
        d = np.diff(curve, prepend=curve[0])
        closes = np.where(np.abs(d) > 1e-12)[0]
        ent_bars = np.where(~np.isnan(np.asarray(sim["exec_buy_price"], float)))[0] if side == "long" else \
                   np.where(~np.isnan(np.asarray(sim["exec_sell_price"], float)))[0]
        for cb in closes:
            j = np.searchsorted(ent_bars, cb, side="right") - 1
            eb = ent_bars[j] if j >= 0 else cb
            rows.append((sym, name, side, df.dt.iloc[eb], code[eb], d[cb]))

    # RSIMA (long, EP=35, CP=65) & RSIMA_SHORT (short, EP=65, CP=35)
    rsi_s, rsi_ma_s = L.calc_rsi_ema(df["close"].astype(float), rsi_period=9, ma_period=7)
    rsi_v = rsi_s.values; rsi_ma_v = rsi_ma_s.values
    for rname, rside, ep, cp in [("RSIMA", "long", 35.0, 65.0), ("RSIMA_SHORT", "short", 65.0, 35.0)]:
        flt_r = L.build_entry_precision_filter(df, side=rside, is_vp=False, is_breakout=False)
        if rside == "long":
            cdf = L.build_rsima3_chart_df(df, rsi_v, rsi_ma_v, ep, cp, 0.025, initial_equity=SIZE, max_trades=1, ma_filter=flt_r, cooldown_bars=288, timeout_bars=48)
            ent_bars = np.where(~np.isnan(cdf["exec_buy_price"].values))[0]
            ex_bars = np.where(~np.isnan(cdf["exec_sell_price"].values))[0]
        else:
            cdf = L.build_rsima3_short_chart_df(df, rsi_v, rsi_ma_v, ep, cp, 0.025, initial_equity=SIZE, ma_filter=flt_r, cooldown_bars=288, timeout_bars=48)
            ent_bars = np.where(~np.isnan(cdf["exec_sell_price"].values))[0]
            ex_bars = np.where(~np.isnan(cdf["exec_buy_price"].values))[0]
        # realized pnl at ex_bars
        curve = cdf["pnl"].values
        prev_realized = SIZE
        for cb in ex_bars:
            j = np.searchsorted(ent_bars, cb, side="right") - 1
            eb = ent_bars[j] if j >= 0 else cb
            pnl_t = curve[cb] - prev_realized
            prev_realized = curve[cb]
            rows.append((sym, rname, rside, df.dt.iloc[eb], code[eb], pnl_t))

    print(f"{sym}: {time.time()-ts:.2f}s", flush=True)

tr = pd.DataFrame(rows, columns=["sym", "strat", "side", "edt", "regime", "pnl"])
tr["edt_jst"] = tr.edt.dt.tz_convert("Asia/Tokyo")
tr["hour"] = tr.edt_jst.dt.hour
tr.to_csv("scratch/prod_logic_trades.csv", index=False, encoding="utf-8")
print("total trades", len(tr))

def stat(g):
    w = g.pnl[g.pnl > 0].sum(); ls = -g.pnl[g.pnl <= 0].sum()
    return pd.Series({"n": int(len(g)), "win%": round((g.pnl > 0).mean() * 100, 1), "sum$": round(g.pnl.sum(), 2),
                      "avg$": round(g.pnl.mean(), 4), "PF": round(w / ls, 2) if ls > 0 else np.nan})

print("\n=== 戦略別 全期間 (全6戦略) ===")
print(tr.groupby("strat", observed=True).apply(stat, include_groups=False).to_string())

print("\n=== VAL_BREAK 地合い別 (n>=30) ===")
vb = tr[tr.strat == "VAL_BREAK"].groupby("regime", observed=True).apply(stat, include_groups=False)
print(vb[vb.n >= 30].sort_values("sum$", ascending=False).to_string())

tr["half"] = np.where(tr.edt_jst < "2025-01-01", "2023-24", "2025-26")
print("\n=== 3候補パターン (N,N,B / N,N,S / N,S,S) 全6戦略 期間分割 ===")
tgt = tr[tr.regime.isin(["N,N,B", "N,N,S", "N,S,S"])]
print(tgt.groupby(["regime", "strat", "half"], observed=True).apply(stat, include_groups=False).to_string())

tr["session"] = pd.cut(tr.hour, [-1, 8, 15, 21, 23], labels=["夜0-9", "東京9-16", "欧州16-22", "夜22-24"])
print("\n=== 3候補パターン × 許可戦略 セッション別 ===")
sub = tr[
    ((tr.regime == "N,N,B") & (tr.strat == "VAH_BREAK")) |
    ((tr.regime.isin(["N,N,S", "N,S,S"])) & (tr.strat.isin(["VAL_BREAK", "VAH_POC", "RSIMA_SHORT"])))
]
print(sub.groupby(["regime", "strat", "session"], observed=True).apply(stat, include_groups=False).to_string())
