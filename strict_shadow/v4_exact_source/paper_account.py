#!/usr/bin/env python3
"""Atlas V4 Confidence70 frozen-signal PAPER account, operational proxy execution.

Never calls broker APIs. Every order is an ASSUMED historical-close PAPER fill,
not a market execution. Signals and proposed weights are produced by the
archived RegionSlotAtlas original engine; only the execution/ledger adapter is
a deliberate approximation. No order is backdated. Append-only accounting,
35 calendar days FIFO lock even for defensive sells, idempotent daily updates.
"""
from __future__ import annotations
import csv,datetime as dt,json,math
from pathlib import Path
from zoneinfo import ZoneInfo

from live_model import HERE,ENGINE,INITIAL
from paper_reporting import ORDER_FIELDS,write_report

UTC=dt.timezone.utc
NY=ZoneInfo("America/New_York")
ROOT=HERE/"forward"
STATUS=ROOT/"latest_status.json"
STATE=ROOT/"state.json"
PORTFOLIO=ROOT/"portfolio.json"
PROPOSAL=ROOT/"proposal_last_close.json"
PREOPEN=ROOT/"preopen"
QUOTES=ROOT/"latest_quotes.json"
ORDER_PATH=ROOT/"orders.csv"
EQUITY=ROOT/"equity.csv"
POLICY="FROZEN_ORIGINAL_SIGNAL_PREVIOUS_COMPLETED_CLOSE_PROXY"
START=dt.date(2026,11,10)
FIRST_DECISION="2026-11-09"
FEE=1.0
SLIPPAGE_BPS=10.0
MIN_HOLD_DAYS=35
TAX_RATE=0.26
RESERVE=100.0


def load(path,default=None):
    return json.loads(path.read_text(encoding="utf8")) if path.is_file() else default


