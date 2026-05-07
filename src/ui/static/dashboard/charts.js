(function () {
  function prepareCanvas(canvasId, defaultHeight = 200) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) {
      return null;
    }

    const dpr = Math.max(1, Number(window.devicePixelRatio || 1));
    const cssWidth = Math.max(280, Math.floor(canvas.clientWidth || canvas.parentElement?.clientWidth || 800));
    const cssHeight = Math.max(120, Number(canvas.dataset.height || defaultHeight));

    canvas.style.height = `${cssHeight}px`;
    canvas.width = Math.floor(cssWidth * dpr);
    canvas.height = Math.floor(cssHeight * dpr);

    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    return { canvas, ctx, w: cssWidth, h: cssHeight };
  }

  function drawPriceChart(canvasId, series, emptyMessage) {
    const c = prepareCanvas(canvasId, 220);
    if (!c) {
      return;
    }

    const { ctx, w, h } = c;
    ctx.clearRect(0, 0, w, h);

    if (!Array.isArray(series) || !series.length) {
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

    const spanRaw = Math.max(yMax - yMin, 1e-6);
    yMin -= spanRaw * 0.08;
    yMax += spanRaw * 0.08;
    const span = yMax - yMin;

    const padLeft = 74;
    const padRight = 16;
    const padTop = 18;
    const padBottom = 44;

    ctx.strokeStyle = "#1f2a3d";
    ctx.lineWidth = 1;
    ctx.strokeRect(padLeft, padTop, w - padLeft - padRight, h - padTop - padBottom);

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
      ctx.font = "11px ui-monospace";
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
      if (i === 0) {
        ctx.moveTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
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
      maSeries.push({ ts: series[right].ts, value: sum / count });
    }

    return maSeries;
  }

  function drawBrtiChart(points, windowSeconds = 60) {
    const brtiSeries = toBrtiSeries(points);
    drawPriceChart("brtiChart", brtiSeries, "No index history yet");

    const movingAverageSeries = buildMovingAverageSeries(brtiSeries, windowSeconds);
    drawPriceChart("movingAvgChart", movingAverageSeries, "No moving-average history yet");
  }

  function drawSeriesLineChart(canvasId, series, emptyMessage, options = {}) {
    const c = prepareCanvas(canvasId, 180);
    if (!c) {
      return;
    }

    const { ctx, w, h } = c;
    ctx.clearRect(0, 0, w, h);

    if (!Array.isArray(series) || !series.length) {
      ctx.fillStyle = "#8ea0bf";
      ctx.font = "12px ui-monospace";
      ctx.fillText(emptyMessage, 12, 20);
      return;
    }

    const vals = series.map((p) => Number(p.value)).filter((v) => Number.isFinite(v));
    const tss = series.map((p) => Number(p.ts)).filter((v) => Number.isFinite(v));
    if (!vals.length || !tss.length) {
      ctx.fillStyle = "#8ea0bf";
      ctx.font = "12px ui-monospace";
      ctx.fillText(emptyMessage, 12, 20);
      return;
    }

    let yMin = Math.min(...vals);
    let yMax = Math.max(...vals);
    const fixedBounds = options.fixedBounds;
    if (fixedBounds && Number.isFinite(fixedBounds.min) && Number.isFinite(fixedBounds.max)) {
      yMin = fixedBounds.min;
      yMax = fixedBounds.max;
    }
    if (yMin === yMax) {
      yMin -= 1;
      yMax += 1;
    }

    const padLeft = 68;
    const padRight = 16;
    const padTop = 18;
    const padBottom = 34;

    ctx.strokeStyle = "#1f2a3d";
    ctx.lineWidth = 1;
    ctx.strokeRect(padLeft, padTop, w - padLeft - padRight, h - padTop - padBottom);

    const minTs = Math.min(...tss);
    const maxTs = Math.max(...tss);
    const tsSpan = Math.max(1e-6, maxTs - minTs);
    const ySpan = yMax - yMin;

    const yTicks = 4;
    for (let i = 0; i <= yTicks; i++) {
      const frac = i / yTicks;
      const y = padTop + (1 - frac) * (h - padTop - padBottom);
      const v = yMin + frac * ySpan;

      ctx.strokeStyle = "#152134";
      ctx.beginPath();
      ctx.moveTo(padLeft, y);
      ctx.lineTo(w - padRight, y);
      ctx.stroke();

      ctx.fillStyle = "#8ea0bf";
      ctx.font = "11px ui-monospace";
      ctx.textAlign = "right";
      const formatY = typeof options.formatY === "function" ? options.formatY : (x) => String(x.toFixed(2));
      ctx.fillText(formatY(v), padLeft - 8, y + 4);
    }

    ctx.beginPath();
    ctx.lineWidth = 2;
    ctx.strokeStyle = options.color || "#4bd19a";
    let started = false;
    series.forEach((point) => {
      const ts = Number(point.ts);
      const value = Number(point.value);
      if (!Number.isFinite(ts) || !Number.isFinite(value)) {
        return;
      }

      const x = padLeft + ((ts - minTs) / tsSpan) * (w - padLeft - padRight);
      const y = h - padBottom - ((value - yMin) / ySpan) * (h - padTop - padBottom);
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else {
        ctx.lineTo(x, y);
      }
    });
    ctx.stroke();
  }

  function drawPnlChart(series) {
    drawSeriesLineChart("pnlChart", series, "PnL curve waiting for settled results", {
      color: "#54e3a1",
      formatY: (v) => `$${(v / 100).toFixed(2)}`,
    });
  }

  function drawEdgeChart(series) {
    drawSeriesLineChart("edgeChart", series, "Edge capture curve waiting for fills", {
      color: "#6ec7ff",
      formatY: (v) => `${Number(v).toFixed(2)}c`,
    });
  }

  function drawWinRateChart(series) {
    const normalized = (series || []).map((pt) => ({
      ts: pt.ts,
      value: Number(pt.value) * 100,
    }));

    drawSeriesLineChart("winRateChart", normalized, "Win-rate curve waiting for settled outcomes", {
      color: "#ffcf72",
      formatY: (v) => `${Number(v).toFixed(1)}%`,
      fixedBounds: { min: 0, max: 100 },
    });
  }

  function drawConfidenceHistogram(confidenceBins) {
    const c = prepareCanvas("confidenceHist", 170);
    if (!c) {
      return;
    }

    const { ctx, w, h } = c;
    ctx.clearRect(0, 0, w, h);

    const entries = Object.entries(confidenceBins || {});
    if (!entries.length) {
      ctx.fillStyle = "#8ea0bf";
      ctx.font = "12px ui-monospace";
      ctx.fillText("No confidence samples yet", 12, 20);
      return;
    }

    const values = entries.map(([, v]) => Number(v) || 0);
    const maxVal = Math.max(1, ...values);

    const padLeft = 44;
    const padRight = 12;
    const padTop = 12;
    const padBottom = 34;
    const usableW = w - padLeft - padRight;
    const usableH = h - padTop - padBottom;
    const barW = usableW / entries.length;

    entries.forEach(([label, raw], idx) => {
      const val = Number(raw) || 0;
      const height = (val / maxVal) * usableH;
      const x = padLeft + idx * barW;
      const y = padTop + (usableH - height);

      ctx.fillStyle = "#47c78d";
      ctx.fillRect(x + 1, y, Math.max(1, barW - 2), height);

      if (idx % 4 === 0 || idx === entries.length - 1) {
        ctx.fillStyle = "#8ea0bf";
        ctx.font = "10px ui-monospace";
        ctx.textAlign = "center";
        ctx.fillText(label, x + barW / 2, h - 12);
      }
    });
  }

  function drawOrderTimingChart(events, windowSeconds = 900) {
    const c = prepareCanvas("timingChart", 170);
    if (!c) {
      return;
    }

    const { ctx, w, h } = c;
    ctx.clearRect(0, 0, w, h);

    const points = (events || []).filter(
      (ev) => Number.isFinite(Number(ev.ts)) && Number.isFinite(Number(ev.seconds_to_expiry)),
    );
    if (!points.length) {
      ctx.fillStyle = "#8ea0bf";
      ctx.font = "12px ui-monospace";
      ctx.fillText("No order timing samples yet", 12, 20);
      return;
    }

    const padLeft = 44;
    const padRight = 12;
    const padTop = 12;
    const padBottom = 30;
    const usableW = w - padLeft - padRight;
    const usableH = h - padTop - padBottom;
    const maxWindow = Math.max(1, Number(windowSeconds || 900));

    ctx.strokeStyle = "#1f2a3d";
    ctx.strokeRect(padLeft, padTop, usableW, usableH);

    points.forEach((ev) => {
      const sec = Math.max(0, Math.min(maxWindow, Number(ev.seconds_to_expiry)));
      const conf = Number(ev.confidence);
      const yNorm = Number.isFinite(conf) ? Math.max(0, Math.min(1, conf / 0.5)) : 0.5;

      const x = padLeft + ((maxWindow - sec) / maxWindow) * usableW;
      const y = padTop + (1 - yNorm) * usableH;

      ctx.fillStyle = String(ev.side || "") === "yes" ? "#4bd19a" : "#ff8f8f";
      ctx.beginPath();
      ctx.arc(x, y, 2.5, 0, Math.PI * 2);
      ctx.fill();
    });

    ctx.fillStyle = "#8ea0bf";
    ctx.font = "11px ui-monospace";
    ctx.textAlign = "left";
    ctx.fillText("Contract progress", padLeft, h - 8);
  }

  function drawCategoryBars(canvasId, categoryMap, emptyMessage, color = "#6ec7ff") {
    const c = prepareCanvas(canvasId, 180);
    if (!c) {
      return;
    }

    const { ctx, w, h } = c;
    ctx.clearRect(0, 0, w, h);

    const entries = Object.entries(categoryMap || {}).filter(([, raw]) => Number(raw) > 0);
    if (!entries.length) {
      ctx.fillStyle = "#8ea0bf";
      ctx.font = "12px ui-monospace";
      ctx.fillText(emptyMessage, 12, 20);
      return;
    }

    const sorted = [...entries].sort((a, b) => Number(b[1]) - Number(a[1])).slice(0, 14);
    const maxVal = Math.max(1, ...sorted.map(([, raw]) => Number(raw) || 0));

    const padLeft = 150;
    const padRight = 12;
    const padTop = 10;
    const padBottom = 14;
    const usableW = w - padLeft - padRight;
    const usableH = h - padTop - padBottom;
    const rowH = usableH / sorted.length;

    sorted.forEach(([label, raw], idx) => {
      const val = Number(raw) || 0;
      const width = (val / maxVal) * usableW;
      const y = padTop + idx * rowH + 2;

      ctx.fillStyle = color;
      ctx.fillRect(padLeft, y, Math.max(1, width), Math.max(1, rowH - 5));

      ctx.fillStyle = "#8ea0bf";
      ctx.font = "11px ui-monospace";
      ctx.textAlign = "right";
      ctx.fillText(String(label), padLeft - 8, y + Math.max(12, rowH - 8));

      ctx.textAlign = "left";
      ctx.fillStyle = "#d9e3ef";
      ctx.fillText(String(val), padLeft + width + 6, y + Math.max(12, rowH - 8));
    });
  }

  function drawRejectionReasonsChart(reasons) {
    drawCategoryBars(
      "rejectionChart",
      reasons,
      "No policy rejections yet",
      "#f08d8d",
    );
  }

  function drawMakerTakerChart(makerCount, takerCount) {
    drawCategoryBars(
      "makerTakerChart",
      {
        maker: Number(makerCount) || 0,
        taker: Number(takerCount) || 0,
      },
      "No fills yet",
      "#47c78d",
    );
  }

  function drawLatencyHistogram(latencyBins) {
    drawCategoryBars(
      "latencyChart",
      latencyBins,
      "No fill latency samples yet",
      "#ffcf72",
    );
  }

  function drawSpreadHistogram(spreadBins) {
    drawCategoryBars(
      "spreadChart",
      spreadBins,
      "No spread samples yet",
      "#7cb5ff",
    );
  }

  function drawWindowFillRateChart(series) {
    const normalized = (series || []).map((pt) => ({
      ts: pt.ts,
      value: Number(pt.value) * 100,
    }));
    drawSeriesLineChart(
      "windowFillChart",
      normalized,
      "No settled windows yet",
      {
        color: "#55d3a7",
        formatY: (v) => `${Number(v).toFixed(1)}%`,
        fixedBounds: { min: 0, max: 100 },
      },
    );
  }

  window.DashboardCharts = {
    drawBrtiChart,
    drawPnlChart,
    drawWinRateChart,
    drawEdgeChart,
    drawConfidenceHistogram,
    drawOrderTimingChart,
    drawRejectionReasonsChart,
    drawMakerTakerChart,
    drawLatencyHistogram,
    drawSpreadHistogram,
    drawWindowFillRateChart,
  };
})();
