"""Offline regression tests for daily, as-recorded cross-market universe updates."""
import csv
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_model as mod
import paper_account as paper


def save_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as out:
        w = csv.DictWriter(out, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


class DailyUniverseTests(unittest.TestCase):
    def test_ranked_replacements_and_no_lookahead_marketcap(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "live_v4"
            snap = live / "dataset" / "universe" / "snapshots" / "2026-11-09"
            snap.mkdir(parents=True)
            rows = [
                {"symbol": f"U{i:03d}", "country": "United States", "currency": "USD",
                 "sector": "Information Technology", "company_name": f"US {i}"}
                for i in range(105)
            ] + [
                {"symbol": f"R{i:03d}.T", "country": "Japan", "currency": "JPY",
                 "sector": "Industrials", "company_name": f"ROW {i}"}
                for i in range(208)
            ]
            # Real feed has 500 rows; keep the ranked list large enough to replace.
            rows += [
                {"symbol": f"X{i:03d}.T", "country": "Japan", "currency": "JPY",
                 "sector": "Industrials", "company_name": f"Filler {i}"}
                for i in range(187)
            ]
            save_csv(snap / "global500.csv", rows)
            (snap / "source.json").write_text(json.dumps({
                "recorded_on": "2026-11-09", "source_as_of": "Nov 09, 2026",
                "type": "ETF_HOLDING_VALUE_PROXY_NOT_MARKET_CAP"
            }), encoding="utf-8")
            # Future market cap snapshot must never alter the decision on Nov 9.
            observed = live / "dataset" / "marketcap_observed" / "observations" / "2026-11-10T060000Z"
            observed.mkdir(parents=True)
            top = [dict(r, market_cap_usd=100000000000-i*1000000,
                        market_cap_vendor_rank=i+1)
                   for i,r in enumerate(rows)]
            save_csv(observed / "ranking.csv", top)
            (observed / "source.json").write_text(json.dumps({
                "capture_day": "2026-11-10",
                "observed_at_utc": "2026-11-10T06:00:00+00:00",
                "sufficient_for_100_usa_200_nonusa": True,
                "historical_pit_market_cap_verified": False
            }), encoding="utf-8")
            with mock.patch.object(mod, "LIVE", live):
                yesterday = mod.latest_vintages(dt.date(2026, 11, 9),
                    excluded_symbols={"U000", "R000.T"})
                self.assertEqual(len(yesterday), 1)
                self.assertEqual(len(yesterday[-1][3]), 100)
                self.assertEqual(len(yesterday[-1][4]), 200)
                self.assertNotIn("U000", [r["symbol"] for r in yesterday[-1][3]])
                self.assertEqual(yesterday[-1][3][-1]["symbol"], "U100")
                today = mod.latest_vintages(dt.date(2026, 11, 10),
                    excluded_symbols={"U000", "R000.T"})
                self.assertEqual(len(today), 2)
                self.assertIn("MARKET_CAP_USD_AS_RECORDED", today[-1][5])
                self.assertEqual(today[-1][0], dt.date(2026, 11, 10))
                self.assertEqual(len(today[-1][3]) + len(today[-1][4]), 300)

    def test_gbp_pence_quotes_are_normalized_before_fx_conversion(self):
        self.assertEqual(mod._quote_unit_scale("AZN.L", "GBp"), 0.01)
        self.assertEqual(mod._quote_unit_scale("AZN.L", "GBX"), 0.01)
        self.assertEqual(mod._quote_unit_scale("AZN.L", "GBP"), 1.0)
        self.assertEqual(mod._quote_unit_scale("AAPL", "USD"), 1.0)

    def test_local_exchange_price_asof_is_not_us_session_stamp(self):
        quotes = {
            "decision_session": "2026-11-10",
            "close_eur": {"AAPL": 100.0, "G::7203.T": 21.0},
            "ticker_price_dates": {"AAPL": "2026-11-10", "G::7203.T": "2026-11-09"}
        }
        usa = paper.next_available_price("AAPL", quotes, {}, {},
                                          dt.date(2026, 11, 11))
        tokyo = paper.next_available_price("G::7203.T", quotes, {}, {},
                                            dt.date(2026, 11, 11))
        self.assertEqual(usa[1], "2026-11-10")
        self.assertEqual(tokyo[1], "2026-11-09")
        self.assertNotIn("STALE", tokyo[2])
        tokyo_stale = paper.next_available_price("G::7203.T", quotes, {}, {},
                                                  dt.date(2026, 11, 17))
        self.assertTrue(tokyo_stale[2].startswith("STALE"))


if __name__ == "__main__":
    unittest.main()
