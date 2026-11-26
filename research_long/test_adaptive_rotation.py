import datetime as dt
import unittest
from types import SimpleNamespace
import pandas as pd
from adaptive_rotation import net_edge,select_swap

class TestEconomicGate(unittest.TestCase):
    def test_strict_lot_lock(self):
        self.assertIsNone(net_edge(1000,1000,0,.1,1,10,.26,35,34,500))
        self.assertIsNotNone(net_edge(1000,1000,0,.1,1,10,.26,35,35,500))
    def test_two_fees_and_two_slippages(self):
        r=net_edge(1000,1000,0,.04,1,10,.26,35,35,500)
        self.assertAlmostEqual(r["investable"],997/1.001)
        self.assertAlmostEqual(r["friction"],1000-997/1.001)
        self.assertGreater(r["net_edge"],0)
    def test_tax_can_prevent_switch(self):
        self.assertIsNone(net_edge(1000,500,0,.08,1,10,.26,35,35,500))
        self.assertIsNotNone(net_edge(1000,1000,0,.08,1,10,.26,35,35,500))
    def test_no_assumed_loss_credit(self):
        a=net_edge(1000,1000,0,.08,1,10,.26,35,35,500)
        b=net_edge(1000,1500,0,.08,1,10,.26,35,35,500)
        self.assertEqual(a,b)
    def test_minimum_trade_after_tax(self):
        self.assertIsNone(net_edge(505,500,0,.50,1,10,.26,35,35,505))
    def test_no_nonfinite_signal(self):
        self.assertIsNone(net_edge(1000,1000,float("nan"),.08,1,10,.26,35,35,500))
    def test_selector_uses_prior_session_only_and_youngest_lot(self):
        ix=pd.to_datetime(["2025-01-06","2025-01-07"])
        close=pd.DataFrame({"A":[100,99999],"B":[100,.01],"C":[100,100]},index=ix)
        score=pd.DataFrame({"A":[.1,10],"B":[1,-10],"C":[.5,50]},index=ix)
        alpha=score*.1
        elig=pd.DataFrame(True,index=ix,columns=score.columns)
        membership=elig.copy();membership.loc[ix[0],"C"]=False
        cfg=SimpleNamespace(minimum_trade_eur=500,rebalance_band_weight=.06,
            minimum_holding_calendar_days=35,maximum_position_weight=.15,
            maximum_sector_weight=.30,max_names_per_sector=3,fixed_fee_eur=1,
            default_slippage_bps_per_side=10)
        pos={"A":SimpleNamespace(shares=10,cost_basis_eur=1000)}
        mature={"A":[{"date":ix[0]-pd.Timedelta(days=35),"shares":10}]}
        young={"A":[{"date":ix[0]-pd.Timedelta(days=35),"shares":9},
                     {"date":ix[0]-pd.Timedelta(days=34),"shares":1}]}
        def go(lots):
            return select_swap(ix[0],ix[1],pos,lots,score,alpha,elig,membership,close,
                               {"A":"x","B":"y","C":"z"},cfg,.26,9000,1.,10000)
        self.assertEqual(go(mature)["new"],"B")
        self.assertIsNone(go(young))
if __name__=="__main__":unittest.main()
