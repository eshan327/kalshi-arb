(function () {
  const {
    fmtQty,
    fmtPriceCent,
    fmtPct,
    vizFillClass,
    vizPctClass,
    humanPricingReason,
  } = window.DashboardFormat;

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
      st.textContent = `Regime: ${pricing.regime ?? "—"} · σ annual ${pricing.sigma_annual}${sigNote} · ${pricing.sigma_samples} ${indexLabel} prints`;
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
      `<div class="summary-box"><div class="summary-label">Spot ${indexLabel}</div><div class="summary-value">${
        pricing.spot_index != null ? "$" + Number(pricing.spot_index).toLocaleString() : "—"
      }</div></div>` +
      `<div class="summary-box" style="grid-column:1/-1"><div class="summary-label">TWAP / window</div><div class="summary-value" style="font-size:12px;line-height:1.4">${twapLine}</div></div>` +
      req;
  }

  window.DashboardRenderers = {
    buildBookTable,
    renderInnerCalcJson,
    renderPhase3Section,
  };
})();
