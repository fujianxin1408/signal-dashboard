#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信号推送：把 data.json 里刚归档的那一档，变成一条微信消息。

设计要点
--------
· **只推 15:05 收盘档**（14:51 盘中档已于 2026-11-11 停用）。live 档（手动刷新、Actions 补跑）一律不推，
  否则每点一次页面上的「更新」就会响一次。
· **去重靠 notify_state.json**：同一天 + 同一档 + 同一只票 + 同一方向只推一次。
  同一只票同一天只响一声，且是收盘确认过的，
  该响两声就响两声，但重复执行同一档不会重复响。
· **推送失败不让 workflow 变红**：数据早就提交了，通知只是附加动作。
  失败打 ::warning:: 留在运行日志里；没配 token 也只告警、不报错。
· 周五 15:05 若无任何信号，发一条「本周无信号」心跳——否则 token 过期这种事
  可能几周都发现不了。心跳可用 HEARTBEAT=0 关掉。

环境变量
--------
PUSHPLUS_TOKEN   PushPlus 的 token（微信扫码 pushplus.plus 获取）
PUSHPLUS_TOPIC   可选，群组编码（一对多推送）
WECOM_URL        可选，企业微信群机器人 Webhook（二选一，优先级低于 PushPlus）
SERVERCHAN_KEY   可选，Server酱 SendKey
NTFY_TOPIC       可选，ntfy 主题（安卓装 ntfy App 订阅即可，零注册）
DINGTALK_URL     可选，钉钉群机器人 Webhook
FEISHU_URL       可选，飞书群机器人 Webhook
WXPUSHER_TOKEN   可选，WxPusher 应用 appToken（微信扫码即关注公众号，消息进微信）
WXPUSHER_UID     可选，接收人 UID，多个用逗号分隔；留空则推给该应用全部订阅者
HEARTBEAT        默认 1，设 0 关闭周五心跳
STATE_FILE       默认 <repo>/notify_state.json
"""
import datetime as dt
import json
import os
import re
import sys

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.environ.get("DATA_FILE") or os.path.join(ROOT, "data.json")
STATE_FILE = os.environ.get("STATE_FILE") or os.path.join(ROOT, "notify_state.json")
UA = {"User-Agent": "signal-notify/1.0"}

SLOT_NAME = {"1451": "盘中预警", "1505": "收盘确认"}
KEEP_DAYS = 5


def log(*a):
    print(*a, flush=True)


# ---------------- 推送通道 ----------------

def send_pushplus(title, content):
    url = os.environ.get("PUSHPLUS_API") or "https://www.pushplus.plus/send"
    body = {
        "token": os.environ["PUSHPLUS_TOKEN"],
        "title": title,
        "content": content,
        "template": "markdown",
        "channel": "wechat",
    }
    if os.environ.get("PUSHPLUS_TOPIC"):
        body["topic"] = os.environ["PUSHPLUS_TOPIC"]
    r = requests.post(url, json=body, headers=UA, timeout=20)
    js = r.json()
    if js.get("code") != 200:
        raise RuntimeError("PushPlus 返回 %s: %s" % (js.get("code"), js.get("msg")))
    return "PushPlus"


def send_wecom(title, content):
    url = os.environ["WECOM_URL"]
    md = "## %s\n%s" % (title, content.replace("**", "**"))
    r = requests.post(url, json={"msgtype": "markdown", "markdown": {"content": md}},
                      headers=UA, timeout=20)
    js = r.json()
    if js.get("errcode") != 0:
        raise RuntimeError("企业微信返回 %s: %s" % (js.get("errcode"), js.get("errmsg")))
    return "企业微信"


def send_serverchan(title, content):
    key = os.environ["SERVERCHAN_KEY"]
    url = "https://sctapi.ftqq.com/%s.send" % key
    r = requests.post(url, data={"title": title, "desp": content}, headers=UA, timeout=20)
    js = r.json()
    if js.get("code") not in (0, 200):
        raise RuntimeError("Server酱返回 %s: %s" % (js.get("code"), js.get("message")))
    return "Server酱"


def send_ntfy(title, content):
    """ntfy：安卓装 App、订阅一个随机 topic 就行，不用注册任何账号。
    标题走 JSON 字段，避免 HTTP header 塞中文。"""
    topic = os.environ["NTFY_TOPIC"]
    server = os.environ.get("NTFY_SERVER") or "https://ntfy.sh"
    r = requests.post(server.rstrip("/") + "/", json={
        "topic": topic,
        "title": title,
        "message": content,
        "markdown": True,
        "priority": 4,
        "tags": ["chart_with_upwards_trend"],
    }, headers=UA, timeout=20)
    if r.status_code >= 300:
        raise RuntimeError("ntfy 返回 %s: %s" % (r.status_code, r.text[:120]))
    return "ntfy"


def send_dingtalk(title, content):
    url = os.environ["DINGTALK_URL"]
    r = requests.post(url, json={
        "msgtype": "markdown",
        "markdown": {"title": title, "text": "### %s\n\n%s" % (title, content)},
    }, headers=UA, timeout=20)
    js = r.json()
    if js.get("errcode") != 0:
        raise RuntimeError("钉钉返回 %s: %s" % (js.get("errcode"), js.get("errmsg")))
    return "钉钉"


def send_feishu(title, content):
    url = os.environ["FEISHU_URL"]
    r = requests.post(url, json={
        "msg_type": "text",
        "content": {"text": "%s\n\n%s" % (title, content.replace("**", ""))},
    }, headers=UA, timeout=20)
    js = r.json()
    if js.get("code") not in (0, None) and js.get("StatusCode") not in (0, None):
        raise RuntimeError("飞书返回 %s" % js)
    return "飞书"


def send_wxpusher(title, content):
    """WxPusher：微信扫码关注公众号即可收，不需要装 App、不需要企业微信。
    推的是公众号消息，落在微信聊天列表里，和 PushPlus/Server酱 同一种形态。"""
    token = os.environ["WXPUSHER_TOKEN"]
    uids = [x.strip() for x in (os.environ.get("WXPUSHER_UID") or "").split(",") if x.strip()]
    body = {
        "appToken": token,
        "content": content,
        "summary": title[:100],
        "contentType": 3,          # 3 = markdown
        "uids": uids,
    }
    r = requests.post("https://wxpusher.zjiecode.com/api/send/message",
                      json=body, headers=UA, timeout=20)
    js = r.json()
    if js.get("code") != 1000:
        raise RuntimeError("WxPusher 返回 %s: %s" % (js.get("code"), js.get("msg")))
    return "WxPusher"


def channels():
    out = []
    if os.environ.get("WXPUSHER_TOKEN"):
        out.append(send_wxpusher)
    if os.environ.get("NTFY_TOPIC"):
        out.append(send_ntfy)
    if os.environ.get("DINGTALK_URL"):
        out.append(send_dingtalk)
    if os.environ.get("FEISHU_URL"):
        out.append(send_feishu)
    if os.environ.get("PUSHPLUS_TOKEN"):
        out.append(send_pushplus)
    if os.environ.get("WECOM_URL"):
        out.append(send_wecom)
    if os.environ.get("SERVERCHAN_KEY"):
        out.append(send_serverchan)
    return out


# ---------------- 文案 ----------------

def num(v, nd=2):
    if v is None:
        return "—"
    return ("%." + str(nd) + "f") % float(v)


def gap_text(r, side):
    """买侧：正=已跌破买点；卖侧：正=已超过卖点"""
    if side == "buy":
        g = r.get("gap_buy")
        if g is None:
            return ""
        return ("已低 %.1f%%" % g) if g > 0.05 else ("正好触价" if g > -0.05 else "还需跌 %.1f%%" % abs(g))
    g = r.get("gap_sell")
    if g is None:
        return ""
    return ("已超 %.1f%%" % g) if g > 0.05 else ""


def sell_note(r):
    """卖点可能由 RSI 触发，此时「现价距 BIAS 卖点」是负的，直接写「还需涨 x%」会自相矛盾。
    有指标到位就报指标，否则退回价格间距。"""
    rule = r.get("sell_rule") or ""
    m = re.search(r"RSI14>\s*([\d.]+)", rule)
    if m and float(r.get("rsi") or 0) > float(m.group(1)):
        return "RSI %s > %s 触发" % (num(r.get("rsi"), 1), m.group(1))
    m = re.search(r"BIAS6>\+?\s*([\d.]+)", rule)
    if m and float(r.get("bias6") or 0) > float(m.group(1)):
        return "BIAS6 %+.2f%% > %s%% 触发" % (float(r.get("bias6")), m.group(1))
    g = float(r.get("gap_sell") or 0)
    return ("已超 %.1f%%" % g) if g > 0.05 else "已触发"


def line_of(r, side):
    """一只票三行：名称/代码、触发价、指标（避免 <sub> 之类 HTML，PushPlus 只吃标准 markdown）"""
    note = gap_text(r, "buy") if side == "buy" else sell_note(r)
    l1 = "**%s** %s" % (r.get("name", ""), r.get("code", ""))
    l2 = "现价 %s　%s %s　%s" % (num(r.get("price")),
                                "买点" if side == "buy" else "卖点",
                                num(r.get("buy") if side == "buy" else r.get("sell")),
                                note)

    bits = ["BIAS6 %+.2f%%" % float(r.get("bias6") or 0)]
    if "RSI" not in note:                       # 触发理由里已经写过 RSI 就不再重复
        bits.append("RSI %s" % num(r.get("rsi"), 1))
    if r.get("ma250"):
        bits.append("年线 %s%s" % (num(r.get("ma250")), "↑" if r.get("ma250up") else "↓"))
    bt = r.get("bt") or {}
    if bt.get("n"):
        bits.append("回测 %d 笔 胜率 %s%%" % (bt["n"], num(bt.get("win"), 1).rstrip("0").rstrip(".")))
    return l1 + "\n" + l2 + "\n" + "　·　".join(bits)


def build(rows, slot, data):
    buys = [r for r in rows if r.get("status") == "buy"]
    sells = [r for r in rows if r.get("status") == "sell"]
    kind = SLOT_NAME[slot]
    mk = data.get("market") or {}

    stamp = dt.datetime.utcnow() + dt.timedelta(hours=8)
    # 标题固定含「信号」二字：钉钉自定义机器人用「关键词」做安全校验时，
    # 关键词填「信号」即可命中这条和心跳，不用去算加签
    title = "【%s】信号 · %d 买 %d 卖 · %s" % (kind, len(buys), len(sells),
                                          stamp.strftime("%m-%d %H:%M"))

    parts = ["### %s · %d 买 %d 卖" % (kind, len(buys), len(sells)),
             "%s　沪深300 %s（%s）" % (data.get("date", ""), num(mk.get("idx"), 1),
                                      "多头" if mk.get("bull") else "空头")]
    if slot == "1451":
        parts.append("**盘中价推算，收盘可能变**——先准备好，别急着下单，15:05 会给确认。")

    if buys:
        parts.append("**— 买入触发 —**")
        parts += [line_of(r, "buy") for r in buys]
    if sells:
        parts.append("**— 卖出触发 —**")
        parts += [line_of(r, "sell") for r in sells]

    parts.append("———\n收盘价口径；同日同档不重复推送。仅供参考，不构成投资建议。")
    return title, "\n\n".join(parts)


def heartbeat(data):
    mk = data.get("market") or {}
    stamp = dt.datetime.utcnow() + dt.timedelta(hours=8)
    title = "【周检】本周无信号 · %s" % stamp.strftime("%m-%d")
    body = ("### 本周无信号\n\n%s　沪深300 %s（%s）　共 %d 只标的\n\n"
            "这条是心跳，收到说明推送链路正常。"
            % (data.get("date", ""), num(mk.get("idx"), 1),
               "多头" if mk.get("bull") else "空头",
               len((data.get("slots", {}).get(data.get("updated_slot", "")) or {}).get("rows", []))))
    return title, body


# ---------------- 去重状态 ----------------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    state = {k: v for k, v in state.items() if k >= (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()}
    raw = json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True)
    old = None
    try:
        old = open(STATE_FILE, encoding="utf-8").read()
    except OSError:
        pass
    if old == raw:
        return False
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(raw)
    return True


# ---------------- 主流程 ----------------

_now = __import__("datetime").datetime.utcnow() + __import__("datetime").timedelta(hours=8)
TEST_TITLE = "【测试】推送链路已接通 · %s" % _now.strftime("%m-%d %H:%M")
TEST_BODY = """### 测试消息 · 1 买 1 卖

