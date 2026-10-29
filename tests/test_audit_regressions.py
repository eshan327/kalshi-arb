"""Regression cases from the repository audit; all exchange data is synthetic."""

import csv
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

import backtest
from data.capture import BookReplay, FeedCapture, iter_capture, load_quote_tapes
from perps_backtest import Book, run_backtest as replay_perps
from pricing.vol_estimator import realized_vol_from_price_points
from taker_backtest import run_backtest as replay_predictions

PREDICTION_WS = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
PERPS_WS = "wss://external-api-margin-ws.kalshi.com/trade-api/ws/v2/margin"


def iso(ts):
    return datetime.fromtimestamp(ts, UTC).isoformat()


def frame(ts, kind, seq=None, sid=1, **msg):
    return {"received_ns": round(ts * 1e9), "session": "s1",
            "data": {"type": kind, "sid": sid, "seq": seq,
                     "msg": {"market_ticker": "TEST", **msg}}}


@pytest.fixture
def research(tmp_path):
    history = tmp_path / "history"
    history.mkdir()
    market = {"ticker": "TEST", "open_time": iso(900), "close_time": iso(1100),
              "result": "yes", "price_ranges": [{"start": "0", "end": "1", "step": "0.01"}]}
    (history / "source.json").write_text(json.dumps({
        "asset": "BTC", "environment": "prod", "ws_endpoint": PREDICTION_WS, "markets": [market],
    }))
    (history / "manifest.json").write_text(json.dumps({
        "ticker": "TEST", "environment": "prod", "ws_endpoint": PERPS_WS,
        "start": iso(900), "end": iso(2000),
        "market": {"ticker": "TEST", "asset_class": "Crypto", "tick_size": "0.01",
                   "fractional_trading_enabled": False},
    }))
    (history / "funding_rates.jsonl").write_text("")
    captures, signals = {}, {}
    for product, endpoint, levels, header, signal in (
        ("predictions", PREDICTION_WS,
         {"yes_dollars_fp": [["0.40", "2"]], "no_dollars_fp": [["0.55", "2"]]},
         "market_ticker,decision_ts,side,contracts,limit_price_cents",
         "TEST,1000.1,yes,1,45"),
        ("perps", PERPS_WS, {"bid": [["99", "2"]], "ask": [["100", "2"]]},
         "ticker,entry_ts,exit_ts,side,contracts,entry_limit_dollars,exit_limit_dollars",
         "TEST,1000.1,1000.5,long,1,100,99"),
    ):
        captures[product] = tmp_path / f"{product}.jsonl"
        start = frame(999, "session_start")
        start["data"].update(environment="prod", endpoint=endpoint, book_price_mode="native_bids")
        captures[product].write_text("".join(json.dumps(row) + "\n" for row in (
            start, frame(1000, "orderbook_snapshot", 10, **levels), frame(1001, "session_end"),
        )))
        signals[product] = tmp_path / f"{product}.csv"
        signals[product].write_text(header + "\n" + signal + "\n")

    def replay(product, **kwargs):
        common = dict(capture_path=captures[product], signals_path=signals[product],
                      output_dir=tmp_path / "out", latency_ms=0,
                      max_book_age_ms=2000, depth_fraction=Decimal("1"))
        common.update(kwargs)
        if product == "predictions":
            return replay_predictions(input_dir=history, **common)
        return replay_perps(history_dir=history, taker_fee_rate=Decimal("0"), **common)

    return history, captures, signals, replay


def test_baseline_refuses_to_overwrite_its_source(research):
    history, _, _, _ = research
    original = {p.name: p.read_bytes() for p in history.iterdir()}
    with pytest.raises(FileExistsError):
        backtest.run_backtest(asset="BTC", input_dir=history, output_dir=history,
                              end_close=iso(1050))
    assert {p.name: p.read_bytes() for p in history.iterdir()} == original


@pytest.mark.parametrize("product", ["predictions", "perps"])
def test_replays_reject_off_grid_limits(research, product):
    _, _, signals, replay = research
    path = signals[product]
    path.write_text(path.read_text().replace(
        ",45\n" if product == "predictions" else ",100,99\n",
        ",45.5\n" if product == "predictions" else ",100,99.005\n",
    ))
    with pytest.raises(ValueError, match="grid|tick_size"):
        replay(product)


