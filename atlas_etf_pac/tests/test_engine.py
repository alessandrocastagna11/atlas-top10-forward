import datetime as dt, hashlib, json, sys, tempfile, unittest
from unittest import mock
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import engine as pac

CFG=json.loads((ROOT/'config.json').read_text())
T1=dt.datetime(2026,11,12,20,0,tzinfo=dt.timezone.utc)
T2=dt.datetime(2026,11,13,20,0,tzinfo=dt.timezone.utc)


def feed(session):
    start=dt.date(2025,1,1)
    end=dt.date.fromisoformat(session)
    dates=[];d=start
    while d<=end:
        if d.weekday()<5:dates.append(d.isoformat())
        d+=dt.timedelta(days=1)
    eq=[{'date':d,'close':100+0.09*i} for i,d in enumerate(dates)]
    bond=[{'date':d,'close':100+0.02*i} for i,d in enumerate(dates)]
    return {'equity':eq,'bond':bond,'bond_usable':True,'bond_problem':None,'macro':pac.v23.synthetic_macro(),
            'macro_problem':None,'cutoff':session,'session':session,'quote_lag_days':0}

def sig(session):return pac.signal(feed(session),CFG)

class TestOriginalV23(unittest.TestCase):
    def test_original_source_exact_hash(self):
        st=pac.seed(CFG);pac.validate(CFG,st)
        p=ROOT/'v23_original/atlas_v23_live.py'
        self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),pac.ORIGINAL_LIVE_SHA)
    def test_original_replay_observed_signal(self):
        v=sig('2026-11-12')
        self.assertEqual(v['method'],'ATLAS_V23_OCT_01_2026_ORIGINAL_LIVE_UNMODIFIED')
        self.assertEqual(v['stress']['month'],'2026-10')
        self.assertEqual(v['stress']['source'],'OBSERVED_COMPLETED_MONTH')
        self.assertEqual(v['decision_session'],'2026-11-12')
    def test_fresh_forward_initial_2_paper_orders_and_idempotence(self):
        st=pac.seed(CFG)
        s1=sig('2026-11-12')
        st,n=pac.ledger_process(CFG,st,'2026-11-12',feed('2026-11-12'),s1,T1)
        self.assertEqual(n,0);self.assertEqual(len(st['orders']),0)
        self.assertEqual(len(st['pending']['proposed_orders']),2)
        s2=sig('2026-11-13')
        st,n=pac.ledger_process(CFG,st,'2026-11-13',feed('2026-11-13'),s2,T2)
        self.assertEqual(n,2);self.assertEqual(len(st['orders']),2)
        self.assertTrue(all(x['execution_session']>x['decision_session'] for x in st['orders']))
        self.assertTrue(all(x['mode']=='VIRTUAL_ONLY' for x in st['orders']))
        self.assertLess(st['cash_eur'],1)
        snap=json.dumps(st,sort_keys=True)
        again,n=pac.ledger_process(CFG,st,'2026-11-13',feed('2026-11-13'),s2,T2)
        self.assertEqual(n,0);self.assertEqual(json.dumps(again,sort_keys=True),snap)
    def test_no_precommit_but_never_creates_backdated_trade(self):
        st=pac.seed(CFG)
        st,n=pac.ledger_process(CFG,st,'2026-11-12',feed('2026-11-12'),sig('2026-11-12'),T1)
        st['pending']['created_at_utc']='2026-11-13T18:00:00+00:00' # later than close
        out,n=pac.ledger_process(CFG,st,'2026-11-13',feed('2026-11-13'),sig('2026-11-13'),T2)
        self.assertEqual(n,0);self.assertEqual(out['orders'],[])
        self.assertIn('ORDER_NOT_PRECOMMITTED_BEFORE_SESSION_CLOSE',str(out['blocked_actions']))
    def test_monthly_deposit_exactly_once(self):
        st=pac.seed(CFG)
        st['last_deposit_month']='2026-11'
        d=pac.accrue_deposit(st,CFG,'2026-12-01')
        self.assertEqual(d,250);self.assertEqual(len(st['contributions']),1)
        self.assertEqual(pac.accrue_deposit(st,CFG,'2026-12-02'),0)
        self.assertEqual(len(st['contributions']),1)
        self.assertAlmostEqual(st['balances']['CORE']['cash_eur'],625)
    def test_realized_tax_and_basis_on_satellite_sale(self):
        st=pac.seed(CFG)
        t=pac.trade(st,CFG,'SATELLITE','BUY','INITIAL',100,'2026-11-13','2026-11-12','INITIAL')
        self.assertEqual(t['mode'],'VIRTUAL_ONLY')
        r=pac.trade(st,CFG,'SATELLITE','SELL','STRATEGIC',125,'2026-11-16','2026-11-13','TEST')
        self.assertGreater(r['tax_proxy_eur'],0)
        self.assertEqual(st['balances']['SATELLITE']['units'],0)
    def test_macro_missing_never_silently_marks_as_observed(self):
        s=pac.CausalStress(CFG,None).regime('2026-11-12')
        self.assertTrue(s['source'].startswith('CARRIED_FROZEN_'))
        self.assertEqual(s['month'],'2026-10')
    def test_2026_macro_missing_prefers_latest_observed_month(self):
        m=pac.v23.synthetic_macro()
        m['rf_annualized'].pop('2026-10')
        m['six'].pop('2026-10')
        s=pac.CausalStress(CFG,m).regime('2026-11-12')
        self.assertEqual(s['source'],'CARRIED_OBSERVED_2026-09')
        self.assertEqual(s['observed_macro_month'],'2026-09')
        self.assertIn('FF_BREADTH_3M',s['missing_inputs'])


    def test_weekend_friday_close_is_usable_for_monday_preclose_signal(self):
        """Friday close is the last completed session Monday morning."""
        monday=dt.datetime(2026,11,16,11,0,tzinfo=dt.timezone.utc)
        self.assertEqual(pac.elapsed_weekday_sessions('2026-11-13','2026-11-15'),0)
        st=pac.seed(CFG)
        st,_=pac.ledger_process(CFG,st,'2026-11-12',feed('2026-11-12'),sig('2026-11-12'),T1)
        st,executed=pac.ledger_process(CFG,st,'2026-11-13',feed('2026-11-13'),sig('2026-11-13'),monday)
        self.assertEqual(executed,0)  # Never back-fill Friday's missed close.
        self.assertEqual(st['last_processed_session'],'2026-11-13')
        self.assertEqual(st['pending']['decision_session'],'2026-11-13')
        self.assertEqual(st['pending']['created_at_utc'],monday.isoformat())
        self.assertIn('STALE_SESSION_NO_RETROACTIVE_FILLS',str(st['blocked_actions']))

    def test_monday_close_executes_precommitted_friday_based_orders(self):
        monday_before=dt.datetime(2026,11,16,11,0,tzinfo=dt.timezone.utc)
        monday_after=dt.datetime(2026,11,16,20,0,tzinfo=dt.timezone.utc)
        st=pac.seed(CFG)
        st,_=pac.ledger_process(CFG,st,'2026-11-12',feed('2026-11-12'),sig('2026-11-12'),T1)
        st,n=pac.ledger_process(CFG,st,'2026-11-13',feed('2026-11-13'),sig('2026-11-13'),monday_before)
        self.assertEqual(n,0)
        pending=st['pending']
        self.assertEqual(pending['created_at_utc'],monday_before.isoformat())
        st,n=pac.ledger_process(CFG,st,'2026-11-16',feed('2026-11-16'),sig('2026-11-16'),monday_after)
        self.assertEqual(n,2)
        self.assertEqual(len(st['orders']),2)
        self.assertTrue(all(q['execution_session']=='2026-11-16' for q in st['orders']))
        self.assertTrue(all(q['decision_session']=='2026-11-13' for q in st['orders']))
        self.assertTrue(all(q['mode']=='VIRTUAL_ONLY' for q in st['orders']))
        self.assertLess(st['cash_eur'],1)
        self.assertIsNone(st['pending'])
        snap=json.dumps(st,sort_keys=True)
        again,n=pac.ledger_process(CFG,st,'2026-11-16',feed('2026-11-16'),sig('2026-11-16'),monday_after)
        self.assertEqual(n,0)
        self.assertEqual(json.dumps(again,sort_keys=True),snap)

    def test_stale_weekday_sessions_fail_closed(self):
        tuesday=dt.datetime(2026,11,17,20,0,tzinfo=dt.timezone.utc)
        self.assertGreater(pac.elapsed_weekday_sessions('2026-11-13',pac.completed_cutoff(tuesday).isoformat()),1)
        st=pac.seed(CFG)
        with self.assertRaisesRegex(pac.InsufficientData,'MISSING_WEEKDAYS'):
            pac.ledger_process(CFG,st,'2026-11-13',feed('2026-11-13'),sig('2026-11-13'),tuesday)


    def test_secondary_yahoo_observed_close_never_fabricated(self):
        now=dt.datetime(2026,11,21,10,0,tzinfo=dt.timezone.utc)
        rows=[{'date':'2026-11-18','close':127.105},
              {'date':'2026-11-19','close':126.79}]
        bars={'chart':{'result':[{
            'meta':{'currency':'EUR'},
            'timestamp':[int(dt.datetime(2026,11,d,9,tzinfo=dt.timezone.utc).timestamp())
                         for d in (19,20,21)],
            'indicators':{'adjclose':[{'adjclose':[126.79,126.55,126.0]}],
                          'quote':[{'close':[126.79,126.55,126.0]}]}
        }]}}
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(pac.v23,'CACHE',Path(temp)), \
                 mock.patch.object(pac.v23,'yahoo_daily',return_value=rows), \
                 mock.patch.object(pac.v23,'fetch_bytes',return_value=json.dumps(bars).encode('utf-8')):
                output,source=pac.yahoo_daily_with_secondary_endpoint(CFG,'EUNL.DE',now)
                self.assertEqual(source,'YAHOO_QUERY2_OBSERVED_CLOSE')
                self.assertEqual(output[-1]['date'],'2026-11-20')
                self.assertEqual(output[-1]['close'],126.55)
                self.assertNotIn('2026-11-21',[r['date'] for r in output])
                self.assertTrue((Path(temp)/'yahoo_EUNL_DE.csv').exists())

    def test_bond_quote_must_match_fill_session(self):
        feeds=feed('2026-11-13')
        feeds['bond']=feeds['bond'][:-1]
        self.assertIsNone(pac.prices_for_session(feeds,'2026-11-13')['BOND'])
        with self.assertRaisesRegex(pac.InsufficientData,'MISSING_HELD_BOND_VALUATION'):
            pac.prices_for_session(feeds,'2026-11-13',True)

    def test_missing_bond_fill_keeps_core_cash_untouched(self):
        st=pac.seed(CFG)
        pending={'decision_session':'2026-11-12','created_at_utc':T1.isoformat(),
                 'proposed_orders':[{'sleeve':'BOND','side':'BUY','kind':'STRATEGIC',
                                     'reason':'TEST_BOND_ENTRY'}]}
        fills=pac.fill_pending(st,CFG,pending,{'EQUITY':100,'BOND':None},
                               '2026-11-13',T2)
        self.assertEqual(fills,[])
        self.assertEqual(st['balances']['CORE']['cash_eur'],500)
        self.assertEqual(st['balances']['BOND']['cash_eur'],0)
        self.assertEqual(st['orders'],[])
        self.assertIn('MISSING_PRICE_FOR_BOND',str(st['blocked_actions']))


    def test_cash_only_stamp_correction_is_audited_and_idempotent(self):
        st=pac.seed(CFG)
        st['stamp_accrued_eur']=0.03833
        st['nav']=[
            {'date':'2026-11-12','cash_eur':1000,'holdings_eur':0,'equity_eur':0,
             'bond_eur':0,'nav_eur':1000,'twr_index':1,'twr_day_pct':0,'drawdown_pct':0},
            {'date':'2026-11-19','cash_eur':1000,'holdings_eur':0,'equity_eur':0,
             'bond_eur':0,'nav_eur':999.96167,'twr_index':0.99996167,
             'twr_day_pct':-0.000548,'drawdown_pct':-0.003833}]
        self.assertTrue(pac.correct_cash_only_stamp(st,T2))
        self.assertEqual(st['nav'][-1]['nav_eur'],1000)
        self.assertEqual(st['nav'][-1]['twr_index'],1)
        self.assertEqual(st['stamp_accrued_eur'],0)
        self.assertEqual(len(st['orders']),0)
        self.assertEqual(st['accounting_adjustments'][0]['paper_orders_changed'],0)
        self.assertFalse(pac.correct_cash_only_stamp(st,T2))
        self.assertEqual(len(st['accounting_adjustments']),1)


    def test_friday_signal_precommitted_on_saturday_fills_monday_only_if_observed(self):
        saturday=dt.datetime(2026,11,21,15,59,tzinfo=dt.timezone.utc)
        tuesday=dt.datetime(2026,11,24,8,30,tzinfo=dt.timezone.utc)
        st=pac.seed(CFG)
        st,_=pac.ledger_process(CFG,st,'2026-11-20',feed('2026-11-20'),
                                sig('2026-11-20'),saturday)
        st['pending']['proposed_orders']=[{'kind':'INITIAL','side':'BUY',
                                           'sleeve':'CORE','reason':'V23_CORE_IN'}]
        st,n=pac.ledger_process(CFG,st,'2026-11-23',feed('2026-11-23'),
                                sig('2026-11-23'),tuesday)
        self.assertEqual(n,1)
        self.assertEqual(st['orders'][0]['execution_session'],'2026-11-23')
        self.assertEqual(st['orders'][0]['decision_session'],'2026-11-20')
        self.assertEqual(st['orders'][0]['sleeve'],'CORE')
        self.assertEqual(st['orders'][0]['mode'],'VIRTUAL_ONLY')

    def test_cash_only_forward_no_security_stamp(self):
        st=pac.seed(CFG)
        st,_=pac.ledger_process(CFG,st,'2026-11-12',feed('2026-11-12'),sig('2026-11-12'),T1)
        # A future date after the first close without any security position:
        st['pending']=None
        st,_=pac.ledger_process(CFG,st,'2026-11-13',feed('2026-11-13'),sig('2026-11-13'),T2)
        self.assertEqual(st['stamp_accrued_eur'],0)
        self.assertEqual(st['nav'][-1]['nav_eur'],1000)


    def test_monthly_macro_lag_is_reported_not_filled_with_estimates(self):
        now=dt.datetime(2026,11,21,15,0,tzinfo=dt.timezone.utc)
        synthetic=pac.v23.synthetic_macro()
        prior={k:{m:v for m,v in synthetic[k].items() if m<='2026-09'}
               for k in ('aaa','baa','gs10','rf_annualized','six')}
        october={k:{**prior[k],'2026-10':synthetic[k]['2026-10']}
                 for k in ('aaa','baa','gs10')}
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(pac.v23,'CACHE',Path(temp)):
                (Path(temp)/'macro_v23.json').write_text(json.dumps(prior))
                with mock.patch.object(pac.v23,'fred_monthly',
                        side_effect=lambda ticker: october[ticker.lower()]), \
                     mock.patch.object(pac.v23,'french_daily_rf_monthly',
                                       return_value=prior['rf_annualized']), \
                     mock.patch.object(pac.v23,'french_six_monthly',
                                       return_value=prior['six']):
                    new,status=pac.refresh_macro_independently(now)
                self.assertEqual(max(new['aaa']),'2026-10')
                self.assertEqual(max(new['rf_annualized']),'2026-09')
                self.assertEqual(max(new['six']),'2026-09')
                self.assertEqual(status['rf_annualized']['status'],
                                 'ONLINE_UPSTREAM_BEHIND_REQUESTED_MONTH')
                self.assertEqual(status['six']['last_month'],'2026-09')
                self.assertNotIn('2026-10',new['six'])
                stress=pac.CausalStress(CFG,new).regime('2026-11-20')
                self.assertEqual(stress['source'],'CARRIED_OBSERVED_2026-09')
                self.assertIn('FF_RF',stress['missing_inputs'])
                self.assertIn('FF_BREADTH_3M',stress['missing_inputs'])

    def test_macro_partial_provider_failure_retains_other_updated_series(self):
        now=dt.datetime(2026,11,21,15,0,tzinfo=dt.timezone.utc)
        synthetic=pac.v23.synthetic_macro()
        past={k:{month:v for month,v in synthetic[k].items() if month<='2026-09'}
              for k in ('aaa','baa','gs10','rf_annualized','six')}
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(pac.v23,'CACHE',Path(temp)):
                (Path(temp)/'macro_v23.json').write_text(json.dumps(past))
                def fred(which):
                    if which=='AAA':raise TimeoutError('test offline FRED AAA')
                    return {**past[which.lower()],
                            '2026-10':synthetic[which.lower()]['2026-10']}
                with mock.patch.object(pac.v23,'fred_monthly',side_effect=fred), \
                     mock.patch.object(pac.v23,'french_daily_rf_monthly',
                                       return_value=past['rf_annualized']), \
                     mock.patch.object(pac.v23,'french_six_monthly',
                                       return_value=past['six']):
                    new,status=pac.refresh_macro_independently(now)
                self.assertEqual(max(new['aaa']),'2026-09')
                self.assertEqual(max(new['baa']),'2026-10')
                self.assertEqual(status['aaa']['status'],'CACHED_PROVIDER_ERROR')
                self.assertEqual(status['baa']['status'],'ONLINE_LATEST_MONTH_AVAILABLE')
                self.assertEqual(status['aaa']['error'],'TimeoutError: test offline FRED AAA')
                self.assertTrue((Path(temp)/'macro_v23.json').exists())

    def test_macro_rejects_future_months_even_when_provider_sends_them(self):
        now=dt.datetime(2026,11,21,15,0,tzinfo=dt.timezone.utc)
        synthetic=pac.v23.synthetic_macro()
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(pac.v23,'CACHE',Path(temp)), \
                 mock.patch.object(pac.v23,'fred_monthly',
                      side_effect=lambda k:synthetic[k.lower()]), \
                 mock.patch.object(pac.v23,'french_daily_rf_monthly',
                      return_value=synthetic['rf_annualized']), \
                 mock.patch.object(pac.v23,'french_six_monthly',
                      return_value=synthetic['six']):
                new,status=pac.refresh_macro_independently(now)
            self.assertEqual(new['source_audit']['requested_completed_month'],'2026-10')
            for key in ('aaa','baa','gs10','rf_annualized','six'):
                self.assertNotIn('2026-11',new[key])
                self.assertEqual(max(new[key]),'2026-10')

    def test_paper_rules_no_35days_no_monthly_cap(self):
        self.assertEqual(CFG['min_hold_days_before_sell'],0)
        self.assertIsNone(CFG['max_total_orders_per_month'])
        self.assertTrue(CFG['no_broker_execution'])

if __name__=='__main__':unittest.main()
