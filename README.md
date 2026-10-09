# Backtest accounting: three gross winners, one net loss

An original synthetic counterexample for long-only strategy authors. Seven invented candles produce three round trips that gain **0.6012% before costs** and lose **0.3001% after 0.10% fees and 5 bps adverse slippage per fill**. Stress costs produce a **1.4894% loss**. These are constructed arithmetic results, not historical market performance, an investment strategy, a customer result or earned income.

Inspect the [public example](https://backtest-evidence.synchrobiz.chatgpt.site/) or [full report](dist/case/report.md). Exact prices, exposure requests, six fills, equity curves, costs, source and checksums are included. There is no LLM, exchange connector, wallet connection or external service in the calculation.

Reproduce from this repository with Python 3.10+:

    python -m evidence_service report --candles dist/case/candles.csv --spec dist/case/spec.json --out reproduced

The base result reconciles within 0.00001 quote units to the independent formula:

    1000 * ((100.20 * (1-.0005) * (1-.001)) / (100 * (1+.0005) * (1+.001)))**3

The small difference comes from quantity-step rounding. The fixture closes all positions. Signals are constructed inputs, not audited predictions. The engine executes close-time signals at the next open, uses long-only unlevered positions and includes buy/sell fees. Its zero minimum notional and 0.00000001 quantity step are simulation assumptions, not current venue rules. Latency, impact, funding, borrowing and tax are excluded.

Run this example locally. Workflow Foundry also prepares standardized evidence reports for buyer-supplied lawful inputs through the existing AgentSouk service, with prices and limits shown on the example page. Paid value is preparation and repeatable packaging; source availability is intentional. Ordering currently requires AgentSouk's agent API and native USDC on Base, not card checkout. No independent customer receipt, commercial effectiveness or profitable trading edge is established.
