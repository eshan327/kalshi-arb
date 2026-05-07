(function () {
  const {
    fmtQty,
    fmtPriceCent,
    fmtPct,
    vizFillClass,
    vizPctClass,
    humanPricingReason,
  } = window.DashboardFormat;

  function fmtMoneyFromCents(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "—";
    return `$${(n / 100).toFixed(2)}`;
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

  function renderPhase3Section(pricing, micro, stateMeta) {
    const barM = document.getElementById("pModelBar");
    const barB = document.getElementById("pBookBar");
    const labM = document.getElementById("pModelPctLabel");
    const labB = document.getElementById("pBookPctLabel");
    const grid = document.getElementById("phase3Grid");
    const st = document.getElementById("phase3Status");
    const indexLabel = stateMeta?.indexLabel || "Index";
    const settlementWindowSeconds = Number(stateMeta?.settlementWindowSeconds || 60);
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
          ? `P(model) unavailable: ${humanPricingReason(pricing.reason)}`
          : "Waiting for index, inferred strike, and market close time...";
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

    const pBookQuality = micro && Number.isFinite(Number(micro.p_book_quality)) ? Number(micro.p_book_quality) : null;

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
      const qualityText = pBookQuality == null ? "n/a" : `${(pBookQuality * 100).toFixed(1)}%`;
      st.textContent = `Regime: ${pricing.regime ?? "—"} · σ annual ${pricing.sigma_annual}${sigNote} · ${pricing.sigma_samples} ${indexLabel} prints · P(book) quality ${qualityText}`;
    }

    const req =
      pricing.twap_required_avg != null
        ? `<div class="summary-box"><div class="summary-label">Req. avg (rest)</div><div class="summary-value">$${Number(
            pricing.twap_required_avg,
          ).toLocaleString()}</div></div>`
        : "";

    const twapLine =
      pricing.twap_partial_avg != null
        ? `Partial avg $${Number(pricing.twap_partial_avg).toLocaleString()} · ${pricing.twap_seconds_elapsed}/${settlementWindowSeconds} s`
        : Number(pricing.seconds_to_expiry) > settlementWindowSeconds
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
      `<div class="summary-box"><div class="summary-label">P(book) Quality</div><div class="summary-value">${
        pBookQuality == null ? "—" : `${(pBookQuality * 100).toFixed(1)}%`
      }</div></div>` +
      `<div class="summary-box"><div class="summary-label">Spot ${indexLabel}</div><div class="summary-value">${
        pricing.spot_index != null ? "$" + Number(pricing.spot_index).toLocaleString() : "—"
      }</div></div>` +
      `<div class="summary-box" style="grid-column:1/-1"><div class="summary-label">TWAP / window</div><div class="summary-value" style="font-size:12px;line-height:1.4">${twapLine}</div></div>` +
      req;
  }

  function renderExecutionState(executionState) {
    const grid = document.getElementById("executionStatusGrid");
    if (!grid) {
      return;
    }

    const state = executionState || {};
    const enabled = Boolean(state.enabled);
    const status = state.status || "—";
    const mode = state.mode || "—";
    const ticker = state.current_market_ticker || "—";
    const openOrders = Number(state.open_orders || 0);
    const position = Number(state.market_position_contracts || 0);
    const dailyPnl = Number(state.daily_realized_pnl_cents || 0);
    const balance = Number(state.available_balance_cents || 0);
    const paper = state.paper_account || null;
    const paperProfile = state.paper_profile || {};
    const profileOverrides = paperProfile.policy_overrides || {};
    const paperWindow = state.paper_window || {};

    const profileCards =
      mode === "paper"
        ? `<div class="summary-box"><div class="summary-label">Paper Profile</div><div class="summary-value">${paperProfile.profile_name || "default"}</div></div>` +
          `<div class="summary-box"><div class="summary-label">Min Edge / Conf / Timing</div><div class="summary-value">${Number(profileOverrides.min_edge_cents ?? 0).toFixed(2)}c / ${Number(profileOverrides.min_confidence ?? 0).toFixed(2)} / ${Number(profileOverrides.min_timing_score ?? 0).toFixed(2)}</div></div>` +
          `<div class="summary-box"><div class="summary-label">Fallback Trigger</div><div class="summary-value">${Number(paperProfile.fallback_trigger_sec ?? 0).toFixed(1)}s · force=${paperProfile.force_fill_per_window ? "on" : "off"}</div></div>` +
          `<div class="summary-box"><div class="summary-label">Window</div><div class="summary-value">${paperWindow.window_id || "—"}</div></div>` +
          `<div class="summary-box"><div class="summary-label">Window Attempts</div><div class="summary-value">${paperWindow.attempts ?? 0} (fallback ${paperWindow.fallback_attempts ?? 0})</div></div>` +
          `<div class="summary-box"><div class="summary-label">Window Fill</div><div class="summary-value ${paperWindow.has_fill ? "ok" : "warn"}">${paperWindow.has_fill ? "yes" : "no"}</div></div>`
        : "";

    const paperCards = paper
      ? `<div class="summary-box"><div class="summary-label">Paper Cash</div><div class="summary-value">${fmtMoneyFromCents(paper.cash_cents)}</div></div>` +
        `<div class="summary-box"><div class="summary-label">Paper Equity</div><div class="summary-value">${fmtMoneyFromCents(paper.equity_cents)}</div></div>` +
        `<div class="summary-box"><div class="summary-label">Paper Unrealized</div><div class="summary-value ${Number(paper.unrealized_pnl_cents || 0) >= 0 ? "ok" : "warn"}">${fmtMoneyFromCents(paper.unrealized_pnl_cents)}</div></div>` +
        `<div class="summary-box"><div class="summary-label">Paper Positions</div><div class="summary-value">${Array.isArray(paper.positions) ? paper.positions.length : 0}</div></div>`
      : "";

    grid.innerHTML =
      `<div class="summary-box"><div class="summary-label">Execution</div><div class="summary-value ${enabled ? "ok" : "warn"}">${enabled ? "Enabled" : "Disabled"}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Mode</div><div class="summary-value">${mode}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Loop Status</div><div class="summary-value">${status}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Market</div><div class="summary-value">${ticker}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Open Orders</div><div class="summary-value">${openOrders}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Position (contracts)</div><div class="summary-value">${position}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Daily Realized PnL</div><div class="summary-value ${dailyPnl >= 0 ? "ok" : "warn"}">${fmtMoneyFromCents(dailyPnl)}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Available Balance</div><div class="summary-value">${fmtMoneyFromCents(balance)}</div></div>` +
      profileCards +
      paperCards;
  }

  function renderRuntimeStats(stats) {
    const grid = document.getElementById("runtimeStatsGrid");
    if (!grid) {
      return;
    }

    const s = stats || {};
    const paper = s.paper || null;
    const winRate = s.win_rate == null ? "—" : fmtPct(Number(s.win_rate) * 100, 2);
    const rejectionReasons = s.rejection_reasons || {};
    const rejectionCount = Object.values(rejectionReasons).reduce((acc, raw) => acc + (Number(raw) || 0), 0);
    const makerCount = Number(s.maker_fills_window || 0);
    const takerCount = Number(s.taker_fills_window || 0);
    const fallbackCount = Number(s.fallback_fills_window || 0);
    const windowsSettled = Number(s.window_count_settled || 0);
    const windowsWithFill = Number(s.window_count_with_fill || 0);
    const windowFillRate =
      windowsSettled > 0 ? `${((windowsWithFill / windowsSettled) * 100).toFixed(1)}%` : "—";

    const paperCards = paper
      ? `<div class="summary-box"><div class="summary-label">Paper Unrealized PnL</div><div class="summary-value ${Number(paper.unrealized_pnl_cents || 0) >= 0 ? "ok" : "warn"}">${fmtMoneyFromCents(paper.unrealized_pnl_cents)}</div></div>` +
        `<div class="summary-box"><div class="summary-label">Paper Settled Trades</div><div class="summary-value">${paper.settled_trades ?? 0}</div></div>` +
        `<div class="summary-box"><div class="summary-label">Paper Win Rate</div><div class="summary-value">${paper.win_rate == null ? "—" : fmtPct(Number(paper.win_rate) * 100, 2)}</div></div>`
      : "";

    grid.innerHTML =
      `<div class="summary-box"><div class="summary-label">Total Realized PnL</div><div class="summary-value ${Number(s.pnl_cents || 0) >= 0 ? "ok" : "warn"}">${fmtMoneyFromCents(s.pnl_cents)}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Win Rate</div><div class="summary-value">${winRate}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Settled Trades</div><div class="summary-value">${s.settled_trades ?? 0}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Fills (window)</div><div class="summary-value">${s.fills_recent_window ?? 0}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Maker / Taker (window)</div><div class="summary-value">${makerCount} / ${takerCount}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Fallback Fills (window)</div><div class="summary-value">${fallbackCount}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Policy Rejections</div><div class="summary-value">${rejectionCount}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Window Fill Rate</div><div class="summary-value">${windowFillRate}</div></div>` +
      `<div class="summary-box"><div class="summary-label">Edge Captured</div><div class="summary-value">${Number(s.edge_captured_cents || 0).toFixed(2)}c</div></div>` +
      `<div class="summary-box"><div class="summary-label">Daily Realized PnL</div><div class="summary-value ${Number(s.daily_pnl_cents || 0) >= 0 ? "ok" : "warn"}">${fmtMoneyFromCents(s.daily_pnl_cents)}</div></div>` +
      paperCards;
  }

  window.DashboardRenderers = {
    buildBookTable,
    renderInnerCalcJson,
    renderPhase3Section,
    renderExecutionState,
    renderRuntimeStats,
  };
})();
