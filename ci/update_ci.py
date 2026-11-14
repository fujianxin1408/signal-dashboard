#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 版数据抓取（替代沙箱内 update.py 的更新职责）

与 update.py 的差异：
  · 数据源改用新浪/腾讯公开接口（东方财富对海外 IP 有地域限制）
  · 不写沙箱托管、不调用 publish.js，只负责 data.json + index.html
  · 输出到仓库根目录，由 workflow 负责 commit & push

2026-11 修订：
  1) 档位由 workflow 传入的「目标档」决定，不再用运行时刻反推——
     Actions cron 常有 5~40 分钟延迟，反推会把 15:12 生成的数据标成 14:51 档。
     同时记录 ts（真实生成时刻）与 skew（相对目标的延迟分钟数）。
  2) 双条件买点价取 min、卖点价取 max（此前取 max 会出现「现价已低于买点却仍显示等待」）。
  3) 间距口径改为「差值/现价」并明确符号：gap_buy>0 = 现价已低于买点，<0 = 还需下跌 |gap|%。
  4) 跨交易日自动清空 slots，避免昨天的档位挂到今天。
  5) 补跑不覆盖真实档：偏离目标 >25 分钟且未 force 时，结果写入 live 档并保留原档。
  6) 触发价口径修正（2026-11-11）：
     · BIAS 触发价改用闭式解 s5×(1+k)÷(5−k)。旧写法 ma6×(1+k) 忽略了「价格下跌会同时
       拉低 MA6」，算出的买点偏高约 1%，按它挂单实际只到 −6.1% 而非设定的 −7%。
     · RSI 触发价改为「5 日匀速走到目标」，旧写法假设单日跳变，会得出「还需涨 23%」
       这类不现实的数字。
  7) 年线条件（2026-11-11）：
     · 新增 MA250上（收盘站上年线）与 MA250升（年线 20 日斜率向上）两个条件，
       都解成「明日收盘价必须高于的下界」，与乖离上界共同构成夹逼区间。
     · MA250升的下界：明日 MA250=(s249+C)/250 要高于 20 日前的 MA250(=A)，
       解得 C > 250A − s249。年线下行时该下界会顶到乖离上界之上，区间自然为空。
     · hist 由 130 根扩到 300 根，前端重算才算得出 MA250（需 251 根）。
  8) 年线下条件（2026-11-12，配合森马服饰 002563）：
     · 新增 MA250下（收盘在年线之下）。临界点仍是 s249/249（与 MA250上 同一个数），
       但不等号方向相反，它是价格「上界」而不是下界。critical_window 按 op 把均线
       临界值分派到 pts（<）或 floors（>），否则「年线下才买」会被解成夹逼区间而算错买点。
     · hit() / 前端 hitTest() 本来就支持 < 方向，无需改动；parse_rule 也已把「下」解析成 <。
  9) 大盘门控方向（2026-11-12，配合诺力股份 603611）：
     · bt.mkt=True/"bull" 表示「需要沪深300 > MA60（多头）」；bt.mkt="bear" 表示
       「需要沪深300 < MA60（空头）」。两者都只在价格条件已触发时把 buy 降为 wait，
       并在行内标记原因；缺省不做大盘判断。
     · 前端 recalcRow 用同一规则读 bt.mkt 与重算后的 bull，保证实时与收盘归档不打架。
