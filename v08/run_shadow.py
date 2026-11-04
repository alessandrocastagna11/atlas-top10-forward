"""Daily research-only Atlas v0.8 runner. No mutation to official v0.7 state."""
from __future__ import annotations
import argparse,copy,csv,hashlib,json,os,sys
from datetime import date,datetime,timedelta,time,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import shadow_engine as se

ROOT=Path(__file__).resolve().parent
STATE=ROOT/'state';REPORTS=ROOT/'reports';MARKET=ROOT/'market_data';ROSTERS=ROOT/'rosters'

def atomic(path,content):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(content,encoding='utf-8');tmp.replace(path)

def save_json(path,obj):atomic(path,json.dumps(obj,indent=2,ensure_ascii=False,sort_keys=True)+'\n')

def init():
    STATE.mkdir(exist_ok=True);REPORTS.mkdir(exist_ok=True);MARKET.mkdir(exist_ok=True)
    for name in se.STRATEGIES:
        p=STATE/f'{name}.json'
        if not p.exists():save_json(p,se.seed(name))

def load(name):return json.loads((STATE/f'{name}.json').read_text(encoding='utf8'))

def date_range(after,until):
    d=date.fromisoformat(after)+timedelta(days=1)
    while d<=date.fromisoformat(until):
        yield d.isoformat()
        d+=timedelta(days=1)

def validate_roster(asof):
    # A historical roster can only become valid after its publication date,
    # never reconstructed retrospectively from today's membership.
    if asof<'2027-02-01':return list(se.BASKET),True,None
    vintage=int(asof[:4])-1
    p=ROSTERS/f'{vintage}.json'
    if not p.exists():return list(se.BASKET),False,'ROSTER_NOT_VERIFIED'
    r=json.loads(p.read_text())
    syms=r.get('symbols',[])
    if len(syms)!=10 or len(set(syms))!=10:return list(se.BASKET),False,'ROSTER_INVALID'
    if not r.get('source_url') or not r.get('snapshot_asof') or not r.get('published_at'):
        return list(se.BASKET),False,'ROSTER_MISSING_PROVENANCE'
    if r['published_at']>asof or r['snapshot_asof']>r['published_at']:
        return list(se.BASKET),False,'ROSTER_LOOKAHEAD'
    identities=r.get('securities',{})
    for sym in syms:
        if sym not in se.CURRENCIES:
            meta=identities.get(sym,{})
            if not meta.get('issuer_key') or not meta.get('currency') in ('USD','SAR','EUR') or not meta.get('venue'):
                return list(se.BASKET),False,'ROSTER_NEW_SECURITY_WITHOUT_VERIFIED_IDENTITY'
            se.CURRENCIES[sym]=meta['currency']
            se.SECURITY_IDS[sym]=meta
    return syms,True,None

def download(asof):
    out={};errors={}
    ticker_list=list(se.SYMBOLS)
    if asof>='2027-02-01':
        roster,valid,_=validate_roster(asof)
        if valid:
            ticker_list=list(dict.fromkeys(ticker_list+roster))
    for sym in ticker_list:
        try:out[sym]=se.retrieve_yahoo(sym,end=asof)
        except Exception as e:errors[sym]=str(e)[:220]
    return out,errors

def source_status(errors,asof):
    return {'asof':asof,'feed':'Yahoo delayed chart public data, non certified',
            'missing_symbols':errors}

