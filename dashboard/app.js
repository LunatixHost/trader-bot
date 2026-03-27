// ─── Trading Terminal ─────────────────────────────────────────

const API_BASE = window.location.origin;
const WS_URL   = `ws://${window.location.host}/ws`;

// ─── State ────────────────────────────────────────────────────

let ws              = null;
let state           = null;
let selectedPair    = null;
let chart           = null;
let candleSeries    = null;
let volumeSeries    = null;
let chartRO         = null;
let currentInterval = '15m';
let tradesData      = [];
let configData      = null;
let reconnectTimer  = null;

// ─── Window Manager ───────────────────────────────────────────

const DEFAULT_LAYOUT = {
    'win-watchlist': { x: 0,   y: 0,   w: 200, h: null }, // h: full
    'win-chart':     { x: 204, y: 0,   w: null, h: null }, // w/h: fill remaining
    'win-signals':   { x: 204, y: null, w: null, h: 130 }, // y: bottom of chart
    'win-sidebar':   { x: null, y: 0,  w: 260, h: null },  // x: right edge
};

let zCounter = 10;

function applyDefaultLayout() {
    const canvas  = document.getElementById('canvas');
    const cw      = canvas.clientWidth;
    const ch      = canvas.clientHeight;
    const sidebar = 260;
    const wlWidth = 200;
    const sigH    = 130;
    const gap     = 4;

    const layout = {
        'win-watchlist': { x: 0,                    y: 0,          w: wlWidth,                    h: ch },
        'win-chart':     { x: wlWidth + gap,         y: 0,          w: cw - wlWidth - sidebar - gap*2, h: ch - sigH - gap },
        'win-signals':   { x: wlWidth + gap,         y: ch - sigH,  w: cw - wlWidth - sidebar - gap*2, h: sigH },
        'win-sidebar':   { x: cw - sidebar,          y: 0,          w: sidebar,                    h: ch },
    };

    for (const [id, pos] of Object.entries(layout)) {
        setWinGeom(id, pos.x, pos.y, pos.w, pos.h);
    }
}

function setWinGeom(id, x, y, w, h) {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.left   = x + 'px';
    el.style.top    = y + 'px';
    el.style.width  = w + 'px';
    el.style.height = h + 'px';
}

function saveLayout() {
    const layout = {};
    document.querySelectorAll('.window').forEach(win => {
        layout[win.id] = {
            x: win.offsetLeft,
            y: win.offsetTop,
            w: win.offsetWidth,
            h: win.offsetHeight,
            min: win.classList.contains('minimized'),
        };
    });
    try { localStorage.setItem('tt-layout', JSON.stringify(layout)); } catch (_) {}
}

function loadLayout() {
    try {
        const raw = localStorage.getItem('tt-layout');
        if (!raw) return false;
        const layout = JSON.parse(raw);
        for (const [id, pos] of Object.entries(layout)) {
            const el = document.getElementById(id);
            if (!el) continue;
            setWinGeom(id, pos.x, pos.y, pos.w, pos.h);
            if (pos.min) el.classList.add('minimized');
        }
        return true;
    } catch (_) { return false; }
}

function bringToFront(el) {
    el.style.zIndex = ++zCounter;
    document.querySelectorAll('.window').forEach(w => w.classList.remove('focused'));
    el.classList.add('focused');
}

// ─── Drag ──────────────────────────────────────────────────────

function initDrag() {
    document.querySelectorAll('[data-drag]').forEach(handle => {
        const winId = handle.dataset.drag;
        const win   = document.getElementById(winId);
        if (!win) return;

        handle.addEventListener('mousedown', e => {
            if (e.button !== 0) return;
            if (e.target.closest('button, input, select')) return;

            bringToFront(win);

            const startX = e.clientX;
            const startY = e.clientY;
            const startL = win.offsetLeft;
            const startT = win.offsetTop;

            handle.style.cursor = 'grabbing';

            const onMove = e => {
                win.style.left = (startL + e.clientX - startX) + 'px';
                win.style.top  = (startT + e.clientY - startY) + 'px';
            };
            const onUp = () => {
                handle.style.cursor = '';
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup',  onUp);
                saveLayout();
            };
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup',  onUp);
            e.preventDefault();
        });
    });
}

// ─── Resize ────────────────────────────────────────────────────

