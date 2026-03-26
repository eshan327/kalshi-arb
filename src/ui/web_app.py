import asyncio
import threading
from flask import Flask, jsonify, request
from core.auth import get_authenticated_client
from core.config import (
    BRTI_RECALC_INTERVAL_SEC,
    ORDERBOOK_VIEW_DEPTH,
    WEB_HOST,
    WEB_PORT,
    WS_LOG_DEFAULT_LIMIT,
)
from engine.streamer import (
    get_live_market_info,
    get_live_orderbook_snapshot,
    get_reconciliation_log,
    get_top10_impact_log,
    get_ws_message_log,
    get_ws_message_log_size,
    get_ws_processing_stats,
    run_market_streamer,
)
from feeds.brti_aggregator import (
    get_brti_state,
  get_brti_settlement_proxy,
    get_brti_ticks,
    get_brti_ws_log,
    get_brti_ws_stats,
    run_brti_aggregator,
)

app = Flask(__name__)

_services_started = False
_services_lock = threading.Lock()


def extract_suggested_strike(market_info: dict):
    """Best-effort strike extraction from Kalshi market metadata."""
    if not market_info:
        return None

    direct_keys = [
        "strike_price",
        "strike",
        "target_price",
        "floor_strike",
        "cap_strike",
    ]
    for key in direct_keys:
        value = market_info.get(key)
        if isinstance(value, (int, float)):
            return float(value)

    text_keys = ["subtitle", "title", "yes_sub_title", "no_sub_title", "rulebook_text"]
    for key in text_keys:
        text = market_info.get(key)
        if not isinstance(text, str):
            continue
        cleaned = text.replace(",", "")
        matches = []
        token = ""
        for ch in cleaned:
            if ch.isdigit() or ch == ".":
                token += ch
            else:
                if token:
                    matches.append(token)
                    token = ""
        if token:
            matches.append(token)

        for candidate in matches:
            try:
                value = float(candidate)
            except ValueError:
                continue
            if 1000 <= value <= 2_000_000:
                return value

    return None


def _start_background_services_once() -> None:
    global _services_started

    with _services_lock:
        if _services_started:
            return

        def _runner() -> None:
            asyncio.run(_run_services())

        thread = threading.Thread(target=_runner, name="kalshi-runtime", daemon=True)
        thread.start()
        _services_started = True


async def _run_services() -> None:
    await asyncio.gather(
        asyncio.create_task(run_market_streamer()),
        asyncio.create_task(run_brti_aggregator(recalc_interval=BRTI_RECALC_INTERVAL_SEC)),
    )


