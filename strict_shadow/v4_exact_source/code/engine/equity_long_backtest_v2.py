#!/usr/bin/env python3
"""Risk-managed v2 of the long-only equity research engine.

The v1 engine remains unchanged and is rerun as a frozen benchmark.  V2 adds:

* 10-stock inverse-volatility allocation with stock and sector caps;
* continuous market/breadth regime and a 14% volatility target, without leverage;
* rank/score hysteresis plus explicit no-trade bands;
* EUR cash remuneration from the official ECB deposit-facility series;
* optional point-in-time membership, fundamental/earnings signals and costs;
* one pre-declared walk-forward tree-model challenger with an embargo;
* cost scenarios, parameter-plateau diagnostics, block bootstrap, DSR and PBO.

The default data still contains survivorship bias unless the optional point-in-
time inputs are supplied.  This is research software, not investment advice.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import html
import itertools
import json
import math
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import kurtosis, norm, skew
from sklearn.covariance import LedoitWolf
from sklearn.ensemble import HistGradientBoostingRegressor

import equity_long_backtest as v1


ECB_DFR_URL = (
    "https://data-api.ecb.europa.eu/service/data/"
    "FM/D.U2.EUR.4F.KR.DFR.LEV?startPeriod=2007-01-01&format=csvdata"
)


@dataclasses.dataclass(frozen=True)
class V2Config:
    max_positions: int = 10
    exit_rank: int = 18
    max_names_per_sector: int = 3
    maximum_position_weight: float = 0.15
    maximum_sector_weight: float = 0.30
    target_volatility: float = 0.14
    covariance_lookback: int = 126
    minimum_holding_calendar_days: int = 21
    replacement_score_improvement: float = 0.30
    rebalance_band_weight: float = 0.05
    minimum_trade_eur: float = 500.0
    decision_interval_weeks: int = 5
    weight_rebalance_weeks: int = 1
    fixed_fee_eur: float = 1.0
    default_slippage_bps_per_side: float = 10.0
    cash_rate_haircut_bps: float = 50.0
    ml_rule_weight: float = 0.60
    ml_prediction_weight: float = 0.40
    ml_horizon_sessions: int = 63
    ml_embargo_sessions: int = 5
    ml_training_years: int = 8


@dataclasses.dataclass
class Position:
    shares: float
    cost_basis_eur: float
    first_entry_date: pd.Timestamp


@dataclasses.dataclass
class V2Result:
    name: str
    equity: pd.Series
    exposure: pd.Series
    orders: pd.DataFrame
    realized_lots: pd.DataFrame
    allocations: pd.DataFrame
    total_fees_eur: float
    estimated_slippage_eur: float
    cash_interest_eur: float
    total_traded_eur: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-start", default="2007-01-01")
    parser.add_argument("--test-start", default="2012-01-03")
    parser.add_argument("--end", default=(dt.date.today() + dt.timedelta(days=1)).isoformat())
    parser.add_argument("--capital", type=float, default=10_000.0)
    parser.add_argument("--cache-dir", default="data_cache")
    parser.add_argument("--input-dir", default="inputs")
    parser.add_argument("--output-dir", default="output_v2")
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--skip-ml", action="store_true")
    parser.add_argument("--skip-robustness", action="store_true")
    return parser.parse_args()


def fetch_ecb_cash_rate(
    index: pd.DatetimeIndex,
    cache_dir: Path,
    refresh: bool,
    haircut_bps: float,
) -> tuple[pd.Series, str]:
    """Return a conservative EUR cash yield derived from the ECB DFR.

    The investor rate is max(DFR - haircut, 0).  It is an explicit research
    assumption, not a claim about the broker's historical remuneration.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "ecb_deposit_facility_rate.csv"
    status = "ECB DFR, investor proxy=max(DFR-haircut, 0)"
    try:
        if refresh or not cache_path.exists():
            request = urllib.request.Request(
                ECB_DFR_URL,
                headers={"User-Agent": "EquityLongResearch/2.0", "Accept": "text/csv"},
            )
            with urllib.request.urlopen(request, timeout=45) as response:
                cache_path.write_bytes(response.read())
        raw = pd.read_csv(cache_path, usecols=["TIME_PERIOD", "OBS_VALUE"])
        raw["TIME_PERIOD"] = pd.to_datetime(raw["TIME_PERIOD"])
        raw = raw.drop_duplicates("TIME_PERIOD", keep="last").set_index("TIME_PERIOD")
        annual_rate = (raw["OBS_VALUE"].astype(float) / 100.0 - haircut_bps / 10_000.0).clip(lower=0.0)
        result = annual_rate.reindex(index).ffill().fillna(0.0)
        result.name = "cash_rate"
        return result, status
    except Exception as exc:
        result = pd.Series(0.0, index=index, name="cash_rate")
        return result, f"cash yield disabled after ECB download failure: {exc}"


