"""Offline tests for the 6 November 2026 precommitted PAPER market-on-close launch.

These tests never use real prices, broker accounts or persistent forward ledgers.
"""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import proxy_paper as proxy
from forward_signal import VERSION
from paper import ENGINE
from provider import Bar


class FakeProvider:
    def bar(self, symbol, session):
        return Bar(session, 100.0, 101.0, 'USD', symbol, 'simulated-only')
    def eur_quote(self, symbol, session, field):
        return {'price_eur': 100.0 if symbol == 'AAPL' else 200.0,
                'session': str(session), 'symbol': symbol, 'field': field,
                'market_sha256': 'synthetic-verified'}
    def history(self, symbol, start, end):
        d = dt.date(2026, 11, 6)
        return {d: self.bar(symbol, d)}


def signal(*, locked_at='2026-11-06T17:21:20+00:00', market_symbol='AAPL', region='USA'):
    return {'schema_version': 'research-forward-1', 'engine_id': ENGINE,
            'model_variant': VERSION, 'historical_engine_equivalent': False,
            'mode': 'SHADOW', 'decision_session': '2026-11-05',
            'execution_session': '2026-11-06',
            'execution_policy': 'US_MOC_LOCKED_PRE_CLOSE',
            'produced_at_utc': '2026-11-06T16:41:47+00:00',
            'locked_at_utc': locked_at,
            'parent_signal_blob_sha': '3fe591dfa8a6cfc173a36547d4ee0bd5b50b3fbe',
            'authorization': {'real_money_authorized': False},
            'universe_source': {'recorded_on': '2026-11-05', 'asof': '2026-11-03'},
            'targets': [{'ticker': market_symbol, 'market_symbol': market_symbol,
                         'region': region, 'sector': 'Information Technology',
                         'weight': 0.10, 'price_eur': 100.0}]}


class LaunchMOCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_forward, self.old_state = proxy.FORWARD, proxy.STATE
        self.addCleanup(self.restore)
        proxy.FORWARD = Path(self.tmp.name)
        proxy.STATE = proxy.FORWARD / 'proxy_paper_state.json'

    def restore(self):
        proxy.FORWARD, proxy.STATE = self.old_forward, self.old_state

    def write(self, value):
        (proxy.FORWARD / 'pending_signal.json').write_text(json.dumps(value))

    def test_moc_is_never_filled_before_verified_close_and_never_duplicated(self):
        self.write(signal())
        early = proxy.execute(dt.datetime(2026,11,6,19,0,tzinfo=dt.timezone.utc),FakeProvider())
        self.assertEqual(early['status'], 'PENDING_EXECUTION_QUOTES')
        self.assertEqual(early['orders_count'], 0)
        after = proxy.execute(dt.datetime(2026,11,7,0,15,tzinfo=dt.timezone.utc),FakeProvider())
        self.assertEqual(after['status'], 'MOC_PAPER_EXECUTED')
        self.assertEqual(after['orders_count'], 1)
        self.assertEqual(after['execution_price_kind'], 'close')
        self.assertEqual(after['execution']['orders'][0]['reference_close_eur'], 100.0)
        self.assertNotIn('reference_open_eur', after['execution']['orders'][0])
        self.assertFalse((proxy.FORWARD / 'pending_signal.json').exists())
        again = proxy.execute(dt.datetime(2026,11,7,0,16,tzinfo=dt.timezone.utc),FakeProvider())
        self.assertEqual(again['orders_count'], 1)
        self.assertEqual(again['real_money_orders'], 0)

    def test_late_locked_intent_is_blocked(self):
        self.write(signal(locked_at='2026-11-06T22:20:00+00:00'))
        res = proxy.execute(dt.datetime(2026,11,7,0,15,tzinfo=dt.timezone.utc),FakeProvider())
        self.assertEqual(res['status'], 'BLOCKED_PROXY_EXECUTION')
        self.assertEqual(res['orders_count'], 0)

    def test_non_us_session_is_not_backdated(self):
        self.write(signal(market_symbol='6098.T',region='ROW'))
        res = proxy.execute(dt.datetime(2026,11,7,0,15,tzinfo=dt.timezone.utc),FakeProvider())
        self.assertEqual(res['status'], 'BLOCKED_PROXY_EXECUTION')
        self.assertEqual(res['orders_count'], 0)


if __name__ == '__main__':
    unittest.main()
