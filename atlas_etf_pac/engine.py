#!/usr/bin/env python3
"""Atlas ETF PAC: V2.3 ORIGINAL LIVE signal + prospective virtual execution.

Replaces the former 4-ETF reconstruction. The original 2026-10-01 live
state-machine lives unmodified in v23_original/atlas_v23_live.py. Signals
are based on completed EUR ETF closes and previous completed macro months.
Paper fills: at the next eligible completed session CLOSE, never on the
signal session and never from a decision written after the target close.
No broker access, no retroactive positions, no forced 35-day hold or monthly
order cap (those restrictions are not in original V2.3).
"""
from __future__ import annotations
import copy,csv,datetime as dt,hashlib,json,math,os,sys,urllib.parse,zipfile
from pathlib import Path
from zoneinfo import ZoneInfo
import exchange_calendars as xcals
import pandas as pd

ROOT=Path(__file__).resolve().parent
CONFIG=ROOT/'config.json'
STATE=ROOT/'state.json'
REPORT=ROOT/'reports/latest_status.json'
AUDIT=ROOT/'reports/latest_quote_audit.json'
ROME=ZoneInfo('Europe/Rome');BERLIN=ZoneInfo('Europe/Berlin')
from v23_original import atlas_v23_live as v23

MODEL='ATLAS_ETF_PAC_V23_ORIGINAL_LIVE_V1'
MIN_CASH=2.0
ORIGINAL_LIVE_SHA='703e4ecc9509ec8278ecd39e008cdc12f53091cdf8c833e3f7aadc4032cb196e'

class InsufficientData(RuntimeError):pass

def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def save(path,obj):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp')
    temp.write_text(json.dumps(obj,ensure_ascii=False,indent=2,sort_keys=True,allow_nan=False)+'\n',encoding='utf-8')
    temp.replace(path)
def day(s):return dt.date.fromisoformat(s)
def stamp_today(now):return now.astimezone(ROME).date()
def listing_calendar(ticker):
    suffix=str(ticker).upper().rsplit('.',1)[-1]
    mapping={'DE':'XETR','MI':'XMIL'}
    if suffix not in mapping:raise ValueError('UNSUPPORTED_ETF_LISTING_CALENDAR_'+str(ticker))
    return xcals.get_calendar(mapping[suffix])

def completed_cutoff(now,ticker='EUNL.DE'):
    """Last scheduled local session after conservative 90-minute EOD buffer."""
    if now.tzinfo is None:raise ValueError('Timezone-aware timestamp required')
    cal=listing_calendar(ticker)
    local=now.astimezone(ROME).date()
    session=cal.date_to_session(pd.Timestamp(local),direction='previous')
    if now<cal.session_close(session).to_pydatetime()+dt.timedelta(minutes=90):
        session=cal.previous_session(session)
    return session.date()

def elapsed_market_sessions(last_observed,through,ticker='EUNL.DE'):
    start=day(last_observed)+dt.timedelta(days=1)
    stop=day(through)
    if stop<start:return 0
    return len(listing_calendar(ticker).sessions_in_range(pd.Timestamp(start),pd.Timestamp(stop)))

# Keep old helper name for existing clients; it now counts exchange sessions.
def elapsed_weekday_sessions(last_observed,through):
    return elapsed_market_sessions(last_observed,through)

def num(x):
    q=float(x)
    if not math.isfinite(q) or q<=0:raise InsufficientData('INVALID_PRICE')
    return q

def validate(cfg,state):
    p=ROOT/'v23_original/atlas_v23_live.py'
    if hashlib.sha256(p.read_bytes()).hexdigest()!=ORIGINAL_LIVE_SHA:
        raise RuntimeError('FROZEN_V23_LIVE_SOURCE_SHA256_MISMATCH')
    official=read(ROOT/'v23_original/live_config.json')
    # Guarantee that *every frozen live rule* remains unchanged in the adapter.
    for k,v in official.items():
        if cfg.get(k)!=v:
            raise RuntimeError('FROZEN_V23_PARAMETER_CHANGED_'+k)
    assert cfg['model']==state['model']==MODEL
    assert cfg['mode']==state['mode']=='PAPER_ONLY_ORIGINAL_V23'
    assert cfg['no_broker_execution'] is True
    assert cfg['execution_policy']=='NEXT_COMPLETED_SESSION_CLOSE_PRECOMMITTED_PAPER'
    assert cfg['min_hold_days_before_sell']==0
    assert cfg['max_total_orders_per_month'] is None
    assert cfg['initial_cash_eur']==1000 and cfg['monthly_contribution_eur']==250
    assert abs(cfg['core_fraction']-0.5)<1e-10
    assert abs(cfg['satellite_fraction']-0.5)<1e-10
    assert state['activation_date'] == cfg['activation_date']


