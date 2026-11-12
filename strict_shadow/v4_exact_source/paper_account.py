#!/usr/bin/env python3
"""Atlas V4 Confidence70 frozen-signal PAPER account, operational proxy execution.

Never calls broker APIs. Every order is an ASSUMED historical-close PAPER fill,
not a market execution. Signals and proposed weights are produced by the
archived RegionSlotAtlas original engine; only the execution/ledger adapter is
a deliberate approximation. No order is backdated. Append-only accounting,
35 calendar days FIFO lock even for defensive sells, idempotent daily updates.
"""
from __future__ import annotations
import csv,datetime as dt,hashlib,json,math
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
# Original five-week rotation remains champion; weekly buys are research-only.
WEEKLY_BUYS_ENABLED=False
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
        asof=quotes.get("ticker_price_dates",{}).get(ticker,quotes["decision_session"])
        age=(now_date-dt.date.fromisoformat(asof)).days
        if age<0:raise ValueError("Future quote "+ticker)
        # Use each exchange's last completed LOCAL close; never pretend that
        # every market closed on the US benchmark session.
        quality=("STALE_LAST_LOCAL_EOD_OVER_FOUR_CALENDAR_DAYS" if age>4
                 else "LAST_VERIFIED_LOCAL_EXCHANGE_EOD")
        return price,asof,quality
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
    # Conservation audit: forward PAPER has no cash flows except recorded
    # buys/sells yet. Every share must originate from a recorded purchase;
    # every EUR of cash must reconcile with executed notional, fees and tax.
    # When interest/dividend funding is introduced it must be booked as
    # explicit ledger cash-flow events, not silently added to state["cash_eur"].
    expected_cash=float(state.get("initial_cash_eur",INITIAL))
    expected_shares={}
    expected_fees=expected_slippage=expected_tax=0.0
    for order in orders:
        action=order.get("action")
        if action not in ("BUY","SELL"):raise ValueError("Unknown accounting action")
        ticker=str(order["ticker"])
        shares=positive(order["shares"],"ledger shares")
        execution=positive(order["execution_price_eur"],"ledger execution price")
        reference=positive(order["reference_price_eur"],"ledger reference price")
        fee=float(order["fee_eur"])
        tax=float(order.get("capital_gains_tax_eur",0.0))
        if not all(math.isfinite(v) and v>=0 for v in (fee,tax)):
            raise ValueError("Invalid accounting charge")
        if action=="BUY" and tax>1e-8:raise ValueError("Purchase charged capital gains tax")
        expected_fees+=fee
        expected_tax+=tax
        expected_slippage+=shares*abs(execution-reference)
        if action=="BUY":
            expected_cash-=shares*execution+fee
            expected_shares[ticker]=expected_shares.get(ticker,0.0)+shares
        else:
            expected_cash+=shares*execution-fee-tax
            expected_shares[ticker]=expected_shares.get(ticker,0.0)-shares
    tolerance=max(1e-5,1e-8*float(state.get("initial_cash_eur",INITIAL)))
    if abs(expected_cash-cash)>tolerance:
        raise ValueError(f"Cash creation/loss: recorded={cash:.6f}, ledger={expected_cash:.6f}")
    for ticker in set(expected_shares)|set(holdings):
        actual=float(holdings.get(ticker,{}).get("shares",0.0))
        if abs(expected_shares.get(ticker,0.0)-actual)>max(1e-8,1e-7*abs(actual)):
            raise ValueError("Unbacked shares / share conservation: "+ticker)
    for field,expected in (("fees_eur_total",expected_fees),
                           ("estimated_slippage_eur",expected_slippage),
                           ("tax_eur_total",expected_tax)):
        if abs(float(state.get(field,0.0))-expected)>tolerance:
            raise ValueError("Non-reconciled accounting total: "+field)


