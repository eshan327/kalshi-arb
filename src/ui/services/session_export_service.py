from __future__ import annotations

import base64
import csv
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from core.asset_context import get_active_asset_context
from core.config import (
    SESSION_EXPORT_CHART_DPI,
    SESSION_EXPORT_DIR,
    SESSION_EXPORT_ENABLED,
    SESSION_EXPORT_MAX_ROWS,
)
from engine.book_microstructure import get_last_p_book_snapshot
from engine.execution.metrics import get_execution_events, get_fill_events, get_runtime_stats_snapshot
from engine.execution.runtime import get_execution_state_snapshot
from engine.stream_metrics import get_reconciliation_log, get_top10_impact_log, get_ws_message_log
from engine.streamer import get_live_market_info
from feeds.brti_aggregator import get_brti_state, get_brti_ticks

_session_start_ts: float = time.time()


def mark_session_started(now_ts: float | None = None) -> None:
    global _session_start_ts
    _session_start_ts = time.time() if now_ts is None else float(now_ts)


def get_session_start_ts() -> float:
    return float(_session_start_ts)


def _safe_session_name(label: str) -> str:
    cleaned = []
    for ch in str(label):
        if ch.isalnum() or ch in {"-", "_"}:
            cleaned.append(ch)
        else:
            cleaned.append("-")
    return "".join(cleaned).strip("-") or "session"


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True))
            handle.write("\n")


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name) for name in fieldnames})


def _plot_line_chart(
    *,
    path: Path,
    title: str,
    x_values: list[float],
    y_values: list[float],
    color: str,
    y_label: str,
) -> None:
    fig, ax = plt.subplots(figsize=(12, 5), dpi=SESSION_EXPORT_CHART_DPI)
    if x_values and y_values:
        ax.plot(x_values, y_values, color=color, linewidth=2.2)
    else:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
    ax.set_title(title)
    ax.set_xlabel("Unix Time")
    ax.set_ylabel(y_label)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, format="png")
    plt.close(fig)


def _plot_confidence_hist(path: Path, bins: dict[str, Any]) -> None:
    labels = list(bins.keys())
    values = [int(bins.get(label, 0)) for label in labels]
    fig, ax = plt.subplots(figsize=(12, 5), dpi=SESSION_EXPORT_CHART_DPI)
    if labels:
        ax.bar(labels, values, color="#2ea56a")
        ax.tick_params(axis="x", rotation=45)
    else:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
    ax.set_title("Confidence Distribution")
    ax.set_xlabel("Confidence Bin")
    ax.set_ylabel("Count")
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(path, format="png")
    plt.close(fig)


def _plot_category_bars(
    path: Path,
    *,
    title: str,
    category_map: dict[str, Any],
    x_label: str,
    y_label: str,
    color: str,
) -> None:
    items = [(str(key), int(value or 0)) for key, value in (category_map or {}).items() if int(value or 0) > 0]
    items.sort(key=lambda item: item[1], reverse=True)
    labels = [item[0] for item in items]
    values = [item[1] for item in items]

    fig, ax = plt.subplots(figsize=(12, 5), dpi=SESSION_EXPORT_CHART_DPI)
    if labels:
        ax.bar(labels, values, color=color)
        ax.tick_params(axis="x", rotation=35)
    else:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
    ax.set_title(title)
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(path, format="png")
    plt.close(fig)


def _plot_timing_scatter(path: Path, events: list[dict[str, Any]]) -> None:
    xs = []
    ys = []
    colors = []
    for event in events:
        sec = event.get("seconds_to_expiry")
        conf = event.get("confidence")
        if not isinstance(sec, (int, float)) or not isinstance(conf, (int, float)):
            continue
        xs.append(float(sec))
        ys.append(float(conf))
        colors.append("#42c67b" if str(event.get("side")) == "yes" else "#cc6f6f")

    fig, ax = plt.subplots(figsize=(12, 5), dpi=SESSION_EXPORT_CHART_DPI)
    if xs and ys:
        ax.scatter(xs, ys, c=colors, s=12, alpha=0.75)
    else:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
    ax.set_title("Order Timing and Confidence")
    ax.set_xlabel("Seconds to Expiry")
    ax.set_ylabel("Confidence")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, format="png")
    plt.close(fig)


