(function () {
  function drawPriceChart(canvasId, series, emptyMessage) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) {
      return;
    }

    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    if (!series.length) {
      ctx.fillStyle = "#8ea0bf";
      ctx.font = "12px ui-monospace";
      ctx.fillText(emptyMessage, 12, 20);
      return;
    }

    const vals = series.map((p) => p.value);
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const suggestedStrike = Number(window.__suggestedStrike);
    const strike = Number.isFinite(suggestedStrike) ? suggestedStrike : null;

    let yMin = min;
    let yMax = max;
    if (strike !== null) {
      yMin = Math.min(yMin, strike);
      yMax = Math.max(yMax, strike);
    }

    const padPct = 0.08;
    const spanRaw = Math.max(yMax - yMin, 1e-6);
    yMin -= spanRaw * padPct;
    yMax += spanRaw * padPct;
    const span = yMax - yMin;

    const padLeft = 74;
    const padRight = 16;
    const padTop = 18;
    const padBottom = 44;
    ctx.strokeStyle = "#1f2a3d";
    ctx.lineWidth = 1;
    ctx.strokeRect(padLeft, padTop, w - padLeft - padRight, h - padTop - padBottom);

    ctx.fillStyle = "#8ea0bf";
    ctx.font = "11px ui-monospace";
    ctx.textAlign = "left";
    ctx.fillText("Price (USD)", padLeft + 4, padTop + 12);

    const yTicks = 4;
    for (let i = 0; i <= yTicks; i++) {
      const frac = i / yTicks;
      const y = padTop + (1 - frac) * (h - padTop - padBottom);
      const v = yMin + frac * span;
      ctx.strokeStyle = "#152134";
      ctx.beginPath();
      ctx.moveTo(padLeft, y);
      ctx.lineTo(w - padRight, y);
      ctx.stroke();

      ctx.fillStyle = "#8ea0bf";
      ctx.textAlign = "right";
      ctx.fillText(v.toFixed(2), padLeft - 8, y + 4);
    }

    const minTs = series[0].ts;
    const maxTs = series[series.length - 1].ts;
    const tsSpan = Math.max(maxTs - minTs, 1e-6);
    const xTicks = 8;
    let lastLabelRight = -Infinity;
    ctx.textAlign = "center";
    for (let i = 0; i <= xTicks; i++) {
      const frac = i / xTicks;
      const x = padLeft + frac * (w - padLeft - padRight);
      const ts = minTs + frac * tsSpan;

      ctx.strokeStyle = "#152134";
      ctx.beginPath();
      ctx.moveTo(x, padTop);
      ctx.lineTo(x, h - padBottom);
      ctx.stroke();

      const label = new Date(ts * 1000).toLocaleTimeString([], {
        hour12: false,
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
      const width = ctx.measureText(label).width;
      const left = x - width / 2;
      const right = x + width / 2;
      if (left > lastLabelRight + 8 || i === xTicks) {
        ctx.fillStyle = "#8ea0bf";
        ctx.fillText(label, x, h - 20);
        lastLabelRight = right;
      }
    }
    ctx.textAlign = "center";
    ctx.fillStyle = "#8ea0bf";
    ctx.fillText("Time (HH:MM:SS)", padLeft + (w - padLeft - padRight) / 2, h - 4);

    if (strike !== null) {
      const yStrike = padTop + (1 - (strike - yMin) / span) * (h - padTop - padBottom);
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = "#ffc96a";
      ctx.beginPath();
      ctx.moveTo(padLeft, yStrike);
      ctx.lineTo(w - padRight, yStrike);
      ctx.stroke();
      ctx.setLineDash([]);

      const label = `Strike ${strike.toFixed(2)}`;
      const labelW = ctx.measureText(label).width;
      const labelX = w - padRight - labelW - 8;
      const labelY = Math.max(14, Math.min(h - padBottom - 4, yStrike - 6));
      ctx.fillStyle = "rgba(13, 21, 32, 0.92)";
      ctx.fillRect(labelX - 4, labelY - 10, labelW + 8, 14);
      ctx.fillStyle = "#ffc96a";
      ctx.textAlign = "left";
      ctx.fillText(label, labelX, labelY);
    }

    ctx.beginPath();
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#4bd19a";
    series.forEach((point, i) => {
      const x = padLeft + ((point.ts - minTs) / tsSpan) * (w - padLeft - padRight);
      const y = h - padBottom - ((point.value - yMin) / span) * (h - padTop - padBottom);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  function toBrtiSeries(points) {
    return points
      .filter((p) => p.brti !== null && Number.isFinite(Number(p.brti)) && Number.isFinite(Number(p.ts)))
      .map((p) => ({ ts: Number(p.ts), value: Number(p.brti) }))
      .sort((a, b) => a.ts - b.ts);
  }

  function buildMovingAverageSeries(series, windowSeconds = 60) {
    if (!series.length) {
      return [];
    }

    const maSeries = [];
    let left = 0;
    let sum = 0;

    for (let right = 0; right < series.length; right++) {
      sum += series[right].value;

      while (series[right].ts - series[left].ts > windowSeconds) {
        sum -= series[left].value;
        left += 1;
      }

      const count = right - left + 1;
      maSeries.push({
        ts: series[right].ts,
        value: sum / count,
      });
    }

    return maSeries;
  }

  function drawBrtiChart(points, windowSeconds = 60) {
    const brtiSeries = toBrtiSeries(points);
    drawPriceChart("brtiChart", brtiSeries, "No index history yet");

    const movingAverageSeries = buildMovingAverageSeries(brtiSeries, windowSeconds);
    drawPriceChart("movingAvgChart", movingAverageSeries, "No moving-average history yet");
  }

  window.DashboardCharts = {
    drawBrtiChart,
  };
})();
