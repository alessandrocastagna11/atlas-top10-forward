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

def validate_state(st):
    if st.get('schema_version')!=1 or st.get('engine_id')!=ENGINE or st.get('mode')!='PAPER':raise PaperBlocked('state identity or mode mismatch')
    if st.get('real_money_authorized') is not False:raise PaperBlocked('real-money flags forbidden')
    finite(st.get('cash_eur'),'cash',0)
    h=st.get('holdings')
    if not isinstance(h,dict) or len(h)>10:raise PaperBlocked('invalid holdings')
    for ticker,pos in h.items():
        if not ticker or not pos.get('market_symbol'):raise PaperBlocked('unmapped holdings symbol')
        finite(pos.get('shares'),'shares',0)
        finite(pos.get('cost_basis_eur_per_share'),'cost basis',0)
        dt.date.fromisoformat(pos['first_entry_date'])
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
    # Calculate all trades before modifying persistent state. Existing holdings protected for 35 calendar days.
    sells=[]
    for t,pos in holdings.items():
        current=pos['shares']*prices[t]; desired=estimated_target.get(t,0)
        excess=current-desired
        if excess<=max(nav*rebalance_fraction,3.):continue
        held=(execution-dt.date.fromisoformat(pos['first_entry_date'])).days
        if held<35:continue  # Preserve locked holdings without blocking other valid orders.
        shares=min(pos['shares'],excess/prices[t])
        if shares*prices[t]>3.:sells.append((t,shares))
    orders=[]
    for t,shares in sorted(sells):
        p=holdings[t];price=prices[t];gross=shares*price*(1-SLIPPAGE)
        cost=shares*p['cost_basis_eur_per_share'];gain=max(0,gross-cost-FEE)
        taxes=gain*TAX
        proceeds=gross-FEE-taxes
        if proceeds<=0:raise PaperBlocked('sell after fees has negative proceeds')
        cash+=proceeds;p['shares']-=shares
        orders.append({'session':str(execution),'action':'SELL','ticker':t,'market_symbol':p['market_symbol'],
                       'shares':round(shares,10),'reference_open_eur':price,'execution_price_eur':round(price*(1-SLIPPAGE),10),
                       'fee_eur':FEE,'tax_estimate_eur':round(taxes,6),'cash_change_eur':round(proceeds,6),
                       'signal_sha256':sig,'mode':'PAPER'})
        if p['shares']<1e-8:del holdings[t]
    # Buy toward target, enforcing 1% cash reserve, max 10 names and 35-day holding on future exits.
    need=[]
    for t,p in tar.items():
        cur=holdings.get(t,{}).get('shares',0)*prices[t]
        difference=estimated_target[t]-cur
        if difference>max(nav*rebalance_fraction,3.):need.append((t,difference))
    # Locked holdings still occupy slots: select only purchases that fit.
    free_slots=max(0,10-len(holdings))
    eligible=[]
    for t,difference in need:
        if t in holdings:
            eligible.append((t,difference))
        elif free_slots>0:
            eligible.append((t,difference))
            free_slots-=1
    need=eligible
    required=sum(v for _,v in need)
    free=max(0,cash-0.01*nav-len(need)*FEE)
    scale=min(1.,free/required) if required>0 else 0
    for t,shortfall in sorted(need):
        target_spend=shortfall*scale
        if target_spend<5.:continue
        p=tar[t];price=prices[t]*(1+SLIPPAGE)
        shares=(target_spend-FEE)/price
        if shares<=0:continue
        spend=shares*price+FEE
        if cash-spend<0.01*nav-1e-6:raise PaperBlocked('cash reserve violation')
        old_p=holdings.get(t)
        if old_p:
            total_shares=old_p['shares']+shares
            old_p['cost_basis_eur_per_share']=(old_p['cost_basis_eur_per_share']*old_p['shares']+spend)/total_shares
            old_p['shares']=total_shares
        else:
            holdings[t]={'shares':shares,'cost_basis_eur_per_share':spend/shares,
                         'first_entry_date':str(execution),'market_symbol':p['market_symbol'],
                         'sector':p['sector'],'region':p['region']}
        cash-=spend
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
