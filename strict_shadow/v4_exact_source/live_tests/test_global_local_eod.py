"""Regression tests for the active global roster freshness gate."""
import datetime as dt
import sys
import unittest
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from live_model import expected_local_close,check_required_local_closes,check_fx_freshness,expected_fx_close
from run_live import listing_calendar

class GlobalLocalEODTests(unittest.TestCase):
    def setUp(self):
        self.asof=dt.date(2026,11,27)
        self.now=dt.datetime(2026,11,28,5,tzinfo=dt.timezone.utc)
    def frame(self,day):
        return pd.DataFrame({'raw_close':[100.]},index=pd.to_datetime([day]))
    def test_europe_stale_two_days_is_blocked(self):
        audit=check_required_local_closes({'SAP.DE':self.frame('2026-11-25'),
             'ASML.AS':self.frame('2026-11-27')},['SAP.DE','ASML.AS'],self.asof,self.now)
        self.assertEqual(audit['status'],'BLOCKED')
        self.assertEqual(audit['stale_or_missing']['SAP.DE'],
                         {'expected':'2026-11-27','observed':'2026-11-25'})
        self.assertNotIn('ASML.AS',audit['stale_or_missing'])
    def test_thanksgiving_and_local_friday_closure(self):
        self.assertEqual(expected_local_close('SPY',dt.date(2026,11,26),self.now),
                         dt.date(2026,11,25))
        self.assertEqual(expected_local_close('2222.SR',self.asof,self.now),
                         dt.date(2026,11,26))
        audit=check_required_local_closes({'2222.SR':self.frame('2026-11-26')},
                                        ['2222.SR'],self.asof,self.now)
        self.assertEqual(audit['status'],'COMPLETE')
    def test_intraday_not_yet_released(self):
        before_close=dt.datetime(2026,11,27,12,tzinfo=dt.timezone.utc)
        self.assertEqual(expected_local_close('SAP.DE',self.asof,before_close),
                         dt.date(2026,11,26))
    def test_missing_and_unverified_calendar(self):
        audit=check_required_local_closes({},['SAP.DE','RELIANCE.NS'],self.asof,self.now)
        self.assertIn('SAP.DE',audit['stale_or_missing'])
        self.assertIn('RELIANCE.NS',audit['calendar_unverified'])
    def test_fx_one_weekday_degraded_two_blocked(self):
        def fx(day):
            return pd.DataFrame({'adj_close':[1.1]},index=pd.to_datetime([day]))
        audit=check_fx_freshness({'EURUSD=X':fx('2026-11-26'),
            'EURJPY=X':fx('2026-11-25'),'EURGBP=X':fx('2026-11-27')},
            ['EURUSD=X','EURJPY=X','EURGBP=X'],self.asof,self.now)
        self.assertEqual(audit['blocked'],['EURJPY=X'])
        self.assertEqual(audit['degraded'],['EURUSD=X'])
        self.assertEqual(audit['pairs']['EURGBP=X']['missed_weekdays'],0)
    def test_fx_weekend_not_a_missed_close(self):
        self.assertEqual(expected_fx_close(dt.date(2026,11,29),self.now),self.asof)
        frame=pd.DataFrame({'adj_close':[1.1]},index=pd.to_datetime(['2026-11-27']))
        audit=check_fx_freshness({'EURUSD=X':frame},['EURUSD=X'],dt.date(2026,11,29),self.now)
        self.assertEqual(audit['status'],'COMPLETE')
    def test_nasdaq_calendar_alias_disclosed(self):
        nas,_=listing_calendar('PANW','NMS')
        ny,_=listing_calendar('KO','NYQ')
        self.assertEqual(nas.session_open(pd.Timestamp('2026-11-30')),
                         ny.session_open(pd.Timestamp('2026-11-30')))
        self.assertEqual(nas.name,'XNYS') # pinned exchange_calendars alias

if __name__=='__main__':
    unittest.main()
