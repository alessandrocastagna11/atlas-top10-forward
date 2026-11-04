import unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import pit_backtest as P
class PITTests(unittest.TestCase):
 def test_avoid_future_restatement(self):
  obs=[['NetIncomeLoss','USD','2015-01-01','2015-12-31','2016-02-01','doc1',100],
       ['NetIncomeLoss','USD','2015-01-01','2015-12-31','2022-03-01','doc2',999]]
  self.assertEqual(P.value_for_period(obs,'2016-04-30','2015-12-31',['NetIncomeLoss'])[0],100)
  self.assertEqual(P.value_for_period(obs,'2023-04-30','2015-12-31',['NetIncomeLoss'])[0],100)
 def test_future_only_missing(self):
  obs=[['Assets','USD','','2020-12-31','2022-02-01','doc1',100]]
  self.assertIsNone(P.value_for_period(obs,'2021-03-01','2020-12-31',['Assets']))
 def test_split_multiplier_preserves_book_value(self):
  sym='TEST';obs=[]
  for tag,val in [('NetIncomeLoss',10000000),('Assets',100000000),('StockholdersEquity',70000000),('Revenues',150000000),('NetCashProvidedByUsedInOperatingActivities',18000000),('WeightedAverageNumberOfDilutedSharesOutstanding',1000000)]:
   unit='shares' if 'Shares' in tag else 'USD'
   obs.append([tag,unit,'','2016-12-31','2017-02-01','doc1',val])
  facts={'entities':{sym:{'status':'OK','splits':[['2020-08-31',4.0]],'observations':obs}}}
  snap,reason=P.sec_snapshot(sym,'2017-04-30',facts,25)
  self.assertIsNone(reason)
  self.assertEqual(snap['split_adjustment'],4)
  self.assertAlmostEqual(snap['book_to_market'],.7)
  self.assertEqual(snap['filing_known_by'],'2017-02-01')
 def test_pre_filing_no_signal(self):
  sym='TEST';obs=[]
  for tag,val in [('NetIncomeLoss',10),('Assets',100),('StockholdersEquity',70),('Revenues',150),('NetCashProvidedByUsedInOperatingActivities',18),('WeightedAverageNumberOfDilutedSharesOutstanding',1)]:
   obs.append([tag,'shares' if 'Shares' in tag else 'USD','','2016-12-31','2017-03-01','doc1',val])
  facts={'entities':{sym:{'status':'OK','splits':[],'observations':obs}}}
  snap,why=P.sec_snapshot(sym,'2017-02-28',facts,25)
  self.assertIsNone(snap)
 def test_tax_losses(self):
  t=P.TaxLoss();self.assertEqual(t.tax(-100,2021),0)
  self.assertEqual(t.tax(60,2023),0)
  self.assertAlmostEqual(t.tax(60,2024),20*.26)
 def test_expire_losses(self):
  t=P.TaxLoss();t.tax(-100,2015);self.assertEqual(t.tax(20,2020),20*.26)
 def test_buy_sell_conservation(self):
  a=P.Account('test');a.buy('2016-02','AAA',100,1000,'test');self.assertEqual(len(a.orders),1)
  self.assertGreater(a.cash,8999)
  a.sell('2016-03','AAA',100,'test');self.assertEqual(len(a.orders),2);self.assertEqual(len(a.pos),0)
 def test_no_forward_look_momentum(self):
  p={str(P.pd.Period('2019-01','M')+i):[str(P.pd.Period('2019-01','M')+i),None,None,None,None,100+i,None,None] for i in range(26)}
  base=P.momentum(p,'2020-03');p['2020-03'][5]=100000000
  self.assertEqual(P.momentum(p,'2020-03'),base)
 def test_rank_sector_limit(self):
  import pandas as pd
  f=pd.DataFrame({'symbol':list('ABCDEFG'),'sector':['tech']*5+['consumer']*2,'qv_score':[7,6,5,4,3,2,1]})
  chosen=P.select(f,'qv_score',n=6,max_per_sector=3)
  self.assertEqual(chosen,list('ABCFG'))
if __name__=='__main__':unittest.main()