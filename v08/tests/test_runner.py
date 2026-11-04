"""Synthetic isolated integration tests; never modify GitHub state."""
import unittest,sys,tempfile,copy,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import run_shadow as r
import shadow_engine as e
from test_shadow import fixture

class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.orig=(r.ROOT,r.STATE,r.REPORTS,r.MARKET,r.ROSTERS)
        self.addCleanup(self.restore)
        r.ROOT=root;r.STATE=root/'state';r.REPORTS=root/'reports';r.MARKET=root/'market_data';r.ROSTERS=root/'rosters'
        r.init()
    def restore(self):r.ROOT,r.STATE,r.REPORTS,r.MARKET,r.ROSTERS=self.orig
    def test_seed_and_identity(self):
        for s in e.STRATEGIES:
            st=r.load(s);e.validate(st)
            self.assertEqual(len(st['orders']),0)
            self.assertEqual(st['last_date'],'2026-11-04')
            self.assertEqual(st['security_ids']['SPCX']['isin'],'US84615Q1031')
    def test_no_trade_before_first_close(self):
        o=r.replay('2026-11-04',{})
        self.assertTrue(all(v['status']=='PENDING_FIRST_OPEN' for v in o.values()))
        self.assertTrue(all(v['orders']==0 for v in o.values()))
    def test_three_independent_accounts(self):
        d=fixture();o=r.replay('2026-11-05',d)
        self.assertEqual(set(o),set(e.STRATEGIES))
        self.assertTrue(all(v['orders']>0 for v in o.values()))
        self.assertTrue(all(v['benchmark_eur']>0 for v in o.values()))
        self.assertEqual(r.load('MA10_Open')['last_date'],'2026-11-05')
        self.assertEqual(r.load('Adaptive_Risk')['last_date'],'2026-11-05')
    def test_replay_no_duplicate_orders(self):
        d=fixture();r.replay('2026-11-05',d)
        n=len(r.load('MA10_Open')['orders'])
        r.replay('2026-11-05',d)
        self.assertEqual(n,len(r.load('MA10_Open')['orders']))
    def test_fx_missing_blocks_without_partial_commit(self):
        d=fixture();d['EURUSD=X']['bars']={'2026-11-05':{'open':1.,'close':1.}}
        x=r.replay('2026-11-05',d)
        self.assertEqual(x['MA10_Open']['orders'],0)
        self.assertEqual(r.load('MA10_Open')['last_date'],'2026-11-04')
    def test_pending_missing_spcx_then_fill(self):
        d=fixture();d['SPCX']['bars'].pop('2026-11-05')
        r.replay('2026-11-05',d)
        st=r.load('MA10_Open')
        self.assertEqual(len(st['pending']),1)
        self.assertEqual(st['pending'][0]['symbol'],'SPCX')
        r.replay('2026-11-06',d)
        self.assertEqual(len(r.load('MA10_Open')['pending']),0)
    def test_benchmark_missing_no_equity_open(self):
        d=fixture();d[e.BENCH]['bars'].pop('2026-11-05')
        r.replay('2026-11-05',d)
        self.assertEqual(len(r.load('MA10_Open')['orders']),0)
        self.assertEqual(r.load('MA10_Open')['benchmark']['shares'],0)
    def test_manifest_provenance_year_end(self):
        roster,valid,err=r.validate_roster('2027-02-05')
        self.assertFalse(valid);self.assertEqual(err,'ROSTER_NOT_VERIFIED')
        r.ROSTERS.mkdir(exist_ok=True)
        (r.ROSTERS/'2026.json').write_text(json.dumps({'symbols':e.BASKET,'source_url':'https://example.org/archive',
            'snapshot_asof':'2026-12-31','published_at':'2027-03-01'}))
        _,valid,err=r.validate_roster('2027-02-05')
        self.assertFalse(valid);self.assertEqual(err,'ROSTER_LOOKAHEAD')
    def test_csv_records_well_formed(self):
        r.replay('2026-11-05',fixture())
        path=r.REPORTS/'Adaptive_Risk_orders.csv'
        self.assertIn('fx_asof',path.read_text().splitlines()[0])
    def test_report_distinguishes_simulated(self):
        o=r.replay('2026-11-05',fixture())
        r.render('2026-11-05',o,{'missing_symbols':{},'asof':'2026-11-05'})
        s=(r.REPORTS/'latest.md').read_text()
        self.assertIn('SHADOW',s)
        self.assertIn('non certificati',s)

if __name__=='__main__':unittest.main()