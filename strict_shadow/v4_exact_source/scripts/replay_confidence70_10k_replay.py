from __future__ import annotations
import sys, math, gzip, pickle, re, json, importlib.util, shutil, dataclasses, warnings
from pathlib import Path
import numpy as np, pandas as pd
warnings.filterwarnings('ignore')

WORK=Path(__file__).resolve().parents[1] / 'inputs'
PKG=Path(__file__).resolve().parents[1] / 'engine' / 'ATLAS_GLOBAL_FLEX_V1_1_LIVE_INTEGRATED_RESEARCH_2026-09-22'
OUT=Path(__file__).resolve().parents[1] / 'runs' / 'confidence70_10k_replay'; OUT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(PKG/'code')); sys.path.insert(0,str(PKG/'code'/'engine'))
import incumbent_engine as inc
import equity_long_backtest as v1
import equity_long_v4_research as v4
import equity_long_bear_tax_comparison as t9

START=pd.Timestamp('2012-01-03'); END=pd.Timestamp('2025-12-10')
CAP=10000.0

def normsym(x): return re.sub(r'[^A-Z0-9]','',str(x).upper())
def aliasnorm(x):
    n=normsym(x)
    return 'GOOGLE' if n in {'GOOG','GOOGL'} else n

# ---------- exact USA engine/control ----------
base=inc.CorrectedAtlas(PKG)
idx=base.features.close_eur.index
# exact control first
ctrl_res, ctrl_m, _, _ = base.run_variant('USA100_EXACT_NO_BALANCE_CONTROL', None)
print('CONTROL', {k:ctrl_m[k] for k in ['cagr_pct','max_drawdown_pct','sharpe_excess_cash','ending_value_eur','orders']}, flush=True)
if abs(ctrl_m['cagr_pct']-10.467604537322494)>1e-9 or abs(ctrl_m['max_drawdown_pct']+13.967752332022354)>1e-9:
    raise RuntimeError('USA exact control parity failed')

inc.CAPITAL=10000.0
inc.STRENGTH_BETA=0.70  # Frozen Confidence70 sizing: control immediately above remains at baseline beta=0.35
# ---------- load prepared global panels ----------
with gzip.open(WORK / 'prepared_uniform_world.pkl.gz','rb') as fh:
    prep=pickle.load(fh)
P=prep['features']
sector_global=prep['sector']

# ---------- build monthly ROW membership: Global200 minus all current S&P 500 names ----------
um=pd.read_csv(WORK/'u300_monthly_membership.csv',parse_dates=['Date'])
ur=pd.read_csv(WORK/'usa_monthly_rank_rebuilt.csv',parse_dates=['Date'])
months=sorted(pd.Timestamp(x) for x in um.Date.unique())
# mapping global asset id -> symbol
symmap={}
for _,r in um[um.sleeve=='GLOBAL'].drop_duplicates('asset_id').iterrows(): symmap[str(r.asset_id)]=str(r.symbol)
row_assets=sorted(a for a in symmap if a in P.close_usd.columns)
row_mem=pd.DataFrame(False,index=idx,columns=row_assets)
row_counts=[]
for j,d in enumerate(months):
    nxt=months[j+1] if j+1<len(months) else END+pd.Timedelta(days=1)
    sp_alias=set(ur[ur.Date==d].ticker.map(aliasnorm))
    g=um[(um.Date==d)&(um.sleeve=='GLOBAL')].copy(); g['alias']=g.symbol.map(aliasnorm)
    names=[str(a) for a in g.loc[~g.alias.isin(sp_alias),'asset_id'] if str(a) in row_assets]
    mask=(idx>=d)&(idx<nxt)
    if names: row_mem.loc[mask,names]=True
    row_counts.append({'Date':d,'row_count':len(names),'global_count':len(g),'global_us_excluded':int(g.alias.isin(sp_alias).sum())})
