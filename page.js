const SNAP = __SNAPSHOT__;

let DATA = SNAP || { slots: {} };
let CUR = null;
let REFRESHING = false;

// 更新按钮的触发代理（Cloudflare Worker）。留空则走浏览器本地实时重算
const WORKER_URL = '';

/* ================== 实时重算引擎 ==================
 * 这是纯静态站，浏览器跑不了 Python。所以：「预埋历史 + 实时价 → 前端复算」
 *   · 历史：data.json 每行带 hist（最近 300 根不复权收盘）、mkt.closes（沪深300）
 *   · 实时：qt.gtimg.cn 批量报价，一次请求拿全部标的，ACAO:* 可直接跨域
 *   · 算法：逐行复刻 ci/update_ci.py 的 MA6 / BIAS6 / RSI14 Wilder /
 *           buy 闭式解 / 卖点反解 / MA60·MA250 夹逼窗口，口径完全一致
 * 结果：点一下 1 秒内出结果，不用等 Actions 那 2 分钟。
 */
const QT_URL = 'https://qt.gtimg.cn/q=';
const TODAY = (function () {
  try { return new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' }); }
  catch (e) {
    const d = new Date();
    return new Date(d.getTime() + (d.getTimezoneOffset() + 480) * 60000).toISOString().slice(0, 10);
  }
})();
let LIVE_AT = '';        // 交易所时间戳 HH:MM:SS
let LAST_TS = '';        // 最近一次报价里的交易所时间戳（YYYYMMDDHHMMSS）
let LIVE_STALE = 0;      // 因数据陈旧未能重算的只数

function _avg(a) { let s = 0; for (let i = 0; i < a.length; i++) s += a[i]; return s / a.length; }

// Wilder 平滑到最后一根的 (平均涨幅, 平均跌幅)
function _wilderState(c, n) {
  n = n || 14;
  let au = 0, ad = 0;
  for (let i = 1; i <= n; i++) { const d = c[i] - c[i - 1]; if (d > 0) au += d; else ad -= d; }
  au /= n; ad /= n;
  for (let i = n + 1; i < c.length; i++) {
    const d = c[i] - c[i - 1];
    au = (au * (n - 1) + (d > 0 ? d : 0)) / n;
    ad = (ad * (n - 1) + (d < 0 ? -d : 0)) / n;
  }
  return [au, ad];
}

function rsi14(c) {
  if (c.length < 15) return 50;
  const s = _wilderState(c, 14);
  return s[1] === 0 ? 100 : 100 - 100 / (1 + s[0] / s[1]);
}

// RSI 触发价：假设 5 个交易日匀速走到目标位（单日跳到位不现实，会算出离谱的涨幅）
function rsiPriceFor(c, target) {
  const n = 14, days = 5, last = c[c.length - 1];
  const st = _wilderState(c, n), au0 = st[0], ad0 = st[1];
  const at = function (p) {
    const step = Math.pow(p / last, 1 / days);
    let px = last, a = au0, b = ad0;
    for (let k = 0; k < days; k++) {
      const nx = px * step, ch = nx - px;
      a = (a * (n - 1) + (ch > 0 ? ch : 0)) / n;
      b = (b * (n - 1) + (ch < 0 ? -ch : 0)) / n;
      px = nx;
    }
    return b <= 0 ? 100 : 100 - 100 / (1 + a / b);
  };
  let lo = last * 0.55, hi = last * 1.7, fLo = at(lo), fHi = at(hi);
  if ((fLo - target) * (fHi - target) > 0) return last;   // 目标在可达区间外
  for (let k = 0; k < 70; k++) {
    const mid = (lo + hi) / 2;
    if ((at(mid) - target) * (fLo - target) > 0) { lo = mid; fLo = at(mid); }
    else { hi = mid; }
  }
  return (lo + hi) / 2;
}

// BIAS6 闭式解：P = s5×(1+k)÷(5−k)，s5 为「不含今日」的最近 5 日收盘和
// 直接用 ma6×(1+k) 会偏高——因为价格下跌本身也会把 MA6 拉低
function biasPrice(c, k) {
  if (c.length < 6) return c[c.length - 1];
  let s5 = 0;
  for (let i = c.length - 6; i < c.length - 1; i++) s5 += c[i];
  return s5 * (1 + k / 100) / (5 - k / 100);
}

// 明日收盘要站上 MAn 的最低价：明日 MAn=(s(n−1)+C)/n，令 C>MAn 得 C>s(n−1)/(n−1)
function maFloor(c, n) {
  if (c.length < n) return null;
  let s = 0;
  for (let i = c.length - (n - 1); i < c.length; i++) s += c[i];
  return s / (n - 1);
}
function ma60Floor(c) { return maFloor(c, 60); }

// 明日 MA250 仍要高于 20 日前的 MA250：C > 250A − s249
function ma250UpFloor(c) {
  const n = 250, back = 20;
  if (c.length < n + back - 1) return null;
  let s249 = 0;
  for (let i = c.length - (n - 1); i < c.length; i++) s249 += c[i];
  let sa = 0;
  for (let i = c.length - (n + back - 1); i < c.length - (back - 1); i++) sa += c[i];
  return sa - s249;          // = 250·A − s249，A 为 20 日前那根 MA250
}

function parseRule(rule) {
  const out = [];
  String(rule || '').replace(/且/g, '|').split('|').forEach(function (p) {
    p = p.replace(/\s/g, '');
    if (!p) return;
    if (p.indexOf('RSI14') === 0) {
      const op = p.indexOf('<') >= 0 ? '<' : '>';
      out.push(['rsi', op, parseFloat(p.split(op)[1])]);
    } else if (p.indexOf('BIAS6') === 0) {
      const op = p.indexOf('<') >= 0 ? '<' : '>';
      out.push(['bias', op, parseFloat(p.split(op)[1].replace('%', '').replace('+', ''))]);
    } else if (p.indexOf('MA250') === 0) {
      out.push(p.indexOf('升') >= 0
        ? ['ma250up', '>', 0]
        : ['ma250', (p.indexOf('上') >= 0 || p.indexOf('>') >= 0) ? '>' : '<', 0]);
    } else if (p.indexOf('MA60') === 0) {
      out.push(['ma60', (p.indexOf('上') >= 0 || p.indexOf('>') >= 0) ? '>' : '<', 0]);
    }
  });
  return out;
}

// 合取条件 → 可成交区间 (lo, hi, nowin, why)。买侧混入 MA60上 / MA250上 时是夹逼：
// 价格必须「在均线之上」同时又「乖离够低」，两者贴住时区间为空，没有买点。
// 2026-11-12：MA250下 / MA60下 则给上界（与「上」共用同一个临界点，方向相反）。
function critWindow(conds, c, ma6) {
  // 买侧 = 条件里有「阈值向下」的 rsi/bias；均线条件只给下界，不能拿 conds[0] 判方向
  const buySide = conds.some(k => (k[0] === 'rsi' || k[0] === 'bias') && k[1] === '<');
  const pts = [], floors = [];
  let why = '';
  for (let i = 0; i < conds.length; i++) {
    const k = conds[i];
    let f;
    if (k[0] === 'ma60') f = ma60Floor(c);
    else if (k[0] === 'ma250') f = maFloor(c, 250);
    else if (k[0] === 'ma250up') f = ma250UpFloor(c);
    else { pts.push(k[0] === 'rsi' ? rsiPriceFor(c, k[2]) : biasPrice(c, k[2])); continue; }
    if (f === null) return [null, null, true, '历史不足'];   // 算不出均线，不给假买点
    // MA250下 / MA60下 用的临界点与「上」是同一个数，但不等号方向相反：
    // 它是价格上界（要跌破它才成立），必须放进 pts；否则会算成夹逼区间、报出假买点。
    if ((k[0] === 'ma60' || k[0] === 'ma250') && k[1] === '<') pts.push(f);
    else floors.push(f);
  }
  const lo0 = floors.length ? Math.max.apply(null, floors) : null;
  if (!pts.length) return [lo0, null, false, ''];
  if (buySide) {
    const hi = Math.floor(Math.min.apply(null, pts) * 100) / 100 - 0.01;  // 让 1 分，避开边界
    if (lo0 !== null) {
      const lo = Math.ceil(lo0 * 100) / 100 + 0.01;
      if (lo >= hi) return [lo, hi, true, '贴线'];
      return [lo, hi, false, ''];
    }
    return [null, hi, false, ''];
  }
  return [null, Math.ceil(Math.max.apply(null, pts) * 100) / 100 + 0.01, false, ''];
}

function hitTest(conds, rsi, bias6, price, ma60, ma250, up250) {
  for (let i = 0; i < conds.length; i++) {
    const ind = conds[i][0], op = conds[i][1], val = conds[i][2];
    let ok;
    if (ind === 'ma60' || ind === 'ma250') {
      const line = ind === 'ma60' ? ma60 : ma250;
      if (!line) return false;
      const up = price > line; ok = (op === '>') ? up : !up;
    } else if (ind === 'ma250up') {
      if (up250 === null || up250 === undefined) return false;
      ok = !!up250;
    } else { const cur = (ind === 'rsi') ? rsi : bias6; ok = (op === '<') ? cur < val : cur > val; }
    if (!ok) return false;
  }
  return true;
}

function gapOf(price, ref, side) {
  if (!ref) return null;
  const v = side === 'buy' ? (ref - price) / price * 100 : (price - ref) / ref * 100;
  return Math.round(v * 10) / 10;
}

// 一行 = 预埋历史 + 今日实时价（当天已有一根就替换，否则追加）
function recalcRow(r, price, mktBull) {
  const h = (r.hist || []).slice();
  if (h.length < 20) return null;
  const lag = r.histDate ? (Date.parse(TODAY) - Date.parse(r.histDate)) / 864e5 : 99;
  if (!(lag >= 0 && lag <= 4)) return null;          // 基准太旧，宁可不算也不给错数
  if (r.histDate === TODAY) h[h.length - 1] = price; else h.push(price);

  const ma6 = _avg(h.slice(-6));
  const sma60 = h.length >= 60 ? _avg(h.slice(-60)) : _avg(h);
  const sma250 = h.length >= 250 ? _avg(h.slice(-250)) : null;
  let up250 = null, slope250 = null;
  if (sma250 !== null && h.length >= 270) {
    const a250 = _avg(h.slice(-269, -19));           // 20 日前的 MA250
    up250 = sma250 > a250;
    slope250 = (sma250 / a250 - 1) * 100;
  }
  const rsi = rsi14(h);
  const bias6 = (price / ma6 - 1) * 100;
  const bc = parseRule(r.buy_rule), sc = parseRule(r.sell_rule);
  const bw = critWindow(bc, h, ma6);
  const sw = critWindow(sc, h, ma6);
  const blocked = !!(r.bt && r.bt.mkt) && !mktBull;
  const bHit = hitTest(bc, rsi, bias6, price, sma60, sma250, up250);
  const sHit = hitTest(sc, rsi, bias6, price, sma60, sma250, up250);

  const o = {};
  for (const k in r) o[k] = r[k];
  const dec = Math.abs(price) < 10 ? 3 : 2;
  o.price = Number(price.toFixed(dec));
  o.rsi = Number(rsi.toFixed(1));
  o.bias6 = Number(bias6.toFixed(2));
  o.ma60 = Number(sma60.toFixed(2));
  o.ma250 = sma250 === null ? null : Number(sma250.toFixed(2));
  o.ma250up = up250;
  o.ma250slope = slope250 === null ? null : Number(slope250.toFixed(2));
  o.buy = (bw[1] === null) ? null : Number(bw[1].toFixed(dec));
  o.buy_lo = (bw[0] === null) ? null : Number(bw[0].toFixed(dec));
  o.nowin = !!bw[2];
  o.nowhy = bw[3] || '';
  o.sell = (sw[1] === null) ? null : Number(sw[1].toFixed(dec));
  o.gap_buy = bw[2] ? null : gapOf(price, bw[1], 'buy');
  o.gap_sell = gapOf(price, sw[1], 'sell');
  o.d5 = Number((((price / h[h.length - 6]) - 1) * 100).toFixed(1));
  o.status = bHit ? (blocked ? 'wait' : 'buy') : (sHit ? 'sell' : 'wait');
  o.blocked = (blocked && bHit) ? '大盘' : '';
  o.intraday = true;
  return o;
}

// 腾讯批量报价（GBK），一次请求取全部标的
async function fetchQuotes(syms) {
  const res = await fetch(QT_URL + syms.join(','), { cache: 'no-store' });
  if (!res.ok) throw new Error('报价接口 HTTP ' + res.status);
  const buf = await res.arrayBuffer();
  let txt;
  try { txt = new TextDecoder('gbk').decode(buf); }
  catch (e) { txt = new TextDecoder('gb18030').decode(buf); }   // 老 Safari 兜底
  const map = {};
  txt.split(';').forEach(function (line) {
    const m = line.match(/v_([a-z]{2}\d{6})="([^"]*)"/);
    if (!m) return;
    const f = m[2].split('~');
    const px = parseFloat(f[3]) || parseFloat(f[4]);
    if (!px || !isFinite(px)) return;
    map[m[1]] = {
      price: px, prev: parseFloat(f[4]) || px,
      high: parseFloat(f[33]) || px, low: parseFloat(f[34]) || px,
      t: f[30] || '',
    };
  });
  return map;
}