def seed(cfg):
    cap=cfg['initial_cash_eur']
    return {'schema':2,'model':cfg['model'],'mode':cfg['mode'],
      'activation_date':cfg['activation_date'],'last_processed_session':None,
      'last_deposit_month':cfg['activation_date'][:7],
      'balances':{'CORE':{'units':0.,'basis_eur':0.,'cash_eur':cap/2},
                  'SATELLITE':{'units':0.,'basis_eur':0.,'cash_eur':cap/2},
                  'BOND':{'units':0.,'basis_eur':0.,'cash_eur':0.}},
      'orders':[],'contributions':[],'signal_history':[],'nav':[],
      'pending':None,'blocked_actions':[],'stamp_accrued_eur':0.,
      'last_quotes':{},'data_digest':None,'last_status':'AWAITING_FIRST_COMPLETED_CLOSE',
      'last_price_session':None}



def yahoo_daily_with_secondary_endpoint(cfg,ticker,now):
    """Retry another Yahoo chart host using observed EUR adjusted closes only."""
    rows=v23.yahoo_daily(ticker,cfg['history_start'],cfg)
    rows=sorted([dict(r) for r in rows],key=lambda x:x['date'])
    cutoff=completed_cutoff(now,ticker).isoformat()
    cal=listing_calendar(ticker)
    rows=[r for r in rows if r['date']<=cutoff and cal.is_session(pd.Timestamp(r['date']))]
    if not rows or elapsed_market_sessions(rows[-1]['date'],cutoff,ticker)==0:
        return rows,'YAHOO_QUERY1'
    last=rows[-1]['date']
    start=dt.datetime.combine(day(last)-dt.timedelta(days=10),dt.time.min,tzinfo=dt.timezone.utc)
    end=now.astimezone(dt.timezone.utc)+dt.timedelta(days=2)
    url=('https://query2.finance.yahoo.com/v8/finance/chart/'
         +urllib.parse.quote(ticker,safe='')
         +'?period1='+str(int(start.timestamp()))+'&period2='+str(int(end.timestamp()))
         +'&interval=1d&events=div%2Csplits&includeAdjustedClose=true')
    try:
        data=json.loads(v23.fetch_bytes(url,timeout=30).decode('utf-8'))['chart']['result'][0]
        if data.get('meta',{}).get('currency')!='EUR':
            raise ValueError('Unexpected Yahoo currency')
        indicator=data['indicators']
        adj=((indicator.get('adjclose') or [{}])[0].get('adjclose') or [])
        close=indicator['quote'][0].get('close') or []
        incoming={}
        for i,t in enumerate(data.get('timestamp') or []):
            x=adj[i] if i<len(adj) and adj[i] is not None else (close[i] if i<len(close) else None)
            if x is None or not math.isfinite(float(x)) or float(x)<=0:continue
            session=dt.datetime.fromtimestamp(t,tz=dt.timezone.utc).date().isoformat()
            if session<=cutoff and cal.is_session(pd.Timestamp(session)):incoming[session]=float(x)
        original={r['date']:float(r['close']) for r in rows}
        overlap=sorted(set(incoming)&set(original))
        if overlap:
            d=overlap[-1]
            if abs(incoming[d]/original[d]-1)>0.005:
                raise ValueError('Inconsistent adjusted close between Yahoo endpoints')
        added={k:v for k,v in incoming.items() if k>last}
        if not added:
            return rows,'YAHOO_BOTH_ENDPOINTS_NO_NEW_COMPLETED_CLOSE'
        merged=[{'date':k,'close':v} for k,v in sorted({**original,**added}.items())]
        cache=v23.CACHE/('yahoo_'+v23.safe_ticker_name(ticker)+'.csv')
        tmp=cache.with_name(cache.name+'.tmp')
        with tmp.open('w',encoding='utf-8',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=['date','close'])
            writer.writeheader();writer.writerows(merged)
        tmp.replace(cache)
        return merged,'YAHOO_QUERY2_OBSERVED_CLOSE'
    except (OSError,ValueError,KeyError,IndexError,TypeError,RuntimeError) as exc:
        print('WARNING: Secondary Yahoo unavailable for',ticker,str(exc)[:180])
        return rows,'YAHOO_QUERY1_SECONDARY_UNAVAILABLE'



