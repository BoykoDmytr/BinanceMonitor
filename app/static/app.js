// Фронтенд моніторингу — vanilla JS. Дані тягнемо з локального API,
// свічки малюємо через lightweight-charts. Київський час — лише тут, в UI.

const KYIV = 'Europe/Kyiv';
let CFG = { notional_usdt: 256, hold_seconds: 30, calm_minutes: 5,
            color_green_max_bps: 20, color_yellow_max_bps: 50 };
let selected = null;
let sortKey = 'total_bps';
let sortDir = 1; // 1 = зростання, -1 = спадання
let chart, candleSeries, volumeSeries;

// ─────────────────────────── утиліти форматування ───────────────────────────
const $ = (id) => document.getElementById(id);

function num(x, digits = 2) {
  if (x === null || x === undefined || Number.isNaN(x)) return '—';
  return Number(x).toLocaleString('uk-UA', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function price(x) {
  if (x === null || x === undefined) return '—';
  const d = Math.abs(x) >= 100 ? 2 : Math.abs(x) >= 1 ? 4 : 6;
  return num(x, d);
}
function kyivTime(ms, withDate = false) {
  if (!ms) return '—';
  const opts = withDate
    ? { timeZone: KYIV, day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }
    : { timeZone: KYIV, hour: '2-digit', minute: '2-digit' };
  return new Date(ms).toLocaleString('uk-UA', opts);
}
function durationText(ms) {
  if (!ms && ms !== 0) return '—';
  const min = Math.round(ms / 60000);
  if (min < 60) return `${min} хв`;
  const h = Math.floor(min / 60), m = min % 60;
  return `${h} год ${m} хв`;
}

// ─────────────────────────── ліміти/банер ───────────────────────────
async function pollLimits() {
  try {
    const l = await fetch('/api/limits').then(r => r.json());
    const pct = l.weight_limit ? Math.round((l.used_weight / l.weight_limit) * 100) : 0;
    let cls = '';
    if (pct >= 80) cls = 'bad'; else if (pct >= 60) cls = 'warn';
    $('limits').innerHTML =
      `вага <span class="${cls}">${l.used_weight}/${l.weight_limit} (${pct}%)</span> · ${l.status || ''}`;

    const banner = $('banner');
    if (l.banned) {
      banner.textContent = '418 — IP заблоковано Binance. Усі запити зупинено. ' +
        'Зачекайте, поки завершиться період блокування, і перезапустіть застосунок.';
      banner.classList.remove('hidden');
    } else if (l.last_error) {
      banner.textContent = 'Мережа: ' + l.last_error;
      banner.classList.remove('hidden');
    } else {
      banner.classList.add('hidden');
    }
  } catch (e) { /* мовчки — покажемо наступного разу */ }
}

// ─────────────────────────── пошук ───────────────────────────
let searchTimer = null;
function initSearch() {
  const input = $('searchInput');
  input.addEventListener('input', () => {
    clearTimeout(searchTimer);
    const q = input.value.trim();
    if (!q) { hideSearch(); return; }
    searchTimer = setTimeout(() => doSearch(q), 250);
  });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.search')) hideSearch();
  });
}
function hideSearch() { $('searchResults').classList.add('hidden'); }

async function doSearch(q) {
  let items;
  try { items = await fetch('/api/search?q=' + encodeURIComponent(q)).then(r => r.json()); }
  catch (e) { return; }
  const box = $('searchResults');
  if (!items.length) {
    box.innerHTML = `<div class="sr-item"><span class="muted">Нічого не знайдено.
      Пошук іде по тикеру й baseAsset. Назви компаній можна додати руками в aliases.json.</span></div>`;
    box.classList.remove('hidden');
    return;
  }
  box.innerHTML = items.map(it => `
    <div class="sr-item">
      <span><span class="sr-sym">${it.symbol}</span>
        <span class="sr-meta">${it.status}</span></span>
      <span class="sr-meta">${it.quote_volume_24h != null ? '24h: ' + num(it.quote_volume_24h, 0) + ' ' + it.quote : ''}</span>
      ${it.in_watchlist
        ? '<span class="sr-meta">у списку</span>'
        : `<button class="btn" data-add="${it.symbol}">Додати</button>`}
    </div>`).join('');
  box.querySelectorAll('[data-add]').forEach(b =>
    b.addEventListener('click', () => addSymbol(b.dataset.add)));
  box.classList.remove('hidden');
}

