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
    def test_issuer_country_crosswalk_and_explicit_adr_alias(self):
        data=[{"vendor_ticker":"AAPL","vendor_rank":1,"market_cap_usd":1e12,
               "vendor_company_name":"AppleAAPL","vendor_country":"🇺🇸 USA"},
              {"vendor_ticker":"TSM","vendor_rank":2,"market_cap_usd":0.9e12,
               "vendor_company_name":"TSMCTSM","vendor_country":"🇹🇼 Taiwan"},
              {"vendor_ticker":"GOOG","vendor_rank":3,"market_cap_usd":0.8e12,
               "vendor_company_name":"Alphabet (Google)GOOG","vendor_country":"🇺🇸 USA"}]
        etf=[{"symbol":"AAPL","country":"United States","company_name":"Apple"},
             {"symbol":"2330.TW","country":"Taiwan","company_name":"Taiwan Semiconductor Manufacturing"},
             {"symbol":"GOOG","country":"United States","company_name":"Alphabet Class C"},
             {"symbol":"GOOGL","country":"United States","company_name":"Alphabet Class A"}]
        matches=m.crosswalk(data,etf)
        self.assertEqual(len(matches),3)
        self.assertEqual({x["symbol"] for x in matches},{"AAPL","2330.TW","GOOG"})
        self.assertIn("CURATED_ISSUER_ALIAS",next(x for x in matches if x["symbol"]=="2330.TW")["capitalization_coverage_status"])
    def test_checked_cross_domicile_same_issuer_only_and_UMC_alias(self):
        v=[
            {"vendor_ticker":"AIR.PA","vendor_rank":1,"market_cap_usd":1.8e11,
             "vendor_company_name":"AirbusAIR.PA","vendor_country":"🇳🇱 Netherlands"},
            {"vendor_ticker":"CB","vendor_rank":2,"market_cap_usd":1.4e11,
             "vendor_company_name":"ChubbCB","vendor_country":"🇨🇭 Switzerland"},
            {"vendor_ticker":"UMC","vendor_rank":3,"market_cap_usd":4e10,
             "vendor_company_name":"United MicroelectronicsUMC","vendor_country":"🇹🇼 Taiwan"},
            {"vendor_ticker":"RANDOM","vendor_rank":4,"market_cap_usd":1e10,
             "vendor_company_name":"Other CorpRANDOM","vendor_country":"🇬🇧 UK"}]
        etf=[
            {"symbol":"AIR.PA","country":"France","company_name":"AIRBUS GROUP"},
            {"symbol":"CB","country":"United States","company_name":"CHUBB"},
            {"symbol":"2303.TW","country":"Taiwan","company_name":"UNITED MICRO ELECTRONICS CORP"},
            {"symbol":"RANDOM","country":"United States","company_name":"OTHER CORP"}]
        matched=m.crosswalk(v,etf)
        self.assertEqual({x["symbol"] for x in matched},{"AIR.PA","CB","2303.TW"})
        self.assertIn("REVIEWED_EXACT_TICKER_NAME_DIFFERENT_DOMICILE",
                      next(x for x in matched if x["symbol"]=="CB")["capitalization_coverage_status"])
        self.assertIn("CURATED_ISSUER_ALIAS",
                      next(x for x in matched if x["symbol"]=="2303.TW")["capitalization_coverage_status"])
        self.assertNotIn("RANDOM",{x["symbol"] for x in matched})

    def test_marketcap_output_cannot_backdate_yesterday(self):
        self.assertIn("OBSERVED_ONLY",m.CAPTURED_TYPE)
        self.assertNotIn("PIT_CERTIFIED",m.RANKING_TYPE)
if __name__=="__main__":unittest.main()