def refresh_macro_independently(now):
    """Refresh each *official* source separately without losing other fresh inputs.

    Unlike the frozen adapter's all-or-nothing download, one provider outage does
    not roll back the entire monthly bundle. Missing months remain missing; never
    interpolate RF or six portfolios or assign a publication date retroactively.
    The frozen V2.3 StressModel and its parameters are untouched.
    """
    cache_path=v23.CACHE/'macro_v23.json'
    cached=read(cache_path) if cache_path.exists() else {}
    refresh={}
    result={}
    allowed=v23.previous_month_key(now.astimezone(ROME).date().isoformat())
    provider_methods={
        'aaa':lambda:v23.fred_monthly('AAA'),
        'baa':lambda:v23.fred_monthly('BAA'),
        'gs10':lambda:v23.fred_monthly('GS10'),
        'rf_annualized':v23.french_daily_rf_monthly,
        'six':v23.french_six_monthly,
    }
    for name,fetcher in provider_methods.items():
        previous=cached.get(name,{})
        if not isinstance(previous,dict):previous={}
        usable={k:v for k,v in previous.items() if isinstance(k,str) and
                len(k)==7 and k[4]=='-' and k<=allowed}
        online=False;error=None
        try:
            payload=fetcher()
            if not isinstance(payload,dict) or not payload:
                raise ValueError('Provider returned empty series')
            clean={}
            for key,value in payload.items():
                if not (isinstance(key,str) and len(key)==7 and key[4]=='-' and
                        key<=allowed):
                    continue
                try:
                    if name=='six':
                        if len(value)!=6:continue
                        converted=[float(x) for x in value]
                        if not all(math.isfinite(x) for x in converted):continue
                    else:
                        converted=float(value)
                        if not math.isfinite(converted):continue
                except (ValueError,TypeError):continue
                clean[key]=converted
            if not clean:raise ValueError('Provider produced no completed-month observations')
            # Reject truncated downloads that would silently roll back history.
            if usable and max(clean)<max(usable):
                raise ValueError('Provider history is older than cached data')
            usable.update(clean)
            online=True
        except Exception as exc:
            error=(type(exc).__name__+': '+str(exc))[:180]
            print('WARNING: macro provider',name,'unavailable:',error)
        result[name]=usable
        last=max(usable) if usable else None
        refresh[name]={'last_month':last,'online_checked':online,
                       'status':('ONLINE_LATEST_MONTH_AVAILABLE' if last==allowed else
                                 'ONLINE_UPSTREAM_BEHIND_REQUESTED_MONTH') if online else
                                 ('CACHED_PROVIDER_ERROR' if last else 'UNAVAILABLE'),
                       'error':error}
    if any(not result[key] for key in provider_methods):
        raise InsufficientData('MISSING_REQUIRED_MACRO_SERIES_'+','.join(
            key for key in provider_methods if not result[key]))
    previous_meta=cached.get('source_audit',{})
    result['source_audit']={
        'checked_at_utc':now.astimezone(dt.timezone.utc).isoformat(),
        'requested_completed_month':allowed,
        'providers':refresh,
        'last_full_download_utc':cached.get('downloaded_utc'),
        'source_urls':{
            'rf_annualized':'Kenneth French F-F_Research_Data_Factors_daily_CSV.zip',
            'six':'Kenneth French 6_Portfolios_2x3_CSV.zip',
            'aaa':'FRED AAA','baa':'FRED BAA','gs10':'FRED GS10'},
    }
    # Keep the original cache shape for v23.StressModel and the existing reports.
    result['downloaded_utc']=result['source_audit']['checked_at_utc'] if all(
        x['online_checked'] for x in refresh.values()) else cached.get('downloaded_utc')
    save(cache_path,result)
    return result,refresh


def historical_feeds(cfg,now):
    """Original Yahoo/FRED/French source functions. Cached copies persist.
    Never treat a cached close for a prior day as today's quote.
    """
    cutoff=completed_cutoff(now,cfg['signal_ticker']).isoformat()
    eq,equity_source=yahoo_daily_with_secondary_endpoint(cfg,cfg['signal_ticker'],now)
    eq=sorted([dict(r) for r in eq if r['date']<=cutoff],key=lambda x:x['date'])
    if len(eq)<160:raise InsufficientData('EQUITY_HISTORY_UNDER_160_SESSIONS')
    last=eq[-1]['date'];gap=(day(cutoff)-day(last)).days
    if elapsed_market_sessions(last,cutoff,cfg['signal_ticker'])>3:raise InsufficientData('STALE_EQUITY_CLOSE_'+last)
    bond=[];bond_problem=None;bond_source=None
    try:
        bond,bond_source=yahoo_daily_with_secondary_endpoint(cfg,cfg['defensive_ticker'],now)
        bond=sorted([dict(r) for r in bond if r['date']<=last],key=lambda x:x['date'])
    except Exception as exc:bond_problem=str(exc)[:160]
    if bond:
        recent_bond=elapsed_market_sessions(bond[-1]['date'],min(last,completed_cutoff(now,cfg['defensive_ticker']).isoformat()),cfg['defensive_ticker'])<=3
    else:recent_bond=False
    if not recent_bond:bond_problem='STALE_OR_MISSING_BOND_'+(bond[-1]['date'] if bond else 'NONE')
    macro_problem=None;macro_refresh={}
    try:
        macro,macro_refresh=refresh_macro_independently(now)
    except Exception as exc:
        macro=None;macro_problem=str(exc)[:160]
    return {'equity':eq,'bond':bond,'bond_usable':recent_bond,
            'bond_problem':bond_problem,'macro':macro,'macro_problem':macro_problem,
            'macro_refresh':macro_refresh,
            'cutoff':cutoff,'session':last,'quote_lag_days':gap,
            'quote_lag_sessions':elapsed_market_sessions(last,cutoff,cfg['signal_ticker']),
            'equity_quote_source':equity_source,'bond_quote_source':bond_source}


