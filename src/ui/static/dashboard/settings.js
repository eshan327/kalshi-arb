(function () {
  const byId = (id) => document.getElementById(id);
  let executionMode = "paper";
  let latestState = {};

  function numberValue(id, fallback) {
    const value = Number(byId(id)?.value);
    return Number.isFinite(value) ? value : fallback;
  }

  function setStatus(message, isError = false) {
    const element = byId("settingsStatus");
    if (!element) return;
    element.textContent = String(message || "");
    element.style.color = isError ? "#ffcf72" : "#93a6c4";
  }

  function setManualStatus(message, isError = false) {
    const element = byId("manualStatus");
    if (!element) return;
    element.textContent = String(message || "");
    element.style.color = isError ? "#ffcf72" : "#93a6c4";
  }

  function setPill(id, text, tone = "") {
    const element = byId(id);
    if (!element) return;
    element.textContent = text;
    element.className = `status-pill${tone ? ` ${tone}-state` : ""}`;
  }

  function applySettings(settings) {
    executionMode = settings.execution_mode || executionMode;
    const modeLabel = executionMode === "live" ? "Live" : "Paper";
    byId("tradingArmBtn").textContent = `Arm ${modeLabel}`;
    byId("tradingHeading").textContent = `${modeLabel} Algorithmic Trading`;
    byId("accountHeading").textContent = `${modeLabel} Account and Signal`;
    byId("manualCount").max = String(settings.max_order_contracts || 1);
    const values = {
      settingEntriesEnabled: settings.entries_enabled ? "true" : "false",
      settingMinEdge: settings.min_edge_cents,
      settingMaxOrderContracts: settings.max_order_contracts,
      settingMaxPosition: settings.max_position_usd,
      settingMaxDailyLoss: settings.max_daily_loss_usd,
      settingCashBuffer: settings.cash_buffer_usd,
      settingCooldown: settings.cooldown_seconds,
      settingSlippage: settings.slippage_ticks,
      settingVolOverride: settings.volatility_override == null ? "" : settings.volatility_override,
      settingVolatilityScale: settings.volatility_scale,
      settingPbookGate: settings.use_p_book_hard_gate ? "true" : "false",
      settingPbookDivergence: settings.p_book_max_divergence,
    };
    Object.entries(values).forEach(([id, value]) => {
      const element = byId(id);
      if (element && value !== undefined) element.value = String(value);
    });
  }

  function collectSettings() {
    const volOverride = (byId("settingVolOverride")?.value || "").trim();
    return {
      entries_enabled: byId("settingEntriesEnabled")?.value === "true",
      min_edge_cents: numberValue("settingMinEdge", 3),
      max_order_contracts: Math.round(numberValue("settingMaxOrderContracts", 5)),
      max_position_usd: numberValue("settingMaxPosition", 10),
      max_daily_loss_usd: numberValue("settingMaxDailyLoss", 10),
      cash_buffer_usd: numberValue("settingCashBuffer", 25),
      cooldown_seconds: Math.round(numberValue("settingCooldown", 5)),
      slippage_ticks: Math.round(numberValue("settingSlippage", 1)),
      volatility_override: volOverride === "" ? null : Number(volOverride),
      volatility_scale: numberValue("settingVolatilityScale", 1),
      use_p_book_hard_gate: byId("settingPbookGate")?.value === "true",
      p_book_max_divergence: numberValue("settingPbookDivergence", 0.35),
    };
  }

  async function settingsRequest(body) {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) {
      throw new Error((payload.errors || []).join(" | ") || payload.error || "Request failed");
    }
    applySettings(payload.current_settings || {});
  }

  async function loadSettings() {
    try {
      const response = await fetch("/api/settings");
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Request failed");
      applySettings(payload.current_settings || {});
      setStatus("Settings loaded.");
    } catch (error) {
      setStatus(`Settings load failed: ${error.message || error}`, true);
    }
  }

  async function saveSettings() {
    setStatus("Saving settings...");
    try {
      await settingsRequest({ settings: collectSettings() });
      setStatus("Settings saved.");
    } catch (error) {
      setStatus(`Settings save failed: ${error.message || error}`, true);
    }
  }

  async function resetSettings() {
    try {
      await settingsRequest({ operation: "reset" });
      setStatus("Settings reset to conservative defaults.");
    } catch (error) {
      setStatus(`Settings reset failed: ${error.message || error}`, true);
    }
  }

  async function control(operation, confirmation = "") {
    setStatus(`${operation} requested...`);
    try {
      const response = await fetch("/api/trading/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ operation, confirmation }),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || payload.cancel_error || "Request failed");
      setStatus(`Trading ${payload.status}.`);
    } catch (error) {
      setStatus(`Trading control failed: ${error.message || error}`, true);
    }
  }

  function finite(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function money(cents) {
    return finite(cents) ? `$${(cents / 100).toFixed(2)}` : "--";
  }

  function cents(value) {
    return finite(value) ? `${value.toFixed(2)}c` : "--";
  }

  function probability(value) {
    return finite(value) ? `${(value * 100).toFixed(2)}%` : "--";
  }

  function renderSignal(monologue) {
    byId("signalIntentTicker").textContent =
      monologue?.action_intent || "Signal engine evaluating market...";
    byId("signalFairValue").textContent = cents(monologue?.model_fair_value_cents);
    byId("signalImpliedProb").textContent = probability(monologue?.market_implied_probability);
    byId("signalLeanSide").textContent = String(monologue?.lean_side || "--").toUpperCase();
    byId("signalBestEdge").textContent = cents(monologue?.best_edge_cents);
  }

  function renderAccount(account) {
    const positions = Array.isArray(account?.positions) ? account.positions : [];
    byId("liveAccountSummary").textContent =
      `Cash: ${money(account?.cash_cents)} | Portfolio: ${money(account?.portfolio_value_cents)} | ` +
      `Equity: ${money(account?.equity_cents)} | Positions: ${positions.length}`;

    const body = byId("livePositionTable")?.querySelector("tbody");
    if (!body) return;
    body.replaceChildren();
    if (!positions.length) {
      const row = body.insertRow();
      const cell = row.insertCell();
      cell.colSpan = 6;
      cell.textContent = "No open positions.";
      return;
    }
    positions.forEach((position) => {
      const values = [
        position.market_ticker || "n/a",
        String(position.side || "").toUpperCase(),
        position.contracts ?? 0,
        cents(position.avg_entry_cents),
        money(position.market_exposure_cents),
        money(position.realized_pnl_cents),
      ];
      const row = body.insertRow();
      values.forEach((value) => {
        row.insertCell().textContent = String(value);
      });
    });
  }

  function renderManualTicket() {
    const state = latestState || {};
    const runtime = state.trading_runtime || {};
    const settings = state.trading_settings || {};
    const risk = runtime.daily_risk || {};
    const orderbook = state.orderbook || {};
    const side = byId("manualSide")?.value || "yes";
    const action = byId("manualAction")?.value || "buy";
    const levelKey = `${side}_${action === "buy" ? "asks" : "bids"}`;
    const level = Array.isArray(orderbook[levelKey]) ? orderbook[levelKey][0] : null;
    const quote = Array.isArray(level) ? Number(level[0]) : NaN;
    const pModel = Number(state.pricing?.p_model);
    const fair = Number.isFinite(pModel)
      ? (side === "yes" ? pModel : 1 - pModel) * 100
      : NaN;
    const positions = Array.isArray(state.account?.positions)
      ? state.account.positions
      : [];
    const position = positions.find(
      (item) =>
        item.market_ticker === runtime.current_market_ticker && item.side === side
    );
    const quoteLabel = action === "buy" ? "ask" : "bid";
    byId("manualQuoteSummary").textContent = Number.isFinite(quote)
      ? `${action.toUpperCase()} ${side.toUpperCase()} | top ${quoteLabel} ${cents(quote)} | ` +
        `model fair ${cents(fair)} | held ${position?.contracts ?? 0} | ` +
        `${settings.slippage_ticks ?? 0} tick IOC protection`
      : `No current ${side.toUpperCase()} ${quoteLabel}.`;

    const ready = Boolean(
      runtime.armed && orderbook.initialized && !risk.locked && Number.isFinite(quote)
    );
    byId("manualSubmitBtn").disabled = !ready;
    if (!runtime.armed) setManualStatus("Arm trading to enable discretionary orders.");
    else if (risk.locked) setManualStatus("Daily-loss guard is locked.", true);
    else if (!Number.isFinite(quote)) {
      setManualStatus("Waiting for a current quote.", true);
    } else setManualStatus("Ready. Every order requires a mode-specific confirmation.");
  }

  async function submitManualOrder() {
    const count = Number(byId("manualCount")?.value);
    if (!Number.isInteger(count) || count < 1) {
      setManualStatus("Contracts must be a positive whole number.", true);
      return;
    }
    const phrase = `SUBMIT ${executionMode.toUpperCase()}`;
    const confirmation =
      window.prompt(`Type ${phrase} to submit this ${executionMode} IOC order:`) || "";
    if (!confirmation) return;

    setManualStatus("Submitting discretionary IOC...");
    try {
      const response = await fetch("/api/trading/manual", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: byId("manualAction")?.value,
          side: byId("manualSide")?.value,
          count,
          confirmation,
        }),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) {
        throw new Error(payload.error || "Order failed");
      }
      setManualStatus(`Order ${payload.status}.`);
    } catch (error) {
      setManualStatus(`Order failed: ${error.message || error}`, true);
    }
  }

  function onState(state) {
    latestState = state || {};
    const runtime = state?.trading_runtime || {};
    const settings = state?.trading_settings || {};
    const account = state?.account || runtime.account || {};
    const risk = runtime.daily_risk || {};
    executionMode = runtime.execution_mode || settings.execution_mode || executionMode;
    const gate = executionMode === "live"
      ? (settings.live_trading_enabled ? "live gate enabled" : "live gate disabled")
      : "simulated fills";
    const armed = runtime.armed ? "ARMED" : "DISARMED";
    const lock = risk.locked ? " | DAILY LOSS LOCKED" : "";
    const orderbookReady = Boolean(state?.orderbook?.initialized);
    const cycleAge = finite(runtime.last_cycle_ts)
      ? Math.max(0, Date.now() / 1000 - runtime.last_cycle_ts)
      : null;
    const bookAge = finite(state?.orderbook?.last_update_ts)
      ? Math.max(0, Date.now() / 1000 - state.orderbook.last_update_ts)
      : null;
    const dataAge = cycleAge == null || bookAge == null ? null : Math.max(cycleAge, bookAge);
    setPill(
      "modeStatus",
      executionMode.toUpperCase(),
      executionMode === "live" ? "warn" : "ok"
    );
    setPill(
      "armedStatus",
      runtime.armed ? "ARMED" : "DISARMED",
      runtime.armed ? (executionMode === "live" ? "danger" : "ok") : "warn"
    );
    setPill(
      "marketStatus",
      runtime.current_market_ticker || "NO MARKET",
      runtime.current_market_ticker ? "ok" : "warn"
    );
    setPill(
      "dataStatus",
      orderbookReady && dataAge != null
        ? `DATA ${dataAge.toFixed(1)}s`
        : "DATA WAITING",
      orderbookReady && dataAge != null && dataAge < 5 ? "ok" : "warn"
    );
    setPill(
      "riskStatus",
      risk.locked ? "DAILY LOSS LOCKED" : `DAY ${money(risk.drawdown_cents)}`,
      risk.locked ? "danger" : "ok"
    );
    byId("runtimeStatus").textContent =
      `${executionMode.toUpperCase()} | ${String(settings.kalshi_env || runtime.kalshi_env || "?").toUpperCase()} | ${gate} | ${armed} | ` +
      `Runtime: ${runtime.status || "n/a"} | Market: ${runtime.current_market_ticker || "n/a"} | ` +
      `Daily P&L: ${money(risk.drawdown_cents)}${lock}`;
    renderSignal(state?.signal_monologue || runtime.signal_monologue || {});
    renderAccount(account);
    renderManualTicket();
  }

  byId("settingsSaveBtn")?.addEventListener("click", saveSettings);
  byId("settingsResetBtn")?.addEventListener("click", resetSettings);
  byId("tradingArmBtn")?.addEventListener("click", () => {
    const phrase = `ARM ${executionMode.toUpperCase()}`;
    const confirmation = window.prompt(`Type ${phrase} to arm ${executionMode} trading:`) || "";
    if (confirmation) control("arm", confirmation);
  });
  byId("tradingPauseBtn")?.addEventListener("click", () => control("pause"));
  byId("tradingFlattenBtn")?.addEventListener("click", () => {
    const confirmation = window.prompt("Type FLATTEN to pause and close the active market position:") || "";
    if (confirmation) control("flatten", confirmation);
  });
  byId("manualAction")?.addEventListener("change", renderManualTicket);
  byId("manualSide")?.addEventListener("change", renderManualTicket);
  byId("manualSubmitBtn")?.addEventListener("click", submitManualOrder);

  window.DashboardSettings = { loadSettings, onState };
  loadSettings();
})();
