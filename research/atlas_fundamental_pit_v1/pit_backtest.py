#!/usr/bin/env python3
"""Atlas Quality/Value SEC-filed-date exploratory backtest, isolated research only.
Historic market-cap rosters are reconstructed, NOT independently certified PIT.
SEC facts are filtered by their ORIGINAL filing dates (including restatements).
Analyst-estimate revisions are unavailable and intentionally NOT modelled.
"""
from __future__ import annotations
import json,gzip,base64,collections,csv,math,hashlib,datetime as dt,copy,sys
from pathlib import Path
import numpy as np,pandas as pd
ROOT=Path(__file__).resolve().parent
DATA=ROOT/'data'; REPORTS=ROOT/'reports'
START='2016-02'; END='2025-11'; CAPITAL=10000.
FEE=1.; SLIP=.001; BOLLO=.002; GAIN_TAX=.26; DIV_TAX=.26
MONTHS=list(pd.period_range(START,END,freq='M').astype(str))
MODELS=['Top10_US_BuyHold','Top10_US_MA6_12','Quant20_Price','Quant20_QualityValue','Quant20_QVM','Quant20_QVM_MA6_12']
VALUE_TAGS={'ni':['NetIncomeLoss','ProfitLoss','NetIncomeLossAvailableToCommonStockholdersBasic'],
 'assets':['Assets'],
 'equity':['StockholdersEquity','StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'],
 'revenue':['Revenues','RevenueFromContractWithCustomerExcludingAssessedTax','SalesRevenueNet'],
 'oper_income':['OperatingIncomeLoss'],
 'cfo':['NetCashProvidedByUsedInOperatingActivities'],
 'capex':['PaymentsToAcquirePropertyPlantAndEquipment'],
 'shares':['CommonStockSharesOutstanding','EntityCommonStockSharesOutstanding','WeightedAverageNumberOfDilutedSharesOutstanding']}

def input_data():
    inputs=([DATA/'market_input.b64'] if (DATA/'market_input.b64').exists() else sorted(DATA.glob('market_input_part_*.b64')))
    blob=base64.b64decode(''.join(''.join(p.read_text().splitlines()) for p in inputs))
    market=json.loads(gzip.decompress(blob))
    facts=json.loads(gzip.decompress((DATA/'sec_pit_facts.json.gz').read_bytes()))
    prices={s:{r[0]:r for r in p['monthly']} for s,p in market['symbols'].items()}
    benchmark={x[0]:float(x[2]) for x in market['benchmark_proxy_monthly']}
    return market,facts,prices,benchmark

def value_for_period(obs,asof,end,tags):
    values=[x for x in obs if x[0] in tags and x[3]==end and x[4]<=asof and x[4]>=end and x[1] in ('USD','shares')]
    for t in tags:
        vv=[v for v in values if v[0]==t]
        if vv:
            # Use FIRST publication of a value for this reporting period, NOT the
            # latest later restatement. Different forms may report same period.
            z=min(vv,key=lambda x:(x[4],x[5]))
            return float(z[6]),z[4],z[5],t
    return None

