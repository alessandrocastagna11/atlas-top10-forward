"""SYNTHETIC DATA ONLY: zero real investments/returns."""
import unittest,sys,copy,math,random
from pathlib import Path
from datetime import date,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import shadow_engine as e

def fixture():
    # Through Oct 2026, with 12+ completed months and >=20 daily observations.
    start=date(2025,8,1);end=date(2026,12,7);bench={}
    t=start;price=90.
    while t<=end:
        if t.weekday()<5:
            price*=1.00035
            bench[str(t)]={'open':price*.999,'close':price,'volume':100000}
        t+=timedelta(days=1)
    stock={}
    for s in e.BASKET:
        dates={}
        t=date(2026,10,20)
        while t<=end:
            if (t.weekday()<5 if s!='2222.SR' else t.weekday() in (6,0,1,2,3)):
                dates[str(t)]={'open':100.,'close':100.,'volume':1000}
            t+=timedelta(days=1)
        stock[s]={'bars':dates,'currency':e.CURRENCIES[s],'splits':{},'dividends':{},'feed':'SYNTHETIC'}
    stock[e.BENCH]={'bars':bench,'currency':'EUR','splits':{},'dividends':{},'feed':'SYNTHETIC'}
    fx={}
    t=date(2026,10,20)
    while t<=end:
        if t.weekday()<5:fx[str(t)]={'open':1.12,'close':1.12,'volume':1}
        t+=timedelta(days=1)
    stock['EURUSD=X']={'bars':fx,'currency':'USD','splits':{},'dividends':{},'feed':'SYNTHETIC'}
    stock['EURSAR=X']={'bars':{d:{'open':4.2,'close':4.2,'volume':1} for d in fx},'currency':'SAR','splits':{},'dividends':{},'feed':'SYNTHETIC'}
    return stock

