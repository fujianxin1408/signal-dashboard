#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 版数据抓取（替代沙箱内 update.py 的更新职责）

与 update.py 的差异：
  · 数据源改用新浪/腾讯公开接口（东方财富对海外 IP 有地域限制）
  · 不写沙箱托管、不调用 publish.js，只负责 data.json + index.html
  · 输出到仓库根目录，由 workflow 负责 commit & push
"""
import datetime as dt
import json
import os
import re
import sys
import time

import numpy as np
import urllib3
import requests

urllib3.disable_warnings()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "data.json")

WATCH = [
    ("600019", "宝钢股份", "RSI14<32", "RSI14>55", "sh"),
    ("600887", "伊利股份", "BIAS6<-2.5%", "BIAS6>+1.5%", "sh"),
    ("000895", "双汇发展", "BIAS6<-3.5%", "BIAS6>+3.0%", "sz"),
    ("603345", "安井食品", "BIAS6<-4.0%", "BIAS6>+6.0%", "sh"),
    ("002032", "苏泊尔", "BIAS6<-4.0%", "BIAS6>+4.0%", "sz"),
    ("000157", "中联重科", "RSI14<32", "RSI14>55", "sz"),
    ("688111", "金山办公", "BIAS6<-6.0%", "BIAS6>+4.0%", "sh"),
    ("600483", "福能股份", "RSI14<35 且 BIAS6<-2.5%", "RSI14>55", "sh"),
    ("601139", "深圳燃气", "RSI14<32", "RSI14>70", "sh"),
    ("600196", "复星医药", "RSI14<30", "RSI14>60", "sh"),
    ("600690", "海尔智家", "RSI14<30 且 BIAS6<-4.0%", "RSI14>60", "sh"),
    ("515080", "中证红利ETF", "RSI14<35 且 BIAS6<-2.0%", "RSI14>65", "sh"),
    ("600941", "中国移动", "RSI14<45 且 BIAS6<-2.5%", "RSI14>70", "sh"),
    ("600600", "青岛啤酒", "RSI14<40 且 BIAS6<-2.5%", "RSI14>70", "sh"),
    ("600098", "广州发展", "RSI14<40 且 BIAS6<-2.5%", "RSI14>65", "sh"),
    ("688029", "南微医学", "RSI14<35 且 BIAS6<-4.0%", "RSI14>65", "sh"),
    ("000828", "东莞控股", "RSI14<30 且 BIAS6<-4.5%", "RSI14>70", "sz"),
    ("600741", "华域汽车", "RSI14<30 且 BIAS6<-3.5%", "RSI14>65", "sh"),
    ("001965", "招商公路", "RSI14<50 且 BIAS6<-3.5%", "RSI14>65", "sz"),
    ("000999", "华润三九", "RSI14<35 且 BIAS6<-3.5%", "RSI14>60", "sz"),
]
ETF_CODES = {"515080"}
UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://finance.sina.com.cn/",
}


def log(*a):
    print(*a, flush=True)


def get(url, tries=3, encoding=None):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=15, verify=False)
            r.raise_for_status()
            if encoding:
                r.encoding = encoding
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            if i < tries - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError("请求失败 %s: %s" % (url[:70], last))


# ---------------- 行情 ----------------

def sina_quote(symbol):
    """新浪实时报价，返回 (price, high, low, prev_close)"""
    txt = get("https://hq.sinajs.cn/list=%s" % symbol, encoding="gbk").text
    m = re.search(r'"(.*)"', txt)
    if not m:
        raise RuntimeError("新浪报价解析失败")
    f = m.group(1).split(",")
    if len(f) < 5:
        raise RuntimeError("新浪报价字段不足")
    # 0名称 1今开 2昨收 3现价 4最高 5最低
    price = float(f[3]) or float(f[2])
    return price, float(f[4]) or price, float(f[5]) or price, float(f[2])


def sina_daily(symbol, days=400):
    """新浪日线（不复权），返回 [(date, open, close, high, low)]"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d" % (symbol, days))
    raw = get(url).text.strip()
    if not raw or raw in ("null", "[]"):
        raise RuntimeError("新浪日线为空")
    raw = re.sub(r"(\w+):", r'"\1":', raw)
    arr = json.loads(raw)
    out = []
    for it in arr:
        out.append((it["day"][:10], float(it["open"]), float(it["close"]),
                    float(it["high"]), float(it["low"])))
    return out


# ---------------- 指标 ----------------

def rsi_wilder(closes, n=14):
    c = np.asarray(closes, dtype=float)
    if len(c) < n + 1:
        return 50.0
    d = np.diff(c)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = up[:n].mean()
    ad = dn[:n].mean()
    for i in range(n, len(d)):
        au = (au * (n - 1) + up[i]) / n
        ad = (ad * (n - 1) + dn[i]) / n
    if ad == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + au / ad)


def rsi_price_for(closes, target):
    base = np.asarray(closes[:-1], dtype=float)
    last = float(closes[-1])
    lo, hi = last * 0.5, last * 1.8
    cache = {}

    def at(p):
        k = round(p, 6)
        if k not in cache:
            cache[k] = rsi_wilder(np.append(base, p))
        return cache[k]

    f_lo, f_hi = at(lo), at(hi)
    if (f_lo - target) * (f_hi - target) > 0:
        return last
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if (at(mid) - target) * (f_lo - target) > 0:
            lo, f_lo = mid, at(mid)
        else:
            hi = mid
    return (lo + hi) / 2.0