def replay(asof,data,errors=None):
    init();errors=errors or {}
    result={}
    for name in se.STRATEGIES:
        state=load(name)
        before=json.dumps(state,sort_keys=True)
        failed=[];processed=0
        for d in date_range(state['last_date'],asof):
            # Missing data means no fabricated price or fill. Still permit a
            # partial session when a single instrument feed is unavailable by
            # retaining any last known market data snapshot.
            trial=copy.deepcopy(state)
            try:
                roster,valid,roster_error=validate_roster(d)
                x=se.advance(trial,data,d,roster=roster,roster_verified=valid)
                if x is None:continue
                se.validate(trial)
                state=trial;processed+=1
            except (ValueError,KeyError,AssertionError,TypeError,ZeroDivisionError) as e:
                failed.append({'date':d,'error':str(e)[:250]})
                # If a position cannot be valued, stop here. Don't jump over
                # the unvalued period or retroactively manufacture a trade.
                if state['positions'] or state['benchmark']['shares']:
                    break
        if state['last_date']!=json.loads(before)['last_date']:
            save_json(STATE/f'{name}.json',state)
        last=state['nav'][-1]
        report_status='PENDING_FIRST_OPEN' if not state['initialized'] else ('PENDING_DATA' if failed or last['date']<asof and processed==0 else last['status'])
        weights={}
        if state['positions']:
            try:
                vals=se.value_positions(state,data,last['date']);tot=sum(vals.values())
                weights={s:round(v/tot,5) for s,v in vals.items()} if tot else {}
            except Exception as e: failed.append({'date':last['date'],'error':'WEIGHT_AUDIT '+str(e)[:160]})
        result[name]=dict(status=report_status,requested_asof=asof,last_nav_date=last['date'],
           nav_eur=last['nav'],benchmark_eur=last['benchmark'],cum_pct=last['return_pct'],
           benchmark_cum_pct=last['benchmark_return_pct'],excess_pp=round(last['return_pct']-last['benchmark_return_pct'],5),
           exposure_target=state['target_exposure'],cash_eur=state['cash'],positions=len(state['positions']),
           orders=len(state['orders']),buys=sum(o['side']=='BUY' for o in state['orders']),
           sells=sum(o['side']=='SELL' for o in state['orders']),
           realized_tax_eur=round(sum(o['tax_eur'] for o in state['orders']),4),
           estimated_div_tax_eur=round(state['dividend_tax'],4),bollo_eur=round(state['bollo_liability'],4),
           commission_eur=round(sum(o['commission_eur'] for o in state['orders']),4),
           venue_fees_eur=round(sum(o['venue_fee_eur'] for o in state['orders']),4),
           pending_tasks=state['pending'],weight_concentration=weights,
           top3_weight=round(sum(sorted(weights.values(),reverse=True)[:3]),5),
           errors=failed[:6],data_missing=list(errors),
           last_signal=state['signal_log'][-1] if state['signal_log'] else None,
           warnings=state['warnings'])
        write_csv(REPORTS/f'{name}_nav.csv',state['nav'])
        write_csv(REPORTS/f'{name}_orders.csv',state['orders'])
    return result

def write_csv(path,rows):
    if not rows:return
    cols=list(dict.fromkeys(k for row in rows for k in row))
    import io
    buf=io.StringIO();w=csv.DictWriter(buf,fieldnames=cols);w.writeheader();w.writerows(rows);atomic(path,buf.getvalue())

def render(asof,r,source):
    lines=[f'# Atlas v0.8 — SHADOW research EOD {asof}','','**I portafogli ufficiali v0.7 non sono stati modificati.**',
      '**Prima data di ingresso autorizzata:** 2026-11-05, al primo OPEN successivo al congelamento delle regole.',
      '**Dati:** Yahoo delayed chart 1d (non certificati), FX della giornata precedente per l’ordine; zero ordini reali.',
      '', '| Ricerca | Stato | NAV € | IUSQ € | Rendimento % | Extra pp | Esposizione | Ordini |',
      '|---|---|---:|---:|---:|---:|---:|---:|']
    for key,v in r.items():
        lines.append(f"| {key} | {v['status']} | {v['nav_eur']:,.2f} | {v['benchmark_eur']:,.2f} | {v['cum_pct']:+.2f} | {v['excess_pp']:+.2f} | {v['exposure_target']:.1%} | {v['orders']} |")
    lines+=['','**Esecuzione:** segnale dal mese completo precedente; simulazione al primo OPEN della seduta idonea per ciascun titolo e slippage dello 0,1%. Il prezzo OPEN è osservato ex post e non è una garanzia di esecuzione reale.',
       '**Costi:** 1 € per operazione, commissione saudita extra 15,5 bps stimata, 26% plusvalenze realizzate con perdite compensabili 4 anni, dividendi 26% proxy, bollo 0,2% annuo maturato.',
       '**Limiti:** split, prezzo dati e dividend withholding non certificati; classifica annuale richiede fonti e timestamp verificati; rischio di concentrazione delle prime dieci società.',
       '']
    for key,v in r.items():
        lines.extend([f'## {key}',f"- Valutazione più recente: **{v['last_nav_date']}**, richiesta: **{asof}**; peso primi tre: **{v['top3_weight']:.1%}**.",
           f"- Acquisti {v['buys']}, vendite {v['sells']}; imposte plusvalenze €{v['realized_tax_eur']:.2f}; bollo maturato €{v['bollo_eur']:.2f}."])
        if v['errors']:lines.append('- **Verifica incompleta:** '+json.dumps(v['errors'],ensure_ascii=False))
        if v['pending_tasks']:lines.append(f"- Ordini ancora in attesa di apertura/fonte: **{len(v['pending_tasks'])}**")
        if v['warnings']:lines.append('- **Avvisi:** '+', '.join(v['warnings']))
        lines.append('')
    if source['missing_symbols']:lines.append('**Feed incompleti:** '+json.dumps(source['missing_symbols'],ensure_ascii=False))
    atomic(REPORTS/'latest.md','\n'.join(lines)+'\n')


