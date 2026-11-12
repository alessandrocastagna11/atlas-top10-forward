#!/usr/bin/env python3
"""Atlas ETF PAC four-sleeve prospective paper restart.

Not an identical replay of frozen ATLAS V2.3 Stress Filter. No broker access.
Signals from completed Xetra closes; fills at the NEXT observed Xetra OPEN only
when the decision had been recorded before that opening. Never backdate orders.
"""
from __future__ import annotations
import copy,csv,datetime as dt,hashlib,json,math,statistics,sys,urllib.parse,urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent
CONFIG=ROOT/'config.json'
STATE=ROOT/'state.json'
REPORT=ROOT/'reports/latest_status.json'
BERLIN=ZoneInfo('Europe/Berlin')
ROME=ZoneInfo('Europe/Rome')

def save(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(obj,indent=2,sort_keys=True,ensure_ascii=False)+'\n',encoding='utf-8')
    tmp.replace(path)

def date(s):return dt.date.fromisoformat(str(s))
def sha(x):return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def num(x):
    v=float(x)
    if not math.isfinite(v) or v<=0:raise ValueError('Nonpositive/nonfinite quotation')
    return v

class NoData(ValueError):pass

def yahoo(symbol,start,end):
    a=int(dt.datetime.combine(start,dt.time.min,dt.timezone.utc).timestamp())-86400
    z=int(dt.datetime.combine(end+dt.timedelta(days=2),dt.time.min,dt.timezone.utc).timestamp())
    url=('https://query1.finance.yahoo.com/v8/finance/chart/'
         +urllib.parse.quote(symbol,safe='')+'?'
         +urllib.parse.urlencode({'period1':a,'period2':z,'interval':'1d','events':'div,splits'}))
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 AtlasETF-PAC-PAPER/1.0'})
    try:
        with urllib.request.urlopen(req,timeout=24) as reply:raw=reply.read(7_000_000)
        data=json.loads(raw)['chart']['result'][0]
        meta=data['meta']
        if meta.get('currency','').upper()!='EUR':
            raise NoData('EUR-listed security expected: '+symbol)
        tz=ZoneInfo(meta['exchangeTimezoneName'])
        q=data['indicators']['quote'][0];bars={}
        stamps=data.get('timestamp') or []
        for i,t in enumerate(stamps):
            try:
                op=num(q['open'][i]);cl=num(q['close'][i])
                day=dt.datetime.fromtimestamp(t,dt.timezone.utc).astimezone(tz).date()
                if start<=day<=end:bars[day.isoformat()]={'open':op,'close':cl}
            except (IndexError,ValueError,TypeError):continue
        if len(bars)<215:raise NoData(f'Insufficient EUR OHLC history {symbol}: {len(bars)}')
        return {'bars':bars,'currency':'EUR','feed':'YAHOO_DAILY_UNOFFICIAL',
                'retrieved_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
                'sha256':hashlib.sha256(raw).hexdigest()}
    except (OSError,KeyError,IndexError,ValueError,TypeError,json.JSONDecodeError) as exc:
        if isinstance(exc,NoData):raise
        raise NoData('Provider unavailable '+symbol+': '+str(exc)[:120]) from exc

def safe_cutoff(now):
    current=now.astimezone(ROME)
    return current.date() if current.time()>=dt.time(19,0) else current.date()-dt.timedelta(days=1)

def common_day(data,through):
    days=None
    for obj in data.values():
        setdays={d for d in obj['bars'] if d<=str(through)}
        days=setdays if days is None else days&setdays
    if not days:raise NoData('No shared EUR exchange-local daily close')
    d=max(days)
    if (through-date(d)).days>4:raise NoData('Stale shared Xetra close '+d)
    return d

