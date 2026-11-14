# -*- coding: utf-8 -*-
"""一次性补丁：把内蒙华电 600863 追加到线上 data.json 的各个档位。

与 ci/update_ci.py::main 同算法，只算这一行，不重跑整表。
两点特殊：
1. 卖点是「收盘站上 MA60」，没有固定触发价 —— 由 sell_line_price() 给出当日临界值，
   每天随均线滚动，行里 selldyn 标出线名，前端据此加「MA60」小标。
2. 两次 10 转 5（2013-07-08 −36.4%、2014-07-25 −38.1%），必须走前复权，
   否则除权跳空会被 Wilder RSI 记成暴跌、凭空造出买点（不复权 40 笔 vs 37 笔）。
"""
import json
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ci"))
import update_ci as U

CODE = "600863"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data.json")


def build_row():
    item = next(x for x in U.WATCH if x[0] == CODE)
    code, name, buy_rule, sell_rule, mkt, bt = item
    sym = mkt + code
    now = U.bj_now()
    today = now.strftime("%Y-%m-%d")

    price, high, low, prev = U.quote(sym)
    dly, fq_mode = U.daily_qfq(sym, 400)
    if len(dly) < 70:
        raise RuntimeError("日线仅 %d 根" % len(dly))
    if dly[-1][0] == today or abs(price - dly[-1][2]) < 1e-6:
        d0, o0, c0, h0, l0 = dly[-1]
        dly[-1] = (d0, o0, price, max(h0, high), min(l0, low) if l0 > 0 else low)
    else:
        dly.append((today, price, price, high, low))
    closes = [x[2] for x in dly]

    ma6 = float(np.mean(closes[-6:]))
    sma60 = float(np.mean(closes[-60:]))
    sma250 = float(np.mean(closes[-250:])) if len(closes) >= 250 else None
    up250, slope250 = None, None
    if len(closes) >= 270 and sma250:
        a250 = float(np.mean(closes[-269:-19]))
        up250 = sma250 > a250
        slope250 = (sma250 / a250 - 1) * 100
    rsi = U.rsi_wilder(closes)
    bias6 = (price / ma6 - 1) * 100
    bc, sc = U.parse_rule(buy_rule), U.parse_rule(sell_rule)
    buy_lo, buy_px, no_win, nowhy = U.resolve_buy(bc, closes, ma6, bt)
    sell_px = U.critical_price(sc, closes, ma6)
    sell_dyn = None
    if sell_px is None:
        sell_px, sell_dyn = U.sell_line_price(sc, closes)
    buy_hit = U.hit(bc, rsi, bias6, price, sma60, sma250, up250)
    sell_hit = U.hit(sc, rsi, bias6, price, sma60, sma250, up250)
    if buy_hit:
        status = "buy"
    elif sell_hit:
        status = "sell"
    else:
        status = "wait"
    ref = closes[-6] if len(closes) >= 6 else closes[0]
    d5 = (price / ref - 1) * 100

    row = {
        "code": code, "name": name, "sym": sym,
        "price": round(price, 2), "rsi": round(rsi, 1), "bias6": round(bias6, 2),
        "buy": None if buy_px is None else round(buy_px, 2),
        "buy_lo": None if buy_lo is None else round(buy_lo, 2),
        "nowin": bool(no_win), "nowhy": nowhy,
        "ma60": round(sma60, 2),
        "ma250": None if sma250 is None else round(sma250, 2),
        "ma250up": up250,
        "ma250slope": None if slope250 is None else round(slope250, 2),
        "sell": None if sell_px is None else round(sell_px, 2),
        "selldyn": sell_dyn,
        "trail": None,
        "pos": None,
        "gap_buy": None if no_win else U.gap(price, buy_px, "buy"),
        "gap_sell": U.gap(price, sell_px, "sell"),
        "d5": round(d5, 1),
        "buy_rule": buy_rule, "sell_rule": sell_rule,
        "status": status, "blocked": "", "bt": bt or None,
        "fq": fq_mode,
        "intraday": False,
        "hist": [round(float(x), 3) for x in closes[-300:]],
        "histDate": dly[-1][0],
    }
    print("%s %s  价 %.2f  RSI %.1f  BIAS %+.2f  MA60 %.3f  MA250 %s  口径 %s"
          % (name, code, price, rsi, bias6, sma60,
             ("%.2f" % sma250) if sma250 else "—", fq_mode))
    print("  买点 %s  卖点 %s（%s）距买点 %s%%  距卖点 %s%%  状态 %s"
          % (row["buy"], row["sell"], sell_dyn, row["gap_buy"],
             row["gap_sell"], status))
    print("  hist %d 根  %s ~ %s" % (len(row["hist"]), dly[-300][0], dly[-1][0]))
    return row


def main():
    data = json.load(open(DATA, encoding="utf-8"))
    row = build_row()
    for slot, payload in data["slots"].items():
        rows = [r for r in payload["rows"] if r["code"] != CODE]
        rows.append(row)
        payload["rows"] = rows
        cnt = {"buy": 0, "sell": 0, "wait": 0}
        for r in rows:
            cnt[r["status"]] += 1
        payload["counts"] = cnt
        print("  档位 %-5s → %d 只  counts %s" % (slot, len(rows), cnt))
    raw = json.dumps(data, ensure_ascii=False, indent=1)
    raw = re.sub(r"\[\s*\n\s*([-\d.,\s]+?)\s*\n\s*\]",
                 lambda m: "[" + ",".join(x.strip() for x in m.group(1).split(",")) + "]",
                 raw)
    open(DATA, "w", encoding="utf-8").write(raw)
    print("已写 data.json %d 字节" % os.path.getsize(DATA))


if __name__ == "__main__":
    main()
