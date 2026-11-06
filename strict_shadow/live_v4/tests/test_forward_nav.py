"""Synthetic-only daily paper NAV journal integrity tests."""
import datetime as dt,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import forward_nav as j

def stamp(day):return dt.datetime.fromisoformat(day+'T23:00:00+00:00')
def nav(day,value):
    return {'session':day,'nav_eur':value,'cash_eur':7603.92,'positions':8}

class NAVHistoryTests(unittest.TestCase):
    def test_initial_cost_and_idempotent_same_day_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'nav.json'
            raw,s=j.record(path,nav('2026-11-06',9989.61),{},stamp('2026-11-06'))
            self.assertEqual(s['unique_sessions'],1)
            self.assertEqual(s['day_return_pct'],-0.1039)
            raw,s=j.record(path,nav('2026-11-06',9989.61),{'another':'provider'},stamp('2026-11-06'))
            self.assertEqual(len(raw['observations']),1)
            self.assertEqual(s['revision_count'],0)
    def test_daily_return_drawdown_and_revision_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'nav.json'
            j.record(path,nav('2026-11-06',9989.61),{},stamp('2026-11-06'))
            _,s=j.record(path,nav('2026-11-09',10020.00),{},stamp('2026-11-09'))
            self.assertEqual(s['unique_sessions'],2)
            self.assertGreater(s['day_return_pct'],0)
            _,s=j.record(path,nav('2026-11-09',9990.00),{'revision':True},stamp('2026-11-09'))
            self.assertEqual(s['revision_count'],1)
            self.assertEqual(s['unique_sessions'],2)
            raw=json.loads(path.read_text())
            self.assertIn('revises_previous_sha256',raw['observations'][-1])
    def test_future_nav_and_negative_nav_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'nav.json'
            with self.assertRaises(ValueError):
                j.record(p,nav('2026-11-09',9990),{},stamp('2026-11-06'))
            with self.assertRaises(ValueError):
                j.record(p,nav('2026-11-06',-1),{},stamp('2026-11-06'))
if __name__=='__main__':unittest.main()