def buy(state,ticker,budget,reference,decision,now,price_date,price_source):
    if ticker not in state["holdings"] and len(state["holdings"])>=10:
        return False  # Exposure/position cap is absolute, including locked lots.
    if ticker in state["holdings"]:
        # Additional independent FIFO lot is permitted, but stays locked 35d.
        pass
    threshold=max(500.,0.06*float(state.get("last_nav_eur",INITIAL)))
    if budget<threshold-1e-8:return False
    cash=state["cash_eur"]
    budget=min(float(budget),max(0.,cash-RESERVE))
    # Never execute a sub-minimum order after remaining cash/reserve limits
    # have clipped an initially eligible request.
    if budget<threshold-1e-8 or budget<=FEE+1:return False
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
    # FIFO COST BASIS, not the weighted average of mature and immature lots.
    # Otherwise sale tax was inconsistent with the lots actually consumed.
    balance=amount
    basis=0.0
    for lot in p["lots"]:
        if dt.date.fromisoformat(lot["sale_eligible_date"])>now.date():continue
        if balance<=1e-9:break
        prior_shares=float(lot["shares"])
        take=min(balance,prior_shares)
        portion=float(lot["cost_basis_eur"])*take/prior_shares
        basis+=portion
        lot["shares"]=prior_shares-take
        lot["cost_basis_eur"]=max(0.,float(lot["cost_basis_eur"])-portion)
        balance-=take
    if balance>1e-7:raise ValueError("FIFO sales exceed mature inventory")
    p["lots"]=[lot for lot in p["lots"] if lot["shares"]>1e-8]
    p["shares"]-=amount;p["cost_basis_eur"]-=basis
    # Explicit lot cost-basis audit: no implicit creation of taxable basis.
    if p["shares"]>1e-8 and abs(p["cost_basis_eur"]-sum(float(l["cost_basis_eur"]) for l in p["lots"]))>1e-5:
        raise ValueError("FIFO cost basis mismatch after sale")
    exitpx=ref*(1.-SLIPPAGE_BPS/10_000)
    proceeds=amount*exitpx-FEE
    gain=proceeds-basis
    tax=max(0.,gain)*TAX_RATE
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


def execute_or_defer_five_week_rebalance(state,candidates,decision,quotes,now,
                                         deferred=False):
    """One frozen five-week signal, executed only with sale-eligible LOCAL bars.

    No retroactive fills and no relaxation of FIFO. If any mandatory replacement
    requires immature shares, keep the WHOLE rotation as an explicit pending
    decision (rather than losing it for the next 5-week interval).
    """
    nav=float(state["cash_eur"])
    priced={}
    for ticker,p in state["holdings"].items():
        px,session,quality=next_available_price(ticker,quotes,
            {"possible_candidates":candidates},state["holdings"],now.date())
        if quality.startswith("STALE"):
            return {"completed":False,"blocked":[ticker],"reason":"STALE_HELD_QUOTE"}
        priced[ticker]=(px,session,quality)
        nav+=p["shares"]*px
    targets={x["ticker"]:nav*float(x["weight_pct"])/100.
             for x in candidates}
    threshold=max(500.,nav*.06)
    planned=[]
    blocked=[]
    for ticker,p in list(state["holdings"].items()):
        px,price_day,quality=priced[ticker]
        excess=p["shares"]*px-targets.get(ticker,0.)
        if ticker in targets and excess<threshold:continue
        required_shares=min(p["shares"],max(0.,excess)/px)
        if required_shares<=1e-10:continue
        mature_shares=0.
        for lot in p["lots"]:
            # Never treat a Dec 14 close as the executable price for a
            # Dec 15-matured lot even when the GitHub runner starts Dec 15.
            if (dt.date.fromisoformat(lot["sale_eligible_date"])<=now.date()
                and dt.date.fromisoformat(lot["sale_eligible_date"])<=dt.date.fromisoformat(price_day)):
                mature_shares+=lot["shares"]
        if mature_shares+1e-9<required_shares:
            blocked.append(ticker)
        else:
            planned.append((ticker,max(0.,excess),px,price_day,quality))
    if blocked:
        return {"completed":False,"blocked":sorted(blocked),
                "reason":"FIFO_OR_COMPLETED_LOCAL_CLOSE_NOT_YET_AVAILABLE",
                "earliest_unlock_dates":{t:min(x["sale_eligible_date"] for x in
                  state["holdings"][t]["lots"]) for t in blocked}}
    for ticker,excess,px,price_day,quality in planned:
        if not sell(state,ticker,excess,px,decision,now,price_day,quality,
                    "original_five_week_rotation_deferred_after_fifo" if deferred
                    else "original_five_week_rotation_proxy"):
            raise ValueError("Prevalidated mature 35-day sale unexpectedly blocked")
    for candidate in candidates:
        ticker=candidate["ticker"]
        if ticker not in quotes.get("close_eur",{}):
            continue   # No imaginary fill from an old planner close.
        px,price_day,quality=next_available_price(ticker,quotes,
                    {"possible_candidates":candidates},state["holdings"],now.date())
        if quality.startswith("STALE") or dt.date.fromisoformat(price_day)<dt.date.fromisoformat(decision):
            continue
        owned=state["holdings"].get(ticker,{}).get("shares",0.)*px
        budget=nav*float(candidate["weight_pct"])/100.-owned
        if budget>=threshold:buy(state,ticker,budget,px,decision,now,price_day,quality)
    return {"completed":True,"blocked":[],"reason":"FIVE_WEEK_TARGET_EXECUTED",
            "deferred":deferred}


