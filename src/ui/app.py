from __future__ import annotations

import asyncio
import contextlib
import math
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import reflex as rx

from core.config import ORDERBOOK_VIEW_DEPTH
from engine.trading.runtime import control_trading, submit_manual_order
from engine.trading.settings import reset_trading_settings, update_trading_settings
from feeds.state.tick_store import get_brti_ticks
from ui.services.dashboard_state_service import build_dashboard_state_payload
from ui.services.runtime_services import (
    run_background_services,
    validate_auth_or_exit,
)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _money(cents: Any) -> str:
    value = _number(cents)
    return f"${value / 100:,.2f}" if value is not None else "—"


def _cents(value: Any) -> str:
    number = _number(value)
    return f"{number:.2f}¢" if number is not None else "—"


def _percent(value: Any, *, ratio: bool = False) -> str:
    number = _number(value)
    return f"{number * (100 if ratio else 1):.1f}%" if number is not None else "—"


def _duration(seconds: Any) -> str:
    value = _number(seconds)
    if value is None:
        return "—"
    minutes, whole_seconds = divmod(max(0, round(value)), 60)
    return f"{minutes}m {whole_seconds:02d}s" if minutes else f"{whole_seconds}s"


def _book_rows(book: dict[str, Any], side: str) -> list[dict[str, str]]:
    levels = [
        *(('ask', level) for level in reversed(book.get(f"{side}_asks") or [])),
        *(('bid', level) for level in book.get(f"{side}_bids") or []),
    ]
    maximum = max((float(level[1]) for _, level in levels), default=1.0)
    return [
        {
            "side": label.upper(),
            "tone": label,
            "price": _cents(level[0]),
            "size": f"{float(level[1]):,.0f}",
            "depth": f"{max(4, float(level[1]) / maximum * 100):.1f}%",
        }
        for label, level in levels
    ]


def moving_average(rows: list[dict[str, Any]], window_seconds: int) -> list[dict[str, Any]]:
    points = sorted(
        (
            (float(row["ts"]), float(row["brti"]))
            for row in rows
            if _number(row.get("ts")) is not None and _number(row.get("brti")) is not None
        ),
        key=lambda point: point[0],
    )
    result: list[dict[str, Any]] = []
    left = 0
    running_sum = 0.0
    for right, (timestamp, spot) in enumerate(points):
        running_sum += spot
        while timestamp - points[left][0] > window_seconds:
            running_sum -= points[left][1]
            left += 1
        result.append(
            {
                "time": datetime.fromtimestamp(timestamp).strftime("%H:%M:%S"),
                "spot": round(spot, 2),
                "average": round(running_sum / (right - left + 1), 2),
            }
        )
    return result


