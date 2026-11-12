"""No-network regressions: same-session FX revisions, FIFO tax and delayed rotations."""
import datetime as dt,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import paper_account as pa

def tm(d):return dt.datetime.fromisoformat(d+"T20:00:00+00:00")
def fresh():
    return {"execution_adapter":pa.POLICY,"initial_cash_eur":10000.,"cash_eur":10000.,
    "holdings":{},"orders":[],"last_nav_eur":10000.,"fees_eur_total":0.,
    "estimated_slippage_eur":0.,"tax_eur_total":0.,"blocked_sell_requests":0,
    "processed_decision_dates":[],"equity_points":[]}
class RevisionAndFifoTests(unittest.TestCase):
    def test_legacy_repeated_market_close_reconciles_once(self):
        s=fresh()
        s["equity_points"]=[
          {"report_date":"2026-11-10","source_close_session":"2026-11-09",
           "mark_to_market_nav_eur":9992.4192,"net_liquidation_nav_eur":9984.8383},
          {"report_date":"2026-11-11","source_close_session":"2026-11-11",
           "mark_to_market_nav_eur":10029.061,"net_liquidation_nav_eur":10011.3352},
          {"report_date":"2026-11-12","source_close_session":"2026-11-11",
           "mark_to_market_nav_eur":10007.1345,"net_liquidation_nav_eur":9992.6322}]
        port={"valuation_session":"2026-11-11",
             "nav_mark_to_market_eur":10007.1345,"nav_net_liquidation_eur":9992.6322}
        fx={"decision_session":"2026-11-11",
            "fx_units_per_eur_asof":{"USD":1.1168941259},
            "fx_currency_source_sessions":{"USD":"2026-11-11"}}
        first=pa.reconcile_daily_nav(s,port,fx,tm("2026-11-12"))
        self.assertEqual(first["historical_daily_sessions"],2)
        self.assertEqual(first["valuation_revision_count"],1)
        self.assertAlmostEqual(s["valuation_revisions"][0]["prior_mark_to_market_nav_eur"],10029.061)
        second=pa.reconcile_daily_nav(s,port,fx,tm("2026-11-12"))
        self.assertEqual(second["valuation_revision_count"],1)
        updated=dict(port,nav_mark_to_market_eur=10006.)
        changed=dict(fx,fx_units_per_eur_asof={"USD":1.1173})
        third=pa.reconcile_daily_nav(s,updated,changed,tm("2026-11-13"))
        self.assertEqual(third["historical_daily_sessions"],2)
        self.assertEqual(third["valuation_revision_count"],2)

    def test_hypothetical_equity_tax_netting_and_no_premature_real_sale(self):
        s=fresh()
        pa.buy(s,"WIN",1000.,100.,"2026-11-09",tm("2026-11-10"),"2026-11-09","EOD")
        pa.buy(s,"LOSS",1000.,100.,"2026-11-09",tm("2026-11-10"),"2026-11-09","EOD")
        q={"decision_session":"2026-11-11","close_eur":{"WIN":110.,"LOSS":90.},
           "ticker_price_dates":{"WIN":"2026-11-11","LOSS":"2026-11-11"}}
        v=pa.generate_portfolio(s,q,{},tm("2026-11-12"))
        self.assertEqual(v["hypothetical_liquidation_tax_eur"],0.)
        self.assertAlmostEqual(v["nav_mark_to_market_eur"]-v["hypothetical_liquidation_sale_cost_eur"],
                               v["nav_net_liquidation_eur"],places=3)
        self.assertTrue(v["hypothetical_liquidation_assumes_all_lots_mature"])
        self.assertEqual(len(s["orders"]),2)
        pa.validate_state(s)

    def test_fifo_cost_of_mature_lot_excludes_expensive_locked_lot(self):
        s=fresh()
        pa.buy(s,"KO",1000.,100.,"2026-11-09",tm("2026-11-10"),"2026-11-09","EOD")
        pa.buy(s,"KO",1000.,200.,"2026-11-16",tm("2026-11-17"),"2026-11-16","EOD")
        initial=s["holdings"]["KO"]["lots"][0].copy()
        self.assertTrue(pa.sell(s,"KO",600.,150.,"2026-12-14",tm("2026-12-15"),
                                "2026-12-15","EOD","rotation"))
        sold=s["orders"][-1]
        cost=initial["cost_basis_eur"]*sold["shares"]/initial["shares"]
        gain=sold["shares"]*sold["execution_price_eur"]-sold["fee_eur"]-cost
        self.assertAlmostEqual(sold["capital_gains_tax_eur"],max(0,gain)*.26,places=5)
        remaining=s["holdings"]["KO"]
        self.assertAlmostEqual(remaining["cost_basis_eur"],
              sum(x["cost_basis_eur"] for x in remaining["lots"]),places=5)
        pa.validate_state(s)

    def test_14_dec_full_rotation_not_executed_at_14_dec_close_before_15_dec_unlock(self):
        s=fresh()
        pa.buy(s,"KO",1000.,100.,"2026-11-09",tm("2026-11-10"),"2026-11-09","EOD")
        candidates=[{"ticker":"ABBV","weight_pct":12.,
                     "reference_previous_close_eur":200.}]
        q={"decision_session":"2026-12-14",
            "close_eur":{"KO":101.,"ABBV":200.},
            "ticker_price_dates":{"KO":"2026-12-14","ABBV":"2026-12-14"}}
        deferred=pa.execute_or_defer_five_week_rebalance(s,candidates,
                                "2026-12-14",q,tm("2026-12-15"))
        self.assertFalse(deferred["completed"])
        self.assertEqual(deferred["blocked"],["KO"])
        self.assertEqual(len(s["orders"]),1)
        q["decision_session"]="2026-12-15"
        q["ticker_price_dates"]={"KO":"2026-12-15","ABBV":"2026-12-15"}
        ready=pa.execute_or_defer_five_week_rebalance(s,candidates,
                        "2026-12-14",q,tm("2026-12-16"),deferred=True)
        self.assertTrue(ready["completed"])
        self.assertNotIn("KO",s["holdings"])
        self.assertIn("ABBV",s["holdings"])
        self.assertEqual(s["orders"][-1]["decision_date"],"2026-12-14")
        self.assertEqual(s["orders"][-1]["date"],"2026-12-16")
        self.assertEqual(s["orders"][-1]["price_source_date"],"2026-12-15")
        pa.validate_state(s)

if __name__=="__main__":unittest.main()