# pre-start warmup use first membership, only for features
first=months[0]
first_sp=set(ur[ur.Date==first].ticker.map(aliasnorm))
g=um[(um.Date==first)&(um.sleeve=='GLOBAL')].copy(); g['alias']=g.symbol.map(aliasnorm)
first_row=[str(a) for a in g.loc[~g.alias.isin(first_sp),'asset_id'] if str(a) in row_assets]
row_factor_mem=row_mem.copy(); row_factor_mem.loc[idx<first, first_row]=True
pd.DataFrame(row_counts).to_csv(OUT/'row_membership_audit.csv',index=False)
print('ROW count min/max/mean', pd.DataFrame(row_counts).row_count.min(), pd.DataFrame(row_counts).row_count.max(), pd.DataFrame(row_counts).row_count.mean(), flush=True)

# Restrict to names ever ROW in live period
active_row=[a for a in row_assets if row_mem.loc[START:END,a].any()]
row_mem=row_mem[active_row]; row_factor_mem=row_factor_mem[active_row]

# ---------- ROW stock score: same H4 formula/weights, standardized within ROW ----------
close_local=P.close_usd[active_row].reindex(idx)
close_eur_row=P.close_eur[active_row].reindex(idx)
open_eur_row=P.open_eur[active_row].reindex(idx)
logp=np.log(close_local.where(close_local>0)); ret=logp.diff(); masked=ret.where(row_factor_mem)
market=masked.mean(axis=1,skipna=True)
secs={a:sector_global[a] for a in active_row}
secfac={}
for s in sorted(set(secs.values())):
    ns=[a for a in active_row if secs[a]==s]
    secfac[s]=masked[ns].mean(axis=1,skipna=True)
resid=pd.DataFrame(index=idx,columns=active_row,dtype=float)
for a in active_row:
    resid[a]=v1.rolling_two_factor_residual(ret[a],market,secfac[secs[a]])
res12=resid.shift(21).rolling(231,min_periods=160).sum()
res6=resid.shift(21).rolling(105,min_periods=80).sum()
raw12=logp.shift(21)-logp.shift(252); raw6=logp.shift(21)-logp.shift(126); raw3=logp.shift(21)-logp.shift(63)
sma50=close_local.rolling(50,min_periods=40).mean(); sma200=close_local.rolling(200,min_periods=160).mean()
vol63=ret.rolling(63,min_periods=42).std()*math.sqrt(252); dv20=ret.rolling(20,min_periods=15).std()
trend=np.log(close_local/sma200)/vol63.replace(0,np.nan); ext=(logp-logp.shift(5))/(dv20*math.sqrt(5)).replace(0,np.nan)
mdv=P.feature_tables['median_dollar_volume'][active_row].reindex(idx)

def rz(x):
    # exact robust cross-sectional transform, but within the current ROW opportunity set
    return v1.robust_cross_sectional_z(x.where(row_factor_mem))
row_score=(.35*rz(res12)+.20*rz(res6)+.15*rz(raw12)+.10*rz(raw6)+.05*rz(raw3)+.075*rz(trend)+.075*rz(-vol63))
scfg=v1.StrategyConfig()
row_elig=(P.close_usd[active_row].reindex(idx)>=scfg.minimum_price_usd)&(mdv>=scfg.minimum_median_dollar_volume)&(close_local>sma200)&(sma50>sma200)&(raw12>0)&(ext<=scfg.maximum_extension_z)&(ext>=scfg.minimum_extension_z)&(res12>0)&row_mem&row_score.notna()

# ---------- combine exact USA Top100 and ROW ----------
usa_cols=list(base.features.close_eur.columns)
all_cols=usa_cols+active_row
close_usd=pd.concat([base.features.close_usd[usa_cols], close_local],axis=1).reindex(idx)
close_eur=pd.concat([base.features.close_eur[usa_cols], close_eur_row],axis=1).reindex(idx)
open_eur=pd.concat([base.features.open_eur[usa_cols], open_eur_row],axis=1).reindex(idx)
membership=pd.concat([base.membership[usa_cols],row_mem],axis=1).reindex(idx).fillna(False).astype(bool)
base_score=pd.concat([base.base_score[usa_cols],row_score],axis=1).reindex(idx)
elig=pd.concat([base.eligible[usa_cols],row_elig],axis=1).reindex(idx).fillna(False).astype(bool)