class DashboardState(rx.State):
    asset = "—"
    market = "Waiting for market"
    runtime_status = "Stopped"
    runtime_detail = "Choose Paper or Live to begin."
    selected_mode = "paper"
    armed = False
    pnl = "$0.00"
    pnl_tone = "flat"

    cash = "—"
    portfolio = "—"
    equity = "—"
    positions: list[dict[str, str]] = []

    spot = "—"
    strike = "—"
    expires = "—"
    volatility = "—"
    probability = "—"
    implied = "—"
    edge = "—"
    lean = "—"
    signal = "Model warming up…"
    model_history: list[dict[str, Any]] = []
    price_history: list[dict[str, Any]] = []
    yes_book: list[dict[str, str]] = []
    no_book: list[dict[str, str]] = []
    book_status = "Connecting"

    min_edge = "5"
    max_order = "5"
    max_position = "10"
    max_daily_loss = "10"
    cash_buffer = "25"
    cooldown = "5"
    slippage = "1"
    vol_override = ""
    vol_scale = "1"
    settings_status = ""

    manual_action = "buy"
    manual_side = "yes"
    manual_count = "1"
    manual_quote = "Waiting for a live quote."
    manual_status = "Start Paper or Live to enable orders."
    can_submit = False

    _snapshot: dict[str, Any] = {}
    _settings: dict[str, Any] = {}

    def _apply_settings(self, settings: dict[str, Any]) -> None:
        self._settings = settings
        self.min_edge = str(settings.get("min_edge_cents", 5))
        self.max_order = str(settings.get("max_order_contracts", 5))
        self.max_position = str(settings.get("max_position_usd", 10))
        self.max_daily_loss = str(settings.get("max_daily_loss_usd", 10))
        self.cash_buffer = str(settings.get("cash_buffer_usd", 25))
        self.cooldown = str(settings.get("cooldown_seconds", 5))
        self.slippage = str(settings.get("slippage_ticks", 1))
        override = settings.get("volatility_override")
        self.vol_override = "" if override is None else str(override)
        self.vol_scale = str(settings.get("volatility_scale", 1))

    def _refresh_manual_quote(self) -> None:
        book = self._snapshot.get("orderbook") or {}
        runtime = self._snapshot.get("trading_runtime") or {}
        risk = runtime.get("daily_risk") or {}
        key = f"{self.manual_side}_{'asks' if self.manual_action == 'buy' else 'bids'}"
        levels = book.get(key) or []
        quote = levels[0][0] if levels else None
        pricing = self._snapshot.get("pricing") or {}
        probability = _number(pricing.get("p_model"))
        fair = None if probability is None else (probability if self.manual_side == "yes" else 1 - probability) * 100
        held = next(
            (
                item.get("contracts", 0)
                for item in (self._snapshot.get("account") or {}).get("positions", [])
                if item.get("market_ticker") == runtime.get("current_market_ticker")
                and item.get("side") == self.manual_side
            ),
            0,
        )
        quote_name = "ask" if self.manual_action == "buy" else "bid"
        self.manual_quote = (
            f"{self.manual_side.upper()} {quote_name} {_cents(quote)} · fair {_cents(fair)} · held {held:g}"
            if _number(quote) is not None
            else f"No current {self.manual_side.upper()} {quote_name}."
        )
        self.can_submit = bool(runtime.get("armed") and book.get("initialized") and not risk.get("locked") and quote is not None)
        if not runtime.get("armed"):
            self.manual_status = "Start Paper or Live to enable orders."
        elif risk.get("locked"):
            self.manual_status = "Daily-loss guard is locked."
        elif quote is None:
            self.manual_status = "Waiting for a current quote."
        else:
            self.manual_status = "Ready for immediate-or-cancel execution."

    def _refresh(self) -> None:
        try:
            state = build_dashboard_state_payload(depth=ORDERBOOK_VIEW_DEPTH)
            self._snapshot = state
            runtime = state.get("trading_runtime") or {}
            risk = runtime.get("daily_risk") or {}
            account = state.get("account") or runtime.get("account") or {}
            pricing = state.get("pricing") or {}
            monologue = state.get("signal_monologue") or {}
            book = state.get("orderbook") or {}
            brti = state.get("brti") or {}

            self.asset = str(state.get("asset_display") or state.get("asset") or "—")
            self.market = str(runtime.get("current_market_ticker") or book.get("market_ticker") or "Waiting for market")
            self.armed = bool(runtime.get("armed"))
            if runtime.get("execution_mode") in {"paper", "live"}:
                self.selected_mode = runtime["execution_mode"]
            self.runtime_status = "Risk locked" if risk.get("locked") else "Trading" if self.armed else "Stopped"
            self.runtime_detail = str(runtime.get("last_error") or runtime.get("last_reason") or self.runtime_status)
            drawdown = _number(risk.get("drawdown_cents")) or 0.0
            self.pnl = _money(drawdown)
            self.pnl_tone = "positive" if drawdown > 0 else "negative" if drawdown < 0 else "flat"

            self.cash = _money(account.get("cash_cents"))
            self.portfolio = _money(account.get("portfolio_value_cents"))
            self.equity = _money(account.get("equity_cents"))
            self.positions = [
                {
                    "side": str(position.get("side") or "—").upper(),
                    "contracts": f"{float(position.get('contracts') or 0):g}",
                    "average": _cents(position.get("avg_entry_cents")),
                    "exposure": _money(position.get("market_exposure_cents")),
                    "realized": _money(position.get("realized_pnl_cents")),
                }
                for position in account.get("positions") or []
            ]

            spot = _number(brti.get("brti"))
            strike = _number(pricing.get("strike_usd") or state.get("suggested_strike"))
            self.spot = f"${spot:,.2f}" if spot is not None else "—"
            self.strike = f"${strike:,.0f}" if strike is not None else "—"
            self.expires = _duration(pricing.get("seconds_to_expiry"))
            self.volatility = _percent(pricing.get("sigma_annual"), ratio=True)
            self.probability = _percent(pricing.get("p_model_pct")) if pricing.get("ready") else "—"
            self.implied = _percent(monologue.get("market_implied_probability"), ratio=True)
            self.edge = _cents(monologue.get("best_edge_cents"))
            self.lean = str(monologue.get("lean_side") or "—").upper()
            self.signal = str(monologue.get("action_intent") or "Model evaluating market…")
            probability = _number(pricing.get("p_model_pct"))
            if pricing.get("ready") and probability is not None:
                point = {"time": datetime.now().strftime("%H:%M:%S"), "probability": round(probability, 2)}
                if not self.model_history or self.model_history[-1] != point:
                    self.model_history = [*self.model_history[-179:], point]
            window = int(state.get("settlement_window_seconds") or 60)
            self.price_history = moving_average(get_brti_ticks(limit=200), window)
            self.yes_book = _book_rows(book, "yes")
            self.no_book = _book_rows(book, "no")
            self.book_status = "Live depth" if book.get("initialized") else "Waiting for bootstrap"
            if not self._settings:
                self._apply_settings(state.get("trading_settings") or {})
            self._refresh_manual_quote()
        except Exception as exc:
            self.runtime_status = "Disconnected"
            self.runtime_detail = str(exc)

    @rx.event
    def load(self) -> None:
        self._refresh()

    @rx.event
    def refresh(self, _date: str) -> None:
        self._refresh()

    @rx.event
    def choose_mode(self, mode: str) -> None:
        if mode in {"paper", "live"}:
            self.selected_mode = mode

    def _control(self, operation: str) -> None:
        try:
            result = control_trading(operation, self.selected_mode if operation == "start" else "")
            self.settings_status = f"Trading {result.get('status', operation)}."
        except (ValueError, RuntimeError) as exc:
            self.settings_status = str(exc)
        self._refresh()

    @rx.event
    def start(self) -> None:
        self._control("start")

    @rx.event
    def stop(self) -> None:
        self._control("pause")

    @rx.event
    def flatten(self) -> None:
        self._control("flatten")

    @rx.event
    def save_settings(self) -> None:
        try:
            settings, errors = update_trading_settings(
                {
                    "min_edge_cents": float(self.min_edge),
                    "max_order_contracts": int(self.max_order),
                    "max_position_usd": float(self.max_position),
                    "max_daily_loss_usd": float(self.max_daily_loss),
                    "cash_buffer_usd": float(self.cash_buffer),
                    "cooldown_seconds": int(self.cooldown),
                    "slippage_ticks": int(self.slippage),
                    "volatility_override": self.vol_override or None,
                    "volatility_scale": float(self.vol_scale),
                    "use_p_book_hard_gate": bool(self._settings.get("use_p_book_hard_gate", False)),
                    "p_book_max_divergence": float(self._settings.get("p_book_max_divergence", 0.35)),
                }
            )
            if errors:
                raise ValueError(" · ".join(errors))
            self._apply_settings(settings)
            self.settings_status = "Settings saved."
        except (TypeError, ValueError) as exc:
            self.settings_status = str(exc)

    @rx.event
    def reset_settings(self) -> None:
        self._apply_settings(reset_trading_settings())
        self.settings_status = "Conservative defaults restored."

    @rx.event
    def set_min_edge(self, value: str) -> None:
        self.min_edge = value

    @rx.event
    def set_max_order(self, value: str) -> None:
        self.max_order = value

    @rx.event
    def set_max_position(self, value: str) -> None:
        self.max_position = value

    @rx.event
    def set_max_daily_loss(self, value: str) -> None:
        self.max_daily_loss = value

    @rx.event
    def set_cash_buffer(self, value: str) -> None:
        self.cash_buffer = value

    @rx.event
    def set_cooldown(self, value: str) -> None:
        self.cooldown = value

    @rx.event
    def set_slippage(self, value: str) -> None:
        self.slippage = value

    @rx.event
    def set_vol_override(self, value: str) -> None:
        self.vol_override = value

    @rx.event
    def set_vol_scale(self, value: str) -> None:
        self.vol_scale = value

    @rx.event
    def set_manual_count(self, value: str) -> None:
        self.manual_count = value

    @rx.event
    def choose_action(self, value: str) -> None:
        self.manual_action = value
        self._refresh_manual_quote()

    @rx.event
    def choose_side(self, value: str) -> None:
        self.manual_side = value
        self._refresh_manual_quote()

    @rx.event
    def submit_order(self) -> None:
        try:
            result = submit_manual_order(
                side=self.manual_side,
                action=self.manual_action,
                count=self.manual_count,
            )
            self.manual_status = f"Order {result.get('status', 'submitted')}."
        except (ValueError, RuntimeError) as exc:
            self.manual_status = str(exc)
        self._refresh()


