from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.config import KALSHI_ENV, PAPER_STARTING_CASH_CENTS
from core.market_metadata import extract_settlement_decimals, extract_suggested_strike
from core.market_profiles import get_market_profile
from data.kalshi_rest import get_market_candlesticks, get_settled_markets
from engine.market_stream.discovery import parse_iso8601_to_epoch
from engine.orderbook import OrderBook
from engine.pricing.pipeline import compute_pricing_snapshot
from engine.trading.fees import taker_fee_cents_per_contract
from engine.trading.models import TradeSignal
from engine.trading.paper import PaperAccount
from engine.trading.settings import TradingSettings
from engine.trading.strategy import (
    apply_pricing_overrides,
    build_trade_signal,
    slipped_price_cents,
)
from research.backtest import _latest_tick, _settlement_state, fetch_cf_feeds

_NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class StrategyDecision:
    market_ticker: str
    eval_ts: float
    seconds_to_expiry: float
    p_model: float
    sigma_annual: float
    yes_bid_cents: float
    yes_ask_cents: float
    no_bid_cents: float
    no_ask_cents: float
    signal_reason: str
    signal_action: str | None
    signal_side: str | None
    signal_count: int | None
    signal_limit_cents: float | None
    signal_edge_cents: float | None
    cash_cents: int
    equity_cents: int
    open_yes_contracts: int
    open_no_contracts: int
    daily_loss_locked: bool


@dataclass(frozen=True)
class StrategyFill:
    market_ticker: str
    ts: float
    side: str
    action: str
    requested_count: int
    filled_count: int
    limit_price_cents: float
    fill_price_cents: float
    fees_cents: float
    model_probability: float
    model_edge_cents: float
    reason: str
    cash_after_cents: int
    equity_after_cents: int


@dataclass(frozen=True)
class MarketReplayResult:
    market_ticker: str
    outcome: str
    start_equity_cents: int
    end_equity_cents: int
    pnl_cents: int
    decisions: int
    fills: int
    buy_contracts: int
    sell_contracts: int
    settlement_contracts: int


class DailyRiskTracker:
    """Minimal replay of runtime.py's New York trading-day loss lock."""

    def __init__(self) -> None:
        self.day: str | None = None
        self.start_equity_cents: int | None = None
        self.locked = False

    @staticmethod
    def _day(ts: float) -> str:
        return datetime.fromtimestamp(ts, _NY).date().isoformat()

    def sync(
        self,
        *,
        ts: float,
        equity_cents: int,
        max_daily_loss_usd: float,
    ) -> tuple[bool, bool]:
        day = self._day(ts)
        if self.day != day:
            self.day = day
            self.start_equity_cents = int(equity_cents)
            self.locked = False

        assert self.start_equity_cents is not None
        was_locked = self.locked
        drawdown = int(equity_cents) - int(self.start_equity_cents)
        if drawdown <= -round(float(max_daily_loss_usd) * 100):
            self.locked = True
        return self.locked, bool(self.locked and not was_locked)


def _quote_component(candle: dict, side: str, field: str = "close") -> float | None:
    quote = candle.get(side) or {}
    raw = quote.get(f"{field}_dollars")
    if raw is None:
        raw = quote.get(field)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or not 0 < value < 1:
        return None
    return value * 100.0


def candle_book(
    market_ticker: str,
    candle: dict,
    *,
    eval_ts: float,
    assumed_top_size: int,
) -> OrderBook | None:
    """
    Build the closest possible top-of-book snapshot from a historical 1-minute candle.

    Kalshi archives YES bid/ask OHLC but not historical L2 depth. The synthetic book
    therefore preserves the production YES/NO complement mapping and uses an explicit
    assumed top size solely for sizing. Price logic still runs through OrderBook and
    build_trade_signal exactly as it does live.
    """
    yes_bid = _quote_component(candle, "yes_bid")
    yes_ask = _quote_component(candle, "yes_ask")
    if yes_bid is None or yes_ask is None or yes_bid >= yes_ask:
        return None

    no_bid = 100.0 - yes_ask
    depth = max(1, int(assumed_top_size))
    book = OrderBook(market_ticker)
    book.load_rest_snapshot(
        {
            "yes_dollars_fp": [[f"{yes_bid / 100.0:.4f}", depth]],
            "no_dollars_fp": [[f"{no_bid / 100.0:.4f}", depth]],
        }
    )
    book.last_update_ts = float(eval_ts)
    book.last_verified_ts = float(eval_ts)
    return book


