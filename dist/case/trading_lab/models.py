"""Shared, dependency-free contracts for the research system."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Candle:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class BacktestSettings:
    initial_cash: float = 1000.0
    fee_rate: float = 0.001
    slippage_bps: float = 5.0
    allocation: float = 0.25
    min_notional: float = 10.0
    step_size: float = 0.000001
    max_drawdown_stop: float = 0.15
    annual_periods: int = 8760