def _panel_header(kicker: str, title: str, trailing: rx.Component | None = None) -> rx.Component:
    return rx.hstack(
        rx.box(rx.text(kicker, class_name="kicker"), rx.heading(title, as_="h2")),
        trailing or rx.fragment(),
        class_name="panel-header",
    )


def _metric(label: str, value: Any, *, large: bool = False) -> rx.Component:
    return rx.box(
        rx.text(label, class_name="metric-label"),
        rx.text(value, class_name="metric-value hero-value" if large else "metric-value"),
        class_name="metric",
    )


def _book_row(row: dict[str, Any]) -> rx.Component:
    return rx.el.tr(
        rx.el.td(rx.text(row["side"], class_name=row["tone"])),
        rx.el.td(row["price"]),
        rx.el.td(
            rx.box(class_name="depth-fill", width=row["depth"]),
            rx.text(row["size"], class_name="depth-value"),
            class_name="depth-cell",
        ),
    )


def _book(title: str, rows: Any, tone: str) -> rx.Component:
    return rx.box(
        rx.hstack(rx.heading(title, as_="h3"), rx.text("PRICE / SIZE"), class_name=f"book-title {tone}"),
        rx.el.table(
            rx.el.thead(rx.el.tr(rx.el.th("Side"), rx.el.th("Price"), rx.el.th("Size"))),
            rx.el.tbody(rx.foreach(rows, _book_row)),
            class_name="data-table book-table",
        ),
        class_name="book-side",
    )


