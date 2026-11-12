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
import copy,datetime as dt,hashlib,json,math,os,sys,zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

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
def completed_cutoff(now):
    local=now.astimezone(ROME)
    return local.date() if local.time()>=dt.time(19,0) else local.date()-dt.timedelta(days=1)
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


def historical_feeds(cfg,now):
    """Original Yahoo/FRED/French source functions. Cached copies persist.
    Never treat a cached close for a prior day as today's quote.
    """
    cutoff=completed_cutoff(now).isoformat()
    eq=v23.yahoo_daily(cfg['signal_ticker'],cfg['history_start'],cfg)
    eq=sorted([dict(r) for r in eq if r['date']<=cutoff],key=lambda x:x['date'])
    if len(eq)<160:raise InsufficientData('EQUITY_HISTORY_UNDER_160_SESSIONS')
    last=eq[-1]['date'];gap=(day(cutoff)-day(last)).days
    if gap>4:raise InsufficientData('STALE_EQUITY_CLOSE_'+last)
    bond=[];bond_problem=None
    try:
        bond=v23.yahoo_daily(cfg['defensive_ticker'],cfg['history_start'],cfg)
        bond=sorted([dict(r) for r in bond if r['date']<=last],key=lambda x:x['date'])
    except Exception as exc:bond_problem=str(exc)[:160]
    if bond:
        recent_bond=(day(last)-day(bond[-1]['date'])).days<=4
    else:recent_bond=False
    if not recent_bond:bond_problem='STALE_OR_MISSING_BOND_'+(bond[-1]['date'] if bond else 'NONE')
    macro_problem=None
    try:
        macro=v23.load_macro(cfg)
    except Exception as exc:
        macro=None;macro_problem=str(exc)[:160]
    return {'equity':eq,'bond':bond,'bond_usable':recent_bond,
            'bond_problem':bond_problem,'macro':macro,'macro_problem':macro_problem,
            'cutoff':cutoff,'session':last,'quote_lag_days':gap}


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
    if feeds['bond_usable']:
        bonds=[r['close'] for r in feeds['bond'] if r['date']<=session]
        b=bonds[-1] if bonds else None
    else:b=None
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
    cutoff=dt.datetime.combine(day(session),dt.time(17,30),tzinfo=BERLIN).astimezone(dt.timezone.utc)
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
        if task['side']=='BUY' and sleeve=='CORE' and st['balances']['BOND']['cash_eur']>0:
            st['balances']['CORE']['cash_eur']+=st['balances']['BOND']['cash_eur'];st['balances']['BOND']['cash_eur']=0.
        if task['side']=='BUY' and sleeve=='BOND':
            st['balances']['BOND']['cash_eur']+=st['balances']['CORE']['cash_eur'];st['balances']['CORE']['cash_eur']=0.
        px=price['BOND' if sleeve=='BOND' else 'EQUITY']
        if px is None:
            st['blocked_actions'].append({'session':session,'reason':'MISSING_PRICE_FOR_'+sleeve})
            continue
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


def ledger_process(cfg,state,session,feeds,sig,now):
    st=copy.deepcopy(state)
    if st['last_processed_session'] is not None and session<=st['last_processed_session']:
        return st,0
    if session<cfg['activation_date']:
        raise ValueError('PRE_ACTIVATION_SESSION')
    # Do not fill a missed session based on future information. On first run,
    # value the latest verified close and plan initial orders for a later close.
    if (stamp_today(now)-day(session)).days>1:
        raise InsufficientData('OLD_DATA_NO_PROSPECTIVE_DECISION_'+session)
    price=prices_for_session(feeds,session,st['balances']['BOND']['units']>0)
    prior=st['nav'][-1] if st['nav'] else None
    previous_session=st['last_processed_session']
    if previous_session is not None:
        days=(day(session)-day(previous_session)).days
        previous_value=prior['nav_eur'] if prior else cfg['initial_cash_eur']
        st['stamp_accrued_eur']+=max(previous_value,0)*cfg['annual_stamp_proxy']*days/365.25
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
    quality={};status='WAITING_COMPLETED_EOD_SESSION'
    try:
        feeds=historical_feeds(cfg,now)
        quality={'equity_last':feeds['session'],'quote_lag_days':feeds['quote_lag_days'],
                 'bond_last':feeds['bond'][-1]['date'] if feeds['bond'] else None,
                 'bond_quality':feeds['bond_problem'] or 'OK',
                 'macro_quality':feeds['macro_problem'] or 'ONLINE_OR_PERSISTED_CACHE'}
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
    return 0

if __name__=='__main__':raise SystemExit(main())