"""
import datetime as dt
import json
import math
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

# code, 名称, 买点条件, 卖点条件, 市场, 回测口径
#   bt: n=笔数 win=胜率% total=累计% stop=止损% years=样本起点备注
#       mkt=大盘门控：True/"bull" 需 HS300>MA60（多头），"bear" 需 HS300<MA60（空头），缺省无要求
WATCH = [
    ("600019", "宝钢股份", "RSI14<32", "RSI14>55", "sh", {}),
    ("600887", "伊利股份", "BIAS6<-2.5%", "BIAS6>+1.5%", "sh",
     {"n": 71, "win": 79.0, "total": 280, "stop": 0}),
    ("000895", "双汇发展", "BIAS6<-3.5%", "BIAS6>+3.0%", "sz",
     {"n": 44, "win": 82.0, "total": 199, "stop": 9}),
    ("603345", "安井食品", "BIAS6<-4.0%", "BIAS6>+6.0%", "sh",
     {"n": 47, "win": 81.0, "total": 743, "stop": 15}),
    ("002032", "苏泊尔", "BIAS6<-4.0%", "BIAS6>+4.0%", "sz",
     {"n": 31, "win": 87.0, "total": 709, "stop": 10}),
    ("000157", "中联重科", "RSI14<32", "RSI14>55", "sz", {}),
    ("688111", "金山办公", "BIAS6<-6.0%", "BIAS6>+4.0%", "sh",
     {"n": 11, "win": 82.0, "total": 65, "stop": 0, "mkt": True}),
    ("600483", "福能股份", "RSI14<35 且 BIAS6<-2.5%", "RSI14>55", "sh", {}),
    ("601139", "深圳燃气", "RSI14<32", "RSI14>70", "sh",
     {"n": 17, "win": 94.0, "total": 427, "stop": 10}),
    ("600196", "复星医药", "RSI14<30", "RSI14>60", "sh", {}),
    ("600690", "海尔智家", "RSI14<30 且 BIAS6<-4.0%", "RSI14>60", "sh", {}),
    ("515080", "中证红利ETF", "RSI14<35 且 BIAS6<-2.0%", "RSI14>65", "sh", {}),
    # ——— 以下 8 只为 2026-11 新增，参数由同区间网格回测选定（次日开盘成交）———
    ("600941", "中国移动", "RSI14<50 且 BIAS6<-3.0%", "RSI14>70", "sh",
     {"n": 11, "win": 90.9, "total": 250, "stop": 9, "years": "2022-01 上市起"}),
    ("600600", "青岛啤酒", "RSI14<45 且 BIAS6<-3.0%", "RSI14>70", "sh",
     {"n": 21, "win": 71.4, "total": 453, "stop": 15, "mkt": True}),
    ("600098", "广州发展", "RSI14<40 且 BIAS6<-3.0%", "RSI14>65", "sh",
     {"n": 39, "win": 66.7, "total": 480, "stop": 15}),
    ("688029", "南微医学", "BIAS6<-6.0%", "BIAS6>+6.0%", "sh",
     {"n": 30, "win": 70.0, "total": 724, "stop": 12, "years": "2019-07 上市起"}),
    ("000828", "东莞控股", "RSI14<32", "RSI14>55", "sz",
     {"n": 48, "win": 56.2, "total": 502, "stop": 7}),
    ("600741", "华域汽车", "BIAS6<-3.0%", "BIAS6>+6.0%", "sh",
     {"n": 100, "win": 62.0, "total": 839, "stop": 15}),
    ("001965", "招商公路", "RSI14<40 且 BIAS6<-3.5%", "RSI14>65", "sz",
     {"n": 24, "win": 87.5, "total": 743, "stop": 15, "years": "2017-12 上市起"}),
    ("000999", "华润三九", "BIAS6<-2.0%", "BIAS6>+6.0%", "sz",
     {"n": 72, "win": 70.8, "total": 1655, "stop": 15}),
    # 2026-11-11 新增：十年网格里 BIAS 类最优，RSI 类最高只到 +414%
    ("002690", "美亚光电", "BIAS6<-3.5%", "BIAS6>+3.0%", "sz",
     {"n": 113, "win": 61.1, "total": 745, "stop": 7}),
    # 2026-11-11 新增：上市 2017-11，样本 8.75 年；深跌双确认，无止损
    ("603605", "珀莱雅", "BIAS6<-7.0% 且 RSI14<45", "RSI14>65", "sh",
     {"n": 17, "win": 100.0, "total": 3673, "stop": 0}),
    # 2026-11-11 新增：MA60 趋势过滤 + 乖离回调，12.6 年 38 笔胜率 81.6%；
    # 去掉 MA60 条件后收益从 +647% 掉到 +350%、单笔最差 -31.7%，趋势过滤是命门
    ("601318", "中国平安", "BIAS6<-1.5% 且 MA60上", "BIAS6>+4.0%", "sh",
     {"n": 38, "win": 81.6, "total": 647, "stop": 0, "years": "2014-04 起 12.6 年"}),
    # 2026-11-11 新增：年线趋势 + 乖离回调。11.8 年 72 笔胜率 59.7% 累计 +1793%，
    # 同期买入持有 +629%、回撤 -67%；策略回撤 -40.9%。10% 止损在这只加分（年化波动 53.7%），
    # 与中国平安相反——平安波动小，止损容易被噪音误伤。
    ("300373", "扬杰科技", "MA250上 且 MA250升 且 BIAS6<-2.0%", "BIAS6>+7.0%", "sz",
     {"n": 72, "win": 59.7, "total": 1793, "stop": 10, "years": "2015-02 起 11.8 年"}),
    # 2026-11-12 新增：14.6 年 22 笔胜率 59.1%、累计 +297.7%、年化 9.9%、回撤 28.2%。
    # 宽幅箱体股，只在「年线之下 + 深跌」才接：加 MA250下 过滤后 54 组参数对照里
    # 54 组年化全部改善、54 组胜率全部提高；只在年线上买反而是 -21%。
    # 回测为前复权口径，看板实时价为不复权，两者年线值会有小幅差异。
    ("002563", "森马服饰", "MA250下 且 RSI14<50 且 BIAS6<-6.0%", "RSI14>65", "sz",
     {"n": 22, "win": 59.1, "total": 297.7, "stop": 8, "years": "2012-04 起 14.6 年"}),
    # 2026-11-12 新增：10.7 年 26 笔胜率 76.9%、累计 +348.5%、年化 15.1%、回撤 17.9%。
    # 与森马不同，它要「弱势里的弱势」：年线下 + 沪深300 也在 MA60 之下才接。
    # 120 组参数对照里加这套过滤后 92% 年化改善、100% 回撤变小、94% 胜率提高。
    # 注意 mkt="bear" 是反向门控（要空头），与金山办公/青岛啤酒的 True（要多头）相反。
    # 回测为前复权口径（年线 19.34），看板实时价为不复权，年线数值会有差异。
    ("603611", "诺力股份", "MA250下 且 RSI14<45 且 BIAS6<-5.0%", "RSI14>55", "sh",
     {"n": 26, "win": 76.9, "total": 348.5, "stop": 12, "mkt": "bear",
      "years": "2016-03 起 10.7 年"}),
    # 2026-11-13 新增：14.9 年 28 笔胜率 60.7%、累计 +1212%、年化 18.9%、回撤 38.3%。
    # 与森马/诺力相反，它要「年线之上」的回调：去掉年线过滤年化掉到 12.7%、回撤升到 68.7%；
    # 反过来只在年线下买只有 9.3% 年化配 58.7% 回撤，是陷阱。卖出不用 RSI（>70 只有 11.2%），
    # 用 17% 峰值回撤跟踪止盈——2019 年 +57%、2025-04~2026-03 +80% 两笔全靠它拿满。
    # 卖点依赖「持仓期最高价」，看板按规则从前向后模拟当前持仓状态，见 trail_pos()。
    ("000338", "潍柴动力", "MA250上 且 RSI14<40 且 BIAS6<-2.0%", "峰值回撤17%", "sz",
     {"n": 28, "win": 60.7, "total": 1212, "stop": 8, "trail": 17,
      "years": "2012-01 起 14.9 年"}),
    # 2026-11-13 新增：12.9 年 40 笔胜率 77.5%、累计 +231.2%、年化 9.8%、回撤 25.0%。
    # 与森马相反：只做「年线之上」的急跌反弹；年线下抄底 37 笔年化 -2.0%、回撤 52.9%。
    # 64 组参数成对比较：加「年线上」后 64 组回撤全部变小、62 组胜率提高。
    # 卖出必须用 BIAS（+3% 就卖），RSI>65 只有 3.7%；持仓只占 16% 时间，常年空仓。
    # 收益集中在 2014-2015（两年把净值 1.00→2.28），2016 年起年化仅 3.5%，别按 9.8% 预期。
    ("600398", "海澜之家", "MA250上 且 RSI14<50 且 BIAS6<-3.0%", "BIAS6>+3.0%", "sh",
     {"n": 40, "win": 77.5, "total": 231.2, "stop": 12,
      "years": "2014-01 起 12.9 年"}),
    # 2026-11-14 新增：15.6 年 26 笔胜率 80.8%、累计 +209.4%、年化 7.5%、回撤 19.4%，
    # 同期买入持有 -14.4%（不复权）/ +约 +100%（含分红），持仓只占 26% 时间。
    # 所有趋势、追涨、均线类策略在这只票上都亏钱，只有「RSI14<30 接超跌 / >60 卖」有效：
    # RSI<30 后 20 日中位 +4.4%、胜率 79%。
    # 77 次除息、每股累计分红 4.58 元。不复权 K 线在除息日留下向下跳空，Wilder RSI
    # 把它记成下跌，全历史多出 36 天假超跌（回测里不存在的买点），故这只票单独走
    # 前复权日线（bt.qfq=True）；其余 28 只仍用不复权，口径差异见 README。
    ("601158", "重庆水务", "RSI14<30", "RSI14>60", "sh",
     {"n": 26, "win": 80.8, "total": 209.4, "stop": 10, "qfq": True,
      "years": "2011-04 起 15.6 年"}),
    # 2026-11-14 新增：16.6 年 31 笔胜率 77.4%、累计 +624.3%、年化 12.7%、回撤 27.9%，
    # 同期买入持有 +39.9%（年化 2.0%）。周期股（白羽鸡），所有趋势/追涨规则全亏。
    # 关键是 BIAS6<-7%（跌急了），不是「跌多了」：2024 年腰斩到 9.98，
    # 但那是 18 个月慢跌、BIAS6 最深只有 -6.27%，全年零交易。
    # 2022-06 至今 BIAS6 破 -7% 只有 1 天（2026-03-23 的 -7.73%）。
    # 放宽到 -6% 回撤从 27.9% 飙到 48.4%，多出的 15 笔全是阴跌里过早接刀。
    # 止损越紧亏越多：8% 年化 10.1%、10% 只剩 5.6%、15% 是 12.7%。
    # 2023/2024/2025 连续三年零交易，近年分段年化仅 2.2%，别按 12.7% 预期。
    # 15 次除息累计 6.76 元、单次跳空中位 -2.75%，不复权会多 9 天假买点
    # （不复权重跑 33 笔、年化 10.7%、回撤 39.4%），故同样走前复权（bt.qfq=True）。
    ("002299", "圣农发展", "RSI14<50 且 BIAS6<-7.0%", "RSI14>55", "sz",
     {"n": 31, "win": 77.4, "total": 624.3, "stop": 15, "qfq": True, "buySim": 5,
      "years": "2010-04 起 16.6 年"}),
]
ETF_CODES = {"515080"}
UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://finance.sina.com.cn/",
}

SLOT_TARGET = {"1451": "14:51", "1505": "15:05", "live": None}
SLOT_LABEL = {"live": "实时刷新", "1451": "14:51 盘中", "1505": "15:05 收盘"}


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
            # 403/404 是封禁或不存在，重试没有意义，直接交给备用源
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (403, 404):
                break
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


# ---------------- 备用数据源 ----------------
# 新浪/腾讯对海外 IP 与部分云机房会返回 403（GitHub Actions 上偶发），
# 抓不到时退到东方财富，字段口径一致（不复权、收盘价即交易所收盘）。

def em_daily(symbol, days=400):
    """东方财富日线（不复权），签名与 sina_daily 相同"""
    secid = ("1." if symbol.startswith("sh") else "0.") + symbol[2:]
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           "?secid=%s&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55"
           "&klt=101&fqt=0&end=20500101&lmt=%d" % (secid, days))
    js = json.loads(get(url).text)
    kl = ((js or {}).get("data") or {}).get("klines") or []
    if not kl:
        raise RuntimeError("东财日线为空")
    out = []
    for s in kl:                       # 日期,开,收,高,低,量,额
        f = s.split(",")
        out.append((f[0][:10], float(f[1]), float(f[2]), float(f[3]), float(f[4])))
    return out


def qt_quote(symbol):
    """腾讯实时报价（备用），返回 (price, high, low, prev_close)"""
    txt = get("https://qt.gtimg.cn/q=%s" % symbol, encoding="gbk").text
    m = re.search(r'"([^"]*)"', txt)
    if not m:
        raise RuntimeError("腾讯报价解析失败")
    f = m.group(1).split("~")
    if len(f) < 35:
        raise RuntimeError("腾讯报价字段不足")
    price = float(f[3]) or float(f[4])
    return price, float(f[33]) or price, float(f[34]) or price, float(f[4])


def qt_daily(symbol, days=400):
    """腾讯日线（不复权），第三备用源。签名与 sina_daily 相同。
    2026-11 加：沙箱与部分云机房访问新浪整站 403、访问东财直接被掐连接，
    只剩腾讯能通；字段口径同样是不复权，收盘价即交易所收盘。"""
    url = ("https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
           "?param=%s,day,,,%d" % (symbol, max(int(days), 60)))
    js = json.loads(get(url).text)
    kl = ((js or {}).get("data") or {}).get(symbol, {}).get("day") or []
    if not kl:
        raise RuntimeError("腾讯日线为空")
    # 日期,开,收,高,低,量
    return [(f[0][:10], float(f[1]), float(f[2]), float(f[3]), float(f[4])) for f in kl]


# ---------------- 前复权日线（高股息标的专用） ----------------
# 默认一律用不复权价（与行情软件一致）。但重庆水务这类年年高分红的公用事业股，
# 除息日不复权收盘会留下一个向下跳空，Wilder RSI 把它记成一次下跌，除息后约一个月内
# 会虚低、报出回测里根本不存在的 RSI 买点（全历史多 36 天假超跌）。
# 前复权序列以「最新价」为基准，最后一根收盘 = 实际收盘，因此与实时盘口价同口径：
# 直接拿它算 RSI / BIAS / 均线 / 触发价，得到的买点价就能照着挂单，无需再做换算。

# 腾讯前复权的两个入口，同一份数据、不同的前置层：
# web.ifzq 有时会吐一段 JS 反爬挑战页（HTTP 501），proxy.finance 没有那层拦截。
_QT_FQ_HOSTS = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
)


def qt_daily_qfq(symbol, days=400):
    """腾讯前复权日线（最新一根 = 实际收盘，与盘口同口径）。签名同 sina_daily。"""
    last = None
    for base in _QT_FQ_HOSTS:
        url = "%s?param=%s,day,,,%d,qfq" % (base, symbol, max(int(days), 60))
        try:
            txt = get(url, tries=2).text
            if not txt.lstrip().startswith("{"):
                raise RuntimeError("非 JSON 应答（反爬挑战页）")
            js = json.loads(txt)
            d = ((js or {}).get("data") or {}).get(symbol, {})
            kl = (d or {}).get("qfqday") or (d or {}).get("day") or []
            if not kl:
                raise RuntimeError("前复权日线为空")
            # 日期,开,收,高,低,量
            return [(f[0][:10], float(f[1]), float(f[2]), float(f[3]), float(f[4]))
                    for f in kl]
        except Exception as e:  # noqa: BLE001
            last = e
            log("  [腾讯前复权入口不可用 %s] %s"
                % (base.split("//")[1].split("/")[0], str(e)[:50]))
    raise RuntimeError("腾讯前复权全部入口失败：%s" % last)


def em_daily_qfq(symbol, days=400):
    """东财前复权日线（fqt=1），口径与腾讯 qfq 一致。"""
    secid = ("1." if symbol.startswith("sh") else "0.") + symbol[2:]
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           "?secid=%s&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55"
           "&klt=101&fqt=1&end=20500101&lmt=%d" % (secid, days))
    js = json.loads(get(url).text)
    kl = ((js or {}).get("data") or {}).get("klines") or []
    if not kl:
        raise RuntimeError("东财前复权日线为空")
    return [(f[0][:10], float(f[1]), float(f[2]), float(f[3]), float(f[4]))
            for f in (s.split(",") for s in kl)]


def daily_qfq(symbol, days=400):
    """前复权日线：腾讯 → 东财 → 新浪不复权（降级）。
    返回 (dly, mode)：mode="qfq" 表示前复权口径，"raw" 表示已降级到不复权。"""
    try:
        return qt_daily_qfq(symbol, days), "qfq"
    except Exception as e:  # noqa: BLE001
        log("  [腾讯前复权不可用，改东财] %s" % str(e)[:60])
    try:
        return em_daily_qfq(symbol, days), "qfq"
    except Exception as e:  # noqa: BLE001
        log("  [东财前复权不可用，降级不复权] %s" % str(e)[:60])
    return daily(symbol, days), "raw"


_SINA_DOWN = False     # 新浪整站 403 后不再逐只重试，本轮直接走备用源


def quote(symbol):
    global _SINA_DOWN
    if not _SINA_DOWN:
        try:
            return sina_quote(symbol)
        except Exception as e:  # noqa: BLE001
            _SINA_DOWN = True
            log("  [新浪报价不可用，本轮改用腾讯] %s" % str(e)[:60])
    return qt_quote(symbol)


def daily(symbol, days=400):
    if not _SINA_DOWN:
        try:
            return sina_daily(symbol, days)
        except Exception:  # noqa: BLE001
            pass
    try:
        return em_daily(symbol, days)
    except Exception as e:  # noqa: BLE001
        log("  [东财日线不可用，改用腾讯] %s" % str(e)[:50])
        return qt_daily(symbol, days)


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


def _wilder_state(closes, n=14):
    """返回 Wilder 平滑到最后一根的 (平均涨幅, 平均跌幅)"""
    c = np.asarray(closes, dtype=float)
    d = np.diff(c)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au, ad = up[:n].mean(), dn[:n].mean()
    for i in range(n, len(d)):
        au = (au * (n - 1) + up[i]) / n
        ad = (ad * (n - 1) + dn[i]) / n
    return au, ad


def rsi_price_for(closes, target, days=5):
    """求收盘价要走到的位置，使 RSI 刚好等于 target。
    假设价格在 days 个交易日内匀速走到该位置（一天跳到位不现实：
    RSI 现值 32 时要单日跳到 65 需涨 23%，按 5 日走则只需 16%）。"""
    c = np.asarray(closes, dtype=float)
    n = 14
    au0, ad0 = _wilder_state(c, n)
    last = float(c[-1])
    lo, hi = last * 0.55, last * 1.7

    def at(p):
        step = (p / last) ** (1.0 / days)
        px, a, b = last, au0, ad0
        for _ in range(days):
            nx = px * step
            ch = nx - px
            a = (a * (n - 1) + (ch if ch > 0 else 0.0)) / n
            b = (b * (n - 1) + (-ch if ch < 0 else 0.0)) / n
            px = nx
        return 100.0 if b <= 0 else 100.0 - 100.0 / (1.0 + a / b)

    f_lo, f_hi = at(lo), at(hi)
    if (f_lo - target) * (f_hi - target) > 0:
        return last          # 目标在可达区间外（现价已满足或永远到不了）
    for _ in range(70):
        mid = (lo + hi) / 2.0
        if (at(mid) - target) * (f_lo - target) > 0:
            lo, f_lo = mid, at(mid)
        else:
            hi = mid
    return (lo + hi) / 2.0


def bias_price(closes, k, ma6=None):
    """BIAS6 触发价的闭式解：价格下跌本身也会把 MA6 拉低，
    所以不能直接用 ma6 × (1+k)（那样算出的买点偏高 1% 左右，挂单后其实没触发）。
      P = s5 × (1+k) ÷ (5 − k)，s5 为不含今日的最近 5 日收盘和。"""
    c = np.asarray(closes, dtype=float)
    if len(c) < 6:
        return float(ma6) * (1 + k / 100.0) if ma6 else float(c[-1])
    s5 = float(c[-6:-1].sum())
    return s5 * (1 + k / 100.0) / (5 - k / 100.0)



def sim_buy_price(conds, closes, days=5):
    """按「价格在 days 个交易日内匀速跌到位」反解买点（仅用于纯 RSI/BIAS 规则）。

    与单日跳口径的区别：单日暴跌时 6 日均线几乎不动，bias_price() 给的是「明天一天
    跌到这儿就触发」；而阴跌时 MA6 跟着下移，必须跌更深才满足同一阈值。
    圣农发展的策略卡用的就是 5 日匀速口径（14.71 vs 单日 15.52），看板必须对齐，
    否则同一个规则在两处给出两个买点。
    含均线条件时不在这里解（交给 critical_window 的夹逼区间），返回 None 表示不适用。"""
    c = list(closes)
    if len(c) < 20:
        return None
    for ind, _op, _val in conds:
        if ind not in ("rsi", "bias"):
            return None
    last = float(c[-1])
    base = c[-400:]

    def at(P):
        step = (P / last) ** (1.0 / days)
        px, cs = last, list(base)
        for _ in range(days):
            px *= step
            cs.append(px)
        rsi = rsi_wilder(cs)
        bias = (cs[-1] / float(np.mean(cs[-6:])) - 1) * 100
        return rsi, bias

    def ok(P):
        rsi, bias = at(P)
        for ind, op, val in conds:
            v = rsi if ind == "rsi" else bias
            if not ((v < val) if op == "<" else (v > val)):
                return False
        return True

    lo, hi = last * 0.40, last * 1.05
    if not ok(lo):                    # 跌 60% 都触发不了：说明现价已满足或规则无解
        return None
    for _ in range(90):
        mid = (lo + hi) / 2.0
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def resolve_buy(bc, closes, ma6, bt=None):
    """买点解析：默认走 critical_window 的夹逼/单日口径；
    bt 里声明了 buySim=N 的规则改用「N 个交易日匀速跌到位」反解（与策略卡同算法）。
    返回 (lo, hi, no_win, why)，与 critical_window 同签名。"""
    lo, hi, no_win, why = critical_window(bc, closes, ma6)
    days = (bt or {}).get("buySim")
    if days:
        sim = sim_buy_price(bc, closes, days)
        if sim is not None:
            dec = 3 if abs(closes[-1]) < 10 else 2
            hi = math.floor(sim * 10 ** dec) / 10 ** dec   # 向下让 1 个最小价位，避开边界
            lo, no_win, why = None, False, ""
    return lo, hi, no_win, why


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
        elif part.startswith("MA250"):
            # 年线：上 = 收盘站上年线（解成下界），升 = 年线 20 日斜率向上（也解成下界）
            if "升" in part:
                conds.append(("ma250up", ">", 0.0))
            else:
                conds.append(("ma250", ">" if "上" in part or ">" in part else "<", 0.0))
        elif part.startswith("MA60"):
            # 趋势过滤：收盘站上 60 日均线。只作前置条件，不产生独立触发价
            conds.append(("ma60", ">" if "上" in part or ">" in part else "<", 0.0))
        elif "峰值回撤" in part or part.upper().startswith("TRAIL"):
            # 跟踪止盈：从持仓期最高价回撤 N% 卖出。它没有固定触发价——
            # 卖点 = 峰值×(1−N%)，峰值要靠持仓状态算，见 trail_pos()
            m = re.search(r"([\d.]+)\s*%", part)
            if not m:
                raise ValueError("无法解析: %s" % rule)
            conds.append(("trail", "<", float(m.group(1))))
        else:
            raise ValueError("无法解析: %s" % rule)
    return conds


def ma_floor(closes, n):
    """明日收盘要站在 MAn 上方，需要的最低价。
    明日 MAn = (s(n−1) + C) / n，令 C > MAn 得 C > s(n−1) / (n−1)。"""
    if len(closes) < n:
        return None
    return float(np.sum(closes[-(n - 1):])) / (n - 1)


def ma60_floor(closes):
    return ma_floor(closes, 60)


def ma250_up_floor(closes, n=250, back=20):
    """明日 MA250 仍要高于 back 日前的 MA250，需要的最低价。
    明日 MA250 = (s249 + C)/250，20 日前的 MA250 = A（定值），
    令 (s249 + C)/250 > A 得 C > 250A − s249。"""
    if len(closes) < n + back - 1:
        return None
    s249 = float(np.sum(closes[-(n - 1):]))
    a = float(np.mean(closes[-(n + back - 1):-(back - 1)]))
    return n * a - s249


def critical_window(conds, closes, ma6):
    """把合取条件解成买/卖的可成交区间。

    纯 BIAS/RSI 的买侧是「价格跌到某个点以下」，只有一个上界；
    一旦混入 MA60 / MA250 这类趋势条件，买侧就变成「均线之上 + 乖离达标」的夹逼区间 (lo, hi)，
    两者贴在一起时区间为空——此时没有任何价格能同时满足，不能报一个假买点。
    返回 (lo, hi, no_window, why)。"""
    # 买侧 = 条件里有「阈值向下」的 rsi/bias（如 BIAS6<-2%）；均线条件本身只给下界，
    # 不能拿 conds[0] 判定——年线过滤写在最前面时会把方向判反
    buy_side = any(c[0] in ("rsi", "bias") and c[1] == "<" for c in conds)
    pts, floors, why = [], [], ""
    for ind, op, val in conds:
        if ind == "trail":
            continue                  # 跟踪止盈没有固定触发价：卖点由持仓峰值决定
        if ind == "ma60":
            f = ma60_floor(closes)
        elif ind == "ma250":
            f = ma_floor(closes, 250)
        elif ind == "ma250up":
            f = ma250_up_floor(closes)
        elif ind == "rsi":
            pts.append(rsi_price_for(closes, val))
            continue
        else:
            pts.append(bias_price(closes, val, ma6))
            continue
        if f is None:                 # 历史不够，算不出这条均线：不给假买点
            return (None, None, True, "历史不足")
        # ma_floor 是「C > 新 MAn」与「C < 新 MAn」的同一个临界点 s(n-1)/(n-1)：
        # MA250上 / MA60上 时它是价格下界（夹逼区间的 lo），
        # MA250下 / MA60下 时它是价格上界（要跌破它才成立），必须放进 pts 而不是 floors，
        # 否则会把「年线下才买」错误解成「年线上才买」的夹逼区间。
        if ind in ("ma60", "ma250") and op == "<":
            pts.append(f)
        else:
            floors.append(f)

    lo = max(floors) if floors else None
    if not pts:                       # 只有均线条件，理论上不会出现
        return (lo, None, False, "")

    if buy_side:
        hi = math.floor(min(pts) * 100) / 100.0 - 0.01   # 向下让 1 分，避开边界
        if lo is not None:
            lo = math.ceil(lo * 100) / 100.0 + 0.01
            if lo >= hi:
                return (lo, hi, True, "贴线")
        return (lo, hi, False, "")
    return (None, math.ceil(max(pts) * 100) / 100.0 + 0.01, False, "")


def critical_price(conds, closes, ma6):
    """单值触发价：买侧取区间上界，卖侧取最大。无解时返回 None。"""
    lo, hi, no_win, _why = critical_window(conds, closes, ma6)
    if no_win:
        return None
    return hi


def hit(conds, rsi, bias6, price=None, ma60=None, ma250=None, ma250up=None, peak=None):
    ok = True
    for ind, op, val in conds:
        if ind in ("ma60", "ma250"):
            line = ma60 if ind == "ma60" else ma250
            if price is None or line is None:
                ok = False
                break
            c = price > line
            ok = ok and (c if op == ">" else not c)
        elif ind == "ma250up":
            if ma250up is None:
                ok = False
                break
            ok = ok and bool(ma250up)
        elif ind == "trail":
            if peak is None:              # 空仓没有峰值，谈不上跟踪止盈
                ok = False
                break
            ok = ok and price <= peak * (1 - val / 100.0)
        else:
            cur = rsi if ind == "rsi" else bias6
            ok = ok and (cur < val if op == "<" else cur > val)
    return ok


# ---------------- 跟踪止盈的持仓状态 ----------------
# 卖点 = 持仓期最高价 × (1−N%)。它没有固定触发价，必须先把「按规则今天该不该持仓、
# 该次持仓的最高价是多少」算出来，否则只能报一个「—」。
# 口径与回测（zz_lib.run_tp）一致：信号日收盘成立、次日开盘买入；止损用盘中最低价，
# 跟踪用盘中最高价；同一天先止损后跟踪；买入当天不判卖（T+1）。

def _sma(x, n):
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        cs = np.concatenate(([0.0], np.cumsum(x)))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def _rsi_series(c, n=14):
    out = np.full(len(c), np.nan)
    if len(c) < n + 1:
        return out
    d = np.diff(c)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au, ad = up[:n].mean(), dn[:n].mean()
    out[n] = 100.0 if ad == 0 else 100.0 - 100.0 / (1.0 + au / ad)
    for i in range(n, len(d)):
        au = (au * (n - 1) + up[i]) / n
        ad = (ad * (n - 1) + dn[i]) / n
        out[i + 1] = 100.0 if ad == 0 else 100.0 - 100.0 / (1.0 + au / ad)
    return out


def trail_pos(dly, buy_conds, trail, stop=None):
    """按规则从前往后模拟，返回当前这笔持仓（None = 按规则应空仓）。
    只用于带 trail 的标的；返回 {"in","entry","peak","peakDate"}。"""
    n = len(dly)
    if n < 262:
        return None
    c = np.array([x[2] for x in dly], dtype=float)
    h = np.array([x[3] for x in dly], dtype=float)
    l = np.array([x[4] for x in dly], dtype=float)
    o = np.array([x[1] for x in dly], dtype=float)
    ma6, ma60 = _sma(c, 6), _sma(c, 60)
    ma250 = _sma(c, 250)
    rsi = _rsi_series(c)
    up250 = np.zeros(n, dtype=bool)
    up250[270:] = ma250[270:] > ma250[250:-20]
    bias6 = np.where(np.isnan(ma6), np.nan, (c / ma6 - 1) * 100)

    def ok_at(i):
        for ind, op, val in buy_conds:
            if ind == "rsi":
                v = rsi[i]
            elif ind == "bias":
                v = bias6[i]
            elif ind in ("ma60", "ma250"):
                # 均线条件比的是「收盘 vs 均线」，不是「均线 vs 0」，别和 rsi/bias 混成一类
                line = ma60[i] if ind == "ma60" else ma250[i]
                if np.isnan(line):
                    return False
                up = c[i] > line
                if not (up if op == ">" else not up):
                    return False
                continue
            elif ind == "ma250up":
                if not up250[i]:
                    return False
                continue
            else:
                continue
            if np.isnan(v):
                return False
            if not (v < val if op == "<" else v > val):
                return False
        return True

    hold, entry, peak = False, 0.0, 0.0
    buy_i, in_date, peak_date = -1, None, None
    for i in range(250, n):
        if not hold:
            if i + 1 < n and ok_at(i):
                hold = True
                buy_i = i + 1
                entry = float(o[buy_i])
                peak = float(max(h[buy_i], entry))
                in_date = peak_date = dly[buy_i][0]
            continue
        if float(h[i]) > peak:                 # 先刷新峰值（与回测顺序一致）
            peak = float(h[i])
            peak_date = dly[i][0]
        if i <= buy_i:                          # T+1：买入当天不判卖
            continue
        if stop and float(l[i]) <= entry * (1 - stop / 100.0):
            hold = False
            continue
        if float(l[i]) <= peak * (1 - trail / 100.0):
            hold = False
    if not hold:
        return None
    return {"in": in_date, "entry": round(entry, 2),
            "peak": round(peak, 2), "peakDate": peak_date}


# ---------------- 档位 ----------------

def bj_now():
    return dt.datetime.utcnow() + dt.timedelta(hours=8)


def resolve_slot(req, now):
    """目标档 → (实际档, 说明)。延迟跨过收盘时降级为收盘档，避免把收盘数据标成盘中。"""
    hm = now.hour * 60 + now.minute
    note = ""
    slot = req if req in SLOT_LABEL else "live"
    # 14:51 档已停用：只有 15:05 一个时点档。
    # 任务若提前跑（cron 极少提前，但可能），不能再降级成 1451——那个档页面已经不显示，
    # 数据会落进一个看不见的槽位。改为保留档位名，只加一句口径提示。
    if slot == "1505" and hm < 14 * 60 + 55:
        note = "抓取时点在 14:55 之前，价格可能不是最终收盘价"
    target = SLOT_TARGET.get(slot)
    skew = None
    if target:
        th, tm = [int(x) for x in target.split(":")]
        skew = hm - (th * 60 + tm)
    return slot, skew, note


def gap(price, ref, side):
    """买侧：正 = 现价已低于买点；卖侧：正 = 现价已高于卖点"""
    if not ref:
        return None
    return round((ref - price) / price * 100, 1) if side == "buy" \
        else round((price - ref) / ref * 100, 1)


# ---------------- 主流程 ----------------

def main():
    now = bj_now()
    today = now.strftime("%Y-%m-%d")
    req = os.environ.get("SLOT", "").strip() or "live"
    force = os.environ.get("FORCE", "").strip() in ("1", "true", "True")
    slot, skew, note = resolve_slot(req, now)
    final = slot == "1505"
    late = skew is not None and abs(skew) > 8
    log("北京时间 %s | 请求档 %s → 实际档 %s | 偏差 %s 分 %s"
        % (now.strftime("%Y-%m-%d %H:%M"), req, slot,
           "—" if skew is None else "%+d" % skew, note))

    # 大盘
    idx_px, _, _, _ = quote("sh000300")
    idx_daily = daily("sh000300", 200)
    idx_closes = [x[2] for x in idx_daily]
    if idx_daily and idx_daily[-1][0] == today:
        idx_closes[-1] = idx_px
    else:
        idx_closes.append(idx_px)
    ma60 = float(np.mean(idx_closes[-60:]))
    bull = idx_px > ma60
    log("沪深300 %.1f  MA60 %.1f  %s" % (idx_px, ma60, "多头" if bull else "空头"))

    rows, errors = [], []
    for code, name, buy_rule, sell_rule, mkt, bt in WATCH:
        sym = mkt + code
        try:
            price, high, low, prev = quote(sym)
            # 带跟踪止盈的标的要把整段历史跑一遍才能知道「持仓期最高价」，
            # 400 根（约 1.6 年）可能不够覆盖一次完整持仓，取 700 根。
            # 高股息票（bt.qfq）改走前复权日线，避免除息跳空污染 RSI 报出假买点。
            need = 700 if "峰值回撤" in sell_rule else 400
            fq_mode = ""
            if bt.get("qfq"):
                dly, fq_mode = daily_qfq(sym, need)
            else:
                dly = daily(sym, need)
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
                a250 = float(np.mean(closes[-269:-19]))     # 20 日前的 MA250
                up250 = sma250 > a250
                slope250 = (sma250 / a250 - 1) * 100
            rsi = rsi_wilder(closes)
            bias6 = (price / ma6 - 1) * 100
            bc, sc = parse_rule(buy_rule), parse_rule(sell_rule)
            buy_lo, buy_px, no_win, nowhy = resolve_buy(bc, closes, ma6, bt)
            # 跟踪止盈：卖点不是常数，先模拟出「按规则现在是否持仓、峰值多少」
            trail = next((c[2] for c in sc if c[0] == "trail"), None)
            pos = trail_pos(dly, bc, trail, bt.get("stop")) if trail else None
            if trail:
                sell_px = round(pos["peak"] * (1 - trail / 100.0), 2) if pos else None
            else:
                sell_px = critical_price(sc, closes, ma6)
            # 大盘门控：True/"bull" 需要多头，"bear" 需要空头；方向由 bt.mkt 决定
            mkt_req = bt.get("mkt")
            if mkt_req == "bear":
                blocked = bool(bull)          # 要空头却是多头 → 拦
            elif mkt_req:
                blocked = not bull            # 要多头却是空头 → 拦
            else:
                blocked = False
            buy_hit = hit(bc, rsi, bias6, price, sma60, sma250, up250)
            sell_hit = hit(sc, rsi, bias6, price, sma60, sma250, up250,
                           pos["peak"] if pos else None)
            if buy_hit:
                status = "wait" if blocked else "buy"
            elif sell_hit:
                status = "sell"
            else:
                status = "wait"
            ref = closes[-6] if len(closes) >= 6 else closes[0]
            d5 = (price / ref - 1) * 100

            rows.append({
                "code": code, "name": name, "sym": sym,
                "price": round(price, 3 if code in ETF_CODES else 2),
                "rsi": round(rsi, 1), "bias6": round(bias6, 2),
                "buy": None if buy_px is None else round(buy_px, 3 if code in ETF_CODES else 2),
                "buy_lo": None if buy_lo is None else round(buy_lo, 3 if code in ETF_CODES else 2),
                "nowin": bool(no_win), "nowhy": nowhy,
                "ma60": round(sma60, 2),
                "ma250": None if sma250 is None else round(sma250, 2),
                "ma250up": up250,
                "ma250slope": None if slope250 is None else round(slope250, 2),
                "sell": None if sell_px is None else round(sell_px, 3 if code in ETF_CODES else 2),
                "trail": trail,
                "pos": pos,
                "gap_buy": None if no_win else gap(price, buy_px, "buy"),
                "gap_sell": gap(price, sell_px, "sell"),
                "d5": round(d5, 1),
                "buy_rule": buy_rule, "sell_rule": sell_rule,
                "status": status,
                "blocked": ("待大盘转空" if mkt_req == "bear" else "大盘拦") if blocked and buy_hit else "",
                "bt": bt or None,
                "fq": fq_mode or "raw",
                "intraday": not final,
                # 前端「更新」按钮做本地实时重算用：最近 300 根收盘 + 最后一根的日期
                # （MA250 需要 251 根，130 根算不出年线）
                # 口径与这里完全一致（含当日实时价），所以两边算出来必然相同；
                # 标 fq="qfq" 的行整条 hist 是前复权，前端算法不变、结果仍一致
                "hist": [round(float(x), 3) for x in closes[-300:]],
                "histDate": dly[-1][0],
            })
            if fq_mode:
                log("      口径：%s" % ("前复权日线（腾讯 qfq / 东财 fqt=1）" if fq_mode == "qfq"
                                    else "复权源全挂，已降级不复权；除息窗口内 RSI 可能失真"))
            log("  %s %s %.2f RSI %.1f BIAS6 %+.2f 买 %s 卖 %s -> %s%s"
                % (code, name, price, rsi, bias6,
                   ("无窗口(%s)" % nowhy) if no_win else ("%.2f" % buy_px if buy_px else "—"),
                   "%.2f" % sell_px if sell_px else "—", status,
                   " (大盘拦截)" if blocked and buy_hit else ""))
            if trail:
                log("      跟踪止盈 %d%%：%s"
                    % (trail, ("自 %s 以 %.2f 持有，峰值 %.2f（%s）"
                               % (pos["in"], pos["entry"], pos["peak"], pos["peakDate"]))
                       if pos else "按规则当前空仓，无卖点"))
            if sma250 and "MA250" in buy_rule:
                log("      年线 %.2f %s %.2f%%  现价距年线 %+.1f%%"
                    % (sma250, "升" if up250 else "降",
                       slope250 if slope250 is not None else 0.0,
                       (price / sma250 - 1) * 100))
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
        data = {}

    # 跨交易日：清空旧档位
    if data.get("date") != today:
        data = {"date": today, "market": {}, "slots": {}}

    counts = {"buy": 0, "sell": 0, "wait": 0}
    for r in rows:
        counts[r["status"]] += 1

    data["date"] = today
    data["market"] = {"idx": round(idx_px, 1), "ma60": round(ma60, 1), "bull": bool(bull)}
    slots = {k: v for k, v in data.get("slots", {}).items() if k in SLOT_LABEL}

    payload = {
        "ts": now.strftime("%H:%M"),
        "label": SLOT_LABEL[slot],
        "target": SLOT_TARGET.get(slot),
        "skew": skew,
        "late": bool(late),
        "note": note,
        "final": final, "counts": counts, "rows": rows,
        # 沪深300 收盘序列，供前端本地重算大盘多空
        "mkt": {
            "closes": [round(float(x), 2) for x in idx_closes[-65:]],
            "date": idx_daily[-1][0] if idx_daily else today,
        },
    }

    keep_old = False
    if slot in slots and not force and skew is not None and abs(skew) > 25:
        # 补跑（明显偏离目标）不覆盖当天已抓到的真实档
        keep_old = True
        payload.update({"label": "补跑 %s" % now.strftime("%H:%M"),
                        "target": None, "skew": None, "late": False,
                        "note": "已保留原档"})
        slots["live"] = payload
        slot = "live"
    else:
        slots[slot] = payload

    data["slots"] = slots
    data["updated"] = now.strftime("%Y-%m-%d %H:%M")
    data["updated_slot"] = slot

    # hist 是纯数字长数组，indent 会把它铺成 130 行——压回一行，其余保持可读
    raw = json.dumps(data, ensure_ascii=False, indent=1)
    raw = re.sub(r"\[\s*\n\s*([-\d.,\s]+?)\s*\n\s*\]",
                 lambda m: "[" + ",".join(x.strip() for x in m.group(1).split(",")) + "]",
                 raw)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        f.write(raw)

    log("完成 %s%s | 买%d 卖%d 等%d | 失败 %d"
        % (slot, "(保留原档)" if keep_old else "", counts["buy"], counts["sell"],
           counts["wait"], len(errors)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
