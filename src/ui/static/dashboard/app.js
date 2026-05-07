(function () {
  const { bindAssetSelector, syncAssetSelectorFromState } = window.DashboardAssetSelection;
  const {
    buildBookTable,
    renderExecutionState,
    renderInnerCalcJson,
    renderPhase3Section,
    renderRuntimeStats,
  } = window.DashboardRenderers;
  const {
    drawConfidenceHistogram,
    drawEdgeChart,
    drawLatencyHistogram,
    drawMakerTakerChart,
    drawOrderTimingChart,
    drawPnlChart,
    drawRejectionReasonsChart,
    drawSpreadHistogram,
    drawWindowFillRateChart,
    drawWinRateChart,
  } = window.DashboardCharts;
  const {
    refreshExecutionEvents,
    refreshFillEvents,
    refreshReconLog,
    refreshRawLog,
    refreshImpactLog,
    refreshBrtiTicks,
    refreshBrtiRawLog,
  } = window.DashboardLogs;

  let activeLogPanelId = "executionEvents";
  let sessionsVisible = false;
  let latestState = null;
  let latestBrtiRows = [];

  const logRefreshers = {
    rawLog: () => refreshRawLog(200),
    impactLog: () => refreshImpactLog(200),
    reconLog: () => refreshReconLog(200),
    brtiRawLog: () => refreshBrtiRawLog(200),
    brtiTicks: () =>
      refreshBrtiTicks(200, {
        onRows: (rows) => {
          latestBrtiRows = Array.isArray(rows) ? rows : [];
        },
      }),
    executionEvents: () => refreshExecutionEvents(200),
    fillEvents: () => refreshFillEvents(200),
    innerCalcJson: async () => {},
  };

  function isDocumentVisible() {
    return document.visibilityState === "visible";
  }

  function isLogPanelActive(contentElementId) {
    return activeLogPanelId === contentElementId;
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

  function setActiveLogPanel(targetId) {
    activeLogPanelId = targetId;

    document.querySelectorAll(".log-tab").forEach((tab) => {
      tab.classList.toggle("active", tab.dataset.logTarget === targetId);
    });

    document.querySelectorAll(".log-panel").forEach((panel) => {
      panel.classList.toggle("active", panel.id === `panel_${targetId}`);
    });
  }

  function bindVerificationTabs() {
    const tabsRoot = document.getElementById("verificationTabs");
    if (!tabsRoot) {
      return;
    }

    tabsRoot.querySelectorAll(".log-tab").forEach((button) => {
      button.addEventListener("click", () => {
        const target = button.dataset.logTarget;
        if (!target) {
          return;
        }

        setActiveLogPanel(target);
        const refresh = logRefreshers[target];
        if (typeof refresh === "function") {
          refresh().catch(() => {});
        }
      });
    });
  }

  async function refreshExportSessions(limit = 20) {
    const response = await fetch(`/api/export-sessions?limit=${Math.max(1, Number(limit) || 20)}`);
    const payload = await response.json();
    const sessions = Array.isArray(payload.sessions) ? payload.sessions : [];

    const list = document.getElementById("exportSessionsList");
    if (!list) {
      return;
    }

    if (!sessions.length) {
      list.innerHTML = '<div class="session-item">No exported sessions yet.</div>';
      return;
    }

    list.innerHTML = sessions
      .map((session) => {
        const meta = session.metadata || {};
        const summary = session.summary || {};
        const winRate = summary.win_rate == null ? "--" : `${(Number(summary.win_rate) * 100).toFixed(2)}%`;
        return `
          <div class="session-item">
            <div>${session.session_id || "unknown"}</div>
            <div class="meta">mode=${summary.mode || "--"} | pnl=${summary.pnl_dollars || "--"} | win_rate=${winRate} | fills=${summary.fills_total || 0}</div>
            <div class="meta">start=${meta.start_ts || "--"} | end=${meta.end_ts || "--"}</div>
          </div>
        `;
      })
      .join("");
  }

  function bindExportControls() {
    const exportBtn = document.getElementById("exportSessionBtn");
    const toggleBtn = document.getElementById("toggleSessionsBtn");
    const statusEl = document.getElementById("exportStatus");
    const panel = document.getElementById("exportSessionsPanel");

    if (exportBtn) {
      exportBtn.addEventListener("click", async () => {
        if (statusEl) {
          statusEl.textContent = "Exporting session artifacts...";
          statusEl.style.color = "var(--warn)";
        }

        try {
          const response = await fetch("/api/export-session", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ reason: "manual_ui" }),
          });
          const payload = await response.json();

          if (!response.ok || !payload.ok) {
            throw new Error(payload.error || "Export failed.");
          }

          if (statusEl) {
            statusEl.textContent = `Export complete: ${payload.session_id} (${payload.path})`;
            statusEl.style.color = "var(--ok)";
          }

          if (sessionsVisible) {
            await refreshExportSessions(30);
          }
        } catch (err) {
          if (statusEl) {
            statusEl.textContent = String(err?.message || err || "Export failed.");
            statusEl.style.color = "var(--danger)";
          }
        }
      });
    }

    if (toggleBtn && panel) {
      toggleBtn.addEventListener("click", async () => {
        sessionsVisible = !sessionsVisible;
        panel.classList.toggle("visible", sessionsVisible);
        toggleBtn.textContent = sessionsVisible ? "Hide Exported Sessions" : "Show Exported Sessions";
        if (sessionsVisible) {
          await refreshExportSessions(30);
        }
      });
    }
  }

  function renderSummary(state) {
    const ob = state.orderbook;
    const summary = document.getElementById("summary");
    if (!summary) {
      return;
    }

    summary.innerHTML =
      `<div class="summary-box"><div class="summary-label">Orderbook Status</div><div class="summary-value ${ob.initialized ? "ok" : "warn"}">${ob.initialized ? "Live and updating" : "Waiting for bootstrap"}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Tracked Market</div><div class="summary-value">${ob.market_ticker ?? "n/a"}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Next Sequence Expected</div><div class="summary-value">${ob.expected_seq ?? "n/a"}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Logged Kalshi Events</div><div class="summary-value">${state.ws_log_size}</div></div>`;
  }

  function renderState(state) {
    const ob = state.orderbook;
    const asset = state.asset || "BTC";
    const indexLabel = state.index_label || "Index";
    const settlementWindowSeconds = Number(state.settlement_window_seconds || 60);
    window.__settlementWindowSeconds = settlementWindowSeconds;

    bindAssetSelector();
    syncAssetSelectorFromState(state);

    renderSummary(state);
    buildBookTable(document.getElementById("yesTable"), ob.yes_bids, ob.yes_asks);
    buildBookTable(document.getElementById("noTable"), ob.no_bids, ob.no_asks);

    const brti = state.brti;
    if (state.suggested_strike != null) {
      window.__suggestedStrike = Number(state.suggested_strike);
    } else {
      window.__suggestedStrike = null;
    }

    const brtiEl = document.getElementById("brti");
    if (brtiEl) {
      brtiEl.textContent = brti.brti ? `$${Number(brti.brti).toLocaleString()}` : "--";
    }

    const brtiMeta = document.getElementById("brtiMeta");
    if (brtiMeta) {
      brtiMeta.textContent = `Latest depth used: ${brti.depth} ${asset} | Exchanges included: ${brti.exchanges} | Timestamp: ${new Date((brti.timestamp || 0) * 1000).toISOString()}`;
      if (state.asset_syncing) {
        brtiMeta.textContent += ` | Feed syncing: ${state.feed_asset || "?"} -> ${asset}`;
      }
    }

    const proxy = state.synthetic_settlement_proxy || {};
    const proxyAvg = proxy.average != null ? `$${Number(proxy.average).toLocaleString()}` : "--";
    const proxyMethod = proxy.method === "discrete_1s_forward_fill" ? "1Hz forward-fill" : "rolling";
    const settlementProxy = document.getElementById("settlementProxy");
    if (settlementProxy) {
      settlementProxy.textContent = `Synthetic ${settlementWindowSeconds}s ${indexLabel} average (${proxyMethod} proxy): ${proxyAvg} | samples=${proxy.samples ?? 0}`;
    }

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
    renderExecutionState(state.execution_state);
    renderRuntimeStats(state.runtime_stats);
    renderInnerCalcJson(state.pricing, state.microstructure);

    const runtimeStats = state.runtime_stats || {};
    drawPnlChart(runtimeStats.pnl_curve || []);
    drawWinRateChart(runtimeStats.win_rate_curve || []);
    drawEdgeChart(runtimeStats.edge_curve || []);
    drawConfidenceHistogram(runtimeStats.confidence_bins || {});
    drawOrderTimingChart(runtimeStats.order_timing_events || [], runtimeStats.window_seconds || 900);
    drawRejectionReasonsChart(runtimeStats.rejection_reasons || {});
    drawMakerTakerChart(runtimeStats.maker_fills_window || 0, runtimeStats.taker_fills_window || 0);
    drawLatencyHistogram(runtimeStats.fill_latency_bins || {});
    drawSpreadHistogram(runtimeStats.fill_spread_bins || {});
    drawWindowFillRateChart(runtimeStats.window_fill_rate_curve || []);

    const help = document.getElementById("metricHelp");
    const k = state.kalshi_ws_stats || {};
    const b = state.brti_ws_stats || {};
    if (help) {
      help.innerHTML =
        `Orderbook Status: whether bootstrap + sequence sync succeeded.<br>` +
        `Tracked Market: exact market ticker currently reconstructed.<br>` +
        `Next Sequence Expected: the next Kalshi delta sequence ID required for in-order updates.<br>` +
        `Logged Kalshi Events: retained websocket events for audit.<br>` +
        `Incoming websocket messages: ${k.total_received ?? "n/a"} | ticker messages: ${k.ticker_received ?? "n/a"}<br>` +
        `${indexLabel} exchange messages: ${b.total_received ?? "n/a"} | parsed: ${b.total_parsed ?? "n/a"}<br>` +
        "Execution panels track mode state, exposure, realized PnL, confidence distribution, timing behavior, rejection diagnostics, fill quality, and per-window participation.";
    }
  }

  async function refreshState() {
    const response = await fetch("/api/state?depth=10");
    const state = await response.json();
    latestState = state;
    renderState(state);
  }

  function bindResizeRedraw() {
    let resizeTimer = null;
    window.addEventListener("resize", () => {
      if (resizeTimer != null) {
        window.clearTimeout(resizeTimer);
      }
      resizeTimer = window.setTimeout(() => {
        if (latestState) {
          renderState(latestState);
        }
      }, 120);
    });
  }

  bindVerificationTabs();
  bindExportControls();
  bindResizeRedraw();

  [
    {
      task: refreshState,
      options: { visibleMs: 700, hiddenMs: 3200, runWhenHidden: false, immediate: true },
    },
    {
      task: () =>
        refreshBrtiTicks(200, {
          onRows: (rows) => {
            latestBrtiRows = Array.isArray(rows) ? rows : [];
          },
        }),
      options: { visibleMs: 1100, hiddenMs: 6000, runWhenHidden: false, immediate: true },
    },
    {
      task: async () => {
        const fn = logRefreshers[activeLogPanelId];
        if (typeof fn === "function") {
          await fn();
        }
      },
      options: {
        visibleMs: 1300,
        hiddenMs: 7000,
        runWhenHidden: false,
        onlyWhen: () => isLogPanelActive(activeLogPanelId),
        immediate: true,
      },
    },
  ].forEach(({ task, options }) => createAdaptivePoller(task, options));
})();
