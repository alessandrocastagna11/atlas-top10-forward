"""Shared forward review schedule: fixed five-week phase, real US sessions.

The legacy operational anchor stays 2026-11-09. A closed Monday moves the
review to the first actual session of the SAME week. This is deliberately
not a claim of parity with the frozen W-FRI historical simulator.
"""
from __future__ import annotations
import datetime as dt
import pandas as pd
import exchange_calendars as xcals

POLICY="FIRST_ACTUAL_US_SESSION_OF_ANCHORED_WEEK_V1"
ANCHOR=dt.date(2026,11,9)

def _date(value):
    return dt.date.fromisoformat(str(value)[:10])

def review_slot(day,anchor=ANCHOR):
    """Return canonical week number, actual review day and rotation flag."""
    day=_date(day);anchor=_date(anchor)
    if anchor.weekday()!=0:raise ValueError("Review anchor must be Monday")
    week=(day-anchor).days//7
    if week<0:return None
    nominal=anchor+dt.timedelta(days=7*week)
    actual=xcals.get_calendar("XNYS").date_to_session(pd.Timestamp(nominal),direction="next").date()
    if actual>=nominal+dt.timedelta(days=7):
        return None  # No tradable session in this week.
    return {"week":week,"nominal":nominal,"actual":actual,"full":week>0 and week%5==0}

def processed_review_weeks(processed,anchor=ANCHOR):
    """A late but genuinely observed review consumes the same anchored slot."""
    anchor=_date(anchor)
    return {(_date(x)-anchor).days//7 for x in processed if _date(x)>=anchor}

def next_full_review(day,processed,anchor=ANCHOR):
    """Earliest not-yet-consumed full review (may be overdue)."""
    anchor=_date(anchor);weeks=processed_review_weeks(processed,anchor)
    for week in range(5,5200,5):
        slot=review_slot(anchor+dt.timedelta(days=7*week),anchor)
        if slot is not None and week not in weeks:
            return slot
    raise ValueError("No future full review available")