def generate_portfolio(state,quote_data,proposal,now):
    records=[];invested=0.;stale=[]
    for ticker,pos in sorted(state["holdings"].items()):
        price,asof,source=next_available_price(ticker,quote_data,proposal,state["holdings"],now.date())
        # Never mark a newly bought paper position at an EOD earlier than
        # the entry's original decision: the first fill deliberately uses
        # the preceding completed close as a disclosed approximation.
        pos["last_price_eur"]=price;pos["last_price_date"]=asof
        value=price*pos["shares"];invested+=value
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
    # Simultaneous HYPOTHETICAL liquidation once all locks have expired.
    # Profits and eligible capital losses from these equity disposals net out
    # within the same taxable category (not dividends or investment income).
    # Does not claim that tax credits can offset unrelated income or that
    # the positions are legally sellable today. Realized profits remain taxed
    # in the order ledger when a sale actually occurs.
    hypothetical_sale_cost=max(0.,invested)*SLIPPAGE_BPS/10000+len(records)*FEE
    hypothetical_net_proceeds=invested-hypothetical_sale_cost
    hypothetical_total_cost_basis=sum(float(p["cost_basis_eur"]) for p in state["holdings"].values())
    hypothetical_net_gain=hypothetical_net_proceeds-hypothetical_total_cost_basis
    hypothetical_sale_tax=max(0.,hypothetical_net_gain)*TAX_RATE
    net=cash+hypothetical_net_proceeds-hypothetical_sale_tax
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
       "hypothetical_liquidation_sale_cost_eur":round(hypothetical_sale_cost,4),
       "hypothetical_liquidation_tax_eur":round(hypothetical_sale_tax,4),
       "hypothetical_liquidation_net_gain_eur":round(hypothetical_net_gain,4),
       "hypothetical_liquidation_assumes_all_lots_mature":True,
       "hypothetical_liquidation_tax_method":"SIMULTANEOUS_COMPENSABLE_EQUITY_GAINS_LOSSES_26PCT",

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


