#!/usr/bin/env python3
"""Cost-aware, long-only equity momentum backtest.

The strategy is intentionally transparent and suitable as a research baseline:

* weekly decisions, executed at the following session's open;
* cross-sectional residual momentum versus SPY and the stock's sector ETF;
* market/stock trend, liquidity, volatility and over-extension gates;
* rank hysteresis to reduce turnover;
* Trade Republic-like fixed order fees plus configurable slippage/spread;
* EUR portfolio accounting for USD securities using EURUSD daily data.

This is research software, not investment advice or production execution code.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import html
import json
import math
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


SECTOR_UNIVERSE: Dict[str, Sequence[str]] = {
    "XLK": ("AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "ADBE", "CSCO", "IBM", "QCOM", "TXN", "AMAT", "INTU"),
    "XLC": ("GOOGL", "META", "NFLX", "DIS", "CMCSA", "TMUS"),
    "XLY": ("AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX", "BKNG"),
    "XLP": ("WMT", "COST", "PG", "KO", "PEP", "PM", "MDLZ"),
    "XLF": ("JPM", "BAC", "WFC", "GS", "MS", "V", "MA", "AXP", "BLK"),
    "XLV": ("LLY", "JNJ", "UNH", "ABBV", "MRK", "TMO", "ABT", "AMGN", "GILD"),
    "XLI": ("CAT", "GE", "HON", "UPS", "RTX", "BA", "DE", "UNP"),
    "XLE": ("XOM", "CVX", "COP", "SLB", "EOG"),
    "XLU": ("NEE", "DUK", "SO"),
    "XLB": ("LIN", "APD", "NEM", "SHW"),
    "XLRE": ("PLD", "AMT"),
}

TICKER_TO_SECTOR = {
    ticker: sector for sector, members in SECTOR_UNIVERSE.items() for ticker in members
}

BENCHMARK = "SPY"
FX_TICKER = "EURUSD=X"


@dataclasses.dataclass(frozen=True)
class StrategyConfig:
    max_positions: int = 5
    exit_rank: int = 10
    fixed_fee_eur: float = 1.0
    slippage_bps_per_side: float = 10.0
    minimum_price_usd: float = 5.0
    minimum_median_dollar_volume: float = 50_000_000.0
    maximum_extension_z: float = 2.5
    minimum_extension_z: float = -2.0
    trend_fast_window: int = 50
    trend_slow_window: int = 200
    score_residual_12_1: float = 0.40
    score_residual_6_1: float = 0.25
    score_raw_12_1: float = 0.15
    score_trend: float = 0.10
    score_low_volatility: float = 0.10


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-start", default="2007-01-01")
    parser.add_argument("--test-start", default="2012-01-03")
    parser.add_argument("--end", default=(dt.date.today() + dt.timedelta(days=1)).isoformat())
    parser.add_argument("--capital", type=float, default=10_000.0)
    parser.add_argument("--cache-dir", default="data_cache")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--workers", type=int, default=5)
    return parser.parse_args()


def _epoch_seconds(date_string: str) -> int:
    value = pd.Timestamp(date_string, tz="UTC")
    return int(value.timestamp())


def _safe_filename(symbol: str) -> str:
    return symbol.replace("=", "_").replace("^", "_").replace("/", "_")


def fetch_yahoo_daily(
    symbol: str,
    start: str,
    end: str,
    cache_dir: Path,
    refresh: bool = False,
    retries: int = 7,
) -> pd.DataFrame:
    """Download adjusted daily OHLCV from Yahoo's chart endpoint."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{_safe_filename(symbol)}.csv"
    if cache_path.exists() and not refresh:
        frame = pd.read_csv(cache_path, parse_dates=["Date"], index_col="Date")
        return frame.sort_index()

    query = urllib.parse.urlencode(
        {
            "period1": _epoch_seconds(start),
            "period2": _epoch_seconds(end),
            "interval": "1d",
            "events": "div,splits",
            "includeAdjustedClose": "true",
        }
    )
    quoted = urllib.parse.quote(symbol, safe="")
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        host = "query1.finance.yahoo.com" if attempt % 2 == 0 else "query2.finance.yahoo.com"
        url = f"https://{host}/v8/finance/chart/{quoted}?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; EquityLongResearch/1.0)",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                payload = json.loads(response.read().decode("utf-8"))
            chart = payload.get("chart", {})
            if chart.get("error"):
                raise RuntimeError(str(chart["error"]))
            result = chart.get("result")
            if not result:
                raise RuntimeError("empty Yahoo chart result")
            result = result[0]
            timestamps = result.get("timestamp") or []
            quote_data = (result.get("indicators", {}).get("quote") or [{}])[0]
            adj_data = (result.get("indicators", {}).get("adjclose") or [{}])[0]
            adjusted_close = adj_data.get("adjclose") or quote_data.get("close") or []
            if not timestamps or not adjusted_close:
                raise RuntimeError("missing timestamps or adjusted close")

            def values(name: str) -> Sequence[Optional[float]]:
                return quote_data.get(name) or [None] * len(timestamps)

            raw_close = pd.Series(values("close"), dtype="float64")
            adj_close = pd.Series(adjusted_close, dtype="float64")
            factor = adj_close.div(raw_close.replace(0.0, np.nan))
            index = pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(None).normalize()
            frame = pd.DataFrame(
                {
                    "Open": pd.Series(values("open"), dtype="float64").mul(factor).to_numpy(),
                    "High": pd.Series(values("high"), dtype="float64").mul(factor).to_numpy(),
                    "Low": pd.Series(values("low"), dtype="float64").mul(factor).to_numpy(),
                    "Close": adj_close.to_numpy(),
                    "RawClose": raw_close.to_numpy(),
                    "Volume": pd.Series(values("volume"), dtype="float64").to_numpy(),
                },
                index=index,
            )
            frame.index.name = "Date"
            frame = frame[~frame.index.duplicated(keep="last")].sort_index()
            frame = frame.dropna(subset=["Close"])
            if len(frame) < 100:
                raise RuntimeError(f"too few observations: {len(frame)}")
            frame.to_csv(cache_path)
            return frame
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(min(12.0, 1.1 * (2 ** attempt)) + random.random())
    raise RuntimeError(f"Unable to download {symbol}: {last_error}")


