(function () {
  const { bindAssetSelector, syncAssetSelectorFromState } = window.DashboardAssetSelection;
  const { buildBookTable, renderInnerCalcJson, renderPhase3Section } = window.DashboardRenderers;
  const {
    refreshReconLog,
    refreshRawLog,
    refreshImpactLog,
    refreshBrtiTicks,
    refreshBrtiRawLog,
  } = window.DashboardLogs;

  function isDocumentVisible() {
    return document.visibilityState === "visible";
  }

  function isDetailsPanelOpen(contentElementId) {
    const contentEl = document.getElementById(contentElementId);
    const detailsEl = contentEl ? contentEl.closest("details") : null;
    return Boolean(detailsEl && detailsEl.open);
  }

  function createAdaptivePoller(task, options) {
    let stopped = false;
    let inFlight = false;
    let failures = 0;

    const visibleMs = Math.max(100, Number(options?.visibleMs || 1000));
    const hiddenMs = Math.max(visibleMs, Number(options?.hiddenMs || visibleMs));
    const runWhenHidden = Boolean(options?.runWhenHidden);
    const onlyWhen = typeof options?.onlyWhen === "function" ? options.onlyWhen : null;

    async function tick() {
      if (stopped) {
        return;
      }

      const visible = isDocumentVisible();
      const shouldRunByVisibility = visible || runWhenHidden;
      const shouldRunByPanel = onlyWhen ? Boolean(onlyWhen()) : true;
      const shouldRun = shouldRunByVisibility && shouldRunByPanel;

      if (shouldRun && !inFlight) {
        inFlight = true;
        try {
          await task();
          failures = 0;
        } catch {
          failures = Math.min(failures + 1, 6);
        } finally {
          inFlight = false;
        }
      }

      const baseInterval = visible ? visibleMs : hiddenMs;
      const errorBackoff = Math.min(1 + failures * 0.5, 4);
      const nextDelay = Math.max(100, Math.round(baseInterval * errorBackoff));
      window.setTimeout(tick, nextDelay);
    }

    const startImmediately = options?.immediate !== false;
    if (startImmediately) {
      tick();
    } else {
      window.setTimeout(tick, visibleMs);
    }

    return () => {
      stopped = true;
    };
  }

  function bindDetailsToggleRefreshes() {
    const onOpenRefreshMap = {
      rawLog: () => refreshRawLog(200),
      impactLog: () => refreshImpactLog(200),
      reconLog: () => refreshReconLog(200),
      brtiRawLog: () => refreshBrtiRawLog(200),
      brtiTicks: () => refreshBrtiTicks(200),
    };

    Object.entries(onOpenRefreshMap).forEach(([contentId, refreshFn]) => {
      const contentEl = document.getElementById(contentId);
      const detailsEl = contentEl ? contentEl.closest("details") : null;
      if (!detailsEl || detailsEl.dataset.refreshBound === "1") {
        return;
      }

      detailsEl.addEventListener("toggle", () => {
        if (detailsEl.open) {
          refreshFn().catch(() => {});
        }
      });
      detailsEl.dataset.refreshBound = "1";
    });
  }

  async function refreshState() {
    const stateRes = await fetch("/api/state?depth=10");
    const state = await stateRes.json();
    const ob = state.orderbook;
    const asset = state.asset || "BTC";
    const indexLabel = state.index_label || "Index";
    const settlementWindowSeconds = Number(state.settlement_window_seconds || 60);
    window.__settlementWindowSeconds = settlementWindowSeconds;

    bindAssetSelector();
    syncAssetSelectorFromState(state);

    const summary = document.getElementById("summary");
    summary.innerHTML =
      `<div class="summary-box"><div class="summary-label">Orderbook Status</div><div class="summary-value ${ob.initialized ? "ok" : "warn"}">${ob.initialized ? "Live and updating" : "Waiting for bootstrap"}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Tracked ${asset} Market</div><div class="summary-value">${ob.market_ticker ?? "n/a"}</div></div>` +
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
      `Latest depth used: ${brti.depth} ${asset} | Exchanges included: ${brti.exchanges} | Timestamp: ${new Date((brti.timestamp || 0) * 1000).toISOString()}`;

    const proxy = state.synthetic_settlement_proxy || {};
    const proxyAvg = proxy.average != null ? `$${Number(proxy.average).toLocaleString()}` : "--";
    const proxyMethod = proxy.method === "discrete_1s_forward_fill" ? "1Hz forward-fill" : "rolling";
    document.getElementById("settlementProxy").textContent =
      `Synthetic ${settlementWindowSeconds}s ${indexLabel} average (${proxyMethod} proxy): ${proxyAvg} | samples=${proxy.samples ?? 0}`;

    const syncSuffix = state.asset_syncing ? ` | Feed syncing: ${state.feed_asset || "?"} -> ${asset}` : "";
    document.getElementById("brtiMeta").textContent += syncSuffix;

    const settlementRuleEl = document.getElementById("settlementRuleText");
    if (settlementRuleEl) {
      const benchmark = state.settlement_benchmark_label || "benchmark index";
      const rule = state.settlement_rule_text || "YES resolves when final-minute average is at or above strike.";
      settlementRuleEl.textContent = `Settlement basis: ${benchmark} · ${rule}`;
    }

    renderPhase3Section(state.pricing, state.microstructure, {
      indexLabel,
      settlementWindowSeconds,
    });
    renderInnerCalcJson(state.pricing, state.microstructure);

    const help = document.getElementById("metricHelp");
    const k = state.kalshi_ws_stats || {};
    const b = state.brti_ws_stats || {};
    help.innerHTML =
      "Orderbook Status: whether bootstrap + sequence sync succeeded.<br>" +
      "Tracked Market: exact market ticker currently reconstructed.<br>" +
      "Next Sequence Expected: the next Kalshi delta sequence ID required for in-order updates.<br>" +
      "Logged Kalshi Events: retained raw websocket events for auditing.<br>" +
      `Latest depth used: ${indexLabel} utilized depth in ${asset} for price calculation.<br>` +
      `Exchanges included: number of clean, non-stale exchanges currently used by ${indexLabel}.<br><br>` +
      `Synthetic ${settlementWindowSeconds}s RTI average: 1Hz forward-filled average of synthetic ${indexLabel} prints over the last ${settlementWindowSeconds} seconds. This approximates settlement mechanics but is not the official CF value.<br><br>` +
      "<strong>Asian pricer &amp; Realized Vol (below charts)</strong><br>" +
      "P(model): Asian / collapsed-variance estimate from <code>asian_pricer.py</code> using σ from <code>vol_estimator.py</code>. P(book): order-book skew from <code>book_microstructure.py</code>. Bars: green = YES (&gt;50%), red = NO (&lt;50%). Inner JSON: right column → Asian Pricer and Realized Vol Calculations.<br><br>" +
      "<strong>Kalshi message processing counters</strong><br>" +
      `Incoming websocket messages: ${k.total_received ?? "n/a"}<br>` +
      `Orderbook deltas received: ${k.orderbook_delta_received ?? "n/a"}<br>` +
      `Orderbook deltas buffered: ${k.orderbook_delta_buffered ?? "n/a"}<br>` +
      `Orderbook deltas applied: ${k.orderbook_delta_applied ?? "n/a"}<br>` +
      `Orderbook stale deltas ignored: ${k.orderbook_delta_stale_ignored ?? "n/a"}<br>` +
      `Orderbook sequence gaps: ${k.orderbook_delta_seq_gap ?? "n/a"}<br><br>` +
      `<strong>${indexLabel} exchange message processing counters</strong><br>` +
      `Incoming exchange websocket messages: ${b.total_received ?? "n/a"}<br>` +
      `Parsed exchange websocket messages: ${b.total_parsed ?? "n/a"}<br>` +
      `Orderbook updates applied to ${indexLabel} books: ${b.book_updates_applied ?? "n/a"}<br>` +
      `Coinbase messages: ${b.coinbase_received ?? "n/a"} | Kraken messages: ${b.kraken_received ?? "n/a"}<br>` +
      `Gemini messages: ${b.gemini_received ?? "n/a"} | Bitstamp messages: ${b.bitstamp_received ?? "n/a"} | Paxos messages: ${b.paxos_received ?? "n/a"}<br>` +
      `Coinbase parsed: ${b.coinbase_parsed ?? "n/a"} | Kraken parsed: ${b.kraken_parsed ?? "n/a"} | Gemini parsed: ${b.gemini_parsed ?? "n/a"}<br>` +
      `Bitstamp parsed: ${b.bitstamp_parsed ?? "n/a"} | Paxos parsed: ${b.paxos_parsed ?? "n/a"}`;
  }

  bindDetailsToggleRefreshes();

  createAdaptivePoller(() => refreshState(), {
    visibleMs: 600,
    hiddenMs: 3000,
    runWhenHidden: false,
    immediate: true,
  });

  createAdaptivePoller(() => refreshBrtiTicks(isDetailsPanelOpen("brtiTicks") ? 200 : 120), {
    visibleMs: 1200,
    hiddenMs: 6000,
    runWhenHidden: false,
    immediate: true,
  });

  createAdaptivePoller(() => refreshRawLog(200), {
    visibleMs: 1400,
    hiddenMs: 7000,
    runWhenHidden: false,
    onlyWhen: () => isDetailsPanelOpen("rawLog"),
    immediate: false,
  });

  createAdaptivePoller(() => refreshImpactLog(200), {
    visibleMs: 1400,
    hiddenMs: 7000,
    runWhenHidden: false,
    onlyWhen: () => isDetailsPanelOpen("impactLog"),
    immediate: false,
  });

  createAdaptivePoller(() => refreshBrtiRawLog(200), {
    visibleMs: 1600,
    hiddenMs: 7000,
    runWhenHidden: false,
    onlyWhen: () => isDetailsPanelOpen("brtiRawLog"),
    immediate: false,
  });

  createAdaptivePoller(() => refreshReconLog(200), {
    visibleMs: 1800,
    hiddenMs: 7000,
    runWhenHidden: false,
    onlyWhen: () => isDetailsPanelOpen("reconLog"),
    immediate: false,
  });
})();