function toast(msg) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.style.opacity = '1';
  clearTimeout(el._tm);
  el._tm = setTimeout(function () { el.style.opacity = '0'; }, 2600);
}


const SLOT_KEYS = ['live', '1505'];
const SLOT_NAME = { live: '实时', '1505': '15:05' };

// 触发股票排前面；同档位内按「离买点/卖点还差多少」排序
function sortRows(rows) {
  const rank = { buy: 0, sell: 1, wait: 2 };
  const gb = r => r.nowin ? -999 :
    ((r.gap_buy === null || r.gap_buy === undefined)
      ? fallbackGap(r) : Number(r.gap_buy));
  return rows.slice().sort((a, b) => {
    const ra = rank[a.status] ?? 3, rb = rank[b.status] ?? 3;
    if (ra !== rb) return ra - rb;
    if (a.status === 'buy') return gb(b) - gb(a);           // 突破买点的幅度越大越靠前
    if (a.status === 'sell') return (Number(b.gap_sell) || 0) - (Number(a.gap_sell) || 0);
    return gb(b) - gb(a);                                    // 等待中：最接近买点的靠前
  });
}

// 旧版 data.json 没有 gap_* 字段时，用现价/买点现算（口径与后端一致）
function fallbackGap(r) {
  if (!r.buy || !r.price) return -999;
  return (r.buy - r.price) / r.price * 100;
}

