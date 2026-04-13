(function () {
  const { drawBrtiChart } = window.DashboardCharts;

  function asPositiveLimit(limit, fallback) {
    const parsed = Number(limit);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      return fallback;
    }
    return Math.floor(parsed);
  }

  async function refreshReconLog(limit = 200) {
    const safeLimit = asPositiveLimit(limit, 200);
    const res = await fetch(`/api/reconciliation-log?limit=${safeLimit}`);
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

  async function refreshRawLog(limit = 200) {
    const safeLimit = asPositiveLimit(limit, 200);
    const res = await fetch(`/api/ws-log?limit=${safeLimit}`);
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

  async function refreshImpactLog(limit = 200) {
    const safeLimit = asPositiveLimit(limit, 200);
    const res = await fetch(`/api/top10-impact?limit=${safeLimit}`);
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

  async function refreshBrtiTicks(limit = 200) {
    const safeLimit = asPositiveLimit(limit, 200);
    const res = await fetch(`/api/brti-ticks?limit=${safeLimit}`);
    const rows = await res.json();
    const lines = rows.map((row) => {
      const ts = new Date(row.ts * 1000).toISOString();
      return `${ts} | status=${row.status} | brti=${row.brti} | depth=${row.depth} | exchanges=${row.exchanges} | levels=${JSON.stringify(row.levels)}`;
    });
    const el = document.getElementById("brtiTicks");
    const wasAtBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 10;
    el.textContent = lines.join("\n");
    if (wasAtBottom) el.scrollTop = el.scrollHeight;
    drawBrtiChart(rows, Number(window.__settlementWindowSeconds || 60));
  }

  async function refreshBrtiRawLog(limit = 200) {
    const safeLimit = asPositiveLimit(limit, 200);
    const res = await fetch(`/api/brti-ws-log?limit=${safeLimit}`);
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

  window.DashboardLogs = {
    refreshReconLog,
    refreshRawLog,
    refreshImpactLog,
    refreshBrtiTicks,
    refreshBrtiRawLog,
  };
})();
