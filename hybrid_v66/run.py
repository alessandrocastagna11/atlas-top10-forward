"""V6.6 Hybrid: shared-input paper challenger, no broker API.
Bootstrap is explicitly retrospective; subsequent decisions are frozen and
executed prospectively using the same approximate EOD adapter as Confidence70.
"""
from __future__ import annotations
import argparse, copy, datetime as dt, json, math, sys, traceback
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parent
OUT=ROOT/"ledger"
FIRST=pd.Timestamp("2026-11-09")
START=pd.Timestamp("2026-11-10")
ENGINE="ATLAS_V66_HYBRID_MULTIFACTOR"
POLICY="HYBRID_SHARED_CONFIDENCE70_EOD_PROXY"
WEIGHTS={"bull":(.0875,.0625,.05,.05),"lateral":(.075,.10,.025,.05),"bear":(.125,.075,.025,.025)}
UTC=dt.timezone.utc

def hybrid_score(base,close,membership,sectors,regime,overlay,zscore):
    def z(x):return zscore(x.where(membership)).fillna(0.)
    lp=np.log(close.where(close>0)); ret=lp.diff()
    down=z(-ret.clip(upper=0).pow(2).rolling(63,min_periods=42).mean().pow(.5))
    rev=z(-(lp-lp.shift(21)))
    near=z(lp-lp.rolling(252,min_periods=160).max())
    sec=pd.DataFrame(0.,index=base.index,columns=base.columns)
    for sector in set(sectors.values()):
        names=[t for t in base.columns if sectors[t]==sector]
        sec[names]=zscore(base[names].where(membership[names])).fillna(0.)
    state=(regime*overlay).clip(0,1)
    reg=pd.Series(np.where(state>=.75,"bull",np.where(state<=.25,"bear","lateral")),index=state.index)
    w=pd.DataFrame([WEIGHTS[x] for x in reg],index=state.index,columns=["down","rev","near","sec"])
    assert np.allclose(w.sum(axis=1),.25)
    score=(.75*base+down.mul(w.down,axis=0)+rev.mul(w.rev,axis=0)+near.mul(w.near,axis=0)+sec.mul(w.sec,axis=0)).clip(-3,3)
    return score,reg

def load(p,default=None):
    return json.loads(p.read_text()) if p.exists() else default

def save(p,obj):
    p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix(".tmp");tmp.write_text(json.dumps(obj,indent=2,allow_nan=False)+"\n");tmp.replace(p)

def initial(now):
    return dict(schema_version=1,engine_id=ENGINE,execution_adapter=POLICY,
        initial_cash_eur=10000.,cash_eur=10000.,holdings={},orders=[],last_nav_eur=10000.,
        fees_eur_total=0.,estimated_slippage_eur=0.,tax_eur_total=0.,blocked_sell_requests=0,
        equity_points=[],processed_decision_dates=[],start_recorded_at_utc=now.isoformat(),
        real_money_authorized=False,comparison_start="2026-11-10",pending=None)

def positions(state,v2):
    return {t:v2.Position(float(p["shares"]),float(p["cost_basis_eur"]),
            pd.Timestamp(min(l["entry_date"] for l in p["lots"]))) for t,p in state["holdings"].items()}

def day_quotes(m,frames,date):
    row=m.raw.close_eur.loc[date]
    quotes={};dates={}
    for t,p in row.items():
        if not np.isfinite(p) or p<=0:continue
        symbol=t.removeprefix("G::")
        if symbol not in frames:continue
        known=frames[symbol].loc[:date,"raw_close"].dropna()
        if known.empty:continue
        quotes[t]=round(float(p),8);dates[t]=str(known.index[-1].date())
    return {"decision_session":str(date.date()),"close_eur":quotes,"ticker_price_dates":dates}