@app.get("/")
def index():
    return """
<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Kalshi Arb Dashboard</title>
  <style>
    :root {
      --bg: #0c1118;
      --card: #141b25;
      --line: #2a3647;
      --text: #d8dfeb;
      --muted: #93a6c4;
      --ok: #4bd19a;
      --warn: #ffcf72;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      --sans: "Avenir Next", "Segoe UI", sans-serif;
    }
    body {
      margin: 0;
      background: radial-gradient(70% 45% at 10% 0%, #1b2640 0%, var(--bg) 58%);
      color: var(--text);
      font-family: var(--sans);
    }
    .wrap {
      max-width: 1380px;
      margin: 18px auto;
      padding: 0 14px;
      display: grid;
      gap: 14px;
      grid-template-columns: 1.45fr 0.85fr;
    }
    .card {
      background: linear-gradient(180deg, #182230 0%, var(--card) 100%);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 14px;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.22);
    }
    h2, h3 { margin: 0 0 10px; }
    .summary-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(220px, 1fr));
      gap: 8px;
      margin-bottom: 12px;
    }
    .summary-box {
      border: 1px solid #31435e;
      border-radius: 10px;
      padding: 8px 10px;
      background: #121a25;
    }
    .summary-label {
      font-size: 11px;
      letter-spacing: 0.3px;
      color: var(--muted);
      margin-bottom: 3px;
      font-family: var(--mono);
    }
    .summary-value {
      font-size: 14px;
      color: var(--text);
      font-family: var(--mono);
    }
    .ok { color: var(--ok); }
    .warn { color: var(--warn); }
    .grid2 {
      display: grid;
      gap: 10px;
      grid-template-columns: 1fr 1fr;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-family: var(--mono);
      font-size: 12px;
    }
    th, td {
      border-bottom: 1px solid #243143;
      padding: 5px 6px;
      text-align: right;
    }
    th:first-child, td:first-child { text-align: left; }
    .hero {
      margin-top: 6px;
      font-family: var(--mono);
      font-size: clamp(34px, 5.4vw, 58px);
      color: #d4ffe2;
      line-height: 1;
    }
    .mono { font-family: var(--mono); color: var(--muted); }
    .chart-wrap {
      margin-top: 10px;
      border: 1px solid #243143;
      border-radius: 10px;
      background: #0d1520;
      padding: 8px;
    }
    #brtiChart {
      width: 100%;
      height: 220px;
      display: block;
    }
    details { margin-top: 10px; }
    summary {
      cursor: pointer;
      font-family: var(--mono);
      color: #9fb1cf;
      margin-bottom: 6px;
    }
    .log {
      max-height: 34vh;
      overflow: auto;
      background: #0b1119;
      border: 1px solid #223044;
      border-radius: 10px;
      padding: 10px;
      font-family: var(--mono);
      font-size: 11px;
      white-space: pre-wrap;
    }
    .badge {
      display: inline-block;
      border: 1px solid #31435e;
      border-radius: 999px;
      padding: 3px 10px;
      margin-right: 6px;
      margin-bottom: 8px;
      font-size: 12px;
      color: var(--muted);
    }
    .meta-list {
      font-family: var(--mono);
      font-size: 12px;
      line-height: 1.7;
      color: var(--muted);
    }
    @media (max-width: 980px) {
      .wrap { grid-template-columns: 1fr; }
      .summary-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <section class=\"card\">
      <h2>Kalshi Reconstructed Orderbook</h2>
      <div class=\"summary-grid\" id=\"summary\"></div>

      <div class=\"grid2\">
        <div>
          <h3>YES Top 10</h3>
          <table id=\"yesTable\"></table>
        </div>
        <div>
          <h3>NO Top 10</h3>
          <table id=\"noTable\"></table>
        </div>
      </div>

      <h3 style=\"margin-top:14px\">BRTI Synthesized Price</h3>
      <div id=\"brti\" class=\"hero\">--</div>
      <div id=\"brtiMeta\" class=\"mono\"></div>
      <div id=\"settlementProxy\" class=\"mono\"></div>

      <div class=\"chart-wrap\">
        <canvas id=\"brtiChart\" width=\"1100\" height=\"220\"></canvas>
      </div>

      <details>
        <summary>Explain technical metrics</summary>
        <div class=\"meta-list\" id=\"metricHelp\"></div>
      </details>
    </section>

    <section class=\"card\">
      <h2>Verification Streams</h2>
      <div class=\"badge\">Kalshi messages tracked end-to-end</div>
      <div class=\"badge\">BRTI exchange messages tracked end-to-end</div>

      <details>
        <summary>Reconciliation Checks (REST vs WebSocket)</summary>
        <div class=\"log\" id=\"reconLog\"></div>
      </details>

      <details>
        <summary>BRTI Tick Stream</summary>
        <div class=\"log\" id=\"brtiTicks\"></div>
      </details>

      <details>
        <summary>Kalshi Top-10 Impact Events</summary>
        <div class=\"log\" id=\"impactLog\"></div>
      </details>

      <details>
        <summary>Kalshi Raw WebSocket Messages</summary>
        <div class=\"log\" id=\"rawLog\"></div>
      </details>

      <details>
        <summary>BRTI Raw Exchange WebSocket Messages</summary>
        <div class=\"log\" id=\"brtiRawLog\"></div>
      </details>
    </section>
  </div>

<script>
function fmtQty(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  if (Math.abs(n) < 1e-6) return "0";
  return n.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 2 });
}

function fmtPriceCent(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return `${n.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 2 })}c`;
}

function buildBookTable(el, bids, asks) {
  const asksDesc = [...asks].sort((a, b) => b[0] - a[0]);
  const bidsDesc = [...bids].sort((a, b) => b[0] - a[0]);

  let html = "<thead><tr><th>Side</th><th>Price</th><th>Contracts</th></tr></thead><tbody>";
  for (const [price, qty] of asksDesc) {
    html += `<tr><td>Ask</td><td>${fmtPriceCent(price)}</td><td>${fmtQty(qty)}</td></tr>`;
  }
  html += `<tr><td colspan=\"3\" style=\"text-align:center;color:#9fb1cf;font-size:11px;\">Spread / Last Trade Zone</td></tr>`;
  for (const [price, qty] of bidsDesc) {
    html += `<tr><td>Bid</td><td>${fmtPriceCent(price)}</td><td>${fmtQty(qty)}</td></tr>`;
  }
  html += "</tbody>";
  el.innerHTML = html;
}

function drawBrtiChart(points) {
  const canvas = document.getElementById('brtiChart');
  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  const series = points
    .filter((p) => p.brti !== null && Number.isFinite(Number(p.brti)) && Number.isFinite(Number(p.ts)))
    .map((p) => ({ ts: Number(p.ts), value: Number(p.brti) }));

  if (!series.length) {
    ctx.fillStyle = '#8ea0bf';
    ctx.font = '12px ui-monospace';
    ctx.fillText('No BRTI history yet', 12, 20);
    return;
  }

  const vals = series.map((p) => p.value);
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const suggestedStrike = Number(window.__suggestedStrike);
  const strike = Number.isFinite(suggestedStrike) ? suggestedStrike : null;

  let yMin = min;
  let yMax = max;
  if (strike !== null) {
    yMin = Math.min(yMin, strike);
    yMax = Math.max(yMax, strike);
  }

  const padPct = 0.08;
  const spanRaw = Math.max(yMax - yMin, 1e-6);
  yMin -= spanRaw * padPct;
  yMax += spanRaw * padPct;
  const span = yMax - yMin;

  const padLeft = 74;
  const padRight = 16;
  const padTop = 18;
  const padBottom = 44;
  ctx.strokeStyle = '#1f2a3d';
  ctx.lineWidth = 1;
  ctx.strokeRect(padLeft, padTop, w - padLeft - padRight, h - padTop - padBottom);

  ctx.fillStyle = '#8ea0bf';
  ctx.font = '11px ui-monospace';
  ctx.textAlign = 'left';
  ctx.fillText('Price (USD)', padLeft + 4, padTop + 12);

  const yTicks = 4;
  for (let i = 0; i <= yTicks; i++) {
    const frac = i / yTicks;
    const y = padTop + (1 - frac) * (h - padTop - padBottom);
    const v = yMin + frac * span;
    ctx.strokeStyle = '#152134';
    ctx.beginPath();
    ctx.moveTo(padLeft, y);
    ctx.lineTo(w - padRight, y);
    ctx.stroke();

    ctx.fillStyle = '#8ea0bf';
    ctx.textAlign = 'right';
    ctx.fillText(v.toFixed(2), padLeft - 8, y + 4);
  }

  const minTs = series[0].ts;
  const maxTs = series[series.length - 1].ts;
  const tsSpan = Math.max(maxTs - minTs, 1e-6);
  const xTicks = 8;
  let lastLabelRight = -Infinity;
  ctx.textAlign = 'center';
  for (let i = 0; i <= xTicks; i++) {
    const frac = i / xTicks;
    const x = padLeft + frac * (w - padLeft - padRight);
    const ts = minTs + frac * tsSpan;

    ctx.strokeStyle = '#152134';
    ctx.beginPath();
    ctx.moveTo(x, padTop);
    ctx.lineTo(x, h - padBottom);
    ctx.stroke();

    const label = new Date(ts * 1000).toLocaleTimeString([], {
      hour12: false,
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
    const width = ctx.measureText(label).width;
    const left = x - width / 2;
    const right = x + width / 2;
    if (left > lastLabelRight + 8 || i === xTicks) {
      ctx.fillStyle = '#8ea0bf';
      ctx.fillText(label, x, h - 20);
      lastLabelRight = right;
    }
  }
  ctx.textAlign = 'center';
  ctx.fillStyle = '#8ea0bf';
  ctx.fillText('Time (HH:MM:SS)', padLeft + (w - padLeft - padRight) / 2, h - 4);

  if (strike !== null) {
    const yStrike = padTop + (1 - (strike - yMin) / span) * (h - padTop - padBottom);
    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = '#ffc96a';
    ctx.beginPath();
    ctx.moveTo(padLeft, yStrike);
    ctx.lineTo(w - padRight, yStrike);
    ctx.stroke();
    ctx.setLineDash([]);

    const label = `Strike ${strike.toFixed(2)}`;
    const labelW = ctx.measureText(label).width;
    const labelX = w - padRight - labelW - 8;
    const labelY = Math.max(14, Math.min(h - padBottom - 4, yStrike - 6));
    ctx.fillStyle = 'rgba(13, 21, 32, 0.92)';
    ctx.fillRect(labelX - 4, labelY - 10, labelW + 8, 14);
    ctx.fillStyle = '#ffc96a';
    ctx.textAlign = 'left';
    ctx.fillText(label, labelX, labelY);
  }

  ctx.beginPath();
  ctx.lineWidth = 2;
  ctx.strokeStyle = '#4bd19a';
  series.forEach((point, i) => {
    const x = padLeft + ((point.ts - minTs) / tsSpan) * (w - padLeft - padRight);
    const y = h - padBottom - ((point.value - yMin) / span) * (h - padTop - padBottom);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

async function refreshState() {
  const stateRes = await fetch('/api/state?depth=10');
  const state = await stateRes.json();
  const ob = state.orderbook;

  const summary = document.getElementById('summary');
  summary.innerHTML =
    `<div class=\"summary-box\"><div class=\"summary-label\">Orderbook Status</div><div class=\"summary-value ${ob.initialized ? 'ok' : 'warn'}\">${ob.initialized ? 'Live and updating' : 'Waiting for bootstrap'}</div></div>` +
    `<div class=\"summary-box\"><div class=\"summary-label\">Tracked Market</div><div class=\"summary-value\">${ob.market_ticker ?? 'n/a'}</div></div>` +
    `<div class=\"summary-box\"><div class=\"summary-label\">Next Sequence Expected</div><div class=\"summary-value\">${ob.expected_seq ?? 'n/a'}</div></div>` +
    `<div class=\"summary-box\"><div class=\"summary-label\">Logged Kalshi Events</div><div class=\"summary-value\">${state.ws_log_size}</div></div>`;

  buildBookTable(document.getElementById('yesTable'), ob.yes_bids, ob.yes_asks);
  buildBookTable(document.getElementById('noTable'), ob.no_bids, ob.no_asks);

  const brti = state.brti;
  if (state.suggested_strike != null) {
    window.__suggestedStrike = Number(state.suggested_strike);
  } else {
    window.__suggestedStrike = null;
  }
  document.getElementById('brti').textContent = brti.brti ? `$${Number(brti.brti).toLocaleString()}` : '--';
  document.getElementById('brtiMeta').textContent =
    `Latest depth used: ${brti.depth} BTC | Exchanges included: ${brti.exchanges} | Timestamp: ${new Date((brti.timestamp || 0) * 1000).toISOString()}`;

  const proxy = state.synthetic_settlement_proxy || {};
  const proxyAvg = proxy.average != null ? `$${Number(proxy.average).toLocaleString()}` : '--';
  document.getElementById('settlementProxy').textContent =
    `Synthetic 60s RTI average (proxy): ${proxyAvg} | samples=${proxy.samples ?? 0}`;

  const help = document.getElementById('metricHelp');
  const k = state.kalshi_ws_stats || {};
  const b = state.brti_ws_stats || {};
  help.innerHTML =
    `Orderbook Status: whether bootstrap + sequence sync succeeded.<br>` +
    `Tracked Market: exact market ticker currently reconstructed.<br>` +
    `Next Sequence Expected: the next Kalshi delta sequence ID required for in-order updates.<br>` +
    `Logged Kalshi Events: retained raw websocket events for auditing.<br>` +
    `Latest depth used: BRTI utilized depth in BTC for price calculation.<br>` +
    `Exchanges included: number of clean, non-stale exchanges currently used by BRTI.<br><br>` +
    `Synthetic 60s RTI average: rolling average of valid synthetic BRTI prints over the last 60 seconds. This approximates settlement mechanics but is not the official CF value.<br><br>` +
    `<strong>Kalshi message processing counters</strong><br>` +
    `Incoming websocket messages: ${k.total_received ?? 'n/a'}<br>` +
    `Orderbook deltas received: ${k.orderbook_delta_received ?? 'n/a'}<br>` +
    `Orderbook deltas buffered: ${k.orderbook_delta_buffered ?? 'n/a'}<br>` +
    `Orderbook deltas applied: ${k.orderbook_delta_applied ?? 'n/a'}<br>` +
    `Orderbook stale deltas ignored: ${k.orderbook_delta_stale_ignored ?? 'n/a'}<br>` +
    `Orderbook sequence gaps: ${k.orderbook_delta_seq_gap ?? 'n/a'}<br><br>` +
    `<strong>BRTI exchange message processing counters</strong><br>` +
    `Incoming exchange websocket messages: ${b.total_received ?? 'n/a'}<br>` +
    `Parsed exchange websocket messages: ${b.total_parsed ?? 'n/a'}<br>` +
    `Orderbook updates applied to BRTI books: ${b.book_updates_applied ?? 'n/a'}<br>` +
    `Coinbase messages: ${b.coinbase_received ?? 'n/a'} | Kraken messages: ${b.kraken_received ?? 'n/a'}<br>` +
    `Gemini messages: ${b.gemini_received ?? 'n/a'} | Bitstamp messages: ${b.bitstamp_received ?? 'n/a'} | Paxos messages: ${b.paxos_received ?? 'n/a'}<br>` +
    `Coinbase parsed: ${b.coinbase_parsed ?? 'n/a'} | Kraken parsed: ${b.kraken_parsed ?? 'n/a'} | Gemini parsed: ${b.gemini_parsed ?? 'n/a'}<br>` +
    `Bitstamp parsed: ${b.bitstamp_parsed ?? 'n/a'} | Paxos parsed: ${b.paxos_parsed ?? 'n/a'}`;
}

async function refreshReconLog() {
  const res = await fetch('/api/reconciliation-log?limit=200');
  const rows = await res.json();
  const lines = rows.map((row) => {
    const ts = new Date(row.ts * 1000).toISOString();
    return `${ts} | breach=${row.breach} | consecutive=${row.consecutive_breaches} | action=${row.action} | metrics=${JSON.stringify(row.metrics)}`;
  });
  const el = document.getElementById('reconLog');
  const wasAtBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
  el.textContent = lines.join("\\n");
  if (wasAtBottom) el.scrollTop = el.scrollHeight;
}

async function refreshRawLog() {
  const res = await fetch('/api/ws-log?limit=200');
  const rows = await res.json();
  const lines = rows.map((row) => {
    const ts = new Date(row.ts * 1000).toISOString();
    return `${ts} | type=${row.type} | seq=${row.seq} | status=${row.status} | payload=${JSON.stringify(row.payload)}`;
  });
  const el = document.getElementById('rawLog');
  const wasAtBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
  el.textContent = lines.join("\\n");
  if (wasAtBottom) el.scrollTop = el.scrollHeight;
}

async function refreshImpactLog() {
  const res = await fetch('/api/top10-impact?limit=200');
  const rows = await res.json();
  const lines = rows.map((row) => {
    const ts = new Date(row.ts * 1000).toISOString();
    return `${ts} | seq=${row.seq} | top10_changed=${row.changed} | payload=${JSON.stringify(row.payload)}`;
  });
  const el = document.getElementById('impactLog');
  const wasAtBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
  el.textContent = lines.join("\\n");
  if (wasAtBottom) el.scrollTop = el.scrollHeight;
}

async function refreshBrtiTicks() {
  const res = await fetch('/api/brti-ticks?limit=200');
  const rows = await res.json();
  const lines = rows.map((row) => {
    const ts = new Date(row.ts * 1000).toISOString();
    return `${ts} | status=${row.status} | brti=${row.brti} | depth=${row.depth} | exchanges=${row.exchanges} | levels=${JSON.stringify(row.levels)}`;
  });
  const el = document.getElementById('brtiTicks');
  const wasAtBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
  el.textContent = lines.join("\\n");
  if (wasAtBottom) el.scrollTop = el.scrollHeight;
  drawBrtiChart(rows);
}

async function refreshBrtiRawLog() {
  const res = await fetch('/api/brti-ws-log?limit=200');
  const rows = await res.json();
  const lines = rows.map((row) => {
    const ts = new Date(row.ts * 1000).toISOString();
    return `${ts} | exchange=${row.exchange} | status=${row.status} | type=${row.raw_type} | channel=${row.raw_channel} | event=${row.raw_event}`;
  });
  const el = document.getElementById('brtiRawLog');
  const wasAtBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
  el.textContent = lines.join("\\n");
  if (wasAtBottom) el.scrollTop = el.scrollHeight;
}

setInterval(() => { refreshState().catch(() => {}); }, 400);
setInterval(() => { refreshRawLog().catch(() => {}); }, 700);
setInterval(() => { refreshImpactLog().catch(() => {}); }, 700);
setInterval(() => { refreshBrtiTicks().catch(() => {}); }, 900);
setInterval(() => { refreshBrtiRawLog().catch(() => {}); }, 900);
setInterval(() => { refreshReconLog().catch(() => {}); }, 1100);

refreshState();
refreshRawLog();
refreshImpactLog();
refreshBrtiTicks();
refreshBrtiRawLog();
refreshReconLog();
</script>
</body>
</html>
"""


