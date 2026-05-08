from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from core.config import SIMULATION_OUTPUT_DIR, SIMULATION_PNG_HEIGHT, SIMULATION_PNG_WIDTH

try:
    import plotly.graph_objects as go
except Exception:  # pragma: no cover - optional dependency path
    go = None


def _run_output_dir() -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    out_dir = Path(SIMULATION_OUTPUT_DIR) / f"simulation_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _matplotlib_equity_png(path: Path, profit_dollars: list[float], return_pct: list[float]) -> None:
    fig, ax1 = plt.subplots(figsize=(13, 6), dpi=150)
    x = np.arange(len(profit_dollars), dtype=float)
    ax1.plot(x, profit_dollars, color="#1f7a48", linewidth=2.2)
    ax1.set_xlabel("Simulated Trade Index")
    ax1.set_ylabel("Raw Dollar Profit", color="#1f7a48")
    ax1.tick_params(axis="y", labelcolor="#1f7a48")
    ax1.grid(alpha=0.25)

    ax2 = ax1.twinx()
    ax2.plot(x, return_pct, color="#12587a", linewidth=2.0)
    ax2.set_ylabel("Percentage Return (%)", color="#12587a")
    ax2.tick_params(axis="y", labelcolor="#12587a")

    fig.tight_layout()
    fig.savefig(path, format="png")
    plt.close(fig)


def _matplotlib_edge_png(path: Path, edge_samples: list[float]) -> None:
    fig, ax = plt.subplots(figsize=(12, 6), dpi=150)
    if edge_samples:
        ax.hist(edge_samples, bins=40, color="#1f5a7a", edgecolor="#0f2a3a", alpha=0.86)
    else:
        ax.text(0.5, 0.5, "No edge samples", ha="center", va="center", transform=ax.transAxes)
    ax.set_title("Edge Distribution")
    ax.set_xlabel("Edge Captured (cents)")
    ax.set_ylabel("Frequency")
    ax.grid(alpha=0.22)
    fig.tight_layout()
    fig.savefig(path, format="png")
    plt.close(fig)


def _matplotlib_tearsheet_png(path: Path, metrics: dict[str, Any]) -> None:
    rows = [
        ("Total Trades Executed", metrics.get("total_trades_executed", 0)),
        ("Win Rate (%)", metrics.get("win_rate_pct", 0.0)),
        ("Average Edge Captured", metrics.get("average_edge_captured_cents", 0.0)),
        ("Max Drawdown (%)", metrics.get("max_drawdown_pct", 0.0)),
        ("Sharpe Ratio", metrics.get("sharpe_ratio", 0.0)),
    ]

    fig, ax = plt.subplots(figsize=(10, 3.8), dpi=170)
    ax.axis("off")
    table = ax.table(
        cellText=[[str(k), str(v)] for k, v in rows],
        colLabels=["Metric", "Value"],
        loc="center",
        cellLoc="left",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(11)
    table.scale(1, 1.6)
    fig.tight_layout()
    fig.savefig(path, format="png")
    plt.close(fig)


def _plotly_equity_figure(profit_dollars: list[float], return_pct: list[float]):
    x = list(range(len(profit_dollars)))
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=x,
            y=profit_dollars,
            mode="lines",
            name="Raw Dollar Profit",
            line={"width": 2.2, "color": "#1f7a48"},
            yaxis="y1",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=x,
            y=return_pct,
            mode="lines",
            name="Percentage Return",
            line={"width": 2.0, "color": "#12587a"},
            yaxis="y2",
        )
    )
    fig.update_layout(
        title="Cumulative PnL Equity Curve",
        xaxis={"title": "Simulated Trade Index"},
        yaxis={"title": "Raw Dollar Profit"},
        yaxis2={"title": "Percentage Return (%)", "overlaying": "y", "side": "right"},
        legend={"orientation": "h"},
        template="plotly_white",
        margin={"l": 60, "r": 70, "t": 64, "b": 48},
    )
    return fig