function initResize() {
    document.querySelectorAll('[data-resize]').forEach(handle => {
        const winId = handle.dataset.resize;
        const win   = document.getElementById(winId);
        if (!win) return;

        handle.addEventListener('mousedown', e => {
            if (e.button !== 0) return;
            bringToFront(win);

            const startX = e.clientX;
            const startY = e.clientY;
            const startW = win.offsetWidth;
            const startH = win.offsetHeight;

            const onMove = e => {
                const nw = Math.max(160, startW + e.clientX - startX);
                const nh = Math.max(60,  startH + e.clientY - startY);
                win.style.width  = nw + 'px';
                win.style.height = nh + 'px';
                // Trigger chart resize
                if (winId === 'win-chart' && chart) {
                    const area = document.getElementById('chart-area');
                    if (area.clientWidth > 0 && area.clientHeight > 0) {
                        chart.applyOptions({ width: area.clientWidth, height: area.clientHeight });
                    }
                }
            };
            const onUp = () => {
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup',  onUp);
                saveLayout();
                if (winId === 'win-chart') triggerChartResize();
            };
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup',  onUp);
            e.preventDefault();
        });
    });
}

// ─── Minimize ─────────────────────────────────────────────────

function initMinimize() {
    document.querySelectorAll('[data-min]').forEach(btn => {
        btn.addEventListener('click', e => {
            e.stopPropagation();
            const win = document.getElementById(btn.dataset.min);
            if (!win) return;
            win.classList.toggle('minimized');
            if (!win.classList.contains('minimized') && btn.dataset.min === 'win-chart') {
                setTimeout(() => { destroyChart(); initChart(); loadChart(selectedPair, currentInterval); }, 50);
            }
            saveLayout();
        });
    });
}

// Focus on click
document.addEventListener('mousedown', e => {
    const win = e.target.closest('.window');
    if (win) bringToFront(win);
});

// ─── Clock ────────────────────────────────────────────────────

function updateClock() {
    const el = document.getElementById('clock');
    if (el) el.textContent = new Date().toLocaleTimeString();
}
setInterval(updateClock, 1000);
updateClock();

// ─── WebSocket ─────────────────────────────────────────────────

function connectWS() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
        setDot(true);
        if (reconnectTimer) { clearInterval(reconnectTimer); reconnectTimer = null; }
    };
    ws.onmessage = e => {
        try {
            const data = JSON.parse(e.data);
            if (data.pairs) { state = data; render(data); }
        } catch (err) { console.error('WS:', err); }
    };
    ws.onclose = () => {
        setDot(false);
        if (!reconnectTimer) reconnectTimer = setInterval(connectWS, 3000);
    };
    ws.onerror = () => ws.close();
}

function setDot(on) {
    const el = document.getElementById('ws-dot');
    if (el) el.className = on ? 'tb-dot on' : 'tb-dot off';
}

// ─── Render ────────────────────────────────────────────────────

function render(data) {
    renderMetrics(data);
    renderPairList(data);
    if (selectedPair && data.pairs[selectedPair]) {
        renderSignalPanel(data.pairs[selectedPair], data.signal_weights);
    }
    renderWeights(data.signal_weights);
    updateBotControls(data.bot);
}

// ─── Metrics ───────────────────────────────────────────────────

function renderMetrics(data) {
    const p = data.portfolio, s = data.stats, m = data.market, b = data.bot;

    setText('m-portfolio', `$${fmt(p.total_value)}`);
    setColor('m-portfolio-pl', `${p.pl_pct >= 0 ? '+' : ''}${p.pl_pct.toFixed(2)}%`, p.pl_pct >= 0);
    setColor('m-pl', `$${fmtSigned(p.pl_usdt)}`, p.pl_usdt >= 0);
    setText('m-pl-pct', `${p.pl_pct >= 0 ? '+' : ''}${p.pl_pct.toFixed(2)}%`);
    setColor('m-drawdown', `${p.drawdown_pct.toFixed(1)}%`, !p.in_drawdown, p.in_drawdown ? 'neg' : 'neu');
    setText('m-peak', `Peak: $${fmt(p.peak_value)}`);
    setText('m-winrate', `${s.win_rate}%`);
    setText('m-trades', `${s.total_trades} (${s.profitable}W/${s.sells - s.profitable}L)`);

    setColor('m-btc-change', `${m.btc_15m_change_pct >= 0 ? '+' : ''}${m.btc_15m_change_pct.toFixed(2)}%`, m.btc_15m_change_pct >= 0);
    const crash = document.getElementById('m-btc-crash');
    if (crash) { crash.textContent = m.btc_crash_active ? 'CRASH MODE' : 'Normal'; crash.className = m.btc_crash_active ? 'neg' : 'neu'; }

    const badge = document.getElementById('mode-badge');
    if (badge) { badge.textContent = b.mode; badge.className = `tb-badge mode-${b.mode.toLowerCase()}`; }
    setText('uptime', b.uptime || '--');
}

// ─── Pair List ─────────────────────────────────────────────────

function renderPairList(data) {
    const container = document.getElementById('pair-list');
    const pairs = data.pairs;
    const names = Object.keys(pairs);

    if (container.children.length !== names.length) {
        container.innerHTML = '';
        for (const name of names) {
            const item = document.createElement('div');
            item.className = 'w-item';
            item.id = `pair-${name}`;
            item.addEventListener('click', () => selectPair(name));
            container.appendChild(item);
        }
    }

    for (const name of names) {
        const p    = pairs[name];
        const item = document.getElementById(`pair-${name}`);
        if (!item) continue;

        item.className = `w-item${name === selectedPair ? ' active' : ''}`;

        const confPct  = Math.min(100, (p.confidence_weighted / (p.max_score || 1)) * 100);
        const barColor = confPct > 60 ? 'var(--green)' : confPct > 35 ? 'var(--yellow)' : 'var(--txt-4)';
        const rsiCls   = p.rsi < 35 ? 'pos' : p.rsi > 65 ? 'neg' : '';

        let posHtml = '';
        if (p.asset_balance > 0) {
            const cls = p.pl_pct >= 0 ? 'pos' : 'neg';
            posHtml = `<div class="w-pos">
                <span>${p.asset_balance.toFixed(4)} @ $${fmtPrice(p.buy_price)}</span>
                <span class="${cls}">${p.pl_pct >= 0 ? '+' : ''}${p.pl_pct.toFixed(1)}%</span>
            </div>`;
        }

        item.innerHTML = `
            <div class="w-row">
                <span class="w-name">${p.base_asset}</span>
                <span class="w-price">$${fmtPrice(p.price)}</span>
            </div>
            <div class="w-row" style="margin-top:2px;">
                <span class="w-meta">RSI <span class="${rsiCls}">${p.rsi.toFixed(0)}</span> &bull; ${p.confidence_weighted.toFixed(1)}/${p.max_score}</span>
                <span class="w-badges">
                    <span class="badge badge-${p.regime.replace(/_/g,'')}">${p.regime.replace(/_/g,' ').substring(0,5)}</span>
                    <span class="badge badge-${p.decision.toLowerCase()}">${p.decision}</span>
                </span>
            </div>
            <div class="w-bar-track"><div class="w-bar-fill" style="width:${confPct}%;background:${barColor};"></div></div>
            ${posHtml}`;
    }

    if (!selectedPair && names.length > 0 && !window._initDone) {
        window._initDone = true;
        setTimeout(() => selectPair(names[0]), 1500);
    }
}

// ─── Pair Selection ────────────────────────────────────────────

function selectPair(pair) {
    const changed = selectedPair !== pair;
    selectedPair  = pair;
    setText('chart-pair-label', pair);
    if (changed) {
        destroyChart();
        initChart();
        loadChart(pair, currentInterval);
        loadPairTrades(pair);
    }
    if (state) renderPairList(state);
}

// ─── Chart ─────────────────────────────────────────────────────

const BAR_SPACING = { '5m': 6, '15m': 8, '1h': 10, '4h': 12, '1d': 16 };
const CANDLE_LIMIT = 300;

function destroyChart() {
    if (chartRO)  { chartRO.disconnect(); chartRO = null; }
    if (chart)    { chart.remove(); chart = null; }
    candleSeries = null;
    volumeSeries = null;
}

function triggerChartResize() {
    const area = document.getElementById('chart-area');
    if (chart && area && area.clientWidth > 0 && area.clientHeight > 0) {
        chart.applyOptions({ width: area.clientWidth, height: area.clientHeight });
    }
}

