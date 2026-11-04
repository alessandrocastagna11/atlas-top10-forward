#!/usr/bin/env python3
"""Atlas v0.8 methodological bridge, research ONLY.

Uses the same historical archived company roster and EUR total-return CLOSE prices as
Atlas v0.5. Monthly trend/volatility from fully completed proxy market months;
orders fill at the next eligible day's *CLOSE* (NOT next-session open because
archived OHLC is unavailable). Annual rank is reconstructed, NOT PIT certified.
No changes to forward v0.7/v0.8 live ledger. No newly trained parameters.
"""
from __future__ import annotations
from pathlib import Path
from dataclasses import dataclass
import math,copy,json,hashlib
import numpy as np
import pandas as pd
import sys

ROOT=Path(__file__).resolve().parent
ARCHIVE=ROOT/'historical_source'
if not ARCHIVE.exists():
    ARCHIVE=Path('/mnt/data/backtest_audit/Atlas_Top10_MA_Study_2026-11-03')
sys.path.insert(0,str(ARCHIVE))
import atlas_top10_v05 as v5
import atlas_top10_v03 as old

START='2016-02-01';END='2025-12-10'
BASE_CAPITAL=10000.; HYST=.01; SLIPPAGE=.001; FEE=1.; BOLLO=.002; DIV_TAX=.26
VARIANTS=('BuyHold_Top10','MA10','MA6_MA12','MA8_MA15','Hybrid_MA3_MA10','Adaptive_Risk')


def performance(x):
    s=x.liquidatable_eur.astype(float)
    years=(s.index[-1]-s.index[0]).days/365.25
    cagr=(s.iloc[-1]/BASE_CAPITAL)**(1/years)-1
    dd=s/s.cummax()-1
    r=s.pct_change().dropna()
    downside=np.minimum(r.to_numpy(),0)
    sortino=float(r.mean()*math.sqrt(252)/(np.sqrt(np.mean(downside**2)))) if (downside**2).mean()>0 else None
    sharpe=float(r.mean()/r.std()*math.sqrt(252)) if r.std()>0 else None
    return dict(cagr_pct=round(100*cagr,4),max_dd_pct=round(100*dd.min(),4),
        volatility_pct=round(float(r.std()*np.sqrt(252)*100),4),sharpe=round(sharpe,3) if sharpe is not None else None,
        sortino=round(sortino,3) if sortino is not None else None,calmar=round(cagr/abs(dd.min()),3) if dd.min()<0 else None,
        final_liquidatable_eur=round(float(s.iloc[-1]),2))


def calc_signal(mode,monthly,market,day,was_on):
    """No same-month peeking: only prior fully completed calendar month and prior day vol."""
    floor=day.to_period('M').to_timestamp()
    old_month=monthly.loc[monthly.index<floor]
    if len(old_month)<15:raise ValueError('Insufficient monthly history')
    m=old_month.iloc[-1];ma=lambda n:float(old_month.iloc[-n:].mean())
    ma3,ma6,ma10,ma12,ma15,ma8=map(ma,(3,6,10,12,15,8))
    if mode=='BuyHold_Top10':risk=True;expo=1.;val=m;slow=ma10
    else:
        if mode=='MA10':metric,slow=m,ma10
        elif mode in ('MA6_MA12','Adaptive_Risk'):metric,slow=ma6,ma12
        elif mode=='MA8_MA15':metric,slow=ma8,ma15
        elif mode=='Hybrid_MA3_MA10':metric,slow=m,ma10
        else:raise ValueError(mode)
        risk=was_on
        if mode=='Hybrid_MA3_MA10':
            if m>ma10*(1+HYST) and ma3>ma10:risk=True
            elif m<ma10*(1-HYST) or ma3<ma10*(1-HYST):risk=False
        else:
            if metric>slow*(1+HYST):risk=True
            elif metric<slow*(1-HYST):risk=False
        expo=float(risk)
        val=metric
        if mode=='Adaptive_Risk':
            prior=market.loc[market.index<day].pct_change(fill_method=None).dropna()
            if len(prior)<20:raise ValueError('No vol history')
            vol=float(prior.iloc[-20:].std(ddof=1)*math.sqrt(252))
            scale=min(1.,max(.25,.16/max(vol,1e-9)))
            expo=(scale if m>=ma3 else .5*scale) if risk else 0.
    return dict(signal_month=str(old_month.index[-1].date()),metric=float(val),slow=float(slow),
                risk_on=bool(risk),exposure=float(expo),ma3=ma3,
                vol20_annual=(float(prior.iloc[-20:].std(ddof=1)*math.sqrt(252)) if mode=='Adaptive_Risk' else None))


