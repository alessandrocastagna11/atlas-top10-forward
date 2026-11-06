"""Synthetic offline forward-proxy tests. They never touch committed ledgers."""
from __future__ import annotations
import datetime as dt,io,json,math,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import forward_signal as fw
import proxy_paper as pp

class ForwardTests(unittest.TestCase):
    def test_cross_sectional_z_is_bounded(self):
        z=fw.robust_z({'A':1.,'B':2.,'C':3.,'D':1e9})
        self.assertEqual(z['D'],3.)
        self.assertTrue(all(math.isfinite(x) for x in z.values()))
    def test_forward_weights_hard_caps(self):
        names=[{'ticker':'T'+str(i),'score':float(10-i),'sector':'TECH' if i<4 else 'HEALTH' if i<7 else 'FIN','vol63':.20+i*.01}
               for i in range(10)]
        out=fw.weighted_allocation(names,.8)
        self.assertLessEqual(sum(out.values()),.80+1e-6)
        self.assertLessEqual(max(out.values()),.15+1e-6)
        for sector in {'TECH','HEALTH','FIN'}:
            self.assertLessEqual(sum(out.get(f['ticker'],0) for f in names if f['sector']==sector),.30+1e-6)
    def test_real_next_business_day(self):
        self.assertEqual(str(fw.next_weekday(dt.date(2026,11,6))),'2026-11-09')
        self.assertEqual(str(fw.next_weekday(dt.date(2026,11,5))),'2026-11-06')
    def test_signal_execution_open_is_not_backdated(self):
        d=dt.date(2026,11,5)
        self.assertEqual(str(fw.executable_session(d,dt.datetime(2026,11,6,13,45,tzinfo=dt.timezone.utc))),'2026-11-06')
        self.assertEqual(str(fw.executable_session(d,dt.datetime(2026,11,6,16,35,tzinfo=dt.timezone.utc))),'2026-11-09')
        # Friday morning in New York is already after the Friday Tokyo open.
        self.assertEqual(str(fw.executable_session(d,dt.datetime(2026,11,6,13,0,tzinfo=dt.timezone.utc),['6098.T'])),'2026-11-09')
        # Thursday after US close, but still before Friday's Tokyo open.
        self.assertEqual(str(fw.executable_session(d,dt.datetime(2026,11,5,23,0,tzinfo=dt.timezone.utc),['6098.T'])),'2026-11-06')
        # The same signal would be too late for Friday's Sydney opening.
        self.assertEqual(str(fw.executable_session(d,dt.datetime(2026,11,5,23,0,tzinfo=dt.timezone.utc),['BHP.AX'])),'2026-11-09')
    def test_empty_data_blocks_research(self):
        old_forward,old_data=fw.FORWARD,fw.DATA
        try:
            with tempfile.TemporaryDirectory() as tmp:
                fw.FORWARD=Path(tmp)/'out';fw.DATA=Path(tmp)/'dataset'
                out=fw.generate(dt.datetime(2026,11,6,1,tzinfo=dt.timezone.utc))
                self.assertEqual(out['status'],'PENDING_FORWARD_DATA')
                self.assertFalse((fw.FORWARD/'pending_signal.json').exists())
        finally:fw.FORWARD,fw.DATA=old_forward,old_data
    def test_universe_is_never_retroactively_read(self):
        old=fw.DATA
        try:
            with tempfile.TemporaryDirectory() as tmp:
                fw.DATA=Path(tmp)
                folder=fw.DATA/'universe/snapshots/2026-11-07'
                folder.mkdir(parents=True)
                (folder/'source.json').write_text(json.dumps({'source_as_of':'Nov 06, 2026','type':'ETF_HOLDING_VALUE_PROXY_NOT_MARKET_CAP'}))
                (folder/'global500.csv').write_text('symbol\n'+'\n'.join('X'+str(i) for i in range(500))+'\n')
                with self.assertRaisesRegex(ValueError,'No 500-member roster'):
                    fw.latest_roster(dt.date(2026,11,5))
        finally:fw.DATA=old
    def test_decision_uses_no_quote_after_asof(self):
        old=fw.DATA
        try:
            with tempfile.TemporaryDirectory() as tmp:
                fw.DATA=Path(tmp)
                target=fw.DATA/'prices'/fw.symbol_filename('AAPL')
                target.parent.mkdir(parents=True)
                target.write_text('date,symbol,open,close,volume,currency,unit_scale,source_sha256\n'
                                  '2026-11-05,AAPL,200,210,10000,USD,1,x\n'
                                  '2026-11-06,AAPL,150,155,10000,USD,1,y\n')
                x,_=fw.prices('AAPL',dt.date(2026,11,5))
                self.assertEqual(len(x),1)
                self.assertEqual(x[-1][1],210)
        finally:fw.DATA=old
    def test_proxy_paper_no_signal_means_no_orders(self):
        old_forward,old_state=pp.FORWARD,pp.STATE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pp.FORWARD=Path(tmp);pp.STATE=Path(tmp)/'state.json'
                x=pp.execute(dt.datetime(2026,11,5,23,tzinfo=dt.timezone.utc))
                self.assertEqual(x['status'],'PENDING_SIGNAL')
                self.assertEqual(x['orders_count'],0)
                self.assertEqual(x['valuation']['nav_eur'],10000.)
        finally:pp.FORWARD,pp.STATE=old_forward,old_state

if __name__=='__main__':unittest.main()