def test_no_limit_is_validated_on_the_yes_price_grid(research):
    history, _, signals, replay = research
    source = json.loads((history / "source.json").read_text())
    source["markets"][0]["price_ranges"] = [
        {"start": "0", "end": "0.4", "step": "0.01"},
        {"start": "0.4", "end": "0.8", "step": "0.02"},
        {"start": "0.8", "end": "1", "step": "0.01"},
    ]
    (history / "source.json").write_text(json.dumps(source))
    signals["predictions"].write_text(signals["predictions"].read_text().replace(",yes,1,45", ",no,1,39"))
    with pytest.raises(ValueError, match="price_ranges grid"):
        replay("predictions")  # A 39-cent No limit is a 61-cent Yes ask.
    signals["predictions"].write_text(signals["predictions"].read_text().replace(",39\n", ",40\n"))
    assert replay("predictions")["all"]["statuses"] == {"no_depth_within_limit": 1}


def test_perps_requires_whole_contracts_unless_market_allows_fractions(research):
    history, _, signals, replay = research
    signals["perps"].write_text(signals["perps"].read_text().replace(",long,1,", ",long,0.5,"))
    with pytest.raises(ValueError, match="whole contracts"):
        replay("perps")
    manifest = json.loads((history / "manifest.json").read_text())
    manifest["market"]["fractional_trading_enabled"] = True
    (history / "manifest.json").write_text(json.dumps(manifest))
    assert replay("perps")["completed_round_trips"] == 1


def test_whole_contract_depth_cannot_combine_fractional_fills():
    book = Book("TEST", Decimal("1"))
    book.ingest(frame(1000, "orderbook_snapshot", 10,
                      bid=[["99", "1"]], ask=[["100", "1"], ["101", "1"]]))
    assert book.walk("buy", Decimal("1"), Decimal("101"), Decimal("0.5")) == (0, 0)
    assert book.walk("buy", Decimal("1"), Decimal("100"), Decimal("1")) == (1, 100)


@pytest.mark.parametrize("product", ["predictions", "perps"])
def test_replays_stop_at_abrupt_capture_end(research, product):
    _, captures, _, replay = research
    path = captures[product]
    path.write_text("\n".join(path.read_text().splitlines()[:-1]) + "\n")
    result = replay(product)
    if product == "predictions":
        assert result["all"]["statuses"] == {"no_continuous_book": 1}
        assert Decimal(result["all"]["net_pnl_dollars"]) == 0
        quotes = load_quote_tapes(path, environment="prod", endpoint=PREDICTION_WS)["TEST"]
        assert quotes[-1]["yes_ask_cents"] is None
    else:
        assert result["completed_round_trips"] == 0
        assert result["open_positions_at_signal_exit"] == 0


def test_perps_exit_after_capture_end_stays_open(research):
    _, captures, _, replay = research
    path = captures["perps"]
    rows = path.read_text().splitlines()[:-1]
    rows.append(json.dumps(frame(1000.2, "ticker")))
    path.write_text("\n".join(rows) + "\n")
    result = replay("perps")
    assert result["completed_round_trips"] == 0
    assert result["open_positions_at_signal_exit"] == 1
    with (path.parent / "out" / "round_trips.csv").open() as handle:
        row = next(csv.DictReader(handle))
    assert row["status"] == "exit_no_continuous_book"
    assert row["open_contracts"] == "1"


@pytest.mark.parametrize("product", ["predictions", "perps"])
@pytest.mark.parametrize("change", ["environment", "endpoint", "missing", "missing_start"])
def test_replays_reject_wrong_or_missing_capture_provenance(research, product, change):
    _, captures, _, replay = research
    path = captures[product]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if change == "missing_start":
        rows = rows[1:]
    elif change == "missing":
        rows[0]["data"].pop("environment")
        rows[0]["data"].pop("endpoint")
    else:
        rows[0]["data"][change] = "demo" if change == "environment" else "wss://other.example"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError, match="provenance|mismatch"):
        replay(product)


def test_capture_checks_provenance_again_on_reconnect(research):
    _, captures, _, _ = research
    path = captures["predictions"]
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    next_start = {"received_ns": 1002 * 10**9, "session": "s2",
                  "data": {**rows[0]["data"], "environment": "demo"}}
    with path.open("a") as handle:
        handle.write(json.dumps(next_start) + "\n")
    with pytest.raises(ValueError, match="mismatch"):
        list(iter_capture(path, environment="prod", endpoint=PREDICTION_WS))


def test_recorder_writes_provenance(monkeypatch, tmp_path):
    from data import capture
    monkeypatch.setattr(capture, "KALSHI_ENV", "prod")
    path = tmp_path / "feed.jsonl"
    recorder = FeedCapture(str(path), "s1", endpoint=PREDICTION_WS)
    recorder.close()
    rows = list(iter_capture(path, environment="prod", endpoint=PREDICTION_WS))
    assert rows[0]["data"]["environment"] == "prod"
    assert rows[0]["data"]["endpoint"] == PREDICTION_WS
    assert rows[-1]["data"]["type"] == "session_end"