class CausalStress:
    """Real previous-month macro if complete, explicit frozen-score carry if not.
    Never substitute silently; the missing series and score origin are audited.
    """
    def __init__(self,cfg,macro):
        self.cfg=cfg;self.macro=macro
        self.real=v23.StressModel(cfg,macro) if macro is not None else None
        with zipfile.ZipFile(ROOT/'v23_original/pinned_reference.zip') as z:
            raw=json.loads(z.read('data/stress_scores_precomputed.json'))
        y,m=map(int,raw['start'].split('-'));self.scores={}
        for v in raw['scores']:
            self.scores[f'{y:04d}-{m:02d}']=int(v)
            m+=1
            if m==13:y+=1;m=1
        self.last_month=max(self.scores)
        self.last_score=self.scores[self.last_month]
        self.quality_history={}
    def is_complete(self,month):
        if self.macro is None:return False
        q=self.macro
        return (month in q['aaa'] and month in q['baa'] and month in q['gs10'] and
                month in q['rf_annualized'] and
                all(v23.add_months(month,-i) in q['six'] for i in range(3)))
    def regime(self,date):
        month=v23.previous_month_key(date)
        if self.real and self.is_complete(month):
            x=self.real.regime(date)
            x['source']='OBSERVED_COMPLETED_MONTH'
            x['missing_inputs']=[]
            self.quality_history[month]='OBSERVED_COMPLETED_MONTH'
            return x
        missing=[]
        if self.macro is None:missing=['FRED_AAA','FRED_BAA','FRED_GS10','FF_RF','FF_BREADTH_3M']
        else:
            for key,label in [('aaa','FRED_AAA'),('baa','FRED_BAA'),('gs10','FRED_GS10'),('rf_annualized','FF_RF')]:
                if month not in self.macro[key]:missing.append(label)
            if not all(v23.add_months(month,-i) in self.macro['six'] for i in range(3)):
                missing.append('FF_BREADTH_3M')
        # Prefer the MOST RECENT fully observed historical monthly macro state,
        # not a stale score carried unchanged since November 2025. Keep the
        # origin explicit; this is NOT the fully observed requested month.
        observed_month=None;carried=None
        if self.real:
            candidates=[k for k in self.macro['aaa'] if k<=month and self.is_complete(k)]
            if candidates:
                observed_month=max(candidates)
                future=v23.add_months(observed_month,1)
                synthetic_decision_date=future+'-01'
                carried=self.real.regime(synthetic_decision_date)
        if carried is not None:
            carried['month']=month
            carried['source']='CARRIED_OBSERVED_'+observed_month
            carried['observed_macro_month']=observed_month
            carried['missing_inputs']=missing
            self.quality_history[month]=carried['source']
            return carried
        score=self.scores.get(month,self.last_score)
        source='FROZEN_HISTORICAL_SCORE' if month in self.scores else 'CARRIED_FROZEN_'+self.last_month
        self.quality_history[month]=source
        return {'month':month,'score':score,'credit':None,'breadth':None,'rates':None,
                'credit_spread':None,'credit_p80':None,'credit_change_3m':None,
                'breadth_healthy':None,'term_spread':None,'term_p20':None,
                'missing_inputs':missing,'observed_macro_month':self.last_month,
                'source':source}


def estimate_defensive(feeds,cfg):
    bond=feeds['bond']
    if not feeds['bond_usable'] or len(bond)<127:
        return {'recommendation':'CASH','ticker':cfg['defensive_ticker'],
                'momentum_126':None,'source':'DATA_UNAVAILABLE_FALLBACK_TO_CASH'}
    close=[float(r['close']) for r in bond]
    momentum=v23.mom(close,len(close)-1,126)
    return {'recommendation':'BOND' if momentum>0 else 'CASH',
            'ticker':cfg['defensive_ticker'],'momentum_126':round(momentum,8),
            'source':'OBSERVED_BOND_ADJUSTED_CLOSE'}


def signal(feeds,cfg):
    stress=CausalStress(cfg,feeds['macro'])
    replay=v23.replay_strategy(feeds['equity'],cfg,stress)
    defensive=estimate_defensive(feeds,cfg)
    s=replay.latest
    out={'decision_session':s['date'],'data_mode':'OBSERVED_EQUITY_ETF_EOD',
         'signal_ticker':cfg['signal_ticker'],'equity_eod':s['close'],
         'state_label':s['state_label'],'core_in':s['core_in'],
         'satellite_in':s['satellite_in'],'bull':s['bull'],
         'market_drawdown_pct':round(s['market_drawdown']*100,4),
         'fast10_pct':round(s['r10']*100,4) if s['r10'] is not None else None,
         'pending_original_actions':s['pending_actions'],
         'target_equity_fraction':s['target_equity_fraction'],
         'stress':s['stress'],'defensive':defensive,
         'original_live_sha256':ORIGINAL_LIVE_SHA,
         'method':'ATLAS_V23_OCT_01_2026_ORIGINAL_LIVE_UNMODIFIED'}
    return out


