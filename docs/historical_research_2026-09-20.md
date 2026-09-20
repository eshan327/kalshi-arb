# Historical research findings — 2026-09-20

This note records the first authenticated backtest of the BTC 15-minute strategy
against Kalshi historical markets and the official CF Benchmarks history feed.

## Scope

The final research suite used:

- 200 settled BTC 15-minute markets for probability calibration.
- A chronological 100-market development / 100-market holdout split for a small
  volatility-window and volatility-scale sweep.
- 100 recent markets for fixed-horizon model-versus-market comparisons and the
  production taker-strategy replay.
- Official CF historical values. For BTC, the historical stream contains 200 ms
  publications; exact second-boundary values are isolated for the 1 Hz volatility
  and settlement-fix state used by the live model.
- The same production Asian pricer, pricing overrides, strategy signal builder,
  taker fee logic, Kelly/risk sizing, paper IOC accounting, exits, settlement, and
  daily-loss guard used by the application.

Historical quote data cannot reproduce the sequenced live L2 book. Strategy P&L
therefore remains an execution-plausibility replay rather than a latency-accurate
fill backtest.

## Settlement replay bug found and fixed

The first historical implementation incorrectly treated every 200 ms CF history
observation as a settlement fix. That created five times too many final-minute
samples and caused every <=60 second replay point to fail validation.

Historical CF history is now split exactly as the live application treats it:

- all published subsecond ticks may update current spot;
- only exact second-boundary publications enter realized-volatility history and
  final-minute settlement accumulation.

A dedicated authenticated diagnostic verified the reconstructed fixing counts:

| Horizon | Known fixes |
| ---: | ---: |
| 60 s | 0 |
| 45 s | 15 |
| 30 s | 30 |
| 20 s | 40 |
| 10 s | 50 |
| 5 s | 55 |
| 1 s | 59 |

The final 200-market calibration has zero replay rejections.

## Production-model calibration

The current production model uses a 300-second realized-volatility window and
volatility scale 1.0.

| Seconds to expiry | Markets | Brier | Log loss |
| ---: | ---: | ---: | ---: |
| 600 | 200 | 0.21411 | 0.61659 |
| 300 | 200 | 0.15027 | 0.46627 |
| 120 | 200 | 0.09616 | 0.36301 |
| 90 | 200 | 0.08099 | 0.27725 |
| 60 | 200 | 0.03985 | 0.12847 |
| 45 | 200 | 0.02401 | 0.09836 |
| 30 | 200 | 0.01196 | 0.03849 |
| 20 | 200 | 0.00399 | 0.01569 |
| 10 | 200 | 0.000045 | 0.000642 |
| 5 | 200 | 0.000377 | 0.001622 |
| 1 | 200 | ~0 | ~0 |

The sample is almost balanced (51% YES), so a constant base-rate forecast has a
Brier score close to 0.25. The model becomes extremely informative as locked
settlement fixes accumulate.

The important weakness is tail calibration before the final minute. There are
several confidently wrong forecasts around 90–120 seconds, including outcomes
assigned probabilities below 1% that resolved the other way. The arithmetic-TWAP
payoff model is not the main problem; the short-horizon return distribution /
volatility estimate is too confident in some regimes.

## 5 Hz incremental value

The faster spot frequently changes the probability before settlement, but its
average proper-score improvement over the latest 1 Hz spot is small. The clearest
improvement in this sample occurs around 45 seconds; other horizons are mixed.

Conclusion: retain 5 Hz for live responsiveness, but do not attribute the
strategy's edge to 5 Hz alone.

## Volatility research

A small grid tested realized-volatility windows of 60, 120, 300, and 600 seconds
with volatility scales 0.75, 1.0, and 1.25. Parameters were selected on the older
100 markets and evaluated without retuning on the newer 100-market holdout.

Production baseline, 300 s / 1.0:

- development Brier: 0.05784
- holdout Brier: 0.05641
- holdout log loss: 0.18531

Development-selected candidate, 600 s / 1.25:

- development Brier: 0.05719
- holdout Brier: 0.05504
- holdout log loss: 0.17079

That is about a 2.4% holdout Brier improvement and 7.8% holdout log-loss
improvement versus production. The benefit is concentrated before and near the
start of settlement; globally applying the larger variance can make already
near-deterministic 20–30 second forecasts worse.

