"""Offline, long-only simulation with signals executed at the following open.

Signals are target states in [0, 1]. A positive signal opens one position using
``allocation * signal`` of available cash, including the entry fee. The position
is held until a zero signal or the drawdown stop; positive signals never cause
per-bar rebalancing. Minimum notional applies to entries. Exits sell the entire
existing lot, including positions whose value has fallen below that minimum.

Equity and the drawdown stop are observed at bar closes. A stop queues an exit
for the next available open, where gap/slippage losses can exceed the threshold.
There are no synthetic end-of-test fills or intrabar stop assumptions.
"""

from decimal import Decimal, localcontext
import math
from numbers import Real
from typing import Sequence

from .models import BacktestSettings, Candle


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _decimal(value: Real) -> Decimal:
    # Preserve integer precision and support other Real implementations (for
    # example Fraction) whose string form is not a decimal literal.
    return Decimal(str(value)) if isinstance(value, (int, float)) else Decimal(str(float(value)))


def _json_number(value: Decimal) -> float:
    result = float(value)
    if not math.isfinite(result) or (value != 0 and result == 0):
        raise ValueError("Backtest calculation exceeds the finite numeric range")
    return result


def _validate_settings(settings: BacktestSettings) -> None:
    initial_cash = _number(settings.initial_cash, "initial_cash")
    fee_rate = _number(settings.fee_rate, "fee_rate")
    slippage_bps = _number(settings.slippage_bps, "slippage_bps")
    allocation = _number(settings.allocation, "allocation")
    min_notional = _number(settings.min_notional, "min_notional")
    step_size = _number(settings.step_size, "step_size")
    drawdown_stop = _number(settings.max_drawdown_stop, "max_drawdown_stop")
    if initial_cash <= 0:
        raise ValueError("initial_cash must be positive")
    if not 0 <= fee_rate < 1:
        raise ValueError("fee_rate must be in [0, 1)")
    if not 0 <= slippage_bps < 10_000:
        raise ValueError("slippage_bps must be in [0, 10000)")
    if not 0 <= allocation <= 1:
        raise ValueError("allocation must be in [0, 1]; leverage is not supported")
    if min_notional < 0:
        raise ValueError("min_notional must be nonnegative")
    if step_size <= 0:
        raise ValueError("step_size must be positive")
    if not 0 < drawdown_stop <= 1:
        raise ValueError("max_drawdown_stop must be in (0, 1]")
    if (
        isinstance(settings.annual_periods, bool)
        or not isinstance(settings.annual_periods, int)
        or settings.annual_periods <= 0
    ):
        raise ValueError("annual_periods must be a positive integer")


def _validate_inputs(candles: Sequence[Candle], signals: Sequence[float]) -> None:
    if len(candles) < 2:
        raise ValueError("At least two candles are required for next-open execution")
    if len(signals) != len(candles):
        raise ValueError("signals must have exactly one value per candle")
    previous_timestamp = None
    for index, candle in enumerate(candles):
        timestamp = candle.timestamp_ms
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError(f"candles[{index}].timestamp_ms must be a nonnegative integer")
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            raise ValueError("Candle timestamps must be strictly increasing")
        previous_timestamp = timestamp
        prices = {}
        for name in ("open", "high", "low", "close"):
            value = getattr(candle, name)
            _number(value, f"candles[{index}].{name}")
            prices[name] = _decimal(value)
        if any(price <= 0 for price in prices.values()):
            raise ValueError(f"candles[{index}] prices must be positive")
        if (
            prices["low"] > min(prices["open"], prices["close"])
            or prices["high"] < max(prices["open"], prices["close"])
            or prices["low"] > prices["high"]
        ):
            raise ValueError(f"candles[{index}] has inconsistent OHLC prices")
        if _number(candle.volume, f"candles[{index}].volume") < 0:
            raise ValueError(f"candles[{index}].volume must be nonnegative")
        signal = _number(signals[index], f"signals[{index}]")
        if not 0 <= signal <= 1:
            raise ValueError(f"signals[{index}] must be in [0, 1]; shorts and leverage are not supported")


