"""Atlas V4 Confidence70 independent EUR PAPER ledger; never submits broker orders."""
from __future__ import annotations
import copy
import datetime as dt
import hashlib
import json
import math
from zoneinfo import ZoneInfo

# Local scheduled opening of supported equity exchanges. Calendar holidays and
# session validity are independently enforced by actual provider bars.
# This cutoff is deliberately 30 minutes BEFORE the earliest relevant open.
EXCHANGE_OPEN={
    '.T':('Asia/Tokyo',9,0),'.TW':('Asia/Taipei',9,0),
    '.HK':('Asia/Hong_Kong',9,30),'.KS':('Asia/Seoul',9,0),
    '.SR':('Asia/Riyadh',10,0),'.AX':('Australia/Sydney',10,0),
    '.NS':('Asia/Kolkata',9,15),'.BO':('Asia/Kolkata',9,15),
    '.SI':('Asia/Singapore',9,0),
    '.L':('Europe/London',8,0),'.SW':('Europe/Zurich',9,0),
    '.TO':('America/Toronto',9,30),'.V':('America/Toronto',9,30),
    '.AS':('Europe/Amsterdam',9,0),'.PA':('Europe/Paris',9,0),
    '.DE':('Europe/Berlin',9,0),'.MI':('Europe/Rome',9,0),
    '.MC':('Europe/Madrid',9,0),'.ST':('Europe/Stockholm',9,0),
    '.CO':('Europe/Copenhagen',9,0),'.OL':('Europe/Oslo',9,0),
    '.HE':('Europe/Helsinki',10,0),
}
US_OPEN=('America/New_York',9,30)

def earliest_open_utc(symbols,session):
    """Earliest possible global stock-session open (never a fill price).

    Unknown exchange suffix fails closed, rather than assuming a US opening.
    Provider must still verify the exact local session at execution.
    """
    date=dt.date.fromisoformat(str(session))
    opens=[]
    for item in symbols or ['AAPL']:
        symbol=str(item).upper()
        suffix=next((s for s in sorted(EXCHANGE_OPEN,key=len,reverse=True) if symbol.endswith(s)),None)
        if suffix:
            zone,hour,minute=EXCHANGE_OPEN[suffix]
        elif '.' not in symbol or symbol.endswith(('.A','.B')):
            zone,hour,minute=US_OPEN
        else:
            raise PaperBlocked('unknown exchange opening schedule: '+symbol)
        local_open=dt.datetime.combine(date,dt.time(hour,minute),ZoneInfo(zone))
        opens.append(local_open.astimezone(dt.timezone.utc))
    return min(opens)

ENGINE="ATLAS_UNIFIED_REGION_ROTATE_V4_CONFIDENCE_BETA070_2026-10-07"
FEE=1.0
SLIPPAGE=0.001
TAX=0.26
INITIAL=10000.0

class PaperBlocked(ValueError): pass

def finite(x,label,minimum=None):
    try: v=float(x)
    except (ValueError,TypeError) as e:raise PaperBlocked(label+' not numeric') from e
    if not math.isfinite(v) or (minimum is not None and v<minimum):raise PaperBlocked(label+' outside bounds')
    return v