function initChart() {
    const container = document.getElementById('chart-area');
    if (!container || typeof LightweightCharts === 'undefined') return;

    chart = LightweightCharts.createChart(container, {
        width:  container.clientWidth,
        height: container.clientHeight,
        layout: { background: { color: '#111111' }, textColor: '#888888', fontSize: 10 },
        grid: { vertLines: { color: '#1a1a1a' }, horzLines: { color: '#1a1a1a' } },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        rightPriceScale: { borderColor: '#333333' },
        timeScale: {
            borderColor:    '#333333',
            timeVisible:    true,
            secondsVisible: false,
            barSpacing:     BAR_SPACING[currentInterval] || 8,
            minBarSpacing:  3,
        },
    });

    candleSeries = chart.addCandlestickSeries({
        upColor: '#00c853', downColor: '#ff5252',
        borderUpColor: '#00c853', borderDownColor: '#ff5252',
        wickUpColor: '#00c85380', wickDownColor: '#ff525280',
    });

    volumeSeries = chart.addHistogramSeries({
        color: '#4dabf730', priceFormat: { type: 'volume' }, priceScaleId: '',
    });
    volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

    let resizeTimer = null;
    chartRO = new ResizeObserver(() => {
        if (resizeTimer) clearTimeout(resizeTimer);
        resizeTimer = setTimeout(triggerChartResize, 80);
    });
    chartRO.observe(container);
}

async function loadChart(pair, interval, isRefresh = false) {
    if (!chart || !pair) return;
    try {
        const resp = await fetch(`${API_BASE}/api/klines/${pair}?interval=${interval}&limit=${CANDLE_LIMIT}`);
        const data = await resp.json();
        if (!data.candles || !data.candles.length) return;

        chart.applyOptions({ timeScale: { barSpacing: BAR_SPACING[interval] || 8 } });

        candleSeries.setData(data.candles);
        volumeSeries.setData(data.candles.map(c => ({
            time: c.time, value: c.volume,
            color: c.close >= c.open ? '#00c85320' : '#ff525220',
        })));

        if (isRefresh) {
            // On periodic refresh: only scroll to latest if already at the right edge
            chart.timeScale().scrollToRealTime();
        } else {
            // On first load / pair switch: fit all candles then scroll to latest
            chart.timeScale().fitContent();
            chart.timeScale().scrollToRealTime();
        }

        await loadTradeMarkers(pair);
    } catch (err) { console.error('Chart:', err); }
}

async function loadTradeMarkers(pair) {
    if (!candleSeries) return;
    try {
        const resp = await fetch(`${API_BASE}/api/trades/${pair}?limit=50`);
        const data = await resp.json();
        if (!data.trades) return;
        const markers = data.trades
            .filter(t => t.timestamp)
            .map(t => ({
                time:     Math.floor(new Date(t.timestamp).getTime() / 1000),
                position: t.action === 'BUY' ? 'belowBar' : 'aboveBar',
                color:    t.action === 'BUY' ? '#00c853' : t.action === 'STOP_LOSS' ? '#ff5252' : '#ffa726',
                shape:    t.action === 'BUY' ? 'arrowUp' : 'arrowDown',
                text:     t.action === 'BUY' ? 'B' : t.action === 'STOP_LOSS' ? 'SL' : 'S',
            }))
            .sort((a, b) => a.time - b.time);
        if (markers.length) candleSeries.setMarkers(markers);
    } catch (_) {}
}

// ─── Interval Buttons ──────────────────────────────────────────

document.querySelectorAll('#interval-btns button').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('#interval-btns button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentInterval = btn.dataset.interval;
        if (selectedPair) { destroyChart(); initChart(); loadChart(selectedPair, currentInterval); }
    });
});

// ─── Signal Panel ──────────────────────────────────────────────

const SIG_CAT   = { rsi:'mean_reversion', bollinger:'mean_reversion', macd:'momentum', gas:'onchain' };
const CAT_COLOR = { mean_reversion:'var(--blue)', momentum:'var(--cyan)', onchain:'var(--yellow)' };
const SIG_LABEL = { rsi:'RSI', bollinger:'BB', macd:'MACD', gas:'Gas' };