def signal(cfg,state,world_bars,session):
    # Only completed preceding months, not the partially completed signal month.
    latest={}
    for d in sorted(world_bars):
        if d<=session and d[:7]<session[:7]:latest[d[:7]]=(d,num(world_bars[d]['close']))
    monthly=[x for _,x in sorted(latest.items())]
    if len(monthly)<10:raise NoData('MONTHLY_MA10_HISTORY_MISSING')
    closing=monthly[-1][1]
    ma=statistics.mean(x[1] for x in monthly[-10:])
    previous=state.get('last_risk_on')
    bull=closing>=ma if previous is None else previous
    if closing>ma*(1+cfg['monthly_hysteresis']):bull=True
    elif closing<ma*(1-cfg['monthly_hysteresis']):bull=False
    observed=[(d,v['close']) for d,v in sorted(world_bars.items()) if d<=session]
    if len(observed)<11:raise NoData('FAST10_HISTORY_MISSING')
    ten=num(observed[-1][1])/num(observed[-11][1])-1
    if ten<=cfg['fast10_drawdown_trigger']:level='CASH'
    elif bull:level='RISK_ON'
    else:level='DEFENSIVE'
    weights=cfg['weights']
    if level=='RISK_ON':target=dict(weights)
    elif level=='DEFENSIVE':
        target={'WORLD':weights['WORLD']*cfg['defensive_global_core_fraction'],
                'NASDAQ':0.,'EM':0.,'GOLD':weights['GOLD']}
    else:target={'WORLD':0.,'NASDAQ':0.,'EM':0.,'GOLD':weights['GOLD']}
    return {'signal_session':session,'signal_month':monthly[-1][0][:7],
            'world_monthly_close':round(closing,6),'world_ma10':round(ma,6),
            'monthly_regime':bull,'fast10_pct':round(ten*100,4),
            'risk_level':level,'target_weights':target,
            'method':'RESEARCH_RECONSTRUCTION_MONTHLY_MA10_FAST10_NOT_FROZEN_V23_STRESS'}

def current_values(st, closes):
    return {k:float(p['shares'])*num(closes[k]) for k,p in st['positions'].items()}

def plan(cfg,st,sig,closes,month):
    vals=current_values(st,closes)
    nav=st['cash_eur']+sum(vals.values())-st.get('stamp_accrued_eur',0)
    used=sum(x['execution_session'][:7]==month for x in st['orders'])
    if used>=cfg['max_total_orders_per_month']:return [],['MONTHLY_ORDER_CAP_REACHED']
    cap=cfg['max_total_orders_per_month']-used
    targets={k:nav*float(w) for k,w in sig['target_weights'].items()}
    sells=[];blocked=[]
    for k,value in vals.items():
        surplus=value-targets.get(k,0)
        if surplus<=max(5,0.03*nav):continue
        bought=date(st['positions'][k]['last_buy_session'])
        age=(date(sig['signal_session'])-bought).days
        if age<cfg['min_hold_days_before_sell']:
            blocked.append('MIN_35_DAY_HOLD_'+k+'_'+str(age))
            continue
        sells.append({'asset':k,'side':'SELL','budget_eur':round(surplus,2),
                      'reason':'PRECOMMITTED_RISK_REDUCTION','date':sig['signal_session']})
    # Safety first: pending risk reductions take precedence over buying.
    sells=sorted(sells,key=lambda x:-x['budget_eur'])[:cap]
    cap-=len(sells)
    if cap<=0:return sells,blocked
    available=st['cash_eur']   # Do not assume unsettled sales fund today's buys.
    candidates=[]
    for k,value in [(k,vals.get(k,0)) for k in cfg['weights']]:
        short=max(0,targets.get(k,0)-value)
        if short>max(3,0.003*nav):candidates.append((short,k))
    # Prefer largest deficit; tie-break permanently dedicated gold.
    candidates.sort(key=lambda x:(-x[0],0 if x[1]=='GOLD' else 1,x[1]))
    budget=available
    buys=[]
    for shortage,k in candidates[:cap]:
        cost=min(shortage,budget)
        if cost<3:continue
        buys.append({'asset':k,'side':'BUY','budget_eur':round(cost,2),
                     'reason':'PAC_UNDERWEIGHT_DEFICIT_FIRST','date':sig['signal_session']})
        budget-=cost
    return sells+buys,blocked

def fill(cfg,st,planned,opens,session):
    out=[];count=sum(x['execution_session'][:7]==session[:7] for x in st['orders'])
    for task in sorted(planned,key=lambda x:x['side']!='SELL'):
        if count>=cfg['max_total_orders_per_month']:break
        k=task['asset'];side=task['side'];px=num(opens[k])
        slip=cfg['slippage_per_side']
        if side=='BUY':
            budget=min(task['budget_eur'],st['cash_eur'])
            if budget<=2:continue
            units=budget/(px*(1+slip))
            if units<=0:continue
            p=st['positions'].setdefault(k,{'shares':0.,'cost_basis_eur':0.,
                                          'last_buy_session':session})
            p['shares']+=units
            p['cost_basis_eur']+=budget
            p['last_buy_session']=session
            st['cash_eur']-=budget
            fee=0.;tax=0.;amount=budget
        else:
            p=st['positions'].get(k)
            if not p:continue
            age=(date(session)-date(p['last_buy_session'])).days
            if age<cfg['min_hold_days_before_sell']:
                st['blocked_actions'].append({'session':session,'reason':'HOLD_35_DAYS',
                                               'asset':k,'age_days':age})
                continue
            units=min(p['shares'],task['budget_eur']/px)
            fee=cfg['strategic_order_fee_eur']
            gross=units*px*(1-slip)
            proceeds=gross-fee
            if proceeds<=0:continue
            basis=p['cost_basis_eur']*(units/p['shares'])
            tax=max(0.,proceeds-basis)*cfg['capital_gain_tax_proxy']
            st['cash_eur']+=proceeds-tax
            p['cost_basis_eur']-=basis
            p['shares']-=units
            if p['shares']<1e-9:del st['positions'][k]
            amount=proceeds
        count+=1
        out.append({'execution_session':session,'asset':k,'symbol':cfg['tickers'][k],
                    'side':side,'shares':round(units,10),'price_open_eur':px,
                    'slippage_rate':slip,'fee_eur':fee,'tax_proxy_eur':round(tax,4),
                    'gross_cash_eur':round(amount,4),'decision_session':task['date'],
                    'reason':task['reason'],'mode':'VIRTUAL_ONLY'})
    st['orders'].extend(out)
    if st['cash_eur']<-1e-6:raise ValueError('Negative cash after paper fills')
    return out