def prices_for_session(feeds,session,has_bond_position=False):
    e=next((r['close'] for r in feeds['equity'] if r['date']==session),None)
    if e is None:raise InsufficientData('MISSING_EQUITY_SESSION_CLOSE_'+session)
    # Never execute or mark a bond holding against a previous session's quote.
    b=next((r['close'] for r in feeds['bond'] if r['date']==session),None) if feeds['bond_usable'] else None
    if has_bond_position and b is None:
        raise InsufficientData('MISSING_HELD_BOND_VALUATION')
    return {'EQUITY':num(e),'BOND':num(b) if b is not None else None}


def cash_total(st):return sum(float(x['cash_eur']) for x in st['balances'].values())
def value(st,price):
    b=st['balances']
    cash=cash_total(st)
    eq=sum(b[k]['units']*price['EQUITY'] for k in ('CORE','SATELLITE'))
    bond=b['BOND']['units']*(price['BOND'] or 0)
    return {'cash_eur':cash,'invested_eur':eq+bond,'equity_eur':eq,
            'bond_eur':bond,'nav_eur':cash+eq+bond-float(st['stamp_accrued_eur'])}


def trade(st,cfg,sleeve,side,kind,price,session,decision_session,reason):
    """Whole-sleeve virtual orders; no broker. Tax/fee/slippage applied on fill."""
    b=st['balances'][sleeve];px=num(price)
    if side=='BUY':
        cash=float(b['cash_eur'])
        fee=0 if kind=='PAC_ORDER' else cfg['strategy_fixed_fee_eur']
        if cash<=max(MIN_CASH,fee):return None
        gross=(cash-fee)/(1+cfg['slippage_each_side'])
        units=gross/px
        slip=gross*cfg['slippage_each_side']
        cost=gross+slip+fee
        b['cash_eur']=max(0,cash-cost)
        b['units']+=units;b['basis_eur']+=gross
        tax=0;amount=gross
    else:
        units=b['units']
        if units<=1e-12:return None
        fee=cfg['strategy_fixed_fee_eur']
        gross=units*px
        slip=gross*cfg['slippage_each_side']
        proceeds=gross-slip-fee
        tax=max(0.,proceeds-b['basis_eur'])*(cfg['tax_bond'] if sleeve=='BOND' else cfg['tax_equity'])
        b['cash_eur']+=proceeds-tax
        b['units']=0.;b['basis_eur']=0.
        amount=gross-slip
    if b['cash_eur']<-0.01:raise AssertionError('Negative cash')
    obj={'execution_session':session,'decision_session':decision_session,
         'sleeve':sleeve,'asset':cfg['defensive_ticker'] if sleeve=='BOND' else cfg['equity_ticker'],
         'side':side,'kind':kind,'units':round(units,10),
         'execution_close_eur':px,'fee_eur':round(fee,6),
         'slippage_eur':round(slip,6),'tax_proxy_eur':round(tax,6),
         'gross_eur':round(amount,6),'reason':reason,'mode':'VIRTUAL_ONLY'}
    st['orders'].append(obj)
    return obj


def plan(st,cfg,sig,defensive):
    p=sig['pending_original_actions'];b=st['balances']
    core=(sig['core_in'] and 'SELL_CORE_NEXT_SESSION' not in p) or 'BUY_CORE_NEXT_SESSION' in p
    sat=(sig['satellite_in'] and 'SELL_SATELLITE_NEXT_SESSION' not in p) or 'BUY_SATELLITE_NEXT_SESSION' in p
    acts=[]
    def add(sl,action,why,kind='STRATEGIC'):
        acts.append({'sleeve':sl,'side':action,'reason':why,'kind':kind})
    if not core:
        if b['CORE']['units']>1e-12:add('CORE','SELL','V23_CORE_EXIT')
        if defensive['recommendation']=='BOND' and b['BOND']['units']<1e-12:
            add('BOND','BUY','V23_DEFENSIVE_MOM126')
        elif b['BOND']['units']>1e-12 and defensive['recommendation']=='CASH':
            add('BOND','SELL','V23_DEFENSIVE_CASH')
    else:
        if b['BOND']['units']>1e-12:add('BOND','SELL','V23_CORE_REENTRY')
        if b['CORE']['cash_eur']+b['BOND']['cash_eur']>MIN_CASH or b['CORE']['units']<1e-12:
            add('CORE','BUY','V23_CORE_IN')
    if not sat:
        if b['SATELLITE']['units']>1e-12:add('SATELLITE','SELL','V23_SATELLITE_EXIT')
    elif b['SATELLITE']['cash_eur']>MIN_CASH or b['SATELLITE']['units']<1e-12:
        add('SATELLITE','BUY','V23_SATELLITE_IN')
    # No phantom transactions for sleeves which hold zero capital.
    return acts


