import sys,unittest,datetime as dt, numpy as np, pandas as pd
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import live_model as mod

class OfflineSourceEngineTests(unittest.TestCase):
    def test_original_region_engine_path(self):
        np.random.seed(2107)
        index=pd.bdate_range('2023-01-02','2026-11-11')
        n=len(index)
        us=[dict(symbol=f'US{i}',currency='USD',country='United States',sector='Information Technology' if i%2 else 'Financials') for i in range(12)]
        row=[dict(symbol=f'ROW{i}.T',currency='JPY',country='Japan',sector='Information Technology' if i%2 else 'Financials') for i in range(12)]
        def chart(base=100.,drift=0.0006,vol=.009):
            z=base*np.exp(np.cumsum(np.random.normal(drift,vol,n)))
            return pd.DataFrame({'adj_open':z*.999,'adj_close':z,'raw_close':z,'volume':np.full(n,9e6),'dividend':np.zeros(n)},index=index)
        frames={'SPY':chart(500.),'EURUSD=X':chart(1.1,drift=0,vol=.0004),
                'EURJPY=X':chart(159.,drift=0,vol=.001),
                'XLK':chart(200.),'XLF':chart(100.)}
        for r in us+row:frames[r['symbol']]=chart(100. if r['country']=='United States' else 17000.)
        def cash(index,cache,refresh,haircut_bps):return pd.Series(.025,index=index), 'test ECB'
        original=mod.v2.fetch_ecb_cash_rate
        mod.v2.fetch_ecb_cash_rate=cash
        try:
            m,meta=mod.build_model((dt.date(2026,11,9),dt.date(2026,11,6),'fixture',us,row),dt.date(2026,11,11),frames)
            self.assertEqual(len(m.features.close_eur.columns),24)
            result,status=mod.evaluate_model(m,meta)
            self.assertIsNotNone(result)
            self.assertIn('PAPER_REPLAYED_EXACT_CODE_PATH',status['status'])
            self.assertNotIn('end_of_test',result.orders.get('reason',pd.Series(dtype=str)).tolist())
            self.assertEqual(m.bundle['cfg'].decision_interval_weeks,5)
            self.assertEqual(m.bundle['cfg'].minimum_holding_calendar_days,35)
        finally:mod.v2.fetch_ecb_cash_rate=original

if __name__=='__main__':unittest.main()