def download_universe(
    symbols: Sequence[str],
    start: str,
    end: str,
    cache_dir: Path,
    refresh: bool,
    workers: int,
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, str]]:
    data: Dict[str, pd.DataFrame] = {}
    failures: Dict[str, str] = {}
    print(f"Downloading/loading {len(symbols)} series ...", flush=True)

    def one(symbol: str) -> Tuple[str, Optional[pd.DataFrame], Optional[str]]:
        try:
            frame = fetch_yahoo_daily(symbol, start, end, cache_dir, refresh)
            return symbol, frame, None
        except Exception as exc:  # retain other symbols and report failure
            return symbol, None, str(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(one, symbol): symbol for symbol in symbols}
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            symbol, frame, error = future.result()
            completed += 1
            if error is None and frame is not None:
                data[symbol] = frame
            else:
                failures[symbol] = error or "unknown error"
            if completed % 10 == 0 or completed == len(symbols):
                print(f"  {completed}/{len(symbols)} complete; failures={len(failures)}", flush=True)
    return data, failures


def robust_cross_sectional_z(frame: pd.DataFrame) -> pd.DataFrame:
    median = frame.median(axis=1, skipna=True)
    mad = frame.sub(median, axis=0).abs().median(axis=1, skipna=True)
    scale = (1.4826 * mad).replace(0.0, np.nan)
    robust = frame.sub(median, axis=0).div(scale, axis=0)
    fallback_std = frame.std(axis=1, skipna=True).replace(0.0, np.nan)
    fallback = frame.sub(frame.mean(axis=1, skipna=True), axis=0).div(fallback_std, axis=0)
    return robust.fillna(fallback).clip(-3.0, 3.0)


def rolling_two_factor_residual(
    stock_return: pd.Series,
    market_return: pd.Series,
    sector_return: pd.Series,
    window: int = 252,
) -> pd.Series:
    min_periods = 160
    var_m = market_return.rolling(window, min_periods=min_periods).var()
    var_s = sector_return.rolling(window, min_periods=min_periods).var()
    cov_ms = market_return.rolling(window, min_periods=min_periods).cov(sector_return)
    cov_ym = stock_return.rolling(window, min_periods=min_periods).cov(market_return)
    cov_ys = stock_return.rolling(window, min_periods=min_periods).cov(sector_return)
    ridge = 1e-10
    determinant = (var_m + ridge) * (var_s + ridge) - cov_ms.pow(2)
    beta_m = ((cov_ym * (var_s + ridge) - cov_ys * cov_ms) / determinant).shift(1)
    beta_s = ((cov_ys * (var_m + ridge) - cov_ym * cov_ms) / determinant).shift(1)
    residual = stock_return - beta_m * market_return - beta_s * sector_return
    return residual.replace([np.inf, -np.inf], np.nan)


@dataclasses.dataclass
class FeatureBundle:
    close_usd: pd.DataFrame
    open_eur: pd.DataFrame
    close_eur: pd.DataFrame
    score: pd.DataFrame
    rank: pd.DataFrame
    eligible: pd.DataFrame
    simple_score: pd.DataFrame
    simple_rank: pd.DataFrame
    simple_eligible: pd.DataFrame
    risk_on: pd.Series
    feature_tables: Mapping[str, pd.DataFrame]