def sec_snapshot(sym,asof,facts,raw_usd,cache=None):
    r=facts['entities'].get(sym,{});
    if r.get('status')!='OK' or r.get('splits') is None:return None,'NO_SEC_OR_SPLIT_DATA'
    obs=r['observations']
    days=(dt.date.fromisoformat(asof)-dt.date(2000,1,1)).days
    ends=sorted({x[3] for x in obs if x[4]<=asof and x[3]<=asof},reverse=True)
    cutoff=dt.date.fromisoformat(asof)
    for end in ends:
        try:
            age=(cutoff-dt.date.fromisoformat(end)).days
            if age<0 or age>580:continue
            d={k:value_for_period(obs,asof,end,tags) for k,tags in VALUE_TAGS.items()}
            if any(d[k] is None for k in ('ni','assets','equity','revenue','cfo','shares')):continue
            ni,assets,equity,revenue,cfo,shares=[d[k][0] for k in ('ni','assets','equity','revenue','cfo','shares')]
            if assets<=0 or revenue<=0 or shares<=0 or not 0<raw_usd:continue
            # Historical archive closes are retrospectively split-adjusted as
            # of 2026-09-12. Convert historical share denominator to SAME units.
            split=1.
            for day,ratio in r['splits']:
                if end<day<='2026-09-12':split*=float(ratio)
            cap=shares*split*raw_usd
            if cap<5e7 or cap>20e12:continue
            roa=ni/assets
            cfoa=cfo/assets;debt_ratio=1-equity/assets
            op_margin=(d['oper_income'][0] if d['oper_income'] is not None else ni)/revenue
            earning_yield=ni/cap;book_to_market=equity/cap
            if not all(map(math.isfinite,[roa,cfoa,op_margin,debt_ratio,earning_yield,book_to_market])):continue
            if not (-2<roa<1 and -3<cfoa<3 and -3<op_margin<3 and -2<debt_ratio<3 and -1<earning_yield<1 and -4<book_to_market<15):continue
            pub=max(x[1] for x in d.values() if x is not None)
            if pub>asof:raise AssertionError('LOOKAHEAD SEC filing')
            return {'fiscal_end':end,'filing_known_by':pub,'roa':roa,'cfoa':cfoa,'margin':op_margin,
                 'solvency':equity/assets,'earnings_yield':earning_yield,'book_to_market':book_to_market,
                 'split_adjustment':split,'market_cap_proxy_usd':cap,'metrics':{k:{'tag':v[3],'filed':v[1],'accession':v[2]} for k,v in d.items() if v}},None
        except (ArithmeticError,OverflowError,ValueError):continue
    return None,'NO_COMPATIBLE_AS_FILED_ANNUAL_10K'

def momentum(p,month):
    m=pd.Period(month,'M')
    try:
        # 12-1 return excludes most recent month, ~11 months of lagged data
        def get(delta,col=5):return float(p[str(m-delta)][col])
        last=get(1); start=get(12);six=get(6)
        if min(last,start,six)<=0:return None
        # uses split/dividend-adjusted historical prices for returns only
        mom12=last/start-1;mom6=last/six-1
        rets=[math.log(get(i)/get(i+1)) for i in range(1,7)]
        vol=float(np.std(rets,ddof=1))*math.sqrt(12)
        return {'mom12':mom12,'mom6':mom6,'vol6':vol}
    except (KeyError,ValueError,ZeroDivisionError):return None

def factors_for_month(month,market,facts,prices,limit_rank=130):
    mm=pd.Period(month,'M');asof=str((mm-1).end_time.date())
    year=mm.year if mm.month>=2 else mm.year-1
    rows=[];missing=collections.Counter();audit=[]
    for sym,rank,sector in market['rosters'].get(str(year),[]):
        if rank>limit_rank:continue
        p=prices.get(sym)
        if not p or str(mm-1) not in p:missing['PRICE_MISSING']+=1;continue
        mom=momentum(p,month)
        if not mom:missing['MOM_12M_MISSING']+=1;continue
        raw_usd=float(p[str(mm-1)][7])
        snap,why=sec_snapshot(sym,asof,facts,raw_usd)
        if not snap:missing[why]+=1;continue
        item=dict(symbol=sym,rank=rank,sector=sector,**mom,**{k:v for k,v in snap.items() if k not in ('metrics',)})
        rows.append(item)
        audit.append({'decision_month':month,'symbol':sym,'rank':rank,'fiscal_end':snap['fiscal_end'],
                      'last_pub_date':snap['filing_known_by'],'metrics':snap['metrics']})
    frame=pd.DataFrame(rows)
    if len(frame)==0:return frame,missing,audit
    signals=[('mom12',1),('mom6',1),('vol6',-1),('roa',1),('cfoa',1),('margin',1),('solvency',1),('earnings_yield',1),('book_to_market',1)]
    for col,sign in signals:
        # pct rank, highest always better; clipping avoids extreme influences
        frame[col+'_pct']=frame[col].rank(pct=True,ascending=True) if sign>0 else 1-frame[col].rank(pct=True,ascending=True)+1/len(frame)
    frame['price_score']=.45*frame.mom12_pct+.25*frame.mom6_pct+.30*frame.vol6_pct
    frame['quality_score']=.35*frame.roa_pct+.25*frame.cfoa_pct+.20*frame.margin_pct+.20*frame.solvency_pct
    frame['value_score']=.60*frame.earnings_yield_pct+.40*frame.book_to_market_pct
    frame['qv_score']=.60*frame.quality_score+.40*frame.value_score
    frame['qvm_score']=.35*frame.price_score+.40*frame.quality_score+.25*frame.value_score
    return frame,missing,audit

