import datetime as dt
import unittest
import json
from monitor import decode_quote, indicative_nav, fetch, preserve_confirmed
import market_sessions

UTC=dt.timezone.utc

def utc(day,hour,minute=0):
    return dt.datetime(2026,11,day,hour,minute,tzinfo=UTC)

def fixture(zone,ccy,opened,closed,mark,dates,price=100.):
    return {"meta":{"exchangeTimezoneName":zone,"currency":ccy,
                     "regularMarketPrice":price,"regularMarketTime":int(mark.timestamp()),
                     "currentTradingPeriod":{"regular":{"start":int(opened.timestamp()),"end":int(closed.timestamp())}}},
            "timestamp":[int(t.timestamp()) for t in dates],
            "indicators":{"quote":[{"open":[price]*len(dates),"close":[price]*len(dates),
                                     "volume":[1000]*len(dates)}],
                          "adjclose":[{"adjclose":[price]*len(dates)}]}}

class MonitorTests(unittest.TestCase):
    def test_tokyo_final_while_usa_still_open(self):
        observed=utc(16,19)
        japan=fixture("Asia/Tokyo","JPY",utc(16,0),utc(16,6,30),utc(16,6,30),[utc(16,0)])
        usa=fixture("America/New_York","USD",utc(16,14,30),utc(16,21),utc(16,18,55),
                    [utc(13,14,30),utc(16,14,30)])
        jp=decode_quote("7203.T",japan,observed,market_sessions)
        us=decode_quote("KO",usa,observed,market_sessions)
        self.assertEqual(jp["last_confirmed_daily_bar"]["session"],"2026-11-16")
        self.assertEqual(us["last_confirmed_daily_bar"]["session"],"2026-11-13")
        self.assertEqual(us["quote_status"],"INDICATIVE_INTRADAY")
    def test_stale_intraday_not_nav(self):
        observed=utc(16,19)
        raw=fixture("America/New_York","USD",utc(16,14,30),utc(16,21),utc(16,17),
                    [utc(13,14,30),utc(16,14,30)])
        row=decode_quote("KO",raw,observed,market_sessions)
        self.assertEqual(row["quote_status"],"STALE_DURING_REPORTED_SESSION")
        nav=indicative_nav({"cash_eur":500,"holdings":[{"ticker":"KO","shares":1}]},{"KO":row})
        self.assertIsNone(nav["nav_eur"])
    def test_gbp_normalization(self):
        raw=fixture("Europe/London","GBp",utc(16,8),utc(16,16,35),utc(16,16,35),
                    [utc(16,8)],price=1234.)
        row=decode_quote("AZN.L",raw,utc(16,19),market_sessions)
        self.assertEqual(row["currency"],"GBP")
        self.assertAlmostEqual(row["quote_price"],12.34)
        self.assertAlmostEqual(row["last_confirmed_daily_bar"]["price"],12.34)
    def test_uses_confirmed_close_for_closed_market(self):
        row={"currency":"EUR","quote_price":99999,"vendor_session_open":False,
             "quote_status":"LAST_REPORTED_QUOTE_OUTSIDE_VENDOR_SESSION",
             "daily_status":"LATEST_EXPECTED_NORMAL_CLOSE_PRESENT",
             "last_confirmed_daily_bar":{"price":10,"session":"2026-11-16"}}
        nav=indicative_nav({"cash_eur":500,"holdings":[{"ticker":"ABC","shares":2}]},{"ABC":row})
        self.assertEqual(nav["nav_eur"],520)
    def test_missing_ledger_not_cash_account(self):
        self.assertIsNone(indicative_nav(None,{})["nav_eur"])
    def test_missing_fx_not_zero_return(self):
        row={"currency":"USD","quote_price":100,"vendor_session_open":True,
             "quote_status":"INDICATIVE_INTRADAY","quote_at_utc":"2026-11-16T19:00:00+00:00"}
        nav=indicative_nav({"cash_eur":500,"holdings":[{"ticker":"KO","shares":1}]},{"KO":row})
        self.assertIsNone(nav["nav_eur"])
        self.assertEqual(nav["missing"],["KO FX"])

    def test_midnight_missing_bar_retries_other_host(self):
        observed=utc(18,1,50)
        old=fixture("America/New_York","USD",utc(18,14,30),utc(18,21),utc(17,21),
                    [utc(16,14,30)])
        complete=fixture("America/New_York","USD",utc(18,14,30),utc(18,21),utc(17,21),
                         [utc(16,14,30),utc(17,14,30)])
        calls=[]
        def request_get(url):
            calls.append(url)
            return json.dumps({"chart":{"result":[old if len(calls)==1 else complete]}}).encode()
        row=fetch("KO",market_sessions,request_get,clock=lambda:observed)
        self.assertEqual(len(calls),2)
        self.assertIn("period1=",calls[0])
        self.assertEqual(row["last_confirmed_daily_bar"]["session"],"2026-11-17")
        self.assertEqual(row["daily_status"],"LATEST_EXPECTED_NORMAL_CLOSE_PRESENT")
    def test_post_close_quote_not_certified_as_daily_bar(self):
        raw=fixture("America/New_York","USD",utc(18,14,30),utc(18,21),utc(17,21),
                    [utc(16,14,30)])
        row=decode_quote("KO",raw,utc(18,1,50),market_sessions)
        self.assertEqual(row["quote_status"],"INDICATIVE_POST_CLOSE_PENDING_EOD")
        self.assertEqual(row["daily_status"],"PENDING_CLOSE_OR_HOLIDAY_VERIFY_CALENDAR")
        self.assertEqual(row["last_confirmed_daily_bar"]["session"],"2026-11-16")
    def test_preserve_confirmed_close_not_relabelled(self):
        old={"currency":"USD","last_confirmed_daily_bar":{"session":"2026-11-17","price":100},
             "source_sha256":"older-audit","observed_at_utc":"2026-11-18T01:00:00+00:00"}
        row={"currency":"USD","last_confirmed_daily_bar":{"session":"2026-11-16","price":90},
             "expected_normal_session":"2026-11-18","daily_status":"PENDING_CLOSE_OR_HOLIDAY_VERIFY_CALENDAR"}
        result=preserve_confirmed(row,old)
        self.assertEqual(result["last_confirmed_daily_bar"]["session"],"2026-11-17")
        self.assertEqual(result["daily_bar_original_source_sha256"],"older-audit")
        self.assertEqual(result["daily_status"],"PENDING_CLOSE_OR_HOLIDAY_VERIFY_CALENDAR")
    def test_no_previous_observation(self):
        row={"currency":"USD","last_confirmed_daily_bar":None}
        self.assertEqual(preserve_confirmed(row,None),row)
