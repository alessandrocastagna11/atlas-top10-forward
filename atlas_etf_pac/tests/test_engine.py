"""ATLAS ETF PAC reconstructed forward — synthetic tests ONLY."""
import copy,datetime as dt,json,math,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import engine as e

HERE=Path(__file__).resolve().parents[1]
CFG=json.loads((HERE/'config.json').read_text())
SEED=json.loads((HERE/'state.json').read_text())
D=dt.date.fromisoformat

def synthetic():
    start=D('2025-01-01');end=D('2026-12-31')
    data={}
    for k,sym in CFG['tickers'].items():
        bars={}
        d=start;p=100.
        while d<=end:
            if d.weekday()<5:
                p*=1.00025
                bars[str(d)]={'open':p*.999,'close':p}
            d+=dt.timedelta(days=1)
        data[sym]={'bars':bars,'currency':'EUR','feed':'SYNTHETIC'}
    return data

def time_for(day):return dt.datetime.fromisoformat(day+'T20:10:00+00:00')

def pristine_state():
    """Deterministic fixture, independent of the live mutable paper ledger."""
    st=copy.deepcopy(SEED)
    st.update({'last_processed_session':'2026-11-07',
               'last_deposit_month':'2026-11','last_risk_on':None,
               'cash_eur':1000.,'positions':{},'orders':[],
               'contributions':[],'signal_history':[],'blocked_actions':[],
               'pending':None,'stamp_accrued_eur':0.,'off_trough':None,
               'nav':[{'date':'2026-11-07','nav_eur':1000.,
                       'cash_eur':1000.,'holdings_eur':0.,
                       'twr_index':1.,'market_session':False,
                       'source_status':'SYNTHETIC_SEED_NO_ORDERS'}]})
    return st

