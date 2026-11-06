"""Engine checks only. ALL quotes here are SYNTHETIC TEST FIXTURES, not market data."""
import sys,tempfile,json,math,unittest,datetime as dt
from pathlib import Path
from unittest.mock import patch
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import engine as f
CFG=json.loads((HERE/'strategies'/'MA10_Official'/'config.json').read_text())

def fake_data(p1=100,p2=101):
    dates=['2026-%02d-28'%m for m in range(1,11)]
    endvals=CFG['monthly_prices_jan_oct_2026_eur']
    data={}
    for x in CFG['basket']:
        curr=x['currency']
        data[x['symbol']]={'bars':{'2026-11-04':p1,'2026-11-05':p2},'currency':curr,'dividends':{},'feed':'SYNTHETIC FIXTURE'}
    bench={d:float(v) for d,v in zip(dates,endvals)}
    bench.update({'2026-11-04':109.,'2026-11-05':110.})
    data['IUSQ.DE']={'bars':bench,'currency':'EUR','dividends':{},'feed':'SYNTHETIC FIXTURE'}
    data['EURUSD=X']={'bars':{'2026-11-04':1.12,'2026-11-05':1.12},'currency':'USD','feed':'SYNTHETIC FIXTURE'}
    data['EURSAR=X']={'bars':{'2026-11-04':4.2,'2026-11-05':4.2},'currency':'SAR','feed':'SYNTHETIC FIXTURE'}
    return data

def seed_state():
    return dict(started_at='2026-11-03',initialized=False,last_update='2026-11-03',cash_eur=10000.,positions={},order_count=0,risk_on=True,last_regime_month='2026-10',last_roster_year=2026,liabilities_eur=0.,tax_losses=[],benchmark={'shares':0.,'cash_eur':10000.,'cost_basis_eur':0.,'liabilities_eur':0.})
class Tests(unittest.TestCase):
    def test_yahoo_split_adjusted_close_is_nominalized(self):
        from unittest.mock import Mock
        def price(day):
            return int(dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp())
        raw={'chart':{'error':None,'result':[{
          'meta':{'exchangeTimezoneName':'America/New_York','currency':'USD'},
          'timestamp':[price('2026-11-04')+43200,price('2026-11-05')+43200],
          'indicators':{'quote':[{'close':[50.,55.]}]},
          'events':{'splits':{'event':{'date':price('2026-11-05')+43200,'numerator':2,'denominator':1}}}
        }]}}
        r=Mock();r.raise_for_status.return_value=None;r.json.return_value=raw
        with patch.object(f.requests,'get',return_value=r):
            quote=f.fetch_yahoo('AAPL',start='2026-11-03',end='2026-11-05')
        self.assertAlmostEqual(quote['bars']['2026-11-04'],100.)
        self.assertAlmostEqual(quote['bars']['2026-11-05'],55.)
        self.assertEqual(quote['splits']['2026-11-05'],2.)
    def test_spacex_identity_is_required_for_spcx(self):
        from unittest.mock import Mock
        raw={'chart':{'error':None,'result':[{
            'meta':{'exchangeTimezoneName':'America/New_York','currency':'USD',
                    'longName':'Unrelated Space Sector ETF'},
            'timestamp':[], 'indicators':{'quote':[{'close':[]}]}
        }]}}
        response=Mock();response.raise_for_status.return_value=None
        response.json.return_value=raw
        with patch.object(f.requests,'get',return_value=response):
            with self.assertRaisesRegex(RuntimeError,'SPCX_IDENTITY_NOT_VERIFIED'):
                f.fetch_yahoo('SPCX',start='2026-11-03',end='2026-11-05')
        raw['chart']['result'][0]['meta']['longName']='Space Exploration Technologies Corp.'
        # The verified corporate issuer passes the identity gate. The empty
        # synthetic fixture then reaches a normal empty OHLC result.
        with patch.object(f.requests,'get',return_value=response):
            rec=f.fetch_yahoo('SPCX',start='2026-11-03',end='2026-11-05')
        self.assertEqual(rec['symbol'],'SPCX')
        self.assertEqual(rec['currency'],'USD')
    def test_spacex_wrong_quote_currency_blocked(self):
        from unittest.mock import Mock
        raw={'chart':{'error':None,'result':[{
            'meta':{'exchangeTimezoneName':'America/New_York','currency':'EUR',
                    'longName':'Space Exploration Technologies Corp.'},
            'timestamp':[], 'indicators':{'quote':[{'close':[]}]}
        }]}}
        response=Mock();response.raise_for_status.return_value=None
        response.json.return_value=raw
        with patch.object(f.requests,'get',return_value=response):
            with self.assertRaisesRegex(RuntimeError,'CURRENCY_OR_VENUE_MISMATCH'):
                f.fetch_yahoo('SPCX',start='2026-11-03',end='2026-11-05')
    def test_ma10(self):
        state,last,ma=f.ma10_state(fake_data()['IUSQ.DE']['bars'],'2026-11-04',False,.01)
        self.assertTrue(state);self.assertAlmostEqual(last,108.26,4);self.assertAlmostEqual(ma,101.754,3)
    def test_initial_orders_and_commissions(self):
        s=seed_state();orders,nav=f.step(s,CFG,fake_data(),'2026-11-04')
        self.assertEqual(len(orders),11);self.assertEqual(s['order_count'],10)
        self.assertEqual(len(s['positions']),10);self.assertAlmostEqual(sum(o['commission_eur'] for o in orders),11)
        self.assertLess(nav['portfolio_nav_eur'],10000)
        self.assertLess(nav['benchmark_nav_eur'],10000)
        self.assertEqual(nav['positions'],10)
    def test_idempotence_and_bollo(self):
        s=seed_state();d=fake_data();f.step(s,CFG,d,'2026-11-04')
        v=f.step(s,CFG,d,'2026-11-05');self.assertEqual(v[0],[])
        self.assertGreater(s['liabilities_eur'],0.)
        self.assertIsNone(f.step(s,CFG,d,'2026-11-05')[1])
    def test_no_future_price_execution(self):
        s=seed_state();data=fake_data();data['TSLA']['bars'].pop('2026-11-04')
        with self.assertRaises(ValueError):f.step(s,CFG,data,'2026-11-04')
        self.assertFalse(s['initialized'])
    def test_sale_tax_offsets(self):
        s={'tax_losses':[]}
        self.assertEqual(f.apply_sale_tax(s,-120,2026,.26),0)
        self.assertAlmostEqual(f.apply_sale_tax(s,100,2027,.26),0)
        self.assertAlmostEqual(f.apply_sale_tax(s,100,2027,.26),20.8)
        self.assertEqual(f.apply_sale_tax(s,-10,2028,.26),0)
        self.assertAlmostEqual(f.apply_sale_tax(s,10,2033,.26),2.6)
    def test_snapshot_not_future(self):
        d=fake_data(); d['EURUSD=X']['bars']['2026-11-07']=.01
        s=seed_state();f.step(s,CFG,d,'2026-11-04')
        self.assertGreater(s['positions']['AAPL']['shares'],0)
if __name__=='__main__':unittest.main(verbosity=2)