function fmt(n, d) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  if (d === undefined) d = (Math.abs(Number(n)) < 10 && !Number.isInteger(Number(n))) ? 3 : 2;
  return Number(n).toFixed(d);
}

// 距买点：负=还需跌，正=已低于买点（进入买区）
function gapBuyHtml(r) {
  if (r.nowin) {
    const t = r.nowhy === '历史不足' ? '数据不足，待补' : '贴线，无买点';
    return '<i class="gap">' + t + '</i>';
  }
  const g = (r.gap_buy === null || r.gap_buy === undefined) ? fallbackGap(r) : Number(r.gap_buy);
  if (!isFinite(g)) return '';
  if (g > 0.05) return '<i class="gap pos">已低 ' + g.toFixed(1) + '%</i>';
  if (g > -0.05) return '<i class="gap pos">正好触价</i>';
  // 还需跌不到 2%：橙色标出，扫一眼就知道谁快到了
  if (g > -2) return '<i class="gap near">还需跌 ' + Math.abs(g).toFixed(1) + '%</i>';
  return '<i class="gap">还需跌 ' + Math.abs(g).toFixed(1) + '%</i>';
}

// 距卖点：正=已超卖点，负=还需涨
function gapSellHtml(r) {
  const g = r.gap_sell === null || r.gap_sell === undefined ? null : Number(r.gap_sell);
  if (g === null || !isFinite(g)) return '';
  if (g > 0.05) return '<i class="gap sell">已超 ' + g.toFixed(1) + '%</i>';
  if (g > -2) return '<i class="gap near">还需涨 ' + Math.abs(g).toFixed(1) + '%</i>';
  return '<i class="gap">还需涨 ' + Math.abs(g).toFixed(1) + '%</i>';
}

