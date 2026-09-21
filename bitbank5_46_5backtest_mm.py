#!/usr/bin/env python
# coding: utf-8

import time
import glob
import datetime
import copy
import pandas as pd
import numpy as np
import sys


class AirExchange:
    def __init__(self):
        self.positions = []
        self.orders = []
        self.exec_history = []
        self.positions = {"avgEntry":0, "qty":0, "pos":0}

    def _rm_order(self, order, fill_timestamp):
        executed = copy.copy(order)
        executed["timestamp"] = fill_timestamp
        self._add_order_to_position(executed)
        self.exec_history.append(executed)
        self.orders.remove(order)

    def check_order(self, high, low, timestamp=None):
        if len(self.orders):
            tmp_orders = copy.copy(self.orders)
            for i, order in enumerate(copy.copy(self.orders)):
                fill_timestamp = timestamp if timestamp is not None else order["timestamp"]
                if order["ord_type"] == "Limit":
                    if order["side"] == "Buy":
                        if order["price"] >= low:
                            self._rm_order(order, fill_timestamp)
                    else:
                        if order["price"] <= high:
                            self._rm_order(order, fill_timestamp)
                elif order["ord_type"] == "Stop":
                    if order["side"] == "Buy":
                        if order["price"] >= high:
                            self._rm_order(order, fill_timestamp)
                    else:
                        if order["price"] <= low:
                            self._rm_order(order, fill_timestamp)

    def _add_order_to_position(self, order):
        qty = self.positions["qty"]
        avg = self.positions["avgEntry"]
        pos = self.positions["pos"]
        size = order["size"]
        price = order["price"]
        new_qty = qty + size
        if new_qty == 0:
            new_avg = 0
            new_pos = 0
        else:
            if qty * size >= 0:
                new_avg = (qty * avg + size * price) / (qty + size)
                new_pos = pos + 1
            else:
                if qty >= 0:
                    if new_qty > 0:
                        new_avg = avg
                    else:
                        new_avg = price
                else:
                    if new_qty > 0:
                        new_avg = price
                    else:
                        new_avg = avg
                new_pos = pos - 1
        self.positions["qty"] = new_qty
        self.positions["avgEntry"] = new_avg
        self.positions["pos"] = new_pos
        
    def set_order(self, id_, timestamp, size, side, ord_type, price, close):
        if side == "Buy":
            if ord_type == "Limit":
                if close > price:
                    self.orders.append({
                        "id":id_, 
                        "timestamp":timestamp, 
                        "size":size,
                        "side":side,
                        "ord_type":ord_type,
                        "price":price
                    })
                else:
                    self.set_market_order(id_, timestamp, size, side, close)
            elif ord_type == "Stop":
                if close < price:
                    self.orders.append({
                        "id":id_, 
                        "timestamp":timestamp, 
                        "size":size,
                        "side":side,
                        "ord_type":ord_type,
                        "price":price
                    })
                else:
                    self.set_market_order(id_, timestamp, size, side, close)
        else:
            if ord_type == "Limit":
                if close < price:
                    self.orders.append({
                        "id":id_, 
                        "timestamp":timestamp, 
                        "size":size,
                        "side":side,
                        "ord_type":ord_type,
                        "price":price
                    })
                else:
                    self.set_market_order(id_, timestamp, size, side, close)
            elif ord_type == "Stop":
                if close > price:
                    self.orders.append({
                        "id":id_, 
                        "timestamp":timestamp, 
                        "size":size,
                        "side":side,
                        "ord_type":ord_type,
                        "price":price
                    })
                else:
                    self.set_market_order(id_, timestamp, size, side, close)
                
    def set_market_order(self, id_, timestamp, size, side, close):
        order = {
            "id":id_, 
            "timestamp":timestamp, 
            "size":size,
            "side":side,
            "ord_type":"Market",
            "price":close
        }
        self._add_order_to_position(order)
        self.exec_history.append(order)
        
    def get_orders(self):
        return self.orders

    def cancel_order(self, idx):
        del self.orders[idx]

    def cancel_all_orders(self):
        self.orders = []

    def get_position(self):
        return self.positions


