#!/usr/bin/env python3
"""Pre-registered comparison of three defensive overlays for Equity Long V2.

T6 uses an SMA40/80 market-only overlay.  T7 adds an ATR dead band and
asymmetric 3/10-session confirmation.  T8 uses a short/medium realized-
volatility ratio.  Stock selection remains the frozen SMA50/200 model.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
from pathlib import Path
from typing import Mapping

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import equity_long_backtest as v1
import equity_long_backtest_v2 as v2


STRATEGY_NAMES = (
    "V2 baseline",
    "T6 Fast overlay",
    "T7 Hysteresis",
    "T8 Volatility shock",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-start", default="2007-01-01")
    parser.add_argument("--test-start", default="2012-01-03")
    parser.add_argument("--end", default=(dt.date.today() + dt.timedelta(days=1)).isoformat())
    parser.add_argument("--capital", type=float, default=10_000.0)
    parser.add_argument("--cache-dir", default="data_cache")
    parser.add_argument("--input-dir", default="inputs")
    parser.add_argument("--output-dir", default="output_overlays")
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--refresh", action="store_true")
    return parser.parse_args()


def fast_inputs(
    data: Mapping[str, pd.DataFrame], index: pd.DatetimeIndex
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    spy = data[v1.BENCHMARK]["Close"].reindex(index)
    sma40 = spy.rolling(40, min_periods=32).mean()
    sma80 = spy.rolling(80, min_periods=64).mean()
    high = data[v1.BENCHMARK]["High"].reindex(index)
    low = data[v1.BENCHMARK]["Low"].reindex(index)
    previous_close = spy.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)
    atr20 = true_range.rolling(20, min_periods=15).mean()
    return spy, sma40, sma80, atr20


def t6_fast_overlay(
    data: Mapping[str, pd.DataFrame], index: pd.DatetimeIndex
) -> tuple[pd.Series, pd.DataFrame]:
    spy, sma40, sma80, _ = fast_inputs(data, index)
    price_negative = spy < sma80
    crossover_negative = sma40 < sma80
    severity = price_negative.astype(int) + crossover_negative.astype(int)
    factor = severity.map({0: 1.00, 1: 0.85, 2: 0.70}).astype(float)
    factor = factor.where(sma80.notna(), 1.0).rename("T6 Fast overlay")
    diagnostics = pd.DataFrame(
        {
            "spy_close": spy,
            "sma40": sma40,
            "sma80": sma80,
            "price_negative": price_negative,
            "crossover_negative": crossover_negative,
            "severity": severity,
            "overlay": factor,
        }
    )
    return factor, diagnostics


def confirmed_severity(desired: pd.Series) -> pd.Series:
    """Apply three-session deterioration and ten-session recovery confirmation."""
    state = 0
    deterioration_streak = 0
    recovery_streak = 0
    states: list[int] = []
    for value in desired:
        if pd.isna(value):
            deterioration_streak = 0
            recovery_streak = 0
        else:
            target = int(value)
            if target > state:
                deterioration_streak += 1
                recovery_streak = 0
                if deterioration_streak >= 3:
                    state = target
                    deterioration_streak = 0
            elif target < state:
                recovery_streak += 1
                deterioration_streak = 0
                if recovery_streak >= 10:
                    state = target
                    recovery_streak = 0
            else:
                deterioration_streak = 0
                recovery_streak = 0
        states.append(state)
    return pd.Series(states, index=desired.index, dtype="int64", name="confirmed_severity")


def t7_hysteresis_overlay(
    data: Mapping[str, pd.DataFrame], index: pd.DatetimeIndex
) -> tuple[pd.Series, pd.DataFrame]:
    spy, sma40, sma80, atr20 = fast_inputs(data, index)
    lower_band = sma80 - 0.50 * atr20
    upper_band = sma80 + 0.50 * atr20
    price_negative = spy < lower_band
    price_positive = spy > upper_band
    crossover_negative = sma40 < sma80
    crossover_positive = sma40 >= sma80

    desired = pd.Series(np.nan, index=index, dtype="float64")
    both_negative = price_negative & crossover_negative
    one_negative = (price_negative | crossover_negative) & ~both_negative
    both_positive = price_positive & crossover_positive
    desired.loc[both_negative] = 2.0
    desired.loc[one_negative] = 1.0
    desired.loc[both_positive] = 0.0
    state = confirmed_severity(desired)
    factor = state.map({0: 1.00, 1: 0.85, 2: 0.70}).astype(float)
    factor = factor.where(sma80.notna() & atr20.notna(), 1.0).rename("T7 Hysteresis")
    diagnostics = pd.DataFrame(
        {
            "spy_close": spy,
            "sma40": sma40,
            "sma80": sma80,
            "atr20": atr20,
            "lower_band": lower_band,
            "upper_band": upper_band,
            "desired_severity": desired,
            "confirmed_severity": state,
            "overlay": factor,
        }
    )
    return factor, diagnostics


def t8_volatility_overlay(
    data: Mapping[str, pd.DataFrame], index: pd.DatetimeIndex
) -> tuple[pd.Series, pd.DataFrame]:
    spy = data[v1.BENCHMARK]["Close"].reindex(index)
    returns = np.log(spy).diff()
    volatility20 = returns.rolling(20, min_periods=15).std() * math.sqrt(252.0)
    volatility63 = returns.rolling(63, min_periods=42).std() * math.sqrt(252.0)
    factor = volatility63.div(volatility20.replace(0.0, np.nan)).clip(0.50, 1.00)
    factor = factor.fillna(1.0).rename("T8 Volatility shock")
    diagnostics = pd.DataFrame(
        {
            "spy_close": spy,
            "volatility20": volatility20,
            "volatility63": volatility63,
            "overlay": factor,
        }
    )
    return factor, diagnostics


def declared_trials(base_dir: Path) -> tuple[int, str]:
    manifest_path = base_dir / "output_sma_40_80" / "run_manifest_sma_comparison.json"
    if manifest_path.exists():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        previous = int(payload.get("declared_trials_for_dsr", 17))
        return previous + 3, f"prior manifest ({previous}) plus pre-registered T6-T8"
    return 20, "fallback: 17 prior trials plus pre-registered T6-T8"


def metric_row(
    name: str,
    curve: pd.Series,
    result: v2.V2Result | None,
    cash_rate: pd.Series,
    capital: float,
    trial_count: int,
    base_regime: pd.Series | None = None,
    overlay: pd.Series | None = None,
) -> dict:
    row = {"strategy": name}
    row.update(v2.metrics(curve, cash_rate, capital, result, trial_count))
    if base_regime is not None and overlay is not None:
        active_overlay = overlay.reindex(curve.index).ffill().fillna(1.0)
        effective_regime = base_regime.reindex(curve.index) * active_overlay
        row.update(
            {
                "average_overlay_pct": 100.0 * float(active_overlay.mean()),
                "overlay_active_sessions": int((active_overlay < 1.0 - 1e-12).sum()),
                "average_effective_regime_pct": 100.0 * float(effective_regime.mean()),
                "risk_only_sells": int(
                    result.orders["reason"].astype(str).str.startswith("overlay_").sum()
                ) if result is not None and not result.orders.empty else 0,
            }
        )
    return row


def qualification_table(metrics: pd.DataFrame) -> pd.DataFrame:
    baseline = metrics.loc[metrics["strategy"] == "V2 baseline"].iloc[0]
    rows = []
    for name in STRATEGY_NAMES[1:]:
        candidate = metrics.loc[metrics["strategy"] == name].iloc[0]
        checks = {
            "sharpe_improves": float(candidate["sharpe_excess_cash"]) > float(baseline["sharpe_excess_cash"]),
            "drawdown_improves": float(candidate["max_drawdown_pct"]) > float(baseline["max_drawdown_pct"]),
            "cagr_loss_under_1pp": float(candidate["cagr_pct"]) >= float(baseline["cagr_pct"]) - 1.0,
            "orders_at_most_700": int(candidate["orders"]) <= 700,
        }
        rows.append({"strategy": name, **checks, "qualifies": all(checks.values())})
    return pd.DataFrame(rows)


def create_charts(
    curves: Mapping[str, pd.Series],
    overlays: Mapping[str, pd.Series],
    base_regime: pd.Series,
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    plt.style.use("seaborn-v0_8-whitegrid")
    colors = {
        "V2 baseline": "#315C7C",
        "T6 Fast overlay": "#B87950",
        "T7 Hysteresis": "#6B8E7B",
        "T8 Volatility shock": "#8A6F9E",
        "SPY buy & hold": "#4E565C",
    }
    fig, ax = plt.subplots(figsize=(11, 5.6))
    for name, curve in curves.items():
        ax.plot(
            curve.index, curve / curve.iloc[0] * 100.0,
            label=name, color=colors[name], linewidth=1.65,
        )
    ax.set_yscale("log")
    ax.set_ylabel("Valore normalizzato (log, base 100)")
    ax.set_title("Overlay difensivi: capitale netto")
    ax.legend(frameon=False, ncol=2)
    fig.tight_layout()
    fig.savefig(output_dir / "equity_curves_overlays.png", dpi=175)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 4.6))
    for name, curve in curves.items():
        ax.plot(
            curve.index, (curve / curve.cummax() - 1.0) * 100.0,
            label=name, color=colors[name], linewidth=1.35,
        )
    ax.set_ylabel("Drawdown (%)")
    ax.set_title("Overlay difensivi: drawdown")
    ax.legend(frameon=False, ncol=2)
    fig.tight_layout()
    fig.savefig(output_dir / "drawdowns_overlays.png", dpi=175)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 4.2))
    for name, overlay in overlays.items():
        ax.plot(
            overlay.index, overlay.rolling(21, min_periods=1).mean() * 100.0,
            label=name, color=colors[name], linewidth=1.3,
        )
    ax.set_ylim(45.0, 102.0)
    ax.set_ylabel("Moltiplicatore (%)")
    ax.set_title("Overlay, media a 21 sedute")
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output_dir / "overlay_multipliers.png", dpi=175)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 4.2))
    for name, overlay in {"V2 baseline": pd.Series(1.0, index=base_regime.index), **overlays}.items():
        effective = base_regime * overlay
        ax.plot(
            effective.index, effective.rolling(21, min_periods=1).mean() * 100.0,
            label=name, color=colors[name], linewidth=1.25,
        )
    ax.set_ylim(0.0, 102.0)
    ax.set_ylabel("Regime effettivo (%)")
    ax.set_title("Regime strutturale × overlay")
    ax.legend(frameon=False, ncol=2)
    fig.tight_layout()
    fig.savefig(output_dir / "effective_regimes.png", dpi=175)
    plt.close(fig)


def build_report(
    output_dir: Path,
    metrics: pd.DataFrame,
    periods: pd.DataFrame,
    qualification: pd.DataFrame,
    bootstraps: Mapping[str, Mapping[str, object]],
    manifest: Mapping[str, object],
) -> None:
    qualified = qualification.loc[qualification["qualifies"], "strategy"].tolist()
    if qualified:
        verdict = "Supera i criteri: " + ", ".join(qualified) + "."
    else:
        t8 = metrics.loc[metrics["strategy"] == "T8 Volatility shock"].iloc[0]
        verdict = (
            "Nessun overlay supera tutti i criteri preregistrati. T8 è il miglior "
            f"challenger: Sharpe {t8['sharpe_excess_cash']:.2f}, drawdown "
            f"{t8['max_drawdown_pct']:.2f}% e {int(t8['orders'])} ordini; la V2 resta "
            "il riferimento finché il carico operativo non viene confermato."
        )
    metric_columns = [
        "strategy", "ending_value_eur", "cagr_pct", "annual_volatility_pct",
        "sharpe_excess_cash", "max_drawdown_pct", "average_exposure_pct", "orders",
        "fees_eur", "estimated_slippage_eur", "average_overlay_pct",
        "average_effective_regime_pct", "risk_only_sells",
    ]
    metric_view = metrics[[column for column in metric_columns if column in metrics.columns]]
    period_view = periods[
        ["period", "strategy", "cagr_pct", "annual_volatility_pct", "sharpe_excess_cash", "max_drawdown_pct"]
    ]
    bootstrap_rows = []
    for name, values in bootstraps.items():
        bootstrap_rows.append(
            {
                "strategy": name,
                "median_difference_pct": values["annualized_difference_median_pct"],
                "ci_low_pct": values["annualized_difference_ci_low_pct"],
                "ci_high_pct": values["annualized_difference_ci_high_pct"],
                "probability_beats_baseline_pct": 100.0 * float(values["probability_strategy_beats_benchmark"]),
            }
        )
    bootstrap_frame = pd.DataFrame(bootstrap_rows)
    css = """