async function addSymbol(symbol) {
  const res = await fetch('/api/watchlist', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbol }),
  }).then(r => r.json());
  if (!res.ok) { alert(res.error || 'Не вдалося додати символ'); return; }
  $('searchInput').value = '';
  hideSearch();
  loadWatchlist();
}

async function removeSymbol(symbol, ev) {
  ev.stopPropagation();
  await fetch('/api/watchlist/' + symbol, { method: 'DELETE' });
  if (selected === symbol) { selected = null; showDetailEmpty(); }
  loadWatchlist();
}

// ─────────────────────────── вотчліст ───────────────────────────
let lastRows = [];
async function loadWatchlist() {
  let rows;
  try { rows = await fetch('/api/watchlist').then(r => r.json()); }
  catch (e) { return; }
  lastRows = rows;
  renderWatchlist();
}

function stateFor(m) {
  // Три кольори за total bps + окремі стани для граничних випадків
  if (!m) return { cls: 'gray', label: 'рахується' };
  if (m.insufficient_depth) return { cls: 'gray', label: 'мало глибини' };
  if (m.dead_market) return { cls: 'gray', label: 'неліквідно' };
  const t = m.total_bps;
  if (t == null) return { cls: 'gray', label: 'рахується' };
  if (t < CFG.color_green_max_bps) return { cls: 'green', label: num(t, 1) };
  if (t < CFG.color_yellow_max_bps) return { cls: 'yellow', label: num(t, 1) };
  return { cls: 'red', label: num(t, 1) };
}

function sortValue(row, key) {
  if (key === 'symbol') return row.symbol;
  if (key === 'price') return row.price ?? -Infinity;
  if (key === 'state') return row.metrics?.total_bps ?? Infinity;
  return row.metrics?.[key] ?? -Infinity;
}

function renderWatchlist() {
  const body = $('watchBody');
  $('watchEmpty').classList.toggle('hidden', lastRows.length > 0);

  const rows = [...lastRows].sort((a, b) => {
    const va = sortValue(a, sortKey), vb = sortValue(b, sortKey);
    if (typeof va === 'string') return va.localeCompare(vb) * sortDir;
    return (va - vb) * sortDir;
  });

  body.innerHTML = rows.map(r => {
    const m = r.metrics;
    const st = stateFor(m);
    const bf = r.backfill;
    const calmDot = m?.calm ? '<span class="calm-dot" title="спокійно">●</span>' : '';
    let symCell = `<span class="sym">${r.symbol}</span>${calmDot}`;
    if (bf && bf.running) {
      const p = bf.total ? Math.round((bf.done / bf.total) * 100) : 0;
      symCell += ` <span class="muted">backfill ${p}%</span>`;
    }
    return `<tr data-sym="${r.symbol}" class="${selected === r.symbol ? 'selected' : ''}">
      <td>${symCell}</td>
      <td class="num">${price(r.price ?? m?.price)}</td>
      <td class="num">${num(m?.sigma_bps)}</td>
      <td class="num">${m?.sigma_pct == null ? '—' : num(m.sigma_pct, 0) + '%'}</td>
      <td class="num">${num(m?.vol_ratio)}</td>
      <td class="num">${m?.insufficient_depth ? '∞' : num(m?.roundtrip_bps)}</td>
      <td class="num">${num(m?.total_bps)}</td>
      <td class="num">${num(m?.total_usd, 3)}</td>
      <td><span class="badge ${st.cls}">${st.label}</span></td>
      <td><button class="btn del" data-del="${r.symbol}">✕</button></td>
    </tr>`;
  }).join('');

  body.querySelectorAll('tr').forEach(tr => {
    tr.addEventListener('click', () => selectSymbol(tr.dataset.sym));
  });
  body.querySelectorAll('[data-del]').forEach(b => {
    b.addEventListener('click', (e) => removeSymbol(b.dataset.del, e));
  });
}

