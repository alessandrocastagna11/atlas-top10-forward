#!/usr/bin/env python3
"""Pre-registered T9 bear-exposure comparison with Italian tax accounting.

T9A adds a broad-market sleeve during structural bear markets using a simple
drawdown ladder.  T9B adds exposure only after an explicit stabilization
signal.  Both keep the frozen V2 SMA50/200 stock-selection model and the T8
volatility-shock overlay for the stock sleeve.

The tax ledger approximates an Italian resident using the administered regime:
historical 20%/26% rates, four-tax-year loss carryforward, weighted-average
cost, U.S. dividend withholding plus Italian tax on the net-border amount,
taxed cash interest and historical stamp duty.  This is research software, not
tax, investment or execution advice.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import html
import json
import math
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import equity_long_backtest as v1
import equity_long_backtest_v2 as v2
import equity_long_overlay_comparison as overlays_module


STRATEGIES = (
    "V2 baseline",
    "T8 Volatility shock",
    "T9A Drawdown ladder",
    "T9B Bear rebound",
)
TRIAL_COUNT = 22
RATE_CHANGE_DATE = pd.Timestamp("2014-07-01")
CORPORATE_ACTION_YIELD_THRESHOLD = 0.10


@dataclasses.dataclass(frozen=True)
class TaxConfig:
    name: str
    enabled: bool
    us_dividend_withholding_rate: float = 0.15
    loss_carry_years: int = 4


@dataclasses.dataclass(frozen=True)
class ExecutionPolicy:
    """Execution controls that do not alter the strategy's alpha signals.

    Defaults reproduce the original T9 implementation exactly.  Slower policies
    can widen only the risk-reduction no-trade band and/or batch sleeve increases
    monthly while retaining immediate sleeve entries and weekly risk reductions.
    """

    name: str = "weekly_reference"
    core_risk_band_weight: float = 0.025
    core_risk_minimum_eur: float = 250.0
    sleeve_band_weight: float = 0.025
    sleeve_minimum_eur: float = 250.0
    sleeve_increase_frequency: str = "weekly"
    sleeve_immediate_entry: bool = True
    sleeve_immediate_reductions: bool = True

    def __post_init__(self) -> None:
        if self.sleeve_increase_frequency not in {"weekly", "monthly"}:
            raise ValueError("sleeve_increase_frequency must be 'weekly' or 'monthly'")
        if self.core_risk_band_weight < 0.0 or self.sleeve_band_weight < 0.0:
            raise ValueError("rebalance bands must be non-negative")
        if self.core_risk_minimum_eur < 0.0 or self.sleeve_minimum_eur < 0.0:
            raise ValueError("minimum trade sizes must be non-negative")


@dataclasses.dataclass
class RawMarketData:
    open_eur: pd.DataFrame
    close_eur: pd.DataFrame
    distribution_eur_per_share: pd.DataFrame
    taxable_distribution: pd.DataFrame
    events: pd.DataFrame


@dataclasses.dataclass
class TaxAwareResult:
    name: str
    scenario: str
    equity: pd.Series
    exposure: pd.Series
    orders: pd.DataFrame
    realized_lots: pd.DataFrame
    allocations: pd.DataFrame
    tax_ledger: pd.DataFrame
    total_fees_eur: float
    estimated_slippage_eur: float
    cash_interest_eur: float
    total_traded_eur: float
    capital_gains_tax_eur: float
    foreign_dividend_tax_eur: float
    italian_dividend_tax_eur: float
    interest_tax_eur: float
    stamp_duty_eur: float
    gross_dividends_eur: float
    gross_interest_eur: float
    losses_used_eur: float
    losses_expired_eur: float
    ending_loss_credit_nominal_eur: float
    ending_loss_credit_usable_eur: float

    @property
    def total_tax_eur(self) -> float:
        return (
            self.capital_gains_tax_eur
            + self.foreign_dividend_tax_eur
            + self.italian_dividend_tax_eur
            + self.interest_tax_eur
            + self.stamp_duty_eur
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-start", default="2007-01-01")
    parser.add_argument("--test-start", default="2012-01-03")
    parser.add_argument("--end", default=(dt.date.today() + dt.timedelta(days=1)).isoformat())
    parser.add_argument("--capital", type=float, default=10_000.0)
    parser.add_argument("--cache-dir", default="data_cache")
    parser.add_argument("--input-dir", default="inputs")
    parser.add_argument("--output-dir", default="output_bear_tax")
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--refresh", action="store_true")
    return parser.parse_args()


def italian_financial_rate(date: pd.Timestamp) -> float:
    """Historical ordinary financial-income rate for this sample."""
    return 0.20 if date < RATE_CHANGE_DATE else 0.26


def historical_stamp_rate(date: pd.Timestamp) -> float:
    if date.year <= 2012:
        return 0.0010
    if date.year == 2013:
        return 0.0015
    return 0.0020


def net_cash_rate(cash_rate: pd.Series, tax_config: TaxConfig) -> pd.Series:
    if not tax_config.enabled:
        return cash_rate.copy()
    rates = pd.Series(
        [italian_financial_rate(date) for date in cash_rate.index], index=cash_rate.index
    )
    return cash_rate * (1.0 - rates)


class LossLedger:
    """Nominal losses by origin year/rate, consumed oldest first."""

    def __init__(self, carry_years: int) -> None:
        self.carry_years = carry_years
        self.buckets: dict[tuple[int, float], float] = {}
        self.expired_nominal = 0.0
        self.used_equivalent = 0.0

    def expire(self, current_year: int) -> float:
        expired = 0.0
        for key in sorted(list(self.buckets)):
            origin_year, _ = key
            if origin_year + self.carry_years < current_year:
                expired += self.buckets.pop(key)
        self.expired_nominal += expired
        return expired

    def add(self, date: pd.Timestamp, nominal_loss: float, origin_rate: float) -> None:
        if nominal_loss <= 0.0:
            return
        key = (date.year, round(float(origin_rate), 8))
        self.buckets[key] = self.buckets.get(key, 0.0) + nominal_loss

    def offset(self, date: pd.Timestamp, gain: float, current_rate: float) -> tuple[float, float, float]:
        """Return taxable gain, gain-equivalent loss used, and expired nominal loss."""
        expired = self.expire(date.year)
        remaining = max(0.0, gain)
        used_equivalent = 0.0
        if remaining <= 0.0 or current_rate <= 0.0:
            return remaining, used_equivalent, expired
        for key in sorted(list(self.buckets)):
            if remaining <= 1e-12:
                break
            origin_rate = key[1]
            conversion = min(1.0, origin_rate / current_rate) if current_rate else 1.0
            available_equivalent = self.buckets[key] * conversion
            used = min(remaining, available_equivalent)
            if used <= 0.0:
                continue
            nominal_used = used / conversion
            self.buckets[key] -= nominal_used
            if self.buckets[key] <= 1e-9:
                self.buckets.pop(key)
            remaining -= used
            used_equivalent += used
        self.used_equivalent += used_equivalent
        return remaining, used_equivalent, expired

    def nominal_balance(self, current_year: int) -> float:
        self.expire(current_year)
        return float(sum(self.buckets.values()))

    def usable_balance(self, current_date: pd.Timestamp) -> float:
        self.expire(current_date.year)
        rate = italian_financial_rate(current_date)
        return float(
            sum(value * min(1.0, origin_rate / rate) for (_, origin_rate), value in self.buckets.items())
        )

    def clone(self) -> "LossLedger":
        other = LossLedger(self.carry_years)
        other.buckets = dict(self.buckets)
        other.expired_nominal = self.expired_nominal
        other.used_equivalent = self.used_equivalent
        return other


def reconstruct_raw_market_data(
    data: Mapping[str, pd.DataFrame],
    index: pd.DatetimeIndex,
    symbols: Sequence[str],
) -> RawMarketData:
    """Recover split-adjusted cash prices and infer distributions from Yahoo factors.

    Cached OHLC is dividend-adjusted while RawClose is only split-adjusted.  A
    positive change in Close/RawClose identifies a cash-equivalent distribution.
    Jumps over 10% are kept as non-taxed corporate-action proxies so spin-offs do
    not become ordinary dividends.
    """
    fx = data[v1.FX_TICKER]["Close"].reindex(index).ffill().bfill()
    execution_fx = fx.shift(1).bfill()
    raw_open: dict[str, pd.Series] = {}
    raw_close: dict[str, pd.Series] = {}
    distributions: dict[str, pd.Series] = {}
    taxable: dict[str, pd.Series] = {}
    event_rows: list[dict] = []
    for symbol in symbols:
        frame = data[symbol].reindex(index)
        factor = frame["Close"].div(frame["RawClose"].replace(0.0, np.nan))
        raw_open_usd = frame["Open"].div(factor.replace(0.0, np.nan))
        raw_close_usd = frame["RawClose"]
        implied_yield = 1.0 - factor.shift(1).div(factor)
        valid_event = implied_yield.gt(1e-5) & implied_yield.lt(0.50)
        implied_yield = implied_yield.where(valid_event, 0.0)
        distribution_usd = implied_yield * raw_close_usd.shift(1)
        distribution_eur = distribution_usd.div(fx)
        is_taxable = valid_event & implied_yield.le(CORPORATE_ACTION_YIELD_THRESHOLD)
        raw_open[symbol] = raw_open_usd.div(execution_fx)
        raw_close[symbol] = raw_close_usd.div(fx)
        distributions[symbol] = distribution_eur.fillna(0.0)
        taxable[symbol] = is_taxable.fillna(False)
        for date in index[valid_event.fillna(False)]:
            event_rows.append(
                {
                    "date": date,
                    "ticker": symbol,
                    "implied_gross_yield": float(implied_yield.loc[date]),
                    "distribution_usd_per_share": float(distribution_usd.loc[date]),
                    "distribution_eur_per_share": float(distribution_eur.loc[date]),
                    "classification": "ordinary_dividend" if bool(is_taxable.loc[date]) else "corporate_action_proxy",
                }
            )
    return RawMarketData(
        open_eur=pd.DataFrame(raw_open, index=index),
        close_eur=pd.DataFrame(raw_close, index=index),
        distribution_eur_per_share=pd.DataFrame(distributions, index=index),
        taxable_distribution=pd.DataFrame(taxable, index=index, dtype=bool),
        events=pd.DataFrame(event_rows),
    )


def bear_exposure_signals(
    data: Mapping[str, pd.DataFrame],
    features: v1.FeatureBundle,
    t8_overlay: pd.Series,
) -> pd.DataFrame:
    index = features.close_usd.index
    spy = data[v1.BENCHMARK]["Close"].reindex(index)
    sma20 = spy.rolling(20, min_periods=16).mean()
    sma50 = spy.rolling(50, min_periods=40).mean()
    sma80 = spy.rolling(80, min_periods=64).mean()
    sma200 = spy.rolling(200, min_periods=160).mean()
    drawdown = spy.div(spy.cummax()).sub(1.0)
    structural_bear = (spy < sma200) & (sma50 < sma200)

    t9a_floor = pd.Series(0.0, index=index)
    t9a_floor.loc[structural_bear] = 0.25
    t9a_floor.loc[structural_bear & drawdown.le(-0.20)] = 0.35
    t9a_floor.loc[structural_bear & drawdown.le(-0.30)] = 0.45

    stock_sma80 = features.close_usd.rolling(80, min_periods=64).mean()
    valid = features.close_usd.notna() & stock_sma80.notna()
    breadth80 = (features.close_usd.gt(stock_sma80) & valid).sum(axis=1).div(
        valid.sum(axis=1).replace(0, np.nan)
    )
    breadth_improving_10 = breadth80.gt(breadth80.shift(10))
    above_sma20_3 = spy.gt(sma20).rolling(3, min_periods=3).sum().eq(3)
    new_low20 = spy.le(spy.shift(1).rolling(20, min_periods=15).min())
    recovery_gate = t8_overlay.gt(0.90) & above_sma20_3 & breadth_improving_10
    reset = t8_overlay.lt(0.75) | new_low20

    states: list[int] = []
    state = 0
    for date in index:
        if not bool(structural_bear.loc[date]):
            state = 0
        elif bool(reset.loc[date]):
            state = 0
        elif state == 0 and bool(recovery_gate.loc[date]):
            state = 2 if bool(spy.loc[date] > sma80.loc[date]) else 1
        elif state == 1 and bool(spy.loc[date] > sma80.loc[date]):
            state = 2
        elif state == 2 and not bool(spy.loc[date] > sma80.loc[date]):
            state = 1
        states.append(state)
    recovery_state = pd.Series(states, index=index, dtype="int64")
    t9b_floor = pd.Series(0.0, index=index)
    t9b_floor.loc[structural_bear] = 0.25
    t9b_floor.loc[structural_bear & recovery_state.eq(1)] = 0.35
    t9b_floor.loc[structural_bear & recovery_state.eq(2)] = 0.45

    return pd.DataFrame(
        {
            "spy_close_adjusted": spy,
            "sma20": sma20,
            "sma50": sma50,
            "sma80": sma80,
            "sma200": sma200,
            "spy_drawdown": drawdown,
            "structural_bear": structural_bear,
            "t8_overlay": t8_overlay,
            "breadth_above_sma80": breadth80,
            "breadth_improving_vs_10_sessions": breadth_improving_10,
            "spy_above_sma20_three_sessions": above_sma20_3,
            "new_20_session_low": new_low20,
            "recovery_gate": recovery_gate,
            "reset_gate": reset,
            "t9b_recovery_state": recovery_state,
            "t9a_exposure_floor": t9a_floor,
            "t9b_exposure_floor": t9b_floor,
        }
    )


def simulate_tax_aware(
    features: v1.FeatureBundle,
    score: pd.DataFrame,
    eligible: pd.DataFrame,
    membership: pd.DataFrame,
    regime: pd.Series,
    cash_rate: pd.Series,
    execution_costs: Optional[pd.DataFrame],
    cfg: v2.V2Config,
    raw_market: RawMarketData,
    test_start: str,
    initial_capital: float,
    strategy_name: str,
    tax_config: TaxConfig,
    weekly_risk_overlay: Optional[pd.Series] = None,
    bear_floor: Optional[pd.Series] = None,
    execution_policy: Optional[ExecutionPolicy] = None,
    cash_reserve_fraction: float = 0.0,
    name_selector: Optional[Callable] = None,
    target_weight_builder: Optional[Callable] = None,
) -> TaxAwareResult:
    if not 0.0 <= cash_reserve_fraction < 1.0:
        raise ValueError("cash_reserve_fraction must be in [0, 1)")
    index = features.close_eur.index
    sim_index = index[index >= pd.Timestamp(test_start)]
    execution_map = v1._weekly_execution_map(index, pd.Timestamp(test_start))
    close_values = raw_market.close_eur.ffill(limit=5)
    open_values = raw_market.open_eur
    rank = score.where(eligible & membership).rank(axis=1, ascending=False, method="min")
    positions: dict[str, v2.Position] = {}
    cash = float(initial_capital)
    previous_date: Optional[pd.Timestamp] = None
    previous_invested = 0.0
    equity_rows: list[float] = []
    exposure_rows: list[float] = []
    order_rows: list[dict] = []
    realized_rows: list[dict] = []
    allocation_rows: list[dict] = []
    tax_rows: list[dict] = []
    total_fees = total_slippage = total_interest = total_traded = 0.0
    capital_tax = foreign_dividend_tax = italian_dividend_tax = 0.0
    interest_tax = stamp_duty = gross_dividends = gross_interest = 0.0
    decision_count = 0
    policy = execution_policy or ExecutionPolicy()
    last_weekly_execution_month: Optional[pd.Period] = None
    loss_ledger = LossLedger(tax_config.loss_carry_years)
    overlay = (
        weekly_risk_overlay.reindex(index).ffill().fillna(1.0).clip(0.0, 1.0)
        if weekly_risk_overlay is not None
        else pd.Series(1.0, index=index)
    )
    floor_series = (
        bear_floor.reindex(index).ffill().fillna(0.0).clip(0.0, 1.0)
        if bear_floor is not None
        else pd.Series(0.0, index=index)
    )

    def slip_bps(date: pd.Timestamp, ticker: str) -> float:
        if execution_costs is not None and ticker in execution_costs.columns:
            value = execution_costs.at[date, ticker]
            if pd.notna(value):
                return max(0.0, float(value))
        return cfg.default_slippage_bps_per_side

    def nav_at(date: pd.Timestamp, prices: pd.DataFrame) -> float:
        return cash + sum(
            position.shares * float(prices.at[date, ticker])
            for ticker, position in positions.items()
            if ticker in prices.columns and pd.notna(prices.at[date, ticker])
        )

    def position_value(date: pd.Timestamp, ticker: str, prices: pd.DataFrame) -> float:
        if ticker not in positions or ticker not in prices.columns or pd.isna(prices.at[date, ticker]):
            return 0.0
        return positions[ticker].shares * float(prices.at[date, ticker])

    def liquidation_nav(date: pd.Timestamp) -> float:
        """Daily net wealth if every open holding were liquidated at this close.

        Realized tax remains cash-accounted only when a sale occurs.  This
        parallel mark prevents the final liquidation from creating an
        artificial one-day tax cliff in risk statistics.
        """
        operational_nav = nav_at(date, close_values)
        if not positions:
            return operational_nav
        hypothetical_losses = loss_ledger.clone()
        deductions = 0.0
        current_rate = italian_financial_rate(date) if tax_config.enabled else 0.0
        for ticker, position in positions.items():
            reference_price = close_values.at[date, ticker]
            if pd.isna(reference_price):
                continue
            bps = slip_bps(date, ticker)
            execution_price = float(reference_price) * (1.0 - bps / 10_000.0)
            gross = position.shares * execution_price
            proceeds = max(0.0, gross - cfg.fixed_fee_eur)
            pnl = proceeds - position.cost_basis_eur
            if tax_config.enabled and pnl < 0.0:
                hypothetical_losses.expire(date.year)
                hypothetical_losses.add(date, -pnl, current_rate)
                tax = 0.0
            elif tax_config.enabled and pnl > 0.0:
                taxable_gain, _, _ = hypothetical_losses.offset(date, pnl, current_rate)
                tax = taxable_gain * current_rate
            else:
                tax = 0.0
            close_value = position.shares * float(reference_price)
            deductions += close_value - proceeds + tax
        return operational_nav - deductions

    def sell(
        date: pd.Timestamp,
        decision_date: pd.Timestamp,
        ticker: str,
        reference_price: float,
        requested_value: float,
        reason: str,
    ) -> None:
        nonlocal cash, total_fees, total_slippage, total_traded, capital_tax
        position = positions[ticker]
        maximum_value = position.shares * reference_price
        reference_value = min(maximum_value, max(0.0, requested_value))
        sell_shares = min(position.shares, reference_value / reference_price)
        if sell_shares <= 1e-12:
            return
        fraction = sell_shares / position.shares
        basis = position.cost_basis_eur * fraction
        entry_date = position.first_entry_date
        bps = slip_bps(date, ticker)
        execution_price = reference_price * (1.0 - bps / 10_000.0)
        gross = sell_shares * execution_price
        fee = cfg.fixed_fee_eur
        proceeds = max(0.0, gross - fee)
        pnl = proceeds - basis
        current_rate = italian_financial_rate(date) if tax_config.enabled else 0.0
        if tax_config.enabled and pnl < 0.0:
            expired = loss_ledger.expire(date.year)
            loss_ledger.add(date, -pnl, current_rate)
            taxable_gain = loss_used = 0.0
        elif tax_config.enabled and pnl > 0.0:
            taxable_gain, loss_used, expired = loss_ledger.offset(date, pnl, current_rate)
        else:
            taxable_gain = max(0.0, pnl)
            loss_used = expired = 0.0
        tax = taxable_gain * current_rate
        cash += proceeds - tax
        capital_tax += tax
        total_fees += fee
        total_slippage += sell_shares * reference_price * bps / 10_000.0
        total_traded += sell_shares * reference_price
        position.shares -= sell_shares
        position.cost_basis_eur -= basis
        is_full = position.shares <= 1e-9
        if is_full:
            positions.pop(ticker)
        realized_rows.append(
            {
                "scenario": tax_config.name,
                "strategy": strategy_name,
                "ticker": ticker,
                "entry_date": entry_date,
                "exit_date": date,
                "cost_basis_eur": basis,
                "proceeds_eur": proceeds,
                "pnl_eur": pnl,
                "taxable_gain_eur": taxable_gain,
                "loss_used_eur": loss_used,
                "capital_gains_tax_eur": tax,
                "return_pct": 100.0 * (proceeds / basis - 1.0) if basis > 0.0 else np.nan,
                "calendar_days": (date - entry_date).days,
                "full_exit": is_full,
                "reason": reason,
            }
        )
        tax_rows.append(
            {
                "date": date,
                "type": "realized_sale",
                "ticker": ticker,
                "gross_base_eur": max(0.0, pnl),
                "foreign_tax_eur": 0.0,
                "italian_tax_eur": tax,
                "tax_rate": current_rate,
                "loss_used_eur": loss_used,
                "expired_loss_eur": expired,
                "net_cash_flow_eur": proceeds - tax,
            }
        )
        order_rows.append(
            {
                "scenario": tax_config.name,
                "strategy": strategy_name,
                "decision_date": decision_date,
                "date": date,
                "action": "SELL",
                "ticker": ticker,
                "shares": sell_shares,
                "reference_price_eur": reference_price,
                "execution_price_eur": execution_price,
                "reference_value_eur": sell_shares * reference_price,
                "fee_eur": fee,
                "slippage_bps_per_side": bps,
                "capital_gains_tax_eur": tax,
                "reason": reason,
            }
        )

    def buy(
        date: pd.Timestamp,
        decision_date: pd.Timestamp,
        ticker: str,
        reference_price: float,
        requested_value: float,
        reason: str,
    ) -> None:
        nonlocal cash, total_fees, total_slippage, total_traded
        fee = cfg.fixed_fee_eur
        reserve = cash_reserve_fraction * max(0.0, nav_at(date, open_values))
        budget = min(requested_value + fee, max(0.0, cash - reserve))
        if budget <= fee + 1.0:
            return
        bps = slip_bps(date, ticker)
        execution_price = reference_price * (1.0 + bps / 10_000.0)
        shares = (budget - fee) / execution_price
        if shares <= 1e-12:
            return
        gross = shares * execution_price
        deployed = gross + fee
        cash -= deployed
        total_fees += fee
        total_slippage += shares * reference_price * bps / 10_000.0
        total_traded += shares * reference_price
        if ticker in positions:
            positions[ticker].shares += shares
            positions[ticker].cost_basis_eur += deployed
        else:
            positions[ticker] = v2.Position(shares, deployed, date)
        order_rows.append(
            {
                "scenario": tax_config.name,
                "strategy": strategy_name,
                "decision_date": decision_date,
                "date": date,
                "action": "BUY",
                "ticker": ticker,
                "shares": shares,
                "reference_price_eur": reference_price,
                "execution_price_eur": execution_price,
                "reference_value_eur": shares * reference_price,
                "fee_eur": fee,
                "slippage_bps_per_side": bps,
                "capital_gains_tax_eur": 0.0,
                "reason": reason,
            }
        )

    for date in sim_index:
        if previous_date is not None:
            day_count = max(0, (date - previous_date).days)
            rate = float(cash_rate.reindex(index).ffill().at[previous_date])
            interest = max(0.0, cash) * (math.exp(rate * day_count / 365.25) - 1.0)
            interest_rate = italian_financial_rate(previous_date) if tax_config.enabled else 0.0
            interest_withholding = interest * interest_rate
            stamp_rate = historical_stamp_rate(previous_date) if tax_config.enabled else 0.0
            stamp = max(0.0, previous_invested) * stamp_rate * day_count / 365.25
            cash += interest - interest_withholding - stamp
            total_interest += interest - interest_withholding
            gross_interest += interest
            interest_tax += interest_withholding
            stamp_duty += stamp
            if interest > 0.0:
                tax_rows.append(
                    {
                        "date": date,
                        "type": "cash_interest",
                        "ticker": "CASH",
                        "gross_base_eur": interest,
                        "foreign_tax_eur": 0.0,
                        "italian_tax_eur": interest_withholding,
                        "tax_rate": interest_rate,
                        "loss_used_eur": 0.0,
                        "expired_loss_eur": 0.0,
                        "net_cash_flow_eur": interest - interest_withholding,
                    }
                )
            if stamp > 0.0:
                tax_rows.append(
                    {
                        "date": date,
                        "type": "stamp_duty_accrual",
                        "ticker": "PORTFOLIO",
                        "gross_base_eur": previous_invested,
                        "foreign_tax_eur": 0.0,
                        "italian_tax_eur": stamp,
                        "tax_rate": stamp_rate * day_count / 365.25,
                        "loss_used_eur": 0.0,
                        "expired_loss_eur": 0.0,
                        "net_cash_flow_eur": -stamp,
                    }
                )

        # Cash distributions are received by holders from the prior close.
        for ticker, position in list(positions.items()):
            if ticker not in raw_market.distribution_eur_per_share.columns:
                continue
            per_share = float(raw_market.distribution_eur_per_share.at[date, ticker])
            if not np.isfinite(per_share) or per_share <= 0.0:
                continue
            gross_distribution = position.shares * per_share
            is_taxable = bool(raw_market.taxable_distribution.at[date, ticker])
            foreign_rate = tax_config.us_dividend_withholding_rate if tax_config.enabled and is_taxable else 0.0
            italian_rate = italian_financial_rate(date) if tax_config.enabled and is_taxable else 0.0
            foreign_tax = gross_distribution * foreign_rate
            frontier_net = gross_distribution - foreign_tax
            italy_tax = frontier_net * italian_rate
            net_distribution = gross_distribution - foreign_tax - italy_tax
            cash += net_distribution
            gross_dividends += gross_distribution
            foreign_dividend_tax += foreign_tax
            italian_dividend_tax += italy_tax
            tax_rows.append(
                {
                    "date": date,
                    "type": "dividend" if is_taxable else "corporate_action_proxy",
                    "ticker": ticker,
                    "gross_base_eur": gross_distribution,
                    "foreign_tax_eur": foreign_tax,
                    "italian_tax_eur": italy_tax,
                    "tax_rate": foreign_rate + (1.0 - foreign_rate) * italian_rate,
                    "loss_used_eur": 0.0,
                    "expired_loss_eur": 0.0,
                    "net_cash_flow_eur": net_distribution,
                }
            )

        previous_date = date
        decision_date = execution_map.get(date)
        decision_type: Optional[str] = None
        monthly_sleeve_review = False
        if decision_date is not None:
            decision_count += 1
            execution_month = decision_date.to_period("M")
            monthly_sleeve_review = execution_month != last_weekly_execution_month
            last_weekly_execution_month = execution_month
            nav_open_preview = nav_at(date, open_values)
            core_value_preview = sum(
                position_value(date, ticker, open_values)
                for ticker in positions
                if ticker != v1.BENCHMARK
            )
            core_exposure_preview = core_value_preview / nav_open_preview if nav_open_preview > 0.0 else 0.0
            preview_overlay = float(overlay.loc[decision_date])
            preview_regime = float(regime.loc[decision_date]) * preview_overlay
            scheduled = decision_count == 1 or decision_count % max(1, cfg.decision_interval_weeks) == 0
            emergency = core_exposure_preview > preview_regime + 0.25
            overlay_check = weekly_risk_overlay is not None and preview_overlay < 1.0
            sleeve_needed = bear_floor is not None and (
                float(floor_series.loc[decision_date]) > 0.0 or v1.BENCHMARK in positions
            )
            if scheduled:
                decision_type = "full"
            elif weekly_risk_overlay is not None and any(t != v1.BENCHMARK for t in positions) and (overlay_check or emergency):
                decision_type = "core_risk_only"
            elif sleeve_needed:
                decision_type = "sleeve_only"
            else:
                decision_date = None

        if decision_date is not None and decision_type is not None:
            nav_open = nav_at(date, open_values)
            core_positions = {ticker: position for ticker, position in positions.items() if ticker != v1.BENCHMARK}
            eligible_row = (eligible.loc[decision_date] & membership.loc[decision_date]).fillna(False)
            score_row = score.loc[decision_date]
            rank_row = rank.loc[decision_date]
            if decision_type == "full":
                selector = name_selector or v2.choose_names
                names = selector(decision_date, score_row, rank_row, eligible_row, core_positions, cfg)
            else:
                names = list(core_positions)
            overlay_multiplier = float(overlay.loc[decision_date])
            regime_multiplier = float(regime.loc[decision_date]) * overlay_multiplier

            if decision_type == "sleeve_only":
                targets = pd.Series(
                    {
                        ticker: position_value(date, ticker, open_values) / nav_open
                        for ticker in core_positions
                        if nav_open > 0.0
                    },
                    dtype="float64",
                )
                risky_vol = realized_target_vol = np.nan
                desired_exposure = float(targets.sum())
            elif names and regime_multiplier > 0.0:
                risky_vol, relative, covariance = v2.estimate_risky_volatility(
                    decision_date, names, features.close_eur, cfg.covariance_lookback
                )
                vol_scale = min(1.0, cfg.target_volatility / risky_vol) if risky_vol > 0.0 else 0.0
                desired_exposure = regime_multiplier * vol_scale
                weight_builder = target_weight_builder or v2.constrained_target_weights
                targets = weight_builder(
                    relative,
                    desired_exposure,
                    cfg,
                    decision_date=decision_date,
                )
                realized_target_vol = float(
                    math.sqrt(max(0.0, targets.to_numpy() @ covariance @ targets.to_numpy()))
                )
                if realized_target_vol > cfg.target_volatility:
                    targets *= cfg.target_volatility / realized_target_vol
                    realized_target_vol = float(
                        math.sqrt(max(0.0, targets.to_numpy() @ covariance @ targets.to_numpy()))
                    )
            else:
                risky_vol = np.nan
                desired_exposure = 0.0
                realized_target_vol = 0.0
                targets = pd.Series(dtype="float64")

            exposure_floor = float(floor_series.loc[decision_date]) if bear_floor is not None else 0.0
            core_target_exposure = float(targets.sum())
            sleeve_weight = min(
                max(0.0, 1.0 - core_target_exposure),
                max(0.0, exposure_floor - core_target_exposure),
            )
            sleeve_first_entry = (
                v1.BENCHMARK not in positions and exposure_floor > 0.0 and sleeve_weight > 0.0
            )
            sleeve_increase_allowed = (
                policy.sleeve_increase_frequency == "weekly"
                or monthly_sleeve_review
                or (policy.sleeve_immediate_entry and sleeve_first_entry)
            )
            all_targets = targets.copy()
            if sleeve_weight > 1e-12:
                all_targets.loc[v1.BENCHMARK] = sleeve_weight

            for ticker, weight in all_targets.items():
                allocation_rows.append(
                    {
                        "scenario": tax_config.name,
                        "strategy": strategy_name,
                        "decision_date": decision_date,
                        "execution_date": date,
                        "ticker": ticker,
                        "sector_etf": "MARKET" if ticker == v1.BENCHMARK else v1.TICKER_TO_SECTOR[ticker],
                        "score": np.nan if ticker == v1.BENCHMARK else score_row.get(ticker),
                        "rank": np.nan if ticker == v1.BENCHMARK else rank_row.get(ticker),
                        "target_weight": weight,
                        "regime_multiplier": regime_multiplier,
                        "risk_overlay_multiplier": overlay_multiplier,
                        "decision_type": decision_type,
                        "bear_exposure_floor": exposure_floor,
                        "core_target_exposure": core_target_exposure,
                        "sleeve_target_weight": sleeve_weight,
                        "desired_exposure": desired_exposure,
                        "estimated_target_volatility": realized_target_vol,
                        "unscaled_risky_volatility": risky_vol,
                        "execution_policy": policy.name,
                        "monthly_sleeve_review": monthly_sleeve_review,
                        "sleeve_first_entry": sleeve_first_entry,
                        "sleeve_increase_allowed": sleeve_increase_allowed,
                    }
                )

            core_threshold = (
                max(cfg.minimum_trade_eur, cfg.rebalance_band_weight * nav_open)
                if decision_type == "full"
                else max(
                    policy.core_risk_minimum_eur,
                    policy.core_risk_band_weight * nav_open,
                )
            )
            sleeve_threshold = max(
                policy.sleeve_minimum_eur,
                policy.sleeve_band_weight * nav_open,
            )
            target_values = {ticker: nav_open * float(weight) for ticker, weight in all_targets.items()}

            if decision_type in {"full", "core_risk_only"}:
                for ticker in list(core_positions):
                    price = open_values.at[date, ticker]
                    if pd.isna(price):
                        continue
                    current_value = position_value(date, ticker, open_values)
                    target_value = target_values.get(ticker, 0.0)
                    reduction = current_value - target_value
                    full_exit = ticker not in targets
                    if full_exit or reduction >= core_threshold:
                        sell(
                            date, decision_date, ticker, float(price),
                            current_value if full_exit else reduction,
                            "target_exit" if full_exit else "overlay_risk_reduction" if decision_type == "core_risk_only" else "risk_rebalance",
                        )

            if v1.BENCHMARK in positions:
                price = open_values.at[date, v1.BENCHMARK]
                if pd.notna(price):
                    current_value = position_value(date, v1.BENCHMARK, open_values)
                    target_value = target_values.get(v1.BENCHMARK, 0.0)
                    reduction = current_value - target_value
                    reduction_allowed = (
                        policy.sleeve_immediate_reductions
                        or policy.sleeve_increase_frequency == "weekly"
                        or monthly_sleeve_review
                    )
                    if reduction_allowed and (target_value <= 0.0 or reduction >= sleeve_threshold):
                        sell(
                            date, decision_date, v1.BENCHMARK, float(price),
                            current_value if target_value <= 0.0 else reduction,
                            "bear_sleeve_exit" if target_value <= 0.0 else "bear_sleeve_reduction",
                        )

            if decision_type == "full":
                nav_after_sales = nav_at(date, open_values)
                for ticker, weight in targets.sort_values(ascending=False).items():
                    active_core_count = sum(1 for key in positions if key != v1.BENCHMARK)
                    if ticker not in positions and active_core_count >= cfg.max_positions:
                        continue
                    price = open_values.at[date, ticker]
                    if pd.isna(price):
                        continue
                    current_value = position_value(date, ticker, open_values)
                    desired_value = nav_after_sales * float(weight)
                    addition = desired_value - current_value
                    if addition >= core_threshold:
                        buy(date, decision_date, ticker, float(price), addition, "target_rebalance")

            if bear_floor is not None and sleeve_weight > 0.0:
                price = open_values.at[date, v1.BENCHMARK]
                if pd.notna(price):
                    nav_before_sleeve = nav_at(date, open_values)
                    current_value = position_value(date, v1.BENCHMARK, open_values)
                    desired_value = nav_before_sleeve * sleeve_weight
                    addition = desired_value - current_value
                    if sleeve_increase_allowed and addition >= sleeve_threshold:
                        buy(date, decision_date, v1.BENCHMARK, float(price), addition, "bear_sleeve_buy")

        close_nav = nav_at(date, close_values)
        invested = close_nav - cash
        equity_rows.append(liquidation_nav(date))
        exposure_rows.append(invested / close_nav if close_nav > 0.0 else 0.0)
        previous_invested = invested

    if len(sim_index) and positions:
        final_date = sim_index[-1]
        for ticker in list(positions):
            price = close_values.at[final_date, ticker]
            if pd.notna(price):
                sell(
                    final_date,
                    final_date - pd.Timedelta(nanoseconds=1),
                    ticker,
                    float(price),
                    positions[ticker].shares * float(price),
                    "end_of_test",
                )
        equity_rows[-1] = cash
        exposure_rows[-1] = 0.0

    final_date = sim_index[-1]
    return TaxAwareResult(
        name=strategy_name,
        scenario=tax_config.name,
        equity=pd.Series(equity_rows, index=sim_index, name=strategy_name),
        exposure=pd.Series(exposure_rows, index=sim_index, name=strategy_name),
        orders=pd.DataFrame(order_rows),
        realized_lots=pd.DataFrame(realized_rows),
        allocations=pd.DataFrame(allocation_rows),
        tax_ledger=pd.DataFrame(tax_rows),
        total_fees_eur=total_fees,
        estimated_slippage_eur=total_slippage,
        cash_interest_eur=total_interest,
        total_traded_eur=total_traded,
        capital_gains_tax_eur=capital_tax,
        foreign_dividend_tax_eur=foreign_dividend_tax,
        italian_dividend_tax_eur=italian_dividend_tax,
        interest_tax_eur=interest_tax,
        stamp_duty_eur=stamp_duty,
        gross_dividends_eur=gross_dividends,
        gross_interest_eur=gross_interest,
        losses_used_eur=loss_ledger.used_equivalent,
        losses_expired_eur=loss_ledger.expired_nominal,
        ending_loss_credit_nominal_eur=loss_ledger.nominal_balance(final_date.year),
        ending_loss_credit_usable_eur=loss_ledger.usable_balance(final_date),
    )


def metric_row(
    result: TaxAwareResult,
    cash_rate: pd.Series,
    initial_capital: float,
) -> dict:
    row = {"scenario": result.scenario, "strategy": result.name}
    row.update(v2.metrics(result.equity, cash_rate, initial_capital, result, TRIAL_COUNT))
    row.update(
        {
            "capital_gains_tax_eur": result.capital_gains_tax_eur,
            "foreign_dividend_tax_eur": result.foreign_dividend_tax_eur,
            "italian_dividend_tax_eur": result.italian_dividend_tax_eur,
            "interest_tax_eur": result.interest_tax_eur,
            "stamp_duty_eur": result.stamp_duty_eur,
            "total_tax_eur": result.total_tax_eur,
            "gross_dividends_eur": result.gross_dividends_eur,
            "losses_used_eur": result.losses_used_eur,
            "losses_expired_eur": result.losses_expired_eur,
            "ending_loss_credit_nominal_eur": result.ending_loss_credit_nominal_eur,
            "ending_loss_credit_usable_eur": result.ending_loss_credit_usable_eur,
        }
    )
    return row


def period_metrics(
    results: Mapping[tuple[str, str], TaxAwareResult],
    cash_rates: Mapping[str, pd.Series],
    initial_capital: float,
) -> pd.DataFrame:
    rows = []
    periods = [
        ("2012-2016", "2012-01-03", "2016-12-31"),
        ("2017-2021", "2017-01-01", "2021-12-31"),
        ("2022+", "2022-01-01", None),
    ]
    for (scenario, strategy), result in results.items():
        for label, start, end in periods:
            curve = result.equity[result.equity.index >= pd.Timestamp(start)]
            if end:
                curve = curve[curve.index <= pd.Timestamp(end)]
            if len(curve) < 2:
                continue
            normalized = curve / curve.iloc[0] * initial_capital
            row = {"scenario": scenario, "strategy": strategy, "period": label}
            row.update(v2.metrics(normalized, cash_rates[scenario], initial_capital, declared_trials=TRIAL_COUNT))
            rows.append(row)
    return pd.DataFrame(rows)


def qualification_table(metrics: pd.DataFrame, periods: pd.DataFrame) -> pd.DataFrame:
    taxed = metrics[metrics["scenario"] == "italian_tax"]
    rows = []
    for strategy in ("T9A Drawdown ladder", "T9B Bear rebound"):
        candidate = taxed[taxed["strategy"] == strategy].iloc[0]
        candidate_periods = periods[
            (periods["scenario"] == "italian_tax") & (periods["strategy"] == strategy)
        ]
        checks = {
            "sharpe_at_least_0_90": float(candidate["sharpe_excess_cash"]) >= 0.90,
            "drawdown_not_worse_than_minus_18_pct": float(candidate["max_drawdown_pct"]) >= -18.0,
            "cagr_at_least_11_pct": float(candidate["cagr_pct"]) >= 11.0,
            "orders_at_most_700": int(candidate["orders"]) <= 700,
            "positive_cagr_all_subperiods": bool((candidate_periods["cagr_pct"] > 0.0).all()),
        }
        rows.append({"strategy": strategy, **checks, "qualifies": all(checks.values())})
    return pd.DataFrame(rows)


def comparison_vs_t8(metrics: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for scenario in ("gross", "italian_tax"):
        group = metrics[metrics["scenario"] == scenario].set_index("strategy")
        baseline = group.loc["T8 Volatility shock"]
        for strategy in ("T9A Drawdown ladder", "T9B Bear rebound"):
            row = group.loc[strategy]
            rows.append(
                {
                    "scenario": scenario,
                    "strategy": strategy,
                    "ending_value_delta_eur": row["ending_value_eur"] - baseline["ending_value_eur"],
                    "cagr_delta_pp": row["cagr_pct"] - baseline["cagr_pct"],
                    "sharpe_delta": row["sharpe_excess_cash"] - baseline["sharpe_excess_cash"],
                    "max_drawdown_delta_pp": row["max_drawdown_pct"] - baseline["max_drawdown_pct"],
                    "orders_delta": int(row["orders"] - baseline["orders"]),
                }
            )
    return pd.DataFrame(rows)


def create_charts(
    results: Mapping[tuple[str, str], TaxAwareResult],
    signals: pd.DataFrame,
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    plt.style.use("seaborn-v0_8-whitegrid")
    colors = {
        "V2 baseline": "#315C7C",
        "T8 Volatility shock": "#8A6F9E",
        "T9A Drawdown ladder": "#B87950",
        "T9B Bear rebound": "#4E8B72",
    }
    taxed = {strategy: results[("italian_tax", strategy)] for strategy in STRATEGIES}
    fig, ax = plt.subplots(figsize=(11, 5.6))
    for strategy, result in taxed.items():
        ax.plot(
            result.equity.index,
            result.equity / result.equity.iloc[0] * 100.0,
            label=strategy,
            color=colors[strategy],
            linewidth=1.65,
        )
    ax.set_yscale("log")
    ax.set_ylabel("Valore normalizzato netto imposte (log, base 100)")
    ax.set_title("T9: curve al netto della fiscalità italiana")
    ax.legend(frameon=False, ncol=2)
    fig.tight_layout()
    fig.savefig(output_dir / "equity_curves_bear_tax.png", dpi=175)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 4.6))
    for strategy, result in taxed.items():
        drawdown = (result.equity / result.equity.cummax() - 1.0) * 100.0
        ax.plot(drawdown.index, drawdown, label=strategy, color=colors[strategy], linewidth=1.35)
    ax.set_ylabel("Drawdown (%)")
    ax.set_title("Drawdown netto imposte")
    ax.legend(frameon=False, ncol=2)
    fig.tight_layout()
    fig.savefig(output_dir / "drawdowns_bear_tax.png", dpi=175)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 4.6))
    for strategy in ("T8 Volatility shock", "T9A Drawdown ladder", "T9B Bear rebound"):
        exposure = taxed[strategy].exposure.rolling(21, min_periods=1).mean() * 100.0
        ax.plot(exposure.index, exposure, label=strategy, color=colors[strategy], linewidth=1.35)
    ax.set_ylim(0.0, 102.0)
    ax.set_ylabel("Esposizione media 21 sedute (%)")
    ax.set_title("Esposizione effettiva")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output_dir / "exposure_bear_tax.png", dpi=175)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 4.2))
    plot_signals = signals.reindex(taxed["T8 Volatility shock"].equity.index)
    ax.step(plot_signals.index, plot_signals["t9a_exposure_floor"] * 100.0, where="post", label="T9A floor", color=colors["T9A Drawdown ladder"])
    ax.step(plot_signals.index, plot_signals["t9b_exposure_floor"] * 100.0, where="post", label="T9B floor", color=colors["T9B Bear rebound"])
    ax.set_ylim(-1.0, 48.0)
    ax.set_ylabel("Floor bear (%)")
    ax.set_title("Floor preregistrati per il market sleeve")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output_dir / "bear_floors.png", dpi=175)
    plt.close(fig)

    categories = ["capital_gains_tax_eur", "foreign_dividend_tax_eur", "italian_dividend_tax_eur", "interest_tax_eur", "stamp_duty_eur"]
    labels = ["Plusvalenze", "Dividendi USA", "Dividendi Italia", "Interessi", "Bollo"]
    x = np.arange(len(STRATEGIES))
    bottom = np.zeros(len(STRATEGIES))
    fig, ax = plt.subplots(figsize=(10.5, 4.8))
    palette = ["#315C7C", "#B87950", "#D9A066", "#6B8E7B", "#8A6F9E"]
    for category, label, color in zip(categories, labels, palette):
        values = np.array([getattr(taxed[strategy], category) for strategy in STRATEGIES])
        ax.bar(x, values, bottom=bottom, label=label, color=color)
        bottom += values
    ax.set_xticks(x, ["V2", "T8", "T9A", "T9B"])
    ax.set_ylabel("Imposte cumulate (€)")
    ax.set_title("Composizione del prelievo stimato")
    ax.legend(frameon=False, ncol=3)
    fig.tight_layout()
    fig.savefig(output_dir / "tax_breakdown.png", dpi=175)
    plt.close(fig)


def build_report(
    output_dir: Path,
    metrics: pd.DataFrame,
    periods: pd.DataFrame,
    qualification: pd.DataFrame,
    comparison: pd.DataFrame,
    manifest: Mapping[str, object],
) -> None:
    taxed = metrics[metrics["scenario"] == "italian_tax"]
    metric_columns = [
        "strategy", "ending_value_eur", "cagr_pct", "annual_volatility_pct",
        "sharpe_excess_cash", "max_drawdown_pct", "average_exposure_pct", "orders",
        "fees_eur", "estimated_slippage_eur", "total_tax_eur", "ending_loss_credit_usable_eur",
    ]
    gross_net = metrics.pivot(index="strategy", columns="scenario", values="ending_value_eur").reset_index()
    gross_net["tax_and_path_drag_eur"] = gross_net["gross"] - gross_net["italian_tax"]
    period_view = periods[periods["scenario"] == "italian_tax"][
        ["period", "strategy", "cagr_pct", "sharpe_excess_cash", "max_drawdown_pct"]
    ]
    verdict_rows = comparison[comparison["scenario"] == "italian_tax"].set_index("strategy")
    best = verdict_rows["ending_value_delta_eur"].idxmax()
    best_row = verdict_rows.loc[best]
    verdict = (
        f"{best} è la variante T9 meno debole rispetto a T8 nel confronto netto: "
        f"Δ CAGR {best_row['cagr_delta_pp']:+.2f} punti, Δ Sharpe {best_row['sharpe_delta']:+.2f}, "
        f"Δ drawdown {best_row['max_drawdown_delta_pp']:+.2f} punti. "
        "La promozione dipende comunque dai criteri preregistrati, inclusi 700 ordini massimi."
    )
    css = """