def select(frame,score,n=20,max_per_sector=6):
    if frame.empty:return []
    groups=collections.Counter();chosen=[]
    for r in frame.sort_values([score,'symbol'],ascending=[False,True]).itertuples():
        if groups[r.sector]>=max_per_sector:continue
        chosen.append(r.symbol);groups[r.sector]+=1
        if len(chosen)==n:break
    return chosen

def regimens(bench):
    bs=sorted(bench)
    out={};prev=False
    for m in bs:
        prior=[bench[x] for x in bs if x<m][-12:]
        if len(prior)>=12:
            fast=sum(prior[-6:])/6;slow=sum(prior)/12
            if fast>slow*1.01:prev=True
            if fast<slow*.99:prev=False
        out[m]=prev
    return out

class TaxLoss:
    def __init__(self):self.loss=[]
    def tax(self,pnl,year):
        self.loss=[x for x in self.loss if x[0]>=year and x[1]>1e-10]
        if pnl<0:self.loss.append([year+4,-pnl]);return 0.
        x=pnl
        for v in sorted(self.loss,key=lambda z:z[0]):
            take=min(x,v[1]);v[1]-=take;x-=take
        return max(0.,x)*GAIN_TAX

class Account:
    def __init__(self,name):
        self.name=name;self.cash=CAPITAL;self.pos={};self.tax=TaxLoss();self.liability=0.;self.orders=[];self.nav=[];self.last_selection=[];self.universe_audit=[]
        self.gains_tax=0.;self.div_tax=0.;self.fees=0.;self.slippage=0.;self.rejects=collections.Counter()
    def buy(self,month,sym,p,budget,why):
        spend=min(budget,self.cash)
        if spend<=FEE+10:return
        gross=(spend-FEE)/(1+SLIP)
        if p<=0 or not math.isfinite(p):self.rejects['BAD_OPEN_BUY']+=1;return
        shares=gross/p
        self.cash-=gross*(1+SLIP)+FEE
        if self.cash < -1e-5:raise ValueError('Overdraft')
        if sym in self.pos:self.pos[sym][0]+=shares;self.pos[sym][1]+=spend
        else:self.pos[sym]=[shares,spend]
        self.orders.append([month,sym,'BUY',shares,p,FEE,gross*SLIP,0.,why]);self.fees+=FEE;self.slippage+=gross*SLIP
    def sell(self,month,sym,p,why):
        if sym not in self.pos:return
        q,basis=self.pos[sym]
        if p<=0 or not math.isfinite(p):self.rejects['BAD_OPEN_SELL']+=1;return
        gross=q*p;net=gross*(1-SLIP)-FEE;tax=self.tax.tax(net-basis,int(month[:4]));self.cash+=net-tax
        self.orders.append([month,sym,'SELL',q,p,FEE,gross*SLIP,tax,why]);self.fees+=FEE;self.slippage+=gross*SLIP;self.gains_tax+=tax
        del self.pos[sym]
    def liquidation_nav(self,closing,year):
        tax=copy.deepcopy(self.tax);val=self.cash-self.liability
        for sym,(q,basis) in self.pos.items():
            if sym not in closing:continue
            close=closing[sym];gross=q*close;net=gross*(1-SLIP)-FEE
            val+=net-tax.tax(net-basis,year)
        return val

