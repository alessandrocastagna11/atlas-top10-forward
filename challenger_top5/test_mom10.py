import unittest
import numpy as np
import pandas as pd
from run import score_mom10, same_targets

class TestRankMom10(unittest.TestCase):
    def setUp(self):
        self.index=pd.bdate_range('2023-01-02',periods=120)
        n=np.arange(120)
        self.close=pd.DataFrame({'A':100*1.003**n,'B':100*1.001**n,'C':100*1.009**n},index=self.index)
        self.base=pd.DataFrame(.5,index=self.index,columns=list(self.close))
        self.path=self.base+.08
        self.eligible=pd.DataFrame(True,index=self.index,columns=list(self.close))
        self.eligible['C']=False
    def test_exact_formula_and_eligible_percentile(self):
        s=score_mom10(self.base,self.path,self.close,self.eligible,self.eligible)
        self.assertAlmostEqual(s['A'].iloc[-1],.68)
        self.assertAlmostEqual(s['B'].iloc[-1],.58)
        self.assertTrue(pd.isna(s['C'].iloc[-1]))
        self.assertTrue(s.iloc[:84].isna().all().all())
    def test_recent_21_bars_not_used(self):
        altered=self.close.copy()
        altered.iloc[-21:,0]*=30
        a=score_mom10(self.base,self.path,self.close,self.eligible,self.eligible)
        b=score_mom10(self.base,self.path,altered,self.eligible,self.eligible)
        pd.testing.assert_series_equal(a.iloc[-1],b.iloc[-1])
    def test_future_bars_cannot_change_past(self):
        altered=self.close.copy()
        altered.iloc[105:]*=11
        a=score_mom10(self.base,self.path,self.close,self.eligible,self.eligible)
        b=score_mom10(self.base,self.path,altered,self.eligible,self.eligible)
        pd.testing.assert_frame_equal(a.iloc[:105],b.iloc[:105])
    def test_revised_target_detection(self):
        p=[{'ticker':'A','target_weight':.20},{'ticker':'B','target_weight':.18}]
        self.assertTrue(same_targets(p,p))
        self.assertFalse(same_targets(p,[{'ticker':'B','target_weight':.20},{'ticker':'A','target_weight':.18}]))
        self.assertFalse(same_targets(p,[{'ticker':'A','target_weight':.19},{'ticker':'B','target_weight':.18}]))

if __name__=='__main__':
    unittest.main()