def _plotly_edge_figure(edge_samples: list[float]):
    fig = go.Figure()
    fig.add_trace(
        go.Histogram(
            x=edge_samples,
            nbinsx=40,
            marker={"color": "#1f5a7a", "line": {"color": "#0f2a3a", "width": 1}},
            name="Edge",
        )
    )
    fig.update_layout(
        title="Edge Distribution",
        xaxis={"title": "Edge Captured (cents)"},
        yaxis={"title": "Frequency"},
        template="plotly_white",
        margin={"l": 60, "r": 30, "t": 64, "b": 48},
    )
    return fig


def _plotly_tearsheet_figure(metrics: dict[str, Any]):
    rows = [
        ("Total Trades Executed", metrics.get("total_trades_executed", 0)),
        ("Win Rate (%)", metrics.get("win_rate_pct", 0.0)),
        ("Average Edge Captured", metrics.get("average_edge_captured_cents", 0.0)),
        ("Max Drawdown (%)", metrics.get("max_drawdown_pct", 0.0)),
        ("Sharpe Ratio", metrics.get("sharpe_ratio", 0.0)),
    ]
    fig = go.Figure(
        data=[
            go.Table(
                header={"values": ["Metric", "Value"], "fill_color": "#dbe8ef", "align": "left"},
                cells={
                    "values": [[r[0] for r in rows], [r[1] for r in rows]],
                    "fill_color": "#f4f8fb",
                    "align": "left",
                },
            )
        ]
    )
    fig.update_layout(
        title="Performance Tearsheet",
        template="plotly_white",
        margin={"l": 30, "r": 30, "t": 64, "b": 24},
        height=360,
    )
    return fig


def _try_plotly_png(fig: Any, path: Path) -> bool:
    try:
        fig.write_image(str(path), width=int(SIMULATION_PNG_WIDTH), height=int(SIMULATION_PNG_HEIGHT), scale=2)
        return True
    except Exception:
        return False


def generate_visual_assets(sim_result: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(sim_result.get("metrics") or {})
    profit_dollars = [float(x) for x in sim_result.get("equity_curve_profit_dollars") or []]
    return_pct = [float(x) for x in sim_result.get("equity_curve_return_pct") or []]
    edge_samples = [float(x) for x in sim_result.get("edge_samples_cents") or []]

    run_dir = _run_output_dir()
    eq_png = run_dir / "equity_curve.png"
    edge_png = run_dir / "edge_distribution.png"
    tear_png = run_dir / "tearsheet.png"

    divs: dict[str, str] = {}

    if go is not None:
        eq_fig = _plotly_equity_figure(profit_dollars, return_pct)
        edge_fig = _plotly_edge_figure(edge_samples)
        tear_fig = _plotly_tearsheet_figure(metrics)

        divs["equity_curve"] = eq_fig.to_html(full_html=False, include_plotlyjs=False, div_id="simEquityPlot")
        divs["edge_distribution"] = edge_fig.to_html(full_html=False, include_plotlyjs=False, div_id="simEdgePlot")
        divs["tearsheet"] = tear_fig.to_html(full_html=False, include_plotlyjs=False, div_id="simTearsheetPlot")

        if not _try_plotly_png(eq_fig, eq_png):
            _matplotlib_equity_png(eq_png, profit_dollars, return_pct)
        if not _try_plotly_png(edge_fig, edge_png):
            _matplotlib_edge_png(edge_png, edge_samples)
        if not _try_plotly_png(tear_fig, tear_png):
            _matplotlib_tearsheet_png(tear_png, metrics)
    else:
        divs["equity_curve"] = "<div>Plotly unavailable. Using PNG fallback output.</div>"
        divs["edge_distribution"] = "<div>Plotly unavailable. Using PNG fallback output.</div>"
        divs["tearsheet"] = "<div>Plotly unavailable. Using PNG fallback output.</div>"
        _matplotlib_equity_png(eq_png, profit_dollars, return_pct)
        _matplotlib_edge_png(edge_png, edge_samples)
        _matplotlib_tearsheet_png(tear_png, metrics)

    payload = {
        "generated_ts": time.time(),
        "metrics": metrics,
        "divs": divs,
        "images": {
            "equity_curve": str(eq_png),
            "edge_distribution": str(edge_png),
            "tearsheet": str(tear_png),
        },
        "output_dir": str(run_dir),
    }

    (run_dir / "simulation_result.json").write_text(json.dumps(sim_result, indent=2), encoding="utf-8")
    (run_dir / "visual_payload.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload
