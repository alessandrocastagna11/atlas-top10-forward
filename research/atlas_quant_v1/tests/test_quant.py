"""Atlas Quant 1.0: no live/trading connections. 12 integrity tests."""
import sys,unittest
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import quant_research as q

class QuantUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src=q.load_frozen()
        cls.report=Path(__file__).resolve().parents[1]/'reports'

    def test_asset_scope(self):
        self.assertGreaterEqual(len(self.src['assets']),180)
        self.assertTrue(all(set(p.currency.dropna().unique())<={'USD','EUR'} for p in self.src['assets'].values()))

    def test_archival_roster_is_prior_year(self):
        r=self.src['roster'];x=r[r.effective_year.between(2016,2025)]
        self.assertTrue((x.ranking_year==x.effective_year-1).all())

    def test_trading_open_uses_previous_fx(self):
        p=self.src['assets']['apple inc'];date=pd.Timestamp('2020-04-01')
        if date not in p.index:self.skipTest('Apple did not trade that day')
        fx=self.src['fx'];prev=float(fx.loc[date-pd.Timedelta(days=1)]);today=float(fx.loc[date])
        self.assertAlmostEqual(p.loc[date,'open_eur']*prev,p.loc[date,'open'],places=6)
        self.assertAlmostEqual(p.loc[date,'close_eur']*today,p.loc[date,'close'],places=6)

    def test_factor_signs(self):
        df,_=q.generate_factors(self.src,pd.Timestamp('2017-01-31'),2017)
        self.assertGreater(len(df),35)
        for col,sign in [('mom12_1',1),('mom6_1',1),('vol126',-1),('down126',-1)]:
            v=df[[col,col+'_rank']].sort_values(col)
            diff=v[col+'_rank'].iloc[-1]-v[col+'_rank'].iloc[0]
            self.assertGreater(diff*sign,0)

    def test_factors_are_not_future_sensitive(self):
        date=pd.Timestamp('2018-01-31')
        a,_=q.generate_factors(self.src,date,2018)
        probe='apple inc';clone=dict(self.src)
        clone['assets']=dict(self.src['assets']);clone['factor_cache']={}
        altered=self.src['assets'][probe].copy()
        altered.loc[altered.index>date,'adj_eur']*=1000.
        clone['assets'][probe]=altered
        b,_=q.generate_factors(clone,date,2018)
        cols=['issuer','score','mom12_1','mom6_1','vol126','down126']
        pd.testing.assert_frame_equal(a[cols],b[cols],check_exact=True)

    def test_selection_sector_limit(self):
        f,_=q.generate_factors(self.src,pd.Timestamp('2024-01-31'),2024)
        selected=q.select_names(f,20,5)
        self.assertEqual(len(selected),20)
        d=f.set_index('issuer').loc[selected]
        self.assertLessEqual(d.sector.value_counts().max(),5)

    def test_weight_projection_constraints(self):
        w=q.project_weights(np.linspace(.4,1.4,20),1.,['A']*5+['B']*5+['C']*5+['D']*5)
        self.assertAlmostEqual(w.sum(),1.,places=6)
        self.assertGreaterEqual(min(w),0)
        self.assertLessEqual(max(w),.09+1e-7)
        for st in range(0,20,5):self.assertLessEqual(sum(w[st:st+5]),.3+1e-7)

    def test_tax_compensation_and_expiry(self):
        a=q.TaxLedger();self.assertEqual(a.apply(-1000,2018),0)
        self.assertAlmostEqual(a.apply(300,2019),0)
        self.assertAlmostEqual(a.apply(1000,2020),78.)
        b=q.TaxLedger();b.apply(-200,2018)
        self.assertAlmostEqual(b.apply(500,2023),130.)

    def test_paper_orders_never_use_future_signal(self):
        for name in ['Top10_Eligible_BH','Top10_Eligible_MA6_12','Quant20_Momentum','Quant20_TrendVol']:
            f=self.report/(name+'_orders.csv')
            if not f.exists():self.skipTest('Run the archive backtest before audit')
            d=pd.read_csv(f)
            self.assertTrue((pd.to_datetime(d.decision_asof)<pd.to_datetime(d.date)).all())
            self.assertTrue((d.shares>0).all())
            self.assertTrue((d.fee_eur==1).all())
            self.assertTrue((d.cash_after>=-1e-5).all())

    def test_simulation_nav_invariants(self):
        for name in ['Top10_Eligible_BH','Top10_Eligible_MA6_12','Quant20_Momentum','Quant20_TrendVol']:
            f=self.report/(name+'_nav.csv')
            if not f.exists():self.skipTest('Run the archive backtest before audit')
            d=pd.read_csv(f)
            self.assertEqual(len(d),2550)
            self.assertTrue((d.cash_eur>=-1e-5).all())
            self.assertTrue((d.positions>=0).all())
            self.assertTrue((d.nav_liquidatable_eur>0).all())
            self.assertTrue((d.pending_count>=0).all())

    def test_report_not_live(self):
        self.assertFalse(q.MODES[0].startswith('LIVE_'))
        self.assertNotIn('/atlas-top10-forward/',str(q.ROOT))

    def test_benchmark_price_eur_conversion(self):
        date=pd.Timestamp('2020-01-02')
        p=self.src['market'].loc[date]
        original=pd.read_csv(q.ARCHIVED_FX/'global_benchmark_proxy.csv',parse_dates=['date']).set_index('date')
        self.assertAlmostEqual(p*self.src['fx'].loc[date],original.loc[date,'global_proxy'],places=6)

if __name__=='__main__':unittest.main()