# raw market: exact USA + SPY, append ROW raw columns from prepared panel
ro=base.raw.open_eur.copy(); rc=base.raw.close_eur.copy(); de=base.raw.distribution_eur_per_share.copy(); ta=base.raw.taxable_distribution.copy()
for a in active_row:
    ro[a]=prep['raw'].open_eur[a]; rc[a]=prep['raw'].close_eur[a]; de[a]=prep['raw'].distribution_eur_per_share[a]; ta[a]=prep['raw'].taxable_distribution[a]
raw_comb=t9.RawMarketData(ro,rc,de,ta,base.raw.events.copy())

# sector map: exact USA sector map + ROW analogous sectors
v1.TICKER_TO_SECTOR.update({a:secs[a] for a in active_row})

# Feature table union used by path/correlation and simulator
ft={}
for key in set(base.features.feature_tables)|{'residual_12_1','residual_6_1','raw_12_1','raw_6_1','trend_strength','extension_5','annual_vol_63','median_dollar_volume'}:
    left=base.features.feature_tables.get(key,pd.DataFrame(index=idx,columns=usa_cols)).reindex(idx).reindex(columns=usa_cols)
    if key=='residual_12_1': right=res12
    elif key=='residual_6_1': right=res6
    elif key=='raw_12_1': right=raw12
    elif key=='raw_6_1': right=raw6
    elif key=='trend_strength': right=trend
    elif key=='extension_5': right=ext
    elif key=='annual_vol_63': right=vol63
    elif key=='median_dollar_volume': right=mdv
    else: right=pd.DataFrame(index=idx,columns=active_row,dtype=float)
    ft[key]=pd.concat([left,right.reindex(idx).reindex(columns=active_row)],axis=1)

# ---------- region strength upstream: USA Top100 vs ROW in EUR ----------
def equal_weight_index(prices, mem):
    lr=np.log(prices.where(prices>0)).diff().where(mem)
    r=lr.mean(axis=1,skipna=True).fillna(0.0)
    return 100*np.exp(r.cumsum())
usa_index=equal_weight_index(base.features.close_eur[usa_cols],base.membership[usa_cols])
row_index=equal_weight_index(close_eur_row,row_mem)
logrel=np.log(usa_index/row_index)
reldaily=logrel.diff(); volrel=reldaily.rolling(252,min_periods=126).std()
z12=(logrel.shift(21)-logrel.shift(252))/(volrel*np.sqrt(231)).replace(0,np.nan)
z6=(logrel.shift(21)-logrel.shift(126))/(volrel*np.sqrt(105)).replace(0,np.nan)
region_strength=(.60*z12+.40*z6).clip(-2.5,2.5).fillna(0.0)
region_strength.rename('usa_minus_row_strength').to_csv(OUT/'region_strength.csv')
usa_index.rename('usa_top100_eq_eur').to_csv(OUT/'usa_index.csv'); row_index.rename('row_eq_eur').to_csv(OUT/'row_index.csv')


# ---------- v2: regional strength acts UPSTREAM through regional slot budgets ----------
median_n=int(round(float(membership.loc[START:END].sum(axis=1).median())))
SCALED_EXIT=max(40,int(round(.40*median_n)))
SCALED_POOL=SCALED_EXIT
print('Unified median N',median_n,'scaled exit/pool',SCALED_EXIT,flush=True)

