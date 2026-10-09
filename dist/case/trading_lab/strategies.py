"""Causal, long-only strategy signals for offline research.

Each returned value is the desired exposure at that candle's CLOSE. A
backtester must apply it no earlier than the NEXT candle's open. These simple
rules are research baselines; their inclusion does not imply profitability.
"""

from collections import deque
from collections.abc import Sequence
from math import fsum, isclose, isfinite
from numbers import Real

from .models import Candle


_DEFAULTS = {
    "flat": {},
    "buy_hold": {},
    "sma_cross": {"fast": 10, "slow": 30},
    "breakout": {"lookback": 20, "exit_lookback": 10},
    "rsi_reversion": {"period": 14, "entry": 30.0, "exit": 55.0},
}


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{label} must be a finite number") from None
    if not isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _validated_params(strategy: str, params: dict | None) -> dict:
    if not isinstance(strategy, str) or strategy not in _DEFAULTS:
        raise ValueError(f"unknown strategy: {strategy!r}")
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be a dict or None")
    supplied = {} if params is None else params
    unknown = supplied.keys() - _DEFAULTS[strategy].keys()
    if unknown:
        names = ", ".join(sorted(repr(key) for key in unknown))
        raise ValueError(f"unknown parameters for {strategy}: {names}")
    settings = _DEFAULTS[strategy] | supplied
    if strategy == "sma_cross":
        fast = _positive_integer(settings["fast"], "fast")
        slow = _positive_integer(settings["slow"], "slow")
        if fast >= slow:
            raise ValueError("fast must be less than slow")
    elif strategy == "breakout":
        lookback = _positive_integer(settings["lookback"], "lookback")
        exit_lookback = _positive_integer(settings["exit_lookback"], "exit_lookback")
        if exit_lookback > lookback:
            raise ValueError("exit_lookback must be no greater than lookback")
    elif strategy == "rsi_reversion":
        _positive_integer(settings["period"], "period")
        settings["entry"] = _finite_number(settings["entry"], "entry")
        settings["exit"] = _finite_number(settings["exit"], "exit")
        if not 0 <= settings["entry"] < settings["exit"] <= 100:
            raise ValueError("RSI thresholds must satisfy 0 <= entry < exit <= 100")
    return settings


def _validated_candles(candles: Sequence[Candle]) -> list[Candle]:
    if not isinstance(candles, Sequence):
        raise ValueError("candles must be a sequence of Candle records")
    records = list(candles)
    previous_timestamp = -1
    for index, candle in enumerate(records):
        label = f"candle {index}"
        if not isinstance(candle, Candle):
            raise ValueError(f"{label} must be a Candle")
        timestamp = candle.timestamp_ms
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError(f"{label} timestamp_ms must be a nonnegative integer")
        if timestamp <= previous_timestamp:
            raise ValueError("candle timestamps must be strictly increasing")
        previous_timestamp = timestamp
        prices = {
            field: _finite_number(getattr(candle, field), f"{label} {field}")
            for field in ("open", "high", "low", "close")
        }
        if any(value <= 0 for value in prices.values()):
            raise ValueError(f"{label} prices must be positive")
        if prices["high"] < max(prices.values()) or prices["low"] > min(prices.values()):
            raise ValueError(f"{label} has inconsistent OHLC prices")
        volume = _finite_number(candle.volume, f"{label} volume")
        if volume < 0:
            raise ValueError(f"{label} volume must be nonnegative")
    return records


def _sma_signals(candles: list[Candle], fast: int, slow: int) -> list[float]:
    signals = []
    fast_average = 0.0
    slow_average = 0.0
    # Remove expired values before adding new ones to keep means bounded.
    # Periodically recompute with fsum to bound rolling arithmetic drift.
    for index, candle in enumerate(candles):
        if index >= fast:
            fast_average -= candles[index - fast].close / fast
        if index >= slow:
            slow_average -= candles[index - slow].close / slow
        fast_average += candle.close / fast
        slow_average += candle.close / slow
        if (index + 1) % slow == 0:
            fast_average = fsum(item.close / fast for item in candles[index + 1 - fast:index + 1])
            slow_average = fsum(item.close / slow for item in candles[index + 1 - slow:index + 1])
        above = fast_average > slow_average and not isclose(
            fast_average, slow_average, rel_tol=1e-12, abs_tol=0.0
        )
        signals.append(float(index + 1 >= slow and above))
    return signals