def _position_row(position: dict[str, Any]) -> rx.Component:
    return rx.el.tr(
        rx.el.td(position["side"]),
        rx.el.td(position["contracts"]),
        rx.el.td(position["average"]),
        rx.el.td(position["exposure"]),
        rx.el.td(position["realized"]),
    )


def _field(label: str, value: Any, handler: Any, **props: Any) -> rx.Component:
    return rx.el.label(
        rx.text(label),
        rx.input(value=value, on_change=handler, type="number", **props),
        class_name="field",
    )


def _chart(data: Any, lines: list[rx.Component], *, domain: list[int] | None = None) -> rx.Component:
    return rx.recharts.line_chart(
        rx.recharts.cartesian_grid(stroke="#253137", stroke_dasharray="2 5", vertical=False),
        rx.recharts.x_axis(data_key="time", axis_line=False, tick_line=False, min_tick_gap=44),
        rx.recharts.y_axis(domain=domain or ["auto", "auto"], axis_line=False, tick_line=False, width=54),
        rx.recharts.graphing_tooltip(),
        *lines,
        data=data,
        height=230,
        width="100%",
        margin={"top": 10, "right": 12, "bottom": 0, "left": 0},
    )


def index() -> rx.Component:
    return rx.box(
        rx.moment(interval=1000, on_change=DashboardState.refresh.temporal, display="none"),
        rx.box(
            rx.el.header(
                rx.box(
                    rx.text("KALSHI / 15 MINUTE CRYPTO", class_name="eyebrow"),
                    rx.heading("Operator Console", as_="h1"),
                ),
                rx.hstack(
                    _metric("Asset", DashboardState.asset),
                    _metric("Session P&L", DashboardState.pnl),
                    _metric("Status", DashboardState.runtime_status),
                    class_name="session-strip",
                ),
                rx.hstack(
                    rx.button("Paper", on_click=lambda: DashboardState.choose_mode("paper"), class_name=rx.cond(DashboardState.selected_mode == "paper", "mode active", "mode")),
                    rx.button("Live", on_click=lambda: DashboardState.choose_mode("live"), class_name=rx.cond(DashboardState.selected_mode == "live", "mode active live", "mode")),
                    rx.button("Start", on_click=DashboardState.start, disabled=DashboardState.armed, class_name="button start"),
                    rx.button("Stop", on_click=DashboardState.stop, disabled=~DashboardState.armed, class_name="button stop"),
                    rx.button("Flatten", on_click=DashboardState.flatten, class_name="button danger"),
                    class_name="command-bar",
                ),
                class_name="topbar",
            ),
            rx.el.main(
                rx.box(
                    _panel_header("ACCOUNT", "Positions", rx.text(DashboardState.runtime_detail, class_name="status-note")),
                    rx.box(
                        rx.hstack(
                            _metric("Cash", DashboardState.cash),
                            _metric("Portfolio", DashboardState.portfolio),
                            _metric("Equity", DashboardState.equity),
                            class_name="account-stats",
                        ),
                        rx.box(
                            rx.el.table(
                                rx.el.thead(rx.el.tr(rx.el.th("Outcome"), rx.el.th("Contracts"), rx.el.th("Average"), rx.el.th("Exposure"), rx.el.th("Realized"))),
                                rx.el.tbody(
                                    rx.cond(
                                        DashboardState.positions.length() > 0,
                                        rx.foreach(DashboardState.positions, _position_row),
                                        rx.el.tr(rx.el.td("No open positions", col_span=5, class_name="empty")),
                                    )
                                ),
                                class_name="data-table",
                            ),
                            class_name="positions",
                        ),
                        class_name="account-layout",
                    ),
                    class_name="surface account-panel",
                ),
                rx.box(
                    rx.box(
                        _panel_header("DECISION VIEW", "Market & Model", rx.text(DashboardState.market, class_name="market-tag")),
                        rx.box(
                            rx.box(
                                _metric("Composite index", DashboardState.spot, large=True),
                                rx.hstack(_metric("Strike", DashboardState.strike), _metric("Expires", DashboardState.expires), _metric("Volatility", DashboardState.volatility), class_name="market-metrics"),
                                class_name="spot-block",
                            ),
                            rx.box(
                                rx.hstack(_metric("Model probability", DashboardState.probability, large=True), rx.text(DashboardState.lean, class_name="lean-pill"), class_name="probability-row"),
                                rx.hstack(_metric("Market implied", DashboardState.implied), _metric("Net edge", DashboardState.edge), class_name="model-metrics"),
                                rx.text(DashboardState.signal, class_name="signal"),
                                class_name="model-block",
                            ),
                            class_name="market-overview",
                        ),
                        rx.box(
                            rx.box(
                                rx.heading("Model history", as_="h3"),
                                _chart(DashboardState.model_history, [rx.recharts.line(data_key="probability", stroke="#7cf4c2", stroke_width=2, dot=False, type_="monotone")], domain=[0, 100]),
                                class_name="chart",
                            ),
                            rx.box(
                                rx.heading("Index / settlement average", as_="h3"),
                                _chart(DashboardState.price_history, [rx.recharts.line(data_key="spot", stroke="#d8f6ea", stroke_width=2, dot=False, type_="monotone"), rx.recharts.line(data_key="average", stroke="#e9b872", stroke_width=2, dot=False, type_="monotone")]),
                                class_name="chart",
                            ),
                            class_name="charts",
                        ),
                        class_name="surface market-panel",
                    ),
                    rx.box(
                        _panel_header("LIQUIDITY", "Order book", rx.text(DashboardState.book_status, class_name="status-note")),
                        rx.box(_book("YES", DashboardState.yes_book, "yes"), _book("NO", DashboardState.no_book, "no"), class_name="book-grid"),
                        class_name="surface book-panel",
                    ),
                    class_name="workspace",
                ),
                rx.box(
                    _panel_header("EXECUTION", "Operator controls"),
                    rx.box(
                        rx.box(
                            rx.heading("Policy & risk", as_="h3"),
                            rx.box(
                                _field("Minimum edge (¢)", DashboardState.min_edge, DashboardState.set_min_edge, step="0.5", min="0.5", max="25"),
                                _field("Max order", DashboardState.max_order, DashboardState.set_max_order, step="1", min="1", max="25"),
                                _field("Max position ($)", DashboardState.max_position, DashboardState.set_max_position, step="1", min="1", max="50"),
                                _field("Daily loss limit ($)", DashboardState.max_daily_loss, DashboardState.set_max_daily_loss, step="1", min="1", max="100"),
                                _field("Cash buffer ($)", DashboardState.cash_buffer, DashboardState.set_cash_buffer, step="1", min="0"),
                                _field("Cooldown (s)", DashboardState.cooldown, DashboardState.set_cooldown, step="1", min="1", max="900"),
                                _field("IOC slippage", DashboardState.slippage, DashboardState.set_slippage, step="1", min="0", max="5"),
                                _field("Volatility override", DashboardState.vol_override, DashboardState.set_vol_override, step="0.01", min="0.01", max="5", placeholder="Realized"),
                                _field("Volatility scale", DashboardState.vol_scale, DashboardState.set_vol_scale, step="0.05", min="0.5", max="2"),
                                class_name="control-grid",
                            ),
                            rx.hstack(
                                rx.button("Save settings", on_click=DashboardState.save_settings, class_name="button primary"),
                                rx.button("Reset", on_click=DashboardState.reset_settings, class_name="button"),
                                rx.text(DashboardState.settings_status, class_name="status-note"),
                                class_name="button-row",
                            ),
                            class_name="risk-controls",
                        ),
                        rx.box(
                            rx.text("DISCRETIONARY", class_name="kicker"),
                            rx.heading("Manual IOC", as_="h3"),
                            rx.box(
                                rx.el.label(rx.text("Action"), rx.select(["buy", "sell"], value=DashboardState.manual_action, on_change=DashboardState.choose_action), class_name="field"),
                                rx.el.label(rx.text("Outcome"), rx.select(["yes", "no"], value=DashboardState.manual_side, on_change=DashboardState.choose_side), class_name="field"),
                                _field("Contracts", DashboardState.manual_count, DashboardState.set_manual_count, step="1", min="1"),
                                class_name="ticket-grid",
                            ),
                            rx.text(DashboardState.manual_quote, class_name="quote"),
                            rx.button("Submit IOC", on_click=DashboardState.submit_order, disabled=~DashboardState.can_submit, class_name="button submit"),
                            rx.text(DashboardState.manual_status, class_name="status-note"),
                            class_name="trade-ticket",
                        ),
                        class_name="execution-layout",
                    ),
                    class_name="surface controls-panel",
                ),
                class_name="dashboard",
            ),
            class_name="shell",
        ),
        class_name="page",
    )


@asynccontextmanager
async def runtime_lifespan():
    validate_auth_or_exit()
    task = asyncio.create_task(run_background_services())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = rx.App(stylesheets=["/dashboard.css"])
app.add_page(index, route="/", title="Kalshi Operator Console", on_load=DashboardState.load)
app.register_lifespan_task(runtime_lifespan)
