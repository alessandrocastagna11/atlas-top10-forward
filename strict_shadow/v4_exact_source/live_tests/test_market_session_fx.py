"""Offline 2026 DST, non-US venue cutoffs, FX causal conversion tests."""
from __future__ import annotations
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

SRC=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SRC))
sys.path.insert(0,str(SRC.parent/'live_v4'))
import market_sessions as session
import provider
import dataset_daily as daily
import live_model

UTC=dt.timezone.utc
def at(s):return dt.datetime.fromisoformat(s.replace('Z','+00:00'))

class GlobalSessionAndFXTests(unittest.TestCase):
    def test_us_dst_summer_and_winter_and_no_partial_candle(self):
        d=dt.date(2026,11,11)   # EST: 16:00 + 90min = 22:30Z
        self.assertEqual(session.release_time_utc('AAPL',d),at('2026-11-11T22:30:00Z'))
        self.assertFalse(session.is_final_bar('AAPL',d,at('2026-11-11T22:29:59Z')))
        self.assertTrue(session.is_final_bar('AAPL',d,at('2026-11-11T22:30:01Z')))
        d=dt.date(2026,6,10)    # EDT: 16:00 + 90min = 21:30Z
        self.assertEqual(session.release_time_utc('AAPL',d),at('2026-06-10T21:30:00Z'))

    def test_london_dst_and_asia_no_false_ny_equivalence(self):
        self.assertEqual(session.release_time_utc('AZN.L',dt.date(2026,11,11)),
                         at('2026-11-11T17:35:00Z'))
        self.assertEqual(session.release_time_utc('AZN.L',dt.date(2026,6,10)),
                         at('2026-06-10T16:35:00Z'))
        tokyo=dt.date(2026,11,12)
        self.assertFalse(session.is_final_bar('7203.T',tokyo,at('2026-11-12T07:29:00Z')))
        self.assertTrue(session.is_final_bar('7203.T',tokyo,at('2026-11-12T07:31:00Z')))
        self.assertFalse(session.is_final_bar('0700.HK',tokyo,at('2026-11-12T09:09:00Z')))
        self.assertTrue(session.is_final_bar('0700.HK',tokyo,at('2026-11-12T09:11:00Z')))
        self.assertEqual(session.release_time_utc('2222.SR',dt.date(2026,11,15)),
                         at('2026-11-15T13:20:00Z'))

    def test_unknown_market_fails_closed_no_invented_calendar(self):
        with self.assertRaisesRegex(ValueError,'Unknown market'):
            session.release_time_utc('ABC.ZZ',dt.date(2026,11,11))
        self.assertEqual(session.venue('BRK.B')[0],'USA')

    def test_fx_sunday_requires_monday_rollover(self):
        sun=dt.date(2026,11,15)
        self.assertFalse(session.is_final_bar('EURUSD=X',sun,at('2026-11-15T23:59:00Z')))
        self.assertEqual(session.release_time_utc('EURUSD=X',sun),
                         at('2026-11-16T22:30:00Z'))

    def test_pence_to_pounds_exactly_once(self):
        self.assertEqual(session.normalize_currency('GBp'),('GBP',.01))
        self.assertEqual(session.normalize_currency('GBX'),('GBP',.01))
        self.assertEqual(session.normalize_currency('GBP'),('GBP',1.))
        self.assertEqual(live_model._quote_unit_scale('AZN.L','GBp'),.01)
        with self.assertRaises(ValueError):
            session.normalize_currency('NOT_A_CURRENCY')

    def test_eur_price_uses_previous_settled_fx_not_future_same_day(self):
        mon=dt.date(2026,11,9);tue=dt.date(2026,11,10);sun=dt.date(2026,11,8)
        bars={
           mon:provider.Bar(mon,1.15,1.15,'GBP','EURGBP=X','FX_MON'),
           sun:provider.Bar(sun,1.99,1.99,'GBP','EURGBP=X','PROVISIONAL_SUNDAY'),
           tue:provider.Bar(tue,1.20,1.20,'GBP','EURGBP=X','FX_TUE')}
        class Stub:
            def history(self,symbol,start,end):
                if symbol=='EURGBP=X':return bars
                raise AssertionError('unexpected '+symbol)
            def bar(self,symbol,date):
                return provider.Bar(tue,100.,100.,'GBP',symbol,'AZN_BAR',
                                    unit_scale=.01)
            def fx_units_per_eur(self,currency,session,for_open=False):
                return provider.YahooChart.fx_units_per_eur(self,currency,session,
                                                            for_open=for_open)
        s=Stub()
        q=provider.YahooChart.eur_quote(s,'AZN.L',tue,'close')
        self.assertEqual(q['fx_session'],'2026-11-09')
        self.assertEqual(q['conversion_convention'],
                         'LAST_PREVIOUS_COMPLETED_WEEKDAY_FX_CLOSE_NOT_SAME_DAY')
        self.assertAlmostEqual(q['price_eur'],1./1.15,places=8)
        self.assertAlmostEqual(q['local_price']*q['local_unit_scale'],1.,places=8)
        self.assertEqual(q['currency'],'GBP')

    def test_sar_fx_cross_respects_pair_dates(self):
        fri=dt.date(2026,11,13);sun=dt.date(2026,11,15)
        quotes={'EURSAR=X':{},'EURUSD=X':{
            fri:provider.Bar(fri,1.10,1.10,'USD','EURUSD=X','EURUSD')},
            'USDSAR=X':{
            fri:provider.Bar(fri,3.75,3.75,'SAR','USDSAR=X','USDSAR')}}
        class Stub:
            def history(self,symbol,start,end):return quotes[symbol]
        rate,meta=provider.YahooChart.fx_units_per_eur(Stub(),'SAR',sun)
        self.assertAlmostEqual(rate,4.125)
        self.assertEqual(meta['fx_session'],'2026-11-13')

    def test_intraday_local_candle_not_saved_as_eod_and_old_partial_removed(self):
        d0=dt.date(2026,11,10);d1=dt.date(2026,11,11)
        rows={d:provider.Bar(d,20.,21.,'JPY','7203.T','HASH',1.,1000.)
              for d in (d0,d1)}
        class Stub:
            def history(self,symbol,start,end):return rows
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)
            with mock.patch.object(daily,'DATA',path),mock.patch.object(
                daily,'is_final_bar',side_effect=lambda symbol,day,now:day==d0):
                audit=daily.update_history(Stub(),'7203.T','prices',d1,d0)
                self.assertEqual(audit['provisional_bars_blocked'],1)
                self.assertEqual(audit['last'],'2026-11-10')
                file=path/'prices'/daily.symbol_filename('7203.T')
                self.assertEqual(len(daily.rows_load(file)),1)
            with mock.patch.object(daily,'DATA',path),mock.patch.object(
                daily,'is_final_bar',return_value=True):
                audit=daily.update_history(Stub(),'7203.T','prices',d1,d0)
                self.assertEqual(audit['last'],'2026-11-11')
                self.assertEqual(len(daily.rows_load(file)),2)

if __name__=='__main__':unittest.main()