class RiskTests(unittest.TestCase):
    def test_signal_only_previous_full_month(self):
        d=fixture();s=e.target_risk('MA10_Open',d,'2026-11-05')
        self.assertEqual(s['signal_month'],'2026-10')
        d[e.BENCH]['bars']['2026-11-04']['close']=.01
        z=e.target_risk('MA10_Open',d,'2026-11-05')
        self.assertAlmostEqual(s['metric'],z['metric'])
    def test_dual_signal_known_prior(self):
        d=fixture();r=e.target_risk('MA6_MA12_Open',d,'2026-11-05')
        self.assertEqual(r['signal_month'],'2026-10')
    def test_adaptive_vol_downscales(self):
        d=fixture();a=e.target_risk('Adaptive_Risk',d,'2026-11-05')
        self.assertGreater(a['exposure'],0.9)
        t=date(2026,9,15)
        i=0
        while t<date(2026,11,1):
            if str(t) in d[e.BENCH]['bars']:
                d[e.BENCH]['bars'][str(t)]['close']*=1.10 if i%2 else .90
                i+=1
            t+=timedelta(days=1)
        b=e.target_risk('Adaptive_Risk',d,'2026-11-05')
        self.assertLess(b['exposure'],a['exposure'])
    def test_prior_day_fx_for_open(self):
        d=fixture();d['EURUSD=X']['bars']['2026-11-05']['close']=200.
        s=e.seed('MA10_Open')
        e.advance(s,d,'2026-11-05');e.validate(s)
        o=next(x for x in s['orders'] if x['symbol']=='AAPL')
        self.assertEqual(o['fx_asof'],'2026-11-04')
        self.assertAlmostEqual(o['eur_per_local'],1/1.12)
    def test_independent_calendar_queue(self):
        d=fixture();d['2222.SR']['bars'].pop('2026-11-05')
        s=e.seed('MA10_Open')
        first=e.advance(s,d,'2026-11-05')
        self.assertEqual(len(s['orders']),9)
        self.assertEqual(len(s['pending']),1)
        self.assertEqual(s['pending'][0]['symbol'],'2222.SR')
        # Saudi trading Sunday; Xetra/Nasdaq closed; risk decision not reevaluated.
        for day in ('2026-11-06','2026-11-08'):
            e.advance(s,d,day)
        self.assertEqual(len(s['orders']),10)
        self.assertEqual(s['orders'][-1]['symbol'],'2222.SR')
        e.validate(s)
    def test_no_lookahead_trade_if_open_missing(self):
        d=fixture();d['TSLA']['bars'].pop('2026-11-05')
        s=e.seed('MA10_Open');e.advance(s,d,'2026-11-05')
        self.assertEqual(len(s['orders']),9)
        self.assertNotIn('TSLA',s['positions'])
    def test_idempotent_date(self):
        d=fixture();s=e.seed('MA10_Open');e.advance(s,d,'2026-11-05')
        n=len(s['orders']);self.assertIsNone(e.advance(s,d,'2026-11-05'))
        self.assertEqual(len(s['orders']),n)
    def test_zero_orders_before_first_entry(self):
        d=fixture();s=e.seed('MA10_Open')
        self.assertEqual(len(s['orders']),0)
        self.assertEqual(s['nav'][-1]['nav'],10000.)
    def test_cost_basis_partial_sell_and_tax(self):
        s=e.seed('MA10_Open');s['tax_losses']=[]
        self.assertEqual(e.sale_tax(s,-200,2026),0)
        self.assertEqual(e.sale_tax(s,100,2027),0)
        self.assertAlmostEqual(e.sale_tax(s,150,2027),13.0)
        self.assertAlmostEqual(e.sale_tax(s,10,2033),2.6)
    def test_dividend_proxy_before_open(self):
        d=fixture();s=e.seed('MA10_Open');e.advance(s,d,'2026-11-05')
        shares=s['positions']['AAPL']['shares']
        d['AAPL']['dividends']={'2026-11-06':2.0}
        e.advance(s,d,'2026-11-06')
        self.assertAlmostEqual(s['dividend_events'][0]['tax'],shares*2/1.12*.26,4)
    def test_split_preserves_value(self):
        d=fixture();s=e.seed('MA10_Open');e.advance(s,d,'2026-11-05')
        p=s['positions']['AAPL'];shares=p['shares'];basis=p['basis'];nav=s['nav'][-1]['nav'];count=len(s['orders'])
        d['AAPL']['splits']['2026-11-06']=2
        for a in ['open','close']:d['AAPL']['bars']['2026-11-06'][a]/=2
        e.advance(s,d,'2026-11-06')
        self.assertEqual(len(s['orders']),count)
        self.assertAlmostEqual(s['positions']['AAPL']['shares'],shares*2)
        self.assertAlmostEqual(s['positions']['AAPL']['basis'],basis)
        self.assertAlmostEqual(s['nav'][-1]['nav'],nav,delta=5.0)
    def test_monthly_rebalance_without_future(self):
        d=fixture();s=e.seed('Adaptive_Risk');e.advance(s,d,'2026-11-05')
        e.advance(s,d,'2026-11-06')
        self.assertEqual(s['signal_log'][-1]['signal_month'],'2026-10')
        self.assertEqual(len(s['signal_log']),1)
    def test_annual_roster_tasks_do_not_double_spend(self):
        d=fixture();s=e.seed('MA10_Open');e.advance(s,d,'2026-11-05')
        sig=e.target_risk('MA10_Open',d,'2026-11-06')
        roster=e.BASKET[2:]+['NEW_A','NEW_B']
        tasks=e.exposure_tasks(s,d,'2026-11-06',sig,roster)
        purchases=sum(t['budget_eur'] for t in tasks if t['side']=='BUY')
        sales=sum(t['budget_eur'] for t in tasks if t['side']=='SELL')
        self.assertGreater(purchases,0)
        self.assertGreater(sales,0)
        self.assertLess(abs(purchases-sales),20)
    def test_missing_fx_fails_open(self):
        d=fixture();d['EURUSD=X']['bars']={'2026-11-05':{'open':1.1,'close':1.1}}
        s=e.seed('MA10_Open')
        with self.assertRaises(ValueError):e.advance(s,d,'2026-11-05')
    def test_requires_exact_yahoo_identity(self):
        # Metadata check exists in the live download; no live HTTP in tests.
        self.assertEqual(e.SECURITY_IDS['SPCX']['first_listed'],'2026-06-12')
        self.assertEqual(e.SECURITY_IDS['SPCX']['isin'],'US84615Q1031')

if __name__=='__main__':unittest.main()