class Backtest:
    def __init__(self, air_exchange, df, size=100, columns = None):
        self.air_exchange = air_exchange
        self.size = size
        if columns:
            self.df = df[["timestamp"] + columns+ [ "close", "high", "low"]].dropna()
        else:
            self.df = df[["timestamp", "close", "high", "low"]].dropna()

        self.arrays = self.df.to_numpy()
        self.previous_array = None
        self.idx = 0
        
        self.column_dic = {}
        self._init_column_idxs()
        self.t = 0
        self.c = None
        self.h = None
        self.l = None
        self.t_idx = self.column_dic["timestamp"]
        self.c_idx = self.column_dic["close"]
        self.h_idx = self.column_dic["high"]
        self.l_idx = self.column_dic["low"]
        
    def _init_column_idxs(self):
        for col in self.df.columns:
            self.column_dic[col] = list(self.df.columns).index(col)
        
    def _get_position(self):
        return self.air_exchange.positions
    
    def _get_orders(self):
        return self.air_exchange.orders
    
    def _cancel_all_orders(self):
        self.air_exchange.cancel_all_orders()
    
    def _limit_order(self, size, price):
        side = "Buy" if size > 0 else "Sell"
        self.air_exchange.set_order(id_=self.idx, timestamp=self.t, size=size, side=side, ord_type="Limit", price=price, close=self.c)
    
    def _stop_order(self, size, price):
        side = "Buy" if size > 0 else "Sell"
        self.air_exchange.set_order(id_=self.idx, timestamp=self.t, size=size, side=side, ord_type="Stop", price=price, close=self.c)
        
    def _market_order(self, size):
        side = "Buy" if size > 0 else "Sell"
        self.air_exchange.set_market_order(id_= self.idx, timestamp=self.t, size=size, side=side, close=self.c)
        
    def _get_original_indi(self, name):
        return self.column_dic[name]

    def _cancel_entry_orders_after_stop(self):
        pos_qty = self.air_exchange.positions["qty"]
        if pos_qty == 0:
            self.air_exchange.cancel_all_orders()
            return

        keep_orders = []
        for order in list(self.air_exchange.orders):
            size = order.get("size", 0)
            if pos_qty > 0 and size > 0:
                continue
            if pos_qty < 0 and size < 0:
                continue
            keep_orders.append(order)
        self.air_exchange.orders = keep_orders

    def run(self, stop_timestamp=None, continue_until_flat=False):
        start = time.time()
        self.stop_timestamp = stop_timestamp
        self.allow_entry = True
        for i in range(len(self.arrays)):
            self.idx = i
            self.previous_array = self.array.copy() if i > 0 else None
            self.array = self.arrays[i]
            self.t, self.c, self.h, self.l = self.array[self.t_idx], self.array[self.c_idx], self.array[self.h_idx], self.array[self.l_idx]
            crossed_stop = stop_timestamp is not None and self.t >= stop_timestamp
            if crossed_stop:
                self.allow_entry = False
                if continue_until_flat:
                    self._cancel_entry_orders_after_stop()
            self.air_exchange.check_order(self.h, self.l, self.t)            
            self.action()
            if self.air_exchange.exec_history:
                self.air_exchange.exec_history[-1]['low'] = self.l
                self.air_exchange.exec_history[-1]['high'] = self.h
            if stop_timestamp is not None:
                if not continue_until_flat and self.t >= stop_timestamp:
                    break
                if continue_until_flat and not self.allow_entry:
                    pos_qty = self.air_exchange.positions["qty"]
                    if pos_qty == 0 and len(self.air_exchange.orders) == 0:
                        break
        elapsed_time = time.time() - start
            
    def action(self):
        orders = self._get_orders()
        position = self._get_position()["qty"]


