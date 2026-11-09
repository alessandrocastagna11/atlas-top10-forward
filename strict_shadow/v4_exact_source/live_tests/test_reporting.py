"""Daily user-facing report and no-invented-trades safety contract."""
import json, tempfile, unittest, sys, datetime as dt
from pathlib import Path
from types import SimpleNamespace
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from paper_reporting import initial_portfolio, to_portfolio, write_report
import run_live

class DailyReportTests(unittest.TestCase):
    def test_first_day_cash_no_invented_orders(self):
        with tempfile.TemporaryDirectory() as t:
            p=initial_portfolio()
            kind=write_report(t,{'status':'WAITING_FIRST_ACTUAL_EXECUTION_SESSION', 'orders_cumulative':0,'new_orders':0},p,[])
            self.assertEqual(kind,'INITIAL_CASH_ONLY_NOT_MARKET_VALUED')
            report=(Path(t)/'latest.md').read_text()
            self.assertIn('10,000.00 €',report)
            self.assertIn('Nessuna operazione',report)
            self.assertIn('35 giorni',report)
            self.assertEqual(len((Path(t)/'orders.csv').read_text().splitlines()),1)
    def test_portfolio_lots_maturity_and_mark(self):
        date=pd.Timestamp('2026-12-01')
        fake=SimpleNamespace(
            equity=pd.Series([10090.],index=[date]),paper_cash_eur=7000.,
            orders=pd.DataFrame([{'action':'BUY'}]),blocked_sell_count=0,
            blocked_sell_notional_eur=0.,
            paper_positions={'AAPL':{'shares':20.,'cost_basis_eur':2900.,
              'first_entry_date':'2026-11-10',
              'lots':[{'shares':20.,'purchase_date':'2026-11-10','sale_eligible_date':'2026-12-15'}]}})
        model=SimpleNamespace(raw=SimpleNamespace(close_eur=pd.DataFrame({'AAPL':[160.]},index=[date])))
        meta={'ticker_to_market_symbol':{'AAPL':'AAPL'},'roster_source_asof':'2026-11-09',
              'universe_kind':'ETF_HOLDING_VALUE_PROXY_NOT_MARKET_CAP'}
        p=to_portfolio(fake,model,meta)
        self.assertEqual(p['cash_eur'],7000.)
        self.assertEqual(p['invested_eur'],3200.)
        self.assertEqual(p['nav_mark_to_market_eur'],10200.)
        self.assertEqual(p['nav_net_liquidation_eur'],10090.)
        self.assertEqual(p['holdings'][0]['locked_shares'],20)
        self.assertEqual(p['holdings'][0]['sell_eligible_shares'],0)
    def test_missing_price_fails_closed(self):
        date=pd.Timestamp('2026-12-01')
        fake=SimpleNamespace(equity=pd.Series([10000.],index=[date]),paper_cash_eur=0.,
              orders=pd.DataFrame(),blocked_sell_count=0,blocked_sell_notional_eur=0.,
              paper_positions={'AAA':{'shares':1.,'cost_basis_eur':1.,'lots':[]}})
        model=SimpleNamespace(raw=SimpleNamespace(close_eur=pd.DataFrame({'AAA':[float('nan')]},index=[date])))
        with self.assertRaisesRegex(ValueError,'Stale/invalid'):
            to_portfolio(fake,model,{'ticker_to_market_symbol':{}})
    def test_failed_feed_leaves_cash_seed_and_readable_report(self):
        with tempfile.TemporaryDirectory() as t:
            fields=['FORWARD','STATE','STATUS','PREOPEN','get_adjusted']
            old={x:getattr(run_live,x) for x in fields}
            try:
                run_live.FORWARD=Path(t)
                run_live.STATE=Path(t)/'state.json'
                run_live.STATUS=Path(t)/'latest_status.json'
                run_live.PREOPEN=Path(t)/'preopen'
                run_live.get_adjusted=lambda *a,**k: ({},{'SPY':'SOURCE_UNAVAILABLE'})
                status=run_live.main(dt.datetime(2026,11,9,21,0,tzinfo=dt.timezone.utc))
                self.assertEqual(status['status'],'PENDING_EXACT_DATA_OR_PARITY')
                self.assertEqual(status['orders_cumulative'],0)
                self.assertEqual(status['new_orders'],0)
                self.assertFalse((Path(t)/'preopen').exists())
                self.assertTrue((Path(t)/'latest.md').exists())
                self.assertEqual(json.loads((Path(t)/'portfolio.json').read_text())['nav_mark_to_market_eur'],10000.)
            finally:
                for x,v in old.items():setattr(run_live,x,v)

if __name__=='__main__':unittest.main()
