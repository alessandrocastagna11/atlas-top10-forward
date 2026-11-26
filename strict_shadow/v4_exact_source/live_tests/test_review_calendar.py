"""Review schedule must keep its original phase across holidays."""
import datetime as dt
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from review_calendar import review_slot,next_full_review

class TestReviewCalendar(unittest.TestCase):
    def test_regular_rotation_and_phase(self):
        slot=review_slot("2026-12-14")
        self.assertEqual(slot["week"],5)
        self.assertTrue(slot["full"])
        self.assertEqual(str(slot["actual"]),"2026-12-14")

    def test_mlk_holiday_on_full_rotation(self):
        slot=review_slot("2027-01-18")
        self.assertEqual(slot["week"],10)
        self.assertTrue(slot["full"])
        self.assertEqual(str(slot["actual"]),"2027-01-19")
        self.assertEqual(review_slot("2027-01-19"),slot)
        self.assertEqual(review_slot("2027-01-20"),slot)
        self.assertFalse(review_slot("2027-01-25")["full"])

    def test_holiday_does_not_shift_future_rotation_phase(self):
        self.assertEqual(str(review_slot("2027-02-22")["actual"]),"2027-02-22")
        self.assertTrue(review_slot("2027-02-22")["full"])

    def test_next_review_is_actual_session_and_no_duplicate(self):
        slot=next_full_review("2026-12-15",["2026-12-14"])
        self.assertEqual(str(slot["actual"]),"2027-01-19")
        slot=next_full_review("2027-01-20",["2026-12-14","2027-01-19"])
        self.assertEqual(str(slot["actual"]),"2027-02-22")
        self.assertEqual(str(next_full_review("2027-01-20",[])["actual"]),"2026-12-14")

if __name__=="__main__":unittest.main()