def run_variant(mode,roster,mark,exact,benchmark,div):
    days=benchmark.loc[START:END].index
    monthly=benchmark.resample('ME').last()
    cash=BASE_CAPITAL; positions={}; pending=[]; ledger=old.ItalianTaxLedger(.26)
    navs=[];orders=[];signals=[];failures=[];canceled=[];tax_paid=0.;fee_paid=0.;slippage_paid=0.;hk_paid=0.;bollo_liab=0.;bollo_paid=0.;div_tax_paid=0.
    risk_on=False;last_month='';prev=None;previous_target=None;previous_selected=None
    hkd=set(roster.loc[roster.currency.eq('HKD'),'company'])
    def duty(name,t):
        return (0.0013 if pd.Timestamp('2021-08-01')<=t<pd.Timestamp('2023-11-17') else 0.001) if name in hkd else 0.
    def price(name,t,exact_needed=True):
        x=(exact if exact_needed else mark).at[t,name]
        return float(x) if pd.notna(x) and float(x)>0 else None
    def mark_value(t):
        v=0.
        for name,p in positions.items():
            pr=price(name,t,False)
            if pr is None:raise ValueError(f'Missing mark for {name} {t}')
            v+=p['shares']*pr
        return v
    def do_trade(task,t):
        nonlocal cash,tax_paid,fee_paid,slippage_paid,hk_paid
        name=task['company']; px=price(name,t,True)
        if px is None:return False
        side=task['side'];rate=duty(name,t)
        if side=='SELL':
            p=positions.get(name)
            if not p:return True
            q=(p['shares'] if task['all'] else min(p['shares'],task['amount']/px))
            if q<=0:return True
            gross=q*px;cost=gross*(SLIPPAGE+rate)+FEE
            if gross<=cost:return True
            net=gross-cost;basis=p['basis']*(q/p['shares']);gain=net-basis
            tax,_=ledger.sale(gain,t.year)
            cash+=net-tax;tax_paid+=tax
            p['shares']-=q;p['basis']-=basis
            if p['shares']<1e-9:del positions[name]
        else:
            budget=min(task['amount'],cash)
            if budget<10.:return False
            gross=(budget-FEE)/(1+SLIPPAGE+rate);q=gross/px
            cost=gross*(1+SLIPPAGE+rate)+FEE
            if cost>cash+1e-5:raise AssertionError('Overdraft')
            cash-=cost;p=positions.setdefault(name,{'shares':0.,'basis':0.});p['shares']+=q;p['basis']+=cost
            gain=tax=0.;basis=0.
        fee_paid+=FEE;slippage_paid+=gross*SLIPPAGE;hk_paid+=gross*rate
        orders.append(dict(date=str(t.date()),signal_month=task['signal_month'],variant=mode,company=name,side=side,
                shares=q,close_execution_proxy_eur=px,commission_eur=FEE,slippage_eur=gross*SLIPPAGE,
                hk_duty_eur=gross*rate,realized_gain_eur=gain,tax_eur=tax,issued_on=task['issued_on'],
                reason=task['reason']))
        return True
    def create_tasks(t,sig,selected,exposure_changed):
        # For all decisions, base exposure on prices available before today's execution.
        pidx=days[days<t]
        prev_day=pidx[-1] if len(pidx) else t
        v={n:p['shares']*price(n,prev_day,False) for n,p in positions.items()}
        equity=sum(v.values());nav=cash+equity-bollo_liab
        desired=max(0,nav*sig['exposure'])
        outgoing=set(v)-set(selected)
        tasks=[dict(company=n,side='SELL',all=True,amount=v[n],reason='roster/exit',signal_month=sig['signal_month'],issued_on=str(t.date())) for n in sorted(outgoing)]
        inc={k:vv for k,vv in v.items() if k in selected}
        newbies=[n for n in selected if n not in v]
        # A pure roster substitution must NOT sell continuing winners merely
        # to restore equal weights: redeploy only outgoing liquidation proceeds.
        # With a genuine exposure change, scale incumbents and newcomers.
        new_allocation=(desired*len(newbies)/len(selected) if selected and exposure_changed
                        else sum(v[n] for n in outgoing))
        for n in newbies:
            if new_allocation/max(1,len(newbies))>10:
                tasks.append(dict(company=n,side='BUY',all=False,amount=new_allocation/max(1,len(newbies)),reason='entrant',signal_month=sig['signal_month'],issued_on=str(t.date())))
        if not exposure_changed:return tasks
        incumbent_budget=max(0.,desired-new_allocation)
        if incumbent_budget<1e-8:
            for n,val in inc.items():tasks.append(dict(company=n,side='SELL',all=True,amount=val,reason='cash transition',signal_month=sig['signal_month'],issued_on=str(t.date())))
        elif sum(inc.values())>0:
            scale=incumbent_budget/sum(inc.values())
            for n,val in inc.items():
                delta=val*(scale-1)
                if abs(delta)>10.:
                    tasks.append(dict(company=n,side='BUY' if delta>0 else 'SELL',all=False,amount=abs(delta),reason='monthly exposure scale',signal_month=sig['signal_month'],issued_on=str(t.date())))
        return tasks
    for t in days:
        if prev is not None:
            elapsed=(t-prev).days
            eq_prev=mark_value(prev)
            bollo=eq_prev*BOLLO*elapsed/365.25
            bollo_liab+=bollo;bollo_paid+=bollo
            # Archived price_eur is derived from adjusted close/total return; the
            # archived dividend column supports *tax drag only* (no added cash).
            dvtax=sum(p['shares']*price(n,prev,False)*float(div.at[t,n])*DIV_TAX for n,p in positions.items())
            bollo_liab+=dvtax;div_tax_paid+=dvtax
        if t.to_period('M')!=pd.Period(last_month,'M') if last_month else True:
            s=calc_signal(mode,monthly,benchmark,t,risk_on)
            risk_on=s['risk_on']
            year=t.year if t.month>=2 else t.year-1
            r=roster.loc[roster.effective_year.eq(year)].sort_values('rank')
            if len(r)!=10:raise ValueError(f'Invalid roster {year}')
            names=list(r.company) if s['exposure']>0 else []
            changed=(previous_target is None or abs(float(previous_target)-s['exposure'])>1e-8
                     or previous_selected!=tuple(names))
            if changed:
                if pending:
                    canceled.extend([{**p,'canceled':str(t.date())} for p in pending])
                pending=create_tasks(t,s,names,exposure_changed=(previous_target is None or abs(float(previous_target)-s['exposure'])>1e-8))
            # Keep existing tasks if unchanged; do not sell winners merely to
            # offset accrued bollo or a few cents of residual cash.
            previous_target=s['exposure'];previous_selected=tuple(names)
            signals.append(dict(date=str(t.date()),variant=mode,roster_year=year,**s,
                                signal_changed=changed,pending_orders=len(pending)))
            last_month=t.strftime('%Y-%m')
        # Previous close signal can generate independent orders on the next
        # eligible date with historical exact CLOSE, which is NOT an OPEN fill.
        still=[]
        for task in sorted(pending,key=lambda x:(x['side']!='SELL',x['company'])):
            if not do_trade(task,t):still.append(task)
        pending=still
        eq=mark_value(t);nav=cash+eq-bollo_liab
        if nav<=0 or not np.isfinite(nav) or cash<-1e-6:raise AssertionError(f'Invalid NAV {t}: cash={cash}, nav={nav}')
        # Fair after-tax hypothetical liquidation, computing all stock taxes on
        # a COPY of the loss ledger so daily estimates do not affect future.
        liquidation=cash-bollo_liab;hypo=copy.deepcopy(ledger)
        sell_details=[]
        for name,p in positions.items():
            px=price(name,t,False)
            gross=p['shares']*px
            net=gross*(1-SLIPPAGE-duty(name,t))-FEE
            sell_details.append((net-p['basis'],net))
        for gain,net in sorted(sell_details):
            tax,_=hypo.sale(gain,t.year);liquidation+=net-tax
        weights=sorted([p['shares']*price(n,t,False)/nav for n,p in positions.items()],reverse=True)
        navs.append(dict(date=t,nav_eur=nav,liquidatable_eur=liquidation,cash_eur=cash,equity_eur=eq,
                         exposure=eq/nav,max_weight=weights[0] if weights else 0.,top3_weight=sum(weights[:3]),
                         positions=len(positions),orders=len(orders),tax_realized_eur=tax_paid,
                         fees_eur=fee_paid,slippage_eur=slippage_paid,hk_stamp_eur=hk_paid,
                         dividend_tax_proxy_eur=div_tax_paid,bollo_accrued_eur=bollo_paid,pending=len(pending)))
        prev=t
    stats=dict(orders=len(orders),buys=sum(x['side']=='BUY' for x in orders),sells=sum(x['side']=='SELL' for x in orders),
                fees_eur=round(fee_paid,2),tax_realized_eur=round(tax_paid,2),slippage_eur=round(slippage_paid,2),
                hkd_stamp_eur=round(hk_paid,2),bollo_accrued_eur=round(bollo_paid,2),
                dividend_tax_proxy_eur=round(div_tax_paid,2),mean_exposure_pct=round(np.mean([x['exposure'] for x in navs])*100,2),
                max_name_weight_pct=round(max(x['max_weight'] for x in navs)*100,2),
                mean_top3_pct=round(np.mean([x['top3_weight'] for x in navs])*100,2),
                pending_end=len(pending),cancelled_tasks=len(canceled),loss_carry_eur=round(ledger.active_loss(),2))
    return pd.DataFrame(navs).set_index('date'),pd.DataFrame(orders),pd.DataFrame(signals),pd.DataFrame(canceled),stats


