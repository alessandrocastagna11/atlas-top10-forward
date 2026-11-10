"""No-market and no-network tests of operational Confidence70 PAPER accounting."""
from __future__ import annotations
import datetime as dt,json,tempfile,unittest
from pathlib import Path
from unittest import mock
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import paper_account as pa

UTC=dt.timezone.utc

def fixture_state():
    return {"execution_adapter":pa.POLICY,"initial_cash_eur":10000.,
        "cash_eur":10000.,"holdings":{},"orders":[],"last_nav_eur":10000.,
        "fees_eur_total":0.,"estimated_slippage_eur":0.,"tax_eur_total":0.,
        "blocked_sell_requests":0,"processed_decision_dates":[],"equity_points":[]}

class PaperAccountTests(unittest.TestCase):
    def test_lock_absolute_defensive_and_next_day(self):
        s=fixture_state();now=dt.datetime(2026,11,10,20,0,tzinfo=UTC)
        self.assertTrue(pa.buy(s,"KO",1000.,85.,"2026-11-09",now,"2026-11-09","FROZEN_PREVIOUS_CLOSE"))
        self.assertFalse(pa.sell(s,"KO",800.,85.,"2026-11-10",
            now+dt.timedelta(days=5),"2026-11-10","EOD","weekly_defensive"))
        self.assertEqual(s["blocked_sell_requests"],1)
        self.assertEqual(len(s["orders"]),1)
        self.assertEqual(s["holdings"]["KO"]["lots"][0]["sale_eligible_date"],"2026-12-15")
        self.assertTrue(pa.sell(s,"KO",800.,90.,"2026-12-15",
            now+dt.timedelta(days=35),"2026-12-15","EOD","weekly_defensive"))
        self.assertEqual(len(s["orders"]),2)
        self.assertTrue(all(x["execution_mode"]==pa.POLICY for x in s["orders"]))
        pa.validate_state(s)

    def test_end_to_end_first_buys_persist_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/"forward";pre=out/"preopen";pre.mkdir(parents=True)
            signal={"market_data_first_observed_before_open":True,
                    "preopen_selected_symbols":["KO","JNJ"]}
            candidates=[
                {"ticker":"KO","market_symbol":"KO","indicative_allocation_eur":1100.,
                 "reference_previous_close_eur":85.,"meets_minimum_order_threshold":True},
                {"ticker":"JNJ","market_symbol":"JNJ","indicative_allocation_eur":900.,
                 "reference_previous_close_eur":250.,"meets_minimum_order_threshold":True}]
            pp={"decision_session":"2026-11-09",
                "possible_candidates":candidates}
            (pre/"2026-11-09.json").write_text(json.dumps(signal))
            (pre/"2026-11-09_proposal.json").write_text(json.dumps(pp))
            (out/"proposal_last_close.json").write_text(json.dumps(pp))
            (out/"latest_status.json").write_text(json.dumps({"decision_session":"2026-11-09"}))
            (out/"latest_quotes.json").write_text(json.dumps({
                "decision_session":"2026-11-09","close_eur":{"KO":85.,"JNJ":250.}}))
            overrides={
                "ROOT":out,"STATUS":out/"latest_status.json","STATE":out/"state.json",
                "PORTFOLIO":out/"portfolio.json","PROPOSAL":out/"proposal_last_close.json",
                "PREOPEN":pre,"QUOTES":out/"latest_quotes.json","ORDER_PATH":out/"orders.csv",
                "EQUITY":out/"equity.csv"}
            now=dt.datetime(2026,11,10,20,0,tzinfo=UTC)
            with mock.patch.multiple(pa,**overrides):
                first=pa.run(now)
                self.assertEqual(first["orders_cumulative"],2)
                self.assertEqual(first["positions_count"],2)
                self.assertLess(first["nav_eur"],10000.)
                self.assertGreater(first["nav_eur"],9970.)
                self.assertLess(first["cash_eur"],8100.)
                second=pa.run(now+dt.timedelta(hours=1))
                self.assertEqual(second["orders_cumulative"],2)
                self.assertEqual(second["new_orders"],0)
                state=json.loads((out/"state.json").read_text())
                self.assertEqual(len(state["orders"]),2)
                self.assertEqual(len(state["equity_points"]),1)
                self.assertEqual(state["orders"][0]["date"],"2026-11-10")
                self.assertFalse(state["orders"][0]["real_market_fill"])
                self.assertTrue((out/"orders.csv").exists())
                self.assertTrue((out/"latest.md").exists())
                self.assertIn("PROXY", (out/"latest.md").read_text())
                # A Monday model decision is preserved, then consumed by the
                # following Tuesday update, even if the latest daily signal is
                # already Tuesday's. No daily execution duplicates permitted.
                (out/"proposals_by_session").mkdir(exist_ok=True)
                (out/"proposals_by_session"/"2026-11-16.json").write_text(
                    json.dumps({"decision_session":"2026-11-16","regime_multiplier":0.1,
                                "possible_candidates":candidates}))
                (out/"latest_status.json").write_text(
                    json.dumps({"decision_session":"2026-11-17"}))
                (out/"latest_quotes.json").write_text(
                    json.dumps({"decision_session":"2026-11-17",
                                "close_eur":{"KO":85.,"JNJ":250.}}))
                tuesday=dt.datetime(2026,11,17,20,0,tzinfo=UTC)
                updated=pa.run(tuesday)
                saved=json.loads((out/"state.json").read_text())
                self.assertIn("2026-11-16",saved["processed_decision_dates"])
                self.assertEqual(updated["orders_cumulative"],2)
                self.assertEqual(pa.run(tuesday+dt.timedelta(minutes=30))["new_orders"],0)
                self.assertEqual(len(json.loads((out/"state.json").read_text())["equity_points"]),2)


if __name__=="__main__":unittest.main()
