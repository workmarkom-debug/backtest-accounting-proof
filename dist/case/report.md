# Backtest Evidence Pack

Dataset: **SYNTHETIC - three small winning round trips**
Data type: **synthetic**. Simulation only.
UTC candle-open window: 2026-01-01T00:00:00+00:00 to 2026-01-01T06:00:00+00:00.
Rows: 7; interval: 3600000 ms; duplicates/gaps: none.
Rule: `supplied_signals`; parameters: `{}`.
Hypothetical starting balance: 1,000.00 quote-currency units.

| Simulation | Return | Maximum drawdown | Fees paid | Fills | Closed round trips | Ending value |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| strategy_gross | 0.60% | 0.00% | 0.0000 | 6 | 3 | 1006.0120 |
| buy_hold_gross | 0.20% | 0.20% | 0.0000 | 1 | 0 | 1002.0000 |
| strategy_base | -0.30% | 0.35% | 5.9910 | 6 | 3 | 996.9985 |
| buy_hold_base | 0.05% | 0.20% | 0.9990 | 1 | 0 | 1000.4988 |
| strategy_stress | -1.49% | 1.49% | 11.9104 | 6 | 3 | 985.1060 |
| buy_hold_stress | -0.15% | 0.35% | 1.9960 | 1 | 0 | 998.5022 |

Base return difference versus buy-and-hold: -0.35 percentage points.
Stress return change versus base: -1.19 percentage points.

## Execution contract

Base fee per fill: 0.1000%; adverse slippage per fill: 5 bps.
Stress doubles the fee rate and triples slippage. Gross removes both.
A close-time exposure request is filled at the NEXT candle open. The final signal has no later fill.
Long-only, unlevered, one position. Entry spends exposure x available cash, including the buy fee.
Positive requests do not resize an existing position; zero closes it on the next open.
Buy-and-hold uses identical costs and first enters at the second candle open.
Quantity step: 0.00000001; minimum notional: 0. These are simulation assumptions, not exchange rules.
No practical drawdown stop (threshold 100%). Drawdowns are observed at candle closes; intrabar risk is not measured.
A final open position is marked at the final close without a fabricated liquidation or exit fee.
Base ending open quantity: 0.00000000; marked value: 0.0000.
Funding, borrowing, spread beyond specified slippage, latency, market impact and tax are excluded.

## Evidence and limits

Signal causality status: `unverified`.
Buyer-supplied signals may contain future information; their generation was not audited.
Input cadence and OHLC consistency are checked. Data authenticity, completeness outside the supplied window and licensing are not verified.
No parameter search, train/test selection or untouched holdout is performed. This checks the submitted setup on one supplied window.
A short window or few closed trades cannot establish a repeatable trading edge. Losing results are retained.
This is a technical simulation report, not a trading recommendation, profit guarantee or live-execution certification.
Synthetic demo prices are invented and must not be presented as historical market performance.

## Reproduction

The ZIP includes the exact normalized specification, candle CSV, result JSON, fill/equity CSVs and executor source.
With Python 3.10+ from the extracted directory:

```text
python -m evidence_service report --candles candles.csv --spec spec.json --out reproduced
```

Input SHA-256: `5bc619f3af7467c7b7fc8f7e7b937d1d0ef7443d73cae5955a8afc169527e591`.
Normalized spec SHA-256: `a42c592496691a7e17745ddef4f99216e9d4af058f54e5b75d4e942916027e10`.
manifest.json hashes each delivery file except itself and the final ZIP. It records the exact executor source hashes.