def _position_inputs(
    snapshot: dict[str, Any], market_ticker: str
) -> tuple[int, int, float | None, float | None]:
    yes_qty = no_qty = 0
    yes_avg = no_avg = None
    for position in snapshot.get("positions", []):
        if position.get("market_ticker") != market_ticker:
            continue
        qty = max(0, int(position.get("strategy_contracts") or position.get("contracts") or 0))
        avg = float(position.get("avg_entry_cents") or 0.0)
        if position.get("side") == "yes":
            yes_qty, yes_avg = qty, avg
        elif position.get("side") == "no":
            no_qty, no_avg = qty, avg
    return yes_qty, no_qty, yes_avg, no_avg


def _record_fill(
    account: PaperAccount,
    *,
    signal: TradeSignal,
    book: OrderBook,
    fee_multiplier: float,
) -> StrategyFill | None:
    result = account.place_ioc(
        market_ticker=signal.market_ticker,
        side=signal.side,
        action=signal.action,
        count=signal.count,
        price_cents=signal.quote_price_cents,
        book=book,
        fee_multiplier=fee_multiplier,
    )
    order = result.get("order", {})
    try:
        filled = int(float(order.get("fill_count") or 0))
    except (TypeError, ValueError):
        filled = 0
    fill_price = order.get("fill_price_cents")
    if filled <= 0 or not isinstance(fill_price, (int, float)):
        return None

    fees = (
        taker_fee_cents_per_contract(
            float(fill_price),
            count=filled,
            fee_multiplier=fee_multiplier,
            action=signal.action,
        )
        * filled
    )

    # Historical candles do not expose L2 depth changes after our hypothetical IOC.
    # Consume the assumed displayed top level locally so repeated same-timestamp
    # sells cannot reuse the same synthetic liquidity over and over.
    if signal.action == "sell":
        delta_side = signal.side
        unified_yes_price = (
            float(fill_price) if signal.side == "yes" else 100.0 - float(fill_price)
        )
    elif signal.side == "yes":
        delta_side = "no"
        unified_yes_price = float(fill_price)
    else:
        delta_side = "yes"
        unified_yes_price = 100.0 - float(fill_price)
    book.apply_delta(
        {
            "side": delta_side,
            "price_dollars": unified_yes_price / 100.0,
            "delta_fp": -filled,
        }
    )
    account.mark_to_market(signal.market_ticker, book)
    snapshot = account.snapshot()
    return StrategyFill(
        market_ticker=signal.market_ticker,
        ts=float(signal.ts),
        side=signal.side,
        action=signal.action,
        requested_count=int(signal.count),
        filled_count=filled,
        limit_price_cents=float(signal.quote_price_cents),
        fill_price_cents=float(fill_price),
        fees_cents=round(float(fees), 6),
        model_probability=float(signal.model_probability),
        model_edge_cents=float(signal.edge_cents),
        reason=str(signal.reason),
        cash_after_cents=int(snapshot["cash_cents"]),
        equity_after_cents=int(snapshot["equity_cents"]),
    )


def _flatten_market(
    account: PaperAccount,
    *,
    market_ticker: str,
    book: OrderBook,
    settings: TradingSettings,
    fee_multiplier: float,
    price_ranges: list[dict[str, Any]] | None,
    ts: float,
) -> list[StrategyFill]:
    """Replay runtime's daily-loss flatten using the current marketable bid."""
    fills: list[StrategyFill] = []
    for position in list(account.snapshot().get("positions", [])):
        if position.get("market_ticker") != market_ticker:
            continue
        side = str(position.get("side") or "")
        qty = int(position.get("strategy_contracts") or position.get("contracts") or 0)
        if side not in {"yes", "no"} or qty <= 0:
            continue
        yes_bid, _, no_bid, _ = book.get_best_prices()
        bid = yes_bid if side == "yes" else no_bid
        if not isinstance(bid, (int, float)):
            continue

        while qty > 0:
            clip = min(qty, int(settings.max_order_contracts))
            limit_price = slipped_price_cents(
                float(bid),
                settings.slippage_ticks,
                "down",
                price_ranges,
            )
            signal = TradeSignal(
                ts=float(ts),
                market_ticker=market_ticker,
                side=side,  # type: ignore[arg-type]
                action="sell",
                count=clip,
                quote_price_cents=limit_price,
                fair_price_cents=0.0,
                credit_cents=0.0,
                edge_cents=0.0,
                edge_probability=0.0,
                confidence=0.0,
                model_probability=0.5,
                market_implied_probability=float(bid) / 100.0,
                reason="daily_loss_flatten",
                diagnostics={"fee_multiplier": fee_multiplier},
            )
            fill = _record_fill(
                account,
                signal=signal,
                book=book,
                fee_multiplier=fee_multiplier,
            )
            if fill is None:
                break
            fills.append(fill)
            qty -= fill.filled_count
    return fills


