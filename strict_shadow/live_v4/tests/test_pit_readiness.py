"""Synthetic tests: ECB data alone must not falsely certify original V4 PIT."""
import datetime as dt,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pit_readiness as p

class ReadinessTests(unittest.TestCase):
    def test_missing_sources_original_stays_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            s=p.status(dt.datetime(2026,11,6,23,tzinfo=dt.timezone.utc),Path(folder))
            self.assertFalse(s['original_confidence70_model_ready'])
            self.assertFalse(s['historical_pit_verified'])
            self.assertEqual(len(s['historical_pit_issues_remaining']),5)
            self.assertEqual(s['research_proxy_orders'],0)
    def test_ecb_rate_history_is_not_original_pit_membership(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            fx=root/'dataset/fx_ecb/audit/latest.json'
            fx.parent.mkdir(parents=True)
            fx.write_text(json.dumps({'status':'ECB_REFERENCE_UPDATED',
                                      'currencies_with_history':15}))
            universe=root/'dataset/audit/latest.json'
            universe.parent.mkdir(parents=True,exist_ok=True)
            universe.write_text(json.dumps({'forward_universe':{
                 'type':'ETF_HOLDING_VALUE_PROXY_NOT_MARKET_CAP'}}))
            s=p.status(dt.datetime(2026,11,6,23,tzinfo=dt.timezone.utc),root)
            self.assertTrue(s['FX']['ecb_daily_reference_available'])
            self.assertFalse(s['historical_pit_verified'])
            self.assertEqual(s['universe_ranking_type'],'ETF_HOLDING_VALUE_PROXY_NOT_MARKET_CAP')
    def test_proxy_order_count_not_misrepresented_as_original(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);f=root/'forward/proxy_paper_status.json'
            f.parent.mkdir(parents=True)
            f.write_text(json.dumps({'status':'PROXY_PAPER_EXECUTED',
                       'orders_count':5,'valuation':{'nav_eur':9990}}))
            s=p.status(dt.datetime(2026,11,6,23,tzinfo=dt.timezone.utc),root)
            self.assertEqual(s['research_proxy_orders'],5)
            self.assertEqual(s['research_proxy_nav_eur'],9990)
            self.assertFalse(s['original_confidence70_model_ready'])
if __name__=='__main__':unittest.main()