def load_membership_mask(
    path: Path,
    index: pd.DatetimeIndex,
    tickers: Sequence[str],
) -> tuple[pd.DataFrame, str]:
    """Load effective-dated tradability/membership without backfilling history."""
    if not path.exists():
        return pd.DataFrame(True, index=index, columns=tickers), "missing: current universe used"
    frame = pd.read_csv(path)
    required = {"ticker", "effective_from", "effective_to", "tradable"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{path} is missing columns: {sorted(missing)}")
    frame["effective_from"] = pd.to_datetime(frame["effective_from"])
    frame["effective_to"] = pd.to_datetime(frame["effective_to"], errors="coerce")
    mask = pd.DataFrame(False, index=index, columns=tickers)
    for row in frame.itertuples(index=False):
        ticker = str(row.ticker).upper()
        if ticker not in mask.columns or not bool(row.tradable):
            continue
        end = row.effective_to if pd.notna(row.effective_to) else index[-1]
        mask.loc[(mask.index >= row.effective_from) & (mask.index <= end), ticker] = True
    return mask, f"loaded {len(frame)} effective-dated membership records"


def load_pit_signals(
    path: Path,
    index: pd.DatetimeIndex,
    tickers: Sequence[str],
) -> tuple[dict[str, pd.DataFrame], str]:
    """Load already-normalized signals keyed by their first observable date."""
    if not path.exists():
        return {}, "missing: fundamental and earnings signals disabled"
    frame = pd.read_csv(path)
    required = {"effective_date", "ticker"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{path} is missing columns: {sorted(missing)}")
    signal_columns = [c for c in ("quality_score", "earnings_score") if c in frame.columns]
    if not signal_columns:
        raise ValueError(f"{path} needs quality_score and/or earnings_score")
    frame["effective_date"] = pd.to_datetime(frame["effective_date"])
    panels: dict[str, pd.DataFrame] = {}
    for signal in signal_columns:
        panel = pd.DataFrame(index=index, columns=tickers, dtype="float64")
        for ticker, rows in frame.groupby(frame["ticker"].astype(str).str.upper()):
            if ticker not in panel.columns:
                continue
            series = rows.set_index("effective_date")[signal].sort_index()
            panel.loc[panel.index.intersection(series.index), ticker] = series.reindex(
                panel.index.intersection(series.index)
            )
        # Quality can remain valid for about a year; earnings information for
        # roughly two quarters.  No backfill is allowed.
        limit = 300 if signal == "quality_score" else 126
        panels[signal] = panel.ffill(limit=limit)
    return panels, f"loaded {len(frame)} point-in-time signal records"


def load_execution_costs(
    path: Path,
    index: pd.DatetimeIndex,
    tickers: Sequence[str],
) -> tuple[Optional[pd.DataFrame], str]:
    if not path.exists():
        return None, "missing: static slippage assumption used"
    frame = pd.read_csv(path)
    required = {"date", "ticker", "slippage_bps_per_side"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{path} is missing columns: {sorted(missing)}")
    frame["date"] = pd.to_datetime(frame["date"])
    panel = frame.pivot_table(
        index="date", columns="ticker", values="slippage_bps_per_side", aggfunc="last"
    )
    panel.columns = panel.columns.astype(str).str.upper()
    panel = panel.reindex(index=index, columns=tickers).ffill(limit=21)
    return panel, f"loaded {len(frame)} effective-dated cost records"


def combine_rule_score(
    base_score: pd.DataFrame,
    pit_signals: Mapping[str, pd.DataFrame],
) -> tuple[pd.DataFrame, dict[str, float]]:
    if not pit_signals:
        return base_score.copy(), {"price_model": 1.0}
    components: list[tuple[str, float, pd.DataFrame]] = [("price_model", 0.70, base_score)]
    if "quality_score" in pit_signals:
        components.append(("quality", 0.15, v1.robust_cross_sectional_z(pit_signals["quality_score"])))
    if "earnings_score" in pit_signals:
        components.append(("earnings", 0.15, v1.robust_cross_sectional_z(pit_signals["earnings_score"])))
    total_weight = sum(weight for _, weight, _ in components)
    result = sum((weight / total_weight) * panel for _, weight, panel in components)
    weights = {name: weight / total_weight for name, weight, _ in components}
    return result, weights


def continuous_regime(
    features: v1.FeatureBundle,
    data: Mapping[str, pd.DataFrame],
    fast_window: int = 50,
    slow_window: int = 200,
) -> pd.Series:
    if fast_window >= slow_window:
        raise ValueError("fast_window must be smaller than slow_window")
    index = features.close_usd.index
    spy = data[v1.BENCHMARK]["Close"].reindex(index)
    spy_ret = np.log(spy).diff()
    annual_vol = spy_ret.rolling(63, min_periods=42).std() * math.sqrt(252.0)
    fast_min_periods = max(2, math.ceil(0.80 * fast_window))
    slow_min_periods = max(2, math.ceil(0.80 * slow_window))
    sma_fast = spy.rolling(fast_window, min_periods=fast_min_periods).mean()
    sma_slow = spy.rolling(slow_window, min_periods=slow_min_periods).mean()
    distance = np.log(spy.div(sma_slow)).div(annual_vol.replace(0.0, np.nan))
    trend_score = (0.65 + 1.50 * distance).clip(0.0, 1.0)
    slope_scaled = sma_fast.div(sma_slow).sub(1.0)
    slope_score = (0.50 + 10.0 * slope_scaled).clip(0.0, 1.0)
    stock_sma_slow = features.close_usd.rolling(
        slow_window, min_periods=slow_min_periods
    ).mean()
    breadth = features.close_usd.gt(stock_sma_slow).mean(axis=1)
    breadth_score = ((breadth - 0.35) / 0.30).clip(0.0, 1.0)
    regime = (0.50 * trend_score + 0.25 * slope_score + 0.25 * breadth_score).fillna(0.0)
    severe_bear = (spy < sma_slow) & (sma_fast < sma_slow)
    regime.loc[severe_bear] = regime.loc[severe_bear].clip(upper=0.25)
    return regime.clip(0.0, 1.0).rename("regime_multiplier")


def prediction_dates(index: pd.DatetimeIndex, test_start: str) -> list[pd.Timestamp]:
    mapping = v1._weekly_execution_map(index, pd.Timestamp(test_start))
    return sorted(set(mapping.values()))


def build_walk_forward_ml_score(
    features: v1.FeatureBundle,
    rule_score: pd.DataFrame,
    membership: pd.DataFrame,
    test_start: str,
    cfg: V2Config,
    pit_signals: Mapping[str, pd.DataFrame],
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    """Build annual-refit, embargoed predictions for 63-session excess returns."""
    index = features.close_eur.index
    tickers = list(features.close_eur.columns)
    dates = prediction_dates(index, test_start)
    feature_panels: dict[str, pd.DataFrame] = {
        "residual_12_1": v1.robust_cross_sectional_z(features.feature_tables["residual_12_1"]),
        "residual_6_1": v1.robust_cross_sectional_z(features.feature_tables["residual_6_1"]),
        "raw_12_1": v1.robust_cross_sectional_z(features.feature_tables["raw_12_1"]),
        "raw_6_1": v1.robust_cross_sectional_z(features.feature_tables["raw_6_1"]),
        "trend_strength": v1.robust_cross_sectional_z(features.feature_tables["trend_strength"]),
        "annual_volatility": v1.robust_cross_sectional_z(features.feature_tables["annual_vol_63"]),
        "extension_5": features.feature_tables["extension_5"].clip(-4.0, 4.0),
        "log_liquidity": v1.robust_cross_sectional_z(
            np.log(features.feature_tables["median_dollar_volume"].where(lambda x: x > 0.0))
        ),
    }
    for name, panel in pit_signals.items():
        feature_panels[name] = v1.robust_cross_sectional_z(panel)

    horizon = cfg.ml_horizon_sessions
    log_stock = np.log(features.close_eur.where(features.close_eur > 0.0))
    benchmark = log_stock.mean(axis=1)  # cross-sectional investable-universe benchmark
    future_stock = log_stock.shift(-horizon) - log_stock
    future_benchmark = benchmark.shift(-horizon) - benchmark
    target = future_stock.sub(future_benchmark, axis=0)

    prediction = pd.DataFrame(index=index, columns=tickers, dtype="float64")
    metadata: list[dict] = []
    index_positions = {date: pos for pos, date in enumerate(index)}
    date_set = set(dates)

    for year in sorted({date.year for date in dates}):
        year_dates = [date for date in dates if date.year == year]
        if not year_dates:
            continue
        first_position = index_positions[year_dates[0]]
        cutoff_position = first_position - horizon - cfg.ml_embargo_sessions
        if cutoff_position <= 0:
            continue
        cutoff_date = index[cutoff_position]
        earliest_date = cutoff_date - pd.DateOffset(years=cfg.ml_training_years)
        train_dates = [
            date for date in date_set if earliest_date <= date <= cutoff_date
        ]
        if not train_dates:
            continue

        blocks = []
        for name, panel in feature_panels.items():
            blocks.append(panel.reindex(train_dates).stack(future_stack=True).rename(name))
        train = pd.concat(blocks, axis=1)
        train["target"] = target.reindex(train_dates).stack(future_stack=True)
        train["member"] = membership.reindex(train_dates).stack(future_stack=True)
        train = train[train["member"].fillna(False)].drop(columns="member").replace(
            [np.inf, -np.inf], np.nan
        ).dropna()
        if len(train) < 750:
            metadata.append({"year": year, "status": "insufficient_samples", "samples": len(train)})
            continue

        model = HistGradientBoostingRegressor(
            loss="squared_error",
            learning_rate=0.05,
            max_iter=120,
            max_leaf_nodes=15,
            min_samples_leaf=80,
            l2_regularization=2.0,
            random_state=42,
        )
        x_train = train[list(feature_panels)]
        model.fit(x_train, train["target"])
        predicted_count = 0
        for date in year_dates:
            x_date = pd.DataFrame(
                {name: panel.loc[date] for name, panel in feature_panels.items()}
            ).replace([np.inf, -np.inf], np.nan)
            valid = x_date.notna().all(axis=1) & membership.loc[date].fillna(False)
            if valid.any():
                prediction.loc[date, valid] = model.predict(x_date.loc[valid])
                predicted_count += int(valid.sum())
        metadata.append(
            {
                "year": year,
                "status": "fit",
                "training_cutoff": cutoff_date.date().isoformat(),
                "samples": int(len(train)),
                "features": len(feature_panels),
                "predictions": predicted_count,
            }
        )

    prediction_z = v1.robust_cross_sectional_z(prediction)
    normalized_rule = v1.robust_cross_sectional_z(rule_score)
    blend = normalized_rule.copy()
    available = prediction_z.notna()
    blend = blend.where(~available, cfg.ml_rule_weight * normalized_rule + cfg.ml_prediction_weight * prediction_z)
    return blend, prediction, metadata


def estimate_risky_volatility(
    date: pd.Timestamp,
    tickers: Sequence[str],
    close_eur: pd.DataFrame,
    lookback: int,
) -> tuple[float, pd.Series, np.ndarray]:
    vol = close_eur.pct_change(fill_method=None).rolling(63, min_periods=42).std().loc[date]
    safe_vol = vol.reindex(tickers).replace(0.0, np.nan).fillna(vol.median()).clip(lower=0.005)
    raw = (1.0 / safe_vol).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    if raw.sum() <= 0.0:
        raw = pd.Series(1.0, index=tickers)
    relative = raw / raw.sum()
    location = close_eur.index.get_loc(date)
    start = max(0, location - lookback + 1)
    returns = close_eur.iloc[start : location + 1][list(tickers)].pct_change(fill_method=None).dropna()
    if len(returns) >= 42:
        covariance = LedoitWolf().fit(returns.to_numpy()).covariance_ * 252.0
    else:
        diagonal = np.square(safe_vol.to_numpy() * math.sqrt(252.0))
        covariance = np.diag(diagonal)
    risky_vol = float(math.sqrt(max(0.0, relative.to_numpy() @ covariance @ relative.to_numpy())))
    return risky_vol, relative, covariance


def constrained_target_weights(
    relative: pd.Series,
    exposure: float,
    cfg: V2Config,
    decision_date: Optional[pd.Timestamp] = None,
) -> pd.Series:
    """Build constrained weights for one decision date.

    ``decision_date`` is deliberately explicit even though the canonical
    allocator does not use it.  Strategy-specific allocators can therefore
    consume the current risk state without relying on mutable state left by a
    prior full-selection call.  This closes the stale-date failure mode found
    in Atlas Strict v1.0 risk-only rebalances.
    """
    del decision_date
    tickers = relative.index.tolist()
    if not tickers or exposure <= 0.0:
        return pd.Series(dtype="float64")
    sectors = sorted({v1.TICKER_TO_SECTOR[ticker] for ticker in tickers})
    feasible_total = min(
        exposure,
        len(tickers) * cfg.maximum_position_weight,
        len(sectors) * cfg.maximum_sector_weight,
    )
    desired = relative.to_numpy() * feasible_total
    constraints = [{"type": "eq", "fun": lambda w: float(w.sum() - feasible_total)}]
    for sector in sectors:
        locations = np.array(
            [i for i, ticker in enumerate(tickers) if v1.TICKER_TO_SECTOR[ticker] == sector]
        )
        constraints.append(
            {
                "type": "ineq",
                "fun": lambda w, loc=locations: float(cfg.maximum_sector_weight - w[loc].sum()),
            }
        )
    result = minimize(
        lambda w: float(np.square(w - desired).sum()),
        x0=np.full(len(tickers), feasible_total / len(tickers)),
        method="SLSQP",
        bounds=[(0.0, cfg.maximum_position_weight)] * len(tickers),
        constraints=constraints,
        options={"maxiter": 250, "ftol": 1e-12},
    )
    if not result.success:
        # Equal weights remain feasible because name selection limits sector count.
        fallback = np.full(len(tickers), feasible_total / len(tickers))
        return pd.Series(fallback, index=tickers)
    return pd.Series(result.x, index=tickers).clip(lower=0.0)


def choose_names(
    date: pd.Timestamp,
    score_row: pd.Series,
    rank_row: pd.Series,
    eligible_row: pd.Series,
    positions: Mapping[str, Position],
    cfg: V2Config,
) -> list[str]:
    ranked = score_row.where(eligible_row).dropna().sort_values(ascending=False).index.tolist()
    kept = []
    sector_counts: dict[str, int] = {}
    for ticker in positions:
        position = positions[ticker]
        old_enough = (date - position.first_entry_date).days >= cfg.minimum_holding_calendar_days
        valid = bool(eligible_row.get(ticker, False)) and pd.notna(rank_row.get(ticker))
        within_buffer = valid and float(rank_row[ticker]) <= cfg.exit_rank
        if within_buffer or not old_enough:
            sector = v1.TICKER_TO_SECTOR[ticker]
            if sector_counts.get(sector, 0) < cfg.max_names_per_sector:
                kept.append(ticker)
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
    kept = kept[: cfg.max_positions]
    for ticker in ranked:
        if len(kept) >= cfg.max_positions:
            break
        if ticker in kept:
            continue
        sector = v1.TICKER_TO_SECTOR[ticker]
        if sector_counts.get(sector, 0) >= cfg.max_names_per_sector:
            continue
        kept.append(ticker)
        sector_counts[sector] = sector_counts.get(sector, 0) + 1

    # A high-ranked newcomer replaces the weakest buffered holding only when
    # its score advantage is material and the holding has aged sufficiently.
    if len(kept) == cfg.max_positions:
        top_new = next((ticker for ticker in ranked[: cfg.max_positions] if ticker not in kept), None)
        replaceable = [
            ticker
            for ticker in kept
            if ticker in positions
            and (date - positions[ticker].first_entry_date).days >= cfg.minimum_holding_calendar_days
        ]
        if top_new and replaceable:
            weakest = min(replaceable, key=lambda ticker: float(score_row.get(ticker, -np.inf)))
            improvement = float(score_row[top_new] - score_row[weakest])
            new_sector = v1.TICKER_TO_SECTOR[top_new]
            weak_sector = v1.TICKER_TO_SECTOR[weakest]
            allowed = new_sector == weak_sector or sector_counts.get(new_sector, 0) < cfg.max_names_per_sector
            if improvement >= cfg.replacement_score_improvement and allowed:
                kept[kept.index(weakest)] = top_new
    return kept


def simulate_v2(
    features: v1.FeatureBundle,
    score: pd.DataFrame,
    eligible: pd.DataFrame,
    membership: pd.DataFrame,
    regime: pd.Series,
    cash_rate: pd.Series,
    execution_costs: Optional[pd.DataFrame],
    cfg: V2Config,
    test_start: str,
    initial_capital: float,
    name: str,
    weekly_risk_overlay: Optional[pd.Series] = None,
) -> V2Result:
    """Simulate the long-only portfolio.

    When ``weekly_risk_overlay`` is supplied, it multiplies the ordinary market
    regime.  Between full five-week decisions it may only reduce existing
    positions; it cannot rotate names or buy risk back.  This preserves the
    strategy's slow selection horizon while allowing a faster defensive layer.
    """
    index = features.close_eur.index
    sim_index = index[index >= pd.Timestamp(test_start)]
    execution_map = v1._weekly_execution_map(index, pd.Timestamp(test_start))
    close_values = features.close_eur.ffill(limit=5)
    open_values = features.open_eur
    rank = score.where(eligible & membership).rank(axis=1, ascending=False, method="min")
    positions: dict[str, Position] = {}
    cash = float(initial_capital)
    previous_date: Optional[pd.Timestamp] = None
    equity_rows: list[float] = []
    exposure_rows: list[float] = []
    order_rows: list[dict] = []
    realized_rows: list[dict] = []
    allocation_rows: list[dict] = []
    total_fees = 0.0
    total_slippage = 0.0
    total_interest = 0.0
    total_traded = 0.0
    decision_count = 0
    overlay = (
        weekly_risk_overlay.reindex(index).ffill().fillna(1.0).clip(0.0, 1.0)
        if weekly_risk_overlay is not None
        else pd.Series(1.0, index=index)
    )

    def slip_bps(date: pd.Timestamp, ticker: str) -> float:
        if execution_costs is not None:
            value = execution_costs.at[date, ticker]
            if pd.notna(value):
                return max(0.0, float(value))
        return cfg.default_slippage_bps_per_side

    def nav_at(date: pd.Timestamp, prices: pd.DataFrame) -> float:
        return cash + sum(
            position.shares * float(prices.at[date, ticker])
            for ticker, position in positions.items()
            if pd.notna(prices.at[date, ticker])
        )

    def sell(
        date: pd.Timestamp,
        decision_date: pd.Timestamp,
        ticker: str,
        reference_price: float,
        requested_value: float,
        reason: str,
    ) -> None:
        nonlocal cash, total_fees, total_slippage, total_traded
        position = positions[ticker]
        maximum_value = position.shares * reference_price
        reference_value = min(maximum_value, max(0.0, requested_value))
        sell_shares = min(position.shares, reference_value / reference_price)
        if sell_shares <= 1e-12:
            return
        fraction = sell_shares / position.shares
        basis = position.cost_basis_eur * fraction
        bps = slip_bps(date, ticker)
        execution_price = reference_price * (1.0 - bps / 10_000.0)
        gross = sell_shares * execution_price
        fee = cfg.fixed_fee_eur
        proceeds = max(0.0, gross - fee)
        cash += proceeds
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
                "strategy": name,
                "ticker": ticker,
                "entry_date": position.first_entry_date,
                "exit_date": date,
                "cost_basis_eur": basis,
                "proceeds_eur": proceeds,
                "pnl_eur": proceeds - basis,
                "return_pct": 100.0 * (proceeds / basis - 1.0) if basis > 0.0 else np.nan,
                "calendar_days": (date - position.first_entry_date).days,
                "full_exit": is_full,
                "reason": reason,
            }
        )
        order_rows.append(
            {
                "strategy": name,
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
                "reason": reason,
            }
        )

    def buy(
        date: pd.Timestamp,
        decision_date: pd.Timestamp,
        ticker: str,
        reference_price: float,
        requested_value: float,
    ) -> None:
        nonlocal cash, total_fees, total_slippage, total_traded
        fee = cfg.fixed_fee_eur
        budget = min(requested_value + fee, cash)
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
            positions[ticker] = Position(shares, deployed, date)
        order_rows.append(
            {
                "strategy": name,
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
                "reason": "target_rebalance",
            }
        )

    for date in sim_index:
        if previous_date is not None:
            day_count = max(0, (date - previous_date).days)
            rate = float(cash_rate.reindex(index).ffill().at[previous_date])
            interest = cash * (math.exp(rate * day_count / 365.25) - 1.0)
            cash += interest
            total_interest += interest
        previous_date = date

        decision_date = execution_map.get(date)
        risk_only_decision = False
        if decision_date is not None:
            decision_count += 1
            preview_nav = nav_at(date, open_values)
            preview_exposure = (preview_nav - cash) / preview_nav if preview_nav > 0.0 else 0.0
            preview_overlay = float(overlay.loc[decision_date])
            preview_regime = float(regime.loc[decision_date]) * preview_overlay
            scheduled_decision = (
                decision_count == 1
                or decision_count % max(1, cfg.decision_interval_weeks) == 0
            )
            emergency_decision = preview_exposure > preview_regime + 0.25
            overlay_check = weekly_risk_overlay is not None and preview_overlay < 1.0
            if not scheduled_decision:
                if weekly_risk_overlay is not None and positions and (overlay_check or emergency_decision):
                    risk_only_decision = True
                elif not emergency_decision:
                    decision_date = None

        if decision_date is not None:
            eligible_row = (eligible.loc[decision_date] & membership.loc[decision_date]).fillna(False)
            score_row = score.loc[decision_date]
            rank_row = rank.loc[decision_date]
            if risk_only_decision:
                names = list(positions)
            else:
                names = choose_names(
                    decision_date, score_row, rank_row, eligible_row, positions, cfg
                )
            overlay_multiplier = float(overlay.loc[decision_date])
            regime_multiplier = float(regime.loc[decision_date]) * overlay_multiplier
            if names and regime_multiplier > 0.0:
                risky_vol, relative, covariance = estimate_risky_volatility(
                    decision_date, names, features.close_eur, cfg.covariance_lookback
                )
                vol_scale = min(1.0, cfg.target_volatility / risky_vol) if risky_vol > 0.0 else 0.0
                desired_exposure = regime_multiplier * vol_scale
                targets = constrained_target_weights(relative, desired_exposure, cfg)
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

            for ticker, weight in targets.items():
                allocation_rows.append(
                    {
                        "decision_date": decision_date,
                        "execution_date": date,
                        "ticker": ticker,
                        "sector_etf": v1.TICKER_TO_SECTOR[ticker],
                        "score": score_row.get(ticker),
                        "rank": rank_row.get(ticker),
                        "target_weight": weight,
                        "regime_multiplier": regime_multiplier,
                        "risk_overlay_multiplier": overlay_multiplier,
                        "decision_type": "risk_only" if risk_only_decision else "full",
                        "desired_exposure": desired_exposure,
                        "estimated_target_volatility": realized_target_vol,
                        "unscaled_risky_volatility": risky_vol,
                    }
                )

            nav_open = nav_at(date, open_values)
            if risk_only_decision:
                # A smaller band is used only for defensive reductions.  It is
                # still large enough to avoid tiny per-name orders.
                trade_threshold = max(250.0, 0.025 * nav_open)
            else:
                trade_threshold = max(cfg.minimum_trade_eur, cfg.rebalance_band_weight * nav_open)
            target_values = {ticker: nav_open * float(weight) for ticker, weight in targets.items()}
            current_exposure_open = (nav_open - cash) / nav_open if nav_open > 0.0 else 0.0
            scheduled_weight_rebalance = (
                decision_count == 1 or decision_count % max(1, cfg.weight_rebalance_weeks) == 0
            )
            emergency_deleveraging = current_exposure_open > desired_exposure + 0.20
            allow_partial_rebalance = (
                risk_only_decision or scheduled_weight_rebalance or emergency_deleveraging
            )

            # Reduce risk first so buy orders never depend on hypothetical sale proceeds.
            for ticker in list(positions):
                price = open_values.at[date, ticker]
                if pd.isna(price):
                    continue
                current_value = positions[ticker].shares * float(price)
                target_value = target_values.get(ticker, 0.0)
                reduction = current_value - target_value
                full_exit = ticker not in target_values
                if full_exit or (allow_partial_rebalance and reduction >= trade_threshold):
                    sell(
                        date,
                        decision_date,
                        ticker,
                        float(price),
                        current_value if full_exit else reduction,
                        (
                            "overlay_risk_exit"
                            if risk_only_decision and full_exit
                            else "target_exit"
                            if full_exit
                            else "overlay_risk_reduction"
                            if risk_only_decision
                            else "risk_rebalance"
                        ),
                    )

            if not risk_only_decision:
                nav_after_sales = nav_at(date, open_values)
                for ticker, weight in targets.sort_values(ascending=False).items():
                    if ticker not in positions and len(positions) >= cfg.max_positions:
                        continue
                    price = open_values.at[date, ticker]
                    if pd.isna(price):
                        continue
                    current_value = positions[ticker].shares * float(price) if ticker in positions else 0.0
                    desired_value = nav_after_sales * float(weight)
                    addition = desired_value - current_value
                    is_new_position = ticker not in positions
                    if addition >= trade_threshold and (is_new_position or allow_partial_rebalance):
                        buy(date, decision_date, ticker, float(price), addition)

        close_nav = nav_at(date, close_values)
        invested = close_nav - cash
        equity_rows.append(close_nav)
        exposure_rows.append(invested / close_nav if close_nav > 0.0 else 0.0)

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

    equity = pd.Series(equity_rows, index=sim_index, name=name)
    exposure = pd.Series(exposure_rows, index=sim_index, name=name)
    return V2Result(
        name=name,
        equity=equity,
        exposure=exposure,
        orders=pd.DataFrame(order_rows),
        realized_lots=pd.DataFrame(realized_rows),
        allocations=pd.DataFrame(allocation_rows),
        total_fees_eur=total_fees,
        estimated_slippage_eur=total_slippage,
        cash_interest_eur=total_interest,
        total_traded_eur=total_traded,
    )


def excess_daily_returns(curve: pd.Series, cash_rate: pd.Series) -> pd.Series:
    returns = curve.pct_change(fill_method=None).dropna()
    daily_cash = cash_rate.reindex(returns.index).ffill().fillna(0.0) / 252.0
    return returns - daily_cash


def metrics(
    curve: pd.Series,
    cash_rate: pd.Series,
    initial_capital: float,
    result: Optional[V2Result] = None,
    declared_trials: int = 1,
) -> dict:
    curve = curve.dropna()
    years = max((curve.index[-1] - curve.index[0]).days / 365.25, 1 / 365.25)
    total_return = curve.iloc[-1] / initial_capital - 1.0
    cagr = (curve.iloc[-1] / initial_capital) ** (1.0 / years) - 1.0
    daily = curve.pct_change(fill_method=None).dropna()
    excess = excess_daily_returns(curve, cash_rate)
    annual_vol = daily.std(ddof=1) * math.sqrt(252.0)
    sharpe0 = daily.mean() / daily.std(ddof=1) * math.sqrt(252.0) if daily.std(ddof=1) else np.nan
    sharpe_excess = (
        excess.mean() / excess.std(ddof=1) * math.sqrt(252.0) if excess.std(ddof=1) else np.nan
    )
    drawdown = curve / curve.cummax() - 1.0
    max_drawdown = float(drawdown.min())
    daily_sr = float(excess.mean() / excess.std(ddof=1)) if excess.std(ddof=1) else np.nan
    observations = len(excess)
    sample_skew = float(skew(excess, bias=False)) if observations > 3 else 0.0
    sample_kurt = float(kurtosis(excess, fisher=False, bias=False)) if observations > 4 else 3.0
    denominator = math.sqrt(
        max(1e-12, 1.0 - sample_skew * daily_sr + ((sample_kurt - 1.0) / 4.0) * daily_sr**2)
    )
    probabilistic_sharpe = float(norm.cdf(daily_sr * math.sqrt(max(1, observations - 1)) / denominator))
    if declared_trials > 1 and observations > 1:
        euler_gamma = 0.5772156649
        sr_std = 1.0 / math.sqrt(observations - 1)
        expected_max_sr = sr_std * (
            (1.0 - euler_gamma) * norm.ppf(1.0 - 1.0 / declared_trials)
            + euler_gamma * norm.ppf(1.0 - 1.0 / (declared_trials * math.e))
        )
        dsr_probability = float(
            norm.cdf((daily_sr - expected_max_sr) * math.sqrt(observations - 1) / denominator)
        )
    else:
        dsr_probability = probabilistic_sharpe
    row = {
        "start": curve.index[0].date().isoformat(),
        "end": curve.index[-1].date().isoformat(),
        "ending_value_eur": float(curve.iloc[-1]),
        "total_return_pct": 100.0 * total_return,
        "cagr_pct": 100.0 * cagr,
        "annual_volatility_pct": 100.0 * annual_vol,
        "sharpe_rf0": sharpe0,
        "sharpe_excess_cash": sharpe_excess,
        "max_drawdown_pct": 100.0 * max_drawdown,
        "calmar": cagr / abs(max_drawdown) if max_drawdown < 0.0 else np.nan,
        "probabilistic_sharpe_probability": probabilistic_sharpe,
        "deflated_sharpe_probability": dsr_probability,
    }
    if result is not None:
        row.update(
            {
                "average_exposure_pct": 100.0 * result.exposure.reindex(curve.index).mean(),
                "orders": len(result.orders),
                "fees_eur": result.total_fees_eur,
                "estimated_slippage_eur": result.estimated_slippage_eur,
                "cash_interest_eur": result.cash_interest_eur,
                "annual_one_way_turnover": result.total_traded_eur
                / max(1e-9, 2.0 * curve.mean() * years),
            }
        )
    return row


def block_bootstrap_difference(
    strategy: pd.Series,
    benchmark: pd.Series,
    block_length: int = 21,
    simulations: int = 2_000,
    seed: int = 42,
) -> dict:
    aligned = pd.concat(
        [strategy.pct_change(fill_method=None), benchmark.pct_change(fill_method=None)], axis=1
    ).dropna()
    values = aligned.to_numpy()
    n = len(values)
    rng = np.random.default_rng(seed)
    differences = np.empty(simulations)
    for simulation in range(simulations):
        rows = []
        while sum(len(chunk) for chunk in rows) < n:
            start = int(rng.integers(0, max(1, n - block_length + 1)))
            rows.append(values[start : start + block_length])
        sampled = np.vstack(rows)[:n]
        annual_strategy = np.prod(1.0 + sampled[:, 0]) ** (252.0 / n) - 1.0
        annual_benchmark = np.prod(1.0 + sampled[:, 1]) ** (252.0 / n) - 1.0
        differences[simulation] = annual_strategy - annual_benchmark
    return {
        "block_length_sessions": block_length,
        "simulations": simulations,
        "annualized_difference_median_pct": 100.0 * float(np.median(differences)),
        "annualized_difference_ci_low_pct": 100.0 * float(np.quantile(differences, 0.025)),
        "annualized_difference_ci_high_pct": 100.0 * float(np.quantile(differences, 0.975)),
        "probability_strategy_beats_benchmark": float((differences > 0.0).mean()),
    }


def probability_backtest_overfit(curves: Mapping[str, pd.Series], blocks: int = 8) -> dict:
    monthly = pd.concat(
        {name: curve.resample("ME").last().pct_change(fill_method=None) for name, curve in curves.items()},
        axis=1,
    ).dropna()
    if len(monthly) < blocks * 2 or len(curves) < 3:
        return {"pbo": np.nan, "splits": 0, "blocks": blocks}
    block_indices = [chunk for chunk in np.array_split(np.arange(len(monthly)), blocks) if len(chunk)]
    below_median = []
    for chosen in itertools.combinations(range(blocks), blocks // 2):
        in_idx = np.concatenate([block_indices[i] for i in chosen])
        out_idx = np.concatenate([block_indices[i] for i in range(blocks) if i not in chosen])
        in_returns = monthly.iloc[in_idx]
        out_returns = monthly.iloc[out_idx]
        in_sharpe = in_returns.mean().div(in_returns.std(ddof=1)).replace([np.inf, -np.inf], np.nan)
        out_sharpe = out_returns.mean().div(out_returns.std(ddof=1)).replace([np.inf, -np.inf], np.nan)
        if in_sharpe.dropna().empty:
            continue
        winner = in_sharpe.idxmax()
        below_median.append(bool(out_sharpe[winner] < out_sharpe.median()))
    return {
        "pbo": float(np.mean(below_median)) if below_median else np.nan,
        "splits": len(below_median),
        "blocks": blocks,
        "variants": len(curves),
        "interpretation": "fraction of CSCV splits where the in-sample winner is below the OOS median",
    }


def period_table(
    curves: Mapping[str, pd.Series],
    cash_rate: pd.Series,
    initial_capital: float,
) -> pd.DataFrame:
    periods = [
        ("2012-2016", "2012-01-03", "2016-12-31"),
        ("2017-2021", "2017-01-01", "2021-12-31"),
        ("2022+", "2022-01-01", None),
    ]
    rows = []
    for label, start, end in periods:
        for name, curve in curves.items():
            sliced = curve[curve.index >= pd.Timestamp(start)]
            if end:
                sliced = sliced[sliced.index <= pd.Timestamp(end)]
            if len(sliced) < 2:
                continue
            normalized = sliced / sliced.iloc[0] * initial_capital
            row = {"period": label, "strategy": name}
            row.update(metrics(normalized, cash_rate, initial_capital))
            rows.append(row)
    return pd.DataFrame(rows)


def create_charts(
    curves: Mapping[str, pd.Series],
    results: Mapping[str, V2Result],
    grid: pd.DataFrame,
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    plt.style.use("seaborn-v0_8-whitegrid")
    colors = ["#315C7C", "#B87950", "#6B8E7B", "#4E565C", "#9B6A6C"]
    fig, ax = plt.subplots(figsize=(11, 5.6))
    for (name, curve), color in zip(curves.items(), colors):
        normalized = curve / curve.iloc[0] * 100.0
        ax.plot(normalized.index, normalized, label=name, color=color, linewidth=1.8)
    ax.set_yscale("log")
    ax.set_ylabel("Valore normalizzato (log, base 100)")
    ax.set_title("Equity Long v2: confronto delle curve")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output_dir / "equity_curves_v2.png", dpi=175)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 4.6))
    for (name, curve), color in zip(curves.items(), colors):
        drawdown = (curve / curve.cummax() - 1.0) * 100.0
        ax.plot(drawdown.index, drawdown, label=name, color=color, linewidth=1.5)
    ax.set_ylabel("Drawdown (%)")
    ax.set_title("Drawdown")
    ax.legend(frameon=False, ncol=2)
    fig.tight_layout()
    fig.savefig(output_dir / "drawdowns_v2.png", dpi=175)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 3.8))
    for name, result in results.items():
        ax.plot(
            result.exposure.index,
            result.exposure.rolling(21, min_periods=1).mean() * 100.0,
            label=name,
            linewidth=1.4,
        )
    ax.set_ylim(0.0, 105.0)
    ax.set_ylabel("Esposizione (%)")
    ax.set_title("Esposizione azionaria media a 21 sedute")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output_dir / "exposure_v2.png", dpi=175)
    plt.close(fig)

    if not grid.empty:
        pivot = grid.pivot(index="max_positions", columns="target_volatility", values="sharpe_excess_cash")
        fig, ax = plt.subplots(figsize=(7.5, 4.5))
        image = ax.imshow(pivot.to_numpy(), cmap="RdYlGn", aspect="auto")
        ax.set_xticks(range(len(pivot.columns)), [f"{100*x:.0f}%" for x in pivot.columns])
        ax.set_yticks(range(len(pivot.index)), pivot.index)
        ax.set_xlabel("Target volatilità")
        ax.set_ylabel("Numero massimo di posizioni")
        ax.set_title("Robustezza: Sharpe netto della liquidità")
        for i in range(len(pivot.index)):
            for j in range(len(pivot.columns)):
                ax.text(j, i, f"{pivot.iloc[i, j]:.2f}", ha="center", va="center")
        fig.colorbar(image, ax=ax, shrink=0.8)
        fig.tight_layout()
        fig.savefig(output_dir / "robustness_heatmap.png", dpi=175)
        plt.close(fig)


