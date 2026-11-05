"""Synthetic offline incremental dataset tests."""
import csv,datetime as dt,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import dataset_daily as mod
from provider import Bar
D=dt.date.fromisoformat

class FakeProvider:
    def __init__(self,days):self.days=days
    def history(self,symbol,start,end):
        return {d:Bar(d,100.,101.,'USD',symbol,'abc123') for d in self.days if start<=d<=end}

class DatasetTests(unittest.TestCase):
    def test_recovered_symbols_exact_count(self):
        self.assertEqual(len(mod.old_symbols()),288)
    def test_incremental_and_idempotent(self):
        old=mod.DATA
        try:
            with tempfile.TemporaryDirectory() as temp:
                mod.DATA=Path(temp)
                one=mod.update_history(FakeProvider([D('2026-11-03'),D('2026-11-04')]),'ABC','prices',D('2026-11-05'),D('2025-12-01'))
                self.assertEqual(one['inserted'],2)
                same=mod.update_history(FakeProvider([D('2026-11-03'),D('2026-11-04')]),'ABC','prices',D('2026-11-05'),D('2025-12-01'))
                self.assertEqual(same['inserted'],0)
                self.assertEqual(same['revised'],0)
                self.assertEqual(len(mod.rows_load(mod.DATA/'prices'/mod.symbol_filename('ABC'))),2)
        finally:mod.DATA=old
    def test_numeric_symbol_filename_preserved(self):
        self.assertNotEqual(mod.symbol_filename('A/B'),mod.symbol_filename('A_B'))
    def test_choose_rotates_and_caps(self):
        hist=['AAPL','MSFT','NVDA','XOM']+['SYM'+str(i) for i in range(60)]
        one,total,c=mod.choose_symbols(hist,[],25,{})
        two,total2,c2=mod.choose_symbols(hist,[],25,{'round_robin_cursor':c})
        self.assertEqual(len(one),25)
        self.assertNotEqual(one,two)
        self.assertEqual(total,total2)
        self.assertEqual(one[0],'AAPL')
    def test_ambiguous_symbol_mapping_rejected(self):
        self.assertEqual(mod.derive('XXX','Unknown Exchange','Unknown'), '')
    def test_merger_delisting_not_an_active_quote_requirement(self):
        self.assertNotIn('WBD',mod.terminal_symbols(D('2026-10-05')))
        self.assertIn('WBD',mod.terminal_symbols(D('2026-11-05')))
    def test_known_issuer_remains_in_historical_inventory(self):
        self.assertIn('WBD',mod.old_symbols())
    def test_roster_is_proxy_not_market_cap(self):
        # Labels are explicit and never used as the original V4 engine's true market cap ranks.
        source=mod.universe_snapshot.__doc__ or ''
        self.assertIn('NOT market cap',source)
if __name__=='__main__':unittest.main()