def build_features(data: Mapping[str, pd.DataFrame], cfg: StrategyConfig) -> FeatureBundle:
    if cfg.trend_fast_window >= cfg.trend_slow_window:
        raise ValueError("trend_fast_window must be smaller than trend_slow_window")
    if BENCHMARK not in data or FX_TICKER not in data:
        raise RuntimeError("SPY or EURUSD=X missing; cannot construct the test")
    stock_symbols = [ticker for ticker in TICKER_TO_SECTOR if ticker in data]
    required_sectors = sorted({TICKER_TO_SECTOR[ticker] for ticker in stock_symbols})
    missing_sectors = [sector for sector in required_sectors if sector not in data]
    if missing_sectors:
        raise RuntimeError(f"Missing sector ETF data: {missing_sectors}")

    index = data[BENCHMARK].index
    close_usd = pd.DataFrame({ticker: data[ticker]["Close"].reindex(index) for ticker in stock_symbols})
    open_usd = pd.DataFrame({ticker: data[ticker]["Open"].reindex(index) for ticker in stock_symbols})
    raw_close = pd.DataFrame({ticker: data[ticker]["RawClose"].reindex(index) for ticker in stock_symbols})
    volume = pd.DataFrame({ticker: data[ticker]["Volume"].reindex(index) for ticker in stock_symbols})

    fx_close = data[FX_TICKER]["Close"].reindex(index).ffill().bfill()
    close_eur = close_usd.div(fx_close, axis=0)
    # The previous FX close is observable before the next US opening auction.
    execution_fx = fx_close.shift(1).bfill()
    open_eur = open_usd.div(execution_fx, axis=0)

    log_close = np.log(close_usd.where(close_usd > 0.0))
    stock_returns = log_close.diff()
    spy_close = data[BENCHMARK]["Close"].reindex(index)
    market_returns = np.log(spy_close).diff()
    sector_close = {sector: data[sector]["Close"].reindex(index) for sector in required_sectors}
    sector_returns = {sector: np.log(series).diff() for sector, series in sector_close.items()}

    residuals = pd.DataFrame(index=index, columns=stock_symbols, dtype="float64")
    for ticker in stock_symbols:
        sector = TICKER_TO_SECTOR[ticker]
        residuals[ticker] = rolling_two_factor_residual(
            stock_returns[ticker], market_returns, sector_returns[sector]
        )

    residual_12_1 = residuals.shift(21).rolling(231, min_periods=160).sum()
    residual_6_1 = residuals.shift(21).rolling(105, min_periods=80).sum()
    raw_12_1 = log_close.shift(21) - log_close.shift(252)
    raw_6_1 = log_close.shift(21) - log_close.shift(126)

    sma_20 = close_usd.rolling(20, min_periods=15).mean()
    fast_min_periods = max(2, math.ceil(0.80 * cfg.trend_fast_window))
    slow_min_periods = max(2, math.ceil(0.80 * cfg.trend_slow_window))
    sma_fast = close_usd.rolling(
        cfg.trend_fast_window, min_periods=fast_min_periods
    ).mean()
    sma_slow = close_usd.rolling(
        cfg.trend_slow_window, min_periods=slow_min_periods
    ).mean()
    daily_vol_20 = stock_returns.rolling(20, min_periods=15).std()
    annual_vol_63 = stock_returns.rolling(63, min_periods=42).std() * math.sqrt(252.0)
    trend_strength = np.log(close_usd.div(sma_slow)).div(annual_vol_63.replace(0.0, np.nan))
    extension_5 = (log_close - log_close.shift(5)).div(
        daily_vol_20.replace(0.0, np.nan) * math.sqrt(5.0)
    )
    median_dollar_volume = (raw_close * volume).rolling(20, min_periods=15).median()

    score = (
        cfg.score_residual_12_1 * robust_cross_sectional_z(residual_12_1)
        + cfg.score_residual_6_1 * robust_cross_sectional_z(residual_6_1)
        + cfg.score_raw_12_1 * robust_cross_sectional_z(raw_12_1)
        + cfg.score_trend * robust_cross_sectional_z(trend_strength)
        + cfg.score_low_volatility * robust_cross_sectional_z(-annual_vol_63)
    )
    simple_score = 0.70 * robust_cross_sectional_z(raw_12_1) + 0.30 * robust_cross_sectional_z(raw_6_1)

    base_gate = (
        (raw_close >= cfg.minimum_price_usd)
        & (median_dollar_volume >= cfg.minimum_median_dollar_volume)
        & (close_usd > sma_slow)
        & (sma_fast > sma_slow)
        & (raw_12_1 > 0.0)
        & (extension_5 <= cfg.maximum_extension_z)
        & (extension_5 >= cfg.minimum_extension_z)
    )
    eligible = base_gate & (residual_12_1 > 0.0) & score.notna()
    simple_eligible = base_gate & simple_score.notna()
    rank = score.where(eligible).rank(axis=1, ascending=False, method="min")
    simple_rank = simple_score.where(simple_eligible).rank(axis=1, ascending=False, method="min")

    spy_sma_fast = spy_close.rolling(
        cfg.trend_fast_window, min_periods=fast_min_periods
    ).mean()
    spy_sma_slow = spy_close.rolling(
        cfg.trend_slow_window, min_periods=slow_min_periods
    ).mean()
    risk_on = ((spy_close > spy_sma_slow) & (spy_sma_fast > spy_sma_slow)).fillna(False)

    return FeatureBundle(
        close_usd=close_usd,
        open_eur=open_eur,
        close_eur=close_eur,
        score=score,
        rank=rank,
        eligible=eligible,
        simple_score=simple_score,
        simple_rank=simple_rank,
        simple_eligible=simple_eligible,
        risk_on=risk_on,
        feature_tables={
            "residual_12_1": residual_12_1,
            "residual_6_1": residual_6_1,
            "raw_12_1": raw_12_1,
            "raw_6_1": raw_6_1,
            "trend_strength": trend_strength,
            "extension_5": extension_5,
            "annual_vol_63": annual_vol_63,
            "median_dollar_volume": median_dollar_volume,
        },
    )


@dataclasses.dataclass
class Position:
    shares: float
    entry_cash: float
    entry_date: pd.Timestamp
    entry_price: float


@dataclasses.dataclass
class SimulationResult:
    equity: pd.Series
    exposure: pd.Series
    orders: pd.DataFrame
    closed_trades: pd.DataFrame
    total_fees: float
    total_slippage_estimate: float


def _weekly_execution_map(index: pd.DatetimeIndex, test_start: pd.Timestamp) -> Dict[pd.Timestamp, pd.Timestamp]:
    eligible_dates = index[index >= test_start]
    if eligible_dates.empty:
        return {}
    mapping: Dict[pd.Timestamp, pd.Timestamp] = {}
    first = eligible_dates[0]
    prior = index[index < first]
    if len(prior):
        mapping[first] = prior[-1]
    decisions = pd.Series(index, index=index).groupby(index.to_period("W-FRI")).last().tolist()
    positions = {date: idx for idx, date in enumerate(index)}
    for decision in decisions:
        location = positions[decision]
        if location + 1 >= len(index):
            continue
        execution = index[location + 1]
        if execution >= test_start:
            mapping[execution] = decision
    return mapping