def process(cfg,st,session,data,now):
    if session<=st['last_processed_session']:return st,0
    if session<cfg['first_possible_decision_session']:
        raise ValueError('Session before precommitted activation')
    x=copy.deepcopy(st)
    last=date(x['last_processed_session'])
    days=(date(session)-last).days
    if days<0:raise ValueError('Backdated step')
    # Accrue estimated stamp on preceding holdings only. It is not a broker tax bill.
    earlier=float(x['nav'][-1]['holdings_eur'])
    x['stamp_accrued_eur']+=earlier*cfg['annual_stamp_proxy']*days/365.25
    contribution=0.
    if session[:7]>=cfg['monthly_contribution_first_month'] and session[:7]>x['last_deposit_month']:
        contribution=cfg['monthly_contribution_eur']
        x['cash_eur']+=contribution
        x['last_deposit_month']=session[:7]
        x['contributions'].append({'date':session,'amount_eur':contribution,
                                   'mode':'VIRTUAL_PAC_CONTRIBUTION'})
    pending=x.get('pending')
    executed=[]
    if pending is not None and pending['decision_session']<session:
        recorded=dt.datetime.fromisoformat(pending['created_at_utc'])
        if recorded.tzinfo is None:raise ValueError('Undated prior decision')
        earliest_open=dt.datetime.combine(date(session),dt.time(9,0),
                                          tzinfo=BERLIN).astimezone(dt.timezone.utc)
        # A delayed data feed can report a session that predates the recorded
        # signal. Never use that session's opening price to fill the signal.
        # Advance the valuation, expire the old instruction, and create a fresh
        # decision from the latest verified close instead of permanently
        # crashing the daily workflow.
        if recorded.astimezone(dt.timezone.utc)>=earliest_open:
            x['blocked_actions'].append({'session':session,
                                         'reason':'DEFERRED_NO_PREOPEN_SIGNAL_PROVENANCE',
                                         'decision_session':pending['decision_session']})
            # Keep the ORIGINAL immutable precommit timestamp: rolling it
            # forward on each lagged quote would starve execution forever.
        # Only an observed session close and a verifiably pre-open decision
        # authorize a paper open fill. Do not backfill old sessions.
        elif (now.astimezone(ROME).date()-date(session)).days>1:
            x['blocked_actions'].append({'session':session,'reason':'EXPIRED_SIGNAL_NO_BACKFILL'})
            x['pending']=None
        else:
            opens={k:num(data[symbol]['bars'][session]['open'])
                   for k,symbol in cfg['tickers'].items()}
            executed=fill(cfg,x,pending['proposed_orders'],opens,session)
            x['pending']=None
    closes={k:num(data[symbol]['bars'][session]['close'])
            for k,symbol in cfg['tickers'].items()}
    vals=current_values(x,closes)
    invested=sum(vals.values())
    net=x['cash_eur']+invested-x['stamp_accrued_eur']
    prior=x['nav'][-1]
    prior_index=float(prior.get('twr_index',1))
    prev_nav=float(prior['nav_eur'])
    # Deposits must not create fake investment returns.
    daily_return=(net-contribution)/prev_nav-1 if prev_nav else 0.
    twr=prior_index*(1+daily_return)
    peak=max(twr,max(float(row.get('twr_index',1)) for row in x['nav']))
    dd=100*(twr/peak-1)
    x['nav'].append({'date':session,'nav_eur':round(net,4),'cash_eur':round(x['cash_eur'],4),
                    'holdings_eur':round(invested,4),'contribution_eur':contribution,
                    'twr_index':round(twr,9),'twr_day_pct':round(100*daily_return,5),
                    'drawdown_pct':round(dd,5),'source_status':'VERIFIED_COMMON_EXCHANGE_SESSION',
                    'market_session':True,'positions':len(x['positions'])})
    s=signal(cfg,x,data[cfg['tickers']['WORLD']]['bars'],session)
    x['last_risk_on']=s['monthly_regime']
    x['signal_history'].append(s)
    # Do not overwrite a previously recorded pending instruction just
    # because the source provider is one or two sessions behind.
    if x.get('pending') is None:
        proposed,blocked=plan(cfg,x,s,closes,session[:7])
        x['blocked_actions'].extend({'session':session,'reason':z} for z in blocked)
    else:
        proposed=[]
    if proposed:
        x['pending']={'decision_session':session,
             'created_at_utc':now.astimezone(dt.timezone.utc).isoformat(),
             'proposed_orders':proposed,'input_quote_digest':sha({k:closes[k] for k in closes}),
             'policy':'NEXT_VERIFIED_COMMON_SESSION_OPEN_PAPER'}
    x['last_processed_session']=session
    return x,len(executed)