function btText(bt) {
  if (!bt) return '';
  const p = [];
  if (bt.n) p.push(bt.n + ' 笔');
  if (bt.win) p.push(bt.win.toFixed ? bt.win.toFixed(1) : bt.win);
  let s = '回测 ' + (bt.n || '—') + ' 笔 · 胜率 ' + fmt(bt.win, 1) + '% · 累计 ';
  s += (bt.total > 0 ? '+' : '') + fmt(bt.total, 0) + '%';
  if (bt.stop) s += ' · 止损 ' + bt.stop + '%';
  return s;
}

// 年线摘要（只有买卖规则里带 MA250 的标的才显示）：年线值 + 20 日斜率 + 现价距年线
function ma250Text(r) {
  if (r.ma250 === null || r.ma250 === undefined || !r.price) return '';
  if (String(r.buy_rule || '').indexOf('MA250') < 0) return '';
  const d = (r.price / r.ma250 - 1) * 100;
  let s = ' ｜ <span class="nw">年线 ' + fmt(r.ma250, 2);
  if (r.ma250up !== null && r.ma250up !== undefined) {
    s += ' <i class="gap ' + (r.ma250up ? 'pos' : 'sell') + '">' +
      (r.ma250up ? '↑' : '↓') +
      (r.ma250slope === null || r.ma250slope === undefined ? '' : Math.abs(r.ma250slope).toFixed(2) + '%') +
      '</i>';
  }
  s += '</span> <span class="nw">现价距年线 <b class="' + (d >= 0 ? 'up' : 'down') + '">' +
    (d >= 0 ? '+' : '') + d.toFixed(1) + '%</b></span>';
  return s;
}

/* ================== 持仓成本 ==================
 * 用户填的成本价只存在浏览器 localStorage，不上传、不进 data.json。
 * 有了成本才能回答三个问题：现在赚亏多少、涨到卖点能赚多少、跌到止损亏多少。
 * 止损幅度取该票自己的回测参数 bt.stop（如扬杰 10%），没有就如实说「未设」，
 * 不要随便套一个 -10% 冒充策略风控。
 */
var POS_KEY = 'sig_pos_v1';
var POS = {};
try { POS = JSON.parse(localStorage.getItem(POS_KEY) || '{}') || {}; } catch (e) { POS = {}; }
var ROWREF = {};

function savePos() {
  try { localStorage.setItem(POS_KEY, JSON.stringify(POS)); } catch (e) {}
}