def replay_market(
    market: dict,
    *,
    asset: str,
    spot_ticks: list[dict[str, float]],
    fix_ticks: list[dict[str, float]],
    account: PaperAccount,
    risk: DailyRiskTracker,
    settings: TradingSettings,
    fee_multiplier: float,
    assumed_top_size: int,
    last_submission_ts: float,
) -> tuple[
    list[StrategyDecision],
    list[StrategyFill],
    MarketReplayResult | None,
    float,
]:
    profile = get_market_profile(asset)
    # runtime.py resets the buy cooldown whenever the active 15-minute market rotates.
    last_submission_ts = float("-inf")
    ticker = str(market.get("ticker") or "")
    strike = extract_suggested_strike(market)
    close_ts = parse_iso8601_to_epoch(market.get("close_time"))
    open_ts = parse_iso8601_to_epoch(market.get("open_time"))
    outcome = str(market.get("result") or "").lower()
    if not ticker or strike is None or close_ts is None or outcome not in {"yes", "no"}:
        return [], [], None, last_submission_ts

    start_ts = max(open_ts or close_ts - 900, close_ts - 900)
    candles = get_market_candlesticks(
        series_ticker=profile.kalshi_series_ticker,
        ticker=ticker,
        start_ts=int(start_ts),
        end_ts=int(close_ts),
        period_interval=1,
        historical=market.get("_data_tier") == "historical",
    )
    candles.sort(key=lambda row: int(row.get("end_period_ts") or 0))
    decimals = extract_settlement_decimals(market, profile.settlement_decimals_fallback)
    price_ranges = market.get("price_ranges") if isinstance(market.get("price_ranges"), list) else None

    starting_snapshot = account.snapshot()
    start_equity = int(starting_snapshot["equity_cents"])
    decisions: list[StrategyDecision] = []
    fills: list[StrategyFill] = []

    for candle in candles:
        eval_ts = float(candle.get("end_period_ts") or 0)
        if eval_ts <= 0 or eval_ts >= close_ts:
            continue

        book = candle_book(
            ticker,
            candle,
            eval_ts=eval_ts,
            assumed_top_size=assumed_top_size,
        )
        if book is None:
            continue

        latest = _latest_tick(spot_ticks, eval_ts)
        if latest is None:
            continue

        state = _settlement_state(
            fix_ticks,
            now_ts=eval_ts,
            close_ts=close_ts,
            window=profile.settlement_window_seconds,
            spot_ts=latest["ts"],
        )
        available_ticks = [tick for tick in fix_ticks if tick["ts"] <= eval_ts]
        pricing = compute_pricing_snapshot(
            profile=profile,
            feed_asset=asset,
            spot=latest["price"],
            ticks=available_ticks,
            strike=strike,
            market_ticker=ticker,
            close_time_iso=market.get("close_time"),
            settlement_decimals=decimals,
            index_state=state,
            now_ts=eval_ts,
        )
        pricing = apply_pricing_overrides(pricing, settings)
        if not pricing.get("ready"):
            continue

        account.mark_to_market(ticker, book)
        snapshot = account.snapshot()
        locked, newly_locked = risk.sync(
            ts=eval_ts,
            equity_cents=int(snapshot["equity_cents"]),
            max_daily_loss_usd=settings.max_daily_loss_usd,
        )
        if newly_locked:
            risk_fills = _flatten_market(
                account,
                market_ticker=ticker,
                book=book,
                settings=settings,
                fee_multiplier=fee_multiplier,
                price_ranges=price_ranges,
                ts=eval_ts,
            )
            fills.extend(risk_fills)
            if risk_fills:
                last_submission_ts = eval_ts
            account.mark_to_market(ticker, book)
            snapshot = account.snapshot()

        yes_qty, no_qty, yes_avg, no_avg = _position_inputs(snapshot, ticker)
        yes_bid, yes_ask, no_bid, no_ask = book.get_best_prices()
        if not all(isinstance(x, (int, float)) for x in (yes_bid, yes_ask, no_bid, no_ask)):
            continue

        signal: TradeSignal | None = None
        reason = "daily_loss_locked" if locked else "no_signal"
        diagnostics: dict[str, Any] = {}
        if not locked:
            signal, reason, diagnostics = build_trade_signal(
                pricing=pricing,
                market_ticker=ticker,
                book=book,
                settings=settings,
                bankroll_cents=int(snapshot["equity_cents"]),
                open_yes_contracts=yes_qty,
                open_no_contracts=no_qty,
                open_yes_avg_entry_cents=yes_avg,
                open_no_avg_entry_cents=no_avg,
                runtime_uptime_seconds=max(0.0, eval_ts - start_ts),
                available_cash_cents=max(
                    0,
                    int(snapshot["cash_cents"]) - round(settings.cash_buffer_usd * 100),
                ),
                fee_multiplier=fee_multiplier,
                fee_type="quadratic",
                price_ranges=price_ranges,
                now_ts=eval_ts,
            )

        decisions.append(
            StrategyDecision(
                market_ticker=ticker,
                eval_ts=eval_ts,
                seconds_to_expiry=close_ts - eval_ts,
                p_model=float(pricing["p_model"]),
                sigma_annual=float(pricing["sigma_annual"]),
                yes_bid_cents=float(yes_bid),
                yes_ask_cents=float(yes_ask),
                no_bid_cents=float(no_bid),
                no_ask_cents=float(no_ask),
                signal_reason=str(reason),
                signal_action=None if signal is None else signal.action,
                signal_side=None if signal is None else signal.side,
                signal_count=None if signal is None else int(signal.count),
                signal_limit_cents=None if signal is None else float(signal.quote_price_cents),
                signal_edge_cents=None if signal is None else float(signal.edge_cents),
                cash_cents=int(snapshot["cash_cents"]),
                equity_cents=int(snapshot["equity_cents"]),
                open_yes_contracts=yes_qty,
                open_no_contracts=no_qty,
                daily_loss_locked=locked,
            )
        )

        if signal is None:
            continue

        # runtime.py updates _last_submission_ts before placing every systematic IOC.
        if signal.action == "buy" and eval_ts - last_submission_ts < settings.cooldown_seconds:
            continue
        last_submission_ts = eval_ts

        fill = _record_fill(
            account,
            signal=signal,
            book=book,
            fee_multiplier=fee_multiplier,
        )
        if fill is not None:
            fills.append(fill)

        # A live fill wakes account-state consumers immediately. Sells are not
        # cooldown-gated, so drain an edge-reversal exit in max-order clips at the
        # same observed quote. A buy is intentionally limited to one candle snapshot:
        # the archive cannot prove the ask persisted for another five seconds.
        if signal.action == "sell" and fill is not None:
            for _ in range(100):
                account.mark_to_market(ticker, book)
                post = account.snapshot()
                yq, nq, ya, na = _position_inputs(post, ticker)
                next_signal, _, _ = build_trade_signal(
                    pricing=pricing,
                    market_ticker=ticker,
                    book=book,
                    settings=settings,
                    bankroll_cents=int(post["equity_cents"]),
                    open_yes_contracts=yq,
                    open_no_contracts=nq,
                    open_yes_avg_entry_cents=ya,
                    open_no_avg_entry_cents=na,
                    runtime_uptime_seconds=max(0.0, eval_ts - start_ts),
                    available_cash_cents=max(
                        0,
                        int(post["cash_cents"]) - round(settings.cash_buffer_usd * 100),
                    ),
                    fee_multiplier=fee_multiplier,
                    fee_type="quadratic",
                    price_ranges=price_ranges,
                    now_ts=eval_ts,
                )
                if next_signal is None or next_signal.action != "sell":
                    break
                last_submission_ts = eval_ts
                next_fill = _record_fill(
                    account,
                    signal=next_signal,
                    book=book,
                    fee_multiplier=fee_multiplier,
                )
                if next_fill is None:
                    break
                fills.append(next_fill)

        # A fill itself can push equity through the daily-loss threshold via fees
        # or execution. Live runtime re-evaluates on the resulting account update,
        # so enforce the same lock immediately rather than waiting a full candle.
        account.mark_to_market(ticker, book)
        post_fill = account.snapshot()
        _, newly_locked_after_fill = risk.sync(
            ts=eval_ts,
            equity_cents=int(post_fill["equity_cents"]),
            max_daily_loss_usd=settings.max_daily_loss_usd,
        )
        if newly_locked_after_fill:
            risk_fills = _flatten_market(
                account,
                market_ticker=ticker,
                book=book,
                settings=settings,
                fee_multiplier=fee_multiplier,
                price_ranges=price_ranges,
                ts=eval_ts,
            )
            fills.extend(risk_fills)
            if risk_fills:
                last_submission_ts = eval_ts

    before_settlement = account.snapshot()
    open_before = sum(
        int(position.get("contracts") or 0)
        for position in before_settlement.get("positions", [])
        if position.get("market_ticker") == ticker
    )
    account.settle(ticker, outcome)
    ending_snapshot = account.snapshot()
    end_equity = int(ending_snapshot["equity_cents"])

    market_result = MarketReplayResult(
        market_ticker=ticker,
        outcome=outcome,
        start_equity_cents=start_equity,
        end_equity_cents=end_equity,
        pnl_cents=end_equity - start_equity,
        decisions=len(decisions),
        fills=len(fills),
        buy_contracts=sum(fill.filled_count for fill in fills if fill.action == "buy"),
        sell_contracts=sum(fill.filled_count for fill in fills if fill.action == "sell"),
        settlement_contracts=open_before,
    )
    return decisions, fills, market_result, last_submission_ts