def simulate(name,market,facts,prices,bench,factor_cache):
    a=Account(name); regime=regimens(bench)
    selected=[]
    for mm in MONTHS:
        period=pd.Period(mm,'M');prior=str(period-1);start=(mm==MONTHS[0]);new_year=(period.month==2)
        choose=(period.month in (2,5,8,11) or start)
        selection_month=(period.month==2 or start) if name.startswith('Top10') else choose
        want_regime=not ('MA6_12' in name) or bool(regime.get(mm,False))
        if selection_month:
            f=factor_cache.get(mm)
            if f is None or f.empty:a.rejects['NO_ELIGIBLE_FUNDAMENTALS']+=1;selected=[]
            elif name.startswith('Top10'):
                selected=f.sort_values('rank').symbol.head(10).tolist()
            else:
                score={'Quant20_Price':'price_score','Quant20_QualityValue':'qv_score','Quant20_QVM':'qvm_score','Quant20_QVM_MA6_12':'qvm_score'}[name]
                selected=select(f,score,n=20,max_per_sector=6)
        # Can exit existing if regime shifts, but don't rebuild annually without ranks.
        target=set(selected if want_regime else [])
        prev_close={s:float(p[prior][4]) for s,p in prices.items() if prior in p}
        # For exits, open must be on next monthly eligible session; otherwise record unavailable.
        for sym in sorted(set(a.pos)-target):
            p=prices.get(sym,{})
            if mm in p:a.sell(mm,sym,float(p[mm][2]),'MEMBERSHIP_OR_RISK_EXIT')
            else:a.rejects['MISSING_OPEN_SELL']+=1
        new=sorted(target-set(a.pos))
        if new:
            budget=a.cash/len(new)
            for sym in new:
                if mm in prices.get(sym,{}):a.buy(mm,sym,float(prices[sym][mm][2]),budget,'PIT_SELECTED')
                else:a.rejects['MISSING_OPEN_BUY']+=1
        # Estimated month dividends at ex-date; position after monthly entry is used.
        for sym,(q,basis) in a.pos.items():
            if mm in prices.get(sym,{}):
                dd=float(prices[sym][mm][6])*q
                if dd>0:a.cash+=dd*(1-DIV_TAX);a.div_tax+=dd*DIV_TAX
        endclose={}
        for sym,(q,basis) in a.pos.items():
            p=prices.get(sym,{})
            if mm not in p:a.rejects['MISSING_MONTH_END']+=1
            else:endclose[sym]=float(p[mm][4])
        if len(endclose)<len(a.pos):
            # Missing close must not get a fictitious NAV; abandon mode.
            raise RuntimeError('Missing monthly close on held name at '+mm+' '+str(set(a.pos)-set(endclose)))
        equity=sum(a.pos[s][0]*px for s,px in endclose.items())
        a.liability+=(equity*BOLLO/12.)
        n=a.liquidation_nav(endclose,period.year)
        a.nav.append([mm,n,a.cash,equity,len(a.pos),len(a.orders),a.liability,a.gains_tax,a.div_tax])
    return a

