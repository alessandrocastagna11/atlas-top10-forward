"""Counterfactual comparison of 27 Nov decision, using 28 Nov roster.
Research only. Never invoke ledger writers. Freeze all common observations.
"""
from __future__ import annotations
import argparse, datetime as dt, hashlib, importlib.util, json, math, sys, traceback
from pathlib import Path
import numpy as np
import pandas as pd
import live_model as lm
from shared_inputs import load
from model_proposal import proposal
DAY=dt.date(2026,11,27)
STAMP=pd.Timestamp(DAY)
HERE=Path(__file__).resolve().parent
OLD_SHA='fc9dc0e207edef38b9bd308659841b8ed1ccd0fe02375eb0a74fb693356b0b92'
CAPTURE='2026-11-28T183957Z'
OUTPUT=HERE/'research_universe_ab_result.json'

def import_file(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module
    spec.loader.exec_module(module)
    return module

def finite(value):
    v=float(value)
    return v if math.isfinite(v) else None

def rank_output(m,plan):
    active=m.membership.loc[STAMP].fillna(False)
    score=m.path_score.loc[STAMP].where(active)
    eligible=(m.eligible.loc[STAMP]&active).fillna(False)
    # Use average ranks for tied scores; top eligible sorted deterministically.
    ranks=score.rank(ascending=False,method='average')
    erank=score.where(eligible).rank(ascending=False,method='min')
    scores={t:{'score':finite(score[t]),'rank':finite(ranks[t]),
               'eligible':bool(eligible[t]),'eligible_rank':finite(erank[t])}
            for t in sorted(active[active].index)}
    top=score.where(eligible).dropna().sort_index().sort_values(ascending=False,kind='stable').head(10)
    return {'scored':int(score.notna().sum()),'eligible':int(eligible.sum()),
            'top10':[{'ticker':t,'score':float(x)} for t,x in top.items()],
            'scores':scores,'proposal':plan}

def compare(a,b):
    common=sorted(set(a['scores'])&set(b['scores']))
    paired=[t for t in common if a['scores'][t]['score'] is not None and b['scores'][t]['score'] is not None]
    shifts=[{'ticker':t,'old_rank':a['scores'][t]['rank'],'new_rank':b['scores'][t]['rank'],
             'old_score':a['scores'][t]['score'],'new_score':b['scores'][t]['score']} for t in paired]
    for x in shifts:x['rank_change']=x['new_rank']-x['old_rank']
    p=pd.Series([a['scores'][t]['score'] for t in paired],dtype=float)
    q=pd.Series([b['scores'][t]['score'] for t in paired],dtype=float)
    ta={x['ticker'] for x in a['top10']};tb={x['ticker'] for x in b['top10']}
    pa={x['ticker']:x['weight_pct'] for x in a['proposal']['targets']}
    pb={x['ticker']:x['weight_pct'] for x in b['proposal']['targets']}
    return {'common_names':len(common),'common_with_finite_scores':len(paired),
            'common_scores_changed_gt_1e_8':sum(abs(x['old_score']-x['new_score'])>1e-8 for x in shifts),
            'mean_abs_rank_change':finite(np.mean([abs(x['rank_change']) for x in shifts])) if shifts else None,
            'max_abs_rank_change':max([abs(x['rank_change']) for x in shifts],default=0),
            'spearman_common_scores':finite(p.corr(q,method='spearman')) if len(paired)>1 else None,
            'eligibility_changed':sum(a['scores'][t]['eligible']!=b['scores'][t]['eligible'] for t in common),
            'common_top10_eligible':len(ta&tb),'top10_entered':sorted(tb-ta),'top10_exited':sorted(ta-tb),
            'largest_rank_changes':sorted(shifts,key=lambda x:-abs(x['rank_change']))[:15],
            'target_entered':sorted(set(pb)-set(pa)),'target_exited':sorted(set(pa)-set(pb)),
            'exposure_change_percentage_points':b['proposal']['exposure_pct']-a['proposal']['exposure_pct'],
            'target_weight_turnover_pct':.5*sum(abs(pa.get(t,0)-pb.get(t,0)) for t in set(pa)|set(pb))+
                .5*abs(sum(pa.values())-sum(pb.values()))}

def run(args,result):
    source=json.loads((HERE/'forward/latest_status.json').read_text())
    desc=source['shared_input']
    if desc['sha256']!=OLD_SHA or source['decision_session']!=str(DAY):
        raise ValueError('Frozen A snapshot not the specified Nov 27 version')
    old,frames,cash=load(HERE/'forward',desc,DAY,desc['roster_sha256'])
    last=old[-1]
    if last[0]!=DAY:raise ValueError('Unexpected baseline vintage date')
    # Preserve the exact archived vintage sequence; replace ONLY the last
    # membership for this counterfactual, not the historical calibration.
    f=lm.LIVE/'dataset/marketcap_observed/observations'/CAPTURE/'ranking.csv'
    meta=json.loads((f.parent/'source.json').read_text())
    if meta['candidate_pool_size']!=750 or meta['crosswalk_total_verified']!=711:
        raise ValueError('Unexpected expanded capture')
    rank=pd.read_csv(f).sort_values('market_cap_usd',ascending=False)
    excluded=set(source.get('short_history_excluded_from_daily_universe') or [])|set(source.get('wrong_currency_excluded_from_daily_universe') or [])
    rank=rank[~rank.symbol.isin(excluded)]
    u=rank[rank.country=='United States'].head(100).to_dict('records')
    g=rank[rank.country!='United States'].head(200).to_dict('records')
    new=old[:-1]+[(DAY,DAY,hashlib.sha256(f.read_bytes()).hexdigest(),u,g,last[5])]
    def names(v):return {str(r['symbol']).upper() for r in v[-1][3]+v[-1][4]}
    a,b=names(old),names(new)
    if len(a)!=300 or len(b)!=300:raise ValueError('Roster size mismatch')
    result.update({'baseline_snapshot_sha256':OLD_SHA,'baseline_roster_sha256':desc['roster_sha256'],
                   'counterfactual_ranking_sha256':new[-1][2],
                   'expanded_capture_observed_at':meta['observed_at_utc'],
                   'baseline_exclusions':sorted(excluded),'common':len(a&b),
                   'entrants':sorted(b-a),'exits':sorted(a-b)})
    needed={str(r['symbol']).upper() for r in u+g}
    currencies={str(r['currency']).upper() for r in u+g}|{'USD'}
    needed|={'EUR'+c+'=X' for c in currencies if c!='EUR'}
    needed|={lm.SECTOR[str(r['sector'])] for r in u+g}
    missing=sorted(needed-set(frames))
    result['additional_frames_required']=missing
    # Only missing instruments are downloaded; never refresh common data.
    additional,errors=lm.get_adjusted(missing,DAY,max_workers=6,require_current_us=missing)
    result['additional_frames_downloaded']=sorted(additional)
    result['download_errors']=errors
    result['additional_frame_provenance']={s:{'first':str(q.index[0].date()),'last':str(q.index[-1].date()),
          'observed_at_utc':str(q.iloc[-1].get('observed_at_utc','UNKNOWN')),
          'source_sha256':str(q.iloc[-1].get('source_sha256','UNKNOWN'))} for s,q in additional.items()}
    if errors or set(additional)!=set(missing):
        raise ValueError('Missing additional adjusted series; no fabricated A/B scores')
    frames={**frames,**additional}
    # Require correct currency and sufficient history, even for entrants
    # present in the frozen archive from earlier roster vintages.
    for r in u+g:
        s=str(r['symbol']).upper();q=frames[s]
        if len(q)<300:raise ValueError('Short history '+s)
        if 'quote_currency' not in q or set(q.quote_currency.dropna())!={str(r['currency']).upper()}:
            raise ValueError('Currency mismatch '+s)
    local=lm.check_required_local_closes(frames,b|{'SPY'},DAY,dt.datetime.now(dt.timezone.utc))
    result['local_eod_audit']=local
    if local.get('stale_or_missing'):raise ValueError('Stale new roster quote')
    hybrid=import_file('ab_hybrid',args.hybrid)
    # Top5 imports live_model from sys.modules; checkout source is explicitly
    # the same frozen module used by both variants.
    top5=import_file('ab_top5',args.top5)
    result['engines']={}
    for engine in ('Confidence70','V6.6 Hybrid','Top5 MOM10'):
        versions={}
        for label,vintages in [('A',old),('B',new)]:
            lm.START_PAPER=top5.START if engine=='Top5 MOM10' else pd.Timestamp('2026-11-10')
            m,provenance=lm.build_model(vintages,DAY,frames,shared_cash_rate=cash)
            if int(m.membership.loc[STAMP].sum())!=300:raise ValueError('Effective roster not 300')
            if engine=='V6.6 Hybrid':
                m.path_score,reg=hybrid.hybrid_score(m.path_score,m.features.close_eur,m.membership,
                    {t:lm.v1.TICKER_TO_SECTOR[t] for t in m.path_score.columns},m.pr_regime,m.pr_t8,lm.v1.robust_cross_sectional_z)
            if engine=='Top5 MOM10':
                cfg=top5.setup(m,vintages)
                targets=top5.choose_targets(m,cfg,STAMP,{})
                plan={'targets':[{'ticker':t['ticker'],'weight_pct':100*t['target_weight']} for t in targets],
                      'exposure_pct':100*sum(t['target_weight'] for t in targets),
                      'indicative_buys':None}
            else:
                p=proposal(m,provenance,STAMP,positions={},nav_eur=10000.)
                plan={'targets':[{'ticker':t['ticker'],'weight_pct':t['weight_pct']} for t in p['possible_candidates']],
                      'exposure_pct':p['target_total_equity_value_eur']/100,
                      'indicative_buys':p['possible_buys_meeting_threshold'],
                      'regime_multiplier':p['regime_multiplier']}
            versions[label]=rank_output(m,plan)
        result['engines'][engine]={'A':versions['A'],'B':versions['B'],'comparison':compare(versions['A'],versions['B'])}
        save(result)
    result['status']='COMPLETE_COUNTERFACTUAL_NO_FILLS'

def save(result):
    OUTPUT.write_text(json.dumps(result,indent=2,allow_nan=False,default=str)+'\n')

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--hybrid',type=Path,required=True)
    parser.add_argument('--top5',type=Path,required=True)
    args=parser.parse_args()
    result={'status':'RUNNING','session':str(DAY),'research_only':True,'real_orders':0,
            'portfolio_assumption':'Identical empty 10000 EUR portfolio in both variants; no fills',
            'limitations':['Expanded ranking was observed Nov 28, after decision session; counterfactual not PIT.',
                           'New instruments fetched after cutoff, truncated to Nov 27; adjustment history may have been revised.',
                           'Common instruments, FX and cash frozen whenever present. No realised return or backtest CAGR inferred.']}
    try:run(args,result)
    except Exception as exc:
        result.update(status='BLOCKED',error_type=type(exc).__name__,reason=str(exc)[:1000])
        raise
    finally:save(result)