Conclusion: the data supports a longer / more conservative volatility estimate
before the final minute, but not blindly applying 600 s / 1.25 at every horizon.
A horizon-aware variance policy is the next model change worth validating.

## Model versus Kalshi

For 100 markets, the backtest compares the model with the most recent non-block
Kalshi trade no more than five seconds old at each fixed horizon. This is
market-relative information, not a claim that the trade price was executable by
our IOC.

| Horizon | Model Brier | Market Brier | Lower score |
| ---: | ---: | ---: | --- |
| 600 s | 0.22556 | 0.22228 | market |
| 300 s | 0.14581 | 0.13252 | market |
| 120 s | 0.10204 | 0.09254 | market |
| 90 s | 0.06321 | 0.05991 | market |
| 60 s | 0.04613 | 0.04162 | market |
| 45 s | 0.03329 | 0.02857 | market |
| 30 s | 0.00510 | 0.00552 | model, slightly |
| 20 s | 0.00054 | 0.00068 | model, slightly |
| 10 s | ~0 | 0.0000076 | model |
| 5 s | ~0 | 0.0000025 | model |
| 1 s | ~0 | 0.0000010 | model |

The market has the stronger probability forecast through roughly 45–60 seconds.
The model starts to look better only around 30 seconds and later, when the known
settlement fixes dominate uncertainty. The 30/20 second advantage is small and
does not by itself prove executable alpha.

## Coarse quote alpha screen

Using one-minute historical quote closes, one contract, the production fee
formula, a 2-cent edge threshold, and the first qualifying signal in each market:

- 97 trades
- 60 wins
- P&L: -380 cents
- ROI on entry cost: -5.96%
- mean ex-ante modeled edge: +4.19 cents

Increasing the nominal model edge threshold did not produce a monotonic increase
in realized returns. In this sample, large model/market disagreements are often
model error or adverse selection rather than stronger alpha.

The largest losses occur in the broad early-entry region where the market-relative
proper scores also favor Kalshi.

## Production-strategy replay

The 100-market replay uses the real production signal builder and paper IOC
accounting against one-minute quote-close synthetic books with assumed top size
10.

Results with the current default settings:

- starting equity: $1,000
- ending equity: $950.67
- P&L: -$49.33
- return: -4.93%
- 87 fills
- 78 buy fills / 740 contracts bought
- 9 sell fills / 90 contracts sold
- fees: $9.45

The daily-loss guard locks the strategy for most subsequent decision points after
losses. Only 22 of 100 markets actually receive fills; those filled markets
produce the entire -$49.33 result. The guard limits future exposure but cannot
prevent a single already-open market position from losing more than the nominal
$10 daily-loss threshold.

Buy fills occur predominantly several minutes before expiry (median about seven
minutes), exactly where the independent model-versus-market analysis says Kalshi
has the better probability estimate.

## Conclusions for the model and strategy

1. **Keep the Asian/TWAP settlement structure.** Conditioning on already-known
   final-minute fixes is strongly supported by the calibration results.
2. **Do not treat model-market disagreement as edge by itself.** Before about
   45–60 seconds, Kalshi's contemporaneous market probability is better calibrated
   than the production model, and the current broad taker strategy loses money.
3. **Do not solve this by merely raising the 2-cent threshold.** Historical
   nominal edge is not monotonic with realized return.
4. **The first model improvement to pursue is variance / tail calibration.** The
   chronological sweep supports a longer, more conservative volatility estimate
   before settlement. More complex jump/heavy-tail or empirical models should be
   justified by residual errors after that simpler change.
5. **The interesting trading region is the final ~30 seconds.** The model begins
   to outperform contemporaneous trade probabilities there, but public historical
   data cannot establish IOC fillability in that sub-minute window.
6. **Do not infer live profitability from the late-horizon scoring result.**
   Record the live sequenced orderbook, CF updates, signals, submissions, and fills
   so the final 20–30 second hypothesis can be tested with real microstructure.
7. **The current broad-entry production strategy should not be treated as
   validated alpha.** The authenticated historical evidence falsifies that version
   of the strategy.

The research pipeline is intentionally reproducible rather than encoding a
profitable-looking parameter choice into production after one recent sample.
The next production-model change should be evaluated on additional forward data
before live risk is increased.