def simulate_strategy(
    features: FeatureBundle,
    score: pd.DataFrame,
    rank: pd.DataFrame,
    eligible: pd.DataFrame,
    cfg: StrategyConfig,
    test_start: str,
    initial_capital: float,
    name: str,
) -> SimulationResult:
    start = pd.Timestamp(test_start)
    full_index = features.close_eur.index
    sim_index = full_index[full_index >= start]
    execution_map = _weekly_execution_map(full_index, start)
    open_values = features.open_eur.ffill()
    close_values = features.close_eur.ffill()
    cash = float(initial_capital)
    positions: Dict[str, Position] = {}
    equity_values: List[float] = []
    exposure_values: List[float] = []
    order_rows: List[dict] = []
    trade_rows: List[dict] = []
    total_fees = 0.0
    total_slippage = 0.0
    slip = cfg.slippage_bps_per_side / 10_000.0

    def value_at(date: pd.Timestamp, field: pd.DataFrame) -> float:
        return cash + sum(
            position.shares * float(field.at[date, ticker])
            for ticker, position in positions.items()
            if pd.notna(field.at[date, ticker])
        )

    def sell(date: pd.Timestamp, ticker: str, reference_price: float, reason: str) -> None:
        nonlocal cash, total_fees, total_slippage
        position = positions.pop(ticker)
        execution_price = reference_price * (1.0 - slip)
        gross = position.shares * execution_price
        fee = cfg.fixed_fee_eur
        proceeds = max(0.0, gross - fee)
        cash += proceeds
        total_fees += fee
        total_slippage += position.shares * reference_price * slip
        pnl = proceeds - position.entry_cash
        order_rows.append(
            {
                "strategy": name,
                "date": date,
                "action": "SELL",
                "ticker": ticker,
                "shares": position.shares,
                "reference_price_eur": reference_price,
                "execution_price_eur": execution_price,
                "gross_eur": gross,
                "fee_eur": fee,
                "reason": reason,
            }
        )
        trade_rows.append(
            {
                "strategy": name,
                "ticker": ticker,
                "entry_date": position.entry_date,
                "exit_date": date,
                "entry_cash_eur": position.entry_cash,
                "exit_proceeds_eur": proceeds,
                "pnl_eur": pnl,
                "return_pct": 100.0 * pnl / position.entry_cash if position.entry_cash else np.nan,
                "calendar_days": (date - position.entry_date).days,
                "exit_reason": reason,
            }
        )

    def buy(date: pd.Timestamp, ticker: str, reference_price: float, budget: float) -> None:
        nonlocal cash, total_fees, total_slippage
        fee = cfg.fixed_fee_eur
        budget = min(budget, cash)
        if budget <= fee + 1.0 or reference_price <= 0.0:
            return
        execution_price = reference_price * (1.0 + slip)
        shares = (budget - fee) / execution_price
        if shares <= 0.0:
            return
        deployed = shares * execution_price + fee
        cash -= deployed
        total_fees += fee
        total_slippage += shares * reference_price * slip
        positions[ticker] = Position(
            shares=shares,
            entry_cash=deployed,
            entry_date=date,
            entry_price=execution_price,
        )
        order_rows.append(
            {
                "strategy": name,
                "date": date,
                "action": "BUY",
                "ticker": ticker,
                "shares": shares,
                "reference_price_eur": reference_price,
                "execution_price_eur": execution_price,
                "gross_eur": shares * execution_price,
                "fee_eur": fee,
                "reason": "weekly_entry",
            }
        )

    for date in sim_index:
        decision_date = execution_map.get(date)
        if decision_date is not None:
            row_score = score.loc[decision_date]
            row_rank = rank.loc[decision_date]
            row_eligible = eligible.loc[decision_date]
            risk_on = bool(features.risk_on.loc[decision_date])
            if risk_on:
                current_keep = [
                    ticker
                    for ticker in positions
                    if bool(row_eligible.get(ticker, False))
                    and pd.notna(row_rank.get(ticker))
                    and float(row_rank[ticker]) <= cfg.exit_rank
                ]
                ranked_candidates = (
                    row_score.where(row_eligible).dropna().sort_values(ascending=False).index.tolist()
                )
                target = current_keep[: cfg.max_positions]
                for ticker in ranked_candidates:
                    if len(target) >= cfg.max_positions:
                        break
                    if ticker not in target:
                        target.append(ticker)
            else:
                target = []

            for ticker in list(positions):
                if ticker not in target:
                    price = open_values.at[date, ticker]
                    if pd.notna(price):
                        sell(date, ticker, float(price), "risk_off" if not risk_on else "rank_exit")

            new_tickers = [ticker for ticker in target if ticker not in positions]
            if new_tickers:
                nav_open = value_at(date, open_values)
                slot_budget = nav_open / cfg.max_positions
                for idx_new, ticker in enumerate(new_tickers):
                    price = open_values.at[date, ticker]
                    if pd.isna(price):
                        continue
                    remaining = len(new_tickers) - idx_new
                    affordable = max(0.0, cash / remaining)
                    buy(date, ticker, float(price), min(slot_budget, affordable))

        close_nav = value_at(date, close_values)
        invested = close_nav - cash
        equity_values.append(close_nav)
        exposure_values.append(invested / close_nav if close_nav > 0.0 else 0.0)

    # Liquidate for a fair, fully realized terminal value and complete trade statistics.
    if len(sim_index) and positions:
        final_date = sim_index[-1]
        for ticker in list(positions):
            price = close_values.at[final_date, ticker]
            if pd.notna(price):
                sell(final_date, ticker, float(price), "end_of_test")
        equity_values[-1] = cash
        exposure_values[-1] = 0.0

    equity = pd.Series(equity_values, index=sim_index, name=name)
    exposure = pd.Series(exposure_values, index=sim_index, name=name)
    orders = pd.DataFrame(order_rows)
    closed_trades = pd.DataFrame(trade_rows)
    return SimulationResult(equity, exposure, orders, closed_trades, total_fees, total_slippage)