def save(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+".tmp")
    temp.write_text(json.dumps(obj,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+"\n",encoding="utf8")
    temp.replace(path)


def positive(value,name):
    p=float(value)
    if not math.isfinite(p) or p<=0:raise ValueError("Invalid "+name)
    return p


def next_available_price(ticker,quotes,proposal,positions,now_date):
    """Verified last completed close; if absent use stored entry quote with stale flag."""
    if ticker in quotes.get("close_eur",{}):
        price=positive(quotes["close_eur"][ticker],"quote "+ticker)
        asof=quotes["decision_session"]
        if dt.date.fromisoformat(asof)>now_date:raise ValueError("Future quote "+ticker)
        return price,asof,"LAST_US_COMPLETED_EOD_CROSS_EXCHANGE"
    if ticker in positions:
        p=positions[ticker]
        return positive(p["last_price_eur"],"stored quote "+ticker),p["last_price_date"],"STALE_LAST_RECORDED_EOD"
    for candidate in proposal.get("possible_candidates",[]):
        if candidate["ticker"]==ticker:
            return positive(candidate["reference_previous_close_eur"],"proposal "+ticker),proposal["decision_session"],"FROZEN_PREVIOUS_CLOSE"
    raise ValueError("No usable paper valuation for "+ticker)


def validate_state(state):
    orders=state.get("orders",[])
    holdings=state.get("holdings",{})
    if any(o.get("execution_mode")!=POLICY for o in orders):
        raise ValueError("Mixed official and approximate-ledger orders prohibited")
    for ticker,p in holdings.items():
        q=sum(float(l["shares"]) for l in p["lots"])
        if abs(q-p["shares"])>max(1e-7,1e-8*p["shares"]):raise ValueError("Lot share mismatch "+ticker)
        if p["shares"]<=0:raise ValueError("Invalid shares "+ticker)
        for lot in p["lots"]:
            if dt.date.fromisoformat(lot["sale_eligible_date"])!=dt.date.fromisoformat(lot["entry_date"])+dt.timedelta(days=35):
                raise ValueError("Lot maturity violation "+ticker)
    cash=float(state["cash_eur"])
    if cash<-.0001 or not math.isfinite(cash):raise ValueError("Negative or invalid cash")
    if len(holdings)>10:raise ValueError("More than ten core positions")
    ids=[o["order_id"] for o in orders]
    if len(ids)!=len(set(ids)):raise ValueError("Duplicate order ID")


def buy(state,ticker,budget,reference,decision,now,price_date,price_source):
    if ticker in state["holdings"]:
        # Additional independent FIFO lot is permitted, but stays locked 35d.
        pass
    if budget<max(500,0.06*float(state.get("last_nav_eur",INITIAL)))-1e-8:return False
    cash=state["cash_eur"]
    budget=min(float(budget),max(0.,cash-RESERVE))
    if budget<=FEE+1:return False
    ref=positive(reference,"purchase ref")
    px=ref*(1.+SLIPPAGE_BPS/10_000)
    shares=(budget-FEE)/px
    if not math.isfinite(shares) or shares<=0:return False
    day=now.date()
    item=state["holdings"].setdefault(ticker,{"shares":0.,"cost_basis_eur":0.,"lots":[],
        "last_price_eur":ref,"last_price_date":price_date})
    eligible=day+dt.timedelta(days=MIN_HOLD_DAYS)
    item["lots"].append({"entry_date":day.isoformat(),"shares":shares,"cost_basis_eur":budget,
                         "sale_eligible_date":eligible.isoformat(),
                         "price_source_date":price_date,"fill_method":POLICY})
    item["shares"]+=shares
    item["cost_basis_eur"]+=budget
    state["cash_eur"]-=budget
    state["fees_eur_total"]+=FEE
    state["estimated_slippage_eur"]+=shares*(px-ref)
    record={
       "order_id":f"APPROX-{len(state['orders'])+1:05d}",
       "scenario":"italian_tax","strategy":"CONFIDENCE70_ORIGINAL_SOURCE_APPROX_PAPER",
       "decision_date":decision,"date":day.isoformat(),"action":"BUY",
       "ticker":ticker,"shares":shares,"reference_price_eur":ref,
       "execution_price_eur":px,"reference_value_eur":shares*ref,
       "fee_eur":FEE,"slippage_bps_per_side":SLIPPAGE_BPS,
       "capital_gains_tax_eur":0.,"reason":"original_model_entry_proxy",
       "execution_mode":POLICY,"simulation_recorded_at_utc":now.isoformat(),
       "price_source_date":price_date,"price_quality":price_source,
       "paper_only":True,"real_market_fill":False,
    }
    state["orders"].append(record)
    return True


def sell(state,ticker,requested_value,reference,decision,now,price_date,price_source,reason):
    """FIFO 35-day absolute lock; applies to risk reductions and all exits."""
    p=state["holdings"][ticker]
    ref=positive(reference,"sale ref")
    matured=[lot for lot in p["lots"] if
             dt.date.fromisoformat(lot["sale_eligible_date"])<=now.date()]
    available=sum(lot["shares"] for lot in matured)
    req=max(0.,float(requested_value))/ref
    amount=min(available,p["shares"],req)
    if amount+1e-9<req:state["blocked_sell_requests"]+=1
    if amount<=1e-10:return False
    oldshares=p["shares"]
    basis=p["cost_basis_eur"]*amount/oldshares
    exitpx=ref*(1.-SLIPPAGE_BPS/10_000)
    proceeds=amount*exitpx-FEE
    gain=proceeds-basis
    tax=max(0.,gain)*TAX_RATE
    balance=amount
    for lot in p["lots"]:
        if dt.date.fromisoformat(lot["sale_eligible_date"])>now.date():continue
        take=min(balance,lot["shares"])
        lot["shares"]-=take
        lot["cost_basis_eur"]*=max(0.,1.-take/max(take,lot["shares"]+take))
        balance-=take
        if balance<=1e-9:break
    if balance>1e-7:raise ValueError("FIFO sales exceed mature inventory")
    p["lots"]=[lot for lot in p["lots"] if lot["shares"]>1e-8]
    p["shares"]-=amount;p["cost_basis_eur"]-=basis
    if p["shares"]<=1e-8:del state["holdings"][ticker]
    state["cash_eur"]+=proceeds-tax
    state["fees_eur_total"]+=FEE
    state["estimated_slippage_eur"]+=amount*(ref-exitpx)
    state["tax_eur_total"]+=tax
    state["orders"].append({
       "order_id":f"APPROX-{len(state['orders'])+1:05d}",
       "scenario":"italian_tax","strategy":"CONFIDENCE70_ORIGINAL_SOURCE_APPROX_PAPER",
       "decision_date":decision,"date":now.date().isoformat(),"action":"SELL",
       "ticker":ticker,"shares":amount,"reference_price_eur":ref,
       "execution_price_eur":exitpx,"reference_value_eur":amount*ref,
       "fee_eur":FEE,"slippage_bps_per_side":SLIPPAGE_BPS,
       "capital_gains_tax_eur":tax,"reason":reason,
       "execution_mode":POLICY,"simulation_recorded_at_utc":now.isoformat(),
       "price_source_date":price_date,"price_quality":price_source,
       "paper_only":True,"real_market_fill":False})
    return True


def generate_portfolio(state,quote_data,proposal,now):
    records=[];invested=0.;unrealized_tax=0.;stale=[]
    for ticker,pos in sorted(state["holdings"].items()):
        price,asof,source=next_available_price(ticker,quote_data,proposal,state["holdings"],now.date())
        # Never mark a newly bought paper position at an EOD earlier than
        # the entry's original decision: the first fill deliberately uses
        # the preceding completed close as a disclosed approximation.
        pos["last_price_eur"]=price;pos["last_price_date"]=asof
        value=price*pos["shares"];invested+=value
        unrealized_tax+=max(0.,value-pos["cost_basis_eur"])*TAX_RATE
        eligible=sum(x["shares"] for x in pos["lots"] if
                     dt.date.fromisoformat(x["sale_eligible_date"])<=now.date())
        if source.startswith("STALE"):stale.append(ticker)
        records.append({
           "ticker":ticker,"market_symbol":ticker.removeprefix("G::"),
           "shares":pos["shares"],"cost_basis_eur":round(pos["cost_basis_eur"],4),
           "market_close_eur":round(price,8),"market_value_eur":round(value,4),
           "sell_eligible_shares":round(eligible,10),
           "locked_shares":round(max(0.,pos["shares"]-eligible),10),
           "price_source_date":asof,"price_quality":source,
           "lots":pos["lots"]})
    cash=state["cash_eur"]
    nav=cash+invested
    # Informational hypothetical liquidation only: not an actual sell, no 35-day bypass.
    net=nav-unrealized_tax-max(0.,invested)*SLIPPAGE_BPS/10000-len(records)*FEE
    for x in records:x["portfolio_weight_pct"]=round(100*x["market_value_eur"]/nav,3)
    buys=sum(o["action"]=="BUY" for o in state["orders"])
    sells=sum(o["action"]=="SELL" for o in state["orders"])
    return {
       "schema_version":3,"type":"PAPER_ONLY","engine_id":ENGINE,
       "variant":"CONFIDENCE70_ORIGINAL_H4_STRICT_35_CALENDAR_DAYS_EVERY_LOT",
       "execution_adapter":POLICY,"execution_pricing_approximate":True,
       "real_orders":0,"real_money_orders":0,
       "initial_capital_eur":INITIAL,"cash_eur":round(cash,4),
       "invested_eur":round(invested,4),
       "nav_mark_to_market_eur":round(nav,4),
       "nav_net_liquidation_eur":round(net,4),
       "pnl_mark_to_market_pct":round((nav/INITIAL-1)*100,5),
       "valuation_session":quote_data.get("decision_session") or proposal["decision_session"],
       "valuation_recorded_at_utc":now.isoformat(),
       "valuation_method":"LAST_CONFIRMED_DAILY_CLOSE_WITH_STALE_FLAG",
       "stale_quote_tickers":stale,
       "positions_count":len(records),"holdings":records,
       "strict_min_holding_calendar_days":35,
       "orders_total":len(state["orders"]),"buy_orders":buys,
       "sell_orders":sells,
       "fees_eur_total":round(state["fees_eur_total"],4),
       "estimated_slippage_eur":round(state["estimated_slippage_eur"],4),
       "tax_eur_total":round(state["tax_eur_total"],4),
       "blocked_or_partial_sell_requests":state["blocked_sell_requests"],
       "universe_market_cap_pit_certified":False,
       "capitalization_shadow_as_recorded":True,
    }


def write_csv(path,headers,rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("w",encoding="utf8",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=headers,extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run(now=None):
    now=now or dt.datetime.now(UTC)
    if now.tzinfo is None:raise ValueError("Aware timestamp required")
    now=now.astimezone(UTC)
    ROOT.mkdir(parents=True,exist_ok=True)
    quote=load(QUOTES,{}) or {}
    proposal=load(PROPOSAL,{}) or {}
    published=load(STATUS,{}) or {}
    previous=load(STATE,{}) or {}
    sig=load(PREOPEN/(FIRST_DECISION+".json"),{}) or {}
    if not sig.get("market_data_first_observed_before_open"):
        raise ValueError("First original decision lacks timely pre-open provenance")
    if proposal.get("decision_session")!=FIRST_DECISION and not previous.get("orders"):
        raise ValueError("No frozen opening proposal for 2026-11-09")
    if not previous.get("orders"):
        # Initial run explicitly uses the preserved original decision weights
        # and previous day's close AFTER the actual US open on START.
        if now.date()<START:raise ValueError("Paper start date not yet reached")
        us_open=dt.datetime(2026,11,10,14,30,tzinfo=UTC)
        if now<us_open:raise ValueError("No US opening yet, not simulating fills")
    if previous.get("orders") and previous.get("execution_adapter")!=POLICY:
        raise ValueError("Cannot mutate historical exact paper ledger into approximate mode")
    previous_orders=previous.get("orders",[])
    state=previous if previous.get("execution_adapter")==POLICY else {
        "schema_version":3,"engine_id":ENGINE,
        "portfolio":"ORIGINAL_SOURCE_APPROX_PAPER",
        "initial_cash_eur":INITIAL,"cash_eur":INITIAL,"holdings":{},
        "orders":[],"last_nav_eur":INITIAL,"fees_eur_total":0.,
        "estimated_slippage_eur":0.,"tax_eur_total":0.,
        "blocked_sell_requests":0,"processed_decision_dates":[],
        "equity_points":[],"start_recorded_at_utc":now.isoformat(),
        "execution_adapter":POLICY,"real_money_authorized":False,
        "universe_lock_recorded_date":previous.get("universe_lock_recorded_date")}
    before=len(state["orders"])
    # The first signal was recorded BEFORE the exchange opening. Preserve its
    # selected tickers, never revise them using a post-open score recomputation.
    if FIRST_DECISION not in state["processed_decision_dates"]:
        candidates=proposal["possible_candidates"]
        frozen=set(sig.get("preopen_selected_symbols",[]))
        if not frozen:raise ValueError("Frozen original shortlist missing")
        eligible=[p for p in candidates if p["market_symbol"] in frozen and
                  p["meets_minimum_order_threshold"]]
        if not eligible:raise ValueError("Frozen signal generated zero executable paper proposals")
        for p in eligible:
            buy(state,p["ticker"],p["indicative_allocation_eur"],
                p["reference_previous_close_eur"],FIRST_DECISION,now,
                FIRST_DECISION,"FROZEN_PREVIOUS_CLOSE_NOT_EXCHANGE_FILL")
        state["processed_decision_dates"].append(FIRST_DECISION)
    # Later full reviews every five completed weeks, with weekly risk checks.
    # A new daily signal does not mean a daily rebalance.
    decision=published.get("decision_session")
    if decision and decision>FIRST_DECISION and decision not in state["processed_decision_dates"]:
        date=dt.date.fromisoformat(decision)
        base=dt.date.fromisoformat(FIRST_DECISION)
        weeks=(date-base).days//7
        weekly=date.weekday()==0 and weeks>=1
        full=weekly and weeks%5==0
        risk=weekly and not full
        current_proposal=proposal if proposal.get("decision_session")==decision else None
        if (full or risk) and current_proposal and now.date()>date:
            NAV=state.get("last_nav_eur",INITIAL)
            pricecache={}
            for t in list(state["holdings"]):
                pricecache[t]=next_available_price(t,quote,proposal,state["holdings"],now.date())
            if full:
                targets={p["ticker"]:NAV*p["weight_pct"]/100
                         for p in current_proposal.get("possible_candidates",[])}
                for ticker,p in list(state["holdings"].items()):
                    price,asof,source=pricecache[ticker]
                    excess=p["shares"]*price-targets.get(ticker,0.)
                    threshold=max(500.,NAV*.06)
                    if ticker not in targets or excess>=threshold:
                        sell(state,ticker,max(excess,0.),price,decision,now,asof,source,"original_full_rotation_proxy")
                for p in current_proposal.get("possible_candidates",[]):
                    ticker=p["ticker"]
                    price,asof,source=next_available_price(ticker,quote,current_proposal,state["holdings"],now.date())
                    have=state["holdings"].get(ticker,{}).get("shares",0.)*price
                    budget=NAV*p["weight_pct"]/100-have
                    if budget>=max(500.,NAV*.06):
                        buy(state,ticker,budget,price,decision,now,asof,source)
            else:
                desired=float(current_proposal.get("regime_multiplier",1.))
                invested=sum(p["shares"]*pricecache[t][0] for t,p in state["holdings"].items())
                current_ratio=invested/max(NAV,1)
                if current_ratio>desired+.25:
                    reduction=(current_ratio-desired)*NAV
                    for ticker,p in list(state["holdings"].items()):
                        if reduction<=0:break
                        px,asof,src=pricecache[ticker]
                        sell(state,ticker,min(reduction,p["shares"]*px),px,
                             decision,now,asof,src,"weekly_risk_reduction_35day_guarded")
                        reduction-=min(reduction,p["shares"]*px)
            state["processed_decision_dates"].append(decision)
    validate_state(state)
    if state["orders"][:len(previous_orders)]!=previous_orders:
        raise ValueError("Append-only historical order prefix mismatch")
    port=generate_portfolio(state,quote,proposal,now)
    state["last_nav_eur"]=port["nav_mark_to_market_eur"]
    state["last_valuation_session"]=port["valuation_session"]
    today=now.date().isoformat()
    # Keep one current observation per observation day, no fake historical NAV.
    states=[x for x in state["equity_points"] if x["report_date"]!=today]
    states.append({"report_date":today,
        "source_close_session":port["valuation_session"],
        "mark_to_market_nav_eur":port["nav_mark_to_market_eur"],
        "net_liquidation_nav_eur":port["nav_net_liquidation_eur"],
        "approximate_fills":True})
    state["equity_points"]=sorted(states,key=lambda x:x["report_date"])
    # Orders, state and report written consistently inside one GitHub Actions
    # transaction; next run's append-only checks ensure no retroactive rewrite.
    save(STATE,state)
    save(PORTFOLIO,port)
    write_csv(ORDER_PATH,ORDER_FIELDS,state["orders"])
    write_csv(EQUITY,["report_date","source_close_session","mark_to_market_nav_eur","net_liquidation_nav_eur","approximate_fills"],state["equity_points"])
    published.update({
       "status":"PAPER_APPROX_LEDGER_ACTIVE",
       "execution_policy":POLICY,"execution_pricing_approximate":True,
       "paper_orders_not_real_market_fills":True,
       "paper_orders_new":len(state["orders"])-before,
       "new_orders":len(state["orders"])-before,
       "orders_cumulative":len(state["orders"]),
       "cash_eur":port["cash_eur"],"invested_eur":port["invested_eur"],
       "positions_count":port["positions_count"],"nav_eur":port["nav_mark_to_market_eur"],
       "nav_net_liquidation_eur":port["nav_net_liquidation_eur"],
       "valuation_source_session":port["valuation_session"],
       "last_valuation_session":port["valuation_session"],
       "last_report_recorded_at_utc":now.isoformat(),
       "min_hold_calendar_days":35,
       "fees_eur_total":port["fees_eur_total"],
       "tax_eur_total":port["tax_eur_total"],
       "estimated_slippage_eur":port["estimated_slippage_eur"],
       "portfolio_report_session":port["valuation_session"],
       "last_order_execution_mode":POLICY,
       "risk_universe_fidelity":"ETF_ACWI_AS_RECORDED_OR_VENDOR_MARKETCAP_PROXY_NOT_HISTORICAL_PIT",
       "reason":"Execution is ASSUMED at previous complete close, not verified market fill.",
       "holdings_summary":[{"ticker":p["ticker"],"market_value_eur":p["market_value_eur"]}
                           for p in port["holdings"]]})
    save(STATUS,published)
    write_report(ROOT,published,port,state["orders"])
    # Put the caveat on the first screen of the human-readable report.
    text=(ROOT/"latest.md").read_text(encoding="utf8")
    warning=(
       "**MODALITÀ OPERATIVA PAPER APPROSSIMATA:** i prezzi degli ordini sono "
       "PROXY dell'ultima chiusura verificata con slippage 10 bps e commissione 1 €. "
       "Le operazioni sono state REGISTRATE ORA nel conto virtuale, non sono "
       "esecuzioni di borsa osservate, né ordini inviati a un broker. "
       "Il riferimento del segnale è la chiusura precedente e non vengono "
       "retrodatate operazioni. Il vincolo FIFO di 35 giorni è assoluto.\n\n")
    (ROOT/"latest.md").write_text(text.replace("\n\n", "\n\n"+warning,1),encoding="utf8")
    print(json.dumps({"status":published["status"],"positions":port["positions_count"],
        "orders":port["orders_total"],"new_orders":published["new_orders"],
        "nav_eur":port["nav_mark_to_market_eur"],"cash_eur":port["cash_eur"],
        "source_close":port["valuation_session"]}))
    return published


if __name__=="__main__":
    run()
