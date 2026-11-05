#!/usr/bin/env python3
"""Atlas Top10 Global A - append-only daily paper ledger.
No fictional executions. Requires complete, dated end-of-day market bars and FX.
Run at/after 23:00 Europe/Rome. Python 3.10+, requests.
"""
from __future__ import annotations
import argparse,csv,datetime as dt,json,math,os,sys,time
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import quote
import requests

ROOT=Path(__file__).resolve().parent
CONFIG=ROOT/'config.json'; STATE=ROOT/'state.json'
JOURNAL=ROOT/'orders.csv'; NAV=ROOT/'daily_nav.csv'; SOURCES=ROOT/'quotes_audit.csv'
FIELDS=['date','symbol','side','shares','price_local','currency','eur_per_local','gross_eur','commission_eur','slippage_eur','tax_eur','realized_pnl_eur','cash_after_eur','reason']
NAVFIELDS=['date','portfolio_nav_eur','benchmark_nav_eur','portfolio_return_pct','benchmark_return_pct','excess_return_pp','cash_eur','invested_eur','regime','positions','cumulative_orders','source_status']
AUDITFIELDS=['asof_date','symbol','bar_date','close','currency','feed']


def write_json(p,data):
    temp=p.with_suffix('.tmp');temp.write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding='utf-8');temp.replace(p)