def buy_and_hold(
    open_eur: pd.Series,
    close_eur: pd.Series,
    test_start: str,
    initial_capital: float,
    fixed_fee: float,
    slippage_bps: float,
) -> pd.Series:
    index = close_eur.index[close_eur.index >= pd.Timestamp(test_start)]
    index = index[open_eur.reindex(index).notna() & close_eur.reindex(index).notna()]
    if index.empty:
        raise RuntimeError("No benchmark observations in test window")
    slip = slippage_bps / 10_000.0
    first = index[0]
    entry_price = float(open_eur.loc[first]) * (1.0 + slip)
    shares = (initial_capital - fixed_fee) / entry_price
    curve = shares * close_eur.reindex(index)
    final = index[-1]
    curve.loc[final] = shares * float(close_eur.loc[final]) * (1.0 - slip) - fixed_fee
    curve.name = "SPY buy & hold"
    return curve


def performance_metrics(
    curve: pd.Series,
    exposure: Optional[pd.Series] = None,
    trades: Optional[pd.DataFrame] = None,
) -> dict:
    curve = curve.dropna()
    if len(curve) < 2:
        return {}
    total_return = curve.iloc[-1] / curve.iloc[0] - 1.0
    years = max((curve.index[-1] - curve.index[0]).days / 365.25, 1 / 365.25)
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1.0 / years) - 1.0
    daily = curve.pct_change(fill_method=None).dropna()
    annual_vol = daily.std(ddof=1) * math.sqrt(252.0)
    sharpe = daily.mean() / daily.std(ddof=1) * math.sqrt(252.0) if daily.std(ddof=1) else np.nan
    downside = daily[daily < 0.0].std(ddof=1) * math.sqrt(252.0)
    sortino = daily.mean() * 252.0 / downside if downside and not np.isnan(downside) else np.nan
    drawdown = curve / curve.cummax() - 1.0
    max_drawdown = drawdown.min()
    calmar = cagr / abs(max_drawdown) if max_drawdown < 0.0 else np.nan
    result = {
        "start": curve.index[0].date().isoformat(),
        "end": curve.index[-1].date().isoformat(),
        "total_return_pct": 100.0 * total_return,
        "cagr_pct": 100.0 * cagr,
        "annual_volatility_pct": 100.0 * annual_vol,
        "sharpe_rf0": sharpe,
        "sortino_rf0": sortino,
        "max_drawdown_pct": 100.0 * max_drawdown,
        "calmar": calmar,
    }
    if exposure is not None:
        result["average_exposure_pct"] = 100.0 * exposure.reindex(curve.index).mean()
    if trades is not None and not trades.empty:
        result.update(
            {
                "closed_trades": int(len(trades)),
                "win_rate_pct": 100.0 * float((trades["pnl_eur"] > 0.0).mean()),
                "median_trade_return_pct": float(trades["return_pct"].median()),
                "median_holding_days": float(trades["calendar_days"].median()),
            }
        )
    return result


def slice_metrics(
    curve: pd.Series,
    start: str,
    end: Optional[str] = None,
    exposure: Optional[pd.Series] = None,
    trades: Optional[pd.DataFrame] = None,
) -> dict:
    sliced = curve[curve.index >= pd.Timestamp(start)]
    if end:
        sliced = sliced[sliced.index <= pd.Timestamp(end)]
    if len(sliced) < 2:
        return {}
    sliced = sliced / sliced.iloc[0] * 100.0
    sliced_exposure = exposure.reindex(sliced.index) if exposure is not None else None
    sliced_trades = None
    if trades is not None and not trades.empty:
        exits = pd.to_datetime(trades["exit_date"])
        mask = exits >= pd.Timestamp(start)
        if end:
            mask &= exits <= pd.Timestamp(end)
        sliced_trades = trades.loc[mask]
    return performance_metrics(sliced, sliced_exposure, sliced_trades)


def latest_ranking(features: FeatureBundle, limit: int = 20) -> pd.DataFrame:
    valid_dates = features.score.dropna(how="all").index
    if valid_dates.empty:
        return pd.DataFrame()
    date = valid_dates[-1]
    rows = []
    for ticker in features.score.columns:
        value = features.score.at[date, ticker]
        if pd.isna(value):
            continue
        rows.append(
            {
                "as_of": date.date().isoformat(),
                "ticker": ticker,
                "sector_etf": TICKER_TO_SECTOR[ticker],
                "score": float(value),
                "rank_if_eligible": features.rank.at[date, ticker],
                "eligible": bool(features.eligible.at[date, ticker]),
                "residual_12_1": features.feature_tables["residual_12_1"].at[date, ticker],
                "raw_12_1": features.feature_tables["raw_12_1"].at[date, ticker],
                "annual_volatility": features.feature_tables["annual_vol_63"].at[date, ticker],
                "extension_5_z": features.feature_tables["extension_5"].at[date, ticker],
            }
        )
    result = pd.DataFrame(rows).sort_values("score", ascending=False)
    return result.head(limit)


def format_metric_table(frame: pd.DataFrame) -> pd.DataFrame:
    formatted = frame.copy()
    percent_columns = [column for column in formatted if column.endswith("_pct")]
    for column in percent_columns:
        formatted[column] = formatted[column].map(lambda x: f"{x:.2f}%" if pd.notna(x) else "")
    # Percentage columns have already been converted to display strings above.
    for column in ("sharpe_rf0", "sortino_rf0", "calmar", "median_holding_days"):
        if column in formatted:
            formatted[column] = formatted[column].map(lambda x: f"{x:.2f}" if pd.notna(x) else "")
    return formatted


