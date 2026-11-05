"""Synthetic end-to-end: 2026 decision -> next verified open -> research PAPER order only."""
import datetime as dt,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import proxy_paper as m
from provider import Bar
from forward_signal import VERSION
from paper import ENGINE

class FakeProvider:
    def bar(self,symbol,session):
        return Bar(session,100.,101.,'USD',symbol,'test-market-digest')
    def eur_quote(self,symbol,session,field):
        return {'price_eur':100. if symbol=='AAPL' else 200.,'session':str(session),
                'fx_sha256':'verified','field':field,'symbol':symbol}
    def history(self,symbol,start,end):
        day=dt.date(2026,11,6)
        return {day:self.bar(symbol,day)}

class ForwardPaperIntegration(unittest.TestCase):
    def test_no_trades_before_open_and_only_paper_after(self):
        original=(m.FORWARD,m.STATE)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                m.FORWARD=Path(tmp);m.STATE=m.FORWARD/'proxy_paper_state.json'
                decision={'schema_version':'research-forward-1','engine_id':ENGINE,
                          'model_variant':VERSION,'historical_engine_equivalent':False,'mode':'SHADOW',
                          'decision_session':'2026-11-05','execution_session':'2026-11-06',
                          'authorization':{'real_money_authorized':False},
                          'universe_source':{'recorded_on':'2026-11-05','asof':'2026-11-04'},
                          'targets':[{'ticker':'AAPL','market_symbol':'AAPL','region':'USA','sector':'TECH',
                                      'weight':.12,'price_eur':99.},
                                     {'ticker':'MSFT','market_symbol':'MSFT','region':'USA','sector':'TECH',
                                      'weight':.1,'price_eur':190.}]}
                (m.FORWARD/'pending_signal.json').write_text(json.dumps(decision))
                premature=m.execute(dt.datetime(2026,11,6,16,tzinfo=dt.timezone.utc),FakeProvider())
                self.assertEqual(premature['status'],'PENDING_EXECUTION_QUOTES')
                self.assertEqual(premature['orders_count'],0)
                later=m.execute(dt.datetime(2026,11,6,23,tzinfo=dt.timezone.utc),FakeProvider())
                self.assertEqual(later['status'],'PROXY_PAPER_EXECUTED')
                self.assertEqual(later['orders_count'],2)
                self.assertEqual(later['real_money_orders'],0)
                self.assertFalse((m.FORWARD/'pending_signal.json').exists())
                again=m.execute(dt.datetime(2026,11,6,23,tzinfo=dt.timezone.utc),FakeProvider())
                self.assertEqual(again['orders_count'],2)
        finally:m.FORWARD,m.STATE=original
if __name__=='__main__':unittest.main()
