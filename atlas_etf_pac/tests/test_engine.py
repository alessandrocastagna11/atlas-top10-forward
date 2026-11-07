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

class PACLiveTests(unittest.TestCase):
    def test_original_approved_weights_and_no_real_broker(self):
        self.assertEqual(CFG['weights'],{'WORLD':.65,'NASDAQ':.15,'EM':.10,'GOLD':.10})
        self.assertTrue(CFG['no_broker_execution'])
        self.assertEqual(CFG['max_total_orders_per_month'],3)
    def test_missing_quotes_fail_closed(self):
        d=synthetic();del d['IS3N.DE']['bars']['2026-11-09']
        self.assertEqual(e.common_day(d,D('2026-11-09')),'2026-11-06')
    def test_precommit_next_open_not_same_day(self):
        d=synthetic();st=copy.deepcopy(SEED)
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
        d=synthetic();st=copy.deepcopy(SEED)
        st,_=e.process(CFG,st,'2026-11-09',d,time_for('2026-11-09'))
        st,_=e.process(CFG,st,'2026-11-10',d,time_for('2026-11-10'))
        a=copy.deepcopy(st)
        again,done=e.process(CFG,st,'2026-11-10',d,time_for('2026-11-10'))
        self.assertEqual(done,0)
        self.assertEqual(again,a)
    def test_monthly_pac_is_cashflow_not_fake_return(self):
        d=synthetic();st=copy.deepcopy(SEED)
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
        s=copy.deepcopy(SEED)
        s['positions']={'WORLD':{'shares':100,'cost_basis_eur':10000,
                                'last_buy_session':'2026-11-01'}}
        signal={'signal_session':'2026-11-09','target_weights':
                {'WORLD':0.,'NASDAQ':0.,'EM':0.,'GOLD':0.}}
        orders,blocked=e.plan(CFG,s,signal,{'WORLD':100.,'NASDAQ':100.,'EM':100.,'GOLD':100.},'2026-11')
        self.assertFalse(any(x['side']=='SELL' for x in orders))
        self.assertTrue(any(x.startswith('MIN_35_DAY_HOLD_WORLD') for x in blocked))
    def test_no_fabricated_entries_before_first_future_session(self):
        self.assertEqual(SEED['orders'],[])
        self.assertEqual(SEED['last_processed_session'],'2026-11-07')
        self.assertEqual(SEED['cash_eur'],1000)
    def test_future_produced_signal_rejected_before_fill(self):
        st=copy.deepcopy(SEED)
        st['pending']={'decision_session':'2026-11-09',
                       'created_at_utc':'2026-11-10T12:00:00+00:00',
                       'proposed_orders':[{'asset':'WORLD','side':'BUY',
                         'budget_eur':600,'date':'2026-11-09','reason':'synthetic'}]}
        st['last_processed_session']='2026-11-09'
        with self.assertRaisesRegex(ValueError,'NO_PREOPEN_SIGNAL_PROVENANCE'):
            e.process(CFG,st,'2026-11-10',synthetic(),time_for('2026-11-10'))

if __name__=='__main__':unittest.main()
