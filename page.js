const SNAP = __SNAPSHOT__;

function fmt(n, d){
  if(n === null || n === undefined || isNaN(n)) return '—';
  if(d === undefined) d = (Math.abs(Number(n)) < 10 && !Number.isInteger(Number(n))) ? 3 : 2;
  return Number(n).toFixed(d);
}

let CUR = null;

function render(){
  const D = SNAP;
  if(!D || !D.slots) return;

  document.getElementById('date').textContent = D.date || '';
  const m = D.market || {};
  document.getElementById('idx').innerHTML =
    '沪深300 ' + fmt(m.idx, 1) + ' · MA60 ' + fmt(m.ma60, 1) +
    ' <span class="badge ' + (m.bull ? 'bull' : 'bear') + '">' +
    (m.bull ? '多头' : '空头') + '</span>';
  document.getElementById('upd').textContent = '更新 ' + (D.updated || '');

  const keys = Object.keys(D.slots);
  const tabs = document.getElementById('tabs');
  tabs.innerHTML = keys.map(k => {
    const s = D.slots[k];
    return '<div class="tab' + (k === CUR ? ' on' : '') + '" data-k="' + k + '">' +
      s.label + ' ' + s.ts + '</div>';
  }).join('');
  tabs.querySelectorAll('.tab').forEach(el => {
    el.onclick = () => { CUR = el.dataset.k; render(); };
  });

  const slot = D.slots[CUR];
  if(!slot) return;
  const c = slot.counts || {};

  document.getElementById('stats').innerHTML =
    '<span>买入 <b style="color:#c0392b">' + (c.buy || 0) + '</b></span>' +
    '<span>卖出 <b style="color:#2e7d32">' + (c.sell || 0) + '</b></span>' +
    '<span>等待 <b style="color:#888">' + (c.wait || 0) + '</b></span>' +
    '<span style="color:#aaa">共 ' + slot.rows.length + ' 只</span>';

  const rows = slot.rows.slice().sort((a, b) => {
    const order = { buy: 0, sell: 1, wait: 2 };
    return (order[a.status] ?? 3) - (order[b.status] ?? 3);
  });

  const tagOf = st =>
    st === 'buy'  ? '<span class="tag t-buy">买入</span>' :
    st === 'sell' ? '<span class="tag t-sell">卖出</span>' :
                    '<span class="tag t-wait">等待</span>';

  let html =
    '<div class="card"><div class="row hd">' +
    '<div>名称</div><div>现价</div><div>RSI14</div><div>BIAS6</div>' +
    '<div class="hide-m">买点</div><div class="hide-m">卖点</div><div>信号</div></div>';

  rows.forEach(r => {
    const d5 = (r.d5 === null || r.d5 === undefined) ? null : Number(r.d5);
    const d5cls = d5 === null ? '' : (d5 > 0 ? 'up' : (d5 < 0 ? 'down' : ''));
    html +=
      '<div class="row">' +
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

render();