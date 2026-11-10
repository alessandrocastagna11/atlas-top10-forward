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
