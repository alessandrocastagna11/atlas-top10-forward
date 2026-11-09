"""Regression tests for V4 proxy turnover guards (no official ledger writes)."""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paper import ENGINE, execute_paper, fresh_state


def signal(targets, execution="2026-11-09"):
    return {
        "engine_id": ENGINE, "mode": "SHADOW",
        "decision_session": "2026-11-06", "execution_session": execution,
        "targets": [{"ticker": ticker, "market_symbol": ticker,
                     "region": "USA", "sector": "Information Technology",
                     "weight": weight} for ticker, weight in targets]
    }


def seeded_state(n):
    st = fresh_state()
    for i in range(n):
        ticker = f"T{i}"
        st["holdings"][ticker] = {
            "shares": 1.0, "cost_basis_eur_per_share": 100.0,
            "first_entry_date": "2026-11-06",
            "market_symbol": ticker, "region": "USA",
            "sector": "Information Technology"
        }
    st["cash_eur"] = 10000.0 - n * 100.0
    return st


class ProxyHoldingLockTests(unittest.TestCase):
    def test_locked_sales_skipped_but_allowed_buys_continue(self):
        st = seeded_state(8)
        quotes = {**{f"T{i}": 100.0 for i in range(8)}, "NEW1": 100.0, "NEW2": 100.0, "NEW3": 100.0}
        targets = [("NEW1", .05), ("NEW2", .05), ("NEW3", .05)]
        new, receipt = execute_paper(st, signal(targets), quotes, rebalance_fraction=.01)
        self.assertEqual(len(new["holdings"]), 10)
        self.assertEqual({o["action"] for o in receipt["orders"]}, {"BUY"})
        self.assertEqual(sum(o["ticker"].startswith("NEW") for o in receipt["orders"]), 2)
        self.assertTrue(all(f"T{i}" in new["holdings"] for i in range(8)))
        self.assertEqual(len(st["holdings"]), 8)  # no mutation of caller state

    def test_full_locked_portfolio_does_not_buy_eleventh(self):
        st = seeded_state(10)
        quotes = {**{f"T{i}": 100.0 for i in range(10)}, "NEW": 100.0}
        new, receipt = execute_paper(st, signal([("NEW", .05)]), quotes, rebalance_fraction=.01)
        self.assertEqual(len(new["holdings"]), 10)
        self.assertEqual(receipt["orders_count"], 0)
        self.assertEqual(new["cash_eur"], st["cash_eur"])

    def test_geographic_slots_prioritize_row_when_eight_us_locked(self):
        st=seeded_state(8)
        quotes={**{f"T{i}":100. for i in range(8)},
                "US1":100., "US2":100., "ROW1":100., "ROW2":100.}
        sig=signal([("US1",.05),("US2",.05),("ROW1",.05),("ROW2",.05)])
        for item in sig["targets"]:
            if item["ticker"].startswith("ROW"):
                item["region"]="ROW"
                item["sector"]="Industrials"
        new,receipt=execute_paper(st,sig,quotes,rebalance_fraction=.001)
        self.assertEqual(len(new["holdings"]),10)
        self.assertEqual({o["ticker"] for o in receipt["orders"]},{"ROW1","ROW2"})
        self.assertEqual(sum(x["region"]=="ROW" for x in new["holdings"].values()),2)

    def test_locked_sector_and_total_exposure_limit_new_buys(self):
        st = seeded_state(1)
        st['holdings']['T0']['shares']=29.
        st['cash_eur']=7100.
        quotes={'T0':100.,'NEW':100.}
        new,receipt=execute_paper(st,signal([('NEW',.15)]),quotes,rebalance_fraction=.001)
        self.assertEqual(receipt['orders_count'],0)
        self.assertEqual(len(new['holdings']),1)

    def test_cannot_add_when_old_position_over_issuer_cap(self):
        st=seeded_state(1)
        st['holdings']['T0']['shares']=18.
        st['cash_eur']=8200.
        quotes={'T0':100.}
        new,receipt=execute_paper(st,signal([('T0',.15)]),quotes,rebalance_fraction=.001)
        self.assertEqual(receipt['orders_count'],0)
        self.assertEqual(new['holdings']['T0']['shares'],18.)

    def test_sale_allowed_after_35_calendar_days(self):
        st = seeded_state(1)
        quotes = {"T0": 100.0}
        new, receipt = execute_paper(st, signal([], execution="2026-12-11"), quotes, rebalance_fraction=.005)
        self.assertEqual(receipt["orders_count"], 1)
        self.assertEqual(receipt["orders"][0]["action"], "SELL")
        self.assertEqual(len(new["holdings"]), 0)


if __name__ == "__main__":
    unittest.main()