def format_table(frame: pd.DataFrame) -> str:
    formatted = frame.copy()
    for column in formatted.columns:
        if column.endswith("_pct"):
            formatted[column] = formatted[column].map(
                lambda value: f"{value:.2f}%" if pd.notna(value) else ""
            )
        elif column in {
            "ending_value_eur",
            "fees_eur",
            "estimated_slippage_eur",
            "cash_interest_eur",
        }:
            formatted[column] = formatted[column].map(
                lambda value: f"€{value:,.0f}" if pd.notna(value) else ""
            )
        elif column in {
            "sharpe_rf0",
            "sharpe_excess_cash",
            "calmar",
            "annual_one_way_turnover",
            "probabilistic_sharpe_probability",
            "deflated_sharpe_probability",
        }:
            formatted[column] = formatted[column].map(
                lambda value: f"{value:.3f}" if pd.notna(value) else ""
            )
    return formatted.to_html(index=False, border=0, classes="data")


def build_report(
    output_dir: Path,
    metric_frame: pd.DataFrame,
    periods: pd.DataFrame,
    cost_sensitivity: pd.DataFrame,
    robustness: pd.DataFrame,
    bootstrap: Mapping[str, object],
    pbo: Mapping[str, object],
    manifest: Mapping[str, object],
) -> None:
    primary = metric_frame[metric_frame["strategy"] == "V2 Low-turnover"].iloc[0]
    baseline = metric_frame[metric_frame["strategy"] == "V1 Residual net"].iloc[0]
    spy = metric_frame[metric_frame["strategy"] == "SPY buy & hold"].iloc[0]
    conclusion = (
        f"V2 chiude a €{primary['ending_value_eur']:,.0f}, con CAGR {primary['cagr_pct']:.2f}%, "
        f"Sharpe netto della liquidità {primary['sharpe_excess_cash']:.2f} e drawdown "
        f"{primary['max_drawdown_pct']:.2f}%. La v1 registra CAGR {baseline['cagr_pct']:.2f}% "
        f"e SPY {spy['cagr_pct']:.2f}%. L'intervallo bootstrap al 95% della differenza "
        f"annualizzata V2-SPY è [{bootstrap['annualized_difference_ci_low_pct']:.2f}%, "
        f"{bootstrap['annualized_difference_ci_high_pct']:.2f}%]."
    )
    robustness_view = robustness[
        ["max_positions", "target_volatility", "cagr_pct", "sharpe_excess_cash", "max_drawdown_pct"]
    ] if not robustness.empty else robustness
    html_text = f"""<!doctype html>
<html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Equity Long v2</title><style>
body {{ font-family:Inter,Arial,sans-serif;margin:0;color:#25313a;background:#f4f6f7;line-height:1.5; }}
main {{ max-width:1180px;margin:24px auto;padding:30px 38px;background:white;box-shadow:0 4px 24px #00000012; }}
h1,h2 {{ color:#244e68; }} .callout {{ background:#edf4f7;border-left:4px solid #315c7c;padding:12px 16px; }}
.warning {{ background:#fff4df;border-left-color:#b87950; }} table.data {{ width:100%;border-collapse:collapse;font-size:12px;margin:14px 0 24px; }}
table.data th {{ background:#315c7c;color:white;padding:7px;text-align:right; }} table.data td {{ border-bottom:1px solid #dbe1e4;padding:6px 7px;text-align:right; }}
table.data th:first-child,table.data td:first-child {{ text-align:left; }} img {{ max-width:100%;margin:8px 0 20px; }}
li {{ margin-bottom:6px; }} code {{ background:#eef1f2;padding:2px 4px; }} .small {{ color:#66737c;font-size:12px; }}
</style></head><body><main><h1>Equity Long v2</h1>
<p>Risk-managed residual momentum · risultato di ricerca riproducibile</p>
<div class="callout"><strong>Esito.</strong> {html.escape(conclusion)}</div>
<h2>Confronto principale</h2>{format_table(metric_frame)}
<img src="equity_curves_v2.png" alt="Curve del capitale"><img src="drawdowns_v2.png" alt="Drawdown">
<img src="exposure_v2.png" alt="Esposizione">
<h2>Sottoperiodi predefiniti</h2>{format_table(periods)}
<h2>Sensibilità dei costi</h2>{format_table(cost_sensitivity)}
<h2>Plateau dei parametri</h2>{format_table(robustness_view)}
<img src="robustness_heatmap.png" alt="Robustezza dei parametri">
<h2>Controlli anti-overfitting</h2><ul>
<li>Block bootstrap V2-SPY: probabilità di sovraperformance {100*float(bootstrap['probability_strategy_beats_benchmark']):.1f}%; CI 95% [{float(bootstrap['annualized_difference_ci_low_pct']):.2f}%, {float(bootstrap['annualized_difference_ci_high_pct']):.2f}%].</li>
<li>PBO diagnostico: {100*float(pbo.get('pbo', np.nan)):.1f}% su {int(pbo.get('splits', 0))} suddivisioni CSCV.</li>
<li>Lo Sharpe deflazionato è riportato nella tabella principale e tiene conto del numero dichiarato di varianti.</li>
</ul>
<h2>Modifiche della v2</h2><ul>
<li>10 posizioni, peso massimo 15% per titolo e 30% per settore.</li>
<li>Pesi inverse-volatility, covarianza Ledoit-Wolf e target volatilità 14%, senza leva.</li>
<li>Regime continuo basato su trend di SPY e breadth; niente interruttore puramente binario.</li>
<li>Decisione completa ogni cinque settimane, con controllo d'emergenza settimanale; banda del 5% del NAV o €500.</li>
<li>La variante settimanale iniziale resta nel confronto; tutti i tentativi intermedi sono registrati nel design log.</li>
<li>Liquidità remunerata con proxy conservativa: max(tasso depositi BCE − 50 bps, zero).</li>
<li>Challenger ML con refit annuale, target a 63 sedute ed embargo di 5 sedute.</li>
</ul>
<h2>Limiti decisivi</h2><div class="callout warning"><ul>
<li>{html.escape(str(manifest['membership_status']))}. Finché manca il file point-in-time, permane survivorship bias.</li>
<li>{html.escape(str(manifest['pit_signal_status']))}.</li>
<li>Financial Datasets è connesso, ma il tentativo di lettura ha restituito saldo API €0; nessun dato fondamentale è stato inventato.</li>
<li>Prezzi Yahoo e 10 bps per lato restano una baseline esplorativa; servono quotazioni bid/ask o costi effettivi.</li>
<li>L'holdout storico è già stato osservato durante la v1: la prossima prova realmente nuova è il paper trading futuro.</li>
</ul></div>
<p class="small">Questo report non è consulenza finanziaria né una raccomandazione su singoli titoli.</p>
</main></body></html>"""
    (output_dir / "report_v2.html").write_text(html_text, encoding="utf-8")