def create_plots(
    curves: Mapping[str, pd.Series],
    main_result: SimulationResult,
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    plt.style.use("seaborn-v0_8-whitegrid")
    palette = {
        "Residual Long net": "#315C7C",
        "Residual Long gross": "#82A6B1",
        "Simple momentum net": "#B87950",
        "SPY buy & hold": "#494F55",
    }
    normalized = pd.DataFrame(
        {name: curve / curve.dropna().iloc[0] * 100.0 for name, curve in curves.items()}
    ).ffill()
    fig, ax = plt.subplots(figsize=(11, 6))
    for column in normalized:
        ax.plot(normalized.index, normalized[column], label=column, lw=2.0, color=palette.get(column))
    ax.set_yscale("log")
    ax.set_ylabel("Valore normalizzato (log, base 100)")
    ax.set_title("Long azionario: crescita del capitale")
    ax.legend(frameon=False, ncol=2)
    fig.tight_layout()
    fig.savefig(output_dir / "equity_curves.png", dpi=170)
    plt.close(fig)

    drawdowns = normalized.div(normalized.cummax()).sub(1.0) * 100.0
    fig, ax = plt.subplots(figsize=(11, 5))
    for column in drawdowns:
        ax.plot(drawdowns.index, drawdowns[column], label=column, lw=1.6, color=palette.get(column))
    ax.set_ylabel("Drawdown (%)")
    ax.set_title("Drawdown dal massimo precedente")
    ax.legend(frameon=False, ncol=2)
    fig.tight_layout()
    fig.savefig(output_dir / "drawdowns.png", dpi=170)
    plt.close(fig)

    annual = normalized.resample("YE").last().pct_change(fill_method=None).mul(100.0)
    if len(annual) > 1:
        fig, ax = plt.subplots(figsize=(12, 5))
        annual.index = annual.index.year
        annual.plot(kind="bar", ax=ax, width=0.8, color=[palette.get(c, "#777777") for c in annual.columns])
        ax.axhline(0.0, color="#333333", lw=0.8)
        ax.set_ylabel("Rendimento annuale (%)")
        ax.set_title("Rendimenti per anno")
        ax.legend(frameon=False, ncol=2)
        fig.tight_layout()
        fig.savefig(output_dir / "annual_returns.png", dpi=170)
        plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 3.8))
    rolling_exposure = main_result.exposure.rolling(21, min_periods=1).mean() * 100.0
    ax.fill_between(rolling_exposure.index, rolling_exposure.values, color="#5C849B", alpha=0.75)
    ax.set_ylim(0.0, 105.0)
    ax.set_ylabel("Esposizione (%)")
    ax.set_title("Esposizione azionaria media mobile a 21 giorni")
    fig.tight_layout()
    fig.savefig(output_dir / "exposure.png", dpi=170)
    plt.close(fig)