body{font-family:Inter,Arial,sans-serif;margin:0;color:#25313a;background:#f4f6f7;line-height:1.5}
main{max-width:1180px;margin:24px auto;padding:30px 38px;background:#fff;box-shadow:0 4px 24px #00000012}
h1,h2{color:#244e68}.callout{background:#edf4f7;border-left:4px solid #315c7c;padding:12px 16px}
.warning{background:#fff4df;border-left-color:#b87950}table{width:100%;border-collapse:collapse;font-size:12px;margin:14px 0 24px}
th{background:#315c7c;color:#fff;padding:7px;text-align:right}td{border-bottom:1px solid #dbe1e4;padding:6px 7px;text-align:right}
th:first-child,td:first-child{text-align:left}img{max-width:100%;margin:8px 0 20px}.small{color:#66737c;font-size:12px}
"""
    body = f"""<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>T9 bear e fiscalità italiana</title>
<style>{css}</style></head><body><main><h1>T9 · esposizione bear e fiscalità italiana</h1>
<p>Equity Long V2 · selezione SMA50/200 congelata · core T8 per T9A/T9B</p>
<div class="callout"><strong>Verdetto.</strong> {html.escape(verdict)}</div>
<h2>Risultati netti</h2>{v2.format_table(taxed[metric_columns])}
<img src="equity_curves_bear_tax.png" alt="Curve nette">
<img src="drawdowns_bear_tax.png" alt="Drawdown netto">
<img src="exposure_bear_tax.png" alt="Esposizione">
<h2>Confronto T9 contro T8</h2>{v2.format_table(comparison)}
<h2>Criteri preregistrati applicati al netto</h2>{qualification.to_html(index=False, border=0)}
<h2>Lordo contro netto fiscale</h2>{v2.format_table(gross_net)}
<img src="tax_breakdown.png" alt="Scomposizione imposte">
<h2>Sottoperiodi netti</h2>{v2.format_table(period_view)}
<h2>Segnali bear</h2><ul>
<li>T9A: 25% in bear strutturale; 35% a drawdown ≥20%; 45% a drawdown ≥30%.</li>
<li>T9B: 25% in bear; 35% dopo stabilizzazione; 45% soltanto sopra SMA80; reset a 25% con shock di volatilità o nuovo minimo a 20 sedute.</li>
<li>Il floor è raggiunto con un sleeve SPY nel backtest; non aumenta il peso dei singoli titoli e non usa leva.</li>
</ul><img src="bear_floors.png" alt="Floor bear">
<div class="callout warning"><strong>Fiscalità modellata.</strong> Aliquota italiana 20% fino al 30/06/2014 e 26% dopo; minusvalenze per quattro anni con conversione 76,92% al cambio di aliquota; dividendi USA con 15% alla fonte e imposta italiana sul netto frontiera; interessi tassati; bollo 0,10%/0,15%/0,20%. Il bollo è rateizzato giornalmente e i dividendi sono inferiti dai fattori di aggiustamento: sono approssimazioni di ricerca.</div>
<p class="small">Dataset congelato al {html.escape(str(manifest['actual_data_end']))}. Universo odierno con survivorship bias. Regime amministrato ipotizzato; nessuna IVAFE e nessuna FTT perché l'universo è USA. Non è consulenza fiscale o finanziaria.</p>
</main></body></html>"""
    (output_dir / "report_bear_tax.html").write_text(body, encoding="utf-8")


def slug(value: str) -> str:
    return value.lower().replace(" ", "_")


def main() -> None:
    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    cache_dir = (base_dir / args.cache_dir).resolve()
    input_dir = (base_dir / args.input_dir).resolve()
    output_dir = (base_dir / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cfg = v2.V2Config()

    symbols = sorted(set(v1.TICKER_TO_SECTOR) | set(v1.SECTOR_UNIVERSE) | {v1.BENCHMARK, v1.FX_TICKER})
    data, failures = v1.download_universe(symbols, args.data_start, args.end, cache_dir, args.refresh, args.workers)
    stocks = [ticker for ticker in v1.TICKER_TO_SECTOR if ticker in data]
    if len(stocks) < 30:
        raise RuntimeError(f"Only {len(stocks)} stocks available")
    print("Building frozen SMA50/200 features and tax cash-flow data ...", flush=True)
    features = v1.build_features(data, v1.StrategyConfig())
    index = features.close_eur.index
    membership, membership_status = v2.load_membership_mask(input_dir / "universe_membership.csv", index, stocks)
    pit_signals, pit_signal_status = v2.load_pit_signals(input_dir / "pit_signals.csv", index, stocks)
    execution_costs, execution_cost_status = v2.load_execution_costs(input_dir / "execution_costs.csv", index, stocks)
    cash_rate, cash_rate_status = v2.fetch_ecb_cash_rate(index, cache_dir, args.refresh, cfg.cash_rate_haircut_bps)
    score, score_weights = v2.combine_rule_score(features.score, pit_signals)
    eligible = features.eligible & membership
    base_regime = v2.continuous_regime(features, data)
    t8, _ = overlays_module.t8_volatility_overlay(data, index)
    signals = bear_exposure_signals(data, features, t8)
    raw_market = reconstruct_raw_market_data(data, index, [*stocks, v1.BENCHMARK])

    strategy_inputs = {
        "V2 baseline": (None, None),
        "T8 Volatility shock": (t8, None),
        "T9A Drawdown ladder": (t8, signals["t9a_exposure_floor"]),
        "T9B Bear rebound": (t8, signals["t9b_exposure_floor"]),
    }
    tax_configs = (TaxConfig("gross", False), TaxConfig("italian_tax", True))
    results: dict[tuple[str, str], TaxAwareResult] = {}
    for tax_config in tax_configs:
        print(f"Running scenario {tax_config.name} ...", flush=True)
        for strategy, (risk_overlay, floor) in strategy_inputs.items():
            print(f"  {strategy}", flush=True)
            results[(tax_config.name, strategy)] = simulate_tax_aware(
                features, score, eligible, membership, base_regime, cash_rate,
                execution_costs, cfg, raw_market, args.test_start, args.capital,
                strategy, tax_config, weekly_risk_overlay=risk_overlay, bear_floor=floor,
            )

    cash_rates = {config.name: net_cash_rate(cash_rate, config) for config in tax_configs}
    metrics = pd.DataFrame(
        [metric_row(result, cash_rates[scenario], args.capital) for (scenario, _), result in results.items()]
    )
    periods = period_metrics(results, cash_rates, args.capital)
    qualification = qualification_table(metrics, periods)
    comparison = comparison_vs_t8(metrics)
    bootstraps = {
        scenario: {
            strategy: v2.block_bootstrap_difference(
                results[(scenario, strategy)].equity,
                results[(scenario, "T8 Volatility shock")].equity,
            )
            for strategy in ("T9A Drawdown ladder", "T9B Bear rebound")
        }
        for scenario in ("gross", "italian_tax")
    }

    metrics.to_csv(output_dir / "metrics_bear_tax.csv", index=False)
    periods.to_csv(output_dir / "period_metrics_bear_tax.csv", index=False)
    qualification.to_csv(output_dir / "qualification_bear_tax.csv", index=False)
    comparison.to_csv(output_dir / "comparison_vs_t8.csv", index=False)
    signals.to_csv(output_dir / "bear_signals.csv", index_label="Date")
    raw_market.events.to_csv(output_dir / "distribution_events_inferred.csv", index=False)
    pd.concat(
        {f"{scenario}__{strategy}": result.equity for (scenario, strategy), result in results.items()},
        axis=1,
    ).to_csv(output_dir / "equity_curves_bear_tax.csv", index_label="Date")
    for (scenario, strategy), result in results.items():
        suffix = f"{scenario}_{slug(strategy)}"
        result.orders.to_csv(output_dir / f"orders_{suffix}.csv", index=False)
        result.realized_lots.to_csv(output_dir / f"realized_lots_{suffix}.csv", index=False)
        result.allocations.to_csv(output_dir / f"allocations_{suffix}.csv", index=False)
        result.tax_ledger.to_csv(output_dir / f"tax_ledger_{suffix}.csv", index=False)
    (output_dir / "bootstrap_bear_tax.json").write_text(json.dumps(bootstraps, indent=2), encoding="utf-8")

    ordinary_events = int((raw_market.events["classification"] == "ordinary_dividend").sum())
    corporate_events = int((raw_market.events["classification"] == "corporate_action_proxy").sum())
    manifest = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "data_start": args.data_start,
        "test_start": args.test_start,
        "requested_end": args.end,
        "actual_data_end": index[-1].date().isoformat(),
        "stocks_available": len(stocks),
        "failed_symbols": failures,
        "stock_selection_windows": {"fast": 50, "slow": 200},
        "core_overlay": "T8 clip(volatility63/volatility20, 0.50, 1.00)",
        "t9a": {"structural_bear": "SPY<SMA200 and SMA50<SMA200", "floors": {"bear": 0.25, "drawdown_20": 0.35, "drawdown_30": 0.45}},
        "t9b": {
            "structural_bear": "SPY<SMA200 and SMA50<SMA200",
            "base_floor": 0.25,
            "recovery_floor": 0.35,
            "above_sma80_floor": 0.45,
            "recovery_gate": "T8>0.90 and 3 closes above SMA20 and breadth80>breadth80[-10]",
            "reset_gate": "T8<0.75 or new 20-session low",
        },
        "market_sleeve": {"backtest_proxy": "SPY", "leverage": False, "maximum_floor": 0.45},
        "tax_model": {
            "regime": "Italian administered-regime research approximation",
            "italian_rate": {"through_2014_06_30": 0.20, "from_2014_07_01": 0.26},
            "loss_carry_tax_years": 4,
            "pre_change_loss_conversion_at_26pct": 0.20 / 0.26,
            "us_dividend_withholding": 0.15,
            "italian_dividend_base": "net border amount",
            "interest_tax": "same historical 20%/26% schedule",
            "stamp_rate": {"2012": 0.0010, "2013": 0.0015, "2014_onward": 0.0020},
            "stamp_timing_approximation": "daily pro-rata on prior close invested value",
            "equity_curve_measure": "daily hypothetical liquidation value net of latent capital-gains tax and exit costs",
            "cost_basis": "weighted average in EUR including fees and execution slippage",
            "losses_do_not_offset": ["dividends", "cash interest"],
            "ivafe": False,
            "financial_transaction_tax": False,
        },
        "distribution_inference": {
            "formula": "yield=max(0,1-factor[t-1]/factor[t]); factor=AdjustedClose/RawClose",
            "ordinary_dividend_events": ordinary_events,
            "corporate_action_proxy_events": corporate_events,
            "corporate_action_threshold": CORPORATE_ACTION_YIELD_THRESHOLD,
        },
        "execution": {"fixed_fee_eur": cfg.fixed_fee_eur, "slippage_bps_per_side": cfg.default_slippage_bps_per_side},
        "qualification_rule_net_tax": {
            "minimum_sharpe": 0.90,
            "minimum_max_drawdown_pct": -18.0,
            "minimum_cagr_pct": 11.0,
            "maximum_orders": 700,
            "positive_cagr_all_subperiods": True,
        },
        "declared_trials_for_dsr": TRIAL_COUNT,
        "portfolio_configuration": vars(cfg),
        "score_weights": score_weights,
        "membership_status": membership_status,
        "pit_signal_status": pit_signal_status,
        "execution_cost_status": execution_cost_status,
        "cash_rate_status": cash_rate_status,
        "financial_datasets_plugin_status": "connected but unavailable: zero credit balance",
    }
    (output_dir / "run_manifest_bear_tax.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    create_charts(results, signals, output_dir)
    build_report(output_dir, metrics, periods, qualification, comparison, manifest)
    print("\nItalian-tax metrics:")
    print(
        metrics[metrics["scenario"] == "italian_tax"][[
            "strategy", "ending_value_eur", "cagr_pct", "sharpe_excess_cash",
            "max_drawdown_pct", "average_exposure_pct", "orders", "total_tax_eur",
        ]].to_string(index=False)
    )
    print("\nQualification:")
    print(qualification.to_string(index=False))
    print(f"\nReport: {output_dir / 'report_bear_tax.html'}")


if __name__ == "__main__":
    main()
