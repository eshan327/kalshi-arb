(function () {
  const { drawBrtiChart } = window.DashboardCharts;

  function asPositiveLimit(limit, fallback) {
    const parsed = Number(limit);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      return fallback;
    }
    return Math.floor(parsed);
  }

  function toIsoTimestamp(tsSeconds) {
    return new Date(Number(tsSeconds || 0) * 1000).toISOString();
  }

  function renderLogLines(elementId, lines) {
    const el = document.getElementById(elementId);
    if (!el) {
      return;
    }

    const wasAtBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
    el.textContent = lines.join("\n");
    if (wasAtBottom) {
      el.scrollTop = el.scrollHeight;
    }
  }

  async function refreshLogStream({ endpoint, limit, elementId, formatRow, onRows }) {
    const safeLimit = asPositiveLimit(limit, 200);
    const res = await fetch(`/api/${endpoint}?limit=${safeLimit}`);
    const rows = await res.json();
    const lines = rows.map((row) => formatRow(row, toIsoTimestamp(row.ts)));
    renderLogLines(elementId, lines);

    if (typeof onRows === "function") {
      onRows(rows);
    }
  }

  async function refreshReconLog(limit = 200) {
    await refreshLogStream({
      endpoint: "reconciliation-log",
      limit,
      elementId: "reconLog",
      formatRow: (row, ts) => {
        return `${ts} | breach=${row.breach} | consecutive=${row.consecutive_breaches} | action=${row.action} | metrics=${JSON.stringify(row.metrics)}`;
      },
    });
  }

  async function refreshRawLog(limit = 200) {
    await refreshLogStream({
      endpoint: "ws-log",
      limit,
      elementId: "rawLog",
      formatRow: (row, ts) => {
        return `${ts} | type=${row.type} | seq=${row.seq} | status=${row.status} | payload=${JSON.stringify(row.payload)}`;
      },
    });
  }

  async function refreshImpactLog(limit = 200) {
    await refreshLogStream({
      endpoint: "top10-impact",
      limit,
      elementId: "impactLog",
      formatRow: (row, ts) => {
        return `${ts} | seq=${row.seq} | top10_changed=${row.changed} | payload=${JSON.stringify(row.payload)}`;
      },
    });
  }

  async function refreshBrtiTicks(limit = 200, options = {}) {
    await refreshLogStream({
      endpoint: "brti-ticks",
      limit,
      elementId: "brtiTicks",
      formatRow: (row, ts) => {
        return `${ts} | status=${row.status} | brti=${row.brti} | depth=${row.depth} | exchanges=${row.exchanges} | levels=${JSON.stringify(row.levels)}`;
      },
      onRows: (rows) => {
        drawBrtiChart(rows, Number(window.__settlementWindowSeconds || 60));
        if (typeof options.onRows === "function") {
          options.onRows(rows);
        }
      },
    });
  }

  async function refreshBrtiRawLog(limit = 200) {
    await refreshLogStream({
      endpoint: "brti-ws-log",
      limit,
      elementId: "brtiRawLog",
      formatRow: (row, ts) => {
        return `${ts} | exchange=${row.exchange} | status=${row.status} | type=${row.raw_type} | channel=${row.raw_channel} | event=${row.raw_event}`;
      },
    });
  }

  async function refreshExecutionEvents(limit = 200) {
    await refreshLogStream({
      endpoint: "execution-events",
      limit,
      elementId: "executionEvents",
      formatRow: (row, ts) => {
        return `${ts} | kind=${row.kind} | reason=${row.reason || ""} | side=${row.side || ""} | qty=${row.count || ""} | px=${row.quote_price_cents || ""} | edge=${row.edge_cents || ""}`;
      },
    });
  }

  async function refreshFillEvents(limit = 200) {
    await refreshLogStream({
      endpoint: "fill-events",
      limit,
      elementId: "fillEvents",
      formatRow: (row, ts) => {
        return `${ts} | fill=${row.fill_id || ""} | order=${row.order_id || ""} | ${row.action || ""}_${row.side || ""} | qty=${row.count || ""} | yes=${row.yes_price || ""} | no=${row.no_price || ""} | edge=${row.expected_edge_cents || ""}`;
      },
    });
  }

  window.DashboardLogs = {
    refreshReconLog,
    refreshRawLog,
    refreshImpactLog,
    refreshBrtiTicks,
    refreshBrtiRawLog,
    refreshExecutionEvents,
    refreshFillEvents,
  };
})();