def make_mm_pl(df, maker_fee=0, taker_fee=0, initial=100, has_ordertype = False):
    start = time.time()

    if "price" not in df.columns:
        raise ValueError("make_mm_pl requires a 'price' column.")
    if "high" not in df.columns:
        df = df.copy()
        df["high"] = df["price"]
    if "low" not in df.columns:
        df = df.copy()
        df["low"] = df["price"]
    
    if df.empty:
        required_cols = [
            "PL",
            "low_PL",
            "comfee",
            "PLcomfee",
            "PL_graph",
            "low_PL_graph",
            "unrealized_loss",
            "_cumsum",
        ]
        for col in required_cols:
            if col not in df.columns:
                df[col] = pd.Series(dtype=float)
        empty_metrics = {'DD_max': 0, 'DD_per': 0, 'max_unrealized_loss': 0, 'win_rate': 0, 'PF': float('inf'), 'trade_count': 0}
        return df, 0.0, empty_metrics

    size = df.sizes.values
    timestamp = df.time.values
    price = df.price.values
    pct_price = df.price.pct_change().values
    if has_ordertype:
        ord_type = df["ord_type"].values
    size = df.sizes.values
    PLs = np.zeros(len(df))
    low_PLs = np.zeros(len(df))
    comfees = np.zeros(len(df))
    cumsum_positon_size = np.cumsum(size)
    entry_prices = [price[0] if len(price) > 0 else 0] * len(df)

    for i in range(1, len(df)):
        if has_ordertype:
            if ord_type[i] == "Market" or ord_type[i] == "Stop" :
                comfee = taker_fee
            else:
                comfee = maker_fee
            com = comfee * abs(cumsum_positon_size[i - 1] - cumsum_positon_size[i])
            PLs[i] = pct_price[i] * cumsum_positon_size[i - 1]
            comfees[i] = com
        else:
            PLs[i] = pct_price[i] * cumsum_positon_size[i - 1]

        if size[i-1] != 0:
            entry_prices[i] = price[i-1]
        else:
            entry_prices[i] = entry_prices[i-1]

        position_size = cumsum_positon_size[i - 1]
        if position_size != 0:
            low_pct_change = (df["low"].values[i] - price[i-1]) / price[i-1] if price[i-1] != 0 else 0
            low_PLs[i] = low_pct_change * position_size
        else:
            low_PLs[i] = 0

    df["PL"] = PLs
    df["low_PL"] = low_PLs
    df["comfee"]= comfees
    df["comfee_graph"] = np.cumsum(comfees)
    if has_ordertype :
        PLcomfee = PLs - comfees
    else:
        PLcomfee = PLs
    df["PLcomfee"] = PLcomfee
    tmp_PLcomfee = copy.copy(PLcomfee) 
    tmp_PLcomfee[0] += initial
    PL_graph = np.cumsum(tmp_PLcomfee)
    df["PL_graph"] = PL_graph
    
    tmp_low_PLcomfee = copy.copy(low_PLs)
    tmp_low_PLcomfee[0] += initial
    low_PL_graph = np.cumsum(tmp_low_PLcomfee)
    df["low_PL_graph"] = low_PL_graph

    low_pl = low_PL_graph[-1] - initial
    valid_trades = len(low_PLs) - (low_PLs == 0).sum()
    low_avg_pl = low_pl / valid_trades if valid_trades > 0 else 0
    low_profit_total = df.low_PL[df.low_PL > 0].sum()
    low_loss_total = df.low_PL[df.low_PL < 0].sum()
    low_PF = low_profit_total / abs(low_loss_total) if low_loss_total != 0 else float('inf')

    position_sizes = np.roll(cumsum_positon_size, 1)
    position_sizes[0] = 0
    prev_prices = np.roll(price, 1)
    prev_prices[0] = price[0] if len(price) > 0 else 1
    
    low_values = df["low"].values
    high_values = df["high"].values
    
    safe_prev_prices = np.where(prev_prices == 0, 1, prev_prices)
    low_pct_change = (low_values - prev_prices) / safe_prev_prices
    high_pct_change = (high_values - prev_prices) / safe_prev_prices
    
    unrealized_loss_array = np.where(
        position_sizes > 0,
        low_pct_change * position_sizes,
        np.where(
            position_sizes < 0,
            -high_pct_change * np.abs(position_sizes),
            0
        )
    )
    unrealized_loss_array[0] = 0
    max_unrealized_loss = unrealized_loss_array.min()

    df["unrealized_loss"] = unrealized_loss_array

    unrealized_loss_cumsum = np.cumsum(unrealized_loss_array)
    running_max = np.maximum.accumulate(unrealized_loss_cumsum)
    drawdowns = running_max - unrealized_loss_cumsum
    low_DD_max = drawdowns.max()
    
    dd_max_idx = np.argmax(drawdowns)
    low_PL_max = running_max[dd_max_idx]
    low_DD_per = (low_DD_max / low_PL_max * 100) if low_PL_max != 0 else 0

    win_cnt = PLs > 0
    none_cnt = PLs == 0
    none_cnt = none_cnt.sum()
    win_cnt = win_cnt.sum()
    win_rate = win_cnt / (len(PLs) - none_cnt) if (len(PLs) - none_cnt) > 0 else 0
    pl = PL_graph[-1] - initial
    avg_pl = pl / len(PLcomfee) if len(PLcomfee) > 0 else 0
    profit_total =  df.PLcomfee[df.PLcomfee > 0].sum()
    loss_total =  df.PLcomfee[ df.PLcomfee < 0].sum()
    PF = profit_total / abs(loss_total) if loss_total != 0 else float('inf')
    PL_max = 0.00
    DD_max = 0.00
    DD_per = 0.0000
    for i in df.PL_graph:
        if PL_max < i:
            PL_max = i
        DD = PL_max - i
        if DD_max < DD:
            DD_max = DD
            if PL_max:
                DD_per = (DD_max / PL_max)*100
            else:
                pl_per = None

    df["_cumsum"] = cumsum_positon_size
    elapsed_time = time.time() - start
    metrics = {
        'DD_max': DD_max,
        'DD_per': DD_per,
        'max_unrealized_loss': max_unrealized_loss,
        'win_rate': win_rate,
        'PF': PF,
        'trade_count': len(PLs)
    }
    return df, pl, metrics
