"""Auditable invariants for historical v0.8 proxy. No external market requests."""
import unittest
from pathlib import Path
import numpy as np
import pandas as pd
import backtest_v08_proxy as b

class BacktestProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.roster,cls.mark,cls.exact,cls.bench,cls.cash,cls.div=b.v5.load_same_universe()
        cls.monthly=cls.bench.resample('ME').last()

    def test_annual_signal_uses_only_prior_month(self):
        t=pd.Timestamp('2020-05-01')
        a=b.calc_signal('MA10',self.monthly,self.bench,t,True)
        m=self.monthly.copy()
        m.loc['2020-05-31']=1_000_000. # unobservable future price must not matter
        c=b.calc_signal('MA10',m,self.bench,t,True)
        self.assertEqual(a['signal_month'],'2020-04-30')
        self.assertEqual(a['exposure'],c['exposure'])
        self.assertEqual(a['metric'],c['metric'])

    def test_adaptive_exposure_within_bound(self):
        for t in [pd.Timestamp('2018-12-03'),pd.Timestamp('2020-04-01'),pd.Timestamp('2023-07-03')]:
            x=b.calc_signal('Adaptive_Risk',self.monthly,self.bench,t,True)
            self.assertGreaterEqual(x['exposure'],0.)
            self.assertLessEqual(x['exposure'],1.)
            self.assertLessEqual(x['signal_month'][:7],(t-pd.Timedelta(days=1)).strftime('%Y-%m'))

    def test_tax_ledger_loss_expiry(self):
        l=b.old.ItalianTaxLedger(.26)
        self.assertEqual(l.sale(-100,2016)[0],0)
        self.assertEqual(l.sale(50,2020)[0],0)
        self.assertAlmostEqual(l.sale(100,2021)[0],26)

    def test_output_files_and_no_churn(self):
        folder=Path(__file__).resolve().parent
        eq=pd.read_csv(folder/'EQUITY.csv',parse_dates=['date']).set_index('date')
        self.assertEqual(len(eq),len(self.bench.loc[b.START:b.END]))
        self.assertTrue(eq.gt(0).all().all())
        buy=pd.read_csv(folder/'BuyHold_Top10_ORDERS.csv')
        self.assertEqual(len(buy),44)
        self.assertEqual(buy.query('side=="BUY"').shape[0],27)
        self.assertEqual(buy.query('side=="SELL"').shape[0],17)
        self.assertFalse(buy.duplicated(['date','company','side']).any())
        self.assertTrue(set(buy.reason).issubset({'entrant','roster/exit'}))
        for model in b.VARIANTS:
            d=pd.read_csv(folder/f'{model}_ORDERS.csv')
            self.assertTrue((d['date']>=d['issued_on']).all())
            self.assertTrue((d['commission_eur']==b.FEE).all())
            self.assertTrue(d['tax_eur'].ge(0).all())
            self.assertTrue(d['shares'].gt(0).all())
            self.assertFalse(d.duplicated(['date','company','side']).any())
        self.assertLessEqual(float(eq['Adaptive_Risk'].min()),float(eq['Adaptive_Risk'].max()))

if __name__=='__main__':unittest.main()