def parse_rule(rule):
    conds = []
    for part in rule.replace("且", "|").split("|"):
        part = part.strip().replace(" ", "")
        if not part:
            continue
        if part.startswith("RSI14"):
            op = "<" if "<" in part else ">"
            conds.append(("rsi", op, float(part.split(op, 1)[1])))
        elif part.startswith("BIAS6"):
            op = "<" if "<" in part else ">"
            conds.append(("bias", op,
                          float(part.split(op, 1)[1].replace("%", "").replace("+", ""))))
        else:
            raise ValueError("无法解析: %s" % rule)
    return conds


def critical_price(conds, closes, ma6):
    pts = []
    for ind, op, val in conds:
        pts.append((rsi_price_for(closes, val) if ind == "rsi" else ma6 * (1 + val / 100.0), op))
    return max(p for p, _ in pts) if pts[0][1] == "<" else min(p for p, _ in pts)


def hit(conds, rsi, bias6):
    ok = True
    for ind, op, val in conds:
        cur = rsi if ind == "rsi" else bias6
        ok = ok and (cur < val if op == "<" else cur > val)
    return ok


# ---------------- 主流程 ----------------

def slot_of(now):
    """北京时间 → 档位键"""
    manual = os.environ.get("MANUAL_SLOT", "").strip()
    if manual and len(manual) == 4 and manual.isdigit():
        return manual
    hm = now.hour * 60 + now.minute
    if hm >= 15 * 60 + 10:
        return "1510"
    if hm < 9 * 60 + 30:
        return "0930"
    if 11 * 60 + 30 <= hm < 13 * 60:
        return "1130"
    half = (hm // 30) * 30
    return "%02d%02d" % (half // 60, half % 60)


def main():
    now = dt.datetime.utcnow() + dt.timedelta(hours=8)   # 北京时间
    today = now.strftime("%Y-%m-%d")
    slot = slot_of(now)
    final = slot == "1510"
    log("北京时间 %s  档位 %s" % (now.strftime("%Y-%m-%d %H:%M"), slot))

    # 大盘
    idx_px, _, _, _ = sina_quote("sh000300")
    idx_daily = sina_daily("sh000300", 200)
    idx_closes = [x[2] for x in idx_daily]
    if idx_daily and idx_daily[-1][0] == today:
        idx_closes[-1] = idx_px
    else:
        idx_closes.append(idx_px)
    ma60 = float(np.mean(idx_closes[-60:]))
    bull = idx_px > ma60
    log("沪深300 %.1f  MA60 %.1f  %s" % (idx_px, ma60, "多头" if bull else "空头"))

    rows, errors = [], []
    for code, name, buy_rule, sell_rule, mkt in WATCH:
        sym = mkt + code
        try:
            price, high, low, prev = sina_quote(sym)
            daily = sina_daily(sym)
            if len(daily) < 70:
                raise RuntimeError("日线仅 %d 根" % len(daily))
            if daily[-1][0] == today:
                d0, o0, c0, h0, l0 = daily[-1]
                daily[-1] = (d0, o0, price, max(h0, high), min(l0, low) if l0 > 0 else low)
            else:
                daily.append((today, price, price, high, low))
            closes = [x[2] for x in daily]

            ma6 = float(np.mean(closes[-6:]))
            rsi = rsi_wilder(closes)
            bias6 = (price / ma6 - 1) * 100
            bc, sc = parse_rule(buy_rule), parse_rule(sell_rule)
            buy_px = critical_price(bc, closes, ma6)
            sell_px = critical_price(sc, closes, ma6)
            status = "buy" if hit(bc, rsi, bias6) else ("sell" if hit(sc, rsi, bias6) else "wait")
            ref = closes[-6] if len(closes) >= 6 else closes[0]
            d5 = (price / ref - 1) * 100

            rows.append({
                "code": code, "name": name,
                "price": round(price, 3 if code in ETF_CODES else 2),
                "rsi": round(rsi, 1), "bias6": round(bias6, 2),
                "buy": round(buy_px, 2),
                "dbuy": round((price / buy_px - 1) * 100, 1) if buy_px else None,
                "sell": round(sell_px, 2),
                "dsell": round((price / sell_px - 1) * 100, 1) if sell_px else None,
                "d5": round(d5, 1),
                "buy_rule": buy_rule, "sell_rule": sell_rule,
                "status": status, "intraday": not final,
            })
            log("  %s %s %.2f RSI %.1f BIAS6 %+.2f -> %s"
                % (code, name, price, rsi, bias6, status))
        except Exception as e:  # noqa: BLE001
            errors.append("%s %s: %s" % (code, name, e))
            log("  [失败] %s %s: %s" % (code, name, e))

    if not rows:
        log("全部失败，不写文件")
        return 1

    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {"date": today, "market": {}, "slots": {}}

    counts = {"buy": 0, "sell": 0, "wait": 0}
    for r in rows:
        counts[r["status"]] += 1

    data["date"] = today
    data["market"] = {"idx": round(idx_px, 1), "ma60": round(ma60, 1), "bull": bool(bull)}
    data.setdefault("slots", {})[slot] = {
        "ts": now.strftime("%H:%M"),
        "label": "收盘确认" if final else "盘中 %s:%s" % (slot[:2], slot[2:]),
        "final": final, "counts": counts, "rows": rows,
    }
    data["updated"] = now.strftime("%Y-%m-%d %H:%M")

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    log("完成 %s | 买%d 卖%d 等%d | 失败 %d"
        % (slot, counts["buy"], counts["sell"], counts["wait"], len(errors)))
    return 0 if not errors else 0   # 部分失败也写出，避免整天无数据


if __name__ == "__main__":
    sys.exit(main())