function initSort() {
  document.querySelectorAll('#watchTable th[data-key]').forEach(th => {
    th.addEventListener('click', () => {
      const key = th.dataset.key;
      if (sortKey === key) sortDir *= -1; else { sortKey = key; sortDir = key === 'symbol' ? 1 : -1; }
      document.querySelectorAll('#watchTable th').forEach(h => h.classList.remove('sorted', 'asc'));
      th.classList.add('sorted');
      if (sortDir === 1) th.classList.add('asc');
      renderWatchlist();
    });
  });
}

// ─────────────────────────── деталь символу ───────────────────────────
function showDetailEmpty() {
  $('detailEmpty').classList.remove('hidden');
  $('detailBody').classList.add('hidden');
  $('detailTitle').textContent = 'Деталь символу';
  $('detailLive').textContent = '';
}

function ensureChart() {
  if (chart) return;
  chart = LightweightCharts.createChart($('chart'), {
    layout: { background: { color: '#161b22' }, textColor: '#8a97a8' },
    grid: { vertLines: { color: '#1c2430' }, horzLines: { color: '#1c2430' } },
    rightPriceScale: { borderColor: '#263041' },
    timeScale: {
      borderColor: '#263041', timeVisible: true, secondsVisible: false,
      // Осі — київський час
      tickMarkFormatter: (t) => new Date(t * 1000)
        .toLocaleString('uk-UA', { timeZone: KYIV, hour: '2-digit', minute: '2-digit' }),
    },
    localization: {
      timeFormatter: (t) => new Date(t * 1000)
        .toLocaleString('uk-UA', { timeZone: KYIV, day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }),
    },
  });
  candleSeries = chart.addCandlestickSeries({
    upColor: '#2ea043', downColor: '#e5534b', borderVisible: false,
    wickUpColor: '#2ea043', wickDownColor: '#e5534b',
  });
  volumeSeries = chart.addHistogramSeries({
    priceFormat: { type: 'volume' }, priceScaleId: '', color: '#3a4453',
  });
  volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  new ResizeObserver(() => chart.applyOptions({ width: $('chart').clientWidth }))
    .observe($('chart'));
}

async function selectSymbol(symbol) {
  selected = symbol;
  renderWatchlist();
  $('detailEmpty').classList.add('hidden');
  $('detailBody').classList.remove('hidden');
  $('detailTitle').textContent = symbol;
  ensureChart();
  const d = await fetch('/api/symbol/' + symbol).then(r => r.json());
  const data = d.klines.map(k => ({ time: k.time, open: k.open, high: k.high, low: k.low, close: k.close }));
  candleSeries.setData(data);
  volumeSeries.setData(d.klines.map(k => ({
    time: k.time, value: k.volume,
    color: k.close >= k.open ? 'rgba(46,160,67,.4)' : 'rgba(229,83,75,.4)',
  })));
  if (d.forming) applyForming(d.forming);
  renderHeatmap(d.heatmap);
  updateLive(symbol);
  if ($('journalOnlySel').checked) loadJournal();
}

function applyForming(f) {
  candleSeries.update({ time: Math.floor(f.open_time / 1000), open: f.open, high: f.high, low: f.low, close: f.close });
  volumeSeries.update({ time: Math.floor(f.open_time / 1000), value: f.quote_volume,
    color: f.close >= f.open ? 'rgba(46,160,67,.4)' : 'rgba(229,83,75,.4)' });
}

// Періодичне оновлення деталі (без setData — зберігаємо зум)
async function refreshDetail() {
  if (!selected) return;
  try {
    const d = await fetch('/api/symbol/' + selected).then(r => r.json());
    if (d.klines.length) {
      const last = d.klines[d.klines.length - 1];
      candleSeries.update({ time: last.time, open: last.open, high: last.high, low: last.low, close: last.close });
      volumeSeries.update({ time: last.time, value: last.volume,
        color: last.close >= last.open ? 'rgba(46,160,67,.4)' : 'rgba(229,83,75,.4)' });
    }
    if (d.forming) applyForming(d.forming);
    renderHeatmap(d.heatmap);
  } catch (e) {}
  updateLive(selected);
}