def fill_pending(st,cfg,pending,price,session,now):
    if not pending:return []
    ds=pending['decision_session'];created=dt.datetime.fromisoformat(pending['created_at_utc'])
    if created.tzinfo is None:raise ValueError('Pending decision timestamp missing offset')
    if ds>=session:raise AssertionError('Cannot execute on decision session')
    # Quotes may arrive late: never invent an execution after the historical fact
    # if the order had not been precommitted by the market close being valued.
    cal=listing_calendar(cfg['equity_ticker'])
    if not cal.is_session(pd.Timestamp(session)):
        raise ValueError('NON_TRADING_EQUITY_SESSION_'+session)
    cutoff=cal.session_close(pd.Timestamp(session)).to_pydatetime()
    if created.astimezone(dt.timezone.utc)>=cutoff:
        st['blocked_actions'].append({'session':session,'reason':'ORDER_NOT_PRECOMMITTED_BEFORE_SESSION_CLOSE',
                                      'decision_session':ds})
        return []
    # Avoid reconstructing old paper fills if the data provider skipped sessions.
    if (stamp_today(now)-day(session)).days>1:
        st['blocked_actions'].append({'session':session,'reason':'STALE_SESSION_NO_RETROACTIVE_FILLS',
                                      'decision_session':ds})
        return []
    out=[]
    acts=list(pending['proposed_orders'])
    for task in [a for a in acts if a['side']=='SELL']+[a for a in acts if a['side']=='BUY']:
        sleeve=task['sleeve']
        listing=cfg['defensive_ticker'] if sleeve=='BOND' else cfg['equity_ticker']
        local_cal=listing_calendar(listing)
        if (not local_cal.is_session(pd.Timestamp(session)) or
            created>=local_cal.session_close(pd.Timestamp(session)).to_pydatetime() or
            now<local_cal.session_close(pd.Timestamp(session)).to_pydatetime()):
            st['blocked_actions'].append({'session':session,'reason':'LOCAL_SESSION_CLOSED_OR_NOT_PRECOMMITTED_'+sleeve})
            continue
        px=price['BOND' if sleeve=='BOND' else 'EQUITY']
        if px is None:
            st['blocked_actions'].append({'session':session,'reason':'MISSING_PRICE_FOR_'+sleeve})
            continue
        # No cash transfers if the intended asset has no observed same-day close.
        if task['side']=='BUY' and sleeve=='CORE' and st['balances']['BOND']['cash_eur']>0:
            st['balances']['CORE']['cash_eur']+=st['balances']['BOND']['cash_eur'];st['balances']['BOND']['cash_eur']=0.
        if task['side']=='BUY' and sleeve=='BOND':
            st['balances']['BOND']['cash_eur']+=st['balances']['CORE']['cash_eur'];st['balances']['CORE']['cash_eur']=0.
        # Initial and strategy switching use 1 EUR fee; subsequent regular
        # PAC purchases may be fee-free only when no exit/reentry is occurring.
        prior_kind='INITIAL' if len(st['orders'])==0 and task['side']=='BUY' else task['kind']
        if prior_kind=='STRATEGIC' and task['side']=='BUY' and task['reason'] in ('V23_CORE_IN','V23_SATELLITE_IN'):
            if task.get('pac_only',False):prior_kind='PAC_ORDER'
        traded=trade(st,cfg,sleeve,task['side'],prior_kind,px,session,ds,task['reason'])
        if traded:out.append(traded)
    return out


def accrue_deposit(st,cfg,session):
    if session[:7]<=st['last_deposit_month'] or session[:7]<cfg['monthly_contribution_first_month']:
        return 0.
    deposit=float(cfg['monthly_contribution_eur'])
    st['balances']['CORE']['cash_eur']+=deposit/2
    st['balances']['SATELLITE']['cash_eur']+=deposit/2
    st['last_deposit_month']=session[:7]
    st['contributions'].append({'date':session,'amount_eur':deposit,'mode':'VIRTUAL_PAC_CONTRIBUTION'})
    return deposit



def correct_cash_only_stamp(st,now):
    """One-off audit correction: security stamp applies to assets, not idle cash.

    Reprice historic cash-only NAV without modifying orders, dates or signals.
    """
    if st.get('orders') or not st.get('nav'):
        return False
    if any(float(x.get('units',0))!=0 for x in st['balances'].values()):
        return False
    if any(float(r.get('holdings_eur',0))!=0 for r in st['nav']):
        return False
    amount=float(st.get('stamp_accrued_eur',0))
    if amount<=1e-10:
        return False
    for row in st['nav']:
        row['nav_eur']=round(float(row['cash_eur']),5)
        row['twr_index']=1.
        row['twr_day_pct']=0.
        row['drawdown_pct']=0.
    st['stamp_accrued_eur']=0.
    st['accounting_adjustments']=st.get('accounting_adjustments',[])+[{
        'changed_at_utc':now.astimezone(dt.timezone.utc).isoformat(),
        'reason':'CORRECT_CASH_ONLY_SECURITY_STAMP_PROXY',
        'removed_incorrect_cash_stamp_eur':round(amount,6),
        'paper_orders_changed':0}]
    return True