def output(market,facts,prices,bench):
    REPORTS.mkdir(parents=True,exist_ok=True)
    cache={};coverage=[];audit=[]
    for mm in MONTHS:
        if pd.Period(mm,'M').month in (2,5,8,11):
            f,miss,records=factors_for_month(mm,market,facts,prices)
            cache[mm]=f;audit.extend(records)
            coverage.append([mm,len(market['rosters'].get(str(int(mm[:4])) if int(mm[5:])>=2 else str(int(mm[:4])-1),[])),len(f),json.dumps(dict(miss),sort_keys=True)])
    # Reuse last quarterly factor frame for first-of-month if necessary
    for mm in MONTHS:
        if mm not in cache:
            prev=max((x for x in cache if x<=mm),default=None)
            cache[mm]=cache[prev] if prev else pd.DataFrame()
    outcomes={};accs={}
    for mode in MODELS:
        a=simulate(mode,market,facts,prices,bench,cache)
        accs[mode]=a
        ss=pd.Series({x[0]:x[1] for x in a.nav},dtype=float)
        years=(pd.Period(END,'M')-pd.Period(START,'M')).n/12
        dd=ss/ss.cummax()-1
        ret=ss.pct_change().dropna()
        cagr=(ss.iloc[-1]/CAPITAL)**(1/years)-1
        outcomes[mode]={
            'cagr_pct':round(100*cagr,4),'maxdd_monthly_pct':round(100*dd.min(),4),
            'final_liquidatable_eur':round(float(ss.iloc[-1]),2),'sharpe_monthly_annualized':round(float(ret.mean()/ret.std(ddof=1)*np.sqrt(12)),3) if ret.std(ddof=1)>0 else None,
            'orders':len(a.orders),'buys':sum(x[2]=='BUY' for x in a.orders),'sells':sum(x[2]=='SELL' for x in a.orders),
            'fees_eur':round(a.fees,2),'slippage_eur':round(a.slippage,2),
            'realized_tax_paid_eur':round(a.gains_tax,2),'estimated_dividend_tax_paid_eur':round(a.div_tax,2),
            'bollo_accrued_eur':round(a.liability,2),'missing_data_warnings':dict(a.rejects)}
        pd.DataFrame(a.orders,columns=['month','symbol','side','shares','open_eur','fee_eur','slippage_eur','tax_eur','reason']).to_csv(REPORTS/(mode+'_orders.csv'),index=False)
        pd.DataFrame(a.nav,columns=['month','NAV_after_hypothetical_liquidation_tax','cash','invested','positions','orders','bollo_liability','tax_realized','tax_dividends']).to_csv(REPORTS/(mode+'_nav.csv'),index=False)
    # benchmark monthly proxy is already EUR total return, tax only theoretical on liquidation
    b0=bench.get(START)
    if b0 is None:raise RuntimeError('Missing benchmark start')
    bq=(CAPITAL-FEE)/(b0*(1+SLIP));bollo=0.;vals=[]
    for m in MONTHS:
        bp=bench[m];gross=bq*bp
        bollo+=gross*BOLLO/12.
        net=gross*(1-SLIP)-FEE-bollo
        nav=net-max(0.,net-CAPITAL)*GAIN_TAX
        vals.append(nav)
    bser=pd.Series(vals);brate=bser.pct_change().dropna()
    years=(pd.Period(END,'M')-pd.Period(START,'M')).n/12
    outcomes['Synthetic_Global_Benchmark']={'cagr_pct':round(100*((vals[-1]/CAPITAL)**(1/years)-1),4),
         'maxdd_monthly_pct':round(100*(bser/bser.cummax()-1).min(),4),'final_liquidatable_eur':round(vals[-1],2),
         'sharpe_monthly_annualized':round(float(brate.mean()/brate.std(ddof=1)*np.sqrt(12)),3),
         'orders':1}
    pd.DataFrame([{'model':k,**v} for k,v in outcomes.items()]).to_csv(REPORTS/'comparison.csv',index=False)
    pd.DataFrame(coverage,columns=['selection_month','roster_eligible_universe','sec_complete_names','excluded_reasons']).to_csv(REPORTS/'coverage.csv',index=False)
    (REPORTS/'factor_audit.json.gz').write_bytes(gzip.compress(json.dumps(audit,separators=(',',':')).encode(),compresslevel=9,mtime=0))
    for mm in MONTHS:
        if mm in cache and not cache[mm].empty and mm in [x[0] for x in coverage]:
            cache[mm].to_csv(REPORTS/('factors_'+mm+'.csv'),index=False)
    checks={
        'min_sec_coverage':min([x[2] for x in coverage]) if coverage else 0,
        'median_sec_coverage':float(np.median([x[2] for x in coverage])) if coverage else 0,
        'max_sec_pub_date_le_decision':all(x['last_pub_date']<=str((pd.Period(x['decision_month'],'M')-1).end_time.date()) for x in audit),
        'first_month':START,'last_month':END,'n_decision_snapshots':len(coverage),
        'source_market_sha256':market['source_historical_prices_sha256'],
        'filing_data_sha256':hashlib.sha256((DATA/'sec_pit_facts.json.gz').read_bytes()).hexdigest(),
        'warning':'US SEC subset only; annual reconstructed membership NOT independently PIT certified. Monthly DD understates intramonth risk. Analyst consensus revisions absent. Yahoo split and dividend data are vendor sourced.'}
    (REPORTS/'audit.json').write_text(json.dumps(checks,indent=2));
    return outcomes,checks

def main():
    m,f,p,b=input_data();o,a=output(m,f,p,b)
    print('PIT_AUDIT',json.dumps(a,ensure_ascii=False),flush=True)
    print('PIT_RESULTS',json.dumps(o,ensure_ascii=False),flush=True)
    if not a['max_sec_pub_date_le_decision']:raise AssertionError('PIT leak identified')
    if a['min_sec_coverage']<20:raise RuntimeError('Insufficient SEC coverage for 20 names; do not publish performance')
if __name__=='__main__':main()