function renderSignalPanel(pd, weights) {
    const container = document.getElementById('signal-bars');
    if (!container) return;

    const signals = ['rsi','macd','bollinger','gas'];
    let html = '';

    for (const name of signals) {
        const weight = (weights && weights[name]) || 1.0;
        const color  = CAT_COLOR[SIG_CAT[name]] || 'var(--txt-4)';
        let active = false, raw = '--';

        switch (name) {
            case 'rsi':       active = pd.rsi < 35;                               raw = pd.rsi?.toFixed(0) ?? '--'; break;
            case 'macd':      active = pd.macd_crossover === 'bullish';            raw = pd.macd_crossover || '--'; break;
            case 'bollinger': active = pd.bb_position === 'below_lower';           raw = pd.bb_position?.replace(/_/g,' ') || '--'; break;
            case 'gas':       active = pd.etherscan_gas > 0 && pd.etherscan_activity === 'high'; raw = pd.etherscan_gas || '-'; break;
        }

        const barW = Math.min(100, (weight / 1.5) * 100).toFixed(1);
        html += `<div class="sig-row" style="opacity:${active ? 1 : 0.5}">
            <span class="sig-name">${SIG_LABEL[name]}</span>
            <div class="sig-track"><div class="sig-fill" style="width:${barW}%;background:${active ? color : 'var(--txt-4)'};"></div></div>
            <span class="sig-w">w${weight.toFixed(2)}</span>
            <span class="sig-val ${active ? 'pos' : ''}">${raw}</span>
        </div>`;
    }
    container.innerHTML = html;
}

// ─── Weights ───────────────────────────────────────────────────

function renderWeights(weights) {
    const container = document.getElementById('weights-display');
    if (!container || !weights) return;
    let html = '<div style="font-size:8px;color:var(--txt-4);letter-spacing:.8px;font-weight:700;margin-bottom:6px;">LEARNED WEIGHTS</div>';
    for (const [name, value] of Object.entries(weights)) {
        const pct   = Math.max(0, Math.min(100, ((value - 0.5) / 1.0) * 100));
        const color = value > 1.1 ? 'var(--green)' : value < 0.9 ? 'var(--red)' : 'var(--blue)';
        html += `<div class="wt-row">
            <span class="wt-name">${SIG_LABEL[name] || name}</span>
            <div class="wt-track"><div class="wt-fill" style="width:${pct}%;background:${color};"></div></div>
            <span class="wt-val" style="color:${color}">${value.toFixed(3)}</span>
        </div>`;
    }
    container.innerHTML = html;
}

// ─── Trades ────────────────────────────────────────────────────

async function loadPairTrades(pair) {
    try {
        const resp = await fetch(`${API_BASE}/api/trades/${pair}?limit=30`);
        const data = await resp.json();
        tradesData = data.trades || [];
        renderTrades(tradesData);
    } catch (err) { console.error('Trades:', err); }
}

async function loadAllTrades() {
    try {
        const resp = await fetch(`${API_BASE}/api/trades?limit=50`);
        const data = await resp.json();
        tradesData = data.trades || [];
        renderTrades(tradesData);
    } catch (err) { console.error('Trades:', err); }
}

function renderTrades(trades) {
    const container = document.getElementById('trade-feed');
    if (!container) return;
    if (!trades.length) {
        container.innerHTML = '<div style="color:var(--txt-4);padding:12px;text-align:center;">No trades yet</div>';
        return;
    }
    let html = '';
    for (const t of trades) {
        const time   = t.timestamp ? new Date(t.timestamp).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '--';
        const plCls  = (t.pl_usdt || 0) >= 0 ? 'pos' : 'neg';
        const plHtml = t.action !== 'BUY' ? `<span class="${plCls}">${fmtSigned(t.pl_usdt || 0)}</span>` : '';

        let snapHtml = '';
        if (t.signal_snapshot && Object.keys(t.signal_snapshot).length) {
            snapHtml = '<div class="snap-grid">';
            for (const [k,v] of Object.entries(t.signal_snapshot))
                snapHtml += `<div class="snap-item"><span class="lbl">${k}</span><span>${typeof v==='number'?v.toFixed(2):v}</span></div>`;
            snapHtml += '</div>';
        }

        const outCls = t.outcome_label==='strong_win'||t.outcome_label==='weak_win' ? 'pos' : t.outcome_label==='loss' ? 'neg' : 'neu';
        html += `<div class="tf-entry" onclick="this.classList.toggle('expanded')">
            <div class="tf-head">
                <span><span class="tf-action tf-action-${t.action}">${t.action}</span> <b>${t.pair}</b></span>
                <span class="tf-time">${time}</span>
            </div>
            <div class="tf-detail">
                <span>$${fmtPrice(t.coin_price)} &times; ${(t.amount_coin||0).toFixed(4)}</span>
                ${plHtml}
            </div>
            <div class="tf-expand">
                <span class="lbl">Conf:</span> ${(t.confidence_weighted||0).toFixed(1)}
                &nbsp;<span class="lbl">Out:</span> <span class="${outCls}">${t.outcome_label||'--'}</span>
                ${snapHtml}
            </div>
        </div>`;
    }
    container.innerHTML = html;
}

