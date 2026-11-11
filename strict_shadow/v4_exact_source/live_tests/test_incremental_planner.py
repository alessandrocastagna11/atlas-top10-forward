"""Planner must match original incumbent-aware chooser and cash-only deltas."""
import datetime as dt
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import model_proposal as planner
import live_model as lm

class PlannerTests(unittest.TestCase):
    def test_owned_capital_is_not_counted_again_as_a_new_purchase(self):
        day=pd.Timestamp('2026-11-10')
        rows=pd.Index([day])
        close=pd.DataFrame({'KO':[100.], 'ABBV':[100.]},index=rows)
        tracked=[]
        def choose(date,score,ranks,eligible,positions,cfg):
            tracked.append(set(positions))
            return ['KO','ABBV']
        def allocate(relative,exposure,cfg,decision_date):
            return pd.Series({'KO':.12,'ABBV':.08})
        m=SimpleNamespace(
            bundle={'cfg':lm.v2.V2Config()},
            callbacks=lambda _: (choose,allocate,None,None),
            path_score=close.copy(),eligible=close.gt(0),
            membership=close.gt(0),
            pr_regime=pd.Series([.7],index=rows),
            pr_t8=pd.Series([1.],index=rows),
            features=SimpleNamespace(close_eur=close),
            raw=SimpleNamespace(close_eur=close))
        owned={'KO':lm.v2.Position(11.,1100.,pd.Timestamp('2026-11-09'))}
        meta={'ticker_to_market_symbol':{'KO':'KO','ABBV':'ABBV'}}
        with mock.patch.object(planner.v2,'estimate_risky_volatility',
                               return_value=(.15,pd.Series({'KO':.5,'ABBV':.5}),
                                             np.eye(2)*.001)):
            data=planner.proposal(m,meta,day.date(),positions=owned,nav_eur=10000.)
        self.assertEqual(tracked,[{'KO'}])
        self.assertTrue(data['selection_considers_existing_lots'])
        self.assertEqual(data['existing_position_count'],1)
        row={x['ticker']:x for x in data['possible_candidates']}
        self.assertAlmostEqual(row['KO']['indicative_allocation_eur'],1200.,places=2)
        self.assertAlmostEqual(row['KO']['existing_position_value_eur'],1100.,places=2)
        self.assertAlmostEqual(row['KO']['indicative_new_buy_eur'],100.,places=2)
        self.assertFalse(row['KO']['meets_minimum_order_threshold'])
        self.assertTrue(row['ABBV']['meets_minimum_order_threshold'])
        self.assertEqual(data['possible_buys_meeting_threshold'],1)
        # If still-locked existing shares consume the entire risk budget,
        # the planner must not invent a second allocation by spending cash.
        overweight={'KO':lm.v2.Position(18.,1800.,pd.Timestamp('2026-11-09'))}
        with mock.patch.object(planner.v2,'estimate_risky_volatility',
                               return_value=(.15,pd.Series({'KO':.5,'ABBV':.5}),
                                             np.eye(2)*.001)):
            capped=planner.proposal(m,meta,day.date(),positions=overweight,
                                    nav_eur=10000.)
        self.assertEqual(capped['possible_buys_meeting_threshold'],0)
        self.assertAlmostEqual(capped['exposure_room_before_buys_eur'],200.,places=2)
        self.assertEqual(sum(x['indicative_new_buy_eur']
                             for x in capped['possible_candidates']),0.)


if __name__=='__main__':unittest.main()