def _plot_brti_history(path: Path, ticks: list[dict[str, Any]]) -> None:
    points = [
        tick
        for tick in ticks
        if isinstance(tick.get("ts"), (int, float)) and isinstance(tick.get("brti"), (int, float))
    ]
    xs = [float(tick["ts"]) for tick in points]
    ys = [float(tick["brti"]) for tick in points]

    fig, ax = plt.subplots(figsize=(12, 5), dpi=SESSION_EXPORT_CHART_DPI)
    if xs and ys:
        ax.plot(xs, ys, color="#5aa4ea", linewidth=2.0)
    else:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes)
    ax.set_title("Index Price History")
    ax.set_xlabel("Unix Time")
    ax.set_ylabel("Index")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, format="png")
    plt.close(fig)


def _embed_png(path: Path) -> str:
    if not path.exists():
        return ""
    data = path.read_bytes()
    return base64.b64encode(data).decode("ascii")


def _build_html_report(*, output_path: Path, summary: dict[str, Any], chart_paths: dict[str, Path]) -> None:
    chart_imgs = {name: _embed_png(path) for name, path in chart_paths.items()}
    html = f"""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <title>Kalshi Arb Session Report</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; margin: 24px; color: #1e2a36; }}
    h1, h2 {{ margin-bottom: 8px; }}
    .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px; margin-bottom: 20px; }}
    .card {{ border: 1px solid #d7dee9; border-radius: 10px; padding: 10px; background: #f7f9fb; }}
    .label {{ font-size: 12px; color: #687588; margin-bottom: 4px; }}
    .value {{ font-size: 18px; font-weight: 600; }}
    .chart {{ margin-bottom: 18px; }}
    .chart img {{ width: 100%; height: auto; border: 1px solid #d7dee9; border-radius: 8px; }}
  </style>
</head>
<body>
  <h1>Kalshi Arb Session Report</h1>
  <div class=\"summary\">
    <div class=\"card\"><div class=\"label\">Mode</div><div class=\"value\">{summary.get('mode')}</div></div>
    <div class=\"card\"><div class=\"label\">Realized PnL</div><div class=\"value\">{summary.get('pnl_dollars')}</div></div>
    <div class=\"card\"><div class=\"label\">Win Rate</div><div class=\"value\">{summary.get('win_rate')}</div></div>
    <div class=\"card\"><div class=\"label\">Settled Trades</div><div class=\"value\">{summary.get('settled_trades')}</div></div>
    <div class=\"card\"><div class=\"label\">Fills</div><div class=\"value\">{summary.get('fills_total')}</div></div>
    <div class=\"card\"><div class=\"label\">Edge Captured</div><div class=\"value\">{summary.get('edge_captured_cents')}</div></div>
  </div>

  <h2>Charts</h2>
  <div class=\"chart\"><h3>PnL Curve</h3>{f'<img src="data:image/png;base64,{chart_imgs.get("pnl_curve", "")}" />' if chart_imgs.get('pnl_curve') else '<p>No chart data.</p>'}</div>
  <div class=\"chart\"><h3>Win Rate Curve</h3>{f'<img src="data:image/png;base64,{chart_imgs.get("win_rate_curve", "")}" />' if chart_imgs.get('win_rate_curve') else '<p>No chart data.</p>'}</div>
  <div class=\"chart\"><h3>Edge Curve</h3>{f'<img src="data:image/png;base64,{chart_imgs.get("edge_curve", "")}" />' if chart_imgs.get('edge_curve') else '<p>No chart data.</p>'}</div>
  <div class=\"chart\"><h3>Confidence Histogram</h3>{f'<img src="data:image/png;base64,{chart_imgs.get("confidence_hist", "")}" />' if chart_imgs.get('confidence_hist') else '<p>No chart data.</p>'}</div>
  <div class=\"chart\"><h3>Timing Scatter</h3>{f'<img src="data:image/png;base64,{chart_imgs.get("timing_scatter", "")}" />' if chart_imgs.get('timing_scatter') else '<p>No chart data.</p>'}</div>
  <div class=\"chart\"><h3>Index History</h3>{f'<img src="data:image/png;base64,{chart_imgs.get("index_history", "")}" />' if chart_imgs.get('index_history') else '<p>No chart data.</p>'}</div>
    <div class="chart"><h3>Policy Rejection Reasons</h3>{f'<img src="data:image/png;base64,{chart_imgs.get("rejection_reasons", "")}" />' if chart_imgs.get('rejection_reasons') else '<p>No chart data.</p>'}</div>
    <div class="chart"><h3>Maker/Taker Fill Mix (window)</h3>{f'<img src="data:image/png;base64,{chart_imgs.get("maker_taker_mix", "")}" />' if chart_imgs.get('maker_taker_mix') else '<p>No chart data.</p>'}</div>
    <div class="chart"><h3>Fill Latency Distribution</h3>{f'<img src="data:image/png;base64,{chart_imgs.get("fill_latency", "")}" />' if chart_imgs.get('fill_latency') else '<p>No chart data.</p>'}</div>
    <div class="chart"><h3>Fill Spread Distribution</h3>{f'<img src="data:image/png;base64,{chart_imgs.get("fill_spread", "")}" />' if chart_imgs.get('fill_spread') else '<p>No chart data.</p>'}</div>
    <div class="chart"><h3>Per-Window Fill Rate</h3>{f'<img src="data:image/png;base64,{chart_imgs.get("window_fill_rate", "")}" />' if chart_imgs.get('window_fill_rate') else '<p>No chart data.</p>'}</div>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")


def _format_money(cents: Any) -> str:
    if not isinstance(cents, (int, float)):
        return "--"
    return f"${float(cents) / 100.0:,.2f}"


def _session_folder(base: Path, asset: str) -> tuple[str, Path]:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    session_id = f"{stamp}_{_safe_session_name(asset)}"
    return session_id, base / session_id


def export_current_session(reason: str = "manual") -> dict[str, Any]:
    if not SESSION_EXPORT_ENABLED:
        return {"ok": False, "error": "Session export is disabled."}

    end_ts = time.time()
    start_ts = get_session_start_ts()
    asset_ctx = get_active_asset_context()
    asset = asset_ctx.profile.asset

    base = Path(os.path.abspath(SESSION_EXPORT_DIR))
    session_id, session_dir = _session_folder(base, asset)
    raw_dir = session_dir / "raw_data"
    charts_dir = session_dir / "charts"
    tables_dir = session_dir / "tables"

    _mkdir(raw_dir)
    _mkdir(charts_dir)
    _mkdir(tables_dir)

    limit = max(200, int(SESSION_EXPORT_MAX_ROWS))
    events = get_execution_events(limit=limit)
    fills = get_fill_events(limit=limit)
    runtime_stats = get_runtime_stats_snapshot(window_seconds=86_400)

    brti_ticks = get_brti_ticks(limit=limit)
    ws_log = get_ws_message_log(limit=limit)
    impact_log = get_top10_impact_log(limit=limit)
    recon_log = get_reconciliation_log(limit=limit)

    metadata = {
        "session_id": session_id,
        "reason": reason,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "duration_seconds": max(0.0, end_ts - start_ts),
        "asset": asset,
        "asset_display": asset_ctx.profile.display_name,
        "env": os.getenv("KALSHI_ENV", "demo"),
        "mode": get_execution_state_snapshot().get("mode"),
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }

    summary = {
        "mode": get_execution_state_snapshot().get("mode"),
        "pnl_dollars": _format_money(runtime_stats.get("pnl_cents")),
        "win_rate": runtime_stats.get("win_rate"),
        "settled_trades": runtime_stats.get("settled_trades"),
        "fills_total": runtime_stats.get("fills_total"),
        "maker_fills_window": runtime_stats.get("maker_fills_window"),
        "taker_fills_window": runtime_stats.get("taker_fills_window"),
        "fallback_fills_window": runtime_stats.get("fallback_fills_window"),
        "window_count_settled": runtime_stats.get("window_count_settled"),
        "window_count_with_fill": runtime_stats.get("window_count_with_fill"),
        "edge_captured_cents": runtime_stats.get("edge_captured_cents"),
        "daily_pnl_dollars": _format_money(runtime_stats.get("daily_pnl_cents")),
        "paper": runtime_stats.get("paper"),
    }

    _write_json(session_dir / "session_metadata.json", metadata)
    _write_json(session_dir / "performance_summary.json", summary)
    _write_json(session_dir / "runtime_stats.json", runtime_stats)

    _write_jsonl(raw_dir / "execution_events.jsonl", events)
    _write_jsonl(raw_dir / "fill_events.jsonl", fills)
    _write_jsonl(raw_dir / "brti_ticks.jsonl", brti_ticks)
    _write_jsonl(raw_dir / "ws_log.jsonl", ws_log)
    _write_jsonl(raw_dir / "top10_impact.jsonl", impact_log)
    _write_jsonl(raw_dir / "reconciliation.jsonl", recon_log)

    fill_fields = [
        "ts",
        "ticker",
        "side",
        "action",
        "count",
        "yes_price",
        "no_price",
        "expected_edge_cents",
        "is_taker",
        "execution_intent",
        "is_fallback_attempt",
        "fill_latency_ms",
        "spread_cents",
        "price_deviation_cents",
        "window_id",
    ]
    decision_rows = [row for row in events if str(row.get("kind")) == "decision"]
    decision_fields = [
        "ts",
        "reason",
        "rejection_reason",
        "has_signal",
        "market_ticker",
        "side",
        "count",
        "quote_price_cents",
        "execution_intent",
        "is_fallback_attempt",
        "policy_profile",
        "edge_cents",
        "confidence",
        "p_model",
        "p_book",
        "p_book_quality",
        "p_book_alignment",
        "seconds_to_expiry",
        "timing_score",
    ]

    window_participation_rows = runtime_stats.get("window_participation_recent") or []
    window_summary_rows = runtime_stats.get("window_summaries") or []

    _write_csv(tables_dir / "fills_summary.csv", fills, fill_fields)
    _write_csv(tables_dir / "execution_decisions.csv", decision_rows, decision_fields)
    _write_csv(
        tables_dir / "window_participation.csv",
        window_participation_rows,
        [
            "ts",
            "window_id",
            "market_ticker",
            "seconds_to_expiry",
            "attempts",
            "fallback_attempts",
            "has_fill",
        ],
    )
    _write_csv(
        tables_dir / "window_summaries.csv",
        window_summary_rows,
        [
            "window_id",
            "market_ticker",
            "started_ts",
            "ended_ts",
            "attempts",
            "fallback_attempts",
            "has_fill",
            "fallback_triggered",
            "settlement_realized_delta_cents",
            "finalize_reason",
        ],
    )

    pnl_curve = runtime_stats.get("pnl_curve") or []
    win_rate_curve = runtime_stats.get("win_rate_curve") or []
    edge_curve = runtime_stats.get("edge_curve") or []

    chart_paths = {
        "pnl_curve": charts_dir / "pnl_curve.png",
        "win_rate_curve": charts_dir / "win_rate_curve.png",
        "edge_curve": charts_dir / "edge_curve.png",
        "confidence_hist": charts_dir / "confidence_hist.png",
        "timing_scatter": charts_dir / "timing_scatter.png",
        "index_history": charts_dir / "index_history.png",
        "rejection_reasons": charts_dir / "rejection_reasons.png",
        "maker_taker_mix": charts_dir / "maker_taker_mix.png",
        "fill_latency": charts_dir / "fill_latency.png",
        "fill_spread": charts_dir / "fill_spread.png",
        "window_fill_rate": charts_dir / "window_fill_rate.png",
    }

    _plot_line_chart(
        path=chart_paths["pnl_curve"],
        title="Realized PnL Curve",
        x_values=[float(item.get("ts", 0.0)) for item in pnl_curve],
        y_values=[float(item.get("value", 0.0)) / 100.0 for item in pnl_curve],
        color="#39b27a",
        y_label="PnL ($)",
    )
    _plot_line_chart(
        path=chart_paths["win_rate_curve"],
        title="Win Rate Curve",
        x_values=[float(item.get("ts", 0.0)) for item in win_rate_curve],
        y_values=[float(item.get("value", 0.0)) * 100.0 for item in win_rate_curve],
        color="#d7a53d",
        y_label="Win Rate (%)",
    )
    _plot_line_chart(
        path=chart_paths["edge_curve"],
        title="Expected Edge Capture",
        x_values=[float(item.get("ts", 0.0)) for item in edge_curve],
        y_values=[float(item.get("value", 0.0)) for item in edge_curve],
        color="#4f9fdf",
        y_label="Edge (cents)",
    )
    _plot_confidence_hist(chart_paths["confidence_hist"], runtime_stats.get("confidence_bins") or {})
    _plot_timing_scatter(chart_paths["timing_scatter"], runtime_stats.get("order_timing_events") or [])
    _plot_brti_history(chart_paths["index_history"], brti_ticks)
    _plot_category_bars(
        chart_paths["rejection_reasons"],
        title="Policy Rejection Reasons",
        category_map=runtime_stats.get("rejection_reasons") or {},
        x_label="Reason",
        y_label="Count",
        color="#d67272",
    )
    _plot_category_bars(
        chart_paths["maker_taker_mix"],
        title="Maker vs Taker Fills (window)",
        category_map={
            "maker": runtime_stats.get("maker_fills_window") or 0,
            "taker": runtime_stats.get("taker_fills_window") or 0,
            "fallback": runtime_stats.get("fallback_fills_window") or 0,
        },
        x_label="Execution Intent",
        y_label="Fill Count",
        color="#4fb68d",
    )
    _plot_category_bars(
        chart_paths["fill_latency"],
        title="Fill Latency Distribution",
        category_map=runtime_stats.get("fill_latency_bins") or {},
        x_label="Latency Bin",
        y_label="Count",
        color="#e2b35f",
    )
    _plot_category_bars(
        chart_paths["fill_spread"],
        title="Fill Spread Distribution",
        category_map=runtime_stats.get("fill_spread_bins") or {},
        x_label="Spread Bin",
        y_label="Count",
        color="#6ea4e6",
    )
    window_fill_rate_curve = runtime_stats.get("window_fill_rate_curve") or []
    _plot_line_chart(
        path=chart_paths["window_fill_rate"],
        title="Per-Window Fill Rate",
        x_values=[float(item.get("ts", 0.0)) for item in window_fill_rate_curve],
        y_values=[float(item.get("value", 0.0)) * 100.0 for item in window_fill_rate_curve],
        color="#4fb68d",
        y_label="Fill Rate (%)",
    )

    _build_html_report(
        output_path=session_dir / "report.html",
        summary=summary,
        chart_paths=chart_paths,
    )

    # Convenience bundle for quick inspection without opening every file.
    _write_json(
        session_dir / "snapshot.json",
        {
            "market_info": get_live_market_info(),
            "brti_state": get_brti_state(),
            "microstructure": get_last_p_book_snapshot(),
            "execution_state": get_execution_state_snapshot(),
            "runtime_stats": runtime_stats,
        },
    )

    return {
        "ok": True,
        "session_id": session_id,
        "path": str(session_dir),
        "events": len(events),
        "fills": len(fills),
        "brti_ticks": len(brti_ticks),
        "ws_log": len(ws_log),
    }


def list_exported_sessions(limit: int = 100) -> list[dict[str, Any]]:
    base = Path(os.path.abspath(SESSION_EXPORT_DIR))
    if not base.exists():
        return []

    sessions: list[dict[str, Any]] = []
    for entry in sorted(base.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        metadata_path = entry / "session_metadata.json"
        summary_path = entry / "performance_summary.json"

        metadata = None
        summary = None
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                metadata = None

        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                summary = None

        sessions.append(
            {
                "session_id": entry.name,
                "path": str(entry),
                "metadata": metadata,
                "summary": summary,
            }
        )

        if len(sessions) >= max(1, int(limit)):
            break

    return sessions
