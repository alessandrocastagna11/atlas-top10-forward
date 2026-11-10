"""Causal roster test: new market caps never alter yesterday's cohorts."""
import datetime as dt,json,sys,tempfile,unittest
from pathlib import Path
from unittest import mock
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import live_model as m

class DailyCapVintageTests(unittest.TestCase):
    def test_marketcap_cannot_backdate_and_does_not_fall_back_to_etf(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            eu=[{'symbol':f'US{i:03d}','country':'United States',
                 'sector':'Information Technology','currency':'USD','company_name':f'USA{i}'}
                 for i in range(293)]
            er=[{'symbol':f'EU{i:03d}.DE','country':'Germany',
                 'sector':'Industrials','currency':'EUR','company_name':f'ROW{i}'}
                 for i in range(207)]
            d=root/'dataset/universe/snapshots'
            for name,asof in [('2026-11-06','Nov 05, 2026'),('2026-11-11','Nov 10, 2026')]:
                p=d/name;p.mkdir(parents=True)
                pd.DataFrame(eu+er).to_csv(p/'global500.csv',index=False)
                (p/'source.json').write_text(json.dumps({
                    'recorded_on':name,'source_as_of':asof,
                    'type':'ETF_HOLDING_VALUE_PROXY_NOT_MARKET_CAP'}))
            cap=root/'dataset/marketcap_observed/observations/2026-11-10T142718Z'
            cap.mkdir(parents=True)
            rows=eu[:100]+er[:200]
            for i,row in enumerate(rows):
                row['market_cap_usd']=1e12-i*1e9
                row['market_cap_vendor_rank']=i+1
            pd.DataFrame(rows).to_csv(cap/'ranking.csv',index=False)
            (cap/'source.json').write_text(json.dumps({
                'capture_day':'2026-11-10',
                'observed_at_utc':'2026-11-10T14:27:18+00:00',
                'historical_pit_market_cap_verified':False,
                'sufficient_for_100_usa_200_nonusa':True}))
            with mock.patch.object(m,'LIVE',root):
                old=m.latest_vintages(dt.date(2026,11,9))
                today=m.latest_vintages(dt.date(2026,11,10))
                tomorrow=m.latest_vintages(dt.date(2026,11,11))
            self.assertEqual(len(old),1)
            self.assertEqual(len(old[0]),5)
            self.assertEqual(len(today[-1]),6)
            self.assertEqual(today[-1][0],dt.date(2026,11,10))
            self.assertEqual(tomorrow[-1][0],dt.date(2026,11,10))
            self.assertIn('MARKET_CAP_USD_AS_RECORDED',tomorrow[-1][5])

if __name__=='__main__':unittest.main()
