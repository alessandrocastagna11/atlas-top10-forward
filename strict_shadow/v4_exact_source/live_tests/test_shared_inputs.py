import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shared_inputs import publish,load

class SharedInputTest(unittest.TestCase):
    def test_lossless_snapshot_and_corruption_guard(self):
        dates=pd.DatetimeIndex(['2026-11-13','2026-11-16'],name='Date')
        frames={'SPY':pd.DataFrame({'adj_open':[1.2345678901234567,2.0],
            'adj_close':[1.3,2.2],'raw_close':[1.4,2.3],
            'volume':np.array([100,200],dtype=np.int64),'dividend':[0.,np.nan],
            'quote_currency':['USD','USD'],'quote_unit_scale':[1.,1.]},index=dates)}
        rate=pd.Series([.0123456789012345,.02],index=dates,name='rate')
        day=dt.date(2026,11,16)
        vintages=[(day,day,'test-roster',[{'symbol':'X','currency':'USD'}],
                   [{'symbol':'Y','currency':'EUR'}],'test-proxy')]
        meta={'roster_sha256':'test-roster','roster_recorded_date':str(day)}
        with tempfile.TemporaryDirectory() as temp:
            desc=publish(temp,day,vintages,frames,rate,meta,'2026-11-17T00:00:00+00:00')
            v,f,c=load(temp,desc,day,'test-roster')
            self.assertEqual(v,vintages)
            pd.testing.assert_frame_equal(f['SPY'],frames['SPY'],check_exact=True)
            pd.testing.assert_series_equal(c,rate,check_exact=True)
            with self.assertRaises(ValueError):load(temp,desc,day,'different-roster')
            with self.assertRaises(ValueError):load(temp,desc,dt.date(2026,11,17),'test-roster')
            with (Path(temp)/desc['path']).open('ab') as stream:stream.write(b'corruption')
            with self.assertRaises(ValueError):load(temp,desc,day,'test-roster')

    def test_missing_snapshot_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):load(temp,None,'2026-11-16','test-roster')

if __name__=='__main__':unittest.main()
