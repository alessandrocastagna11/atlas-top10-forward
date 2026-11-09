"""Offline safety and code parity regression tests."""
import ast,hashlib,sys,unittest,datetime as dt
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import live_model,live_simulator,frozen_region

class GuardTests(unittest.TestCase):
    def test_frozen_constructor_keeps_five_week_cadence(self):
        config=live_model.v2.V2Config()
        self.assertEqual(config.decision_interval_weeks,5)
        self.assertEqual(config.minimum_holding_calendar_days,21)
        # Original frozen bundle used 35, explicitly recreated by our adapter.
        src=Path(live_model.__file__).read_text()
        self.assertIn('minimum_holding_calendar_days=35',src)
        self.assertIn('minimum_trade_eur=500',src)
    def test_live_simulator_does_not_auto_liquidate(self):
        source=Path(live_simulator.__file__).read_text()
        self.assertIn('if liquidate_on_final_session and len(sim_index) and positions:',source)
        self.assertIn('liquidate_on_final_session: bool = False',source)
    def test_membership_is_never_present_before_observed(self):
        root=live_model.LIVE/'dataset'/'universe'/'snapshots'
        if not root.exists():self.skipTest('historical snapshots are not installed')
        try:items=live_model.latest_vintages(dt.date(2026,11,4))
        except ValueError:items=[]
        self.assertFalse(items)
    def test_region_source_is_verbatim(self):
        source=Path(frozen_region.__file__).read_text()
        tree=ast.parse(source);cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='RegionSlotAtlas')
        original=''.join(source.splitlines(True)[cls.lineno-1:cls.end_lineno])
        self.assertEqual(hashlib.sha256(original.encode()).hexdigest(),
                         'bb1d65f3035577ef8dfbb68f0607f762b501b1b3b2d8fc91ff406dbb652dc8a7')

if __name__=='__main__':unittest.main()
