(function () {
  const { friendlyAssetName } = window.DashboardFormat;

  let assetSelectorBound = false;
  let assetSwitchInFlight = false;

  async function requestAssetSwitch(asset) {
    const selector = document.getElementById("assetSelector");
    const status = document.getElementById("assetSelectorStatus");
    if (!selector || !status) {
      return;
    }

    assetSwitchInFlight = true;
    selector.disabled = true;
    status.textContent = `Requesting ${friendlyAssetName(asset)} switch...`;
    status.style.color = "var(--warn)";

    try {
      const response = await fetch("/api/market-selection", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ asset }),
      });

      const payload = await response.json();
      if (!response.ok || !payload.ok) {
        throw new Error(payload.error || payload.message || "Asset switch failed.");
      }

      status.textContent = payload.message || `Switch queued to ${friendlyAssetName(asset)}.`;
      status.style.color = "var(--ok)";
    } catch (err) {
      status.textContent = String(err?.message || err || "Asset switch failed.");
      status.style.color = "var(--warn)";
    } finally {
      selector.disabled = false;
      assetSwitchInFlight = false;
    }
  }

  function bindAssetSelector() {
    if (assetSelectorBound) {
      return;
    }

    const selector = document.getElementById("assetSelector");
    if (!selector) {
      return;
    }

    selector.addEventListener("change", (event) => {
      const selected = event.target?.value;
      if (!selected) {
        return;
      }
      requestAssetSwitch(selected).catch(() => {});
    });

    assetSelectorBound = true;
  }

  function syncAssetSelectorFromState(state) {
    const selector = document.getElementById("assetSelector");
    const status = document.getElementById("assetSelectorStatus");
    if (!selector || !status) {
      return;
    }

    const selection = state.market_selection || {};
    const options = Array.isArray(selection.options) && selection.options.length ? selection.options : ["BTC", "ETH"];

    const currentMarkup = options
      .map((asset) => `<option value="${asset}">${friendlyAssetName(asset)}</option>`)
      .join("");

    if (selector.dataset.optionMarkup !== currentMarkup) {
      selector.innerHTML = currentMarkup;
      selector.dataset.optionMarkup = currentMarkup;
    }

    const activeAsset = selection.active_asset || state.asset || "BTC";
    if (!assetSwitchInFlight) {
      selector.value = activeAsset;
    }

    const requested = selection.requested_asset;
    if (requested) {
      status.textContent = `Switch queued: ${friendlyAssetName(requested)} (applies on market close)`;
      status.style.color = "var(--warn)";
    } else {
      status.textContent = `Active: ${friendlyAssetName(activeAsset)} (${activeAsset})`;
      status.style.color = "var(--ok)";
    }
  }

  window.DashboardAssetSelection = {
    bindAssetSelector,
    syncAssetSelectorFromState,
  };
})();
