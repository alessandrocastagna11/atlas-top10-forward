"""Per-exchange causal signal tests: a late Asian open cannot block US signals."""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import run_live as live


class ExchangeSignalTests(unittest.TestCase):
    def test_utc_exchange_opens_and_non_us_week(self):
        d=dt.date(2026,11,10)
        self.assertEqual(live.symbol_next_open("AAPL",d)[1],dt.datetime(2026,11,10,14,30,tzinfo=dt.timezone.utc))
        self.assertEqual(live.symbol_next_open("7203.T",d)[1],dt.datetime(2026,11,10,0,0,tzinfo=dt.timezone.utc))
        self.assertEqual(live.symbol_next_open("2222.SR",dt.date(2026,11,13))[0],dt.date(2026,11,15))

    def test_thanksgiving_and_black_friday_calendar(self):
        day=dt.date(2026,11,26)
        session,opening=live.symbol_next_open("KO",day)
        self.assertEqual(session,dt.date(2026,11,27))
        self.assertEqual(opening,dt.datetime(2026,11,27,14,30,tzinfo=dt.timezone.utc))
        cal,verified=live.listing_calendar("KO")
        self.assertTrue(verified)
        self.assertFalse(cal.is_session(pd.Timestamp(day)))
        self.assertEqual(cal.session_close(pd.Timestamp(session)),
                         pd.Timestamp("2026-11-27T18:00:00Z"))
        self.assertEqual(live.symbol_next_open("KO",day,
            not_before=dt.datetime(2026,11,27,14,30,tzinfo=dt.timezone.utc))[0],
            dt.date(2026,11,30))
        self.assertEqual(live.symbol_next_open("7203.T",day)[0],day)
        self.assertEqual(live.symbol_next_open("SPY",day)[0],session)

    def test_christmas_and_dst(self):
        self.assertEqual(live.symbol_next_open("SPY",dt.date(2026,12,25))[0],
                         dt.date(2026,12,28))
        self.assertEqual(live.symbol_next_open("SPY",dt.date(2026,7,6))[1],
                         dt.datetime(2026,7,6,13,30,tzinfo=dt.timezone.utc))
        self.assertFalse(live.listing_calendar("RELIANCE.NS")[1])

    def test_corrected_holiday_revalidates_immutable_proof(self):
        date=pd.Timestamp("2026-11-25")
        cols=["KO"]
        vals=pd.DataFrame([[0.6]],index=[date],columns=cols)
        yes=pd.DataFrame(True,index=[date],columns=cols)
        m=SimpleNamespace(bundle={"cfg":None},
            callbacks=lambda _: (lambda *args:cols,None,None,None),
            path_score=vals,eligible=yes,membership=yes,bear_floor={},
            pr_regime=pd.Series([0.8],index=[date]),
            pr_t8=pd.Series([1.],index=[date]))
        meta={"ticker_to_market_symbol":{"KO":"KO"},
              "roster_sha256":"fixture","active_roster_names":1}
        now=dt.datetime(2026,11,26,16,0,tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with mock.patch.object(live,"PREOPEN",root/"preopen"), \
                 mock.patch.object(live,"FORWARD",root/"forward"), \
                 mock.patch.object(live,"CACHE",root/"cache"):
                proof=live.preview(m,meta,date.date(),now,[])
                self.assertEqual(proof["execution_session"],"2026-11-27")
                p=root/"preopen"/"2026-11-25.json"
                prior=json.loads(p.read_text())
                prior["execution_session"]="2026-11-26"
                prior["produced_at_utc"]="2026-11-26T04:58:39+00:00"
                item=prior["per_symbol_exchange_plan"]["KO"]
                item["first_candidate_open_utc"]="2026-11-26T14:30:00+00:00"
                item["exchange_holiday_calendar_verified"]=False
                p.write_text(json.dumps(prior))
                original=p.read_bytes()
                out=live.preview(m,meta,date.date(),now,[])
                plan=out["per_symbol_exchange_plan"]["KO"]
                self.assertEqual(out["execution_session"],"2026-11-27")
                self.assertEqual(plan["next_eligible_local_session"],"2026-11-27")
                self.assertTrue(plan["original_calendar_plan_revalidated"])
                self.assertTrue(plan["attested_before_first_candidate_open"])
                self.assertEqual(p.read_bytes(),original)

    def test_us_signal_available_after_asian_open(self):
        date=pd.Timestamp("2026-11-09")
        cols=["AAPL","G::7203.T"]
        vals=pd.DataFrame([[0.6,0.4]],index=[date],columns=cols)
        yes=pd.DataFrame(True,index=[date],columns=cols)
        def choose(day,score,ranks,elig,positions,cfg):
            return cols
        m=SimpleNamespace(
            bundle={"cfg":None},
            callbacks=lambda _: (choose,None,None,None),
            path_score=vals,eligible=yes,membership=yes,
            bear_floor={},pr_regime=pd.Series([0.8],index=[date]),
            pr_t8=pd.Series([1.],index=[date]))
        meta={"ticker_to_market_symbol":{"AAPL":"AAPL","G::7203.T":"7203.T"},
              "roster_sha256":"fixture","active_roster_names":2}
        now=dt.datetime(2026,11,10,10,0,tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with mock.patch.object(live,"PREOPEN",root/"preopen"), \
                 mock.patch.object(live,"FORWARD",root/"forward"), \
                 mock.patch.object(live,"CACHE",root/"cache"):
                out=live.preview(m,meta,dt.date(2026,11,9),now,[])
                self.assertEqual(out["status"],"SIGNAL_PUBLISHED_PER_EXCHANGE")
                plan=out["per_symbol_exchange_plan"]
                self.assertTrue(plan["AAPL"]["attested_before_first_candidate_open"])
                self.assertFalse(plan["7203.T"]["attested_before_first_candidate_open"])
                self.assertEqual(plan["7203.T"]["status"],"DEFERRED_PAST_FIRST_LOCAL_OPEN")
                self.assertEqual(plan["7203.T"]["next_eligible_local_session"],"2026-11-11")
                saved=json.loads((root/"forward"/"latest_signal.json").read_text())
                self.assertTrue(saved["signal_published"])
                self.assertEqual(saved["decision_session"],"2026-11-09")
                self.assertFalse(saved["market_data_first_observed_before_open"])
                self.assertTrue(saved["any_market_preopen_eligible"])


if __name__=="__main__":
    unittest.main()