def canonical_sha(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def fresh_state():
    return {'schema_version':1,'engine_id':ENGINE,'mode':'PAPER','initial_cash_eur':INITIAL,
            'cash_eur':INITIAL,'holdings':{},'orders':[],'decisions':[],
            'last_execution_session':None,'real_money_authorized':False}

# Persist each purchase as an independent tax/holding-period lot.
# Pre-existing November 6 paper holdings are migrated lazily from their exact
# recorded first-entry date and aggregate cost basis, without rewriting history.
LOT_EPS=1e-8

def position_lots(pos):
    if 'lots' not in pos:
        return [{'entry_date':pos['first_entry_date'],
                 'shares':pos['shares'],
                 'cost_basis_eur_per_share':pos['cost_basis_eur_per_share']}]
    lots=pos['lots']
    if not isinstance(lots,list) or not lots:raise PaperBlocked('missing position lots')
    return lots

def ensure_lots(pos):
    if 'lots' not in pos:
        pos['lots']=position_lots(pos)
    return pos['lots']

def sync_position(pos):
    lots=[x for x in ensure_lots(pos) if x['shares']>LOT_EPS]
    if not lots:raise PaperBlocked('cannot sync empty position')
    shares=sum(x['shares'] for x in lots)
    pos['lots']=lots
    pos['shares']=shares
    pos['cost_basis_eur_per_share']=sum(
        x['shares']*x['cost_basis_eur_per_share'] for x in lots)/shares
    pos['first_entry_date']=min(x['entry_date'] for x in lots)

def validate_state(st):
    if st.get('schema_version')!=1 or st.get('engine_id')!=ENGINE or st.get('mode')!='PAPER':raise PaperBlocked('state identity or mode mismatch')
    if st.get('real_money_authorized') is not False:raise PaperBlocked('real-money flags forbidden')
    finite(st.get('cash_eur'),'cash',0)
    h=st.get('holdings')
    if not isinstance(h,dict) or len(h)>10:raise PaperBlocked('invalid holdings')
    for ticker,pos in h.items():
        if not ticker or not pos.get('market_symbol'):raise PaperBlocked('unmapped holdings symbol')
        shares=finite(pos.get('shares'),'shares',0)
        basis=finite(pos.get('cost_basis_eur_per_share'),'cost basis',0)
        first=dt.date.fromisoformat(pos['first_entry_date'])
        if shares<=LOT_EPS:raise PaperBlocked('nonpositive holding')
        lots=position_lots(pos)
        count=0.; cost=0.; earliest=None
        for lot in lots:
            if not isinstance(lot,dict):raise PaperBlocked('invalid position lot')
            day=dt.date.fromisoformat(lot['entry_date'])
            if day<first:raise PaperBlocked('lot predates first entry')
            qty=finite(lot.get('shares'),'lot shares',0)
            unit=finite(lot.get('cost_basis_eur_per_share'),'lot cost',0)
            if qty<=LOT_EPS:raise PaperBlocked('empty position lot')
            count+=qty;cost+=qty*unit
            earliest=day if earliest is None or day<earliest else earliest
        if first!=earliest or not math.isclose(count,shares,rel_tol=1e-9,abs_tol=LOT_EPS):
            raise PaperBlocked('lot share reconciliation mismatch')
        if not math.isclose(cost,shares*basis,rel_tol=1e-9,abs_tol=1e-6):
            raise PaperBlocked('lot cost reconciliation mismatch')
    if not isinstance(st.get('decisions'),list) or not isinstance(st.get('orders'),list):raise PaperBlocked('missing ledger history')
    return st

def validate_targets(snapshot):
    targets=snapshot['targets']; seen=set();sec={};w=0.
    if not isinstance(targets,list) or len(targets)>10:raise PaperBlocked('invalid target count')
    for p in targets:
        name=p['ticker'];symbol=p.get('market_symbol')
        if not name or name in seen or not isinstance(symbol,str) or not symbol.strip():raise PaperBlocked('missing ticker mapping or duplicate')
        seen.add(name)
        if p.get('region') not in ('USA','ROW') or not p.get('sector'):raise PaperBlocked('region/sector not validated')
        weight=finite(p.get('weight'),'target weight',0)
        if weight>0.15+1e-9:raise PaperBlocked('single name cap 15% exceeded')
        w+=weight
        sec[p['sector']]=sec.get(p['sector'],0)+weight
    if w>0.99+1e-9 or any(x>0.30+1e-9 for x in sec.values()):raise PaperBlocked('exposure or sector cap breached')
    return {x['ticker']:x for x in targets}

def execute_paper(state,snapshot,quotes,rebalance_fraction=0.05,execution_price_kind='open'):
    """Atomic PAPER execution at verified session price ('open' or locked US MOC 'close').

    A closing-price fill requires a separately committed pre-close US MOC intent.
    This method never fetches or invents the price and never sends broker orders.
    """
    if execution_price_kind not in ('open','close'):
        raise PaperBlocked('unsupported paper execution price field')
    if execution_price_kind=='close' and snapshot.get('execution_policy')!='US_MOC_LOCKED_PRE_CLOSE':
        raise PaperBlocked('close fills require pre-close locked MOC intent')
    rebalance_fraction=finite(rebalance_fraction,'rebalance_fraction',0)
    if rebalance_fraction>0.1:raise PaperBlocked('rebalance threshold too large')
    old=validate_state(copy.deepcopy(state))
    if snapshot.get('engine_id')!=ENGINE or snapshot.get('mode')!='SHADOW':raise PaperBlocked('wrong signal source')
    tar=validate_targets(snapshot)
    execution=dt.date.fromisoformat(snapshot['execution_session'])
    decision=dt.date.fromisoformat(snapshot['decision_session'])
    if execution<=decision:raise PaperBlocked('signal has future knowledge')
    if old['last_execution_session'] and execution<=dt.date.fromisoformat(old['last_execution_session']):raise PaperBlocked('out of order execution session')
    sig=canonical_sha(snapshot)
    if any(x['decision_session']==str(decision) for x in old['decisions']):raise PaperBlocked('decision session already consumed')
    holdings=old['holdings']
    all_names=set(holdings)|set(tar)
    prices={t:finite(quotes[t],f'price:{t}',0.00000001) for t in all_names}
    nav=old['cash_eur']+sum(finite(p['shares'],'shares',0)*prices[t] for t,p in holdings.items())
    if nav<=0:raise PaperBlocked('nonpositive NAV')
    cash=old['cash_eur']
    estimated_target={t:nav*finite(p['weight'],'target weight',0) for t,p in tar.items()}
    # Sell ONLY lots aged at least 35 calendar days. Locked lots remain.
    for t,pos in holdings.items():
        ensure_lots(pos)
        if any(dt.date.fromisoformat(lot['entry_date'])>execution for lot in pos['lots']):
            raise PaperBlocked('future-dated purchase lot')
        if t in tar and (pos['market_symbol']!=tar[t]['market_symbol']
                         or pos['sector']!=tar[t]['sector']
                         or pos['region']!=tar[t]['region']):
            raise PaperBlocked('held ticker identity/sector mismatch')
    sells=[]
    for t,pos in holdings.items():
        current=pos['shares']*prices[t];desired=estimated_target.get(t,0)
        excess=current-desired
        if excess<=max(nav*rebalance_fraction,3.):continue
        unlocked=sum(lot['shares'] for lot in pos['lots']
                     if (execution-dt.date.fromisoformat(lot['entry_date'])).days>=35)
        shares=min(unlocked,excess/prices[t])
        if shares*prices[t]>3.:sells.append((t,shares))
    orders=[]
    for t,shares in sorted(sells):
        p=holdings[t];price=prices[t]
        remaining=shares;cost=0.
        # Deterministic FIFO among unlocked lots, never reduce locked shares.
        for lot in sorted(p['lots'],key=lambda x:x['entry_date']):
            if (execution-dt.date.fromisoformat(lot['entry_date'])).days<35:continue
            part=min(remaining,lot['shares'])
            if part<=0:continue
            cost+=part*lot['cost_basis_eur_per_share']
            lot['shares']-=part;remaining-=part
            if remaining<=LOT_EPS:break
        if remaining>LOT_EPS:raise PaperBlocked('unlocked lot quantity exceeded')
        gross=shares*price*(1-SLIPPAGE)
        gain=max(0,gross-cost-FEE)
        taxes=gain*TAX;proceeds=gross-FEE-taxes
        if proceeds<=0:raise PaperBlocked('sell after fees has negative proceeds')
        cash+=proceeds
        p['lots']=[x for x in p['lots'] if x['shares']>LOT_EPS]
        if p['lots']:sync_position(p)
        else:del holdings[t]
        orders.append({'session':str(execution),'action':'SELL','ticker':t,'market_symbol':p['market_symbol'],
                       'shares':round(shares,10),'reference_open_eur':price,'execution_price_eur':round(price*(1-SLIPPAGE),10),
                       'fee_eur':FEE,'tax_estimate_eur':round(taxes,6),'cash_change_eur':round(proceeds,6),
                       'signal_sha256':sig,'mode':'PAPER'})
    # Buy toward target, enforcing 1% cash reserve, max 10 names and 35-day holding on future exits.
    need=[]
    for t,p in tar.items():
        cur=holdings.get(t,{}).get('shares',0)*prices[t]
        difference=estimated_target[t]-cur
        if difference>max(nav*rebalance_fraction,3.):need.append((t,difference))
    # Locked holdings occupy slots. Allocate remaining slots to the largest
    # *regional shortfall* first so eight locked USA names cannot crowd out
    # the two ROW slots explicitly selected by a 8-USA/2-ROW signal.
    free_slots=max(0,10-len(holdings))
    eligible=[(t,v) for t,v in need if t in holdings]
    new_names=[(t,v) for t,v in need if t not in holdings]
    target_region={}
    actual_region={}
    for entry in tar.values():
        region=entry['region']
        target_region[region]=target_region.get(region,0.)+nav*entry['weight']
    for t,pos in holdings.items():
        region=pos['region']
        actual_region[region]=actual_region.get(region,0.)+pos['shares']*prices[t]
    new_names.sort(key=lambda item:(
        -max(0,target_region.get(tar[item[0]]['region'],0.)-
               actual_region.get(tar[item[0]]['region'],0.)),
        -item[1],item[0]))
    eligible.extend(new_names[:free_slots])
    need=eligible
    required=sum(v for _,v in need)
    free=max(0,cash-0.01*nav-len(need)*FEE)
    gross_exposure=sum(pos['shares']*prices[t] for t,pos in holdings.items())
    target_exposure=nav*sum(x['weight'] for x in tar.values())
    # Locked holdings consume the budget: don't buy beyond the signal's risk regime.
    budget=max(0,target_exposure-gross_exposure)
    scale=min(1.,free/required,budget/required) if required>0 else 0
    sector_values={}
    for t,pos in holdings.items():
        sector_values[pos['sector']]=sector_values.get(pos['sector'],0.)+pos['shares']*prices[t]
    for t,shortfall in sorted(need):
        target_spend=shortfall*scale
        p=tar[t];price=prices[t]*(1+SLIPPAGE)
        current=holdings.get(t,{}).get('shares',0)*prices[t]
        # Caps constrain actual holdings, not merely target weights.
        issuer_room=max(0,.15*nav-current)
        sector_room=max(0,.30*nav-sector_values.get(p['sector'],0.))
        exposure_room=max(0,target_exposure-gross_exposure)
        notional_room=min(issuer_room,sector_room,exposure_room)
        if notional_room<=LOT_EPS:continue
        target_spend=min(target_spend,notional_room*(1+SLIPPAGE)+FEE,
                         max(0,cash-.01*nav))
        if target_spend<5.:continue
        shares=(target_spend-FEE)/price
        if shares<=0:continue
        spend=shares*price+FEE
        if cash-spend<0.01*nav-1e-6:raise PaperBlocked('cash reserve violation')
        old_p=holdings.get(t)
        purchase={'entry_date':str(execution),'shares':shares,
                  'cost_basis_eur_per_share':spend/shares}
        if old_p:
            ensure_lots(old_p).append(purchase)
            sync_position(old_p)
        else:
            holdings[t]={'shares':shares,'cost_basis_eur_per_share':spend/shares,
                         'first_entry_date':str(execution),'lots':[purchase],
                         'market_symbol':p['market_symbol'],
                         'sector':p['sector'],'region':p['region']}
        cash-=spend
        amount=shares*prices[t]
        gross_exposure+=amount
        sector_values[p['sector']]=sector_values.get(p['sector'],0.)+amount
        orders.append({'session':str(execution),'action':'BUY','ticker':t,'market_symbol':p['market_symbol'],
                       'shares':round(shares,10),'reference_open_eur':prices[t],
                       'execution_price_eur':round(price,10),'fee_eur':FEE,
                       'tax_estimate_eur':0.,'cash_change_eur':round(-spend,6),
                       'signal_sha256':sig,'mode':'PAPER'})
    if len(holdings)>10 or cash<0.01*nav-1e-5:raise PaperBlocked('portfolio size or cash reserve')
    if execution_price_kind=='close':
        for order in orders:
            order['reference_close_eur']=order.pop('reference_open_eur')
            order['execution_price_kind']='close'
    old['cash_eur']=cash
    old['last_execution_session']=str(execution)
    old['orders'].extend(orders)
    old['decisions'].append({'decision_session':str(decision),'execution_session':str(execution),
                             'signal_sha256':sig,'orders':len(orders)})
    new_nav=cash+sum(p['shares']*prices[t] for t,p in holdings.items())
    if new_nav<=0:raise PaperBlocked('bad posttrade NAV')
    receipt={'execution_session':str(execution),'nav_at_execution_eur':round(new_nav,2),
             'execution_price_kind':execution_price_kind,'cash_eur':round(cash,2),
             'orders_count':len(orders),'order_ids_sha256':canonical_sha(orders),
             'signal_sha256':sig,'orders':orders}
    if execution_price_kind=='open':
        receipt['nav_at_open_eur']=round(new_nav,2)
    return old,receipt

def mark(state,close_quotes,session):
    st=validate_state(state)
    if any(t not in close_quotes for t in st['holdings']):raise PaperBlocked('incomplete mark prices')
    nav=st['cash_eur']+sum(x['shares']*finite(close_quotes[t],t,0.0000001) for t,x in st['holdings'].items())
    return {'session':str(session),'nav_eur':round(nav,2),'cash_eur':round(st['cash_eur'],2),
            'positions':len(st['holdings']),'return_pct':round(100*(nav/st['initial_cash_eur']-1),5)}