def main() -> None:
    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    cache_dir = (base_dir / args.cache_dir).resolve()
    input_dir = (base_dir / args.input_dir).resolve()
    output_dir = (base_dir / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cfg = V2Config()

    symbols = sorted(set(v1.TICKER_TO_SECTOR) | set(v1.SECTOR_UNIVERSE) | {v1.BENCHMARK, v1.FX_TICKER})
    data, failures = v1.download_universe(
        symbols, args.data_start, args.end, cache_dir, args.refresh, args.workers
    )
    available_stocks = [ticker for ticker in v1.TICKER_TO_SECTOR if ticker in data]
    if len(available_stocks) < 30:
        raise RuntimeError(f"Only {len(available_stocks)} stocks available")
    print(f"Building shared features for {len(available_stocks)} stocks ...", flush=True)
    features = v1.build_features(data, v1.StrategyConfig())

    membership, membership_status = load_membership_mask(
        input_dir / "universe_membership.csv", features.close_eur.index, available_stocks
    )
    pit_signals, pit_signal_status = load_pit_signals(
        input_dir / "pit_signals.csv", features.close_eur.index, available_stocks
    )
    execution_costs, execution_cost_status = load_execution_costs(
        input_dir / "execution_costs.csv", features.close_eur.index, available_stocks
    )
    cash_rate, cash_rate_status = fetch_ecb_cash_rate(
        features.close_eur.index, cache_dir, args.refresh, cfg.cash_rate_haircut_bps
    )
    rule_score, score_weights = combine_rule_score(features.score, pit_signals)
    eligible = features.eligible & membership
    regime = continuous_regime(features, data)

    print("Running frozen v1 and pre-declared v2 ...", flush=True)
    v1_result = v1.simulate_strategy(
        features,
        features.score,
        features.rank,
        features.eligible,
        v1.StrategyConfig(),
        args.test_start,
        args.capital,
        "V1 Residual net",
    )
    weekly_cfg = dataclasses.replace(
        cfg,
        rebalance_band_weight=0.025,
        minimum_trade_eur=250.0,
        decision_interval_weeks=1,
        weight_rebalance_weeks=1,
    )
    weekly_result = simulate_v2(
        features,
        rule_score,
        eligible,
        membership,
        regime,
        cash_rate,
        execution_costs,
        weekly_cfg,
        args.test_start,
        args.capital,
        "V2 Weekly risk-managed",
    )
    v2_result = simulate_v2(
        features,
        rule_score,
        eligible,
        membership,
        regime,
        cash_rate,
        execution_costs,
        cfg,
        args.test_start,
        args.capital,
        "V2 Low-turnover",
    )

    ml_result: Optional[V2Result] = None
    ml_metadata: list[dict] = []
    ml_prediction = pd.DataFrame()
    if not args.skip_ml:
        print("Building embargoed walk-forward ML challenger ...", flush=True)
        ml_score, ml_prediction, ml_metadata = build_walk_forward_ml_score(
            features, rule_score, membership, args.test_start, cfg, pit_signals
        )
        ml_result = simulate_v2(
            features,
            ml_score,
            eligible,
            membership,
            regime,
            cash_rate,
            execution_costs,
            cfg,
            args.test_start,
            args.capital,
            "V2 ML ensemble",
        )

    fx_close = data[v1.FX_TICKER]["Close"].reindex(data[v1.BENCHMARK].index).ffill().bfill()
    spy_close_eur = data[v1.BENCHMARK]["Close"].reindex(fx_close.index).div(fx_close)
    spy_open_eur = data[v1.BENCHMARK]["Open"].reindex(fx_close.index).div(fx_close.shift(1).bfill())
    spy = v1.buy_and_hold(
        spy_open_eur,
        spy_close_eur,
        args.test_start,
        args.capital,
        cfg.fixed_fee_eur,
        cfg.default_slippage_bps_per_side,
    )

    curves: dict[str, pd.Series] = {
        "V1 Residual net": v1_result.equity,
        "V2 Weekly risk-managed": weekly_result.equity,
        "V2 Low-turnover": v2_result.equity,
    }
    results: dict[str, V2Result] = {
        "V2 Weekly risk-managed": weekly_result,
        "V2 Low-turnover": v2_result,
    }
    if ml_result is not None:
        curves["V2 ML ensemble"] = ml_result.equity
        results["V2 ML ensemble"] = ml_result
    curves["SPY buy & hold"] = spy

    robustness_rows: list[dict] = []
    robustness_curves: dict[str, pd.Series] = {}
    if not args.skip_robustness:
        print("Running the pre-declared 3x3 robustness plateau ...", flush=True)
        for positions, target_vol in itertools.product((8, 10, 12), (0.12, 0.14, 0.16)):
            variant_cfg = dataclasses.replace(
                cfg,
                max_positions=positions,
                exit_rank=positions + 8,
                target_volatility=target_vol,
            )
            variant_name = f"n{positions}_vol{int(100*target_vol)}"
            result = simulate_v2(
                features,
                rule_score,
                eligible,
                membership,
                regime,
                cash_rate,
                execution_costs,
                variant_cfg,
                args.test_start,
                args.capital,
                variant_name,
            )
            row = {
                "variant": variant_name,
                "max_positions": positions,
                "target_volatility": target_vol,
            }
            row.update(metrics(result.equity, cash_rate, args.capital, result))
            robustness_rows.append(row)
            robustness_curves[variant_name] = result.equity
    robustness = pd.DataFrame(robustness_rows)
    # Two discarded operational variants (T2/T3) are still counted: hiding
    # unsuccessful trials would overstate the Deflated Sharpe Ratio.
    declared_trials = max(1, len(robustness_curves) + len(curves) + 2)

    metric_rows = []
    for name, curve in curves.items():
        row = {"strategy": name}
        row.update(metrics(curve, cash_rate, args.capital, results.get(name), declared_trials))
        if name == "V1 Residual net":
            row.update(
                {
                    "average_exposure_pct": 100.0 * v1_result.exposure.mean(),
                    "orders": len(v1_result.orders),
                    "fees_eur": v1_result.total_fees,
                    "estimated_slippage_eur": v1_result.total_slippage_estimate,
                }
            )
        metric_rows.append(row)
    metric_frame = pd.DataFrame(metric_rows)
    periods = period_table(curves, cash_rate, args.capital)

    cost_rows = []
    for bps, fee in ((5.0, 1.0), (10.0, 1.0), (20.0, 1.0), (10.0, 2.0)):
        scenario_cfg = dataclasses.replace(
            cfg, default_slippage_bps_per_side=bps, fixed_fee_eur=fee
        )
        scenario = simulate_v2(
            features,
            rule_score,
            eligible,
            membership,
            regime,
            cash_rate,
            None,
            scenario_cfg,
            args.test_start,
            args.capital,
            f"cost_{bps:g}bps_{fee:g}eur",
        )
        row = {"slippage_bps_per_side": bps, "fixed_fee_eur": fee}
        row.update(metrics(scenario.equity, cash_rate, args.capital, scenario))
        cost_rows.append(row)
    cost_sensitivity = pd.DataFrame(cost_rows)

    bootstrap = block_bootstrap_difference(v2_result.equity, spy)
    pbo = probability_backtest_overfit(robustness_curves) if robustness_curves else {
        "pbo": np.nan,
        "splits": 0,
        "blocks": 8,
    }

    combined_curves = pd.concat(curves.values(), axis=1)
    combined_curves.to_csv(output_dir / "equity_curves_v2.csv", index_label="Date")
    metric_frame.to_csv(output_dir / "metrics_v2.csv", index=False)
    periods.to_csv(output_dir / "period_metrics_v2.csv", index=False)
    cost_sensitivity.to_csv(output_dir / "cost_sensitivity_v2.csv", index=False)
    robustness.to_csv(output_dir / "robustness_grid.csv", index=False)
    v2_result.orders.to_csv(output_dir / "orders_v2.csv", index=False)
    v2_result.realized_lots.to_csv(output_dir / "realized_lots_v2.csv", index=False)
    v2_result.allocations.to_csv(output_dir / "allocations_v2.csv", index=False)
    pd.DataFrame({"date": regime.index, "regime_multiplier": regime.values}).to_csv(
        output_dir / "regime_v2.csv", index=False
    )
    pd.DataFrame({"date": cash_rate.index, "annual_cash_rate": cash_rate.values}).to_csv(
        output_dir / "cash_rate_v2.csv", index=False
    )
    if ml_result is not None:
        ml_result.orders.to_csv(output_dir / "orders_ml_v2.csv", index=False)
        ml_prediction.loc[prediction_dates(ml_prediction.index, args.test_start)].to_csv(
            output_dir / "ml_predictions.csv", index_label="Date"
        )
    (output_dir / "ml_training_manifest.json").write_text(
        json.dumps(ml_metadata, indent=2), encoding="utf-8"
    )
    (output_dir / "bootstrap_v2.json").write_text(
        json.dumps(bootstrap, indent=2), encoding="utf-8"
    )
    (output_dir / "pbo_v2.json").write_text(json.dumps(pbo, indent=2), encoding="utf-8")

    manifest = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "data_start": args.data_start,
        "test_start": args.test_start,
        "requested_end": args.end,
        "stocks_available": len(available_stocks),
        "failed_symbols": failures,
        "v2_configuration": dataclasses.asdict(cfg),
        "score_weights": score_weights,
        "membership_status": membership_status,
        "pit_signal_status": pit_signal_status,
        "execution_cost_status": execution_cost_status,
        "cash_rate_status": cash_rate_status,
        "financial_datasets_status": "connected but calls returned balance $0.00",
        "declared_trials_for_dsr": declared_trials,
        "data_source": "Yahoo adjusted OHLCV; ECB DFR for cash; optional PIT CSV adapters",
    }
    (output_dir / "run_manifest_v2.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    create_charts(curves, results, robustness, output_dir)
    build_report(
        output_dir,
        metric_frame,
        periods,
        cost_sensitivity,
        robustness,
        bootstrap,
        pbo,
        manifest,
    )
    print("\nV2 main metrics:")
    print(
        metric_frame[
            [
                "strategy",
                "cagr_pct",
                "sharpe_excess_cash",
                "max_drawdown_pct",
                "average_exposure_pct",
                "orders",
            ]
        ].to_string(index=False)
    )
    print(f"\nReport: {output_dir / 'report_v2.html'}")


if __name__ == "__main__":
    main()