class PACLiveTests(unittest.TestCase):
    def test_original_approved_weights_and_no_real_broker(self):
        self.assertEqual(CFG['weights'],{'WORLD':.65,'NASDAQ':.15,'EM':.10,'GOLD':.10})
        self.assertTrue(CFG['no_broker_execution'])
        self.assertEqual(CFG['max_total_orders_per_month'],3)
    def test_missing_quotes_fail_closed(self):
        d=synthetic();del d['IS3N.DE']['bars']['2026-11-09']
        self.assertEqual(e.common_day(d,D('2026-11-09')),'2026-11-06')
    def test_precommit_next_open_not_same_day(self):
        d=synthetic();st=pristine_state()
        st,done=e.process(CFG,st,'2026-11-09',d,time_for('2026-11-09'))
        self.assertEqual(done,0)
        self.assertEqual(len(st['orders']),0)
        self.assertEqual(st['pending']['decision_session'],'2026-11-09')
        self.assertEqual(st['cash_eur'],1000)
        st,done=e.process(CFG,st,'2026-11-10',d,time_for('2026-11-10'))
        self.assertEqual(done,3)
        self.assertEqual(len(st['orders']),3)
        self.assertGreater(st['nav'][-1]['holdings_eur'],0)
        self.assertEqual(st['nav'][-1]['contribution_eur'],0)
        for order in st['orders']:
            self.assertEqual(order['decision_session'],'2026-11-09')
            self.assertEqual(order['execution_session'],'2026-11-10')
            self.assertEqual(order['mode'],'VIRTUAL_ONLY')
    def test_retry_cannot_duplicate_orders_or_contributions(self):
        d=synthetic();st=pristine_state()
        st,_=e.process(CFG,st,'2026-11-09',d,time_for('2026-11-09'))
        st,_=e.process(CFG,st,'2026-11-10',d,time_for('2026-11-10'))
        a=copy.deepcopy(st)
        again,done=e.process(CFG,st,'2026-11-10',d,time_for('2026-11-10'))
        self.assertEqual(done,0)
        self.assertEqual(again,a)
    def test_monthly_pac_is_cashflow_not_fake_return(self):
        d=synthetic();st=pristine_state()
        st['last_processed_session']='2026-11-30'
        st['nav'][-1]['date']='2026-11-30'
        st,done=e.process(CFG,st,'2026-12-01',d,time_for('2026-12-01'))
        self.assertEqual(done,0)
        self.assertEqual(st['cash_eur'],1250)
        self.assertEqual(st['nav'][-1]['nav_eur'],1250)
        self.assertEqual(st['nav'][-1]['twr_day_pct'],0)
        self.assertEqual(len(st['contributions']),1)
        st,_=e.process(CFG,st,'2026-12-02',d,time_for('2026-12-02'))
        self.assertEqual(len(st['contributions']),1)
    def test_35_days_protects_recently_bought_units(self):
        s=pristine_state()
        s['positions']={'WORLD':{'shares':100,'cost_basis_eur':10000,
                                'last_buy_session':'2026-11-01'}}
        signal={'signal_session':'2026-11-09','target_weights':
                {'WORLD':0.,'NASDAQ':0.,'EM':0.,'GOLD':0.}}
        orders,blocked=e.plan(CFG,s,signal,{'WORLD':100.,'NASDAQ':100.,'EM':100.,'GOLD':100.},'2026-11')
        self.assertFalse(any(x['side']=='SELL' for x in orders))
        self.assertTrue(any(x.startswith('MIN_35_DAY_HOLD_WORLD') for x in blocked))
    def test_no_fabricated_entries_before_first_future_session(self):
        seed=pristine_state()
        self.assertEqual(seed['orders'],[])
        self.assertEqual(seed['last_processed_session'],'2026-11-07')
        self.assertEqual(seed['cash_eur'],1000)
        # Persisted live state is allowed to advance independently of tests.
        self.assertGreaterEqual(SEED['last_processed_session'],seed['last_processed_session'])
    def test_future_produced_signal_rejected_before_fill(self):
        st=pristine_state()
        st['pending']={'decision_session':'2026-11-09',
                       'created_at_utc':'2026-11-10T12:00:00+00:00',
                       'proposed_orders':[{'asset':'WORLD','side':'BUY',
                         'budget_eur':600,'date':'2026-11-09','reason':'synthetic'}]}
        st['last_processed_session']='2026-11-09'
        st,done=e.process(CFG,st,'2026-11-10',synthetic(),time_for('2026-11-10'))
        self.assertEqual(done,0)
        self.assertEqual(st['orders'],[])
        self.assertTrue(any(x['reason']=='DEFERRED_NO_PREOPEN_SIGNAL_PROVENANCE'
                            for x in st['blocked_actions']))
        self.assertEqual(st['pending']['decision_session'],'2026-11-09')
        self.assertEqual(st['pending']['created_at_utc'],'2026-11-10T12:00:00+00:00')
        st,done=e.process(CFG,st,'2026-11-11',synthetic(),time_for('2026-11-11'))
        self.assertEqual(done,1)
        self.assertEqual(st['orders'][0]['execution_session'],'2026-11-11')

    def test_delayed_quotes_retain_original_preopen_timestamp(self):
        d=synthetic();st=pristine_state()
        st['last_processed_session']='2026-11-09'
        st['nav'][-1]['date']='2026-11-09'
        st['pending']={'decision_session':'2026-11-09',
                       'created_at_utc':'2026-11-12T09:35:00+00:00',
                       'proposed_orders':[{'asset':'WORLD','side':'BUY',
                         'budget_eur':650,'date':'2026-11-09','reason':'synthetic'}]}
        for session in ('2026-11-10','2026-11-11','2026-11-12'):
            st,done=e.process(CFG,st,session,d,time_for('2026-11-12'))
            self.assertEqual(done,0)
            self.assertEqual(st['pending']['created_at_utc'],'2026-11-12T09:35:00+00:00')
            self.assertEqual(len(st['orders']),0)
        st,done=e.process(CFG,st,'2026-11-13',d,
                          dt.datetime.fromisoformat('2026-11-14T00:20:00+00:00'))
        self.assertEqual(done,1)
        self.assertEqual(st['orders'][0]['execution_session'],'2026-11-13')
        self.assertIsNone(st['pending'])


if __name__=='__main__':unittest.main()
