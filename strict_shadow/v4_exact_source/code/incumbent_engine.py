#!/usr/bin/env python3
"""Reproduce Atlas Strict with the v1.1 correctness fixes.

The frozen v1.0 artifacts are inputs/reference only and are never overwritten.
V1.1 makes the allocation decision date explicit, prevents the bull floor from
using stale state during risk-only rebalances, preserves a real 1% pre-buy cash
reserve, and applies a fundamental entry veto to the final correlation pool.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import pickle
import platform
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize


ENGINE_ID = "ATLAS_STRICT_CORRECTED_V1_1_RESEARCH_2026-09-19"
BASELINE = "ATLAS_STRICT_V1_1_CORRECTED"
BALANCE = "ATLAS_STRICT_V1_1_BALANCE_TRUE_VETO"
FROZEN = "ATLAS_STRICT_V1_0_FROZEN_REFERENCE"
SPY = "SPY_BH_SAME_ACCOUNTING"
CAPITAL = 100_000.0
TARGET_VOL = 0.18
BULL_FLOOR = 0.80
BULL_THRESHOLD = 0.75
CORE_BAND = 0.05
HARD_EXIT_RANK = 40
STRENGTH_BETA = 0.35
MAX_EXPOSURE = 0.99
SLIPPAGE_BPS = 10.0
CASH_RESERVE = 0.01
EXPECTED_CANONICAL_BUNDLE_SHA256 = (
    "fb1321e534d21059fc4f151e8027071106cc1899ef8b830a249ab61d6d8be2d3"
)
EXPECTED_BALANCE_SCORE_SHA256 = (
    "66f2004df52d3894857af3ebf81841ceab0600d0671c2ce83dde9ee6f2c00932"
)

PERIODS = (
    ("2012-2016", "2012-01-03", "2016-12-31"),
    ("2017-2021", "2017-01-01", "2021-12-31"),
    ("2022-2025", "2022-01-01", "2025-12-10"),
    ("2019-2025", "2019-01-01", "2025-12-10"),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def paired_block_bootstrap(
    base_equity: pd.Series,
    variant_equity: pd.Series,
    block: int = 63,
    samples: int = 3_000,
    seed: int = 20260919,
) -> dict:
    aligned = pd.concat(
        [
            base_equity.pct_change(fill_method=None).rename("base"),
            variant_equity.pct_change(fill_method=None).rename("variant"),
        ],
        axis=1,
    ).dropna()
    values = aligned.to_numpy(float)
    count = len(values)
    rng = np.random.default_rng(seed)
    excess = np.empty(samples, dtype=float)
    max_start = max(1, count - block + 1)
    blocks_needed = math.ceil(count / block)
    for sample in range(samples):
        starts = rng.integers(0, max_start, size=blocks_needed)
        indices = np.concatenate(
            [np.arange(start, min(start + block, count)) for start in starts]
        )[:count]
        draw = values[indices]
        base_growth = np.prod(1.0 + draw[:, 0])
        variant_growth = np.prod(1.0 + draw[:, 1])
        base_cagr = base_growth ** (252.0 / count) - 1.0
        variant_cagr = variant_growth ** (252.0 / count) - 1.0
        excess[sample] = 100.0 * (variant_cagr - base_cagr)
    return {
        "block_sessions": block,
        "samples": samples,
        "seed": seed,
        "median_excess_pp_per_year": float(np.median(excess)),
        "ci2_5_pp_per_year": float(np.quantile(excess, 0.025)),
        "ci97_5_pp_per_year": float(np.quantile(excess, 0.975)),
        "p_excess_gt0_pct": float(100.0 * np.mean(excess > 0.0)),
    }


def subperiod_rows(name: str, equity: pd.Series) -> list[dict]:
    rows: list[dict] = []
    for label, start, end in PERIODS:
        period = equity.loc[start:end].dropna()
        if len(period) < 2:
            continue
        years = max((period.index[-1] - period.index[0]).days / 365.25, 1 / 365.25)
        cagr = (period.iloc[-1] / period.iloc[0]) ** (1.0 / years) - 1.0
        max_drawdown = float((period / period.cummax() - 1.0).min())
        rows.append(
            {
                "strategy": name,
                "period": label,
                "cagr_pct": 100.0 * cagr,
                "max_drawdown_pct": 100.0 * max_drawdown,
            }
        )
    return rows


def annual_rows(name: str, equity: pd.Series, initial_capital: float) -> list[dict]:
    rows: list[dict] = []
    previous = initial_capital
    for year, group in equity.dropna().groupby(equity.dropna().index.year):
        ending = float(group.iloc[-1])
        rows.append({"strategy": name, "year": int(year), "return_pct": 100.0 * (ending / previous - 1.0)})
        previous = ending
    return rows


class CorrectedAtlas:
    def __init__(self, root: Path):
        self.root = root
        engine_dir = root / "code" / "engine"
        sys.path.insert(0, str(engine_dir.resolve()))
        import equity_long_backtest as v1
        import equity_long_backtest_v2 as v2
        import equity_long_bear_tax_comparison as t9
        import equity_long_v4_research as v4

        self.v1 = v1
        self.v2 = v2
        self.t9 = t9
        self.v4 = v4
        bundle_path = root / "inputs" / "core" / "H4_CANONICAL_R1_INPUT_BUNDLE.pkl.gz"
        bundle_hash = sha256(bundle_path)
        if bundle_hash != EXPECTED_CANONICAL_BUNDLE_SHA256:
            raise RuntimeError(
                "Canonical input failed SHA-256 verification: "
                f"expected {EXPECTED_CANONICAL_BUNDLE_SHA256}, got {bundle_hash}"
            )
        with gzip.open(bundle_path, "rb") as handle:
            self.bundle = pickle.load(handle)
        self.v1.TICKER_TO_SECTOR.update(self.bundle["ticker_to_sector"])
        self.features = self.bundle["features"]
        self.base_score = self.bundle["score"]
        self.eligible = self.bundle["eligible"]
        self.membership = self.bundle["membership"]
        self.base_regime = self.bundle["regime"]
        self.cash_rate = self.bundle["cash_rate"]
        self.raw = self.bundle["raw_market"]
        self.base_t8 = self.bundle["t8_overlay"]
        self.bear_floor = self.bundle["bear_floor"]
        self.tax = self.t9.TaxConfig("italian_tax", True)
        self.net_cash = self.t9.net_cash_rate(self.cash_rate, self.tax)
        self._build_path_score()
        self._build_panic_rebound()
        self._returns = self.features.close_eur.pct_change(fill_method=None)
        self._corr_cache: dict[pd.Timestamp, pd.DataFrame] = {}
        self.expected_alpha, _ = self.v4.build_expected_alpha(
            self.path_score,
            self.features.close_eur,
            self.bundle["start"],
        )

    def _build_path_score(self) -> None:
        log_price = np.log(self.features.close_eur.where(self.features.close_eur > 0.0))
        log_return = log_price.diff()
        absolute_return = log_return.abs()
        window = 231
        numerator = (log_price.shift(21) - log_price.shift(252)).abs()
        denominator = absolute_return.shift(21).rolling(window, min_periods=180).sum()
        efficiency = (numerator / denominator.replace(0.0, np.nan)).clip(0.0, 1.0)
        maximum_absolute = absolute_return.shift(21).rolling(window, min_periods=180).max()
        jump_share = (maximum_absolute / denominator.replace(0.0, np.nan)).clip(0.0, 1.0)
        path_quality = (
            efficiency.rank(axis=1, pct=True) - jump_share.rank(axis=1, pct=True)
        ).clip(-1.0, 1.0)
        self.path_score = (self.base_score + 0.20 * path_quality).clip(-3.0, 3.0)

    def _build_panic_rebound(self) -> None:
        spy = self.raw.close_eur[self.v1.BENCHMARK]
        return20 = spy.pct_change(20)
        return10 = spy.pct_change(10)
        drawdown63 = spy / spy.rolling(63, min_periods=42).max() - 1.0
        sma50 = self.features.close_eur.rolling(50, min_periods=40).mean()
        breadth = (self.features.close_eur > sma50).mean(axis=1)
        breadth_change10 = breadth - breadth.shift(10)
        self.panic = (
            (drawdown63 <= -0.10) & (return20 <= -0.05) & (breadth < 0.35)
        ).fillna(False)
        recent_panic = (
            self.panic.shift(1).rolling(42, min_periods=1).max().fillna(0).astype(bool)
        )
        self.rebound = (
            recent_panic
            & (return10 >= 0.05)
            & (breadth >= 0.50)
            & (breadth_change10 >= 0.15)
        ).fillna(False)
        self.pr_t8 = self.base_t8.copy()
        self.pr_t8.loc[self.panic] = np.minimum(self.pr_t8.loc[self.panic], 0.50)
        self.pr_regime = self.base_regime.copy()
        self.pr_regime.loc[self.rebound] = np.maximum(self.pr_regime.loc[self.rebound], 0.65)
        self.pr_t8.loc[self.rebound] = np.maximum(self.pr_t8.loc[self.rebound], 0.90)

    def corr_matrix(self, date: pd.Timestamp, lookback: int = 126) -> pd.DataFrame:
        if date not in self._corr_cache:
            location = self._returns.index.get_loc(date)
            start = max(0, location - lookback + 1)
            self._corr_cache[date] = self._returns.iloc[start : location + 1].corr(min_periods=63)
        return self._corr_cache[date]

    def correlations(
        self,
        date: pd.Timestamp,
        chosen: list[str],
        candidates: list[str],
    ) -> dict[str, float]:
        if not chosen or not candidates:
            return {ticker: 0.0 for ticker in candidates}
        matrix = self.corr_matrix(date)
        result: dict[str, float] = {}
        for candidate in candidates:
            values: list[float] = []
            if candidate in matrix.index:
                for held in chosen:
                    if held in matrix.columns:
                        value = matrix.at[candidate, held]
                        if np.isfinite(value):
                            values.append(float(value))
            result[candidate] = max([0.0, *values])
        return result

    def callbacks(self, fundamental_score: pd.DataFrame | None):
        base_gate = self.v4.gate_choose_factory(
            self.expected_alpha,
            self.features.close_eur,
            True,
            self.v4.GateSpec(True, HARD_EXIT_RANK, 0.010, 0.25, 0.20),
        )
        strength: dict[str, float] = {}
        allocator_audit: list[dict] = []
        gate_stats = {
            "blocked_entry_checks": 0,
            "dates_with_gate": 0,
            "unique_blocked_tickers": set(),
            "post_correlation_veto_violations": 0,
        }

        def choose(date, score_row, rank_row, eligible_row, positions, cfg):
            effective_eligible = eligible_row.astype(bool).copy()
            blocked: list[str] = []
            if fundamental_score is not None and date in fundamental_score.index:
                known = (
                    effective_eligible
                    & self.membership.loc[date].astype(bool)
                    & fundamental_score.loc[date].notna()
                )
                if int(known.sum()) >= 10:
                    percentile = fundamental_score.loc[date, known].rank(method="average", pct=True)
                    blocked = percentile.index[percentile <= 0.10].tolist()
                if blocked:
                    gate_stats["dates_with_gate"] += 1
                    held = set(positions)
                    for ticker in blocked:
                        if ticker not in held and bool(effective_eligible.get(ticker, False)):
                            effective_eligible.loc[ticker] = False
                            gate_stats["blocked_entry_checks"] += 1
                            gate_stats["unique_blocked_tickers"].add(ticker)

            names = list(
                base_gate(date, score_row, rank_row, effective_eligible, positions, cfg)
            )
            if names:
                incumbents = [ticker for ticker in names if ticker in positions]
                initial_newcomers = [ticker for ticker in names if ticker not in positions]
                open_slots = len(names) - len(incumbents)
                if open_slots > 0:
                    # V1.1 fix: the correlation pool uses the already-gated row.
                    ranked = (
                        score_row.where(effective_eligible)
                        .dropna()
                        .sort_values(ascending=False)
                    )
                    alpha_row = self.expected_alpha.loc[date]
                    alpha_values = [
                        float(alpha_row.get(ticker, np.nan))
                        for ticker in initial_newcomers
                    ]
                    alpha_values = [value for value in alpha_values if np.isfinite(value)]
                    alpha_floor = min(alpha_values) if alpha_values else -np.inf
                    chosen = list(incumbents)
                    sector_counts: dict[str, int] = {}
                    for ticker in chosen:
                        sector = self.v1.TICKER_TO_SECTOR[ticker]
                        sector_counts[sector] = sector_counts.get(sector, 0) + 1
                    pool = [
                        ticker
                        for ticker in ranked.index[:40]
                        if ticker not in chosen
                        and float(alpha_row.get(ticker, -np.inf)) >= alpha_floor - 1e-12
                    ]
                    while len(chosen) < len(names) and pool:
                        correlations = self.correlations(date, chosen, pool)
                        best = None
                        best_value = -np.inf
                        for ticker in pool:
                            sector = self.v1.TICKER_TO_SECTOR[ticker]
                            if sector_counts.get(sector, 0) >= cfg.max_names_per_sector:
                                continue
                            correlation = correlations.get(ticker, 0.0)
                            penalty = 0.50 * max(0.0, correlation - 0.70) / 0.30
                            value = float(score_row.get(ticker, -np.inf)) - penalty
                            if value > best_value:
                                best_value = value
                                best = ticker
                        if best is None:
                            break
                        chosen.append(best)
                        pool.remove(best)
                        sector = self.v1.TICKER_TO_SECTOR[best]
                        sector_counts[sector] = sector_counts.get(sector, 0) + 1
                    if len(chosen) == len(names):
                        names = chosen

            invalid_new = [
                ticker
                for ticker in names
                if ticker not in positions and not bool(effective_eligible.get(ticker, False))
            ]
            if invalid_new:
                gate_stats["post_correlation_veto_violations"] += len(invalid_new)
                raise AssertionError(f"Final entry veto failed on {date.date()}: {invalid_new}")

            strength.clear()
            if names:
                values = pd.Series(
                    {ticker: float(score_row.get(ticker, np.nan)) for ticker in names}
                ).replace([np.inf, -np.inf], np.nan)
                percentile = values.rank(method="average", pct=True)
                if len(percentile) > 1 and percentile.max() > percentile.min():
                    percentile = (percentile - percentile.min()) / (
                        percentile.max() - percentile.min()
                    )
                else:
                    percentile = percentile * 0.0 + 0.5
                strength.update({ticker: float(percentile[ticker]) for ticker in names})
            return names

        def weights(relative, exposure, cfg, decision_date=None):
            if decision_date is None:
                raise AssertionError("V1.1 allocator requires the current decision_date")
            tickers = relative.index.tolist()
            if not tickers or exposure <= 0.0:
                return pd.Series(dtype=float)
            # V1.1 fix: current effective Panic/Rebound state, not a stale full-selection date.
            effective_state = float(self.pr_regime.loc[decision_date]) * float(
                self.pr_t8.loc[decision_date]
            )
            desired = float(exposure)
            floor_applied = bool(
                BULL_FLOOR > 0.0
                and effective_state >= BULL_THRESHOLD
                and desired < BULL_FLOOR
            )
            if floor_applied:
                desired = BULL_FLOOR
            desired = min(desired, MAX_EXPOSURE)

            strength_values = np.array([strength.get(ticker, 0.5) for ticker in tickers])
            relative_values = relative.to_numpy(float) * np.exp(
                STRENGTH_BETA * (2.0 * strength_values - 1.0)
            )
            relative_values = (
                relative_values / relative_values.sum()
                if relative_values.sum() > 0.0
                else np.full(len(relative_values), 1.0 / len(relative_values))
            )
            caps = np.full(len(tickers), cfg.maximum_position_weight)
            sectors = sorted({self.v1.TICKER_TO_SECTOR[ticker] for ticker in tickers})
            maximum_sector_total = sum(
                min(
                    cfg.maximum_sector_weight,
                    float(
                        caps[
                            [
                                index
                                for index, ticker in enumerate(tickers)
                                if self.v1.TICKER_TO_SECTOR[ticker] == sector
                            ]
                        ].sum()
                    ),
                )
                for sector in sectors
            )
            feasible = min(
                desired,
                float(caps.sum()),
                float(maximum_sector_total),
                MAX_EXPOSURE,
            )
            desired_weights = relative_values * feasible
            constraints = [
                {"type": "eq", "fun": lambda values: float(values.sum() - feasible)}
            ]
            for sector in sectors:
                locations = np.array(
                    [
                        index
                        for index, ticker in enumerate(tickers)
                        if self.v1.TICKER_TO_SECTOR[ticker] == sector
                    ]
                )
                constraints.append(
                    {
                        "type": "ineq",
                        "fun": lambda values, locations=locations: float(
                            cfg.maximum_sector_weight - values[locations].sum()
                        ),
                    }
                )
            initial = np.minimum(desired_weights, caps)
            for _ in range(20):
                gap = feasible - initial.sum()
                room = np.maximum(0.0, caps - initial)
                if abs(gap) < 1e-10:
                    break
                if gap > 0.0 and room.sum() > 0.0:
                    initial += np.minimum(room, gap * room / room.sum())
                elif gap < 0.0 and initial.sum() > 0.0:
                    initial *= feasible / initial.sum()
            optimization = minimize(
                lambda values: float(np.square(values - desired_weights).sum()),
                x0=initial,
                method="SLSQP",
                bounds=[(0.0, float(cap)) for cap in caps],
                constraints=constraints,
                options={"maxiter": 250, "ftol": 1e-12},
            )
            if not optimization.success:
                raise RuntimeError(
                    f"Allocation failed on {decision_date.date()}: {optimization.message}"
                )
            result = pd.Series(optimization.x, index=tickers).clip(lower=0.0)
            if float(result.max()) > cfg.maximum_position_weight + 1e-7:
                raise AssertionError("Single-name cap violated")
            for sector in sectors:
                sector_weight = sum(
                    float(result[ticker])
                    for ticker in tickers
                    if self.v1.TICKER_TO_SECTOR[ticker] == sector
                )
                if sector_weight > cfg.maximum_sector_weight + 1e-7:
                    raise AssertionError("Sector cap violated")
            if float(result.sum()) > MAX_EXPOSURE + 1e-7:
                raise AssertionError("Maximum exposure violated")
            if effective_state < BULL_THRESHOLD and float(result.sum()) > float(exposure) + 1e-7:
                raise AssertionError(
                    "Risk-off allocation was increased above the simulator request"
                )
            allocator_audit.append(
                {
                    "decision_date": decision_date,
                    "effective_state": effective_state,
                    "simulator_requested_exposure": float(exposure),
                    "allocator_target_exposure": float(result.sum()),
                    "bull_floor_applied": floor_applied,
                    "risk_off_nonincrease_pass": bool(
                        effective_state >= BULL_THRESHOLD
                        or float(result.sum()) <= float(exposure) + 1e-7
                    ),
                }
            )
            return result

        return choose, weights, gate_stats, allocator_audit

    def run_variant(self, name: str, fundamental_score: pd.DataFrame | None):
        cfg = replace(
            self.bundle["cfg"],
            target_volatility=TARGET_VOL,
            max_positions=10,
            default_slippage_bps_per_side=SLIPPAGE_BPS,
        )
        selector, weight_builder, gate_stats, allocator_audit = self.callbacks(
            fundamental_score
        )
        policy = self.t9.ExecutionPolicy(
            name=f"corrected_coreband{CORE_BAND}",
            core_risk_band_weight=CORE_BAND,
        )
        result = self.t9.simulate_tax_aware(
            self.features,
            self.path_score,
            self.eligible,
            self.membership,
            self.pr_regime,
            self.cash_rate,
            None,
            cfg,
            self.raw,
            self.bundle["start"],
            CAPITAL,
            name,
            self.tax,
            weekly_risk_overlay=self.pr_t8,
            bear_floor=self.bear_floor,
            execution_policy=policy,
            cash_reserve_fraction=CASH_RESERVE,
            name_selector=selector,
            target_weight_builder=weight_builder,
        )
        metrics = self.v4.metric_row(result, self.net_cash, CAPITAL)
        metrics["calmar"] = metrics["cagr_pct"] / abs(metrics["max_drawdown_pct"])
        metrics.update(
            {
                "engine_id": ENGINE_ID,
                "name": name,
                "target_vol": TARGET_VOL,
                "bull_floor": BULL_FLOOR,
                "bull_threshold": BULL_THRESHOLD,
                "core_band": CORE_BAND,
                "hard_exit_rank": HARD_EXIT_RANK,
                "strength_beta": STRENGTH_BETA,
                "max_exposure": MAX_EXPOSURE,
                "cash_reserve": CASH_RESERVE,
                "slippage_bps": SLIPPAGE_BPS,
            }
        )
        gate_stats = {
            **gate_stats,
            "unique_blocked_tickers": sorted(gate_stats["unique_blocked_tickers"]),
        }
        return result, metrics, gate_stats, pd.DataFrame(allocator_audit)


def save_result(root: Path, name: str, result, metrics: dict, gate_stats: dict, audit: pd.DataFrame) -> None:
    run_dir = root / "results" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    result.equity.rename("equity").to_csv(run_dir / "equity.csv", index_label="Date")
    result.exposure.rename("exposure").to_csv(run_dir / "exposure.csv", index_label="Date")
    result.orders.to_csv(run_dir / "orders.csv", index=False)
    result.allocations.to_csv(run_dir / "allocations.csv", index=False)
    result.realized_lots.to_csv(run_dir / "realized_lots.csv", index=False)
    result.tax_ledger.to_csv(run_dir / "tax_ledger.csv", index=False)
    audit.to_csv(run_dir / "allocator_audit.csv", index=False)
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    (run_dir / "gate_stats.json").write_text(json.dumps(gate_stats, indent=2), encoding="utf-8")


def exposure_audit(result) -> dict:
    target_totals = result.allocations.groupby(
        ["decision_date", "execution_date", "decision_type"],
        dropna=False,
    )["target_weight"].sum()
    maximum_target = float(target_totals.max()) if len(target_totals) else 0.0
    return {
        "max_declared_target_exposure": maximum_target,
        "declared_target_cap": MAX_EXPOSURE,
        "declared_target_cap_pass": bool(maximum_target <= MAX_EXPOSURE + 1e-8),
        "max_realized_daily_exposure": float(result.exposure.max()),
        "realized_exposure_note": (
            "Daily realized exposure may drift slightly above the 99% decision target "
            "between rebalances; buys preserve the separate 1% cash reserve."
        ),
    }


def frozen_bug_audit(model: CorrectedAtlas, root: Path) -> dict:
    allocations = pd.read_csv(
        root / "reference" / "frozen_v1_0_allocations.csv",
        parse_dates=["decision_date", "execution_date"],
    )
    unique = allocations.drop_duplicates(
        ["decision_date", "execution_date", "decision_type"]
    ).copy()
    unique["current_effective_state"] = [
        float(model.pr_regime.loc[date]) * float(model.pr_t8.loc[date])
        for date in unique["decision_date"]
    ]
    unique["floor_lift"] = (
        unique["core_target_exposure"] - unique["desired_exposure"]
    )
    invalid = unique[
        (unique["floor_lift"] > 1e-8)
        & (unique["current_effective_state"] < BULL_THRESHOLD)
    ].copy()
    invalid.to_csv(root / "results" / "frozen_stale_state_events.csv", index=False)
    return {
        "invalid_floor_events": int(len(invalid)),
        "max_invalid_floor_lift_pct": 100.0 * float(invalid["floor_lift"].max())
        if len(invalid)
        else 0.0,
        "first_invalid_event": invalid["decision_date"].min().date().isoformat()
        if len(invalid)
        else None,
        "last_invalid_event": invalid["decision_date"].max().date().isoformat()
        if len(invalid)
        else None,
        "all_corrected_risk_off_nonincrease": True,
    }


def write_manifest(root: Path) -> None:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in {"MANIFEST_SHA256.csv", "PACKAGE_SHA256.txt"}:
            rows.append(
                {
                    "file": str(path.relative_to(root)),
                    "bytes": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )
    pd.DataFrame(rows).to_csv(root / "MANIFEST_SHA256.csv", index=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Package root; defaults to the directory containing code/ and inputs/.",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    (root / "results").mkdir(parents=True, exist_ok=True)

    print("1/4 LOAD CANONICAL PIT INPUTS", flush=True)
    model = CorrectedAtlas(root)
    balance_path = root / "inputs" / "core" / "balance_sheet_score_daily.pkl.gz"
    balance_hash = sha256(balance_path)
    if balance_hash != EXPECTED_BALANCE_SCORE_SHA256:
        raise RuntimeError(
            "Balance-score input failed SHA-256 verification: "
            f"expected {EXPECTED_BALANCE_SCORE_SHA256}, got {balance_hash}"
        )
    balance_score = pd.read_pickle(
        balance_path,
        compression="gzip",
    ).reindex(index=model.base_score.index, columns=model.base_score.columns)

    print("2/4 RUN CORRECTED BASELINE", flush=True)
    baseline_result, baseline_metrics, baseline_gate, baseline_audit = model.run_variant(
        BASELINE, None
    )
    save_result(
        root,
        BASELINE,
        baseline_result,
        baseline_metrics,
        baseline_gate,
        baseline_audit,
    )
    print(
        f"{BASELINE}: CAGR={baseline_metrics['cagr_pct']:.6f}% "
        f"DD={baseline_metrics['max_drawdown_pct']:.6f}% "
        f"Sharpe={baseline_metrics['sharpe_excess_cash']:.6f}",
        flush=True,
    )

    print("3/4 RUN BALANCE-SHEET TRUE-VETO CHALLENGER", flush=True)
    balance_result, balance_metrics, balance_gate, balance_audit = model.run_variant(
        BALANCE, balance_score
    )
    save_result(
        root,
        BALANCE,
        balance_result,
        balance_metrics,
        balance_gate,
        balance_audit,
    )
    print(
        f"{BALANCE}: CAGR={balance_metrics['cagr_pct']:.6f}% "
        f"DD={balance_metrics['max_drawdown_pct']:.6f}% "
        f"Sharpe={balance_metrics['sharpe_excess_cash']:.6f}",
        flush=True,
    )

    print("4/4 VALIDATE, COMPARE AND HASH", flush=True)
    frozen_metrics = json.loads(
        (root / "reference" / "frozen_v1_0_metrics.json").read_text(encoding="utf-8")
    )
    frozen_equity = pd.read_csv(
        root / "reference" / "frozen_v1_0_equity.csv",
        index_col=0,
        parse_dates=True,
    ).iloc[:, 0]
    spy_equity, spy_tax = model.v4.spy_buy_hold(
        model.raw,
        model.cash_rate,
        model.tax,
        model.bundle["start"],
        CAPITAL,
        fee=1.0,
        slip=SLIPPAGE_BPS,
    )
    spy_metrics = model.v2.metrics(
        spy_equity,
        model.net_cash,
        CAPITAL,
        None,
        declared_trials=model.v4.TRIAL_COUNT,
    )
    spy_metrics.update(
        {
            "name": SPY,
            "execution_cost_eur": float(spy_tax["fees_eur"] + spy_tax["slippage_eur"]),
            "total_tax_eur": float(spy_tax["total_tax_eur"]),
            "orders": int(spy_tax["orders"]),
        }
    )

    frozen_row = {**frozen_metrics, "name": FROZEN}
    summary = pd.DataFrame(
        [frozen_row, baseline_metrics, balance_metrics, spy_metrics]
    )
    summary["delta_cagr_pp_vs_corrected"] = summary["cagr_pct"] - baseline_metrics["cagr_pct"]
    summary["delta_maxdd_pp_vs_corrected"] = (
        summary["max_drawdown_pct"] - baseline_metrics["max_drawdown_pct"]
    )
    summary.to_csv(root / "results" / "comparison.csv", index=False)

    subperiods = []
    annual = []
    for name, curve in (
        (FROZEN, frozen_equity),
        (BASELINE, baseline_result.equity),
        (BALANCE, balance_result.equity),
        (SPY, spy_equity),
    ):
        subperiods.extend(subperiod_rows(name, curve))
        annual.extend(annual_rows(name, curve, CAPITAL))
    pd.DataFrame(subperiods).to_csv(root / "results" / "subperiods.csv", index=False)
    pd.DataFrame(annual).to_csv(root / "results" / "annual_returns.csv", index=False)

    bootstrap = {
        "balance_vs_corrected": paired_block_bootstrap(
            baseline_result.equity, balance_result.equity
        ),
        "corrected_vs_frozen": paired_block_bootstrap(
            frozen_equity, baseline_result.equity
        ),
    }
    (root / "results" / "paired_bootstrap_63d.json").write_text(
        json.dumps(bootstrap, indent=2), encoding="utf-8"
    )

    bug_audit = frozen_bug_audit(model, root)
    corrected_audit = {
        BASELINE: {
            "allocator_calls": int(len(baseline_audit)),
            "risk_off_nonincrease_pass": bool(
                baseline_audit["risk_off_nonincrease_pass"].all()
            ),
            "post_correlation_veto_violations": int(
                baseline_gate["post_correlation_veto_violations"]
            ),
            **exposure_audit(baseline_result),
        },
        BALANCE: {
            "allocator_calls": int(len(balance_audit)),
            "risk_off_nonincrease_pass": bool(
                balance_audit["risk_off_nonincrease_pass"].all()
            ),
            "post_correlation_veto_violations": int(
                balance_gate["post_correlation_veto_violations"]
            ),
            **exposure_audit(balance_result),
        },
    }
    replay = {
        "engine_id": ENGINE_ID,
        "status": "PASS"
        if all(
            row["risk_off_nonincrease_pass"]
            and row["post_correlation_veto_violations"] == 0
            and row["declared_target_cap_pass"]
            for row in corrected_audit.values()
        )
        else "FAIL",
        "frozen_bug_audit": bug_audit,
        "corrected_invariants": corrected_audit,
        "inputs": {
            "canonical_bundle_sha256": sha256(
                root / "inputs" / "core" / "H4_CANONICAL_R1_INPUT_BUNDLE.pkl.gz"
            ),
            "balance_score_sha256": sha256(
                root / "inputs" / "core" / "balance_sheet_score_daily.pkl.gz"
            ),
        },
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
        },
    }
    (root / "REPLAY_CHECK.json").write_text(
        json.dumps(replay, indent=2), encoding="utf-8"
    )
    if replay["status"] != "PASS":
        raise SystemExit("Corrected-engine invariants failed")

    write_manifest(root)
    print(json.dumps({"baseline": baseline_metrics, "balance": balance_metrics, "replay": replay}, indent=2))


if __name__ == "__main__":
    main()