def _breakout_signals(candles: list[Candle], lookback: int, exit_lookback: int) -> list[float]:
    signals = []
    highs: deque[int] = deque()
    lows: deque[int] = deque()
    holding = False
    for index, candle in enumerate(candles):
        # Evict expired observations before comparing with prior candles only.
        while highs and highs[0] < index - lookback:
            highs.popleft()
        while lows and lows[0] < index - exit_lookback:
            lows.popleft()
        if index >= lookback:
            if holding:
                if candle.close < candles[lows[0]].low:
                    holding = False
            elif candle.close > candles[highs[0]].high:
                holding = True
        signals.append(float(holding))
        while highs and candles[highs[-1]].high <= candle.high:
            highs.pop()
        highs.append(index)
        while lows and candles[lows[-1]].low >= candle.low:
            lows.pop()
        lows.append(index)
    return signals


def _rsi_signals(candles: list[Candle], period: int, entry: float, exit: float) -> list[float]:
    signals = []
    average_gain = 0.0
    average_loss = 0.0
    holding = False
    for index, candle in enumerate(candles):
        if index:
            change = candle.close - candles[index - 1].close
            gain = max(change, 0.0)
            loss = max(-change, 0.0)
            if index <= period:
                average_gain += gain / period
                average_loss += loss / period
            else:
                # Wilder smoothing: each new change has weight 1 / period.
                average_gain = average_gain * ((period - 1) / period) + gain / period
                average_loss = average_loss * ((period - 1) / period) + loss / period
        if index >= period:
            if average_gain == 0 and average_loss == 0:
                rsi = 50.0
            elif average_loss == 0:
                rsi = 100.0
            elif average_gain == 0:
                rsi = 0.0
            elif average_gain >= average_loss:
                rsi = 100.0 / (1.0 + average_loss / average_gain)
            else:
                ratio = average_gain / average_loss
                rsi = 100.0 * ratio / (1.0 + ratio)
            if holding:
                if rsi >= exit:
                    holding = False
            elif rsi <= entry:
                holding = True
        signals.append(float(holding))
    return signals


def generate_signals(
    candles: Sequence[Candle], strategy: str, params: dict | None = None
) -> list[float]:
    """Return one 0.0 (flat) or 1.0 (long) signal per candle close.

    ``flat`` always stays out; ``buy_hold`` requests exposure at the first
    close. ``sma_cross`` holds when the fast closing-price mean exceeds the
    slow mean, after ``slow`` closes; differences within 1e-12 relative
    tolerance count as tied and flat. ``breakout`` enters above the highest
    HIGH in the preceding ``lookback`` candles and exits below the lowest LOW
    in the preceding ``exit_lookback`` candles; equality does not trigger.
    ``rsi_reversion`` enters at Wilder RSI <= ``entry`` and exits at RSI >=
    ``exit`` after ``period`` price changes. An unchanged series has RSI 50.

    Warmup signals are flat. Computation uses only present and prior candles.
    Invalid candle records, unknown parameters and invalid settings raise
    ValueError, including when the candle sequence is empty. Parameters and
    input records are never mutated.
    """
    settings = _validated_params(strategy, params)
    records = _validated_candles(candles)
    if strategy == "flat":
        return [0.0] * len(records)
    if strategy == "buy_hold":
        return [1.0] * len(records)
    if strategy == "sma_cross":
        return _sma_signals(records, **settings)
    if strategy == "breakout":
        return _breakout_signals(records, **settings)
    return _rsi_signals(records, **settings)


def candidate_grid() -> list[dict]:
    """Return a fixed, deliberately small comparison set of twelve rules.

    The two baseline rules and ten parameter choices are declared in advance.
    This is a research comparison set, not an exhaustive optimization search.
    Every call returns fresh dictionaries so callers can safely annotate them.
    """
    return [
        {"name": "flat", "params": {}},
        {"name": "buy_hold", "params": {}},
        {"name": "sma_cross", "params": {"fast": 5, "slow": 20}},
        {"name": "sma_cross", "params": {"fast": 10, "slow": 40}},
        {"name": "sma_cross", "params": {"fast": 20, "slow": 60}},
        {"name": "breakout", "params": {"lookback": 12, "exit_lookback": 6}},
        {"name": "breakout", "params": {"lookback": 24, "exit_lookback": 12}},
        {"name": "breakout", "params": {"lookback": 48, "exit_lookback": 24}},
        {"name": "rsi_reversion", "params": {"period": 7, "entry": 20, "exit": 55}},
        {"name": "rsi_reversion", "params": {"period": 14, "entry": 25, "exit": 55}},
        {"name": "rsi_reversion", "params": {"period": 14, "entry": 30, "exit": 60}},
        {"name": "rsi_reversion", "params": {"period": 21, "entry": 30, "exit": 55}},
    ]