def ledger_process(cfg,state,session,feeds,sig,now):
    st=copy.deepcopy(state)
    if st['last_processed_session'] is not None and session<=st['last_processed_session']:
        return st,0
    if session<cfg['activation_date']:
        raise ValueError('PRE_ACTIVATION_SESSION')
    # Do not fill a missed session based on future information. On first run,
    # value the latest verified close and plan initial orders for a later close.
    cal=listing_calendar(cfg['equity_ticker'])
    if not cal.is_session(pd.Timestamp(session)) or session>completed_cutoff(now,cfg['equity_ticker']).isoformat():
        raise InsufficientData('NOT_COMPLETED_EQUITY_EXCHANGE_SESSION_'+session)
    missing_sessions=elapsed_market_sessions(session,completed_cutoff(now,cfg['equity_ticker']).isoformat(),cfg['equity_ticker'])
    if missing_sessions>1:
        raise InsufficientData('OLD_DATA_NO_PROSPECTIVE_DECISION_'+session+
                               '_MISSING_WEEKDAYS_'+str(missing_sessions))
    price=prices_for_session(feeds,session,st['balances']['BOND']['units']>0)
    prior=st['nav'][-1] if st['nav'] else None
    previous_session=st['last_processed_session']
    if previous_session is not None:
        days=(day(session)-day(previous_session)).days
        taxable_securities=(float(prior.get('equity_eur',0))+float(prior.get('bond_eur',0))) if prior else 0.
        st['stamp_accrued_eur']+=max(taxable_securities,0)*cfg['annual_stamp_proxy']*days/365.25
    executed=fill_pending(st,cfg,st.get('pending'),price,session,now)
    st['pending']=None
    deposit=accrue_deposit(st,cfg,session)
    x=value(st,price)
    oldnav=prior['nav_eur'] if prior else cfg['initial_cash_eur']
    prev_index=prior.get('twr_index',1.) if prior else 1.
    twr=prev_index*(x['nav_eur']-deposit)/oldnav if oldnav else prev_index
    peak=max([1.]+[float(r.get('twr_index',1)) for r in st['nav']]+[twr])
    dd=100*(twr/peak-1)
    st['nav'].append({'date':session,'nav_eur':round(x['nav_eur'],5),
             'cash_eur':round(x['cash_eur'],5),'holdings_eur':round(x['invested_eur'],5),
             'equity_eur':round(x['equity_eur'],5),'bond_eur':round(x['bond_eur'],5),
             'contribution_eur':deposit,'twr_index':round(twr,10),
             'twr_day_pct':round((twr/prev_index-1)*100,6),
             'drawdown_pct':round(dd,6),'source_status':'OBSERVED_COMPLETED_EUR_ETF_CLOSE'})
    st['last_processed_session']=session
    st['last_price_session']=session
    st['last_quotes']=price
    st['last_status']='PAPER_ORDERS_EXECUTED' if executed else 'SIGNAL_PRECOMMITTED_FOR_FUTURE_CLOSE'
    st['cash_eur']=round(x['cash_eur'],5)
    st['signal_history'].append(sig)
    st['signal_history']=st['signal_history'][-300:]
    st['blocked_actions']=st['blocked_actions'][-100:]
    acts=plan(st,cfg,sig,sig['defensive'])
    if acts:
        kind='INITIAL' if not st['orders'] and not st['contributions'] else 'STRATEGIC'
        for a in acts:
            a['kind']=kind
            if a['side']=='BUY' and st['contributions'] and a['sleeve'] in ('CORE','SATELLITE'):
                a['pac_only']=len([q for q in executed if q['side']=='SELL'])==0
        st['pending']={'decision_session':session,
            'created_at_utc':now.astimezone(dt.timezone.utc).isoformat(),
            'proposed_orders':acts,'policy':cfg['execution_policy'],
            'signal_method':sig['method'],'input_session':sig['decision_session']}
    return st,len(executed)


def build_report(cfg,st,status,now,quality=None):
    last=st['nav'][-1] if st['nav'] else None
    cash=cash_total(st)
    return {'checked_at_utc':now.astimezone(dt.timezone.utc).isoformat(),
       'model':cfg['model'],'mode':cfg['mode'],'status':status,
       'last_nav_session':last['date'] if last else None,
       'last_signal_session':st['signal_history'][-1]['decision_session'] if st['signal_history'] else None,
       'nav_eur':last['nav_eur'] if last else cfg['initial_cash_eur'],
       'cash_eur':round(cash,4),
       'stamp_proxy_eur':round(float(st.get('stamp_accrued_eur',0)),6),
       'accounting_adjustments':st.get('accounting_adjustments',[])[-3:],
       'invested_eur':last['holdings_eur'] if last else 0,
       'positions':{k:{'units':round(v['units'],8),'cash_eur':round(v['cash_eur'],4),
                       'cost_basis_eur':round(v['basis_eur'],4)} for k,v in st['balances'].items()},
       'orders_total':len(st['orders']),'last_orders':st['orders'][-8:],
       'pending_orders':st['pending'],'blocked_actions':st['blocked_actions'][-8:],
       'latest_signal':st['signal_history'][-1] if st['signal_history'] else None,
       'monthly_cash_contributions':len(st['contributions']),
       'contributed_eur':cfg['initial_cash_eur']+sum(c['amount_eur'] for c in st['contributions']),
       'cumulative_twr_pct':round((last['twr_index']-1)*100,5) if last else 0,
       'latest_day_pct':last['twr_day_pct'] if last else 0,
       'max_drawdown_pct':round(min([0]+[r.get('drawdown_pct',0) for r in st['nav']]),5),
       'no_real_orders':True,'real_broker_orders':0,
       'historical_frozen_v23_parity':'ORIGINAL_LIVE_SHA256_VERIFIED; TRADABLE_ETF_PROXY_DIFFERS_FROM_1926_FF',
       'source_quality':quality or {},
       'notes':'Original V2.3 LIVE replay EUNL.DE + CSBGE3.MI; planned next completed-session CLOSE paper fills. Macro may be carried with explicit warning; no real trades.'}


