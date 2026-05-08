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
    if (minEdge) minEdge.value = String(settings.min_edge_cents ?? 0.5);
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
      min_edge_cents: asNumber(byId("settingMinEdge")?.value, 0.5),
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
    const ledger = runtime?.paper_ledger || {};

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
