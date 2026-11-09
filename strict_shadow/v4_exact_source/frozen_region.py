"""Verbatim RegionSlotAtlas from the archived 2012-2025 Confidence70 backtest.
Only module imports/variable injection are external; the class body is unchanged.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import math
import scipy
import incumbent_engine as inc
import equity_long_backtest as v1
region_strength=None
usa_cols=None
SCALED_EXIT=None
SCALED_POOL=None

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