def read_json(p):return json.loads(p.read_text(encoding='utf-8'))
def replace_rows(p,keys,rows):
    tmp=p.with_suffix(p.suffix+'.tmp')
    with tmp.open('w',encoding='utf-8',newline='') as out:
        writer=csv.DictWriter(out,fieldnames=keys,extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(p)

def append_rows(p,keys,rows):
    if not rows:return
    exists=p.exists()
    with p.open('a',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=keys,extrasaction='ignore')
        if not exists:w.writeheader()
        w.writerows(rows)
def historical_month_ends(bars,asof):
    by={}
    for d,price in bars.items():
        if d[:7]<asof[:7] and d<=asof:by[d[:7]]=(d,price) if d[:7] not in by or d>by[d[:7]][0] else by[d[:7]]
    return [by[m][1] for m in sorted(by)]
def ma10_state(bars,asof,previous,threshold):
    months=historical_month_ends(bars,asof)
    if len(months)<10:raise ValueError(f'Only {len(months)} complete monthly closes for benchmark (need 10)')
    last=float(months[-1]);ma=sum(months[-10:])/10
    current=previous
    if last>ma*(1+threshold):current=True
    if last<ma*(1-threshold):current=False
    return current,last,ma

def signal_state(bars, asof, previous, cfg):
    """Point-in-time signal at last FULL completed month, no intramonth peeking.

    single: price close vs SMA10; dual: SMA6 vs SMA12; ±1% deadband.
    """
    months=historical_month_ends(bars,asof)
    band=float(cfg['hysteresis'])
    mode=cfg.get('signal_mode','ma10')
    if mode=='ma10':
        if len(months)<10:raise ValueError('PENDING_SIGNAL_HISTORY: need 10 full months')
        last=float(months[-1]);slow=sum(months[-10:])/10
        if last>slow*(1+band):return True,last,slow
        if last<slow*(1-band):return False,last,slow
        return previous,last,slow
    if mode=='dual_6_12':
        if len(months)<12:raise ValueError('PENDING_SIGNAL_HISTORY: need 12 full months')
        fast=sum(months[-6:])/6;slow=sum(months[-12:])/12
        if fast>slow*(1+band):return True,fast,slow
        if fast<slow*(1-band):return False,fast,slow
        return previous,fast,slow
    raise ValueError('Unsupported signal mode: '+str(mode))

def fetch_yahoo(symbol:str, start='2025-09-01',end=None)->dict:
    today=dt.date.today();end=end or str(today)
    begin=int(dt.datetime.strptime(start,'%Y-%m-%d').replace(tzinfo=dt.timezone.utc).timestamp())
    stop=int((dt.datetime.strptime(end,'%Y-%m-%d').replace(tzinfo=dt.timezone.utc)+dt.timedelta(days=1)).timestamp())
    url=f'https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol,safe="")}'
    rr=None;errors=[]
    for host in ('query1.finance.yahoo.com','query2.finance.yahoo.com'):
        try:
            url=f'https://{host}/v8/finance/chart/{quote(symbol,safe="")}'
            rr=requests.get(url,params={'period1':begin,'period2':stop,'interval':'1d','events':'div,splits'},headers={'User-Agent':'Mozilla/5.0 AtlasPaperTracker/0.7'},timeout=12)
            rr.raise_for_status()
            data=rr.json()['chart']
            if data.get('error'):raise RuntimeError(str(data['error']))
            break
        except (requests.RequestException,ValueError,KeyError,RuntimeError) as exc:
            errors.append(f'{host}: {str(exc)[:120]}')
    else:
        raise RuntimeError('Market data unavailable for '+symbol+'; '+'; '.join(errors))
    if data.get('error'):raise RuntimeError(f'{symbol} feed error {data["error"]}')
    result=data['result'][0];meta=result['meta'];tz=ZoneInfo(meta.get('exchangeTimezoneName') or 'UTC')
    timestamps=result.get('timestamp') or []
    closes=result['indicators']['quote'][0]['close']
    bars={}
    for t,p in zip(timestamps,closes):
        if p is not None and math.isfinite(p) and p>0:
            day=dt.datetime.fromtimestamp(t,tz=tz).strftime('%Y-%m-%d')
            bars[day]=float(p)
    dividends={}
    for ev in (result.get('events') or {}).get('dividends',{}).values():
        d=dt.datetime.fromtimestamp(ev['date'],tz=tz).strftime('%Y-%m-%d')
        dividends[d]=dividends.get(d,0)+float(ev['amount'])
    splits={}
    for ev in (result.get('events') or {}).get('splits',{}).values():
        day=dt.datetime.fromtimestamp(ev['date'],tz=tz).strftime('%Y-%m-%d')
        ratio=float(ev.get('numerator',0))/max(float(ev.get('denominator',0)),1e-12)
        if not math.isfinite(ratio) or ratio<=0:raise RuntimeError('Invalid stock split for '+symbol)
        splits[day]=ratio
    # Yahoo Chart OHLC are split-adjusted even before a future split within
    # the requested history. Recover historical nominal closes so applying
    # actual share splits to held positions will NOT double-count the split.
    # Must use the same price convention as v0.8 shadow_engine.retrieve_yahoo.
    for bar_day,price in list(bars.items()):
        factor=1.0
        for split_day,ratio in splits.items():
            if split_day>bar_day:
                factor*=ratio
        bars[bar_day]=price*factor
    return {'symbol':symbol,'currency':meta.get('currency'),'bars':bars,'dividends':dividends,'splits':splits,'feed':'Yahoo Finance delayed daily chart (nominalized for splits)','retrieved_utc':dt.datetime.now(dt.timezone.utc).isoformat()}

def obtain_data(cfg,asof,fixture=None):
    if fixture:
        data=read_json(Path(fixture))
        return data
    symbols=[x['symbol'] for x in cfg['basket']]+[cfg['benchmark']['symbol'],'EURUSD=X','EURSAR=X']
    roster=ROOT/'annual_rosters'/f'{int(asof[:4])-1}.json'
    if asof>='2027-02-01' and roster.exists():symbols+=read_json(roster)['symbols']
    symbols=list(dict.fromkeys(symbols))
    data={}
    for sym in symbols:
        try:data[sym]=fetch_yahoo(sym,end=asof)
        except Exception as e:raise RuntimeError(f'Failed price/FX download {sym}: {e}') from e
    return data

def latest(bars,date,max_age_days=6,exact=False):
    eligible=[d for d in bars if d<=date]
    if not eligible:raise ValueError(f'No observation available up to {date}')
    d=max(eligible)
    if exact and d!=date:raise ValueError(f'No exact close for {date}; last={d}')
    if (dt.date.fromisoformat(date)-dt.date.fromisoformat(d)).days>max_age_days:raise ValueError(f'Stale bar {d} vs {date}')
    return d,float(bars[d])
def eur_per_local(currency,date,data,exact=False):
    if currency=='EUR':return 1.0
    # FX snapshots from the as-of date, never any later fixing.
    if currency=='USD':
        _,r=latest(data['EURUSD=X']['bars'],date,4,exact=False)
        return 1/r
    if currency=='SAR':
        _,r=latest(data['EURSAR=X']['bars'],date,4,exact=False)
        return 1/r
    raise ValueError('Unsupported currency '+str(currency))

def market_is_complete(date, cfg, data):
    # Require an actual close on the very same date for EVERY frozen basket constituent
    # and the benchmark. Works on common Saudi / US / Xetra trading days.
    for item in cfg['basket']:
        sym=item['symbol']; latest(data[sym]['bars'],date,exact=True)
        eur_per_local(data[sym]['currency'],date,data)
    latest(data[cfg['benchmark']['symbol']]['bars'],date,exact=True)
    return True

def loss_trim(losses,year):return [x for x in losses if x['expires']>=year and x['amount']>1e-8]
def apply_sale_tax(state,pnl,year,taxrate):
    state['tax_losses']=loss_trim(state['tax_losses'],year)
    if pnl<0:
        state['tax_losses'].append({'amount':-pnl,'expires':year+4})
        return 0.0
    taxable=pnl
    for x in sorted(state['tax_losses'],key=lambda x:x['expires']):
        u=min(taxable,x['amount']);taxable-=u;x['amount']-=u
        if taxable<=1e-8:break
    state['tax_losses']=loss_trim(state['tax_losses'],year)
    return max(taxable,0)*taxrate

def mark_positions(state,cfg,data,date):
    total=0.0
    for s,pos in state['positions'].items():
        _,p=latest(data[s]['bars'],date)
        total+=pos['shares']*p*eur_per_local(data[s]['currency'],date,data)
    return total

def transact(state,cfg,data,date,target_symbols,reason):
    """Append-only transactions: sell removed first, then buy newly added.
    Existing names are NEVER rebalanced, preserving low turnover.
    """
    before=state['positions']; existing=set(before); target=set(target_symbols)
    changes=sorted(existing-target)+sorted(target-existing)
    if not changes:return []
    # Do not trade a basket unless its source date is verified, not stale.
    market_is_complete(date,cfg,data)
    orders=[]
    year=int(date[:4]); fee=cfg['commission_eur'];s=cfg['slippage_rate']
    def fxprice(sym):
        return data[sym]['bars'][date], data[sym]['currency'], eur_per_local(data[sym]['currency'],date,data)
    # FIFO enough because one position per issuer and no rebalance additions.
    for name in sorted(existing-target):
        pos=state['positions'][name];p,cur,fx=fxprice(name);gross=pos['shares']*p*fx;slip=gross*s
        proceeds=gross-slip-fee;gain=proceeds-pos['cost_basis_eur'];tax=apply_sale_tax(state,gain,year,cfg['capital_gain_tax'])
        state['cash_eur']+=proceeds-tax;del state['positions'][name]
        orders.append(dict(date=date,symbol=name,side='SELL',shares=pos['shares'],price_local=p,currency=cur,eur_per_local=fx,gross_eur=gross,commission_eur=fee,slippage_eur=slip,tax_eur=tax,realized_pnl_eur=gain,cash_after_eur=state['cash_eur'],reason=reason))
    new=sorted(target-existing)
    if new:
        # Seed: equal nominal budget across all 10; later rotate only proceeds into new.
        # If zero holdings, allocate evenly to the 10; otherwise divide unallocated cash.
        amount=state['cash_eur']/len(new)
        for name in new:
            p,cur,fx=fxprice(name);gross=(amount-fee)/(1+s);q=gross/(p*fx)
            if q<=0:raise ValueError('Insufficient cash for '+name)
            actual=q*p*fx*(1+s)+fee
            state['cash_eur']-=actual
            state['positions'][name]={'shares':q,'cost_basis_eur':actual}
            orders.append(dict(date=date,symbol=name,side='BUY',shares=q,price_local=p,currency=cur,eur_per_local=fx,gross_eur=q*p*fx,commission_eur=fee,slippage_eur=q*p*fx*s,tax_eur=0.0,realized_pnl_eur=0.0,cash_after_eur=state['cash_eur'],reason=reason))
        if abs(state['cash_eur'])<1e-6:state['cash_eur']=0.0
    state['order_count']+=len(orders)
    return orders

def benchmark_purchase(state,cfg,data,date):
    sym=cfg['benchmark']['symbol'];p=data[sym]['bars'][date];fe=cfg['commission_eur'];sl=cfg['slippage_rate']
    budget=cfg['initial_capital_eur'];shares=(budget-fe)/((1+sl)*p)
    state['benchmark']['shares']=shares;state['benchmark']['cash_eur']=0.0;state['benchmark']['cost_basis_eur']=budget
    return {'date':date,'symbol':sym,'side':'BUY','shares':shares,'price_local':p,'currency':'EUR','eur_per_local':1,'gross_eur':shares*p,'commission_eur':fe,'slippage_eur':shares*p*sl,'tax_eur':0,'realized_pnl_eur':0,'cash_after_eur':0,'reason':'benchmark initial long-only'}

def read_roster(cfg,date):
    # Freeze as-of-launch basket through January 2027. Annual updates at first
    # common trading date of February only after a verified year-end ranking is supplied.
    if date<'2027-02-01':return [x['symbol'] for x in cfg['basket']],None
    file=ROOT/'annual_rosters'/f'{int(date[:4])-1}.json'
    if not file.exists():return [x['symbol'] for x in cfg['basket']],'YEAR_END_ROSTER_MISSING'
    r=read_json(file)
    if len(r['symbols'])!=10 or len(set(r['symbols']))!=10:raise ValueError('Invalid annual roster')
    return list(r['symbols']),None

def log_nav(state,cfg,data,date):
    value=mark_positions(state,cfg,data,date);state['cash_eur']=max(0,float(state['cash_eur']))
    n=value+state['cash_eur']-state['liabilities_eur']
    bench=state['benchmark'];bp=latest(data[cfg['benchmark']['symbol']]['bars'],date)[1]
    bn=bench['cash_eur']+bench['shares']*bp-bench.get('liabilities_eur',0)
    base=cfg['initial_capital_eur']
    return {'date':date,'portfolio_nav_eur':round(n,2),'benchmark_nav_eur':round(bn,2),
        'portfolio_return_pct':round((n/base-1)*100,4),'benchmark_return_pct':round((bn/base-1)*100,4),
        'excess_return_pp':round((n-bn)/base*100,4),'cash_eur':round(state['cash_eur'],2),
        'invested_eur':round(value,2),'regime':'RISK_ON' if state['risk_on'] else 'CASH',
        'positions':len(state['positions']),'cumulative_orders':state['order_count'],'source_status':'COMPLETE' if value>0 else 'CASH'}

def step(state,cfg,data,date):
    """One daily as-of close. Exactly dated trading; valuation may use <=6d old bars.

    Signals are only executed on a common real-close date, with a durable
    PENDING_TRANSITION across holidays/missing quotes. Do not backfill orders
    at an older price or accidentally record an unexecuted regime switch.
    """
    if date<=state['last_update']:return [],None
    orders=[]
    benchsym=cfg['benchmark']['symbol']
    if not state['initialized']:
        if date<cfg['first_executable_date']:return [],None
        market_is_complete(date,cfg,data)
        risk,metric,ma=signal_state(data[benchsym]['bars'],date,state['risk_on'],cfg)
        if cfg.get('signal_mode','ma10')=='ma10':
            if abs(metric-cfg['october_close_eur'])>cfg['initial_signal_tolerance_eur']:
                raise ValueError('SIGNAL_SOURCE_MISMATCH: October close inconsistent with frozen signal')
            if risk!=cfg['seed_risk_on']:
                raise ValueError('SIGNAL_SOURCE_MISMATCH: seed regime differs from verified October signal')
        target,warning=read_roster(cfg,date)
        if warning:raise ValueError('PENDING_YEAR_END_ROSTER: '+warning)
        orders=transact(state,cfg,data,date,target if risk else [],'initial point-in-time basket')
        orders.append(benchmark_purchase(state,cfg,data,date))
        state['initialized']=True
        state['risk_on']=risk
        state['last_regime_month']=(dt.date.fromisoformat(date).replace(day=1)-dt.timedelta(days=1)).strftime('%Y-%m')
        state['last_roster_year']=2026
        state['last_update']=date
        return orders,log_nav(state,cfg,data,date)

    # Accrue annual securities tax and cash dividends from the previous date.
    elapsed=(dt.date.fromisoformat(date)-dt.date.fromisoformat(state['last_update'])).days
    if elapsed<0:raise ValueError('Backwards date')
    # Cache the previous mark to avoid Yahoo rewriting pre-split historical
    # closes when a corporate action occurred after that valuation date.
    value_prev=float(state.get('nav_history', [{}])[-1].get('invested_eur',
                      mark_positions(state,cfg,data,state['last_update'])))
    state['liabilities_eur']+=value_prev*cfg['annual_bollo_rate']*elapsed/365.25
    state['benchmark']['liabilities_eur']+=state['benchmark']['shares']*latest(data[benchsym]['bars'],state['last_update'])[1]*cfg['annual_bollo_rate']*elapsed/365.25
    # Shares change on a stock split, but there is no BUY/SELL and no tax
    # realization. Yahoo's historical closes can be split-adjusted retroactively;
    # previously committed equity/quotes are NEVER rewritten.
    for sym,pos in state['positions'].items():
        for action_date,ratio in sorted(data[sym].get('splits',{}).items()):
            if state['last_update']<action_date<=date:
                before=float(pos['shares'])
                pos['shares']=before*float(ratio)
                state.setdefault('corporate_actions',[]).append({'date':action_date,'symbol':sym,
                    'type':'SPLIT','ratio':ratio,'shares_before':before,
                    'shares_after':pos['shares'],'cost_basis_eur_unchanged':pos['cost_basis_eur']})
    for ex,ratio in sorted(data[benchsym].get('splits',{}).items()):
        if state['last_update']<ex<=date:
            before=float(state['benchmark']['shares'])
            state['benchmark']['shares']=before*float(ratio)
            state.setdefault('corporate_actions',[]).append({'date':ex,'symbol':benchsym,
                'type':'BENCHMARK_SPLIT','ratio':ratio,'shares_before':before,
                'shares_after':state['benchmark']['shares']})
    for sym,pos in state['positions'].items():
        for ex,per_share in data[sym].get('dividends',{}).items():
            if state['last_update']<ex<=date:
                amount=pos['shares']*float(per_share)*eur_per_local(data[sym]['currency'],ex,data)
                tax=amount*cfg['dividend_tax_rate']
                state['cash_eur']+=amount-tax
                state.setdefault('dividend_history',[]).append({'date':ex,'symbol':sym,
                    'gross_eur':amount,'tax_eur':tax,'net_eur':amount-tax,
                    'tax_model':'Italian 26pct proxy; foreign withholding not included'})

    # Detect new completed-month close (calculation is intentionally on the
    # first following month, not within the signal month).
    completed_month=(dt.date.fromisoformat(date).replace(day=1)-dt.timedelta(days=1)).strftime('%Y-%m')
    if completed_month>state.get('last_regime_month',''):
        risk,metric,slow=signal_state(data[benchsym]['bars'],date,state['risk_on'],cfg)
        state['last_regime_month']=completed_month
        if risk!=state['risk_on']:
            state['pending_transition']={'risk_on':risk,'signal_month':completed_month,'metric':metric,'slow':slow}
        else:
            state.pop('pending_transition',None)

    # Frozen annual memberships: a missing year-end roster blocks changing
    # constituents, not daily accounting. Keep warning until resolved.
    needed_roster_year=int(date[:4])-1
    due=(date[5:] >= '02-01' and int(date[:4])>state.get('last_roster_year',2026))
    target,warning=read_roster(cfg,date)
    if due:
        if warning:state['data_warning']=warning
        else:state.pop('data_warning',None)

    # Attempt a PENDING regime switch on any subsequent valid date.
    pending=state.get('pending_transition')
    if pending:
        trade_target=[] if not pending['risk_on'] else target
        if not (pending['risk_on'] and warning):
            try:
                orders+=transact(state,cfg,data,date,trade_target,'monthly verified trend transition')
                state['risk_on']=pending['risk_on']
                state.pop('pending_transition',None)
            except (ValueError,KeyError) as e:
                state['pending_trade_reason']=str(e)
        else:
            state['pending_trade_reason']='YEAR_END_ROSTER_MISSING'
    if not state.get('pending_transition'):state.pop('pending_trade_reason',None)

    # Annual rotation only if risk ON and no unresolved risk change; do it in
    # February or later after the year-end membership is supplied.
    if due and not warning:
        if state['risk_on'] and not state.get('pending_transition'):
            try:
                orders+=transact(state,cfg,data,date,target,'annual Top 10 refresh')
                state['last_roster_year']=int(date[:4])
            except (ValueError,KeyError) as e:
                state['pending_roster_execution']=str(e)
        elif not state['risk_on'] and not state.get('pending_transition'):
            state['last_roster_year']=int(date[:4])
    if not due:state.pop('pending_roster_execution',None)
    state['last_update']=date
    nav=log_nav(state,cfg,data,date)
    if state.get('pending_transition') or state.get('data_warning'):
        nav['source_status']='PENDING_TRADE' if state.get('pending_transition') else 'PENDING_ROSTER'
    return orders,nav

def render_dashboard(cfg,state):
    if NAV.exists():
        with NAV.open(encoding='utf-8') as fh: rows=list(csv.DictReader(fh))
    else: rows=[]
    last=rows[-1] if rows else {'date':cfg['start_date'],'portfolio_nav_eur':'10000.00','benchmark_nav_eur':'10000.00','portfolio_return_pct':'0.00','benchmark_return_pct':'0.00','excess_return_pp':'0.00','regime':'RISK_ON','source_status':'PENDING'}
    from html import escape
    list_items=''.join(f'<tr><td>{x["rank"]}</td><td>{escape(x["name"])}</td><td><code>{escape(x["symbol"])}</code></td><td>{escape(x["currency"])}</td></tr>' for x in cfg['basket'])
    n=float(last['portfolio_nav_eur']);b=float(last['benchmark_nav_eur']);diff=float(last['excess_return_pp'])
    dataset=json.dumps(rows)
    html=f'''<!doctype html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Atlas Top 10 Forward · {cfg['start_date']}</title><style>
:root{{--ink:#eaf0f9;--muted:#a3b5ce;--line:#35445a;--bg:#0b1220;--surface:#141f31;--accent:#90c4ff}}
*{{box-sizing:border-box}}body{{background:var(--bg);color:var(--ink);font:16px system-ui;margin:0}}main{{max-width:960px;margin:auto;padding:30px 18px}}h1{{font-size:27px}}h2{{font-size:19px;margin-top:32px}}.hint{{color:var(--muted);font-size:13px;line-height:1.6}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-top:22px}}.card{{border:1px solid var(--line);background:var(--surface);border-radius:14px;padding:19px}}.label{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}}.val{{font-size:25px;font-weight:700;margin-top:10px}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;border-bottom:1px solid var(--line);padding:10px}}th{{color:var(--muted)}}.status{{color:var(--accent)}}.plot{{width:100%;height:270px;border:1px solid var(--line);border-radius:14px;background:var(--surface)}}a{{color:var(--accent)}}
</style></head><body><main><p class="hint">Forward paper trading · avvio {cfg['start_date']}</p><h1>Atlas Top 10 Global A</h1><p class="status">{last['regime']} · ultimo aggiornamento: {last['date']} · {last['source_status']}</p><div class="cards"><div class="card"><div class="label">Portafoglio</div><div class="val">€ {n:,.2f}</div></div><div class="card"><div class="label">ETF IUSQ benchmark</div><div class="val">€ {b:,.2f}</div></div><div class="card"><div class="label">Extra-rendimento</div><div class="val">{diff:+.2f} pp</div></div><div class="card"><div class="label">Ordini eseguiti</div><div class="val">{state['order_count']}</div></div></div><h2>Curva forward</h2><svg id="plot" class="plot" viewBox="0 0 900 270" role="img" aria-label="Curva patrimonio cumulata"></svg><p class="hint">Il patrimonio è marcato a prezzi osservati; plusvalenze non ancora realizzate non sono tassate. Uscite e imposte effettive vengono registrate nel ledger.</p><h2>Universo congelato al lancio</h2><table><thead><tr><th>#</th><th>Azienda</th><th>Ticker</th><th>Valuta</th></tr></thead><tbody>{list_items}</tbody></table><h2>Metodo</h2><p class="hint">10 società più capitalizzate al lancio, 10% ciascuna alla prima entrata, nessun ribilanciamento degli incumbent, aggiornamento annuale. Regime mensile: IUSQ sopra/sotto MA10 con banda 1%; esecuzione solo con barre chiuse e verifica calendario. Commissioni €1 per ordine, slippage 0,10%, capital gain 26% sulle vendite con minusvalenze fino a 4 anni, dividendi 26% stimato, bollo 0,20% annuo. Benchmark IUSQ stesso punto di ingresso e medesimi costi iniziali.</p><p class="hint">Uso didattico e di ricerca: dati Yahoo non garantiti, valute e fiscalità sono approssimazioni. Nessun ordine reale inviato.</p></main><script>const rows={dataset};const svg=document.getElementById('plot');if(rows.length<2){{svg.innerHTML='<text fill="#a3b5ce" x="34" y="135">Il grafico apparirà dal secondo aggiornamento.</text>'}}else{{const A=rows.map(r=>+r.portfolio_nav_eur),B=rows.map(r=>+r.benchmark_nav_eur),all=[...A,...B],lo=Math.min(...all)*.995,hi=Math.max(...all)*1.005,xx=i=>40+i*820/Math.max(rows.length-1,1),yy=v=>230-190*(v-lo)/Math.max(hi-lo,1e-9);function line(vals,color){{const path=vals.map((v,i)=>`${{i?'L':'M'}}${{xx(i)}},${{yy(v)}}`).join(' ');return `<path d="${{path}}" stroke="${{color}}" fill="none" stroke-width="2.6"/>`}}svg.innerHTML='<line x1="40" y1="230" x2="865" y2="230" stroke="#3a516f"/>'+line(A,'#64d9c1')+line(B,'#88adff')+'<text x="40" y="26" fill="#64d9c1">Atlas</text><text x="118" y="26" fill="#88adff">IUSQ</text>'}}</script></body></html>'''
    (ROOT/'dashboard.html').write_text(html,encoding='utf-8')

# Importable accounting engine. CLI entrypoint: daily_runner.py