function posInner(r) {
  var cost = Number(POS[r.code]);
  if (!cost || !r.price) return '';
  var px = Number(r.price);
  var pnl = (px / cost - 1) * 100;
  var out = [];
  out.push('<span class="dim">成本</span> <b>' + fmt(cost) + '</b>');
  var word = pnl > 0.05 ? '浮盈' : (pnl < -0.05 ? '浮亏' : '持平');
  out.push(word + ' <b class="' + (pnl > 0.05 ? 'up' : (pnl < -0.05 ? 'down' : '')) + '">' +
    (pnl > 0 ? '+' : '') + pnl.toFixed(1) + '%</b>');

  // 卖点：绝对价位由 BIAS6 反解得出，与成本无关；这里只算「相对你的成本赚多少」
  // 成本高于卖点时，等到卖点仍然是亏的——必须点破，否则容易误以为「拿着就能回本」
  if (r.sell) {
    var gain = (r.sell / cost - 1) * 100;
    var need = (r.sell / px - 1) * 100;
    var tail;
    if (gain < -0.05 && need > 0.05) {
      tail = '<b class="down">（低于成本，等它不划算）</b>';
    } else if (need > 0.05) {
      tail = '<span class="dim">（还需涨 ' + need.toFixed(1) + '%）</span>';
    } else {
      tail = '<b class="up">（价格已到，该卖）</b>';
    }
    out.push('卖点 <b>' + fmt(r.sell) + '</b> <span class="dim">→</span> ' +
      (gain > 0 ? '+' : '') + gain.toFixed(1) + '% ' + tail);
  } else {
    out.push('卖点 <span class="dim">—</span>');
  }

  // 止损：成本 × (1 − stop%)，跌到就按纪律走
  var sp = Number((r.bt && r.bt.stop) || 0);
  if (sp > 0) {
    var stopPx = cost * (1 - sp / 100);
    var toStop = (stopPx / px - 1) * 100;
    out.push('止损 <b>' + fmt(stopPx) + '</b> <span class="dim">→ -' + sp + '% </span>' +
      (toStop < -0.05
        ? '<span class="dim">（再跌 ' + Math.abs(toStop).toFixed(1) + '%）</span>'
        : '<b class="down">（已跌破，按纪律走）</b>'));
  } else if (r.ma60) {
    // 没有硬止损的票（如中国平安回测显示加止损反而拖累），风控线就用买点条件本身：
    // 买入要求站上 MA60，那跌破 MA60 就是趋势假设被推翻，比拍一个百分比更讲得通
    var d60 = (r.ma60 / px - 1) * 100;
    out.push('风控 <b>' + fmt(r.ma60) + '</b> ' +
      (d60 < -0.05
        ? '<span class="dim">（跌破 MA60 走，再跌 ' + Math.abs(d60).toFixed(1) + '%）</span>'
        : '<b class="down">已在 MA60 下方，趋势已破</b>'));
  } else {
    out.push('止损 <span class="dim">该策略未设</span>');
  }
  return out.join('　·　');
}

// 输入即算：只改这一行的信息条，不重绘整页——否则输入框会丢焦点
function onPosInput(e) {
  var el = e.target;
  if (!el || !el.classList || !el.classList.contains('pin')) return;
  var code = el.dataset.code;
  var v = parseFloat(String(el.value).replace(/[^\d.]/g, ''));
  if (v > 0) POS[code] = v; else delete POS[code];
  savePos();
  var row = el.closest ? el.closest('.row') : null;
  var line = row && row.querySelector('.posline');
  if (line && ROWREF[code]) line.innerHTML = posInner(ROWREF[code]);
}
document.addEventListener('input', onPosInput);