def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument('--asof',default=datetime.now(ZoneInfo('America/New_York')).date().isoformat())
    ap.add_argument('--seed',action='store_true')
    ap.add_argument('--test',action='store_true')
    args=ap.parse_args(argv)
    if args.asof>datetime.now(ZoneInfo('America/New_York')).date().isoformat():raise SystemExit('future asof forbidden')
    init()
    if args.seed:
        res=replay(se.START,{})
        src={'asof':se.START,'missing_symbols':{},'feed':'NO TRADES: SEED'}
        save_json(REPORTS/'latest_status.json',{'asof':se.START,'type':'SHADOW','results':res,'source':src})
        render(se.START,res,src)
        return
    if args.asof<se.FIRST_DAY:
        return main(['--seed'])
    now=datetime.now(ZoneInfo('America/New_York'))
    if args.asof==now.date().isoformat() and now.time()<time(17,30):
        print('PENDING_US_CLOSE; no read/write to ledger')
        return
    data,errors=download(args.asof)
    # A full feed failure or missing benchmark/FX blocks the entire update.
    for essential in (se.BENCH,'EURUSD=X','EURSAR=X'):
        if essential in errors:
            save_json(REPORTS/'latest_status.json',{'asof':args.asof,'type':'SHADOW','status':'PENDING_DATA','errors':errors})
            print('PENDING_DATA essential source: '+essential);return
    # Some stock feeds can be absent as long as the stock is not held; a
    # previously-held stock lacking any historical price stops update safely.
    if errors:
        cache=MARKET/'last_snapshot.json'
        if cache.exists():
            saved=json.loads(cache.read_text()).get('data',{})
            for sym in list(errors):
                if sym in saved:
                    data[sym]=saved[sym]
    symbols=list(dict.fromkeys(se.BASKET+([*validate_roster(args.asof)[0]] if args.asof>='2027-02-01' else [])))
    for sym in symbols:
        if sym not in data:
            # An explicit empty bar series keeps buy task pending, rather than
            # fabricating quote or rejecting all other independent sessions.
            data[sym]={'bars':{},'currency':se.CURRENCIES[sym],'dividends':{},'splits':{},'feed':'MISSING_PROVIDER'}
    raw=json.dumps(data,sort_keys=True,ensure_ascii=False).encode('utf8')
    digest=hashlib.sha256(raw).hexdigest()
    res=replay(args.asof,data,errors)
    src=source_status(errors,args.asof);src['sha256']=digest
    # Immutable daily source digest + exact raw quotes used to mark that day.
    audit=MARKET/'audit';audit.mkdir(parents=True,exist_ok=True)
    compact={sym:{'bar':rec['bars'].get(args.asof), 'currency':rec.get('currency'),
            'retrieved_at_utc':rec.get('retrieved_at_utc'), 'identity':rec.get('identity')}
            for sym,rec in data.items()}
    name=f'{args.asof}.json';target=audit/name
    if target.exists():
        previous=json.loads(target.read_text())
        if previous.get('sha256')!=digest:target=audit/f'{args.asof}-{digest[:12]}.json'
    if not target.exists():
        save_json(target,{'asof':args.asof,'sha256':digest,'source':'YAHOO_CHART_DELAYED',
            'quotes_asof':compact})
    save_json(MARKET/'last_snapshot.json',{'asof':args.asof,'sha256':digest,'data':data})
    save_json(REPORTS/'latest_status.json',{'asof':args.asof,'type':'SHADOW','results':res,'source':src})
    render(args.asof,res,src)
    print(json.dumps({k:{i:r[i] for i in ('status','nav_eur','benchmark_eur','orders','exposure_target')} for k,r in res.items()},indent=2))

if __name__=='__main__':main()