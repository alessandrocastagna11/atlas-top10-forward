"""All quote data in this module are SYNTHETIC and MUST NEVER enter paper ledger."""
import copy,datetime as dt,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import engine as e
import verified_quotes
import daily_runner as dr

HERE=Path(__file__).resolve().parent
OFFICIAL=HERE/'strategies'/'MA10_Official'
CHALLENGER=HERE/'strategies'/'MA6_MA12_Challenger'

def synthetic(dual=False,missing=False):
    cfg=json.loads((OFFICIAL/'config.json').read_text())
    months={f'2026-{i:02d}-28':p for i,p in enumerate([94.19,95.76,90.68,98.59,104.38,105.82,104.44,106.74,108.68,108.26],1)}
    if dual:
        months.update({'2025-11-28':88,'2025-12-31':89})
    months.update({'2026-11-04':109,'2026-11-05':110})
    data={'IUSQ.DE':{'currency':'EUR','bars':months,'dividends':{},'feed':'SYNTHETIC'}}
    for x in cfg['basket']:
        data[x['symbol']]={'currency':x['currency'],'bars':{'2026-11-04':100,'2026-11-05':110},'dividends':{},'feed':'SYNTHETIC'}
    if missing: data['SPCX']['bars'].pop('2026-11-04')
    data['EURUSD=X']={'currency':'USD','bars':{'2026-11-04':1.1,'2026-11-05':1.1},'dividends':{},'feed':'SYNTHETIC'}
    data['EURSAR=X']={'currency':'SAR','bars':{'2026-11-04':4.125,'2026-11-05':4.125},'dividends':{},'feed':'SYNTHETIC'}
    return data

class DailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name)
        for item in ['config.json','state.json']:
            (self.folder/item).write_bytes((OFFICIAL/item).read_bytes())
        (self.folder/'annual_rosters').mkdir()
        self.orig=(e.ROOT,e.CONFIG,e.STATE,e.JOURNAL,e.NAV,e.SOURCES)
        self.addCleanup(self.reset_globals)
    def reset_globals(self):
        e.ROOT,e.CONFIG,e.STATE,e.JOURNAL,e.NAV,e.SOURCES=self.orig
    def test_eod_scheduler_uses_previous_session_before_close(self):
        ny=dt.timezone(dt.timedelta(hours=-5))
        self.assertEqual(dr.latest_closed_us_day(dt.datetime(2026,11,5,8,0,tzinfo=ny)),'2026-11-04')
        self.assertEqual(dr.latest_closed_us_day(dt.datetime(2026,11,4,18,0,tzinfo=ny)),'2026-11-04')
    def test_missing_exact_symbols_audited(self):
        d=synthetic()
        d['2222.SR']['bars'].pop('2026-11-04')
        d['IUSQ.DE']['bars'].pop('2026-11-04')
        r=dr.do_strategy(self.folder,'2026-11-04',d)
        self.assertEqual({v['symbol'] for v in r['missing_exact_closes']},{'2222.SR','IUSQ.DE'})
        self.assertEqual(r['report_status'],'PENDING_DATA')
    def test_verified_eod_supplement_and_no_overwrite(self):
        d=synthetic()
        d['IUSQ.DE']['bars'].pop('2026-11-04')
        d['2222.SR']['bars'].pop('2026-11-04')
        samples={'schema':1,'quotes':[]}
        for sym,price,currency in [('IUSQ.DE',108.9,'EUR'),('2222.SR',26.24,'SAR')]:
            samples['quotes'].append({'symbol':sym,'date':'2026-11-04','currency':currency,
              'close':price,'open':price,'venue':'SYNTHETIC',
              'verified_on':'2026-11-05','source_urls':['https://first.test/evidence','https://second.test/evidence']})
        verified_quotes.apply_verified(d,'2026-11-04',supplements=samples)
        self.assertEqual(d['IUSQ.DE']['bars']['2026-11-04'],108.9)
        self.assertEqual(d['2222.SR']['bars']['2026-11-04'],26.24)
        d['IUSQ.DE']['bars']['2026-11-04']=109
        verified_quotes.apply_verified(d,'2026-11-04',supplements=samples)
        self.assertEqual(d['IUSQ.DE']['bars']['2026-11-04'],109)
    def test_no_initial_late_fill_when_supplement_arrives(self):
        d=synthetic()
        r=dr.do_strategy(self.folder,'2026-11-04',d,allow_initial_trade=False)
        self.assertEqual(r['order_count'],0)
        self.assertFalse(r['initialized'])
    def test_gap_replay_and_idempotence(self):
        r=dr.do_strategy(self.folder,'2026-11-05',synthetic())
        self.assertEqual(r['report_status'],'COMPLETE')
        self.assertEqual(r['last_verified_valuation'],'2026-11-05')
        self.assertEqual(r['buys'],10)
        self.assertEqual(r['sells'],0)
        self.assertEqual(r['order_count'],10)
        self.assertLess(r['portfolio_eur'],11000)
        r2=dr.do_strategy(self.folder,'2026-11-05',synthetic())
        self.assertEqual(r2['order_count'],10)
        self.assertEqual(len(json.loads((self.folder/'state.json').read_text())['order_history']),11)
    def test_late_backfill_cannot_create_fictitious_past_orders(self):
        d=synthetic()
        r=dr.do_strategy(self.folder,'2026-11-05',d)
        self.assertEqual(r['order_count'],10)
        st=json.loads((self.folder/'state.json').read_text())
        self.assertTrue(all(o['date']=='2026-11-05' for o in st['order_history']))
    def test_missing_first_day_uses_next_real_day(self):
        r=dr.do_strategy(self.folder,'2026-11-05',synthetic(missing=True))
        self.assertEqual(r['order_count'],10)
        st=json.loads((self.folder/'state.json').read_text())
        self.assertEqual(st['order_history'][0]['date'],'2026-11-05')
        self.assertEqual(st['last_update'],'2026-11-05')
        self.assertEqual(r['last_verified_valuation'],'2026-11-05')
    def test_missing_fx_blocks_all_trades(self):
        d=synthetic();d.pop('EURSAR=X')
        r=dr.do_strategy(self.folder,'2026-11-05',d)
        self.assertEqual(r['order_count'],0)
        self.assertFalse(r['initialized'])
        self.assertEqual(r['report_status'],'PENDING_DATA')
        st=json.loads((self.folder/'state.json').read_text())
        self.assertEqual(st['last_update'],'2026-11-03')
    def test_challenger_independent_12months(self):
        (self.folder/'config.json').write_bytes((CHALLENGER/'config.json').read_bytes())
        (self.folder/'state.json').write_bytes((CHALLENGER/'state.json').read_bytes())
        r=dr.do_strategy(self.folder,'2026-11-05',synthetic(dual=True))
        self.assertEqual(r['report_status'],'COMPLETE')
        self.assertEqual(r['order_count'],10)
        self.assertEqual(r['regime'],'RISK_ON')
    def test_not_enough_dual_history(self):
        (self.folder/'config.json').write_bytes((CHALLENGER/'config.json').read_bytes())
        (self.folder/'state.json').write_bytes((CHALLENGER/'state.json').read_bytes())
        r=dr.do_strategy(self.folder,'2026-11-05',synthetic(dual=False))
        self.assertEqual(r['report_status'],'PENDING_DATA')
        self.assertEqual(r['order_count'],0)
    def test_pending_monthly_exit_until_common_session(self):
        d=synthetic()
        s=json.loads((self.folder/'state.json').read_text());cfg=json.loads((self.folder/'config.json').read_text())
        e.ROOT=self.folder
        e.step(s,cfg,d,'2026-11-04')
        d['IUSQ.DE']['bars']['2026-11-30']=40
        for rec in d.values():
            rec['bars']['2026-11-30']=100 if rec['currency']!='EUR' else 40
        for rec in d.values():
            rec['bars']['2026-12-01']=100 if rec['currency']!='EUR' else 40
            rec['bars']['2026-12-02']=100 if rec['currency']!='EUR' else 40
        d['2222.SR']['bars'].pop('2026-12-01')
        orders,nav=e.step(s,cfg,d,'2026-11-30')
        self.assertEqual(len(orders),0)
        orders,nav=e.step(s,cfg,d,'2026-12-01')
        self.assertTrue(s.get('pending_transition'))
        self.assertTrue(s['risk_on'])
        self.assertEqual(len(orders),0)
        orders,nav=e.step(s,cfg,d,'2026-12-02')
        self.assertFalse(s['risk_on'])
        self.assertEqual(sum(o['side']=='SELL' for o in orders),10)
        self.assertEqual(s['order_count'],20)
    def test_fx_future_stripped(self):
        d=synthetic();d['EURUSD=X']['bars']['2026-11-07']=1e5
        dr.snapshot_valid(d,'2026-11-05')
        self.assertNotIn('2026-11-07',d['EURUSD=X']['bars'])
    def test_tax_eur_commission(self):
        d=synthetic()
        e.ROOT=self.folder
        cfg=json.loads((self.folder/'config.json').read_text())
        s=json.loads((self.folder/'state.json').read_text())
        orders,_=e.step(s,cfg,d,'2026-11-04')
        self.assertEqual(sum(o['side']=='BUY' for o in orders),11)
        self.assertAlmostEqual(sum(o['commission_eur'] for o in orders),11)
        self.assertEqual(s['order_count'],10)

if __name__=='__main__':unittest.main(verbosity=2)

# Additional direct corporate action test (ex-date split is not a trade).
class SplitTests(unittest.TestCase):
    def test_split_doubles_shares_not_order_count(self):
        cfg=json.loads((OFFICIAL/'config.json').read_text())
        state=json.loads((OFFICIAL/'state.json').read_text())
        d=synthetic()
        e.step(state,cfg,d,'2026-11-04')
        before=state['positions']['AAPL']['shares']
        base=state['positions']['AAPL']['cost_basis_eur']
        old_orders=state['order_count']
        # prices as-if post split on Nov 5; historical adjusted closes in provider
        d['AAPL']['splits']={'2026-11-05':2.0}
        d['AAPL']['bars']['2026-11-05']=50
        orders,nav=e.step(state,cfg,d,'2026-11-05')
        self.assertEqual(orders,[])
        self.assertEqual(state['order_count'],old_orders)
        self.assertAlmostEqual(state['positions']['AAPL']['shares'],2*before)
        self.assertEqual(state['positions']['AAPL']['cost_basis_eur'],base)
        self.assertEqual(state['corporate_actions'][0]['type'],'SPLIT')