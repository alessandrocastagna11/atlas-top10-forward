"""Synthetic-only offline regression tests. NEVER connected to live account."""
import copy
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from paper import PaperBlocked, execute_paper, fresh_state, mark, ENGINE, validate_state
from run import validate_signal, SignalBlocked, REPLAY_EQUITY_SHA,REPLAY_ORDERS_SHA,CANONICAL_BUNDLE_SHA

def snap(decision='2026-11-05', execution='2026-11-06', targets=None):
    if targets is None:
        targets=[{'ticker':'AAPL','market_symbol':'AAPL','sector':'XLK','region':'USA','weight':0.14,'price_eur':100.},
                 {'ticker':'MSFT','market_symbol':'MSFT','sector':'XLK','region':'USA','weight':0.10,'price_eur':200.}]
    return {'engine_id':ENGINE,'schema_version':'3.0','mode':'SHADOW','decision_session':decision,
            'execution_session':execution,'targets':targets,
            'authorization':{'real_money_authorized':False},
            'data_freshness':{'prices_as_of_session':decision,'scores_as_of_session':decision,
                              'membership_as_of_session':decision,'fx_as_of_session':decision},
            'provenance':{'replay_equity_sha256':REPLAY_EQUITY_SHA,'replay_orders_sha256':REPLAY_ORDERS_SHA,
                          'canonical_usa_bundle_sha256':CANONICAL_BUNDLE_SHA,'parity_status':'REPLAY_DIFFERS_FROM_ORIGINAL',
                          'model_name':'Confidence70','confidence_beta':0.70,'lookahead_audit':'RESEARCH_ONLY',
                          'live_source_sha256':{x:'1'*64 for x in ('prices','fx','universe','scores')},
                          'produced_by_engine_run':True}}