def test_stale_prediction_snapshots_do_not_restore_depth_or_freshness():
    replay = BookReplay()
    snapshot = frame(1000, "orderbook_snapshot", 10,
                     yes_dollars_fp=[["0.40", "1"]], no_dollars_fp=[["0.55", "3"]])
    replay.ingest(snapshot)
    replay.ingest(frame(1000.1, "orderbook_delta", 11,
                        side="no", price_dollars="0.55", delta_fp="-3"))
    assert replay.ingest({**snapshot, "received_ns": round(1000.2 * 1e9)}) == set()
    assert replay.books["TEST"].no == {}
    assert replay.updated_ns["TEST"] == round(1000.1 * 1e9)
    replay.ingest(frame(1000.3, "orderbook_delta", 13,
                        side="yes", price_dollars="0.40", delta_fp="1"))
    assert "TEST" not in replay.updated_ns
    replay.ingest({**snapshot, "received_ns": round(1000.4 * 1e9)})
    assert replay.books["TEST"].needs_resync
    old_after_gap = {**snapshot, "received_ns": round(1000.45 * 1e9),
                     "data": {**snapshot["data"], "seq": 12}}
    assert replay.ingest(old_after_gap) == set()
    assert replay.books["TEST"].needs_resync
    fresh = frame(1000.5, "orderbook_snapshot", 13,
                  yes_dollars_fp=[["0.40", "1"]], no_dollars_fp=[])
    replay.ingest(fresh)
    assert not replay.books["TEST"].needs_resync
    # A new subscription restarts its sequence; deltas from the old one are ignored.
    replay.ingest(frame(1000.6, "orderbook_snapshot", 1, sid=2,
                        yes_dollars_fp=[["0.40", "1"]], no_dollars_fp=[]))
    assert replay.ingest(frame(1000.7, "orderbook_delta", 2, sid=1,
                               side="no", price_dollars="0.55", delta_fp="3")) == set()
    assert replay.books["TEST"].no == {}
    assert replay.ingest({**snapshot, "received_ns": round(1000.8 * 1e9)}) == set()
    assert replay.books["TEST"].sid == 2


def test_two_second_volatility_window_is_rejected_before_statistics(tmp_path):
    points = [(999.0, 100.0), (1000.0, 101.0)]
    with pytest.raises(ValueError, match="three seconds"):
        realized_vol_from_price_points(points, window_seconds=2, now_ts=1000.4)
    assert realized_vol_from_price_points([(998.0, 100.0), *points],
                                          window_seconds=3, now_ts=1000.4) > 0
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="three seconds"):
        backtest.run_backtest(asset="BTC", output_dir=output, vol_window_seconds=2)
    assert not output.exists()


@pytest.mark.parametrize("product", ["predictions", "perps"])
def test_reconnect_after_crash_does_not_extend_old_session_coverage(research, product):
    _, captures, _, replay = research
    path = captures[product]
    rows = [json.loads(line) for line in path.read_text().splitlines()[:-1]]
    rows.append({"received_ns": 1001 * 10**9, "session": "s2", "data": rows[0]["data"]})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    result = replay(product)
    if product == "predictions":
        assert result["all"]["statuses"] == {"no_continuous_book": 1}
    else:
        assert result["completed_round_trips"] == 0


@pytest.mark.parametrize("product", ["predictions", "perps"])
@pytest.mark.parametrize("field", ["environment", "ws_endpoint"])
def test_replays_require_history_provenance(research, product, field):
    history, _, _, replay = research
    path = history / ("source.json" if product == "predictions" else "manifest.json")
    metadata = json.loads(path.read_text())
    metadata.pop(field)
    path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="production export with ws_endpoint"):
        replay(product)


def test_perps_rejects_market_metadata_for_another_ticker(research):
    history, _, _, replay = research
    path = history / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["market"]["ticker"] = "OTHER"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="ticker mismatch"):
        replay("perps")


@pytest.mark.parametrize("field", ["environment", "ws_endpoint"])
def test_offline_baseline_rejects_quotes_without_provenance(research, field):
    history, _, _, _ = research
    path = history / "source.json"
    source = json.loads(path.read_text())
    source.pop(field)
    source.update(trades={}, quotes={"TEST": [
        {"ts": 1000, "yes_bid_cents": 40, "yes_ask_cents": 45},
    ]})
    path.write_text(json.dumps(source))
    with pytest.raises(ValueError, match="Saved quotes need production capture provenance"):
        backtest.run_backtest(asset="BTC", input_dir=history, output_dir=history.parent / "out")
