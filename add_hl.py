# -*- coding: utf-8 -*-
"""一次性补丁：把海澜之家 600398 追加到线上 data.json 的各个档位。

不重跑整表（避免非交易时段抓价改写其他 27 只的档位/时间戳），
只用与 ci/update_ci.py::main 完全相同的算法生成这一行。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ci"))
import update_ci as U

CODE = "600398"
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")


def build_row():
    item = next(x for x in U.WATCH if x[0] == CODE)
    code, name, buy_rule, sell_rule, mkt, bt = item
    sym = mkt + code
    now = U.bj_now()
    today = now.strftime("%Y-%m-%d")

    price, high, low, prev = U.quote(sym)
    dly = U.daily(sym, 700 if "峰值回撤" in sell_rule else 400)
    if len(dly) < 70:
        raise RuntimeError("日线仅 %d 根" % len(dly))
    if dly[-1][0] == today:
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
    buy_lo, buy_px, no_win, nowhy = U.critical_window(bc, closes, ma6)
    trail = next((c[2] for c in sc if c[0] == "trail"), None)
    pos = U.trail_pos(dly, bc, trail, bt.get("stop")) if trail else None
    sell_px = (round(pos["peak"] * (1 - trail / 100.0), 2) if pos
               else U.critical_price(sc, closes, ma6))
    buy_hit = U.hit(bc, rsi, bias6, price, sma60, sma250, up250)
    sell_hit = U.hit(sc, rsi, bias6, price, sma60, sma250, up250,
                     pos["peak"] if pos else None)
    status = "buy" if buy_hit else ("sell" if sell_hit else "wait")
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
        "trail": trail,
        "pos": pos,
        "gap_buy": None if no_win else U.gap(price, buy_px, "buy"),
        "gap_sell": U.gap(price, sell_px, "sell"),
        "d5": round(d5, 1),
        "buy_rule": buy_rule, "sell_rule": sell_rule,
        "status": status, "blocked": "", "bt": bt or None, "intraday": False,
        "hist": [round(float(x), 3) for x in closes[-300:]],
        "histDate": dly[-1][0],
    }
    print("%s %s  价 %.2f  RSI %.1f  BIAS %+.2f  年线 %s (%.2f%%)"
          % (name, code, price, rsi, bias6, sma250,
             (price / sma250 - 1) * 100 if sma250 else float("nan")))
    print("  买点 %s  下界 %s  窗口空=%s(%s)  卖点 %s  状态 %s"
          % (row["buy"], row["buy_lo"], row["nowin"], row["nowhy"],
             row["sell"], status))
    print("  hist %d 根  %s ~ %s" % (len(row["hist"]), dly[-300][0], dly[-1][0]))
    return row


def main():
    import json
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
    json.dump(data, open(DATA, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    print("已写 data.json %d 字节" % os.path.getsize(DATA))


if __name__ == "__main__":
    main()