class PaperTests(unittest.TestCase):
    def setUp(self):self.quotes={'AAPL':101.,'MSFT':198.}
    def test_initial_buy_and_cash(self):
        st,report=execute_paper(fresh_state(),snap(),self.quotes)
        self.assertEqual(report['orders_count'],2)
        self.assertGreaterEqual(st['cash_eur'],100.)
        self.assertEqual(len(st['holdings']),2)
        self.assertAlmostEqual(sum(o['fee_eur'] for o in st['orders']),2.)
        self.assertFalse(st['real_money_authorized'])
    def test_duplicate_decision_blocked(self):
        st,_=execute_paper(fresh_state(),snap(),self.quotes)
        with self.assertRaises(PaperBlocked):execute_paper(st,snap(),self.quotes)
    def test_protect_35days(self):
        st,_=execute_paper(fresh_state(),snap(),self.quotes)
        newer,receipt=execute_paper(st,snap('2026-11-25','2026-11-26',targets=[]),self.quotes)
        self.assertEqual(receipt['orders_count'],0)
        self.assertEqual(len(newer['holdings']),2)
        self.assertEqual(len(st['holdings']),2)
    def test_sell_after_35_days_and_tax(self):
        st,_=execute_paper(fresh_state(),snap(),self.quotes)
        st,report=execute_paper(st,snap('2026-12-15','2026-12-16',targets=[]),{'AAPL':130.,'MSFT':250.})
        self.assertEqual(report['orders_count'],2)
        self.assertEqual(len(st['holdings']),0)
        self.assertTrue(all(x['tax_estimate_eur']>0 for x in report['orders']))
    def test_multiple_lots_protect_newer_shares_and_allow_old_sale(self):
        st = fresh_state()
        only = [{'ticker':'AAPL','market_symbol':'AAPL','sector':'XLK','region':'USA','weight':.10}]
        st,_ = execute_paper(st,snap('2026-11-05','2026-11-06',only),{'AAPL':100.},rebalance_fraction=.001)
        first_qty = st['holdings']['AAPL']['shares']
        only[0]['weight'] = .15
        st,_ = execute_paper(st,snap('2026-11-19','2026-11-20',only),{'AAPL':100.},rebalance_fraction=.001)
        self.assertEqual(len(st['holdings']['AAPL']['lots']),2)
        second_qty=st['holdings']['AAPL']['lots'][1]['shares']
        self.assertGreater(second_qty,0)
        old=copy.deepcopy(st)
        st,receipt = execute_paper(st,snap('2026-12-10','2026-12-11',[]),{'AAPL':100.},rebalance_fraction=.001)
        self.assertEqual(receipt['orders_count'],1)
        self.assertEqual(receipt['orders'][0]['action'],'SELL')
        self.assertAlmostEqual(receipt['orders'][0]['shares'],first_qty,places=7)
        self.assertAlmostEqual(st['holdings']['AAPL']['shares'],second_qty,places=7)
        self.assertEqual(st['holdings']['AAPL']['first_entry_date'],'2026-11-20')
        self.assertEqual(len(old['holdings']['AAPL']['lots']),2)
        st,receipt=execute_paper(st,snap('2026-12-24','2026-12-25',[]),{'AAPL':100.},rebalance_fraction=.001)
        self.assertEqual(receipt['orders_count'],1)
        self.assertEqual(len(st['holdings']),0)

    def test_lot_ledger_rejects_inconsistent_shares(self):
        st=fresh_state()
        st['holdings']['AAPL']={'shares':2.,'cost_basis_eur_per_share':100.,
            'first_entry_date':'2026-11-06','market_symbol':'AAPL',
            'sector':'XLK','region':'USA',
            'lots':[{'entry_date':'2026-11-06','shares':1.,'cost_basis_eur_per_share':100.}]}
        st['cash_eur']=9800.
        with self.assertRaisesRegex(PaperBlocked,'lot share reconciliation'):
            validate_state(st)

    def test_legacy_holding_migrates_without_changing_cost_or_date(self):
        st=fresh_state()
        st['holdings']['AAPL']={'shares':5.,'cost_basis_eur_per_share':99.,
            'first_entry_date':'2026-11-06','market_symbol':'AAPL',
            'sector':'XLK','region':'USA'}
        st['cash_eur']=9505.
        newer,receipt=execute_paper(st,snap('2026-11-07','2026-11-09',[]),
                                    {'AAPL':100.},rebalance_fraction=.01)
        self.assertEqual(receipt['orders_count'],0)
        self.assertEqual(newer['holdings']['AAPL']['lots'][0]['entry_date'],'2026-11-06')
        self.assertEqual(newer['holdings']['AAPL']['lots'][0]['cost_basis_eur_per_share'],99.)
        self.assertNotIn('lots',st['holdings']['AAPL'])

    def test_bad_sector_and_weight(self):
        bad=snap();bad['targets'][0]['weight']=0.16
        with self.assertRaisesRegex(PaperBlocked,'15%'):execute_paper(fresh_state(),bad,self.quotes)
    def test_insufficient_quote_cannot_trade(self):
        with self.assertRaises(KeyError):execute_paper(fresh_state(),snap(),{'AAPL':101})
    def test_state_reject_real_money(self):
        st=fresh_state();st['real_money_authorized']=True
        with self.assertRaises(PaperBlocked):validate_state(st)
    def test_cash_only_mark(self):
        self.assertEqual(mark(fresh_state(),{},'2026-11-05')['nav_eur'],10000.)
    def test_required_provenance(self):
        validate_signal(snap(),dt.datetime(2026,11,6,17,tzinfo=dt.timezone.utc))
        bad=snap();bad['provenance']['replay_equity_sha256']='0'*64
        with self.assertRaises(SignalBlocked):validate_signal(bad,dt.datetime(2026,11,6,17,tzinfo=dt.timezone.utc))
    def test_future_data_rejected(self):
        bad=snap();bad['data_freshness']['scores_as_of_session']='2026-11-06'
        with self.assertRaises(SignalBlocked):validate_signal(bad,dt.datetime(2026,11,6,17,tzinfo=dt.timezone.utc))
if __name__=='__main__':unittest.main()
