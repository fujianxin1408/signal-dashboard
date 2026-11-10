const SNAP = __SNAPSHOT__;

let DATA = SNAP || { slots: {} };
let CUR = null;
let REFRESHING = false;

// 更新按钮的触发代理（Cloudflare Worker）。留空则退化为本地直接刷新 data.json
const WORKER_URL = '';

const SLOT_KEYS = ['live', '1451', '1505'];
const SLOT_NAME = { live: '实时', '1451': '14:51', '1505': '15:05' };

// 触发股票排前面；同为触发时，超出幅度越大越靠前（信号越强）
function sortRows(rows) {
  const rank = { buy: 0, sell: 1, wait: 2 };
  return rows.slice().sort((a, b) => {
    const ra = rank[a.status] ?? 3, rb = rank[b.status] ?? 3;
    if (ra !== rb) return ra - rb;
    if (a.status === 'wait') {
      return Math.abs(a.dbuy ?? 99) - Math.abs(b.dbuy ?? 99);
    }
    const ea = a.status === 'buy' ? -(a.dbuy ?? 0) : (a.dsell ?? 0);
    const eb = b.status === 'buy' ? -(b.dbuy ?? 0) : (b.dsell ?? 0);
    return eb - ea;
  });
}

function fmt(n, d) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  if (d === undefined) d = (Math.abs(Number(n)) < 10 && !Number.isInteger(Number(n))) ? 3 : 2;
  return Number(n).toFixed(d);
}

function render() {
  const D = DATA;
  if (!D || !D.slots) return;

  document.getElementById('date').textContent = D.date || '';
  const m = D.market || {};
  document.getElementById('idx').innerHTML =
    '沪深300 ' + fmt(m.idx, 1) + ' · MA60 ' + fmt(m.ma60, 1) +
    ' <span class="badge ' + (m.bull ? 'bull' : 'bear') + '">' +
    (m.bull ? '多头' : '空头') + '</span>';
  document.getElementById('upd').textContent = D.updated ? '更新 ' + D.updated : '';

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
    return '<div class="' + cls + '" data-k="' + k + '">' + SLOT_NAME[k] + '</div>';
  }).join('');

  tabs.querySelectorAll('.tab[data-k]').forEach(el => {
    el.onclick = () => { CUR = el.dataset.k; render(); };
  });
  const btn = document.getElementById('btnLive');
  if (btn) btn.onclick = refreshLive;

  const slot = D.slots[CUR];
  if (!slot) {
    document.getElementById('stats').innerHTML = '';
    document.getElementById('list').innerHTML =
      '<div class="sep">该时点还没有数据</div>';
    return;
  }

  const c = slot.counts || {};
  const hit = (c.buy || 0) + (c.sell || 0);
  document.getElementById('stats').innerHTML =
    '<span>买入 <b style="color:#c0392b">' + (c.buy || 0) + '</b></span>' +
    '<span>卖出 <b style="color:#2e7d32">' + (c.sell || 0) + '</b></span>' +
    '<span>等待 <b style="color:#888">' + (c.wait || 0) + '</b></span>' +
    '<span style="color:#aaa">' + slot.ts + ' · 共 ' + slot.rows.length + ' 只' +
    (hit ? ' · 触发 ' + hit : '') + '</span>';

  const rows = sortRows(slot.rows);

  const tagOf = st =>
    st === 'buy' ? '<span class="tag t-buy">买入</span>' :
    st === 'sell' ? '<span class="tag t-sell">卖出</span>' :
    '<span class="tag t-wait">等待</span>';

  let html =
    '<div class="card"><div class="row hd">' +
    '<div>名称</div><div>现价</div><div>RSI14</div><div>BIAS6</div>' +
    '<div class="hide-m">买点</div><div class="hide-m">卖点</div><div>信号</div></div>';

  let lastStatus = null;
  rows.forEach(r => {
    if (r.status !== lastStatus) {
      const t = { buy: '买入触发', sell: '卖出触发', wait: '其余观察' }[r.status];
      html += '<div class="sep">' + t + '</div>';
      lastStatus = r.status;
    }
    const d5 = (r.d5 === null || r.d5 === undefined) ? null : Number(r.d5);
    const d5cls = d5 === null ? '' : (d5 > 0 ? 'up' : (d5 < 0 ? 'down' : ''));
    html +=
      '<div class="row s-' + r.status + '">' +
      '<div class="name">' + r.name + '<span class="code">' + r.code + '</span></div>' +
      '<div class="px">' + fmt(r.price) + '</div>' +
      '<div class="num">' + fmt(r.rsi, 1) + '</div>' +
      '<div class="num ' + (r.bias6 > 0 ? 'up' : 'down') + '">' +
      (r.bias6 > 0 ? '+' : '') + fmt(r.bias6, 2) + '%</div>' +
      '<div class="num hide-m">' + fmt(r.buy) + '</div>' +
      '<div class="num hide-m">' + fmt(r.sell) + '</div>' +
      '<div>' + tagOf(r.status) + '</div>' +
      '<div class="rule">买 ' + r.buy_rule + ' ｜ 卖 ' + r.sell_rule + '</div>' +
      '</div>';
  });

  html += '</div>';
  document.getElementById('list').innerHTML = html;
}

// 点「更新」：触发 GitHub Actions 抓实时价，轮询等结果回写
async function refreshLive() {
  if (REFRESHING) return;
  REFRESHING = true;
  render();
  let fired = false;
  if (WORKER_URL) {
    try {
      const r = await fetch(WORKER_URL, { method: 'POST' });
      fired = r.ok;
    } catch (e) { fired = false; }
  }
  if (!fired) {
    // 没配代理或代理失败：直接重拉 data.json（至少能看到别处更新的结果）
    REFRESHING = false;
    await pullLatest();
    render();
    return;
  }

  const cur = (DATA && DATA.updated) || '';
  const t0 = Date.now();
  const timer = setInterval(async () => {
    if (Date.now() - t0 > 180000) {
      clearInterval(timer); REFRESHING = false; render(); return;
    }
    try {
      const r = await fetch('./data.json?t=' + Date.now(), { cache: 'no-store' });
      if (r.ok) {
        const j = await r.json();
        if (j.slots && j.slots.live && j.updated !== cur) {
          DATA = j; CUR = 'live';
          clearInterval(timer); REFRESHING = false; render(); return;
        }
      }
    } catch (e) { /* 继续等 */ }
  }, 5000);
}

async function pullLatest() {
  try {
    const r = await fetch('./data.json?t=' + Date.now(), { cache: 'no-store' });
    if (r.ok) DATA = await r.json();
  } catch (e) { /* 保留旧数据 */ }
}

// 默认选中最新有数据的档位
const _ks = Object.keys(DATA.slots || {});
CUR = _ks.includes('1505') ? '1505' : (_ks.includes('1451') ? '1451' : (_ks[0] || 'live'));
render();