def backtest(
    candles: Sequence[Candle],
    signals: Sequence[float],
    settings: BacktestSettings | None = None,
) -> dict:
    """Return JSON-safe results without network access or real order execution.

    ``trade_count`` counts fills; ``closed_trades`` counts completed round trips.
    ``exposure`` is the fraction of observed bar closes holding a position.
    ``final_exposure`` is final marked position value divided by final equity.
    Unrealized P&L subtracts the entry cost/fee, with no assumed future exit fee.
    ``halted`` stays true once the close-observed drawdown reaches the stop.
    ``liquidation_pending`` exposes an open position when no later bar exists.
    """
    settings = settings if settings is not None else BacktestSettings()
    _validate_settings(settings)
    _validate_inputs(candles, signals)

    # Decimal protects cash and lot boundaries from binary rounding. High
    # precision also covers the exponent range of otherwise finite float inputs.
    with localcontext() as context:
        context.prec = 1500
        initial_cash = _decimal(settings.initial_cash)
        cash = initial_cash
        quantity = Decimal(0)
        entry_cost = Decimal(0)
        entry_price = None
        fees_paid = Decimal(0)
        peak_equity = initial_cash
        maximum_drawdown = Decimal(0)
        fee_rate = _decimal(settings.fee_rate)
        slippage = _decimal(settings.slippage_bps) / Decimal(10_000)
        allocation = _decimal(settings.allocation)
        minimum = _decimal(settings.min_notional)
        step_size = _decimal(settings.step_size)
        stop = _decimal(settings.max_drawdown_stop)
        trades = []
        equity_curve = []
        closed_trades = 0
        wins = 0
        exposed_closes = 0
        halted = False
        liquidation_pending = False

        for index, candle in enumerate(candles):
            if index:
                signal = _decimal(signals[index - 1])
                if quantity > 0 and (liquidation_pending or signal == 0):
                    price = _decimal(candle.open) * (1 - slippage)
                    notional = quantity * price
                    fee = notional * fee_rate
                    proceeds = notional - fee
                    cash += proceeds
                    fees_paid += fee
                    closed_trades += 1
                    wins += proceeds > entry_cost
                    trades.append({
                        "timestamp_ms": candle.timestamp_ms,
                        "side": "sell",
                        "quantity": _json_number(quantity),
                        "price": _json_number(price),
                        "fee": _json_number(fee),
                        "notional": _json_number(notional),
                    })
                    quantity = Decimal(0)
                    entry_cost = Decimal(0)
                    entry_price = None
                    liquidation_pending = False
                elif not halted and quantity == 0 and signal > 0:
                    price = _decimal(candle.open) * (1 + slippage)
                    budget = cash * allocation * signal
                    step_cost = step_size * price * (1 + fee_rate)
                    # Integer division of exact decimal ratios makes floor
                    # sizing safe even at exact affordable-lot boundaries.
                    budget_numerator, budget_denominator = budget.as_integer_ratio()
                    cost_numerator, cost_denominator = step_cost.as_integer_ratio()
                    steps = (
                        budget_numerator * cost_denominator
                        // (budget_denominator * cost_numerator)
                    )
                    candidate_quantity = Decimal(steps) * step_size
                    notional = candidate_quantity * price
                    fee = notional * fee_rate
                    debit = notional + fee
                    if candidate_quantity > 0 and notional >= minimum:
                        if debit > budget or debit > cash:
                            raise ValueError("Order sizing exceeded its cash budget")
                        # Check representability before changing the account.
                        fill = {
                            "timestamp_ms": candle.timestamp_ms,
                            "side": "buy",
                            "quantity": _json_number(candidate_quantity),
                            "price": _json_number(price),
                            "fee": _json_number(fee),
                            "notional": _json_number(notional),
                        }
                        cash -= debit
                        quantity = candidate_quantity
                        entry_cost = debit
                        entry_price = price
                        fees_paid += fee
                        trades.append(fill)

            market_value = quantity * _decimal(candle.close)
            equity = cash + market_value
            peak_equity = max(peak_equity, equity)
            drawdown = (peak_equity - equity) / peak_equity
            maximum_drawdown = max(maximum_drawdown, drawdown)
            if not halted and peak_equity - equity >= peak_equity * stop:
                halted = True
                liquidation_pending = quantity > 0
            exposed_closes += quantity > 0
            equity_curve.append({
                "timestamp_ms": candle.timestamp_ms,
                "equity": _json_number(equity),
                "drawdown": _json_number(drawdown),
            })

        unrealized_pnl = market_value - entry_cost if quantity > 0 else Decimal(0)
        return {
            "metrics": {
                "final_equity": _json_number(equity),
                "total_return": _json_number(equity / initial_cash - 1),
                "max_drawdown": _json_number(maximum_drawdown),
                "fees_paid": _json_number(fees_paid),
                "trade_count": len(trades),
                "closed_trades": closed_trades,
                "win_rate": wins / closed_trades if closed_trades else None,
                "exposure": exposed_closes / len(candles),
                "final_exposure": _json_number(market_value / equity),
                "halted": halted,
            },
            "equity_curve": equity_curve,
            "trades": trades,
            "position": {
                "cash": _json_number(cash),
                "quantity": _json_number(quantity),
                "mark_price": _json_number(_decimal(candles[-1].close)),
                "market_value": _json_number(market_value),
                "entry_price": _json_number(entry_price) if entry_price is not None else None,
                "cost_basis": _json_number(entry_cost),
                "unrealized_pnl": _json_number(unrealized_pnl),
                "liquidation_pending": liquidation_pending,
            },
        }