function render() {
  const D = DATA;
  if (!D || !D.slots) return;

  document.getElementById('date').textContent = D.date || '';
  const m = D.market || {};
  document.getElementById('mktBadge').innerHTML =
    '<span class="badge ' + (m.bull ? 'bull' : 'bear') + '">' +
    (m.bull ? '大盘多头' : '大盘空头') + '</span>';
  document.getElementById('idx').textContent =
    '沪深300 ' + fmt(m.idx, 1) + ' · MA60 ' + fmt(m.ma60, 1);
  document.getElementById('upd').textContent = LIVE_AT
    ? ('实时 ' + LIVE_AT.slice(8, 10) + ':' + LIVE_AT.slice(10, 12) +
       ':' + LIVE_AT.slice(12, 14) + '　基准 ' + (D.updated || '—'))
    : (D.updated ? '更新 ' + D.updated : '');

  // 固定三个时点，第一个是刷新按钮
  const tabs = document.getElementById('tabs');
  tabs.innerHTML = SLOT_KEYS.map(k => {
    if (k === 'live') {
      const cls = 'tab tab-live' + (CUR === k ? ' on' : '') + (REFRESHING ? ' busy' : '');
      return '<div class="' + cls + '" id="btnLive">' +
        (REFRESHING ? '刷新中…' : '更新') + '</div>';
    }
    const s = D.slots[k];
    const cls = 'tab' + (k === CUR ? ' on' : '') + (s ? '' : ' off');
    const t = s ? s.ts : '';
    return '<div class="' + cls + '" data-k="' + k + '">' + SLOT_NAME[k] +
      (t ? '<span class="tabts">' + t + '</span>' : '') + '</div>';
  }).join('');

  tabs.querySelectorAll('.tab[data-k]').forEach(el => {
    el.onclick = () => { CUR = el.dataset.k; render(); };
  });
  const btn = document.getElementById('btnLive');
  if (btn) btn.onclick = refreshLive;

  const slot = D.slots[CUR];
  if (!slot) {
    document.getElementById('list').innerHTML =
      '<div class="card"><div class="sep">该时点还没有数据</div></div>';
    return;
  }

  const c = slot.counts || {};
  const hitN = (c.buy || 0) + (c.sell || 0);
  let stamp = slot.ts || '';
  if (slot.target && slot.ts && slot.ts !== slot.target) {
    const sk = (slot.skew === null || slot.skew === undefined) ? null : Number(slot.skew);
    stamp = slot.target + ' 档 · 实际 ' + slot.ts + ' 生成' +
      (sk === null ? '' : '（' + (sk > 0 ? '延迟 ' : '提前 ') + Math.abs(sk) + ' 分）');
  }

  const rows = sortRows(slot.rows);

  const tagOf = r => {
    if (r.status === 'buy') return '<span class="tag t-buy">买入</span>';
    if (r.status === 'sell') return '<span class="tag t-sell">卖出</span>';
    return '<span class="tag t-wait">等待</span>' +
      (r.blocked ? '<span class="tag t-blk">' + r.blocked + '拦</span>' : '');
  };

  let html =
    '<div class="card">' +
    '<div class="bar"><div class="barl">' +
    '<span>买入 <b class="c-buy">' + (c.buy || 0) + '</b></span>' +
    '<span>卖出 <b class="c-sell">' + (c.sell || 0) + '</b></span>' +
    '<span>等待 <b class="c-wait">' + (c.wait || 0) + '</b></span>' +
    '</div><div class="barr">' + stamp + ' · 共 ' + slot.rows.length + ' 只' +
    (hitN ? ' · 触发 ' + hitN : '') + '</div></div>' +
    '<div class="row hd">' +
    '<div>名称</div><div>现价</div><div class="hide-m">RSI14</div><div class="hide-m">BIAS6</div>' +
    '<div>买点</div><div class="hide-m">卖点</div><div>信号</div><div>持仓</div></div>';

  let lastStatus = null;
  const grpCnt = {};
  rows.forEach(r => { grpCnt[r.status] = (grpCnt[r.status] || 0) + 1; });
  ROWREF = {};
  rows.forEach(r => {
    ROWREF[r.code] = r;
    if (r.status !== lastStatus) {
      const t = { buy: '买入触发', sell: '卖出触发', wait: '其余观察' }[r.status];
      html += '<div class="sep s-' + r.status + '">' + t +
        ' <span class="cnt">· ' + (grpCnt[r.status] || 0) + ' 只</span></div>';
      lastStatus = r.status;
    }
    html +=
      '<div class="row s-' + r.status + '">' +
      '<div class="name">' + r.name + '<span class="code">' + r.code + '</span></div>' +
      '<div class="px">' + fmt(r.price) + '</div>' +
      '<div class="num hide-m">' + fmt(r.rsi, 1) + '</div>' +
      '<div class="num hide-m ' + (r.bias6 > 0 ? 'up' : 'down') + '">' +
      (r.bias6 > 0 ? '+' : '') + fmt(r.bias6, 2) + '%</div>' +
      '<div class="px2">' + (r.nowin ? '—' : fmt(r.buy)) + gapBuyHtml(r) + '</div>' +
      '<div class="px2 hide-m">' + fmt(r.sell) + gapSellHtml(r) + '</div>' +
      '<div class="sg">' + tagOf(r) + '</div>' +
      '<div class="pcell"><input class="pin" inputmode="decimal" autocomplete="off" ' +
      'data-code="' + r.code + '" placeholder="成本" value="' +
      (POS[r.code] ? POS[r.code] : '') + '"></div>' +
      '<div class="posline">' + posInner(r) + '</div>' +
      '<div class="rule">买 ' + r.buy_rule + ' ｜ 卖 ' + r.sell_rule +
      (r.bt ? ' ｜ ' + btText(r.bt) : '') + ma250Text(r) + '</div>' +
      '</div>';
  });

  html += '</div>';
  document.getElementById('list').innerHTML = html;
}

// 点「更新」：拉实时报价 → 浏览器本地重算 → 立刻重绘（约 1 秒）
// 不再依赖 GitHub Actions：那条链路要 2 分钟，且 cron 还会排队延迟
async function refreshLive(opts) {
  if (REFRESHING) return;
  const silent = !!(opts && opts.silent);
  REFRESHING = true;
  render();

  // 配了 Worker 就顺带触发一次 Actions 归档，不阻塞、失败也不影响
  if (WORKER_URL) fetch(WORKER_URL, { method: 'POST' }).catch(function () {});

  try {
    const base = DATA.slots.live || DATA.slots[DATA.updated_slot];
    const rows0 = (base && base.rows) || [];
    if (!rows0.length) throw new Error('没有基准数据');
    const usable = rows0.filter(function (r) { return r.hist && r.hist.length >= 20; });
    if (!usable.length) throw new Error('数据没有历史序列，等下一次定时任务');

    const syms = usable.map(function (r) {
      return r.sym || ((/^[65]/.test(r.code)) ? 'sh' : 'sz') + r.code;
    });
    const q = await fetchQuotes(syms.concat(['sh000300']));
    if (!Object.keys(q).length) throw new Error('报价接口返回空');

    // 大盘多空（沪深300 实时价 vs 重算后的 MA60）
    let bull = DATA.market ? !!DATA.market.bull : true;
    const mk = base.mkt, qi = q['sh000300'];
    if (mk && mk.closes && mk.closes.length >= 60 && qi) {
      const mc = mk.closes.slice();
      if (mk.date === TODAY) mc[mc.length - 1] = qi.price; else mc.push(qi.price);
      const m60 = _avg(mc.slice(-60));
      bull = qi.price > m60;
      DATA.market = { idx: Number(qi.price.toFixed(1)), ma60: Number(m60.toFixed(1)), bull: bull };
    }

    const out = [];
    let ok = 0, stale = 0;
    rows0.forEach(function (r) {
      const quote = q[r.sym];
      if (!quote) { out.push(r); return; }
      const nr = recalcRow(r, quote.price, bull);
      if (nr) {
        out.push(nr); ok++;
        if (quote.t) LAST_TS = quote.t;
      } else { out.push(r); stale++; }
    });
    if (!ok) throw new Error('全部未能重算');

    const counts = { buy: 0, sell: 0, wait: 0 };
    out.forEach(function (r) { counts[r.status] = (counts[r.status] || 0) + 1; });

    const ts = LAST_TS.length >= 12
      ? LAST_TS.slice(8, 10) + ':' + LAST_TS.slice(10, 12) : '';
    DATA.slots.live = {
      ts: ts, label: '实时', rows: out, counts: counts, mkt: mk,
      live: true, late: false, note: '', final: false,
    };
    LIVE_AT = LAST_TS;
    LIVE_STALE = stale;
    CUR = 'live';
    if (!silent) {
      toast(stale ? (ok + ' 只已重算，' + stale + ' 只基准过旧未重算')
                  : ('已按 ' + (ts || '实时') + ' 报价重算 ' + ok + ' 只'));
    }
  } catch (e) {
    REFRESHING = false;
    await pullLatest();
    render();
    if (!silent) toast('取实时价失败：' + ((e && e.message) ? e.message : '网络异常'));
    return;
  }
  REFRESHING = false;
  render();
}

