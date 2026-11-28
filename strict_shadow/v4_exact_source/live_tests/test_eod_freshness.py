"""Market-data freshness invariants for the original forward PAPER input."""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT.parent/'live_v4'))
import live_model


def frame(last):
    dates=pd.date_range('2026-11-17',last,freq='B',name='Date')
    return pd.DataFrame({'adj_open':100.,'adj_close':101.,
                         'raw_close':101.,'volume':1000.,
                         'adjustment_source':'VENDOR_ADJUSTED_CLOSE'},index=dates)


class ExactUSPriceFreshnessTests(unittest.TestCase):
    def test_us_equities_must_match_confirmed_spy_session(self):
        with self.assertRaisesRegex(ValueError,'STALE_REQUIRED_US_EOD'):
            live_model.check_required_us_closes(
                {'PANW':frame('2026-11-18'),'KO':frame('2026-11-19')},
                {'PANW','KO'},dt.date(2026,11,19))

    def test_all_verified_us_bars_pass(self):
        status=live_model.check_required_us_closes(
            {'PANW':frame('2026-11-19'),'KO':frame('2026-11-19')},
            {'PANW','KO'},dt.date(2026,11,19))
        self.assertEqual(status['status'],'COMPLETE')
        self.assertEqual(status['us_symbols_verified'],2)

    def test_historical_inactive_tickers_are_not_required(self):
        status=live_model.check_required_us_closes(
            {'PANW':frame('2026-11-19'),'OLD':frame('2026-11-18')},
            {'PANW'},dt.date(2026,11,19))
        self.assertEqual(status['us_symbols_verified'],1)

    def test_missing_current_us_bar_cannot_be_called_a_holiday(self):
        self.assertTrue(live_model.requires_same_us_close('PANW'))
        self.assertTrue(live_model.requires_same_us_close('XLK'))
        self.assertFalse(live_model.requires_same_us_close('SPY'))
        self.assertFalse(live_model.requires_same_us_close('7203.T'))

    def test_stale_cache_cannot_cover_failed_live_us_fetch(self):
        with tempfile.TemporaryDirectory() as folder:
            cache=Path(folder)
            frame('2026-11-18').to_csv(
                cache/live_model._cache_filename('PANW'),compression='gzip')
            with mock.patch.object(live_model,'CACHE',cache),mock.patch.object(
                live_model.urllib.request,'urlopen',
                side_effect=RuntimeError('vendor unavailable')),mock.patch.object(
                live_model.time,'sleep',return_value=None):
                with self.assertRaisesRegex(RuntimeError,'Yahoo adjusted chart unavailable'):
                    live_model._chart('PANW',dt.date(2026,11,19),
                                      require_current_us=True)

    def test_same_session_legacy_cache_without_adjustment_proof_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            cache=Path(folder)
            frame('2026-11-19').drop(columns=['adjustment_source']).to_csv(
                cache/live_model._cache_filename('PANW'),compression='gzip')
            with mock.patch.object(live_model,'CACHE',cache),mock.patch.object(
                live_model.urllib.request,'urlopen',
                side_effect=RuntimeError('vendor unavailable')),mock.patch.object(
                live_model.time,'sleep',return_value=None):
                with self.assertRaisesRegex(RuntimeError,'Yahoo adjusted chart unavailable'):
                    live_model._chart('PANW',dt.date(2026,11,19),
                                      require_current_us=True)

    def test_same_session_cached_validated_eod_allowed_when_vendor_is_down(self):
        with tempfile.TemporaryDirectory() as folder:
            cache=Path(folder)
            frame('2026-11-19').to_csv(
                cache/live_model._cache_filename('PANW'),compression='gzip')
            with mock.patch.object(live_model,'CACHE',cache),mock.patch.object(
                live_model.urllib.request,'urlopen',
                side_effect=RuntimeError('vendor unavailable')),mock.patch.object(
                live_model.time,'sleep',return_value=None):
                received=live_model._chart('PANW',dt.date(2026,11,19),
                                           require_current_us=True)
                self.assertEqual(str(received.index[-1].date()),'2026-11-19')


if __name__=='__main__':
    unittest.main()
