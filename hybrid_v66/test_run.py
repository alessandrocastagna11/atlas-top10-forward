import datetime as dt
import unittest
import exchange_calendars as xcals
from run import validate_execution_quotes

UTC=dt.timezone.utc

def calendar(symbol):
    return xcals.get_calendar("XTKS" if symbol.endswith(".T") else "XNYS"), True

def moment(day,hour):
    return dt.datetime(2026,11,day,hour,tzinfo=UTC)

class TestHybridExecutionCalendar(unittest.TestCase):
    def setUp(self):
        self.quote={"decision_session":"2026-11-25","close_eur":{"KO":89.,"G::7203.T":20.},
                    "ticker_price_dates":{"KO":"2026-11-25","G::7203.T":"2026-11-25"}}
        self.recorded=moment(24,7)

    def test_holiday_preserves_pending(self):
        result=validate_execution_quotes({"KO"},self.quote,"2026-11-23",
                self.recorded,moment(26,16),calendar)
        self.assertEqual(result["KO"],"LOCAL_MARKET_NOT_OPENED_ON_ORDER_DATE")

    def test_black_friday_not_before_open(self):
        early=validate_execution_quotes({"KO"},self.quote,"2026-11-23",
                self.recorded,moment(27,13),calendar)
        self.assertEqual(early["KO"],"LOCAL_MARKET_NOT_OPENED_ON_ORDER_DATE")
        self.assertEqual(validate_execution_quotes({"KO","G::7203.T"},self.quote,
                "2026-11-23",self.recorded,moment(27,16),calendar),{})

    def test_missing_and_stale_local_close(self):
        q={**self.quote,"ticker_price_dates":{"KO":"2026-11-24"}}
        result=validate_execution_quotes({"KO","CB"},q,"2026-11-23",
                self.recorded,moment(27,16),calendar)
        self.assertEqual(result["CB"],"MISSING_OBSERVED_LOCAL_CLOSE")
        self.assertEqual(result["KO"],"STALE_OR_FUTURE_LOCAL_CLOSE_IN_SHARED_SNAPSHOT")

    def test_close_cannot_predate_publication(self):
        result=validate_execution_quotes({"KO"},self.quote,"2026-11-23",
                moment(26,5),moment(27,16),calendar)
        self.assertEqual(result["KO"],"NO_COMPLETED_POST_PUBLICATION_LOCAL_CLOSE")

if __name__=="__main__":
    unittest.main()
