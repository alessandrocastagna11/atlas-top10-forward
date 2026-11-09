"""Proof that risk-only sells cannot bypass the per-purchase 35-calendar-day lock."""
import sys,unittest,dataclasses
from pathlib import Path
import numpy as np,pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from types import SimpleNamespace
import live_model,live_simulator

class StrictLotAgeTests(unittest.TestCase):
    def make_case(self,min_days=35):
        t9=live_model.t9;v2=live_model.v2
        live_model.v1.TICKER_TO_SECTOR['AAA']='XLK' 
        days=pd.bdate_range('2025-01-01','2026-04-30')
        px=np.linspace(110,150,len(days))
        close=pd.DataFrame({'AAA':px},index=days)
        raw=t9.RawMarketData(
            open_eur=close*.999,close_eur=close,
            distribution_eur_per_share=pd.DataFrame(0.,index=days,columns=close.columns),
            taxable_distribution=pd.DataFrame(False,index=days,columns=close.columns),
            events=pd.DataFrame(columns=['date','ticker','classification']))
        # Decided bullish at entry, then bearish in following week: risk reduction
        # must not sell the lot until 35 calendar days have passed.
        regime=pd.Series(1.,index=days)
        regime.loc[regime.index>=pd.Timestamp('2026-01-09')]=0.
        cfg=dataclasses.replace(v2.V2Config(),max_positions=1,minimum_holding_calendar_days=min_days,
              decision_interval_weeks=5,minimum_trade_eur=25,rebalance_band_weight=.01,
              target_volatility=.9,covariance_lookback=63)
        def selector(decision,score,rank,eligible,pos,cfg):return ['AAA']
        def sizing(relative,exposure,cfg,decision_date=None):return pd.Series({'AAA':min(.8,exposure)})
        return (SimpleNamespace(close_eur=close),pd.DataFrame(1.,index=days,columns=['AAA']),
             pd.DataFrame(True,index=days,columns=['AAA']),pd.DataFrame(True,index=days,columns=['AAA']),
             regime,pd.Series(.0,index=days),None,cfg,raw,'2026-01-02',10000.,'test_hold35',
             t9.TaxConfig('italian_tax',True)),dict(weekly_risk_overlay=pd.Series(1.,index=days),
            cash_reserve_fraction=.01,name_selector=selector,target_weight_builder=sizing,
            liquidate_on_final_session=False)
    def test_all_sells_including_risk_wait_35_days(self):
        args,kwargs=self.make_case()
        result=live_simulator.simulate_tax_aware(*args,**kwargs)
        self.assertEqual(result.strict_hold_calendar_days,35)
        self.assertGreater(len(result.orders),0)
        self.assertGreater(result.blocked_sell_count,0)
        self.assertTrue(result.realized_lots.empty or (result.realized_lots.calendar_days>=35).all())
        self.assertNotIn('end_of_test',result.orders.get('reason',pd.Series(dtype=str)).tolist())
    def test_guard_rejects_changing_the_minimum_away_from_backtest(self):
        args,kwargs=self.make_case(min_days=21)
        with self.assertRaisesRegex(ValueError,'exactly 35'):
            live_simulator.simulate_tax_aware(*args,**kwargs)

if __name__=='__main__':unittest.main()