async function pullLatest() {
  try {
    const r = await fetch('./data.json?t=' + Date.now(), { cache: 'no-store' });
    if (r.ok) { DATA = await r.json(); LIVE_AT = ''; }   // 退回快照，清掉实时标记
  } catch (e) { /* 保留旧数据 */ }
}

// 当前北京时间的「语义」坐标：拿 getHours/getDay 读出来就是北京时间
function cnNow() {
  return new Date(Date.now() + (d_off() + 480) * 60000);
}
function d_off() { return new Date().getTimezoneOffset(); }

// 是否在连续竞价时段（9:30-11:30 / 13:00-15:00，周一至周五）
// 集合竞价 9:15-9:25 和尾盘 14:57-15:00 不单独处理：前者没有连续成交价，
// 后者只剩 3 分钟，刷新一次纯属浪费请求。
function isTradingTime() {
  const d = cnNow();
  const wd = d.getDay(), hm = d.getHours() * 60 + d.getMinutes();
  return wd >= 1 && wd <= 5 && ((hm >= 570 && hm <= 690) || (hm >= 780 && hm <= 900));
}

// 距下一段连续竞价开盘还有多少分钟（非交易时段用来显示提示）
function minsToOpen() {
  const d = cnNow();
  const wd = d.getDay(), hm = d.getHours() * 60 + d.getMinutes();
  if (wd >= 1 && wd <= 5) {
    if (hm < 570) return 570 - hm;              // 今日 9:30
    if (hm >= 690 && hm < 780) return 780 - hm; // 今日 13:00
  }
  let add = 1;                                   // 跨到下一个工作日 9:30
  while (true) {
    const nd = (wd + add) % 7;
    if (nd >= 1 && nd <= 5) break;
    add++;
  }
  return add * 1440 + 570 - hm;
}

// 默认档位：交易时段优先显示「实时」，收盘后显示最近一个时点档
const _ks = Object.keys(DATA.slots || {});
CUR = (isTradingTime() && _ks.includes('live')) ? 'live'
    : (_ks.includes('1505') ? '1505' : (_ks[0] || 'live'));
render();

// 交易时段打开页面：立刻按实时价重算一次，省得每次都要手点。
// 注意不能判断 _ks.includes('live')——data.json 只存 1505 档，live 是前端派生出来的，
// 用它当条件会让「打开即刷新」永远不成立。
if (isTradingTime()) {
  setTimeout(function () {
    refreshLive({ silent: true });
    AUTO_NEXT = Date.now() + AUTO_MS;   // 首次刷新后重新起算 10 分钟
  }, 700);
}

/* ================== 自动刷新（仅 A 股交易时段，10 分钟一次） ==================
 * 只在浏览器里算：拉实时报价 → 本地重算 → 重绘。
 * 不写 data.json、不产生 git 提交、不推送——这三件事都留给 15:05 的收盘档。
 *
 * 时间口径：只在 9:30-11:30 / 13:00-15:00（工作日）刷新，其余时间一个请求都不发：
 *   · 收盘后、午休、周末、节假日打开页面 → 只显示 data.json 里最近的收盘档，不轮询
 *   · 非交易时段定时器降到 30 秒一次，纯粹是等开盘那一刻，不碰网络
 *   · 从非交易时段跨进交易时段（开盘 / 午休结束）→ 立刻补刷一次，不等 10 分钟
 *
 * 为什么不交给 GitHub Actions 每 10 分钟跑一次：cron 最小间隔 5 分钟且常延迟
 * 5～40 分钟，排 10 分钟会得到一堆陈旧数据，还每次 commit 一遍。
 */