def build_html_report(
    output_dir: Path,
    cfg: StrategyConfig,
    metrics: pd.DataFrame,
    period_metrics: pd.DataFrame,
    cost_sensitivity: pd.DataFrame,
    latest: pd.DataFrame,
    failures: Mapping[str, str],
    data_start: str,
    test_start: str,
    end: str,
) -> None:
    def value(frame: pd.DataFrame, strategy: str, column: str, period: Optional[str] = None) -> float:
        subset = frame[frame["strategy"] == strategy]
        if period is not None:
            subset = subset[subset["period"] == period]
        return float(subset.iloc[0][column])

    residual_cagr = value(metrics, "Residual Long net", "cagr_pct")
    residual_sharpe = value(metrics, "Residual Long net", "sharpe_rf0")
    residual_drawdown = value(metrics, "Residual Long net", "max_drawdown_pct")
    gross_cagr = value(metrics, "Residual Long gross", "cagr_pct")
    spy_cagr = value(metrics, "SPY buy & hold", "cagr_pct")
    spy_sharpe = value(metrics, "SPY buy & hold", "sharpe_rf0")
    spy_drawdown = value(metrics, "SPY buy & hold", "max_drawdown_pct")
    validation_residual = value(period_metrics, "Residual Long net", "cagr_pct", "Validazione 2019-2022")
    validation_spy = value(period_metrics, "SPY buy & hold", "cagr_pct", "Validazione 2019-2022")
    holdout_residual = value(period_metrics, "Residual Long net", "cagr_pct", "Holdout 2023+")
    holdout_spy = value(period_metrics, "SPY buy & hold", "cagr_pct", "Holdout 2023+")
    metric_html = format_metric_table(metrics).to_html(index=False, border=0, classes="data")
    period_html = format_metric_table(period_metrics).to_html(index=False, border=0, classes="data")
    cost_html = format_metric_table(cost_sensitivity).to_html(index=False, border=0, classes="data")
    latest_view = latest.copy()
    for column in ("score", "rank_if_eligible", "residual_12_1", "raw_12_1", "annual_volatility", "extension_5_z"):
        if column in latest_view:
            latest_view[column] = latest_view[column].map(lambda x: f"{x:.3f}" if pd.notna(x) else "")
    latest_html = latest_view.to_html(index=False, border=0, classes="data")
    failures_html = "<p>Nessun download fallito.</p>" if not failures else "<pre>" + html.escape(json.dumps(failures, indent=2)) + "</pre>"
    weights = {
        "Residual momentum 12-1": cfg.score_residual_12_1,
        "Residual momentum 6-1": cfg.score_residual_6_1,
        "Momentum grezzo 12-1": cfg.score_raw_12_1,
        "Forza del trend": cfg.score_trend,
        "Bassa volatilità": cfg.score_low_volatility,
    }
    html_text = f"""<!doctype html>
<html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backtest Long Azionario</title>
<style>
body {{ font-family: Inter, Arial, sans-serif; margin: 0; color: #25313a; background: #f4f6f7; line-height: 1.5; }}
main {{ max-width: 1120px; margin: 24px auto; padding: 30px 38px; background: white; box-shadow: 0 4px 24px #00000012; }}
h1,h2 {{ color:#244e68; }} h1 {{ margin-bottom:4px; }} .subtitle {{ color:#66737c; margin-top:0; }}
.callout {{ background:#edf4f7; border-left:4px solid #315c7c; padding:12px 16px; }}
.warning {{ background:#fff4df; border-left-color:#b87950; }}
table.data {{ width:100%; border-collapse:collapse; font-size:13px; margin:14px 0 24px; }}
table.data th {{ background:#315c7c; color:white; padding:8px; text-align:right; }} table.data th:nth-child(-n+3) {{ text-align:left; }}
table.data td {{ border-bottom:1px solid #dbe1e4; padding:7px 8px; text-align:right; }} table.data td:nth-child(-n+3) {{ text-align:left; }}
img {{ max-width:100%; margin:8px 0 20px; }} code {{ background:#eef1f2; padding:2px 4px; }}
li {{ margin-bottom:6px; }} .small {{ font-size:12px; color:#66737c; }}
</style></head><body><main>
<h1>Backtest Long Azionario</h1>
<p class="subtitle">Motore Residual Momentum · dati {html.escape(data_start)} – {html.escape(end)} · simulazione da {html.escape(test_start)}</p>
<div class="callout"><strong>Obiettivo.</strong> Verificare una strategia long multiday, trasparente e compatibile con i costi fissi di Trade Republic. I segnali sono calcolati a chiusura e gli ordini vengono eseguiti all'apertura successiva.</div>

<h2>Risultati principali</h2>
{metric_html}
<div class="callout warning"><strong>Lettura corretta.</strong> Nel periodo completo il modello residuo netto registra CAGR {residual_cagr:.2f}% contro {spy_cagr:.2f}% di SPY e drawdown {residual_drawdown:.2f}% contro {spy_drawdown:.2f}%. Tuttavia lo Sharpe a tasso zero è inferiore ({residual_sharpe:.2f} contro {spy_sharpe:.2f}) e il costo stimato riduce il CAGR lordo da {gross_cagr:.2f}% a {residual_cagr:.2f}%. Inoltre il modello sottoperforma SPY nella validazione 2019–2022 ({validation_residual:.2f}% contro {validation_spy:.2f}% annuo), mentre sovraperforma nell'holdout 2023+ ({holdout_residual:.2f}% contro {holdout_spy:.2f}%). Il segnale è quindi promettente ma non conclusivo.</div>
<img src="equity_curves.png" alt="Curve del capitale">
<img src="drawdowns.png" alt="Drawdown">
<img src="annual_returns.png" alt="Rendimenti annuali">
<img src="exposure.png" alt="Esposizione">

<h2>Stabilità nei sottoperiodi</h2>
{period_html}

<h2>Sensibilità al capitale e ai costi fissi</h2>
{cost_html}

<h2>Regole del motore</h2>
<ul>
  <li>Universo: large/mid cap USA liquide, raggruppate per ETF settoriale.</li>
  <li>Massimo {cfg.max_positions} posizioni; controllo e possibile rotazione settimanale.</li>
  <li>Regime attivo solo con SPY sopra SMA200 e SMA50 sopra SMA200.</li>
  <li>Entrata soltanto con titolo sopra SMA200, SMA50 sopra SMA200, momentum positivo, volume mediano ≥ ${cfg.minimum_median_dollar_volume:,.0f} e nessuna estensione estrema.</li>
  <li>I titoli già detenuti rimangono finché sono idonei e non scendono oltre il rango {cfg.exit_rank}; questa isteresi limita il turnover.</li>
  <li>Score: {html.escape(json.dumps(weights, ensure_ascii=False))}.</li>
  <li>Costi base: €{cfg.fixed_fee_eur:.2f} per ordine e {cfg.slippage_bps_per_side:.1f} bps per lato.</li>
</ul>

<h2>Ranking più recente (informativo)</h2>
{latest_html}

<h2>Limiti da non ignorare</h2>
<div class="callout warning">
<ul>
  <li>È un primo test su un universo selezionato oggi: contiene survivorship bias e non è ancora una validazione definitiva.</li>
  <li>I prezzi Yahoo sono adeguati per ricerca esplorativa, non costituiscono un feed di esecuzione.</li>
  <li>La conversione USD/EUR usa il cambio di chiusura giornaliero; per l'apertura viene usato il cambio noto del giorno precedente.</li>
  <li>Non sono ancora inclusi calendario utili, revisioni degli analisti o notizie; saranno il prossimo filtro ortogonale.</li>
  <li>Imposte escluse. Gli ordini sono frazionari e il test non modella limiti specifici di disponibilità su Trade Republic.</li>
  <li>Il risultato non è una previsione né una raccomandazione su singoli titoli.</li>
</ul></div>

<h2>Download non riusciti</h2>
{failures_html}
<h2>Riferimenti</h2>
<ul>
  <li><a href="https://repub.eur.nl/pub/22252/ResidualMomentum-2011.pdf">Blitz, Huij e Martens (2011), Residual Momentum</a>.</li>
  <li><a href="https://escholarship.org/uc/item/4w1110bb">Bailey et al., The Probability of Backtest Overfitting</a>.</li>
  <li><a href="https://traderepublic.com/en-it?openModal=pricing-scheme">Trade Republic, schema prezzi italiano</a>.</li>
</ul>
<p class="small">Generato da equity_long_backtest.py. Tutti i parametri sono visibili e modificabili nel codice.</p>
</main></body></html>"""
    (output_dir / "report.html").write_text(html_text, encoding="utf-8")