def summarize(
    *,
    starting_cash_cents: int,
    account: PaperAccount,
    decisions: list[StrategyDecision],
    fills: list[StrategyFill],
    markets: list[MarketReplayResult],
    fee_multiplier: float,
    assumed_top_size: int,
    settings: TradingSettings,
    cf_resolution: str,
) -> dict[str, Any]:
    ending = account.snapshot()
    total_fees = sum(fill.fees_cents for fill in fills)
    buy_fills = [fill for fill in fills if fill.action == "buy"]
    sell_fills = [fill for fill in fills if fill.action == "sell"]
    return {
        "starting_cash_cents": int(starting_cash_cents),
        "ending_equity_cents": int(ending["equity_cents"]),
        "pnl_cents": int(ending["equity_cents"]) - int(starting_cash_cents),
        "return_on_starting_cash": (
            (int(ending["equity_cents"]) - int(starting_cash_cents))
            / int(starting_cash_cents)
        ),
        "markets": len(markets),
        "profitable_markets": sum(1 for market in markets if market.pnl_cents > 0),
        "decisions": len(decisions),
        "fills": len(fills),
        "buy_fills": len(buy_fills),
        "sell_fills": len(sell_fills),
        "buy_contracts": sum(fill.filled_count for fill in buy_fills),
        "sell_contracts": sum(fill.filled_count for fill in sell_fills),
        "fees_cents": round(total_fees, 6),
        "fee_multiplier": float(fee_multiplier),
        "cf_spot_resolution": cf_resolution,
        "assumed_top_size": int(assumed_top_size),
        "settings": settings.model_dump(),
        "replay_fidelity": {
            "exact_from_production_logic": [
                "Asian pricing pipeline and volatility estimator",
                "pricing overrides",
                "YES/NO complement orderbook semantics",
                "taker fee rounding",
                "minimum-edge and deterministic-edge gates",
                "20-second entry cutoff",
                "Kelly sizing",
                "bankroll, fixed-dollar, position-fraction, cash-buffer, and max-order caps",
                "slippage-aware IOC limit construction",
                "edge-reversal exits",
                "buy cooldown semantics",
                "paper IOC fill/accounting semantics",
                "daily-loss lock and flatten behavior",
                "settlement accounting",
            ],
            "historical_approximations": [
                "Kalshi archives 1-minute YES bid/ask OHLC, not sequenced historical L2 books",
                "top-of-book size is an explicit assumption used only for sizing",
                "each candle close is treated as the observable quote snapshot at end_period_ts",
                "all simulated cash is assumed available to the active exchange shard",
                "fee multiplier is fixed by the CLI because historical event fee overrides are not archived with market candles",
                "at most one buy is allowed per 1-minute snapshot; persistent intra-minute liquidity is never assumed",
                "historical pause/resume lifecycle events are not replayed; a candle is treated as tradable when present",
                "latency, queue position, and sub-minute quote changes cannot be reconstructed",
            ],
        },
    }