var AUTO_MS = 10 * 60 * 1000;
var AUTO_IDLE_MS = 30 * 1000;
var AUTO_KEY = 'sig_auto_v1';
var AUTO_ON = true;
try { AUTO_ON = localStorage.getItem(AUTO_KEY) !== '0'; } catch (e) {}
var AUTO_NEXT = isTradingTime() ? Date.now() + AUTO_MS : 0;
var AUTO_TMR = null;
var AUTO_TXT = '';

function pad2(n) { return n < 10 ? '0' + n : '' + n; }

// 非交易时段的提示：尽量说清「什么时候会自己动」
function idleText() {
  const m = minsToOpen();
  if (m <= 90) return '↻ 仅交易时段 · ' + m + ' 分钟后开盘';
  const d = cnNow();
  const total = d.getHours() * 60 + d.getMinutes() + m;
  const shift = Math.floor(total / 1440);
  const hh = Math.floor((total % 1440) / 60), mi = total % 60;
  const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
  const day = shift === 0 ? '今日'
            : shift === 1 ? '明日'
            : WEEK[(d.getDay() + shift) % 7];
  return '↻ 仅交易时段 · ' + day + ' ' + hh + ':' + pad2(mi) + ' 开盘';
}

function autoText() {
  if (!AUTO_ON) return '↻ 自动刷新已关';
  if (!isTradingTime()) return idleText();
  var left = Math.max(0, Math.round((AUTO_NEXT - Date.now()) / 1000));
  return '↻ 10 分钟自动 · 下次 ' + Math.floor(left / 60) + ':' + pad2(left % 60);
}

function paintAuto(force) {
  var el = document.getElementById('autoBtn');
  if (!el) return;
  var t = autoText();
  el.className = 'auto' + (AUTO_ON ? '' : ' off') + (isTradingTime() ? '' : ' idle');
  el.title = AUTO_ON ? 'A 股交易时段内每 10 分钟自动刷新一次，点击可关闭' : '已关闭，点击重新开启';
  if (!force && t === AUTO_TXT) return;      // 文案没变就不碰 DOM，避免每秒触发重排
  AUTO_TXT = t;
  el.textContent = t;
}

var WAS_TRADING = isTradingTime();

// 交易时段 1 秒一跳（只为走倒计时），非交易时段 30 秒一跳（只为等开盘）
function scheduleTick() {
  if (AUTO_TMR) { clearTimeout(AUTO_TMR); AUTO_TMR = null; }
  if (!AUTO_ON) return;                      // 关掉就彻底不排队，点开时再起
  AUTO_TMR = setTimeout(autoTick, isTradingTime() ? 1000 : AUTO_IDLE_MS);
}

function autoTick() {
  var trading = isTradingTime();
  // 午休结束 / 隔夜开盘：页面一直开着也要立刻刷一次，不能傻等下一个 10 分钟
  if (trading && !WAS_TRADING) {
    AUTO_NEXT = Date.now() + AUTO_MS;        // 先排下一次，避免刷新耗时导致连环触发
    refreshLive({ silent: true });
  }
  WAS_TRADING = trading;
  if (!trading) {
    AUTO_NEXT = 0;                           // 收盘即清空倒计时，下一个交易日重排
  } else {
    if (!AUTO_NEXT) AUTO_NEXT = Date.now() + AUTO_MS;
    if (Date.now() >= AUTO_NEXT) {
      AUTO_NEXT = Date.now() + AUTO_MS;
      refreshLive({ silent: true });
    }
  }
  paintAuto();
  scheduleTick();
}

// 后台标签的定时器会被浏览器节流（Chrome 可低到 1 分钟一次甚至更久），
// 切回前台时如果已经过期，立刻补一次，别让用户盯着一张过期的表
document.addEventListener('visibilitychange', function () {
  if (document.visibilityState !== 'visible') return;
  if (!AUTO_ON) { paintAuto(true); return; }
  var trading = isTradingTime();
  if (trading) {
    if (!AUTO_NEXT || Date.now() >= AUTO_NEXT) refreshLive({ silent: true });
    AUTO_NEXT = Date.now() + AUTO_MS;
  } else {
    AUTO_NEXT = 0;
  }
  WAS_TRADING = trading;
  paintAuto(true);
  scheduleTick();
});

// 点标签切换开/关，选择记在 localStorage
document.addEventListener('click', function (e) {
  var el = e.target;
  if (!el || el.id !== 'autoBtn') return;
  AUTO_ON = !AUTO_ON;
  try { localStorage.setItem(AUTO_KEY, AUTO_ON ? '1' : '0'); } catch (er) {}
  AUTO_NEXT = (AUTO_ON && isTradingTime()) ? Date.now() + AUTO_MS : 0;
  paintAuto(true);
  if (AUTO_ON && isTradingTime()) refreshLive({ silent: true });
  scheduleTick();
});

paintAuto(true);
scheduleTick();