@app.get("/api/state")
def api_state():
    depth = request.args.get("depth", default=ORDERBOOK_VIEW_DEPTH, type=int)
    snapshot = get_live_orderbook_snapshot(depth=max(1, min(depth, ORDERBOOK_VIEW_DEPTH)))
    brti = get_brti_state()
    log_size = get_ws_message_log_size()
    kalshi_stats = get_ws_processing_stats()
    brti_stats = get_brti_ws_stats()
    settlement_proxy = get_brti_settlement_proxy(window_seconds=60)
    market_info = get_live_market_info()
    suggested_strike = extract_suggested_strike(market_info)
    return jsonify(
        {
            "orderbook": snapshot,
            "brti": brti,
            "synthetic_settlement_proxy": settlement_proxy,
            "ws_log_size": log_size,
            "kalshi_ws_stats": kalshi_stats,
            "brti_ws_stats": brti_stats,
            "market_info": market_info,
            "suggested_strike": suggested_strike,
        }
    )


@app.get("/api/ws-log")
def api_ws_log():
    limit = request.args.get("limit", default=WS_LOG_DEFAULT_LIMIT, type=int)
    return jsonify(get_ws_message_log(limit=max(1, min(limit, WS_LOG_DEFAULT_LIMIT))))


@app.get("/api/top10-impact")
def api_top10_impact():
    limit = request.args.get("limit", default=WS_LOG_DEFAULT_LIMIT, type=int)
    return jsonify(get_top10_impact_log(limit=max(1, min(limit, WS_LOG_DEFAULT_LIMIT))))


