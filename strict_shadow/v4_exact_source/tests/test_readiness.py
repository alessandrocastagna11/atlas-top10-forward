import datetime as dt
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from readiness import EXPECTED_SHA256, assess, source_integrity

class OriginalEngineReadiness(unittest.TestCase):
    def test_exact_original_source_hashes(self):
        src = source_integrity()
        self.assertEqual(len(src), 7)
        self.assertTrue(all(x['exact_match'] for x in src.values()))

    def test_frozen_engine_stays_fail_closed_in_2026(self):
        report = assess(now=dt.datetime(2026, 11, 9, tzinfo=dt.timezone.utc))
        self.assertTrue(report['original_engine_source_present'])
        self.assertEqual(report['status'], 'PENDING_EXACT_2026_PIT_ENGINE_ADAPTER')
        self.assertFalse(report['verified_frozen_engine_2026_execution'])
        self.assertFalse(report['allowed_to_use_cohort_proxy_signals'])
        self.assertEqual(report['automatic_broker_orders'], 0)

    def test_tampered_engine_must_fail_integrity(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            p = next(iter(EXPECTED_SHA256))
            (root/p).parent.mkdir(parents=True, exist_ok=True)
            (root/p).write_text('changed source', encoding='utf-8')
            integrity = source_integrity(root)
            self.assertFalse(all(x['exact_match'] for x in integrity.values()))

if __name__ == '__main__': unittest.main()
