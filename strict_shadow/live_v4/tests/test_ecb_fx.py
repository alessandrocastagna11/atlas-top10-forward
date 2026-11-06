"""Offline ECB FX provenance and timing tests. Synthetic data only."""
import csv,datetime as dt,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import ecb_fx as e

D=dt.date.fromisoformat
SAMPLE=("FREQ,CURRENCY,CURRENCY_DENOM,TIME_PERIOD,OBS_VALUE\n"
        "D,USD,EUR,2026-11-03,1.12\n"
        "D,USD,EUR,2026-11-04,1.13\n"
        "D,JPY,EUR,2026-11-03,162.5\n").encode()

class ECBTests(unittest.TestCase):
    def test_valid_daily_ecb_not_interpolated(self):
        got=e.parse_ecb_csv(SAMPLE,D('2026-11-06'))
        self.assertEqual(len(got),3)
        self.assertEqual(got[('USD','2026-11-04')],1.13)
        self.assertNotIn(('USD','2026-11-05'),got)
    def test_future_and_wrong_schema_blocked(self):
        with self.assertRaises(ValueError):e.parse_ecb_csv(SAMPLE,D('2026-11-03'))
        with self.assertRaises(ValueError):e.parse_ecb_csv(b"bad,header\nA,B\n",D('2026-11-06'))
    def test_publication_lag_prevents_same_day_lookahead(self):
        rows=[{'date':'2026-11-05','currency':'USD','units_per_eur':'999.0'},
              {'date':'2026-11-04','currency':'USD','units_per_eur':'1.13'}]
        observed,rate=e.published_prior(rows,'USD',D('2026-11-05'))
        self.assertEqual(str(observed),'2026-11-04')
        self.assertEqual(rate,1.13)
    def test_revisions_are_logged_with_original_observed_timestamp(self):
        with tempfile.TemporaryDirectory() as folder:
            dest=Path(folder)
            x=e.parse_ecb_csv(SAMPLE,D('2026-11-06'))
            e.merge(x,'2026-11-06T22:00:00+00:00','h1',folder=dest)
            second=dict(x);second[('USD','2026-11-04')]=1.14
            summary=e.merge(second,'2026-11-07T22:00:00+00:00','h2',folder=dest)
            self.assertEqual(summary['USD']['revisions'],1)
            us=list(csv.DictReader((dest/'USD.csv').open()))
            edited=next(x for x in us if x['date']=='2026-11-04')
            self.assertEqual(edited['first_observed_at_utc'],'2026-11-06T22:00:00+00:00')
            self.assertEqual(edited['units_per_eur'],'1.14')
            rev=list(csv.DictReader((dest/'revisions.csv').open()))
            self.assertEqual(len(rev),1)
            self.assertEqual(rev[0]['previous_units_per_eur'],'1.13')
    def test_unavailable_ecb_does_not_claim_pit_completeness(self):
        with tempfile.TemporaryDirectory() as folder:
            prior=e.ROOT;e.ROOT=Path(folder)
            try:
                def fail(start,end):raise OSError('offline')
                result=e.run(dt.datetime(2026,11,6,22,tzinfo=dt.timezone.utc),fail)
                self.assertEqual(result['status'],'ECB_REFERENCE_UNAVAILABLE')
                self.assertFalse(result['original_confidence70_pit_certified'])
            finally:e.ROOT=prior

if __name__=='__main__':unittest.main()