@app.get("/api/brti-ticks")
def api_brti_ticks():
    limit = request.args.get("limit", default=WS_LOG_DEFAULT_LIMIT, type=int)
    return jsonify(get_brti_ticks(limit=max(1, min(limit, WS_LOG_DEFAULT_LIMIT))))


@app.get("/api/brti-ws-log")
def api_brti_ws_log():
    limit = request.args.get("limit", default=WS_LOG_DEFAULT_LIMIT, type=int)
    return jsonify(get_brti_ws_log(limit=max(1, min(limit, WS_LOG_DEFAULT_LIMIT))))


@app.get("/api/reconciliation-log")
def api_reconciliation_log():
    limit = request.args.get("limit", default=WS_LOG_DEFAULT_LIMIT, type=int)
    return jsonify(get_reconciliation_log(limit=max(1, min(limit, WS_LOG_DEFAULT_LIMIT))))


def run_web_app() -> None:
    try:
        client = get_authenticated_client()
        balance_res = client.get_balance()
        print(f"Balance: ${balance_res.balance / 100:,.2f}")
    except Exception as exc:
        print(f"Authentication Failed: {exc}")
        raise SystemExit(1)

    _start_background_services_once()
    print(f"Web dashboard running at http://{WEB_HOST}:{WEB_PORT}")
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)