def report(cfg,st,asof,fetch_status,now):
    nav=st['nav'][-1];hist=st['nav']
    peak=1.;dd=0.
    for v in hist:
        peak=max(peak,float(v.get('twr_index',1)))
        dd=min(dd,100*(float(v.get('twr_index',1))/peak-1))
    return {'checked_at_utc':now.astimezone(dt.timezone.utc).isoformat(),
            'mode':cfg['mode'],'model':cfg['model'],'historical_frozen_v23_parity':'NOT_CLAIMED',
            'status':fetch_status,'asof_requested':str(asof),
            'last_nav_session':nav['date'],'nav_eur':nav['nav_eur'],
            'cash_eur':nav['cash_eur'],'invested_eur':nav['holdings_eur'],
            'cumulative_twr_pct':round(100*(float(nav.get('twr_index',1))-1),5),
            'latest_day_pct':nav.get('twr_day_pct',0),
            'max_drawdown_pct':round(dd,5),'orders_total':len(st['orders']),
            'last_orders':st['orders'][-6:],'monthly_cash_contributions':len(st['contributions']),
            'contributed_eur':round(cfg['initial_cash_eur']+
                                   sum(x['amount_eur'] for x in st['contributions']),2),
            'positions':{k:{'units':round(p['shares'],8),'last_buy_session':p['last_buy_session']}
                         for k,p in st['positions'].items()},
            'latest_signal':st['signal_history'][-1] if st['signal_history'] else None,
            'pending_orders':st.get('pending'),
            'blocked_actions':st['blocked_actions'][-6:],
            'real_broker_orders':0,'notes':'Research shadow; not identical to original V2.3 with macro stress and bond defense.'}

def main():
    now=dt.datetime.now(dt.timezone.utc)
    cfg=json.loads(CONFIG.read_text(encoding='utf-8'))
    state=json.loads(STATE.read_text(encoding='utf-8'))
    if state['mode']!=cfg['mode'] or state['model']!=cfg['model']:
        raise ValueError('Model identity mismatch; ledger unchanged')
    if len(cfg['tickers'])!=4 or abs(sum(cfg['weights'].values())-1)>1e-9:
        raise ValueError('Invalid frozen ETF four-sleeve weights')
    cutoff=safe_cutoff(now)
    try:
        data={sym:yahoo(sym,dt.date(2023,1,1),cutoff) for sym in cfg['tickers'].values()}
        asof=common_day(data,cutoff)
        if asof<=state['last_processed_session']:
            status='WAITING_NEW_XETRA_CLOSE'
        else:
            state,executed=process(cfg,state,asof,data,now)
            save(STATE,state)
            status='PAPER_ORDERS_EXECUTED' if executed else 'SIGNAL_PENDING_NEXT_VERIFIED_OPEN'
        metadata={'symbols':{sym:{'source_sha256':q['sha256'],'latest_bar':max(q['bars'])}
                             for sym,q in data.items()},'session':asof}
        save(ROOT/'reports/latest_quote_audit.json',metadata)
    except (NoData,OSError,KeyError,ValueError,TypeError) as ex:
        status='PENDING_DATA: '+str(ex)[:180]
    out=report(cfg,state,cutoff,status,now)
    save(REPORT,out)
    print(json.dumps({'status':out['status'],'NAV':out['nav_eur'],'orders':out['orders_total'],
                      'last_session':out['last_nav_session'],'pending':bool(out['pending_orders'])}))
    return 0

if __name__=='__main__':raise SystemExit(main())
