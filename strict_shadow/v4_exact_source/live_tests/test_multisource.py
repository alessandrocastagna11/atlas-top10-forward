import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from multisource import audit,compare,exact_row,eod_symbol,adjusted_overlap

class MultiSourceTests(unittest.TestCase):
    def test_comparisons_and_dates(self):
        self.assertEqual(compare(100,100.3,.01),'AGREES')
        self.assertEqual(compare(100,120,.01),'DIFFERENT_REVIEW')
        self.assertEqual(compare(None,100,.01),'ALTERNATIVE_AVAILABLE_PRIMARY_MISSING')
        self.assertIsNone(exact_row([{'date':'2026-11-25','close':100}],'2026-11-27'))
        self.assertIsNone(exact_row([{'date':'2026-11-27','close':float('nan')}],'2026-11-27'))
        self.assertEqual(eod_symbol('SAP.DE'),'SAP.XETRA')
    def test_adjusted_history_overlap(self):
        dates=pd.bdate_range('2026-07-01',periods=90)
        values=[100+i*.2 for i in range(len(dates))]
        primary=pd.DataFrame({'adj_close':values},index=dates)
        rows=[{'date':str(day.date()),'adjusted_close':v*2} for day,v in zip(dates,values)]
        expected=dates[-1].date()
        self.assertEqual(adjusted_overlap(primary,rows,expected)['status'],'OVERLAP_CONSISTENT_SHADOW_ONLY')
        rows[70]['adjusted_close']*=.8
        self.assertEqual(adjusted_overlap(primary,rows,expected)['status'],'ADJUSTMENT_MISMATCH')
        self.assertEqual(adjusted_overlap(primary,rows[:-1],expected)['status'],'MISSING_TARGET_SESSION')
        self.assertEqual(adjusted_overlap(primary,rows+[rows[-1]],expected)['status'],'DUPLICATE_DATE')
    def test_disabled_no_requests_ecb_only(self):
        now=dt.datetime(2026,11,28,12,tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{},clear=True):
            def forbidden(*args):raise AssertionError('unexpected network request')
            report=audit({},['KO'],[],dt.date(2026,11,27),{},now,getter=forbidden,ecb_folder=Path(root))
            self.assertFalse(report['provider_opt_in'])
            self.assertEqual(sum(x['requests'] for x in report['providers'].values()),0)
    def test_massive_one_call_no_price_published_or_mutation(self):
        now=dt.datetime(2026,11,28,12,tzinfo=dt.timezone.utc)
        d=dt.date(2026,11,27)
        frames={'KO':pd.DataFrame({'raw_close':[100.]},index=pd.to_datetime([d]))}
        original=frames['KO'].copy(deep=True)
        ts=int(dt.datetime(2026,11,27,5,tzinfo=dt.timezone.utc).timestamp()*1000)
        def mock(url,params):
            return {'status':'OK','results':[{'T':'KO','t':ts,'c':100.2},{'T':'CB','t':ts,'c':345.}]}
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ,{'ATLAS_MULTISOURCE_OPT_IN':'1','MASSIVE_API_KEY':'test-secret'},clear=True):
            report=audit(frames,['KO','CB'],[],d,{'KO':str(d),'CB':str(d)},now,
                         missing=['CB'],getter=mock,ecb_folder=Path(root))
        self.assertEqual(report['providers']['massive']['requests'],1)
        self.assertEqual(report['alternative_available_for_missing'],['CB'])
        self.assertEqual(report['independent_agreements'],1)
        self.assertNotIn('test-secret',str(report))
        self.assertNotIn('345.0',str(report))
        pd.testing.assert_frame_equal(frames['KO'],original)

if __name__=='__main__':unittest.main()