def main(now=None):
    now=now or dt.datetime.now(dt.timezone.utc)
    cfg=read(CONFIG);st=read(STATE);validate(cfg,st)
    stamp_corrected=correct_cash_only_stamp(st,now)
    if stamp_corrected:save(STATE,st)
    quality={};status='WAITING_COMPLETED_EOD_SESSION'
    try:
        feeds=historical_feeds(cfg,now)
        quality={'accounting_adjustment':'CORRECT_CASH_ONLY_SECURITY_STAMP_PROXY' if stamp_corrected else None,
                 'equity_last':feeds['session'],'quote_lag_days':feeds['quote_lag_days'],
                 'quote_lag_sessions_exchange_calendar':feeds['quote_lag_sessions'],
                 'exchange_calendar':listing_calendar(cfg['equity_ticker']).name,
                 'equity_quote_source':feeds['equity_quote_source'],
                 'bond_quote_source':feeds['bond_quote_source'],
                 'bond_last':feeds['bond'][-1]['date'] if feeds['bond'] else None,
                 'bond_quality':feeds['bond_problem'] or 'OK',
                 'macro_quality':feeds['macro_problem'] or 'SOURCE_BY_SOURCE_VERIFIED',
                 'macro_sources':feeds['macro_refresh']}
        sig=signal(feeds,cfg)
        quality['stress_source']=sig['stress'].get('source')
        quality['missing_macro_inputs']=sig['stress'].get('missing_inputs',[])
        quality['observed_macro_month']=sig['stress'].get('observed_macro_month',sig['stress'].get('month'))
        quality['macro_previous_month']=sig['stress'].get('month')
        quality['defensive_source']=sig['defensive'].get('source')
        if feeds['session']>=(st['last_processed_session'] or cfg['activation_date']) and \
          feeds['session']!=st['last_processed_session']:
            st,executed=ledger_process(cfg,st,feeds['session'],feeds,sig,now)
            save(STATE,st)
            status='PAPER_ORDERS_EXECUTED' if executed else 'V23_SIGNAL_PENDING_NEXT_COMPLETED_CLOSE'
        else:status='WAITING_NEW_EQUITY_ETF_CLOSE'
        if feeds['quote_lag_sessions']>=2:
            status='PENDING_DATA: EQUITY_CLOSE_MISSING_'+str(feeds['quote_lag_sessions'])+'_EXCHANGE_SESSIONS'
            quality['blocker']='EQUITY_PROVIDER_STALE_BUT_LEDGER_PRESERVED'
        if sig['stress'].get('source','').startswith(('CARRIED_FROZEN_','CARRIED_OBSERVED_')):
            status+='__MACRO_PROXY_STALE'
        save(AUDIT,{'checked_at_utc':now.astimezone(dt.timezone.utc).isoformat(),
                    'market_session':feeds['session'], 'source_quality':quality,
                    'first_quote_date':feeds['equity'][0]['date'],
                    'quote_samples':len(feeds['equity']),
                    'provider':'Yahoo adjusted close EUR; FRED and Kenneth French monthly',
                    'original_live_sha256':ORIGINAL_LIVE_SHA})
    except (InsufficientData,ValueError,KeyError,OSError,RuntimeError) as exc:
        status='PENDING_DATA: '+str(exc)[:160]
        quality['blocker']=str(exc)[:160]
    report=build_report(cfg,st,status,now,quality)
    save(REPORT,report)
    print(json.dumps({'status':status,'model':report['model'],'nav_eur':report['nav_eur'],
                      'orders_total':report['orders_total'], 'last_nav_session':report['last_nav_session'],
                      'pending':bool(report['pending_orders']),'quality':report['source_quality']},ensure_ascii=False))
    # A green workflow must not certify a multi-session-old paper account.
    return 2 if quality.get('quote_lag_sessions_exchange_calendar',0)>=2 else 0

if __name__=='__main__':raise SystemExit(main())
