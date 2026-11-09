"""Original tax-aware simulator adapted ONLY to avoid terminal forced liquidation in live.
The historical backtest automatically closes positions at the end of its sample;
this is not a tradable exit in a continuing paper portfolio. Default False;
all historical execution/tax/portfolio logic is preserved verbatim.
"""
from __future__ import annotations
import math
import numpy as np
import pandas as pd
import equity_long_backtest as v1
import equity_long_backtest_v2 as v2
import equity_long_bear_tax_comparison as original
from equity_long_bear_tax_comparison import *
from equity_long_bear_tax_comparison import italian_financial_rate, historical_stamp_rate, LossLedger, RawMarketData, TaxAwareResult, TaxConfig, ExecutionPolicy
from typing import Optional,Callable

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
    liquidate_on_final_session: bool = False,
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
    # FIFO purchase lots for minimum holding period; original tax basis remains
    # weighted average by ticker. The lot dates enforce execution eligibility.
    import os
    # Identical FIFO sale restriction to the validated 2012-2025 35-day replay.
    # The runtime cannot quietly disable it with an environment variable.
    required_hold_days = int(cfg.minimum_holding_calendar_days)
    if required_hold_days != 35:
        raise ValueError('This validated live paper requires exactly 35 calendar days')
    if required_hold_days < 0 or required_hold_days > 365:raise ValueError('hold period outside valid range')
    lot_inventory: dict[str, list[dict]] = {}
    blocked_sell_count = 0
    blocked_sell_notional_eur = 0.0
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
        nonlocal blocked_sell_count, blocked_sell_notional_eur
        available = sum(lot['shares'] for lot in lot_inventory.get(ticker, [])
                        if (date-lot['date']).days >= required_hold_days)
        if abs(available-position.shares) > 1e-6 and required_hold_days == 0:
            raise ValueError('FIFO lot share reconciliation failed: '+ticker)
        if sell_shares > available + 1e-9:
            blocked_sell_count += 1
            blocked_sell_notional_eur += (sell_shares-max(0.,available))*reference_price
        sell_shares = min(sell_shares, max(0.,available))
        if sell_shares <= 1e-12: return
        sold_lot_dates=[]
        remaining = sell_shares
        for lot in lot_inventory.get(ticker, []):
            if remaining <= 1e-10:break
            if (date - lot['date']).days < required_hold_days:continue
            take=min(remaining,lot['shares'])
            if take > 0:
                sold_lot_dates.append(lot['date'])
                lot['shares'] -= take
                remaining -= take
        if remaining > 1e-6:raise ValueError('FIFO sold more than matured shares')
        lot_inventory[ticker] = [lot for lot in lot_inventory[ticker] if lot['shares']>1e-8]
        fraction = sell_shares / position.shares
        # Preserve the original Italian weighted-average-basis proxy calculation.
        basis = position.cost_basis_eur * fraction
        # Report the newest sold lot to verify the strict minimum age.
        entry_date = max(sold_lot_dates) if sold_lot_dates else position.first_entry_date
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
            lot_inventory.pop(ticker,None)
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
        lot_inventory.setdefault(ticker, []).append({'date':date, 'shares':shares})
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

    # Continuing paper does not close at the end of every nightly run.
    if liquidate_on_final_session and len(sim_index) and positions:
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
    result = TaxAwareResult(
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
    result.blocked_sell_count=blocked_sell_count
    result.blocked_sell_notional_eur=blocked_sell_notional_eur
    result.strict_hold_calendar_days=required_hold_days
    # Observability only: never alter trading rules, execution, cash or tax.
    result.paper_cash_eur=float(cash)
    result.paper_positions={ticker:{'shares':float(p.shares),
        'cost_basis_eur':float(p.cost_basis_eur),
        'first_entry_date':str(p.first_entry_date.date()),
        'lots':[{'purchase_date':str(lot['date'].date()),
                 'shares':float(lot['shares']),
                 'sale_eligible_date':str((lot['date']+pd.Timedelta(days=required_hold_days)).date())}
                for lot in lot_inventory.get(ticker,[]) if lot['shares']>1e-9]}
        for ticker,p in positions.items()}
    return result