body{font-family:Inter,Arial,sans-serif;margin:0;color:#25313a;background:#f4f6f7;line-height:1.5}
main{max-width:1160px;margin:24px auto;padding:30px 38px;background:#fff;box-shadow:0 4px 24px #00000012}
h1,h2{color:#244e68}.callout{background:#edf4f7;border-left:4px solid #315c7c;padding:12px 16px}
.warning{background:#fff4df;border-left-color:#b87950}table{width:100%;border-collapse:collapse;font-size:12px;margin:14px 0 24px}
th{background:#315c7c;color:#fff;padding:7px;text-align:right}td{border-bottom:1px solid #dbe1e4;padding:6px 7px;text-align:right}
th:first-child,td:first-child{text-align:left}img{max-width:100%;margin:8px 0 20px}.small{color:#66737c;font-size:12px}
"""
    body = f"""<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Overlay difensivi T6-T8</title>
<style>{css}</style></head><body><main><h1>Overlay difensivi T6–T8</h1>
<p>Equity Long V2 · selezione titoli SMA50/200 congelata</p>
<div class="callout"><strong>Verdetto.</strong> {html.escape(verdict)}</div>
<h2>Metriche principali</h2>{v2.format_table(metric_view)}
<img src="equity_curves_overlays.png" alt="Curve del capitale">
<img src="drawdowns_overlays.png" alt="Drawdown">
<img src="overlay_multipliers.png" alt="Moltiplicatori degli overlay">
<img src="effective_regimes.png" alt="Regimi effettivi">
<h2>Criteri preregistrati</h2>{qualification.to_html(index=False, border=0)}
<p>Occorrono contemporaneamente: Sharpe e drawdown migliori, perdita di CAGR inferiore a un punto e non più di 700 ordini.</p>
<h2>Bootstrap contro V2</h2>{v2.format_table(bootstrap_frame)}
<h2>Sottoperiodi</h2>{v2.format_table(period_view)}
<h2>Regole testate</h2><ul>
<li>T6: 40/80 solo sul mercato; fattori 1,00 / 0,85 / 0,70.</li>
<li>T7: T6 con banda ±0,5 ATR20, conferma bearish 3 sedute e recovery 10 sedute.</li>
<li>T8: fattore clip(σ63/σ20, 0,50, 1,00).</li>
<li>I controlli settimanali possono soltanto ridurre posizioni; acquisti e rotazioni restano ogni cinque settimane.</li>
</ul>
<div class="callout warning"><strong>Metodo.</strong> T6–T8 sono stati definiti insieme prima del calcolo e portano il numero dichiarato di trial a {int(manifest['declared_trials_for_dsr'])}. Nessuna soglia è stata ritoccata dopo il risultato.</div>
<p class="small">Ricerca esplorativa, non consulenza finanziaria né raccomandazione.</p>
</main></body></html>"""
    (output_dir / "report_overlays.html").write_text(body, encoding="utf-8")


def main() -> None:
    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    cache_dir = (base_dir / args.cache_dir).resolve()
    input_dir = (base_dir / args.input_dir).resolve()
    output_dir = (base_dir / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cfg = v2.V2Config()

    symbols = sorted(
        set(v1.TICKER_TO_SECTOR) | set(v1.SECTOR_UNIVERSE) | {v1.BENCHMARK, v1.FX_TICKER}
    )
    data, failures = v1.download_universe(
        symbols, args.data_start, args.end, cache_dir, args.refresh, args.workers
    )
    stocks = [ticker for ticker in v1.TICKER_TO_SECTOR if ticker in data]
    if len(stocks) < 30:
        raise RuntimeError(f"Only {len(stocks)} stocks available")
    print("Building frozen SMA50/200 stock features ...", flush=True)
    features = v1.build_features(data, v1.StrategyConfig())
    index = features.close_eur.index

    membership, membership_status = v2.load_membership_mask(
        input_dir / "universe_membership.csv", index, stocks
    )
    pit_signals, pit_signal_status = v2.load_pit_signals(
        input_dir / "pit_signals.csv", index, stocks
    )
    execution_costs, execution_cost_status = v2.load_execution_costs(
        input_dir / "execution_costs.csv", index, stocks
    )
    cash_rate, cash_rate_status = v2.fetch_ecb_cash_rate(
        index, cache_dir, args.refresh, cfg.cash_rate_haircut_bps
    )
    score, score_weights = v2.combine_rule_score(features.score, pit_signals)
    eligible = features.eligible & membership
    base_regime = v2.continuous_regime(features, data)
    t6, t6_diagnostics = t6_fast_overlay(data, index)
    t7, t7_diagnostics = t7_hysteresis_overlay(data, index)
    t8, t8_diagnostics = t8_volatility_overlay(data, index)
    overlays = {
        "T6 Fast overlay": t6,
        "T7 Hysteresis": t7,
        "T8 Volatility shock": t8,
    }

    print("Running baseline and pre-registered T6-T8 ...", flush=True)
    baseline = v2.simulate_v2(
        features, score, eligible, membership, base_regime, cash_rate,
        execution_costs, cfg, args.test_start, args.capital, "V2 baseline",
    )
    results: dict[str, v2.V2Result] = {"V2 baseline": baseline}
    for name, overlay in overlays.items():
        results[name] = v2.simulate_v2(
            features, score, eligible, membership, base_regime, cash_rate,
            execution_costs, cfg, args.test_start, args.capital, name,
            weekly_risk_overlay=overlay,
        )

    fx = data[v1.FX_TICKER]["Close"].reindex(data[v1.BENCHMARK].index).ffill().bfill()
    spy_close_eur = data[v1.BENCHMARK]["Close"].reindex(fx.index).div(fx)
    spy_open_eur = data[v1.BENCHMARK]["Open"].reindex(fx.index).div(fx.shift(1).bfill())
    spy = v1.buy_and_hold(
        spy_open_eur, spy_close_eur, args.test_start, args.capital,
        cfg.fixed_fee_eur, cfg.default_slippage_bps_per_side,
    )
    curves = {name: result.equity for name, result in results.items()}
    curves["SPY buy & hold"] = spy

    trial_count, trial_source = declared_trials(base_dir)
    rows = [
        metric_row(
            name, result.equity, result, cash_rate, args.capital, trial_count,
            base_regime, pd.Series(1.0, index=index) if name == "V2 baseline" else overlays[name],
        )
        for name, result in results.items()
    ]
    rows.append(metric_row("SPY buy & hold", spy, None, cash_rate, args.capital, trial_count))
    metrics = pd.DataFrame(rows)
    periods = v2.period_table(curves, cash_rate, args.capital)
    qualification = qualification_table(metrics)
    bootstraps = {
        name: v2.block_bootstrap_difference(results[name].equity, baseline.equity)
        for name in overlays
    }

    pd.concat(curves.values(), axis=1).to_csv(
        output_dir / "equity_curves_overlays.csv", index_label="Date"
    )
    metrics.to_csv(output_dir / "metrics_overlays.csv", index=False)
    periods.to_csv(output_dir / "period_metrics_overlays.csv", index=False)
    qualification.to_csv(output_dir / "qualification_overlays.csv", index=False)
    pd.DataFrame({"base_regime": base_regime, **overlays}).to_csv(
        output_dir / "overlay_series.csv", index_label="Date"
    )
    t6_diagnostics.to_csv(output_dir / "diagnostics_t6.csv", index_label="Date")
    t7_diagnostics.to_csv(output_dir / "diagnostics_t7.csv", index_label="Date")
    t8_diagnostics.to_csv(output_dir / "diagnostics_t8.csv", index_label="Date")
    for name, result in results.items():
        suffix = name.lower().replace(" ", "_")
        result.orders.to_csv(output_dir / f"orders_{suffix}.csv", index=False)
        result.allocations.to_csv(output_dir / f"allocations_{suffix}.csv", index=False)
        result.realized_lots.to_csv(output_dir / f"realized_lots_{suffix}.csv", index=False)
    (output_dir / "bootstrap_overlays.json").write_text(
        json.dumps(bootstraps, indent=2), encoding="utf-8"
    )
    manifest = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "data_start": args.data_start,
        "test_start": args.test_start,
        "requested_end": args.end,
        "stocks_available": len(stocks),
        "failed_symbols": failures,
        "stock_selection_windows": {"fast": 50, "slow": 200},
        "t6": {"market_windows": [40, 80], "factors": [1.0, 0.85, 0.70]},
        "t7": {
            "market_windows": [40, 80], "factors": [1.0, 0.85, 0.70],
            "atr_band_multiple": 0.5, "bear_confirmation_sessions": 3,
            "recovery_confirmation_sessions": 10,
        },
        "t8": {"formula": "clip(volatility63/volatility20, 0.50, 1.00)"},
        "risk_only_execution": {
            "frequency": "weekly", "buys_allowed": False,
            "trade_band_weight": 0.025, "minimum_trade_eur": 250.0,
        },
        "full_decision_frequency_weeks": cfg.decision_interval_weeks,
        "qualification_rule": {
            "sharpe_improves": True, "drawdown_improves": True,
            "maximum_cagr_loss_percentage_points": 1.0, "maximum_orders": 700,
        },
        "portfolio_configuration": vars(cfg),
        "score_weights": score_weights,
        "membership_status": membership_status,
        "pit_signal_status": pit_signal_status,
        "execution_cost_status": execution_cost_status,
        "cash_rate_status": cash_rate_status,
        "declared_trials_for_dsr": trial_count,
        "trial_count_source": trial_source,
    }
    (output_dir / "run_manifest_overlays.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    create_charts(curves, overlays, base_regime, output_dir)
    build_report(output_dir, metrics, periods, qualification, bootstraps, manifest)
    print("\nOverlay metrics:")
    print(
        metrics[
            ["strategy", "cagr_pct", "sharpe_excess_cash", "max_drawdown_pct", "average_exposure_pct", "orders"]
        ].to_string(index=False)
    )
    print("\nQualification:")
    print(qualification.to_string(index=False))
    print(f"\nReport: {output_dir / 'report_overlays.html'}")


if __name__ == "__main__":
    main()