def main() -> None:
    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    cache_dir = (base_dir / args.cache_dir).resolve()
    output_dir = (base_dir / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cfg = StrategyConfig()

    symbols = sorted(set(TICKER_TO_SECTOR) | set(SECTOR_UNIVERSE) | {BENCHMARK, FX_TICKER})
    data, failures = download_universe(
        symbols, args.data_start, args.end, cache_dir, args.refresh, args.workers
    )
    available_stocks = [ticker for ticker in TICKER_TO_SECTOR if ticker in data]
    if len(available_stocks) < 30:
        raise RuntimeError(f"Only {len(available_stocks)} stocks downloaded; refusing an under-sized test")
    print(f"Building features for {len(available_stocks)} stocks ...", flush=True)
    features = build_features(data, cfg)

    capital = args.capital
    main_net = simulate_strategy(
        features, features.score, features.rank, features.eligible, cfg, args.test_start, capital, "Residual Long net"
    )
    gross_cfg = dataclasses.replace(cfg, fixed_fee_eur=0.0, slippage_bps_per_side=0.0)
    main_gross = simulate_strategy(
        features, features.score, features.rank, features.eligible, gross_cfg, args.test_start, capital, "Residual Long gross"
    )
    simple_net = simulate_strategy(
        features,
        features.simple_score,
        features.simple_rank,
        features.simple_eligible,
        cfg,
        args.test_start,
        capital,
        "Simple momentum net",
    )

    fx_close = data[FX_TICKER]["Close"].reindex(data[BENCHMARK].index).ffill().bfill()
    spy_close_eur = data[BENCHMARK]["Close"].reindex(fx_close.index).div(fx_close)
    spy_open_eur = data[BENCHMARK]["Open"].reindex(fx_close.index).div(fx_close.shift(1).bfill())
    spy = buy_and_hold(
        spy_open_eur,
        spy_close_eur,
        args.test_start,
        capital,
        cfg.fixed_fee_eur,
        cfg.slippage_bps_per_side,
    )

    curves = {
        "Residual Long net": main_net.equity,
        "Residual Long gross": main_gross.equity,
        "Simple momentum net": simple_net.equity,
        "SPY buy & hold": spy,
    }
    metric_rows = []
    result_lookup = {
        "Residual Long net": main_net,
        "Residual Long gross": main_gross,
        "Simple momentum net": simple_net,
    }
    for name, curve in curves.items():
        result = result_lookup.get(name)
        row = {"strategy": name}
        row.update(performance_metrics(curve, result.exposure if result else None, result.closed_trades if result else None))
        if result:
            row["fees_eur"] = result.total_fees
            row["estimated_slippage_eur"] = result.total_slippage_estimate
        metric_rows.append(row)
    metrics = pd.DataFrame(metric_rows)

    periods = [
        ("Intero periodo", args.test_start, None),
        ("Validazione 2019-2022", "2019-01-01", "2022-12-31"),
        ("Holdout 2023+", "2023-01-01", None),
    ]
    period_rows = []
    for label, start, end in periods:
        for name, curve in curves.items():
            result = result_lookup.get(name)
            row = {"period": label, "strategy": name}
            row.update(
                slice_metrics(
                    curve,
                    start,
                    end,
                    result.exposure if result else None,
                    result.closed_trades if result else None,
                )
            )
            period_rows.append(row)
    period_metrics = pd.DataFrame(period_rows)

    sensitivity_rows = []
    sensitivity_results: Dict[float, SimulationResult] = {}
    for amount in (2_000.0, 5_000.0, 10_000.0, 25_000.0):
        result = simulate_strategy(
            features,
            features.score,
            features.rank,
            features.eligible,
            cfg,
            args.test_start,
            amount,
            f"Residual Long net €{amount:,.0f}",
        )
        sensitivity_results[amount] = result
        row = {"initial_capital_eur": amount, "fees_eur": result.total_fees, "estimated_slippage_eur": result.total_slippage_estimate}
        row.update(performance_metrics(result.equity, result.exposure, result.closed_trades))
        sensitivity_rows.append(row)
    cost_sensitivity = pd.DataFrame(sensitivity_rows)

    latest = latest_ranking(features)
    create_plots(curves, main_net, output_dir)

    combined_curves = pd.concat(curves.values(), axis=1)
    combined_curves.to_csv(output_dir / "equity_curves.csv", index_label="Date")
    metrics.to_csv(output_dir / "metrics.csv", index=False)
    period_metrics.to_csv(output_dir / "period_metrics.csv", index=False)
    cost_sensitivity.to_csv(output_dir / "cost_sensitivity.csv", index=False)
    latest.to_csv(output_dir / "latest_ranking.csv", index=False)
    main_net.orders.to_csv(output_dir / "orders.csv", index=False)
    main_net.closed_trades.to_csv(output_dir / "closed_trades.csv", index=False)
    pd.DataFrame(
        {
            "date": features.risk_on.index,
            "risk_on": features.risk_on.astype(bool).values,
        }
    ).to_csv(output_dir / "market_regime.csv", index=False)

    run_manifest = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "data_start": args.data_start,
        "test_start": args.test_start,
        "requested_end": args.end,
        "stocks_requested": len(TICKER_TO_SECTOR),
        "stocks_available": len(available_stocks),
        "failed_symbols": failures,
        "configuration": dataclasses.asdict(cfg),
        "data_source": "Yahoo Finance chart endpoint (exploratory research)",
    }
    (output_dir / "run_manifest.json").write_text(json.dumps(run_manifest, indent=2), encoding="utf-8")
    build_html_report(
        output_dir,
        cfg,
        metrics,
        period_metrics,
        cost_sensitivity,
        latest,
        failures,
        args.data_start,
        args.test_start,
        args.end,
    )

    print("\nMain metrics:")
    print(metrics[[c for c in ["strategy", "cagr_pct", "max_drawdown_pct", "sharpe_rf0", "closed_trades", "fees_eur"] if c in metrics]].to_string(index=False))
    print(f"\nReport: {output_dir / 'report.html'}")


if __name__ == "__main__":
    main()