class RegionSlotAtlas(inc.CorrectedAtlas):
    def __init__(self,*args,schedule='BALANCED',**kwargs):
        super().__init__(*args,**kwargs)
        self.schedule=str(schedule)
        self.region_slot_audit=[]

    def row_slots_at(self,date,max_positions):
        s=float(region_strength.get(date,0.0))
        if self.schedule=='ROTATE':
            # Literal upstream rotation: no RoW allocation while USA relative strength is positive.
            n = 0 if s>=0.0 else (2 if s>=-0.50 else (4 if s>=-1.00 else 6))
        elif self.schedule=='SOFT_ROTATE':
            # Small diversification allowance near parity; large RoW budget only with clear RoW leadership.
            n = 0 if s>=0.50 else (1 if s>=0.0 else (3 if s>=-0.50 else (5 if s>=-1.00 else 6)))
        else:
            raise ValueError(self.schedule)
        n=int(np.clip(n,0,max_positions)); frac=n/max_positions
        return n,s,frac

    def callbacks(self, fundamental_score=None):
        strength={}; allocator_audit=[]
        gate_stats={'blocked_entry_checks':0,'dates_with_gate':0,'unique_blocked_tickers':set(),'post_correlation_veto_violations':0}

        def choose(date, score_row, rank_row, eligible_row, positions, cfg):
            effective_eligible=eligible_row.astype(bool).copy()
            ranked=score_row.where(effective_eligible).dropna().sort_values(ascending=False)
            row_slots,rs,rfrac=self.row_slots_at(date,cfg.max_positions)
            usa_slots=cfg.max_positions-row_slots

            # 1) Minimum-hold protection is absolute for rotation. Old holdings are eligible for regional pruning.
            protected=[]; old_candidates=[]
            for ticker,pos in positions.items():
                age=(date-pos.first_entry_date).days
                valid=bool(effective_eligible.get(ticker,False)) and pd.notna(rank_row.get(ticker))
                if age < cfg.minimum_holding_calendar_days:
                    protected.append(ticker)
                elif valid and float(rank_row[ticker])<=SCALED_EXIT:
                    old_candidates.append(ticker)

            # Deduplicate and respect sector caps for protected names first.
            chosen=[]; sector_counts={}; region_counts={'USA':0,'ROW':0}
            def region(t): return 'USA' if t in usa_cols else 'ROW'
            def can_sector(t): return sector_counts.get(self.v1.TICKER_TO_SECTOR[t],0)<cfg.max_names_per_sector
            def add(t):
                if t in chosen or len(chosen)>=cfg.max_positions or not can_sector(t): return False
                chosen.append(t); sec=self.v1.TICKER_TO_SECTOR[t]; sector_counts[sec]=sector_counts.get(sec,0)+1; region_counts[region(t)]+=1; return True

            # Protected young positions cannot be sold merely due to region strength.
            for t in sorted(protected,key=lambda x: float(rank_row.get(x,np.inf))): add(t)

            # Old safe incumbents: best ranks survive, but regional target may prune excess old names.
            old_candidates=sorted(old_candidates,key=lambda x: float(rank_row.get(x,np.inf)))
            for reg,target in [('USA',usa_slots),('ROW',row_slots)]:
                # protected positions can temporarily exceed the target; do not force another region below zero.
                room=max(0,target-region_counts[reg])
                for t in [x for x in old_candidates if region(x)==reg][:room]: add(t)

            # Candidate chooser preserving score + correlation penalty. Expected-alpha floor prevents weak replacements.
            alpha_row=self.expected_alpha.loc[date]
            candidate_pool=[t for t in ranked.index[:SCALED_POOL] if t not in chosen]
            def pick_best(reg=None):
                pool=[t for t in candidate_pool if t not in chosen and (reg is None or region(t)==reg) and can_sector(t)]
                if not pool: return None
                cors=self.correlations(date,chosen,pool) if chosen else {t:0.0 for t in pool}
                best=None; bestval=-np.inf
                for t in pool:
                    av=float(alpha_row.get(t,np.nan))
                    if not np.isfinite(av): continue
                    penalty=.50*max(0.0,float(cors.get(t,0.0))-.70)/.30
                    val=float(score_row.get(t,-np.inf))-penalty
                    if val>bestval: bestval=val; best=t
                return best

            # 2) Fill the upstream regional budgets first.
            for reg,target in [('USA',usa_slots),('ROW',row_slots)]:
                while len(chosen)<cfg.max_positions and region_counts[reg]<target:
                    t=pick_best(reg)
                    if t is None: break
                    add(t)

            # 3) If a region lacks eligible names, spill unused slots to the best remaining stock globally.
            while len(chosen)<cfg.max_positions:
                t=pick_best(None)
                if t is None: break
                add(t)

            strength.clear()
            if chosen:
                values=pd.Series({t:float(score_row.get(t,np.nan)) for t in chosen}).replace([np.inf,-np.inf],np.nan)
                pct=values.rank(method='average',pct=True)
                if len(pct)>1 and pct.max()>pct.min(): pct=(pct-pct.min())/(pct.max()-pct.min())
                else: pct=pct*0+.5
                strength.update({t:float(pct[t]) for t in chosen})

            self.region_slot_audit.append({
                'decision_date':date,'region_strength':rs,'row_fraction_target':rfrac,'row_slots_target':row_slots,
                'usa_slots_target':usa_slots,'row_selected':region_counts['ROW'],'usa_selected':region_counts['USA'],
                'protected_total':len(protected),'protected_row':sum(region(t)=='ROW' for t in protected),
                'selected_total':len(chosen)
            })
            return chosen

        def weights(relative, exposure, cfg, decision_date=None):
            if decision_date is None: raise AssertionError('decision_date required')
            tickers=relative.index.tolist()
            if not tickers or exposure<=0: return pd.Series(dtype=float)
            state=float(self.pr_regime.loc[decision_date])*float(self.pr_t8.loc[decision_date]); desired=float(exposure)
            if inc.BULL_FLOOR>0 and state>=inc.BULL_THRESHOLD and desired<inc.BULL_FLOOR: desired=inc.BULL_FLOOR
            desired=min(desired,inc.MAX_EXPOSURE)
            sv=np.array([strength.get(t,.5) for t in tickers]); rv=relative.to_numpy(float)*np.exp(inc.STRENGTH_BETA*(2*sv-1))
            rv=rv/rv.sum() if rv.sum()>0 else np.full(len(rv),1/len(rv))
            caps=np.full(len(tickers),cfg.maximum_position_weight); sectors=sorted({self.v1.TICKER_TO_SECTOR[t] for t in tickers})
            feas=min(desired,float(caps.sum()),sum(min(cfg.maximum_sector_weight,float(caps[[i for i,t in enumerate(tickers) if self.v1.TICKER_TO_SECTOR[t]==s]].sum())) for s in sectors),inc.MAX_EXPOSURE)
            target=rv*feas
            from scipy.optimize import minimize
            cons=[{'type':'eq','fun':lambda w:float(w.sum()-feas)}]
            for s in sectors:
                loc=np.array([i for i,t in enumerate(tickers) if self.v1.TICKER_TO_SECTOR[t]==s]); cons.append({'type':'ineq','fun':lambda w,loc=loc:float(cfg.maximum_sector_weight-w[loc].sum())})
            x=np.minimum(target,caps)
            if x.sum()<=0: x=np.full(len(x),feas/len(x))
            else: x*=feas/x.sum()
            res=__import__('scipy').optimize.minimize(lambda w:float(np.square(w-target).sum()),x0=x,method='SLSQP',bounds=[(0,float(c)) for c in caps],constraints=cons,options={'maxiter':250,'ftol':1e-12})
            if not res.success: raise RuntimeError(res.message)
            out=pd.Series(res.x,index=tickers).clip(lower=0)
            allocator_audit.append({'decision_date':decision_date,'effective_state':state,'simulator_requested_exposure':float(exposure),'allocator_target_exposure':float(out.sum()),'bull_floor_applied':bool(state>=inc.BULL_THRESHOLD and exposure<inc.BULL_FLOOR),'risk_off_nonincrease_pass':bool(state>=inc.BULL_THRESHOLD or out.sum()<=exposure+1e-7)})
            return out
        return choose,weights,gate_stats,allocator_audit


