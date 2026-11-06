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
    def test_future_verified_supplement_is_rejected(self):
        d=synthetic()
        sample={'schema':1,'quotes':[{'symbol':'IUSQ.DE','date':'2026-11-04',
            'currency':'EUR','close':108.9,'venue':'XETRA','verified_on':'2099-01-01',
            'source_urls':['https://one.test/evidence','https://two.test/evidence']}]}
        with self.assertRaises(ValueError):
            verified_quotes.apply_verified(d,'2026-11-04',supplements=sample)
    def test_same_domain_supplement_is_rejected(self):
        d=synthetic()
        sample={'schema':1,'quotes':[{'symbol':'IUSQ.DE','date':'2026-11-04',
            'currency':'EUR','close':108.9,'venue':'XETRA','verified_on':'2026-11-05',
            'source_urls':['https://one.test/evidence','https://one.test/other']}]}
        with self.assertRaises(ValueError):
            verified_quotes.apply_verified(d,'2026-11-04',supplements=sample)
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
        self.assertEqual(r['report_status'],'WAITING_NEXT_LIVE_SESSION')
    def test_no_retro_fill_with_real_verifiable_yahoo_bar(self):
        d=synthetic()
        # All same-day closes, including IUSQ, are now known,
        # but the run is delayed to the following morning.
        r=dr.do_strategy(self.folder,'2026-11-05',d,allow_initial_trade=False)
        self.assertEqual(r['price_freshness'],'EXACT')
        self.assertEqual(r['order_count'],0)
        self.assertEqual(r['report_status'],'WAITING_NEXT_LIVE_SESSION')
        st=json.loads((self.folder/'state.json').read_text())
        self.assertEqual(st['last_update'],'2026-11-03')
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
    def test_frozen_prelaunch_signal_mismatch_blocks_all_orders(self):
        # Synthetic history says MA10 is risk-on, while a stale/falsified
        # launch attestation says risk-off. No portfolio can initialize.
        data=synthetic()
        result=dr.do_strategy(self.folder,'2026-11-05',data,
                              allow_initial_trade=True,expected_initial_risk=False)
        self.assertEqual(result['order_count'],0)
        self.assertFalse(result['initialized'])
        self.assertEqual(result['report_status'],'PENDING_DATA')
        self.assertIn('SIGNAL_DIFFERS_FROM_PRECOMMITTED_OCTOBER_REGIME',
                      str(result['execution_attempts_blocked']))
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

class LaunchIntentTests(unittest.TestCase):
    """A genuine forward decision must predate the shared trading session."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.report=self.base/'reports';self.report.mkdir()
        self.strategy=self.base/'strategies'
        self.intent={'schema_version':1,'mode':'PAPER_ONLY',
                     'declaration':'PRECOMMITTED_CLOSE_PRICE_PAPER_INTENT_NO_REAL_BROKER_ORDERS',
                     'activation_date':'2026-11-06',
                     'first_eligible_common_close':'2026-11-09',
                     'latest_eligible_common_close':'2026-11-12',
                     'locked_at_utc':'2026-11-06T20:14:28+00:00',
                     'strategies':{}}
        for name in dr.LABELS:
            root=self.strategy/name;root.mkdir(parents=True)
            orig=HERE/'strategies'/name
            for filename in ['config.json','state.json']:
                (root/filename).write_bytes((orig/filename).read_bytes())
            cfg=json.loads((root/'config.json').read_text())
            self.intent['strategies'][name]={
                 'config_blob_sha':dr.git_blob_sha((root/'config.json').read_bytes()),
                 'initial_state_blob_sha':dr.git_blob_sha((root/'state.json').read_bytes()),
                 'basket_symbols':[x['symbol'] for x in cfg['basket']],
                 'benchmark_symbol':cfg['benchmark']['symbol'],
                 'precommitted_risk_on':True,'precommitted_signal_month':'2026-10'}
        self.oldreport=dr.REPORTS;self.oldstrat=dr.STRATEGIES
        dr.REPORTS=self.report;dr.STRATEGIES=self.strategy
        self.addCleanup(self.reset)
        self.write()
    def reset(self):
        dr.REPORTS=self.oldreport;dr.STRATEGIES=self.oldstrat
    def write(self):
        (self.report/'launch_intent.json').write_text(json.dumps(self.intent))
    def check(self,day):
        return dr.launch_ready(day,dt.datetime(2026,11,13,0,tzinfo=dt.timezone.utc))
    def test_monday_precommitted_frozen_paper_allowed(self):
        ok,reason=self.check('2026-11-09')
        self.assertTrue(ok,reason)
        self.assertEqual(reason,'PRECOMMITTED_PAPER_LAUNCH_APPROVED')
    def test_friday_and_expired_session_rejected(self):
        self.assertFalse(self.check('2026-11-06')[0])
        self.assertFalse(self.check('2026-11-13')[0])
    def test_late_post_open_decision_rejected(self):
        self.intent['locked_at_utc']='2026-11-09T08:00:00+00:00';self.write()
        self.assertFalse(self.check('2026-11-09')[0])
    def test_tampered_config_rejected(self):
        with (self.strategy/'MA10_Official'/'config.json').open('a') as fh:fh.write(' ')
        ok,reason=self.check('2026-11-09')
        self.assertFalse(ok)
        self.assertIn('CONFIG_CHANGED',reason)
    def test_tampered_initial_ledger_rejected(self):
        state=self.strategy/'MA6_MA12_Challenger'/'state.json'
        with state.open('a') as fh:fh.write(' ')
        ok,reason=self.check('2026-11-09')
        self.assertFalse(ok)
        self.assertIn('INITIAL_LEDGER_CHANGED',reason)
    def test_mode_cannot_be_promoted_to_real_orders(self):
        self.intent['mode']='REAL';self.write()
        self.assertFalse(self.check('2026-11-09')[0])