这条不是真信号，是用来确认微信能收到。

**— 买入触发 —**

**示例股份** 000000
现价 10.00　买点 10.20　已低 2.0%
BIAS6 -2.31%　·　RSI 38.4　·　年线 10.90↑　·　回测 72 笔 胜率 59.7%

**— 卖出触发 —**

**示例科技** 111111
现价 20.00　卖点 19.80　已超 1.0%
BIAS6 +7.42%　·　年线 18.20↑

———
每天 15:05 收盘确认；同一只票只推一次。"""


def main():
    if "--test" in sys.argv:
        chans = channels()
        if not chans:
            log("没有可用通道：先设置 NTFY_TOPIC / DINGTALK_URL / FEISHU_URL / PUSHPLUS_TOKEN 之一")
            return 1
        for fn in chans:
            try:
                log("测试推送 via %s 成功" % fn(TEST_TITLE, TEST_BODY))
            except Exception as e:  # noqa: BLE001
                log("测试推送失败（%s）：%s" % (fn.__name__, e))
                return 1
        return 0

    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        log("::warning::读不到 data.json：%s" % e)
        return 0

    slot = data.get("updated_slot") or ""
    if slot not in SLOT_NAME:
        log("档位 %r 不属于 15:05，跳过提醒" % slot)
        return 0
    payload = (data.get("slots") or {}).get(slot) or {}
    label = payload.get("label") or ""
    if label.startswith("补跑"):
        log("本档为补跑（%s），跳过提醒" % label)
        return 0

    rows = payload.get("rows") or []
    state = load_state()
    day = data.get("date") or dt.date.today().isoformat()
    sent_today = state.setdefault(day, {}).setdefault(slot, [])

    hits = [r for r in rows if r.get("status") in ("buy", "sell")]
    fresh = []
    for r in hits:
        k = "%s|%s" % (r.get("code"), r.get("status"))
        if k in sent_today:
            continue
        fresh.append((k, r))

    chans = channels()
    if not chans:
        log("::warning::未配置任何推送通道，本次不推送")
        return 0

    if not fresh:
        log("本档无新增信号（已有 %d 条推过）" % len(hits))
        # 周五收盘且全周静默 → 发心跳，避免 token 失效后无声无息
        hb = os.environ.get("HEARTBEAT", "1") not in ("0", "false", "False")
        is_fri = (dt.datetime.utcnow() + dt.timedelta(hours=8)).weekday() == 4
        if slot == "1505" and is_fri and hb:
            title, body = heartbeat(data)
            for fn in chans:
                try:
                    log("已推送心跳 via %s" % fn(title, body))
                except Exception as e:  # noqa: BLE001
                    log("::warning::%s 心跳失败：%s" % (fn.__name__, e))
        return 0

    title, body = build([r for _k, r in fresh], slot, data)
    ok = False
    for fn in chans:
        try:
            log("已推送 via %s：%s" % (fn(title, body), title))
            ok = True
        except Exception as e:  # noqa: BLE001
            log("::warning::%s 推送失败：%s" % (fn.__name__, e))

    if not ok:
        # 一条都没发出去就不记账，下一次运行会重试，不会把信号永久吞掉
        log("::warning::全部通道推送失败，本次不记入去重状态")
        return 0

    for k, _r in fresh:
        sent_today.append(k)
    save_state(state)
    log("本次新增 %d 条：%s" % (len(fresh), ",".join(k for k, _ in fresh)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
