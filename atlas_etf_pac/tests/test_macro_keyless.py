import datetime as dt, json, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import macro_keyless as m

class TestKeylessShadow(unittest.TestCase):
    def test_calendar_thanksgiving_and_early_close(self):
        before=dt.datetime(2026,11,27,18,0,tzinfo=dt.timezone.utc)
        after=dt.datetime(2026,11,27,21,0,tzinfo=dt.timezone.utc)
        self.assertEqual(m.us_cutoff(before),'2026-11-25')
        self.assertEqual(m.us_cutoff(after),'2026-11-27')
        self.assertEqual(m.us_cutoff(dt.datetime(2026,11,28,12,tzinfo=dt.timezone.utc)),'2026-11-27')
    def test_calendar_covers_research_history(self):
        cal=m.xcals.get_calendar('XNYS',start='2000-01-01',end='2035-12-31')
        self.assertTrue(cal.is_session(m.pd.Timestamp('2001-01-02')))
    def test_missing_is_not_zero_stress(self):
        self.assertIsNone(m.score({},'2026-11-27')['score'])
    def test_cache_regression_rejected(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(m,'CACHE',Path(d)), mock.patch.object(m,'END','2026-11-27'):
            now=dt.datetime(2026,11,28,12,tzinfo=dt.timezone.utc)
            old={'rows':{'2026-11-25':100.,'2026-11-27':101.},'meta':{},'downloaded_at_utc':now.isoformat()}
            x,a=m.use_cache_or_new('HYG',{'2026-11-27':101.},{},now,old)
            self.assertEqual(x,old['rows']);self.assertEqual(a['acquisition'],'CACHE_AFTER_ERROR')
    def test_report_never_mutates_ledger_or_original_score(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);status=root/'status.json';audit=root/'audit.json';ledger=root/'state.json';shadow=root/'shadow.json'
            original={'latest_signal':{'decision_session':'2026-11-27','stress':{'score':1}},'orders_total':1,'pipeline_health':'DEGRADED_MACRO'}
            status.write_text(json.dumps(original));audit.write_text(json.dumps({'status':'old'}));ledger.write_text('immutable')
            with mock.patch.object(m,'REPORT',shadow),mock.patch.object(m,'STATUS',status),mock.patch.object(m,'AUDIT',audit),mock.patch.object(m,'acquire',return_value=({},{})):
                self.assertEqual(m.report(dt.datetime(2026,11,28,12,tzinfo=dt.timezone.utc),original),3)
            x=json.loads(status.read_text());self.assertEqual(x['latest_signal'],original['latest_signal'])
            self.assertEqual(x['orders_total'],1);self.assertEqual(x['pipeline_health'],'DEGRADED_MACRO')
            self.assertEqual(ledger.read_text(),'immutable')
            self.assertIsNone(x['macro_keyless_shadow']['latest_monitor']['score'])
if __name__=='__main__':unittest.main()
