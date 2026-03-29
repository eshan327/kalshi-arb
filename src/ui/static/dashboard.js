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

function fmtPct(value, decimals = 2) {
  if (value == null || !Number.isFinite(Number(value))) return "—";
  return `${Number(value).toFixed(decimals)}%`;
}

/** Probability in [0,1]: green YES if >50%, red NO if <50%, neutral at 50%. */
function vizFillClass(p) {
  if (p == null || !Number.isFinite(Number(p))) return "viz-fill-neutral";
  const x = Number(p);
  if (x > 0.5) return "viz-fill-yes";
  if (x < 0.5) return "viz-fill-no";
  return "viz-fill-neutral";
}

function vizPctClass(p) {
  if (p == null || !Number.isFinite(Number(p))) return "viz-pct viz-pct-neutral";
  const x = Number(p);
  if (x > 0.5) return "viz-pct viz-pct-yes";
  if (x < 0.5) return "viz-pct viz-pct-no";
  return "viz-pct viz-pct-neutral";
}

function renderPhase3Section(pricing, micro) {
  const barM = document.getElementById("pModelBar");
  const barB = document.getElementById("pBookBar");
  const labM = document.getElementById("pModelPctLabel");
  const labB = document.getElementById("pBookPctLabel");
  const grid = document.getElementById("phase3Grid");
  const st = document.getElementById("phase3Status");
  if (!barM || !grid) {
    return;
  }

  if (!pricing || !pricing.ready) {
    barM.style.width = "0%";
    barM.className = `viz-fill ${vizFillClass(null)}`;
    if (barB) {
      barB.style.width = "0%";
      barB.className = `viz-fill ${vizFillClass(null)}`;
    }
    if (labM) {
      labM.textContent = "—";
      labM.className = vizPctClass(null);
    }
    if (labB) {
      labB.textContent = "—";
      labB.className = vizPctClass(null);
    }
    grid.innerHTML = "";
    if (st) {
      st.textContent = pricing?.reason
        ? `P(model) unavailable: ${pricing.reason}`
        : "Waiting for BRTI, inferred strike, and market close time…";
    }
    return;
  }

  const pm = Number(pricing.p_model);
  const pctModel = Number.isFinite(pm) ? Math.min(100, Math.max(0, pm * 100)) : 0;
  barM.style.width = `${pctModel}%`;
  barM.className = `viz-fill ${vizFillClass(pm)}`;
  if (labM) {
    labM.textContent = fmtPct(pricing.p_model_pct, 2);
    labM.className = vizPctClass(pm);
  }

  if (micro && micro.p_book != null && Number.isFinite(Number(micro.p_book))) {
    const pb = Number(micro.p_book);
    const pbw = Math.min(100, Math.max(0, pb * 100));
    if (barB) {
      barB.style.width = `${pbw}%`;
      barB.className = `viz-fill ${vizFillClass(pb)}`;
    }
    if (labB) {
      labB.textContent = fmtPct(100 * pb, 2);
      labB.className = vizPctClass(pb);
    }
  } else {
    if (barB) {
      barB.style.width = "0%";
      barB.className = `viz-fill ${vizFillClass(null)}`;
    }
    if (labB) {
      labB.textContent = "—";
      labB.className = vizPctClass(null);
    }
  }

  const sigNote = pricing.vol_is_fallback ? " (fallback σ)" : "";
  if (st) {
    st.textContent = `Regime: ${pricing.regime ?? "—"} · σ annual ${pricing.sigma_annual}${sigNote} · ${pricing.sigma_samples} BRTI prints`;
  }

  const req =
    pricing.twap_required_avg != null
      ? `<div class="summary-box"><div class="summary-label">Req. avg (rest)</div><div class="summary-value">$${Number(
          pricing.twap_required_avg,
        ).toLocaleString()}</div></div>`
      : "";

  const twapLine =
    pricing.twap_partial_avg != null
      ? `Partial avg $${Number(pricing.twap_partial_avg).toLocaleString()} · ${pricing.twap_seconds_elapsed}/60 s`
      : Number(pricing.seconds_to_expiry) > 60
        ? "Not in final minute yet"
        : "Accumulating…";

  grid.innerHTML =
    `<div class="summary-box"><div class="summary-label">Sec to expiry</div><div class="summary-value">${
      pricing.seconds_to_expiry != null ? Number(pricing.seconds_to_expiry).toFixed(1) : "—"
    }</div></div>` +
    `<div class="summary-box"><div class="summary-label">Strike (USD)</div><div class="summary-value">${
      pricing.strike_usd != null ? "$" + Number(pricing.strike_usd).toLocaleString() : "—"
    }</div></div>` +
    `<div class="summary-box"><div class="summary-label">σ eff (pricer)</div><div class="summary-value">${
      pricing.sigma_eff != null ? pricing.sigma_eff : "—"
    }</div></div>` +
    `<div class="summary-box"><div class="summary-label">Spot BRTI</div><div class="summary-value">${
      pricing.spot_brti != null ? "$" + Number(pricing.spot_brti).toLocaleString() : "—"
    }</div></div>` +
    `<div class="summary-box" style="grid-column:1/-1"><div class="summary-label">TWAP / window</div><div class="summary-value" style="font-size:12px;line-height:1.4">${twapLine}</div></div>` +
    req;
}

