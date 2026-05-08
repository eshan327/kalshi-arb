(function () {
  function byId(id) {
    return document.getElementById(id);
  }

  function asNumber(value, fallback) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) {
      return fallback;
    }
    return parsed;
  }

  function setStatus(message, isError) {
    const el = byId("settingsStatus");
    if (!el) {
      return;
    }
    el.textContent = String(message || "");
    el.style.color = isError ? "#ffcf72" : "#93a6c4";
  }

  function setActiveTab(tabName) {
    const settingsBtn = byId("settingsTabBtn");
    const simulationBtn = byId("simulationTabBtn");
    const settingsPanel = byId("settingsPanel");
    const simulationPanel = byId("simulationPanel");

    const showSettings = tabName !== "simulation";

    if (settingsBtn) {
      settingsBtn.classList.toggle("active", showSettings);
    }
    if (simulationBtn) {
      simulationBtn.classList.toggle("active", !showSettings);
    }
    if (settingsPanel) {
      settingsPanel.style.display = showSettings ? "block" : "none";
    }
    if (simulationPanel) {
      simulationPanel.style.display = showSettings ? "none" : "block";
    }
  }

  function applySettingsToForm(settings) {
    if (!settings) {
      return;
    }

    const mode = byId("settingExecutionMode");
    const minEdge = byId("settingMinEdge");
    const slippage = byId("settingSlippage");
    const tradePct = byId("settingTradePct");
    const maxPosition = byId("settingMaxPosition");
    const volOverride = byId("settingVolOverride");
    const levy = byId("settingLevyResponsiveness");
    const pbookGate = byId("settingPbookGate");

    if (mode) mode.value = settings.execution_mode || "observe";
    if (minEdge) minEdge.value = String(settings.min_edge_cents ?? 0.1);
    if (slippage) slippage.value = String(settings.slippage_ticks ?? 1);
    if (tradePct) tradePct.value = String(settings.trade_size_pct ?? 0.05);
    if (maxPosition) maxPosition.value = String(settings.max_position_usd ?? 50);
    if (volOverride) {
      volOverride.value = settings.volatility_override == null ? "" : String(settings.volatility_override);
    }
    if (levy) levy.value = String(settings.levy_responsiveness ?? 1.35);
    if (pbookGate) pbookGate.value = settings.use_p_book_hard_gate ? "true" : "false";
  }

  function collectSettingsFromForm() {
    return {
      execution_mode: (byId("settingExecutionMode")?.value || "observe").toLowerCase(),
      min_edge_cents: asNumber(byId("settingMinEdge")?.value, 0.1),
      slippage_ticks: Math.max(0, Math.round(asNumber(byId("settingSlippage")?.value, 1))),
      trade_size_pct: asNumber(byId("settingTradePct")?.value, 0.05),
      max_position_usd: asNumber(byId("settingMaxPosition")?.value, 50),
      volatility_override:
        (byId("settingVolOverride")?.value || "").trim() === ""
          ? null
          : asNumber(byId("settingVolOverride")?.value, null),
      levy_responsiveness: asNumber(byId("settingLevyResponsiveness")?.value, 1.35),
      use_p_book_hard_gate: (byId("settingPbookGate")?.value || "false") === "true",
    };
  }

  function formatCents(cents) {
    if (typeof cents !== "number" || !Number.isFinite(cents)) {
      return "--";
    }
    return `${cents.toFixed(2)}c`;
  }

  function formatProbability(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      return "--";
    }
    return `${(value * 100).toFixed(2)}%`;
  }

  function renderSignalMonologue(monologue) {
    const ticker = byId("signalIntentTicker");
    if (ticker) {
      ticker.textContent =
        monologue?.action_intent || monologue?.decision_reason || "Signal engine evaluating market...";
    }

    const fairValue = byId("signalFairValue");
    if (fairValue) {
      fairValue.textContent = formatCents(Number(monologue?.model_fair_value_cents));
    }

    const implied = byId("signalImpliedProb");
    if (implied) {
      implied.textContent = formatProbability(Number(monologue?.market_implied_probability));
    }

    const lean = byId("signalLeanSide");
    if (lean) {
      lean.textContent = (monologue?.lean_side || "--").toString().toUpperCase();
    }

    const edge = byId("signalBestEdge");
    if (edge) {
      edge.textContent = formatCents(Number(monologue?.best_edge_cents));
    }
  }

  function renderLedger(ledger) {
    const summary = byId("liveLedgerSummary");
    const table = byId("liveLedgerTable");
    const positions = Array.isArray(ledger?.open_positions) ? ledger.open_positions : [];

    const cash = Number(ledger?.cash_cents || 0) / 100;
    const equity = Number(ledger?.equity_cents || 0) / 100;
    const realized = Number(ledger?.realized_pnl_cents || 0) / 100;
    const unrealized = Number(ledger?.unrealized_pnl_cents || 0) / 100;
    const fills = Number(ledger?.fills_total || 0);

    if (summary) {
      summary.textContent =
        `Cash: $${cash.toFixed(2)} | Equity: $${equity.toFixed(2)} | ` +
        `Realized: $${realized.toFixed(2)} | Unrealized: $${unrealized.toFixed(2)} | Fills: ${fills}`;
    }

    if (!table) {
      return;
    }
    const body = table.querySelector("tbody");
    if (!body) {
      return;
    }

    if (!positions.length) {
      body.innerHTML = "<tr><td colspan='6'>No open positions.</td></tr>";
      return;
    }

    body.innerHTML = positions
      .map((pos) => {
        const upnl = Number(pos?.unrealized_pnl_cents || 0) / 100;
        const pnlClass = upnl >= 0 ? "pos-up" : "pos-down";
        return (
          `<tr>` +
          `<td>${pos?.market_ticker || "n/a"}</td>` +
          `<td>${(pos?.side || "").toString().toUpperCase()}</td>` +
          `<td>${Number(pos?.contracts || 0)}</td>` +
          `<td>${Number(pos?.avg_entry_cents || 0).toFixed(2)}c</td>` +
          `<td>${Number(pos?.mark_cents || 0).toFixed(2)}c</td>` +
          `<td class='${pnlClass}'>$${upnl.toFixed(2)}</td>` +
          `</tr>`
        );
      })
      .join("");
  }

  async function loadSettings() {
    try {
      const res = await fetch("/api/settings");
      const payload = await res.json();
      if (!payload.ok) {
        setStatus("Failed to load settings", true);
        return;
      }
      applySettingsToForm(payload.current_settings || {});
      setStatus("Settings loaded.", false);
    } catch (error) {
      setStatus(`Settings load failed: ${error?.message || error}`, true);
    }
  }

  async function saveSettings() {
    setStatus("Saving settings...", false);
    try {
      const body = { settings: collectSettingsFromForm() };
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const payload = await res.json();
      if (!res.ok || !payload.ok) {
        const msg = (payload.errors || []).join(" | ") || payload.error || "Save failed";
        setStatus(msg, true);
        return;
      }
      applySettingsToForm(payload.current_settings || {});
      setStatus("Settings saved.", false);
    } catch (error) {
      setStatus(`Settings save failed: ${error?.message || error}`, true);
    }
  }

  async function resetSettings() {
    setStatus("Resetting settings...", false);
    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ operation: "reset" }),
      });
      const payload = await res.json();
      if (!res.ok || !payload.ok) {
        setStatus(payload.error || "Reset failed", true);
        return;
      }
      applySettingsToForm(payload.current_settings || {});
      setStatus("Settings reset to defaults.", false);
    } catch (error) {
      setStatus(`Settings reset failed: ${error?.message || error}`, true);
    }
  }

  async function resetLedger() {
    setStatus("Resetting paper ledger...", false);
    try {
      const res = await fetch("/api/shadow/ledger/reset", { method: "POST" });
      const payload = await res.json();
      if (!res.ok || !payload.ok) {
        setStatus(payload.error || "Ledger reset failed", true);
        return;
      }
      setStatus("Paper ledger reset.", false);
    } catch (error) {
      setStatus(`Ledger reset failed: ${error?.message || error}`, true);
    }
  }

  function onState(state) {
    const runtime = state?.shadow_runtime || {};
    const settings = state?.shadow_settings || {};
    const ledger = state?.paper_ledger || runtime?.paper_ledger || {};
    const monologue = state?.signal_monologue || runtime?.signal_monologue || {};

    const runtimeStatus = byId("runtimeStatus");
    if (runtimeStatus) {
      const modeReq = settings.execution_mode || "?";
      const modeEff = settings.effective_mode || runtime.effective_mode || "?";
      const modeReason = settings.mode_reason || runtime.mode_reason || "ok";
      const ticker = runtime.current_market_ticker || "n/a";
      const status = runtime.status || "n/a";
      const equity = Number(ledger.equity_cents || 0) / 100;
      const unrealized = Number(ledger.unrealized_pnl_cents || 0) / 100;
      runtimeStatus.textContent =
        `Mode req/eff: ${modeReq}/${modeEff} (${modeReason}) | Runtime: ${status} | Market: ${ticker} | ` +
        `Equity: $${equity.toFixed(2)} | Unrealized: $${unrealized.toFixed(2)}`;
    }

    renderSignalMonologue(monologue);
    renderLedger(ledger);
  }

  function init() {
    byId("settingsSaveBtn")?.addEventListener("click", () => {
      saveSettings().catch(() => {});
    });
    byId("settingsResetBtn")?.addEventListener("click", () => {
      resetSettings().catch(() => {});
    });
    byId("ledgerResetBtn")?.addEventListener("click", () => {
      resetLedger().catch(() => {});
    });

    byId("settingsTabBtn")?.addEventListener("click", () => setActiveTab("settings"));
    byId("simulationTabBtn")?.addEventListener("click", () => setActiveTab("simulation"));

    setActiveTab("settings");
    loadSettings().catch(() => {});
  }

  window.DashboardSettings = {
    init,
    loadSettings,
    onState,
    setActiveTab,
  };

  init();
})();