def run_all(output=ROOT):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    roster,mark,exact,benchmark,cash,div=v5.load_same_universe()
    cfg=old.Config(start=START,end=END)
    reference=old.bench_accounting(benchmark,cfg)
    benchmark_price=benchmark.loc[START:END]
    curves={'Global_proxy_60_30_10':reference.liquidatable_eur}
    results=[]
    for mode in VARIANTS:
        nav,orders,signals,canceled,stats=run_variant(mode,roster,mark,exact,benchmark,div)
        nav.to_csv(output/f'{mode}_NAV.csv')
        orders.to_csv(output/f'{mode}_ORDERS.csv',index=False)
        signals.to_csv(output/f'{mode}_SIGNALS.csv',index=False)
        curves[mode]=nav.liquidatable_eur
        results.append(dict(strategy=mode,**performance(nav),**stats))
    perf=pd.DataFrame(results).sort_values('cagr_pct',ascending=False)
    perf.to_csv(output/'COMPARISON.csv',index=False)
    curve=pd.DataFrame(curves);curve.to_csv(output/'EQUITY.csv')
    report=perf.to_markdown(index=False)
    summary='''# Atlas Top10 v0.8 — Backtest esplorativo (PROXY, non definitivo)\n\n'''
    summary+='**Eseguito su dati storici reali archiviati**: 2016-02-01 — 2025-12-10. Serie EUR dei titoli e classifiche ricostruite v0.5, proxy 60% SPY + 30% EFA + 10% EEM. **Non** è un test point-in-time certificato delle classifiche e **non** usa l\'ETF IUSQ.\n\n'
    summary+='**LIMITAZIONE DECISIVA:** i dati archiviati dispongono di prezzi giornalieri di **chiusura**, non di **apertura**. Gli ordini sono simulati alla prima CHIUSURA successiva al segnale mensile, con 0,1% slippage e 1 €/ordine, non come reali fill OPEN della v0.8. Calendario storico principalmente basato sulle sedute benchmark USA. Prima di sostituire il backtest precedente serve la ricostruzione OHLC/exchange calendars e ranking PIT.\n\n'
    summary+='**Strategie:** BuyHold Top10, MA10, MA6/12, MA8/15, Hybrid MA3/MA10 e Adaptive Risk secondo parametri già congelati (MA6/MA12 ±1%, MA3 conferma, volatilità 20 sedute, target vol 16%, floor 25% in regime ON, dimezzamento esposizione senza conferma MA3). Per ragioni di coerenza, le medie sono sullo stesso proxy globale anziché su IUSQ.\n\n'
    summary+='**Fiscalità:** 26% su plusvalenze realizzate (minusvalenze fino a 4 anni), 26% proxy tassazione dei dividendi stimati, bollo 0,2% annuo stimato sulle azioni, commissione 1 € e slippage 0,1% per lato, cash 0%; tassa su liquidazione ipotetica finale incorporata nella NAV liquidabile; HKD duty storica proxy. I dividendi nel dataset sono parte di una serie adjusted e perciò non vengono aggiunti come nuovi flussi in contanti.\n\n'
    summary+='**Rischi di interpretazione:** l\'intero 2016–2025 è già stato esplorato per Atlas; non è out-of-sample cieco. Il backtest è retrospettivo e la differenza tra strategie è descrittiva, non garanzia di performance. Il benchmark è differente da IUSQ in composizione e modalità fiscali.\n\n'
    summary+='## Confronto numerico (NAV dopo liquidazione ipotetica)\n\n'+report+'\n'
    summary+='\n**Validazione:** test automatici su assenza lookahead nelle medie, esposizione 0 quando il trend è OFF, nessuna leva, tassazione di vendita, somma ordini coerente e stesso periodo/benchmark. Vedere `test_backtest.py`.\n'
    (output/'REPORT.md').write_text(summary,encoding='utf-8')
    return perf,curve

if __name__=='__main__':
    p,c=run_all()
    print(p[['strategy','cagr_pct','max_dd_pct','orders','tax_realized_eur','mean_exposure_pct']].to_string(index=False))