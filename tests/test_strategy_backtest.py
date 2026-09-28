import csv
import json

import pytest

import strategy_backtest


def test_mixed_study_uses_common_nonleaking_holdout(monkeypatch, tmp_path):
    calls = []

    def replay(filename, rows, **kwargs):
        calls.append(kwargs)
        kwargs["output_dir"].mkdir()
        with (kwargs["output_dir"] / filename).open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    monkeypatch.setattr(strategy_backtest, "replay_15m", lambda **kw: replay(
        "fills.csv", [
            {"market_ticker": "A", "decision_ts": "800", "outcome_ts": "900", "status": "filled", "pnl_dollars": "1"},
            {"market_ticker": "B", "decision_ts": "900", "outcome_ts": "1100", "status": "filled", "pnl_dollars": "2"},
            {"market_ticker": "C", "decision_ts": "1050", "outcome_ts": "1200", "status": "filled", "pnl_dollars": "3"},
        ], **kw))
    monkeypatch.setattr(strategy_backtest, "replay_perps", lambda **kw: replay(
        "round_trips.csv", [
            {"ticker": "P", "entry_ts": "800", "exit_arrival_ts": "900", "status": "filled", "open_contracts": "0", "net_pnl_dollars": "-0.5"},
            {"ticker": "P", "entry_ts": "1050", "exit_arrival_ts": "1200", "status": "stale_book", "open_contracts": "0", "net_pnl_dollars": ""},
            {"ticker": "P", "entry_ts": "1250", "exit_arrival_ts": "1300", "status": "exit_stale_book", "open_contracts": "1", "net_pnl_dollars": ""},
        ], **kw))
    config = tmp_path / "study.json"
    config.write_text(json.dumps({
        "holdout_start": "1970-01-01T00:16:40Z",
        "legs": [
            {"name": "market", "product": "15m", "history_dir": "history",
             "capture_path": "book.jsonl", "signals_path": "signals.csv", "fee_multiplier": "1"},
            {"name": "perp", "product": "perps", "history_dir": "history",
             "capture_path": "book.jsonl", "signals_path": "signals.csv", "taker_fee_rate": "0.001"},
        ],
    }))
    out = tmp_path / "study"
    summary = strategy_backtest.run_study(config, out)
    assert calls[0]["input_dir"] == tmp_path / "history"
    assert calls[0]["holdout_start"] == 1000
    assert calls[1]["history_dir"] == tmp_path / "history"
    assert summary["train"]["scored_net_pnl_dollars"] == "0.5"
    assert summary["holdout"]["scored_net_pnl_dollars"] == "3"
    assert summary["crossing"]["scored_net_pnl_dollars"] == "2"
    assert summary["holdout"]["unscored"] == 1
    assert summary["holdout"]["open_positions"] == 1
    rows = list(csv.DictReader((out / "results.csv").open()))
    assert [row["split"] for row in rows] == ["train", "train", "crossing", "holdout", "holdout", "holdout"]
    assert rows[4]["net_pnl_dollars"] == "0"


def test_mixed_study_rejects_noninteger_latency(tmp_path):
    config = tmp_path / "study.json"
    config.write_text(json.dumps({
        "holdout_start": "1970-01-01T00:16:40Z",
        "legs": [{"name": "market", "product": "15m", "history_dir": "history",
                  "capture_path": "book.jsonl", "signals_path": "signals.csv",
                  "fee_multiplier": "1", "latency_ms": 0.5}],
    }))
    with pytest.raises(ValueError, match="latency_ms"):
        strategy_backtest.run_study(config, tmp_path / "study")