function renderInnerCalcJson(pricing, micro) {
  const el = document.getElementById("innerCalcJson");
  if (!el) {
    return;
  }
  const payload = {
    pricing: pricing ?? null,
    microstructure: micro ?? null,
  };
  el.textContent = JSON.stringify(payload, null, 2);
}

function buildBookTable(el, bids, asks) {
  const asksDesc = [...asks].sort((a, b) => b[0] - a[0]);
  const bidsDesc = [...bids].sort((a, b) => b[0] - a[0]);

  let html = "<thead><tr><th>Side</th><th>Price</th><th>Contracts</th></tr></thead><tbody>";
  for (const [price, qty] of asksDesc) {
    html += `<tr><td>Ask</td><td>${fmtPriceCent(price)}</td><td>${fmtQty(qty)}</td></tr>`;
  }
  html += '<tr><td colspan="3" style="text-align:center;color:#9fb1cf;font-size:11px;">Spread / Last Trade Zone</td></tr>';
  for (const [price, qty] of bidsDesc) {
    html += `<tr><td>Bid</td><td>${fmtPriceCent(price)}</td><td>${fmtQty(qty)}</td></tr>`;
  }
  html += "</tbody>";
  el.innerHTML = html;
}

function drawPriceChart(canvasId, series, emptyMessage) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) {
    return;
  }

  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  if (!series.length) {
    ctx.fillStyle = "#8ea0bf";
    ctx.font = "12px ui-monospace";
    ctx.fillText(emptyMessage, 12, 20);
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
  ctx.strokeStyle = "#1f2a3d";
  ctx.lineWidth = 1;
  ctx.strokeRect(padLeft, padTop, w - padLeft - padRight, h - padTop - padBottom);

  ctx.fillStyle = "#8ea0bf";
  ctx.font = "11px ui-monospace";
  ctx.textAlign = "left";
  ctx.fillText("Price (USD)", padLeft + 4, padTop + 12);

  const yTicks = 4;
  for (let i = 0; i <= yTicks; i++) {
    const frac = i / yTicks;
    const y = padTop + (1 - frac) * (h - padTop - padBottom);
    const v = yMin + frac * span;
    ctx.strokeStyle = "#152134";
    ctx.beginPath();
    ctx.moveTo(padLeft, y);
    ctx.lineTo(w - padRight, y);
    ctx.stroke();

    ctx.fillStyle = "#8ea0bf";
    ctx.textAlign = "right";
    ctx.fillText(v.toFixed(2), padLeft - 8, y + 4);
  }

  const minTs = series[0].ts;
  const maxTs = series[series.length - 1].ts;
  const tsSpan = Math.max(maxTs - minTs, 1e-6);
  const xTicks = 8;
  let lastLabelRight = -Infinity;
  ctx.textAlign = "center";
  for (let i = 0; i <= xTicks; i++) {
    const frac = i / xTicks;
    const x = padLeft + frac * (w - padLeft - padRight);
    const ts = minTs + frac * tsSpan;

    ctx.strokeStyle = "#152134";
    ctx.beginPath();
    ctx.moveTo(x, padTop);
    ctx.lineTo(x, h - padBottom);
    ctx.stroke();

    const label = new Date(ts * 1000).toLocaleTimeString([], {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    const width = ctx.measureText(label).width;
    const left = x - width / 2;
    const right = x + width / 2;
    if (left > lastLabelRight + 8 || i === xTicks) {
      ctx.fillStyle = "#8ea0bf";
      ctx.fillText(label, x, h - 20);
      lastLabelRight = right;
    }
  }
  ctx.textAlign = "center";
  ctx.fillStyle = "#8ea0bf";
  ctx.fillText("Time (HH:MM:SS)", padLeft + (w - padLeft - padRight) / 2, h - 4);

  if (strike !== null) {
    const yStrike = padTop + (1 - (strike - yMin) / span) * (h - padTop - padBottom);
    ctx.setLineDash([5, 4]);
    ctx.strokeStyle = "#ffc96a";
    ctx.beginPath();
    ctx.moveTo(padLeft, yStrike);
    ctx.lineTo(w - padRight, yStrike);
    ctx.stroke();
    ctx.setLineDash([]);

    const label = `Strike ${strike.toFixed(2)}`;
    const labelW = ctx.measureText(label).width;
    const labelX = w - padRight - labelW - 8;
    const labelY = Math.max(14, Math.min(h - padBottom - 4, yStrike - 6));
    ctx.fillStyle = "rgba(13, 21, 32, 0.92)";
    ctx.fillRect(labelX - 4, labelY - 10, labelW + 8, 14);
    ctx.fillStyle = "#ffc96a";
    ctx.textAlign = "left";
    ctx.fillText(label, labelX, labelY);
  }

  ctx.beginPath();
  ctx.lineWidth = 2;
  ctx.strokeStyle = "#4bd19a";
  series.forEach((point, i) => {
    const x = padLeft + ((point.ts - minTs) / tsSpan) * (w - padLeft - padRight);
    const y = h - padBottom - ((point.value - yMin) / span) * (h - padTop - padBottom);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function toBrtiSeries(points) {
  return points
    .filter((p) => p.brti !== null && Number.isFinite(Number(p.brti)) && Number.isFinite(Number(p.ts)))
    .map((p) => ({ ts: Number(p.ts), value: Number(p.brti) }))
    .sort((a, b) => a.ts - b.ts);
}

function buildMovingAverageSeries(series, windowSeconds = 60) {
  if (!series.length) {
    return [];
  }

  const maSeries = [];
  let left = 0;
  let sum = 0;

  for (let right = 0; right < series.length; right++) {
    sum += series[right].value;

    while (series[right].ts - series[left].ts > windowSeconds) {
      sum -= series[left].value;
      left += 1;
    }

    const count = right - left + 1;
    maSeries.push({
      ts: series[right].ts,
      value: sum / count,
    });
  }

  return maSeries;
}

function drawBrtiChart(points) {
  const brtiSeries = toBrtiSeries(points);
  drawPriceChart("brtiChart", brtiSeries, "No BRTI history yet");

  const movingAverageSeries = buildMovingAverageSeries(brtiSeries, 60);
  drawPriceChart("movingAvgChart", movingAverageSeries, "No moving-average history yet");
}

async function refreshState() {
  const stateRes = await fetch("/api/state?depth=10");
  const state = await stateRes.json();
  const ob = state.orderbook;

  const summary = document.getElementById("summary");
  summary.innerHTML =
    `<div class="summary-box"><div class="summary-label">Orderbook Status</div><div class="summary-value ${ob.initialized ? "ok" : "warn"}">${ob.initialized ? "Live and updating" : "Waiting for bootstrap"}</div></div>` +
    `<div class="summary-box"><div class="summary-label">Tracked Market</div><div class="summary-value">${ob.market_ticker ?? "n/a"}</div></div>` +
    `<div class="summary-box"><div class="summary-label">Next Sequence Expected</div><div class="summary-value">${ob.expected_seq ?? "n/a"}</div></div>` +
    `<div class="summary-box"><div class="summary-label">Logged Kalshi Events</div><div class="summary-value">${state.ws_log_size}</div></div>`;

  buildBookTable(document.getElementById("yesTable"), ob.yes_bids, ob.yes_asks);
  buildBookTable(document.getElementById("noTable"), ob.no_bids, ob.no_asks);

  const brti = state.brti;
  if (state.suggested_strike != null) {
    window.__suggestedStrike = Number(state.suggested_strike);
  } else {
    window.__suggestedStrike = null;
  }
  document.getElementById("brti").textContent = brti.brti ? `$${Number(brti.brti).toLocaleString()}` : "--";
  document.getElementById("brtiMeta").textContent =
    `Latest depth used: ${brti.depth} BTC | Exchanges included: ${brti.exchanges} | Timestamp: ${new Date((brti.timestamp || 0) * 1000).toISOString()}`;

  const proxy = state.synthetic_settlement_proxy || {};
  const proxyAvg = proxy.average != null ? `$${Number(proxy.average).toLocaleString()}` : "--";
  document.getElementById("settlementProxy").textContent =
    `Synthetic 60s RTI average (proxy): ${proxyAvg} | samples=${proxy.samples ?? 0}`;

  renderPhase3Section(state.pricing, state.microstructure);
  renderInnerCalcJson(state.pricing, state.microstructure);

  const help = document.getElementById("metricHelp");
  const k = state.kalshi_ws_stats || {};
  const b = state.brti_ws_stats || {};
  help.innerHTML =
    "Orderbook Status: whether bootstrap + sequence sync succeeded.<br>" +
    "Tracked Market: exact market ticker currently reconstructed.<br>" +
    "Next Sequence Expected: the next Kalshi delta sequence ID required for in-order updates.<br>" +
    "Logged Kalshi Events: retained raw websocket events for auditing.<br>" +
    "Latest depth used: BRTI utilized depth in BTC for price calculation.<br>" +
    "Exchanges included: number of clean, non-stale exchanges currently used by BRTI.<br><br>" +
    "Synthetic 60s RTI average: rolling average of valid synthetic BRTI prints over the last 60 seconds. This approximates settlement mechanics but is not the official CF value.<br><br>" +
    "<strong>Asian pricer &amp; Realized Vol (below charts)</strong><br>" +
    "P(model): Asian / collapsed-variance estimate from <code>asian_pricer.py</code> using σ from <code>vol_estimator.py</code>. P(book): order-book skew from <code>book_microstructure.py</code>. Bars: green = YES (&gt;50%), red = NO (&lt;50%). Inner JSON: right column → Asian Pricer and Realized Vol Calculations.<br><br>" +
    "<strong>Kalshi message processing counters</strong><br>" +
    `Incoming websocket messages: ${k.total_received ?? "n/a"}<br>` +
    `Orderbook deltas received: ${k.orderbook_delta_received ?? "n/a"}<br>` +
    `Orderbook deltas buffered: ${k.orderbook_delta_buffered ?? "n/a"}<br>` +
    `Orderbook deltas applied: ${k.orderbook_delta_applied ?? "n/a"}<br>` +
    `Orderbook stale deltas ignored: ${k.orderbook_delta_stale_ignored ?? "n/a"}<br>` +
    `Orderbook sequence gaps: ${k.orderbook_delta_seq_gap ?? "n/a"}<br><br>` +
    "<strong>BRTI exchange message processing counters</strong><br>" +
    `Incoming exchange websocket messages: ${b.total_received ?? "n/a"}<br>` +
    `Parsed exchange websocket messages: ${b.total_parsed ?? "n/a"}<br>` +
    `Orderbook updates applied to BRTI books: ${b.book_updates_applied ?? "n/a"}<br>` +
    `Coinbase messages: ${b.coinbase_received ?? "n/a"} | Kraken messages: ${b.kraken_received ?? "n/a"}<br>` +
    `Gemini messages: ${b.gemini_received ?? "n/a"} | Bitstamp messages: ${b.bitstamp_received ?? "n/a"} | Paxos messages: ${b.paxos_received ?? "n/a"}<br>` +
    `Coinbase parsed: ${b.coinbase_parsed ?? "n/a"} | Kraken parsed: ${b.kraken_parsed ?? "n/a"} | Gemini parsed: ${b.gemini_parsed ?? "n/a"}<br>` +
    `Bitstamp parsed: ${b.bitstamp_parsed ?? "n/a"} | Paxos parsed: ${b.paxos_parsed ?? "n/a"}`;
}

async function refreshReconLog() {
  const res = await fetch("/api/reconciliation-log?limit=200");
  const rows = await res.json();
  const lines = rows.map((row) => {
    const ts = new Date(row.ts * 1000).toISOString();
    return `${ts} | breach=${row.breach} | consecutive=${row.consecutive_breaches} | action=${row.action} | metrics=${JSON.stringify(row.metrics)}`;
  });
  const el = document.getElementById("reconLog");
  const wasAtBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
  el.textContent = lines.join("\n");
  if (wasAtBottom) el.scrollTop = el.scrollHeight;
}

async function refreshRawLog() {
  const res = await fetch("/api/ws-log?limit=200");
  const rows = await res.json();
  const lines = rows.map((row) => {
    const ts = new Date(row.ts * 1000).toISOString();
    return `${ts} | type=${row.type} | seq=${row.seq} | status=${row.status} | payload=${JSON.stringify(row.payload)}`;
  });
  const el = document.getElementById("rawLog");
  const wasAtBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
  el.textContent = lines.join("\n");
  if (wasAtBottom) el.scrollTop = el.scrollHeight;
}

async function refreshImpactLog() {
  const res = await fetch("/api/top10-impact?limit=200");
  const rows = await res.json();
  const lines = rows.map((row) => {
    const ts = new Date(row.ts * 1000).toISOString();
    return `${ts} | seq=${row.seq} | top10_changed=${row.changed} | payload=${JSON.stringify(row.payload)}`;
  });
  const el = document.getElementById("impactLog");
  const wasAtBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
  el.textContent = lines.join("\n");
  if (wasAtBottom) el.scrollTop = el.scrollHeight;
}

async function refreshBrtiTicks() {
  const res = await fetch("/api/brti-ticks?limit=200");
  const rows = await res.json();
  const lines = rows.map((row) => {
    const ts = new Date(row.ts * 1000).toISOString();
    return `${ts} | status=${row.status} | brti=${row.brti} | depth=${row.depth} | exchanges=${row.exchanges} | levels=${JSON.stringify(row.levels)}`;
  });
  const el = document.getElementById("brtiTicks");
  const wasAtBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
  el.textContent = lines.join("\n");
  if (wasAtBottom) el.scrollTop = el.scrollHeight;
  drawBrtiChart(rows);
}

async function refreshBrtiRawLog() {
  const res = await fetch("/api/brti-ws-log?limit=200");
  const rows = await res.json();
  const lines = rows.map((row) => {
    const ts = new Date(row.ts * 1000).toISOString();
    return `${ts} | exchange=${row.exchange} | status=${row.status} | type=${row.raw_type} | channel=${row.raw_channel} | event=${row.raw_event}`;
  });
  const el = document.getElementById("brtiRawLog");
  const wasAtBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
  el.textContent = lines.join("\n");
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
