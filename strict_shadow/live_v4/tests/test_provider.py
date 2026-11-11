"""Offline provider tests: exact session and FX lookahead protections."""
import datetime as dt
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from provider import YahooChart, Bar, DataUnavailable

D=dt.date.fromisoformat
class InMemory(YahooChart):
    def __init__(self,series):self.series=series
    def history(self,symbol,start,end):return {d:b for d,b in self.series.get(symbol,{}).items() if start<=d<=end}

class ProviderTests(unittest.TestCase):
    def test_open_uses_prior_day_fx(self):
        data={'TST':{D('2026-11-06'):Bar(D('2026-11-06'),120.,125.,'USD','TST','mkt')},
              'EURUSD=X':{
                  D('2026-11-05'):Bar(D('2026-11-05'),1.1,1.2,'USD','EURUSD=X','fxprior'),
                  D('2026-11-06'):Bar(D('2026-11-06'),1.3,1.5,'USD','EURUSD=X','fxfuture')}}
        q=InMemory(data).eur_quote('TST',D('2026-11-06'),'open')
        self.assertAlmostEqual(q['price_eur'],100.)
        self.assertEqual(q['fx_session'],'2026-11-05')
    def test_exact_session_required(self):
        provider=InMemory({'ABC':{D('2026-11-05'):Bar(D('2026-11-05'),1,2,'EUR','ABC','h')}})
        with self.assertRaises(DataUnavailable):provider.bar('ABC',D('2026-11-06'))
    def test_stale_fx_rejected(self):
        data={'ABC':{D('2026-11-15'):Bar(D('2026-11-15'),1,2,'USD','ABC','m')},
              'EURUSD=X':{D('2026-11-05'):Bar(D('2026-11-05'),1,1.2,'USD','EURUSD=X','fx')}}
        with self.assertRaises(DataUnavailable):InMemory(data).eur_quote('ABC',D('2026-11-15'),'close')
    def test_valuation_uses_published_prior_close_for_holiday(self):
        data={'JP.T':{D('2026-11-05'):Bar(D('2026-11-05'),200,205,'JPY','JP.T','oldjp')},
              'EURJPY=X':{D('2026-11-04'):Bar(D('2026-11-04'),160,160,'JPY','EURJPY=X','oldfx'),
                          D('2026-11-05'):Bar(D('2026-11-05'),170,170,'JPY','EURJPY=X','unavailable_at_local_equity_close'),
                          D('2026-11-06'):Bar(D('2026-11-06'),180,180,'JPY','EURJPY=X','futurefx')}}
        result=InMemory(data).latest_published_eur_quote('JP.T',D('2026-11-06'))
        self.assertEqual(result['valuation_quote_session'],'2026-11-05')
        self.assertEqual(result['valuation_staleness_days'],1)
        self.assertFalse(result['is_exact_valuation_session'])
        self.assertAlmostEqual(result['price_eur'],205/160)
        self.assertEqual(result['fx_session'],'2026-11-04')
    def test_valuation_rejects_stale_market(self):
        data={'ABC':{D('2026-10-28'):Bar(D('2026-10-28'),10,11,'EUR','ABC','x')}}
        with self.assertRaises(DataUnavailable):
            InMemory(data).latest_published_eur_quote('ABC',D('2026-11-06'),max_age_days=5)
    def test_pence_metadata_factor(self):
        data={'ABC.L':{D('2026-11-05'):Bar(D('2026-11-05'),200,202,'GBP','ABC.L','m',0.01)},
              'EURGBP=X':{D('2026-11-04'):Bar(D('2026-11-04'),0.8,0.8,'GBP','EURGBP=X','prior'),
                          D('2026-11-05'):Bar(D('2026-11-05'),0.85,0.85,'GBP','EURGBP=X','later_same_day')}}
        q=InMemory(data).eur_quote('ABC.L',D('2026-11-05'),'close')
        self.assertAlmostEqual(q['price_eur'],2.525)
        self.assertEqual(q['fx_session'],'2026-11-04')
if __name__=='__main__':unittest.main()