def configure_slots(schedule):
    # no regional bonus/malus to individual stock scores.
    adj=base_score.copy()
    rank=adj.where(elig).rank(axis=1,ascending=False,method='min')
    f=v1.FeatureBundle(close_usd=close_usd,open_eur=open_eur,close_eur=close_eur,score=adj,rank=rank,eligible=elig,simple_score=adj,simple_rank=rank,simple_eligible=elig,risk_on=pd.Series(True,index=idx),feature_tables=ft)
    m=RegionSlotAtlas(PKG,schedule=schedule)
    m.features=f; m.base_score=adj; m.eligible=elig; m.membership=membership; m.raw=raw_comb
    m.base_regime=base.base_regime.copy(); m.base_t8=base.base_t8.copy(); m.bear_floor=base.bear_floor.copy() if base.bear_floor is not None else None
    m.cash_rate=base.cash_rate.copy(); m.net_cash=base.net_cash.copy(); m.tax=base.tax
    m._build_path_score()
    m.pr_regime=base.pr_regime.copy(); m.pr_t8=base.pr_t8.copy(); m.panic=base.panic.copy(); m.rebound=base.rebound.copy()
    m._returns=m.features.close_eur.pct_change(fill_method=None); m._corr_cache={}
    m.expected_alpha,_=m.v4.build_expected_alpha(m.path_score,m.features.close_eur,m.bundle['start'])
    return m

