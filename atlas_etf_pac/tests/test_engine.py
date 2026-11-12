import datetime as dt, hashlib, json, sys, unittest
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
    def test_paper_rules_no_35days_no_monthly_cap(self):
        self.assertEqual(CFG['min_hold_days_before_sell'],0)
        self.assertIsNone(CFG['max_total_orders_per_month'])
        self.assertTrue(CFG['no_broker_execution'])

if __name__=='__main__':unittest.main()