def make_proposal(m,meta,date,state,quote,pa,lm,planner,reg):
    nav=state["cash_eur"]
    for t,p in state["holdings"].items():
        nav+=p["shares"]*pa.next_available_price(t,quote,{},state["holdings"],date.date())[0]
    # Original planner refuses missing current held marks. Only the valuation
    # fallback is permitted here; this is NOT a tradable price.
    for t,p in state["holdings"].items():
        if t not in quote["close_eur"]:
            return None
    p=planner(m,meta,date,positions=positions(state,lm.v2),nav_eur=nav)
    p["source_original_engine"]=False
    p["engine_source"]="V6.6 Hybrid score + original RegionSlotAtlas callbacks"
    p["regime"]=str(reg.loc[date])
    return p

def validate_execution_quotes(tickers,quote,decision,recorded,now,calendar_for):
    """Check all required quotes before any ledger mutation.
    The actual order remains a disclosed previous-close PAPER proxy.
    A later observed completed close must postdate the frozen decision and
    its publication, and the runner must be on a real local trading session.
    """
    blocked={}
    for ticker in sorted(set(tickers)):
        symbol=ticker.removeprefix('G::')
        cal,verified=calendar_for(symbol)
        price_day=quote.get("ticker_price_dates",{}).get(ticker)
        if not verified:
            blocked[ticker]="UNVERIFIED_LISTING_CALENDAR"
            continue
        if ticker not in quote.get("close_eur",{}) or not price_day:
            blocked[ticker]="MISSING_OBSERVED_LOCAL_CLOSE"
            continue
        if not np.isfinite(quote["close_eur"][ticker]) or quote["close_eur"][ticker]<=0:
            blocked[ticker]="INVALID_LOCAL_CLOSE"
            continue
        day=pd.Timestamp(price_day)
        if not cal.is_session(day):
            blocked[ticker]="QUOTE_ON_NON_TRADING_SESSION"
            continue
        close=cal.session_close(day).to_pydatetime()
        if price_day<=decision or close<=recorded or close>now:
            blocked[ticker]="NO_COMPLETED_POST_PUBLICATION_LOCAL_CLOSE"
            continue
        # The append-only adapter records the UTC runner date as order date.
        # Do not record a simulated order on a date with no local session.
        runner=pd.Timestamp(now.date())
        if not cal.is_session(runner) or now<cal.session_open(runner).to_pydatetime():
            blocked[ticker]="LOCAL_MARKET_NOT_OPENED_ON_ORDER_DATE"
            continue
        # The shared snapshot has a common benchmark date. Use the actual
        # latest local session at or before that date, not today's unrelated
        # Asian close; otherwise mixed-zone rotations could never complete.
        snapshot=pd.Timestamp(quote["decision_session"])
        expected=cal.date_to_session(snapshot,direction='previous')
        if day!=expected:
            blocked[ticker]="STALE_OR_FUTURE_LOCAL_CLOSE_IN_SHARED_SNAPSHOT"
    return blocked

def execute(state,p,quote,now,pa,kind):
    if not p:return False
    decision=p["decision_session"]
    if decision>=now.date().isoformat():return False
    candidates=p["possible_candidates"]
    if kind=="initial":
        for x in candidates:
            t=x["ticker"]
            if not x["meets_minimum_order_threshold"] or t not in quote["close_eur"]:continue
            px,d,q=pa.next_available_price(t,quote,p,state["holdings"],now.date())
            if not q.startswith("STALE"):
                pa.buy(state,t,x["indicative_allocation_eur"],px,decision,now,d,q)
        return True
    if kind=="full":
        if not candidates and float(p["regime_multiplier"])>0:
            raise ValueError("Full rotation missing candidates")
        return pa.execute_or_defer_five_week_rebalance(state,candidates,decision,quote,now)["completed"]
    nav=state["cash_eur"];priced={}
    for t,h in state["holdings"].items():
        priced[t]=pa.next_available_price(t,quote,p,state["holdings"],now.date())
        nav+=h["shares"]*priced[t][0]
    invested=nav-state["cash_eur"];desired=float(p["regime_multiplier"])
    if invested/max(nav,1.)>desired+.25:
        reduction=invested-desired*nav
        for t,h in list(state["holdings"].items()):
            if reduction<=0:break
            px,d,q=priced[t];old=h["shares"]
            pa.sell(state,t,min(reduction,old*px),px,decision,now,d,q,"weekly_risk_reduction_35day_guarded")
            reduction-=max(0.,old-state["holdings"].get(t,{}).get("shares",0.))*px
    return True

