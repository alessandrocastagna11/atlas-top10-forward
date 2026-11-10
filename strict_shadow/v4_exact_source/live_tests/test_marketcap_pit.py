"""Offline parser and disclosure tests for daily market-cap observations."""
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import marketcap_pit as m

class RankingTests(unittest.TestCase):
    def test_table_parses_star_column_ticker_and_company_cap(self):
        html=b"""<table><tr><td class="fav-td">*</td><td class="rank-td">1</td><td class="name-td"><a><div class="company-name">Apple</div><div class="company-code">AAPL</div></a></td><td class="td-right" data-sort="5096000000000">$5.096 T</td><td>USD</td><td>2.4%</td><td>USA</td></tr><tr><td class="fav-td">*</td><td class="rank-td">2</td><td class="name-td"><span class="company-code">2330.TW</span></td><td>$1.850 T</td><td>USD</td><td>1%</td><td>Taiwan</td></tr></table>"""
        r=m.parse_ranking(html.decode())
        self.assertEqual(len(r),2,r)
        self.assertEqual(r[0]["vendor_ticker"],"AAPL")
        self.assertEqual(r[1]["vendor_ticker"],"2330.TW")
        self.assertGreater(r[0]["market_cap_usd"],5e12)
    def test_exact_ticker_crosswalk_no_adr_guessing(self):
        data=[{"vendor_ticker":"AAPL","vendor_rank":1,"market_cap_usd":1e12},
              {"vendor_ticker":"TSM","vendor_rank":2,"market_cap_usd":0.9e12}]
        etf=[{"symbol":"AAPL","country":"United States"},
             {"symbol":"2330.TW","country":"Taiwan"}]
        match=m.crosswalk(data,etf)
        self.assertEqual(len(match),1)
        self.assertEqual(match[0]["symbol"],"AAPL")
    def test_marketcap_output_cannot_backdate_yesterday(self):
        self.assertIn("OBSERVED_ONLY",m.CAPTURED_TYPE)
        self.assertNotIn("PIT_CERTIFIED",m.RANKING_TYPE)
if __name__=="__main__":unittest.main()