async function updateLive(symbol) {
  try {
    const rt = await fetch('/api/symbol/' + symbol + '/roundtrip').then(r => r.json());
    const row = lastRows.find(r => r.symbol === symbol);
    const m = row?.metrics;
    let parts = [];
    if (m) {
      const sp = m.sigma_pct == null ? '—' : num(m.sigma_pct, 0) + '%ᵖ';
      parts.push(`σ <b>${num(m.sigma_bps)}</b> bps (${sp})`);
      parts.push(`ATR <b>${num(m.atr_bps)}</b>`);
      if (m.seasonality_ratio != null) parts.push(`сезон ×<b>${num(m.seasonality_ratio)}</b>`);
    }
    if (rt.insufficient_depth) {
      parts.push(`<b>insufficient_depth</b> (${rt.side})`);
    } else if (rt.total_bps != null) {
      parts.push(`RT <b>${num(rt.roundtrip_bps)}</b> + дрейф <b>${num(rt.drift_bps)}</b>`);
      parts.push(`Total <b>${num(rt.total_bps)}</b> bps = <b>${num(rt.total_usd, 3)}</b> $ на ${num(rt.notional, 0)} USDT`);
    }
    $('detailLive').innerHTML = parts.join(' · ');
  } catch (e) {}
}

// ─────────────────────────── теплокарта ───────────────────────────
const DOW = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Нд'];
function heatColor(v, min, max) {
  if (v == null) return null;
  // менший total = темніший зелений; більший = червоний
  const t = max > min ? (v - min) / (max - min) : 0;
  const hue = 140 - 140 * t;          // 140 (зелений) → 0 (червоний)
  const light = 26 + 24 * (1 - t);    // темніше для дешевших
  return `hsl(${hue}, 55%, ${light}%)`;
}
function renderHeatmap(grid) {
  const flat = grid.flat().filter(v => v != null);
  $('heatmapEmpty').classList.toggle('hidden', flat.length > 0);
  const el = $('heatmap');
  if (!flat.length) { el.innerHTML = ''; return; }
  const min = Math.min(...flat), max = Math.max(...flat);

  let html = '<div class="hm-corner"></div>';
  for (let h = 0; h < 24; h++) html += `<div class="hm-col">${h % 3 === 0 ? h : ''}</div>`;
  for (let d = 0; d < 7; d++) {
    html += `<div class="hm-row">${DOW[d]}</div>`;
    for (let h = 0; h < 24; h++) {
      const v = grid[d][h];
      if (v == null) { html += `<div class="hm-cell empty" title="${DOW[d]} ${h}:00 — немає даних"></div>`; }
      else {
        html += `<div class="hm-cell" style="background:${heatColor(v, min, max)}"
          title="${DOW[d]} ${h}:00 — медіана total ${num(v)} bps"></div>`;
      }
    }
  }
  el.innerHTML = html;
}

// ─────────────────────────── журнал ───────────────────────────
async function loadJournal() {
  const onlySel = $('journalOnlySel').checked;
  const url = onlySel && selected ? '/api/journal?symbol=' + selected : '/api/journal';
  $('csvLink').href = onlySel && selected ? '/api/journal.csv?symbol=' + selected : '/api/journal.csv';
  let items;
  try { items = await fetch(url).then(r => r.json()); } catch (e) { return; }
  $('journalEmpty').classList.toggle('hidden', items.length > 0);
  $('journalBody').innerHTML = items.map(e => `
    <tr>
      <td><span class="sym">${e.symbol}</span></td>
      <td class="${e.kind === 'calm_enter' ? 'kind-enter' : 'kind-exit'}">
        ${e.kind === 'calm_enter' ? 'увійшов у спокій' : 'вийшов зі спокою'}</td>
      <td>${kyivTime(e.ts, true)}</td>
      <td class="num">${e.kind === 'calm_exit' ? durationText(e.duration_ms) : '—'}</td>
    </tr>`).join('');
}

// ─────────────────────────── старт ───────────────────────────
async function init() {
  try { CFG = await fetch('/api/config').then(r => r.json()); } catch (e) {}
  document.querySelector('#watchTable th[data-key="total_bps"]').classList.add('sorted');
  initSearch();
  initSort();
  $('journalOnlySel').addEventListener('change', loadJournal);

  await loadWatchlist();
  pollLimits();
  loadJournal();

  setInterval(loadWatchlist, 2000);
  setInterval(pollLimits, 3000);
  setInterval(refreshDetail, 2500);
  setInterval(loadJournal, 5000);
}
init();
