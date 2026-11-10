"""Regression checks for date-effective membership and downtime recovery."""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import sys
import pandas as pd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import live_model,run_live


def snap(day,us,row):
    def record(symbol):return {'symbol':symbol,'sector':'Information Technology','currency':'USD'}
    return (dt.date.fromisoformat(day),dt.date.fromisoformat(day),'dummy-'+day,
            [record(s) for s in us],[record(s) for s in row])


class DailyRecoveryTests(unittest.TestCase):
    def test_new_roster_never_rewrites_prior_date(self):
        index=pd.bdate_range('2026-11-04','2026-11-12')
        groups=[snap('2026-11-06',['A'],['X']),snap('2026-11-09',['B'],['Y'])]
        m=live_model.asof_membership(index,groups,['A','B'],['G::X','G::Y'])
        self.assertFalse(m.loc['2026-11-05'].any())
        self.assertTrue(m.loc['2026-11-06','A'])
        self.assertFalse(m.loc['2026-11-06','B'])
        self.assertTrue(m.loc['2026-11-09','B'])
        self.assertFalse(m.loc['2026-11-09','A'])
        self.assertFalse(m.loc['2026-11-06','G::Y'])
        self.assertTrue(m.loc['2026-11-09','G::Y'])

    def test_non_attested_missed_day_recovered_as_paper_only(self):
        date=pd.Timestamp('2026-11-09')
        score=pd.DataFrame({'AAA':[2.]},index=[date])
        flag=pd.DataFrame({'AAA':[True]},index=[date])
        model=SimpleNamespace(
            bundle={'cfg':object()},
            path_score=score,eligible=flag,membership=flag,
            pr_regime=pd.Series([.8],index=[date]),
            pr_t8=pd.Series([1.],index=[date]),
            callbacks=lambda _: (lambda *args: ['AAA'],None,None,None))
        order={'decision_date':'2026-11-09','date':'2026-11-10',
               'reason':'target_rebalance','ticker':'AAA','action':'BUY',
               'shares':1.,'execution_price_eur':100.,'fee_eur':1.}
        prior=run_live.PREOPEN
        try:
            with tempfile.TemporaryDirectory() as directory:
                run_live.PREOPEN=Path(directory)
                result,dates=run_live.order_timecheck(
                    [order],[],model,dt.datetime(2026,11,12,tzinfo=dt.timezone.utc),
                    dt.date(2026,11,11))
                self.assertEqual(result,[order])
                self.assertEqual(dates,['2026-11-09'])
                audit=json.loads((Path(directory)/'recoveries'/'2026-11-09.json').read_text())
                self.assertTrue(audit['simulated_execution_not_real_order'])
                self.assertEqual(audit['mode'],'RECOVERED_HISTORICAL_PAPER_SIMULATION_NOT_LIVE_FILL')
        finally:run_live.PREOPEN=prior

    def test_cannot_recover_unfinished_session(self):
        date=pd.Timestamp('2026-11-09')
        model=SimpleNamespace()
        row={'decision_date':'2026-11-09','date':'2026-11-12','action':'BUY','reason':'target_rebalance'}
        with self.assertRaisesRegex(ValueError,'after last verified'):
            run_live.order_timecheck([row],[],model,
                dt.datetime(2026,11,10,tzinfo=dt.timezone.utc),
                dt.date(2026,11,09))


if __name__=='__main__':unittest.main()
