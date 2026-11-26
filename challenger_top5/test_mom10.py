import unittest
import datetime as dt
import numpy as np
import pandas as pd
from run import score_mom10, same_targets, reconcile_orders, exchange_plan, holiday_correction

class TestRankMom10(unittest.TestCase):
    def setUp(self):
        self.index=pd.bdate_range('2023-01-02',periods=120)
        n=np.arange(120)
        self.close=pd.DataFrame({'A':100*1.003**n,'B':100*1.001**n,'C':100*1.009**n},index=self.index)
        self.base=pd.DataFrame(.5,index=self.index,columns=list(self.close))
        self.path=self.base+.08
        self.eligible=pd.DataFrame(True,index=self.index,columns=list(self.close))
        self.eligible['C']=False
    def test_exact_formula_and_eligible_percentile(self):
        s=score_mom10(self.base,self.path,self.close,self.eligible,self.eligible)
        self.assertAlmostEqual(s['A'].iloc[-1],.68)
        self.assertAlmostEqual(s['B'].iloc[-1],.58)
        self.assertTrue(pd.isna(s['C'].iloc[-1]))
        self.assertTrue(s.iloc[:84].isna().all().all())
    def test_recent_21_bars_not_used(self):
        altered=self.close.copy()
        altered.iloc[-21:,0]*=30
        a=score_mom10(self.base,self.path,self.close,self.eligible,self.eligible)
        b=score_mom10(self.base,self.path,altered,self.eligible,self.eligible)
        pd.testing.assert_series_equal(a.iloc[-1],b.iloc[-1])
    def test_future_bars_cannot_change_past(self):
        altered=self.close.copy()
        altered.iloc[105:]*=11
        a=score_mom10(self.base,self.path,self.close,self.eligible,self.eligible)
        b=score_mom10(self.base,self.path,altered,self.eligible,self.eligible)
        pd.testing.assert_frame_equal(a.iloc[:105],b.iloc[:105])
    def test_revised_target_detection(self):
        p=[{'ticker':'A','target_weight':.20},{'ticker':'B','target_weight':.18}]
        self.assertTrue(same_targets(p,p))
        self.assertFalse(same_targets(p,[{'ticker':'B','target_weight':.20},{'ticker':'A','target_weight':.18}]))
        self.assertFalse(same_targets(p,[{'ticker':'A','target_weight':.19},{'ticker':'B','target_weight':.18}]))


class TestExchangeCalendar(unittest.TestCase):
    def test_thanksgiving_and_original_proof(self):
        utc=dt.timezone.utc
        recorded=dt.datetime(2026,11,26,5,32,tzinfo=utc)
        day,opens,eligible=exchange_plan(dt.date(2026,11,25),{'KO','CB'},{},recorded)
        self.assertEqual(str(day),'2026-11-27')
        self.assertTrue(eligible)
        self.assertEqual(opens['KO']['utc'],'2026-11-27T14:30:00+00:00')
        prior={'decision_session':'2026-11-25','execution_session':'2026-11-26',
               'first_recorded_at_utc':recorded.isoformat(),'targets':[{'ticker':'KO','target_weight':.2}],
               'candidate_exchange_opens':{'KO':{'date':'2026-11-26'}},'preopen_eligible':True}
        fixed=holiday_correction(prior,dt.datetime(2026,11,26,20,tzinfo=utc))
        self.assertEqual(fixed['execution_session'],'2026-11-27')
        self.assertEqual(prior['execution_session'],'2026-11-26')
        self.assertEqual(fixed['targets'],prior['targets'])
        self.assertIsNone(holiday_correction(prior,dt.datetime(2026,11,27,15,tzinfo=utc)))

class TestImmutablePaperLedger(unittest.TestCase):
    @staticmethod
    def order(ticker,shares=1.,date='2026-11-18T00:00:00'):
        return {
            'scenario':'italian_tax','strategy':'ATLAS_TOP5_ALWAYS_RANK_MOM10_V1',
            'decision_date':'2026-11-17T00:00:00','date':date,
            'action':'BUY','ticker':ticker,'shares':shares,
            'reference_price_eur':100.,'execution_price_eur':100.1,
            'reference_value_eur':100.,'fee_eur':1.,
            'slippage_bps_per_side':10,'capital_gains_tax_eur':0.,
            'reason':'target_rebalance'
        }

    def test_permutation_of_committed_equal_weight_fills_is_harmless(self):
        saved=[self.order(t) for t in ('PANW','CB','MRK','KO','TXN')]
        replay=[saved[i].copy() for i in (0,3,2,1,4)]
        self.assertEqual(reconcile_orders(replay,saved),saved)

    def test_revised_shares_must_still_block(self):
        saved=[self.order('PANW'),self.order('KO')]
        replay=[x.copy() for x in reversed(saved)]
        replay[0]['shares']=1.1
        with self.assertRaisesRegex(ValueError,'Prior paper order revised: shares'):
            reconcile_orders(replay,saved)

    def test_revised_execution_price_must_still_block(self):
        saved=[self.order('KO')]
        replay=[self.order('KO')]
        replay[0]['execution_price_eur']=102.
        with self.assertRaisesRegex(ValueError,'Prior paper order revised: execution_price_eur'):
            reconcile_orders(replay,saved)

    def test_missing_committed_fill_must_block(self):
        saved=[self.order('PANW'),self.order('KO')]
        with self.assertRaisesRegex(ValueError,'Replay removed committed orders'):
            reconcile_orders([self.order('PANW')],saved)

    def test_extra_backdated_fill_must_block(self):
        saved=[self.order('PANW')]
        replay=saved+[self.order('KO')]
        with self.assertRaisesRegex(ValueError,'backdated'):
            reconcile_orders(replay,saved)

    def test_extra_forward_fill_appends_without_reordering_history(self):
        saved=[self.order('PANW'),self.order('KO')]
        next_fill=self.order('MRK',date='2026-12-23T00:00:00')
        replay=[saved[1].copy(),next_fill,saved[0].copy()]
        self.assertEqual(reconcile_orders(replay,saved),saved+[next_fill])

    def test_missing_identity_same_count_must_block(self):
        saved=[self.order('KO')]
        with self.assertRaisesRegex(ValueError,'identity revised'):
            reconcile_orders([self.order('MRK')],saved)

    def test_duplicate_matching_fills_are_consumed_one_to_one(self):
        one=self.order('KO')
        self.assertEqual(reconcile_orders([one.copy(),one.copy()],[one,one]),[one,one])
        changed=one.copy()
        changed['fee_eur']=2.
        with self.assertRaisesRegex(ValueError,'fee_eur'):
            reconcile_orders([one.copy(),changed],[one,one])


if __name__=='__main__':
    unittest.main()