// ─── Config ────────────────────────────────────────────────────

async function loadConfig() {
    try {
        const resp = await fetch(`${API_BASE}/api/config`);
        configData = await resp.json();
        renderConfig(configData);
    } catch (err) { console.error('Config:', err); }
}

function renderConfig(cfg) {
    const c = document.getElementById('config-display');
    if (!c || !cfg) return;
    const row = (l,v) => `<div class="cfg-row"><span class="lbl">${l}</span><span class="val">${v}</span></div>`;
    c.innerHTML = `
        <div class="cfg-section"><h4>Trading</h4>
            ${row('Mode',cfg.mode)}${row('Threshold',cfg.buy_threshold)}${row('Stop Loss',cfg.stop_loss_pct+'%')}
            ${row('Profit Target',cfg.profit_target_pct+'%')}${row('ATR Exits',cfg.use_atr_exits?'ON':'OFF')}
        </div>
        <div class="cfg-section"><h4>Risk</h4>
            ${row('Drawdown',cfg.drawdown_trigger_pct+'%')}${row('Cooldown',cfg.cooldown_seconds+'s')}
            ${row('Reserve',(cfg.reserve_pct*100)+'%')}
        </div>
        <div class="cfg-section"><h4>Caps</h4>
            ${Object.entries(cfg.category_caps||{}).map(([k,v])=>row(k,v)).join('')}
        </div>`;
}

// ─── Controls ──────────────────────────────────────────────────

function updateBotControls(bot) {
    const btn = document.getElementById('btn-pause');
    if (!btn) return;
    btn.textContent = bot.paused ? 'Resume Bot' : 'Pause Bot';
    btn.className   = bot.paused ? 'ctl-btn paused' : 'ctl-btn';
    const inp = document.getElementById('threshold-input');
    if (inp && !inp.matches(':focus')) inp.value = bot.threshold;
}

async function togglePause() {
    try { await fetch(`${API_BASE}/api/control/pause`, { method:'POST' }); } catch(e) { console.error(e); }
}
async function setThreshold() {
    const val = parseFloat(document.getElementById('threshold-input').value);
    if (isNaN(val)||val<0||val>10) { alert('0–10'); return; }
    try { await fetch(`${API_BASE}/api/control/threshold?value=${val}`, { method:'POST' }); } catch(e) { console.error(e); }
}
async function closeAll() {
    if (!confirm('EMERGENCY: Close ALL positions at market price?')) return;
    try {
        const resp = await fetch(`${API_BASE}/api/control/close-all`, { method:'POST' });
        const data = await resp.json();
        alert(`Closed ${data.count} positions`);
    } catch(e) { alert('Failed'); }
}

document.getElementById('btn-pause')    ?.addEventListener('click', togglePause);
document.getElementById('btn-threshold')?.addEventListener('click', setThreshold);
document.getElementById('btn-close-all')?.addEventListener('click', closeAll);

// ─── Sidebar Tabs ──────────────────────────────────────────────

document.querySelectorAll('.sb-tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.sb-tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById(`tab-${tab.dataset.tab}`)?.classList.add('active');
        if (tab.dataset.tab === 'config' && !configData) loadConfig();
        if (tab.dataset.tab === 'trades') { if (selectedPair) loadPairTrades(selectedPair); else loadAllTrades(); }
    });
});

// ─── Helpers ───────────────────────────────────────────────────

function fmt(n) {
    if (n == null) return '--';
    return Number(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
}
function fmtPrice(n) {
    if (!n && n!==0) return '--';
    if (n>=1000) return Number(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
    if (n>=1)    return Number(n).toFixed(4);
    return Number(n).toFixed(6);
}
function fmtSigned(n) {
    if (n==null) return '--';
    return (n>=0?'+':'') + Number(n).toFixed(2);
}
function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}
function setColor(id, text, positive, explicitCls) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.className   = explicitCls || (positive ? 'pos' : 'neg');
}

// ─── Init ──────────────────────────────────────────────────────

// Load saved layout or apply default, then init interactions
if (!loadLayout()) applyDefaultLayout();
initDrag();
initResize();
initMinimize();

// Bring chart window to front by default
bringToFront(document.getElementById('win-chart'));

connectWS();
loadAllTrades();

setInterval(() => { if (selectedPair) loadChart(selectedPair, currentInterval, true); }, 30000);
setInterval(() => { if (selectedPair) loadPairTrades(selectedPair); }, 15000);
