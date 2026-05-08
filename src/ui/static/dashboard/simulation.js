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

  function setSimulationStatus(message, isError) {
    const el = byId("simulationStatus");
    if (!el) {
      return;
    }
    el.textContent = String(message || "");
    el.style.color = isError ? "#ffcf72" : "#93a6c4";
  }

  function injectHtmlWithScripts(container, html) {
    if (!container) {
      return;
    }

    container.innerHTML = html || "";
    const scripts = container.querySelectorAll("script");
    scripts.forEach((oldScript) => {
      const script = document.createElement("script");
      Array.from(oldScript.attributes).forEach((attr) => {
        script.setAttribute(attr.name, attr.value);
      });
      script.text = oldScript.text;
      oldScript.parentNode?.replaceChild(script, oldScript);
    });
  }

  function renderMetrics(metrics) {
    const el = byId("simulationMetrics");
    if (!el) {
      return;
    }

    const rows = [
      ["Total Trades Executed", metrics.total_trades_executed ?? "-"],
      ["Win Rate (%)", metrics.win_rate_pct ?? "-"],
      ["Average Edge Captured", metrics.average_edge_captured_cents ?? "-"],
      ["Max Drawdown (%)", metrics.max_drawdown_pct ?? "-"],
      ["Sharpe Ratio", metrics.sharpe_ratio ?? "-"],
      ["Starting Bankroll (USD)", metrics.starting_bankroll_usd ?? "-"],
      ["Ending Bankroll (USD)", metrics.ending_bankroll_usd ?? "-"],
    ];

    const html =
      "<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>" +
      rows.map((row) => `<tr><td>${row[0]}</td><td>${row[1]}</td></tr>`).join("") +
      "</tbody></table>";

    el.innerHTML = html;
  }

  function renderImageLinks(imageUrls, outputDir) {
    const el = byId("simulationAssets");
    if (!el) {
      return;
    }

    const links = [];
    if (imageUrls?.equity_curve) {
      links.push(`<a href="${imageUrls.equity_curve}" target="_blank" rel="noreferrer">Equity Curve PNG</a>`);
    }
    if (imageUrls?.edge_distribution) {
      links.push(`<a href="${imageUrls.edge_distribution}" target="_blank" rel="noreferrer">Edge Distribution PNG</a>`);
    }
    if (imageUrls?.tearsheet) {
      links.push(`<a href="${imageUrls.tearsheet}" target="_blank" rel="noreferrer">Tearsheet PNG</a>`);
    }

    if (!links.length) {
      el.innerHTML = "No image assets available yet.";
      return;
    }

    const output = outputDir ? `<div>Output folder: ${outputDir}</div>` : "";
    el.innerHTML = `${output}<div>${links.join(" | ")}</div>`;
  }

  function renderSimulationPayload(payload) {
    const divs = payload?.divs || {};
    injectHtmlWithScripts(byId("simulationEquityDiv"), divs.equity_curve || "<div>No equity chart available.</div>");
    injectHtmlWithScripts(byId("simulationEdgeDiv"), divs.edge_distribution || "<div>No edge chart available.</div>");
    injectHtmlWithScripts(byId("simulationTearsheetDiv"), divs.tearsheet || "<div>No tearsheet available.</div>");

    renderMetrics(payload?.metrics || {});
    renderImageLinks(payload?.image_urls || {}, payload?.output_dir || "");
  }

  function collectRequestPayload() {
    return {
      n_paths: Math.max(100, Math.round(asNumber(byId("simPaths")?.value, 5000))),
      horizon_seconds: Math.max(60, Math.round(asNumber(byId("simHorizon")?.value, 900))),
      n_steps: Math.max(60, Math.round(asNumber(byId("simSteps")?.value, 900))),
      drift_annual: asNumber(byId("simDrift")?.value, 0.0),
    };
  }

  async function generateSimulation() {
    const button = byId("simulationGenerateBtn");
    if (button) {
      button.disabled = true;
      button.textContent = "Generating...";
    }

    setSimulationStatus("Running Monte Carlo simulation...", false);
    try {
      const res = await fetch("/api/simulation/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(collectRequestPayload()),
      });
      const payload = await res.json();
      if (!res.ok || !payload.ok) {
        setSimulationStatus(payload.error || "Simulation generation failed.", true);
        return;
      }

      renderSimulationPayload(payload);
      setSimulationStatus("Simulation generated successfully.", false);
    } catch (error) {
      setSimulationStatus(`Simulation failed: ${error?.message || error}`, true);
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = "Generate Monte Carlo";
      }
    }
  }

  async function loadLatestSimulation() {
    setSimulationStatus("Loading latest simulation...", false);
    try {
      const res = await fetch("/api/simulation/latest");
      const payload = await res.json();
      if (!res.ok || !payload.ok) {
        setSimulationStatus(payload.error || "No saved simulation available.", true);
        return;
      }

      renderSimulationPayload(payload);
      setSimulationStatus("Loaded latest simulation.", false);
    } catch (error) {
      setSimulationStatus(`Failed to load latest simulation: ${error?.message || error}`, true);
    }
  }

  function onState(state) {
    const summary = state?.simulation_summary || {};
    if (!summary.ok) {
      return;
    }
    const generatedTs = summary.generated_ts;
    const metrics = summary.metrics || {};
    const totalTrades = metrics.total_trades_executed ?? "-";
    if (generatedTs) {
      const iso = new Date(Number(generatedTs) * 1000).toISOString();
      setSimulationStatus(`Latest run: ${iso} | Trades: ${totalTrades}`, false);
    }
  }

  function init() {
    byId("simulationGenerateBtn")?.addEventListener("click", () => {
      generateSimulation().catch(() => {});
    });
    byId("simulationRefreshBtn")?.addEventListener("click", () => {
      loadLatestSimulation().catch(() => {});
    });
  }

  window.DashboardSimulation = {
    init,
    onState,
    generateSimulation,
    loadLatestSimulation,
  };

  init();
})();
