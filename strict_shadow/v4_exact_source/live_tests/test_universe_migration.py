"""Regression using immutable, observed ranking captures; no network or ledger writes."""
import datetime as dt
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from live_model import latest_vintages,universe_transition_audit

class UniverseMigrationTests(unittest.TestCase):
    def test_pre_cutover_roster_unchanged(self):
        day=dt.date(2026,11,27)
        vintages=latest_vintages(day)
        self.assertEqual(vintages[-1][2],'569eb5f4044031c4e32304cbf280c4768d32096cf184f50b45ff5c4813ae82b5')
        self.assertEqual(universe_transition_audit(vintages,day)['status'],'LEGACY_PRE_CUTOVER')
    def test_expanded_cutover_uses_real_capture(self):
        day=dt.date(2026,11,30)
        vintages=latest_vintages(day)
        audit=universe_transition_audit(vintages,day)
        self.assertEqual(audit['status'],'EXPANDED_ACTIVE')
        self.assertEqual(audit['candidate_pool_size'],750)
        self.assertEqual(audit['matched_issuers'],711)
        self.assertEqual(audit['active_source_date'],'2026-11-28')
        self.assertEqual(audit['active_roster_effective_date'],'2026-11-30')
        self.assertEqual(audit['active_usa'],100)
        self.assertEqual(audit['active_nonusa'],200)
        self.assertEqual(audit['common_usa'],99)
        self.assertEqual(audit['common_nonusa'],171)
        self.assertEqual(len(audit['entered']),30)
        self.assertEqual(len(audit['exited']),30)
        self.assertEqual(len(set(audit['active_symbols'])),300)
        old=latest_vintages(dt.date(2026,11,27))
        self.assertEqual([x[:3] for x in old],[x[:3] for x in vintages if x[0]<=dt.date(2026,11,27)])

if __name__=='__main__':unittest.main()