def reconcile_daily_nav(state,portfolio,quotes,now):
    """One observation per ACTUAL market-close session, not per runner date.

    Vendor FX/chart revisions can revalue an already-seen EOD. Keep the latest
    mark for that session but audit the previous numerical version. Neither
    the revision nor a rerun counts as an additional trading day, and neither
    mutates any historical order, position or cash.
    """
    session=portfolio["valuation_session"]
    observed=now.isoformat()
    fx_rates=quotes.get("fx_units_per_eur_asof") or {}
    fx_dates=quotes.get("fx_currency_source_sessions") or {}
    owned_quotes={ticker:{"price_eur":float(pos["market_close_eur"]),
                           "exchange_session":str(pos.get("price_source_date",""))}
                  for ticker,pos in state["holdings"].items()}
    evidence={"exchange_close_session":session,"held_quotes":owned_quotes,
              "fx_units_per_eur":fx_rates,"fx_source_sessions":fx_dates}
    fingerprint=hashlib.sha256(json.dumps(evidence,sort_keys=True,
                separators=(",",":"),allow_nan=False).encode()).hexdigest()
    current={"report_date":now.date().isoformat(),
             "source_close_session":session,
             "mark_to_market_nav_eur":portfolio["nav_mark_to_market_eur"],
             "net_liquidation_nav_eur":portfolio["nav_net_liquidation_eur"],
             "approximate_fills":True,
             "first_observed_at_utc":observed,
             "last_observed_at_utc":observed,
             "quote_evidence_sha256":fingerprint,
             "fx_source_sessions":fx_dates,"fx_units_per_eur":fx_rates,
             "held_quotes":owned_quotes}
    history={}
    revisions=state.setdefault("valuation_revisions",[])
    def materially_different(before,after):
        return (abs(float(before["mark_to_market_nav_eur"])-
                    float(after["mark_to_market_nav_eur"]))>0.0001 or
                abs(float(before["net_liquidation_nav_eur"])-
                    float(after["net_liquidation_nav_eur"]))>0.0001)
    def archive(old,new,why):
        revisions.append({"source_close_session":new["source_close_session"],
            "detected_at_utc":observed,"reason":why,
            "prior_report_date":old.get("report_date"),
            "prior_mark_to_market_nav_eur":old["mark_to_market_nav_eur"],
            "new_mark_to_market_nav_eur":new["mark_to_market_nav_eur"],
            "prior_net_liquidation_nav_eur":old["net_liquidation_nav_eur"],
            "new_net_liquidation_nav_eur":new["net_liquidation_nav_eur"],
            "prior_fx_units_per_eur":old.get("fx_units_per_eur",{}),
            "new_fx_units_per_eur":new.get("fx_units_per_eur",{}),
            "prior_held_quotes":old.get("held_quotes",{}),
            "new_held_quotes":new.get("held_quotes",{}),
            "prior_evidence_sha256":old.get("quote_evidence_sha256","LEGACY_UNAVAILABLE"),
            "new_evidence_sha256":new.get("quote_evidence_sha256","LEGACY_UNAVAILABLE")})
    # Prior runner-version convention could save the same EOD two times
    # under separate UTC dates. Audit both values, keeping the later mark.
    for old in sorted(state.get("equity_points",[]),
                      key=lambda v:(v.get("report_date",""),v.get("last_observed_at_utc",""))):
        key=old.get("source_close_session") or old["report_date"]
        if key in history and materially_different(history[key],old):
            archive(history[key],old,"LEGACY_DUPLICATE_CLOSE_REVALUED")
        history[key]=old
    prior=history.get(session)
    if prior is not None:
        current["first_observed_at_utc"]=prior.get("first_observed_at_utc",
                                                   prior.get("last_observed_at_utc",
                                                             "LEGACY_UNKNOWN"))
        current["report_date"]=prior.get("report_date",current["report_date"])
        if materially_different(prior,current):
            reason=("FX_REFERENCE_REVISED_FOR_SAME_CLOSE"
                    if prior.get("fx_units_per_eur") and
                       prior.get("fx_units_per_eur")!=fx_rates and
                       prior.get("held_quotes")==owned_quotes
                    else "SAME_CLOSE_REVALUED_PRICE_OR_FX")
            archive(prior,current,reason)
        else:
            # Even an unchanged NAV can gain new currency provenance. This
            # is metadata enrichment, never a second daily P&L observation.
            current["last_observed_at_utc"]=observed
    history[session]=current
    state["equity_points"]=[history[x] for x in sorted(history)]
    if len({p["source_close_session"] for p in state["equity_points"]})!=len(state["equity_points"]):
        raise ValueError("Multiple daily NAV points from one exchange session")
    return {"valuation_session":session,"historical_daily_sessions":len(history),
            "valuation_revision_count":len(revisions),
            "valuation_revised_in_current_run":any(
                row["detected_at_utc"]==observed for row in revisions),
            "quote_evidence_sha256":fingerprint}


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
    # Immutable first decision is never recalculated after source revisions.
    frozen_first=load(PREOPEN/(FIRST_DECISION+"_proposal.json"),{}) or {}
    published=load(STATUS,{}) or {}
    previous=load(STATE,{}) or {}
    sig=load(PREOPEN/(FIRST_DECISION+".json"),{}) or {}
    if not sig.get("market_data_first_observed_before_open"):
        raise ValueError("First original decision lacks timely pre-open provenance")
    if frozen_first.get("decision_session")!=FIRST_DECISION and not previous.get("orders"):
        raise ValueError("No immutable opening proposal for 2026-11-09")
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
        candidates=frozen_first["possible_candidates"]
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
    # Persisted daily source vintages make Monday decisions available on
    # Tuesday. Never use a Tuesday signal as a substitute for Monday's review.
    # Evaluate each dated Monday at most once, in chronological order.
    dated_folder=ROOT/"proposals_by_session"
    for dated_file in sorted(dated_folder.glob("*.json")):
        decision=dated_file.stem
        if decision<=FIRST_DECISION or decision in state["processed_decision_dates"]:
            continue
        try:date=dt.date.fromisoformat(decision)
        except ValueError:continue
        if date.weekday()!=0 or date>=now.date():
            continue
        base=dt.date.fromisoformat(FIRST_DECISION)
        weeks=(date-base).days//7
        if weeks<1:continue
        full=(weeks%5)==0
        current_proposal=load(dated_file,{})
        if current_proposal.get("decision_session")!=decision:
            raise ValueError("Dated original model proposal does not match its filename: "+decision)
        # Never trade from an unobservably old or incomplete price feed. A
        # missed review remains pending until a completed post-decision close.
        source_session=quote.get("decision_session","")
        if not source_session or source_session<decision:
            break
        NAV=state.get("last_nav_eur",INITIAL)
        pricecache={}
        for t in list(state["holdings"]):
            pricecache[t]=next_available_price(t,quote,current_proposal,state["holdings"],now.date())
        if full:
            frozen_candidates=current_proposal.get("possible_candidates",[])
            if not frozen_candidates or any("weight_pct" not in x for x in frozen_candidates):
                raise ValueError("Missing original frozen target weights for "+decision)
            outcome=execute_or_defer_five_week_rebalance(
                state,frozen_candidates,decision,quote,now)
            if not outcome["completed"]:
                state["pending_full_rotation"]={
                    "original_decision_session":decision,
                    "frozen_candidates":frozen_candidates,
                    "first_deferred_at_utc":now.isoformat(),
                    "blocked_tickers":outcome["blocked"],
                    "earliest_unlock_dates":outcome.get("earliest_unlock_dates",{}),
                    "defer_reason":outcome["reason"],
                    "executed":False}
            else:
                state.pop("pending_full_rotation",None)
        else:
            desired=float(current_proposal.get("regime_multiplier",1.))
            invested=sum(p["shares"]*pricecache[t][0] for t,p in state["holdings"].items())
            current_ratio=invested/max(NAV,1.)
            if current_ratio>desired+.25:
                reduction=(current_ratio-desired)*NAV
                for ticker,p in list(state["holdings"].items()):
                    if reduction<=0:break
                    px,asof,source=pricecache[ticker]
                    previous_shares=p["shares"]
                    sell(state,ticker,min(reduction,p["shares"]*px),px,
                         decision,now,asof,source,"weekly_risk_reduction_35day_guarded")
                    residual=state["holdings"].get(ticker,{}).get("shares",0.)
                    reduction-=max(0.,previous_shares-residual)*px
            if WEEKLY_BUYS_ENABLED:
                # Weekly BUY challenger: the 35-day restriction applies ONLY to
                # sells. Buy using an observed Monday decision, NEVER an intraday
                # or retrospectively inferred weekly ranking.
                candidates=current_proposal.get("possible_candidates",[])
                # Backwards-compatible frozen proposals may contain target EUR
                # without explicit weight_pct. This is a target weight, NOT a
                # second cash grant or an unbacked order.
                def target_weight_pct(candidate):
                    if "weight_pct" in candidate:
                        return float(candidate["weight_pct"])
                    original_cap=float(current_proposal.get("capital_eur",INITIAL))
                    return 100.*float(candidate["indicative_allocation_eur"])/original_cap
                portfolio_nav=state["cash_eur"]+sum(
                    p["shares"]*pricecache[t][0] for t,p in state["holdings"].items())
                target_equity=portfolio_nav*sum(
                    max(0.,target_weight_pct(p)) for p in candidates)/100.
                owned_equity=sum(
                    p["shares"]*pricecache[t][0] for t,p in state["holdings"].items())
                room=max(0.,target_equity-owned_equity)
                threshold=max(500.,0.06*portfolio_nav)
                for candidate in candidates:
                    ticker=candidate["ticker"]
                    if ticker not in state["holdings"] and len(state["holdings"])>=10:
                        continue
                    price,asof,source=next_available_price(
                        ticker,quote,current_proposal,state["holdings"],now.date())
                    if source.startswith("STALE"):
                        continue
                    owned=state["holdings"].get(ticker,{}).get("shares",0.)*price
                    target=portfolio_nav*target_weight_pct(candidate)/100.
                    addition=min(room,max(0.,target-owned))
                    if addition>=threshold and buy(
                        state,ticker,addition,price,decision,now,asof,source):
                        # Shares were paid entirely from settled cash, including
                        # fees and slippage, never from synthetic credit.
                        room=max(0.,room-addition)
        state["processed_decision_dates"].append(decision)
    # A Monday's frozen full rotation is not silently discarded simply
    # because the youngest lot becomes sellable on the following Tuesday.
    # Retry ONLY with subsequently completed, eligible market data and record
    # the actual runner date; no retroactive price or execution timestamp.
    deferred_rotation=state.get("pending_full_rotation")
    if deferred_rotation and not deferred_rotation.get("executed"):
        outcome=execute_or_defer_five_week_rebalance(
            state,deferred_rotation["frozen_candidates"],
            deferred_rotation["original_decision_session"],quote,now,deferred=True)
        if outcome["completed"]:
            state.setdefault("completed_deferred_rotations",[]).append({
                "original_decision_session":deferred_rotation["original_decision_session"],
                "completed_at_utc":now.isoformat(),
                "source_close_session":quote.get("decision_session"),
                "retroactive_execution":False})
            state.pop("pending_full_rotation",None)
        else:
            deferred_rotation["blocked_tickers"]=outcome["blocked"]
            deferred_rotation["defer_reason"]=outcome["reason"]
            deferred_rotation["earliest_unlock_dates"]=outcome.get("earliest_unlock_dates",{})
    # The last recorded quote is not necessarily the latest decision's quote.
    # The preserved first-day prices remain available for new-lot validation.
    if not quote.get("close_eur"):
        quote={"decision_session":frozen_first.get("decision_session", FIRST_DECISION),
               "close_eur":{x["ticker"]:x["reference_previous_close_eur"]
                            for x in frozen_first.get("possible_candidates",[])}}
    validate_state(state)
    if state["orders"][:len(previous_orders)]!=previous_orders:
        raise ValueError("Append-only historical order prefix mismatch")
    port=generate_portfolio(state,quote,proposal,now)
    state["last_nav_eur"]=port["nav_mark_to_market_eur"]
    state["last_valuation_session"]=port["valuation_session"]
    # Never count the same market session twice due to a second run or a
    # corrected end-of-day FX rate. Archive the previously published valuation.
    valuation_audit=reconcile_daily_nav(state,port,quote,now)
    # Orders, state and report written consistently inside one GitHub Actions
    # transaction; next run's append-only checks ensure no retroactive rewrite.
    save(STATE,state)
    save(PORTFOLIO,port)
    write_csv(ORDER_PATH,ORDER_FIELDS,state["orders"])
    write_csv(EQUITY,["report_date","source_close_session","mark_to_market_nav_eur","net_liquidation_nav_eur","approximate_fills"],state["equity_points"])
    # Auditable append-only revisions; never recast an FX correction as
    # a new daily gain/loss. JSON state also stores the full provenance.
    write_csv(ROOT/"valuation_revisions.csv",
              ["source_close_session","detected_at_utc","reason","prior_report_date",
               "prior_mark_to_market_nav_eur","new_mark_to_market_nav_eur",
               "prior_net_liquidation_nav_eur","new_net_liquidation_nav_eur",
               "prior_evidence_sha256","new_evidence_sha256"],
              state.get("valuation_revisions",[]))
    first_rotation=dt.date.fromisoformat(FIRST_DECISION)
    next_rotation=first_rotation+dt.timedelta(days=35)
    while next_rotation<=now.date() and next_rotation.isoformat() in state["processed_decision_dates"]:
        next_rotation+=dt.timedelta(days=35)
    published.update({
       "status":"PAPER_APPROX_LEDGER_ACTIVE",
       "next_full_rotation_decision_date":next_rotation.isoformat(),
       "valuation_audit":valuation_audit,
       "daily_performance_basis":"UNIQUE_EXCHANGE_CLOSE_SESSION_AFTER_FX_REVISIONS",
       "daily_performance_revision_guard":True,
       "portfolio_account_status":"PAPER_APPROX_LEDGER_ACTIVE",
       "portfolio_data_lag_calendar_days":(now.date()-dt.date.fromisoformat(port["valuation_session"])).days,
       "signal_engine_status":published.get("signal_engine_status","NOT_REPORTED"),
       "signal_engine_error":published.get("signal_engine_error"),
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
       "processed_review_dates":state["processed_decision_dates"],
       "pending_full_rotation":{k:v for k,v in state.get("pending_full_rotation",{}).items()
                                if k!="frozen_candidates"} if state.get("pending_full_rotation") else None,
       "completed_deferred_rotation_count":len(state.get("completed_deferred_rotations",[])),
       "weekly_monday_review":True,"full_rebalance_every_five_weeks":True,
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