# preregistered coarse structural schedules; no fine parameter optimization.
variants=['ROTATE']
rows=[]
rows.append({**ctrl_m,'name':'USA100_EXACT_NO_BALANCE_CONTROL','variant_type':'reference','hard_exit_rank':40})
rows.append({'name':'USA100_BALANCE_REFERENCE','variant_type':'reference','cagr_pct':10.83438119454363,'max_drawdown_pct':-13.73077323715738,'sharpe_excess_cash':0.912391237209879,'ending_value_eur':419342.3046352268,'orders':600})
rows.append({'name':'V1_2_FROZEN_REFERENCE','variant_type':'reference','cagr_pct':11.304069694,'max_drawdown_pct':-14.304488103,'sharpe_excess_cash':0.92289,'ending_value_eur':444797.276935,'orders':740})
for sched in variants:
    name=f'UNIFIED_REGION_THRESHOLD_{sched}'
    print('RUN',name,flush=True)
    m=configure_slots(sched)
    res,met,gate,audit=m.run_variant(name,None)
    met.update({'name':name,'variant_type':'region_threshold','schedule':sched,'hard_exit_rank':SCALED_EXIT,'candidate_pool':SCALED_POOL,'universe_median':median_n})
    al=res.allocations.copy()
    if not al.empty:
        al['region']=np.where(al['ticker'].isin(usa_cols),'USA','ROW')
        rw=al.groupby('decision_date').apply(lambda x:x.loc[x.region=='ROW','target_weight'].sum(),include_groups=False)
        rn=al.groupby('decision_date').apply(lambda x:(x.region=='ROW').sum(),include_groups=False)
        met['avg_row_weight_pct']=100*float(rw.mean()); met['avg_row_positions']=float(rn.mean())
    aud=pd.DataFrame(m.region_slot_audit)
    if not aud.empty:
        met['avg_target_row_slots']=float(aud.row_slots_target.mean()); met['avg_realized_row_selected']=float(aud.row_selected.mean())
        met['pct_days_protected_over_target']=100*float((aud.protected_row>aud.row_slots_target).mean())
    rows.append(met)
    res.equity.rename('equity').to_csv(OUT/f'equity_{name}.csv')
    res.exposure.rename('exposure').to_csv(OUT/f'exposure_{name}.csv')
    res.orders.to_csv(OUT/f'orders_{name}.csv',index=False)
    res.allocations.to_csv(OUT/f'allocations_{name}.csv',index=False)
    aud.to_csv(OUT/f'region_slots_{name}.csv',index=False)
    print(name,{k:met.get(k) for k in ['cagr_pct','max_drawdown_pct','sharpe_excess_cash','ending_value_eur','orders','avg_row_weight_pct','avg_row_positions']},flush=True)
out=pd.DataFrame(rows); out.to_csv(OUT/'comparison.csv',index=False)
print('\nFINAL\n',out[['name','cagr_pct','max_drawdown_pct','sharpe_excess_cash','ending_value_eur','orders','avg_row_weight_pct','avg_row_positions']].to_string(index=False),flush=True)
