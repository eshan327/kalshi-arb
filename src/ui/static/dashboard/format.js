(function () {
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

  function humanPricingReason(reason) {
    if (reason === "asset_syncing") {
      return "asset switch in progress (waiting for index feed handoff)";
    }
    if (reason === "no_brti") {
      return "no live index print yet";
    }
    if (reason === "no_close_time") {
      return "market close time unavailable";
    }
    if (reason === "no_strike") {
      return "inferred strike unavailable";
    }
    return String(reason || "unknown");
  }

  function friendlyAssetName(asset) {
    if (asset === "ETH") return "Ethereum";
    if (asset === "BTC") return "Bitcoin";
    return String(asset || "Unknown");
  }

  window.DashboardFormat = {
    fmtQty,
    fmtPriceCent,
    fmtPct,
    vizFillClass,
    vizPctClass,
    humanPricingReason,
    friendlyAssetName,
  };
})();
