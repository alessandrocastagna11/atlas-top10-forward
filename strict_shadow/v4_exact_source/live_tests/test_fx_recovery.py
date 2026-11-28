import datetime as dt
import sys
import unittest
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fx_recovery import inverse_symbol,validate_inverse

class InverseFXTests(unittest.TestCase):
    def setUp(self):
        dates=pd.bdate_range('2025-01-01',periods=400)
        self.expected=dates[-1].date()
        values=np.linspace(1.05,1.15,len(dates))
        self.primary=pd.DataFrame({c:values[:-1] for c in ('adj_open','adj_close','raw_close')},index=dates[:-1])
        self.primary['quote_currency']='USD'
        self.inverse=pd.DataFrame({c:1/values for c in ('adj_open','adj_close','raw_close')},index=dates)
        self.inverse['quote_currency']='EUR'
    def test_exact_reciprocal_recovers_missing_close(self):
        result=validate_inverse('EURUSD=X',self.inverse,self.primary,self.expected)
        self.assertAlmostEqual(result.raw_close.iloc[-1],1.15)
        self.assertEqual(result.quote_currency.iloc[-1],'USD')
        self.assertEqual(result.source_symbol.iloc[-1],'USDEUR=X')
        self.assertEqual(len(result),400)
        cols=['adj_open','adj_close','raw_close']
        pd.testing.assert_frame_equal(result.loc[self.primary.index,cols],self.primary[cols])
        self.assertAlmostEqual(self.inverse.raw_close.iloc[-1],1/1.15)
    def test_mismatch_rejected(self):
        broken=self.inverse.copy()
        broken.loc[broken.index[-50],'raw_close']*=1.1
        with self.assertRaisesRegex(ValueError,'overlap mismatch'):
            validate_inverse('EURUSD=X',broken,self.primary,self.expected)
    def test_stale_rejected(self):
        with self.assertRaisesRegex(ValueError,'stale'):
            validate_inverse('EURUSD=X',self.inverse.iloc[:-1],self.primary,self.expected)
    def test_long_gap_rejected(self):
        with self.assertRaisesRegex(ValueError,'three contiguous'):
            validate_inverse('EURUSD=X',self.inverse,self.primary.iloc[:-5],self.expected)
    def test_no_equity_or_wrong_currency(self):
        self.assertIsNone(inverse_symbol('KO'))
        bad=self.inverse.copy();bad['quote_currency']='USD'
        with self.assertRaisesRegex(ValueError,'currency mismatch'):
            validate_inverse('EURUSD=X',bad,self.primary,self.expected)

if __name__=='__main__':unittest.main()