def _write_csv(path: Path, rows: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    records = [asdict(row) for row in rows]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def run_strategy_backtest(
    *,
    asset: str,
    max_markets: int,
    starting_cash_cents: int,
    fee_multiplier: float,
    assumed_top_size: int,
    min_edge_cents: float | None = None,
    settings: TradingSettings | None = None,
) -> tuple[
    list[StrategyDecision],
    list[StrategyFill],
    list[MarketReplayResult],
    dict[str, Any],
]:
    profile = get_market_profile(asset)
    if settings is None:
        settings = TradingSettings(
            **(
                {"min_edge_cents": float(min_edge_cents)}
                if min_edge_cents is not None
                else {}
            )
        )
    elif min_edge_cents is not None:
        settings = TradingSettings.model_validate(
            {**settings.model_dump(), "min_edge_cents": float(min_edge_cents)}
        )
    markets = [
        market
        for market in get_settled_markets(profile.kalshi_series_ticker)
        if str(market.get("result") or "").lower() in {"yes", "no"}
    ]
    markets.sort(key=lambda row: parse_iso8601_to_epoch(row.get("close_time")) or 0)
    if max_markets > 0:
        markets = markets[-max_markets:]
    if not markets:
        account = PaperAccount(starting_cash_cents)
        return [], [], [], summarize(
            starting_cash_cents=starting_cash_cents,
            account=account,
            decisions=[],
            fills=[],
            markets=[],
            fee_multiplier=fee_multiplier,
            assumed_top_size=assumed_top_size,
            settings=settings,
            cf_resolution="unavailable",
        )

    closes = [
        parse_iso8601_to_epoch(market.get("close_time"))
        for market in markets
    ]
    closes = [ts for ts in closes if ts is not None]
    spot_ticks, fix_ticks, cf_resolution = fetch_cf_feeds(
        profile,
        min(closes) - 20 * 60,
        max(closes) + 1,
    )

    account = PaperAccount(starting_cash_cents)
    risk = DailyRiskTracker()
    all_decisions: list[StrategyDecision] = []
    all_fills: list[StrategyFill] = []
    market_results: list[MarketReplayResult] = []
    for market in markets:
        decisions, fills, result, _ = replay_market(
            market,
            asset=profile.asset,
            spot_ticks=spot_ticks,
            fix_ticks=fix_ticks,
            account=account,
            risk=risk,
            settings=settings,
            fee_multiplier=fee_multiplier,
            assumed_top_size=assumed_top_size,
            last_submission_ts=float("-inf"),
        )
        all_decisions.extend(decisions)
        all_fills.extend(fills)
        if result is not None:
            market_results.append(result)

    return all_decisions, all_fills, market_results, summarize(
        starting_cash_cents=starting_cash_cents,
        account=account,
        decisions=all_decisions,
        fills=all_fills,
        markets=market_results,
        fee_multiplier=fee_multiplier,
        assumed_top_size=assumed_top_size,
        settings=settings,
        cf_resolution=cf_resolution,
    )


def main() -> None:
    if KALSHI_ENV != "prod":
        raise RuntimeError(
            "Historical research uses production Kalshi/CF data; set KALSHI_ENV=prod."
        )
    parser = argparse.ArgumentParser(
        description="Replay the production Kalshi taker strategy on historical data."
    )
    parser.add_argument("asset", nargs="?", default="BTC")
    parser.add_argument("--max-markets", type=int, default=100)
    parser.add_argument("--starting-cash-cents", type=int, default=PAPER_STARTING_CASH_CENTS)
    parser.add_argument("--fee-multiplier", type=float, default=1.0)
    parser.add_argument("--assumed-top-size", type=int, default=10)
    parser.add_argument("--min-edge-cents", type=float)
    parser.add_argument(
        "--settings-json",
        help=(
            "Optional JSON file with any TradingSettings fields. "
            "--min-edge-cents overrides the file when both are supplied."
        ),
    )
    parser.add_argument("--output-dir", default="output/strategy_backtests")
    args = parser.parse_args()

    settings = TradingSettings()
    if args.settings_json:
        raw = json.loads(Path(args.settings_json).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("--settings-json must contain a JSON object")
        settings = TradingSettings.model_validate(
            {**settings.model_dump(), **raw}
        )

    decisions, fills, markets, summary = run_strategy_backtest(
        asset=args.asset,
        max_markets=max(0, args.max_markets),
        starting_cash_cents=max(100, args.starting_cash_cents),
        fee_multiplier=max(0.0, args.fee_multiplier),
        assumed_top_size=max(1, args.assumed_top_size),
        min_edge_cents=(
            None if args.min_edge_cents is None else max(0.5, args.min_edge_cents)
        ),
        settings=settings,
    )

    out = Path(args.output_dir)
    _write_csv(out / "decisions.csv", decisions)
    _write_csv(out / "fills.csv", fills)
    _write_csv(out / "markets.csv", markets)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