def run(source):
    now=dt.datetime.now(UTC)
    forward=source/"forward"
    status=load(forward/"latest_status.json",{})
    desc=status.get("shared_input")
    if status.get("shared_input_status")!="PUBLISHED_LOSSLESS" or not desc:
        raise ValueError("Confidence70 shared input not published")
    asof=pd.Timestamp(desc["decision_session"])
    if status.get("decision_session")!=desc["decision_session"]:raise ValueError("Shared decision mismatch")
    if status.get("signal_engine_status")!="SIGNAL_READY":raise ValueError("Confidence70 source signal not ready")
    source_data_audit={k:status.get(k) for k in ("local_eod_quote_audit","fx_freshness_audit","cash_rate_audit","multisource_audit","fx_recovery_audit","universe_transition_audit")}
    for key in ("local_eod_quote_audit","fx_freshness_audit"):
        item=source_data_audit.get(key) or {}
        if item.get("status")=="BLOCKED" or item.get("stale_or_missing") or item.get("blocked"):
            raise ValueError("Source data freshness failed: "+key)
    if source_data_audit.get("multisource_audit") and source_data_audit["multisource_audit"].get("decision_session")!=desc["decision_session"]:
        raise ValueError("Source multisource audit session mismatch")
    sys.path.insert(0,str(source))
    import live_model as lm
    import paper_account as pa
    from shared_inputs import load as load_inputs
    from model_proposal import proposal as planner
    from run_live import listing_calendar
    from review_calendar import review_slot,next_full_review,processed_review_weeks,POLICY as REVIEW_POLICY
    pa.ENGINE=ENGINE;pa.POLICY=POLICY
    vintages,frames,cash=load_inputs(forward,desc,desc["decision_session"],desc["roster_sha256"])
    m,meta=lm.build_model(vintages,asof.date(),frames,shared_cash_rate=cash)
    if asof.date()>=dt.date(2026,11,30):
        migration=status.get("universe_transition_audit") or {}
        actual={str(r["symbol"]).upper() for r in vintages[-1][3]+vintages[-1][4]}
        if (migration.get("status")!="EXPANDED_ACTIVE" or migration.get("decision_session")!=str(asof.date())
            or migration.get("active_roster_sha256")!=desc["roster_sha256"]
            or actual!=set(migration.get("active_symbols",[])) or len(actual)!=300):
            raise ValueError("Expanded shared universe audit mismatch")
    # Keep original alpha calibration; only replace ranking score.
    m.path_score,reg=hybrid_score(m.path_score,m.features.close_eur,m.membership,
        {t:lm.v1.TICKER_TO_SECTOR[t] for t in m.path_score.columns},m.pr_regime,m.pr_t8,lm.v1.robust_cross_sectional_z)
    state=load(OUT/"state.json");bootstrap=state is None
    if bootstrap:
        state=initial(now)
        if FIRST not in m.path_score.index or not m.membership.loc[FIRST].any():
            raise ValueError("Cannot align: missing November 9 observed membership")
        state["bootstrap_through"]=str(asof.date())
        q=day_quotes(m,frames,FIRST)
        p=make_proposal(m,meta,FIRST,state,q,pa,lm,planner,reg)
        if p is None:raise ValueError("Initial proposal unavailable")
        simulated=dt.datetime.combine(START.date(),dt.time(22),UTC)
        execute(state,p,q,simulated,pa,"initial")
        state["processed_decision_dates"].append(str(FIRST.date()))
        for o in state["orders"]:
            o["strategy"]=ENGINE;o["phase"]="RETROSPECTIVE_RECONSTRUCTION"
            o["simulation_recorded_at_utc"]=now.isoformat()
        sessions=m.path_score.index[(m.path_score.index>=START)&(m.path_score.index<=asof)]
    else:
        pa.validate_state(state)
        if state["engine_id"]!=ENGINE:raise ValueError("Wrong ledger")
        if asof<pd.Timestamp(state["last_valuation_session"]):raise ValueError("Source session regressed")
        # Never recreate missed prospective signals. Reuse only frozen proposals.
        sessions=[asof] if asof>=pd.Timestamp(state["last_valuation_session"]) else []
    previous=copy.deepcopy(state["orders"])
    for date in sessions:
        date=pd.Timestamp(date)
        ds=str(date.date())
        q=day_quotes(m,frames,date)
        # The current actual source quotes carry their precise FX provenance.
        if date==asof:
            official_q=load(forward/"latest_quotes.json",{})
            if official_q.get("decision_session")!=ds:raise ValueError("Confidence70 quote session mismatch")
            q=official_q
        clock=dt.datetime.combine(date.date()+dt.timedelta(days=1),dt.time(6),UTC) if bootstrap else now
        pending=state.get("pending")
        if pending and pending["proposal"]["decision_session"]<ds:
            before=len(state["orders"])
            blocked={}
            if not bootstrap:
                recorded=dt.datetime.fromisoformat(pending["recorded_at_utc"])
                if recorded.tzinfo is None:raise ValueError("Pending signal timestamp is not timezone aware")
                required=set(state["holdings"])
                if pending["kind"]=="full":
                    required.update(x["ticker"] for x in pending["proposal"]["possible_candidates"] if float(x["weight_pct"])>0)
                blocked=validate_execution_quotes(required,q,pending["proposal"]["decision_session"],recorded,clock,listing_calendar)
            if blocked:
                pending["last_execution_check"]={"checked_at_utc":now.isoformat(),"blocked_tickers":blocked}
                done=False
            else:
                pending.pop("last_execution_check",None)
                # Full rotation is attempted on a copy: no partial mutation if
                # any validation or execution step subsequently fails.
                trial=copy.deepcopy(state)
                done=execute(trial,pending["proposal"],q,clock,pa,pending["kind"])
                if done:
                    pa.validate_state(trial)
                    state=trial
                else:
                    pending["last_execution_check"]={"checked_at_utc":now.isoformat(),"reason":"ROTATION_DEFERRED_BY_FIFO_OR_EXECUTION_GUARD"}
            for o in state["orders"][before:]:
                o["strategy"]=ENGINE;o["phase"]="RETROSPECTIVE_RECONSTRUCTION" if bootstrap else "FORWARD_PAPER"
                o["simulation_recorded_at_utc"]=now.isoformat()
            if done:
                state["processed_decision_dates"].append(pending["proposal"]["decision_session"])
                state["pending"]=None
        pa.validate_state(state)
        port=pa.generate_portfolio(state,q,{},clock)
        state["last_nav_eur"]=port["nav_mark_to_market_eur"]
        state["last_valuation_session"]=ds
        # Use actual observation timestamp, not a fictional past recording timestamp.
        pa.reconcile_daily_nav(state,port,q,now)
        p=make_proposal(m,meta,date,state,q,pa,lm,planner,reg)
        if p is None:
            state["last_proposal_status"]="BLOCKED_MISSING_HELD_MARK"
        else:
            state["last_proposal_status"]="READY"
            state["last_proposal"]=p
            slot=review_slot(date.date(),FIRST.date())
            if (slot is not None and slot["week"]>=1 and date.date()>=slot["actual"]
                    and slot["week"] not in processed_review_weeks(state["processed_decision_dates"],FIRST.date())):
                # The actual first US session replaces a closed Monday.
                # Preserve the frozen pending decision on same-day reruns.
                if state["pending"] is None:
                    state["pending"]={"kind":"full" if slot["full"] else "weekly","proposal":p,
                                      "recorded_at_utc":now.isoformat(),"retrospective":bootstrap,
                                      "nominal_review_date":slot["nominal"].isoformat(),"review_week":slot["week"]}
                elif review_slot(state["pending"]["proposal"]["decision_session"],FIRST.date())["week"]!=slot["week"]:
                    # Explicitly record the blocked review; do not silently
                    # replace a previously committed target or backdate a fill.
                    audit=state.setdefault("blocked_review_slots",{})
                    audit.setdefault(slot["nominal"].isoformat(),{"nominal_review_date":slot["nominal"].isoformat(),
                        "full":slot["full"],"reason":"EARLIER_PENDING_DECISION",
                        "pending_decision":state["pending"]["proposal"]["decision_session"],
                        "observed_at_utc":now.isoformat()})
    if state["orders"][:len(previous)]!=previous:raise ValueError("Order prefix modified")
    pa.validate_state(state)
    port=pa.generate_portfolio(state,q,state.get("last_proposal",{}),now)
    port["variant"]=ENGINE
    port["comparison_start"]="2026-11-10"
    port["bootstrap_through"]=state["bootstrap_through"]
    port["forward_started_at_utc"]=state["start_recorded_at_utc"]
    port["retrospective_orders"]=sum(o.get("phase")=="RETROSPECTIVE_RECONSTRUCTION" for o in state["orders"])
    port["forward_orders"]=sum(o.get("phase")=="FORWARD_PAPER" for o in state["orders"])
    # Use the identical valuation session, never compare differently dated NAVs.
    orig=load(forward/"state.json",{})
    common={x["source_close_session"]:x for x in orig.get("equity_points",[])}
    pairs=[]
    for x in state["equity_points"]:
        d=x["source_close_session"]
        if d not in common:continue
        a=float(common[d]["net_liquidation_nav_eur"]);b=float(x["net_liquidation_nav_eur"])
        pairs.append({"session":d,"confidence70_net_eur":a,"hybrid_net_eur":b,
                      "difference_eur":b-a,"difference_return_pp":(b-a)/100.,
                      "hybrid_phase":"RETROSPECTIVE_RECONSTRUCTION" if d<=state["bootstrap_through"] else "FORWARD_PAPER"})
    next_slot=next_full_review(asof.date(),state["processed_decision_dates"],FIRST.date())
    result={"status":"PAPER_APPROX_LEDGER_ACTIVE","engine":ENGINE,"session":str(asof.date()),
        "review_calendar_policy":REVIEW_POLICY,
        "next_full_rotation_nominal_date":next_slot["nominal"].isoformat(),
        "next_full_rotation_decision_date":next_slot["actual"].isoformat(),
        "blocked_review_slots":state.get("blocked_review_slots",{}),
        "recorded_at_utc":now.isoformat(),"shared_input":desc,"portfolio":port,
        "source_data_audit":source_data_audit,
        "comparison_latest":pairs[-1] if pairs else None,"comparison_sessions":pairs,
        "new_orders":state["orders"] if bootstrap else state["orders"][len(previous):],
        "bootstrap":bootstrap,"pending":state.get("pending"),"proposal":state.get("last_proposal"),
        "proposal_status":state.get("last_proposal_status"),
        "limitations":["Prospective rotations require verified post-publication local closes and a real runner-date market opening; blocked rotations remain pending.",
        "Retrospective bootstrap uses later-ingested historical bars and observed dated rosters, not contemporaneously recorded Hybrid signals.",
        "Shared universe not certified historical PIT; previous completed close proxy, no broker fills.",
        "Same forward ledger adapter as Confidence70, no SPY sleeve, cash interest or dividend cash flows; not exact historical simulator parity."]}
    save(OUT/"state.json",state);save(OUT/"portfolio.json",port);save(OUT/"latest.json",result)
    print(json.dumps({"status":result["status"],"session":result["session"],"nav":port["nav_net_liquidation_eur"],"orders":len(state["orders"]),"comparison":result["comparison_latest"]}))

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--source",type=Path,required=True)
    args=parser.parse_args()
    try:run(args.source.resolve())
    except Exception as exc:
        save(OUT/"failure.json",{"status":"BLOCKED","recorded_at_utc":dt.datetime.now(UTC).isoformat(),"reason":str(exc),"traceback":traceback.format_exc()[-5000:]})
        raise
    else:
        (OUT/"failure.json").unlink(missing_ok=True)
