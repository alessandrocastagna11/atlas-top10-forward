"""Read-only-to-strategy daily PAPER portfolio/reporting, separate from Cohort Proxy.

Never synthesizes fills or NAV values for unobserved market sessions. All holdings
are copied from the exact 35-calendar-day FIFO live simulator after parity audit.
"""
from __future__ import annotations
import csv, datetime as dt, json, math
from pathlib import Path

ORDER_FIELDS=['scenario','strategy','decision_date','date','action','ticker','shares','reference_price_eur',
              'execution_price_eur','reference_value_eur','fee_eur','slippage_bps_per_side',
              'capital_gains_tax_eur','reason','order_id','execution_mode','price_source_date',
              'price_quality','simulation_recorded_at_utc','paper_only','real_market_fill']


def to_portfolio(result,model,meta):
    """Snapshot after a verified model replay: never reconstruct fills from prices."""
    date=result.equity.index[-1]
    shares=[]
    invested=0.0
    for ticker,pos in sorted(result.paper_positions.items()):
        if ticker not in model.raw.close_eur.columns:
            raise ValueError('Missing closing quote for held ticker: '+ticker)
        quote=float(model.raw.close_eur.at[date,ticker])
        if not math.isfinite(quote) or quote<=0:
            raise ValueError('Stale/invalid last close for held ticker '+ticker)
        qty=float(pos['shares'])
        lots=pos['lots']
        if abs(sum(float(z['shares']) for z in lots)-qty)>max(1e-7,qty*1e-8):
            raise ValueError('FIFO lot inventory does not reconcile for '+ticker)
        value=qty*quote
        invested+=value
        eligible=sum(float(z['shares']) for z in lots if dt.date.fromisoformat(z['sale_eligible_date'])<=date.date())
        shares.append({'ticker':ticker,'market_symbol':meta['ticker_to_market_symbol'].get(ticker,ticker),
                       'shares':qty,'cost_basis_eur':round(float(pos['cost_basis_eur']),4),
                       'market_close_eur':round(quote,8),'market_value_eur':round(value,4),
                       'portfolio_weight_pct':None,
                       'sell_eligible_shares':round(eligible,10),
                       'locked_shares':round(max(0,qty-eligible),10),
                       'lots':lots})
    cash=float(result.paper_cash_eur)
    marked_nav=cash+invested
    if marked_nav<=0 or not math.isfinite(marked_nav):raise ValueError('Invalid mark-to-market NAV')
    for pos in shares:pos['portfolio_weight_pct']=round(pos['market_value_eur']/marked_nav*100,3)
    net_liquidation=float(result.equity.iloc[-1])
    if not math.isfinite(net_liquidation):raise ValueError('Invalid net-liquidation NAV')
    # Net hypothetical liquidation NAV can differ from mark-to-market due to unrealized tax.
    # Do not interpret the hypothetical end-sale as a real order or sell-eligibility override.
    out={'schema_version':2,'type':'PAPER_ONLY','engine_id':'ATLAS_UNIFIED_REGION_ROTATE_V4_CONFIDENCE_BETA070_2026-10-07',
         'variant':'CONFIDENCE70_ORIGINAL_H4_STRICT_35_CALENDAR_DAYS_EVERY_LOT',
         'valuation_session':str(date.date()),'data_source_asof':meta.get('roster_source_asof'),
         'universe_source':meta.get('universe_kind'),'universe_market_cap_pit_certified':False,
         'initial_capital_eur':10000.,'cash_eur':round(cash,4),'invested_eur':round(invested,4),
         'nav_mark_to_market_eur':round(marked_nav,4),'nav_net_liquidation_eur':round(net_liquidation,4),
         'pnl_mark_to_market_pct':round((marked_nav/10000-1)*100,4),
         'positions_count':len(shares),'holdings':shares,'strict_min_holding_calendar_days':35,
         'orders_total':len(result.orders),'buy_orders':int((result.orders.get('action')=='BUY').sum()) if not result.orders.empty else 0,
         'sell_orders':int((result.orders.get('action')=='SELL').sum()) if not result.orders.empty else 0,
         'blocked_or_partial_sell_requests':int(result.blocked_sell_count),
         'blocked_notional_eur_repeated_requests':round(float(result.blocked_sell_notional_eur),4),
         'real_money_orders':0}
    if len(shares)>11:raise ValueError('Position cap violated')
    return out


def initial_portfolio():
    return {'schema_version':2,'type':'PAPER_ONLY','variant':'CONFIDENCE70_ORIGINAL_H4_STRICT_35_CALENDAR_DAYS_EVERY_LOT',
            'valuation_session':None,'initial_capital_eur':10000.,'cash_eur':10000.,'invested_eur':0.,
            'nav_mark_to_market_eur':10000.,'nav_net_liquidation_eur':10000.,'pnl_mark_to_market_pct':0.,
            'positions_count':0,'holdings':[],'strict_min_holding_calendar_days':35,
            'orders_total':0,'buy_orders':0,'sell_orders':0,'real_money_orders':0,
            'universe_market_cap_pit_certified':False}


def write_report(out_dir,status,portfolio,orders):
    """Only present confirmed ledger; stale data explicitly labelled."""
    out_dir=Path(out_dir);out_dir.mkdir(parents=True,exist_ok=True)
    source=status.get('source') or {}
    status_text=status.get('status','PENDING_DATA')
    asof=portfolio.get('valuation_session')
    if status_text=='PAPER_APPROX_LEDGER_ACTIVE':
        recency='PAPER_LAST_EOD_CLOSE_APPROXIMATE_FILLS_NOT_REAL_BROKER_ORDERS'
    elif status_text!='PAPER_LEDGER_VERIFIED':
        recency='INITIAL_CASH_ONLY_NOT_MARKET_VALUED' if not asof else 'LAST_VERIFIED_PORTFOLIO_NOT_NEW_VALUATION'
    else:
        recency='CURRENT_VERIFIED_MODEL_VALUATION'
    def money(value):return f"{float(value or 0):,.2f} €"
    pending=status.get('pending_full_rotation') or {}
    fx=(status.get('fx_audit') or {})
    valuation=status.get('valuation_audit') or {}
    next_review=status.get('next_full_rotation_decision_date') or 'da calendario'
    lines=[
        '# Atlas V4 Confidence70 — paper originale, vincolo 35 giorni',
        '',f"**Stato:** `{status_text}` — **Validità della valutazione:** `{recency}`",
        '',f"**Motore segnali:** {status.get('signal_engine_status','NON_VERIFICATO')}; errore: {status.get('signal_engine_error') or 'nessuno'}",
        f"**Capitalizzazione osservata:** {status.get('universe_observed_capture_day') or 'non disponibile'}; copertura USA/RoW: {status.get('universe_observed_marketcap_usa_count','n/d')}/{status.get('universe_observed_marketcap_nonusa_count','n/d')}; NON PIT storico",
        f"**Titoli sostituiti per storico insufficiente:** {', '.join(status.get('short_history_excluded_from_daily_universe',[])) or 'nessuno'}",
        '',f"**Ultima sessione decisionale:** {status.get('decision_session') or 'non disponibile'}",
        f"**Ultima sessione valorizzata:** {asof or 'non ancora avviata'}",
        f"**Dati mercato / universo:** {source.get('universe_kind') or 'in attesa'}; fotografia utilizzata al {source.get('roster_recorded_date') or 'n/d'}",
        f"**Universo dinamico:** {status.get('historical_universe_vintages',0)} fotografie cronologiche, ultima disponibile {status.get('latest_available_roster_recorded_date') or 'n/d'}; nuove società eleggibili SOLO dalla data registrata",
        f"**Primo segnale / apertura successiva:** {(status.get('next_preopen') or {}).get('decision_session','n/d')} / {(status.get('next_preopen') or {}).get('execution_session','n/d')}",
        f"**Candidati segnalati (NON ORDINI):** {', '.join((status.get('next_preopen') or {}).get('preopen_selected_symbols',[])) or 'nessuno'}; verifica: {(status.get('next_preopen') or {}).get('status','n/d')}",
        f"**Segnale originale pubblicato:** {'SÌ' if status.get('signal_published') else 'in attesa'}; esito: {status.get('signal_status','n/d')}",
        f"**Contabilità paper:** {status.get('execution_policy','attesa')} — prezzi degli ordini {'APPROSSIMATI' if status.get('execution_pricing_approximate') else 'non verificati'}; nessun ordine reale.",
        f"**Ultimo aggiornamento portafoglio:** {portfolio.get('valuation_recorded_at_utc','n/d')}; fonte ultimo prezzo {portfolio.get('valuation_session','n/d')}.",
        f"**Recuperi dopo interruzioni:** {status.get('recovered_paper_decision_count',0)} sedute; {status.get('new_orders_mode','in attesa')}",
         '',
         '## Segnali, proposte e ordini: tre cose diverse',
         '',
         f"**Segnale giornaliero:** {'pubblicato' if status.get('signal_published') else 'non disponibile'} — NON equivale a un ordine.",
         f"**Proposte sopra soglia (solo indicazioni):** {(status.get('indicative_order_test') or {}).get('possible_buys_meeting_threshold','n/d')}.",
         f"**Rotazione completa:** ogni 5 settimane; prossimo lunedi teorico: {next_review}; non sono previsti nuovi acquisti quotidiani.",
         f"**Ordini registrati nell'ultimo aggiornamento:** {int(status.get('new_orders',0) or 0)}.",
         f"**Rotazione differita a causa dei 35 giorni:** {'NESSUNA' if not pending else str(pending.get('original_decision_session'))+' / '+', '.join(pending.get('blocked_tickers',[]))}.",
         '',
         '## Audit delle valutazioni e cambi',
         '',
         f"**Numero di chiusure di mercato distinte:** {valuation.get('historical_daily_sessions','n/d')}; **revisioni di valutazione:** {valuation.get('valuation_revision_count',0)}.",
         f"**Revisione della stessa chiusura durante il run:** {'SI, vedere valuation_revisions.csv' if valuation.get('valuation_revised_in_current_run') else 'NO'}. Una revisione non genera una seconda giornata di rendimento.",
         f"**Cambio EUR/USD:** {fx.get('fx_rates',{}).get('USD','n/d')} USD per EUR; seduta FX {fx.get('fx_quote_sessions',{}).get('USD','n/d')}. Il fixing giornaliero non e un cambio di broker.",
         f"**Fingerprint dei dati:** {valuation.get('quote_evidence_sha256','n/d')}.",
        '**Attenzione:** l’universo ACWI attuale è un proxy, non la classifica originale PIT per capitalizzazione.',
        '',
        '| Portafoglio virtuale | Importo |', '|---|---:|',
        f"| Capitale iniziale | {money(portfolio.get('initial_capital_eur',10000))} |",
        f"| NAV netto liquidabile ipotetico* | {money(portfolio.get('nav_net_liquidation_eur',10000))} |",
        f"| NAV a prezzi di mercato | {money(portfolio.get('nav_mark_to_market_eur',10000))} |",
        f"| Liquidità | {money(portfolio.get('cash_eur',10000))} |",
        f"| Valore titoli | {money(portfolio.get('invested_eur',0))} |",
        f"| Posizioni | {portfolio.get('positions_count',0)} |",
        f"| Ordini complessivi | {portfolio.get('orders_total',0)} |",
        f"| Nuovi ordini verificati | {int(status.get('new_orders',0) or 0)} |",
        f"| Sedute paper ricostruite dopo interruzione | {int(status.get('recovered_paper_decision_count',0) or 0)} |",
        '',
        '*NAV netto liquidabile: vendita simultanea IPOTETICA dopo lo sblocco dei lotti, con compensazione delle minusvalenze azionarie ammissibili e costi teorici. Non equivale a una vendita attualmente consentita dal blocco FIFO.',
        '',
        '## Posizioni e lotti',
        '', '| Titolo | Quote | Valore € | Peso | Quote vincolate | Prima data vendibile |',
        '|---|---:|---:|---:|---:|---|',
    ]
    for p in portfolio.get('holdings',[]):
        locked=[l['sale_eligible_date'] for l in p.get('lots',[]) if l['sale_eligible_date']>(asof or '9999-12-31')]
        lines.append(f"| {p['ticker']} | {p['shares']:.6g} | {p['market_value_eur']:,.2f} | {p['portfolio_weight_pct']:.2f}% | {p['locked_shares']:.6g} | {min(locked) if locked else 'già vendibile'} |")
    if not portfolio.get('holdings'):lines.append('| — Nessuna posizione | — | — | — | — | — |')
    lines.extend(['','## Piano delle aperture per singolo mercato','','| Titolo | Primo mercato utile | Stato del segnale | Prossima apertura locale prevista (UTC)* |', '|---|---|---|---|'])
    plans=(status.get('next_preopen') or {}).get('per_symbol_exchange_plan',{})
    for symbol,item in sorted(plans.items()):
        lines.append(f"| {symbol} | {item.get('first_candidate_session','n/d')} | {item.get('status','n/d')} | {item.get('next_eligible_open_utc','n/d')} |")
    if not plans:lines.append('| — Non ancora disponibile | — | — | — |')
    lines.extend(['','*Orari indicativi: festività, sospensioni e quotazioni reali devono essere verificate dal provider. Il segnale non equivale a un acquisto; un mercato già aperto non autorizza esecuzioni retroattive.*',''])
    lines.extend(['','## Ultime operazioni paper registrate','','| Seduta simulata | Tipo | Titolo | Quote | Prezzo € | Metodo / fonte |', '|---|---|---|---:|---:|---|'])
    for o in orders[-15:][::-1]:
        mode=('PROXY ultima chiusura '+str(o.get('price_source_date','n/d')) if o.get('execution_mode') else str(o.get('reason','')))
        lines.append(f"| {str(o.get('date',''))[:10]} | {o.get('action','')} | {o.get('ticker','')} | {float(o.get('shares',0)):.6g} | {float(o.get('execution_price_eur',0)):.4f} | {mode} |")
    if not orders:lines.append('| — Nessuna operazione | — | — | — | — | — |')
    if status.get('recovered_paper_decision_dates'):
        lines+=['','**Nota recupero:** operazioni ricostruite ex post con prezzi storici, NON esecuzioni osservate in diretta.',
                'Date di decisione ricostruite: '+', '.join(status['recovered_paper_decision_dates'])]
    lines+=['','**Politica ordini:** minimo 35 giorni CALENDARIO su ogni lotto, anche vendite difensive; controlli di rischio settimanali e rotazione ogni 5 settimane; nessun ordine reale.','',
            'Il workflow aggiorna i dati e verifica il modello, ma non garantisce che ogni giorno vi sia un acquisto o una vendita.',
            '',f"**Eventuale blocco:** {status.get('reason','nessuno') or 'nessuno'}",'']
    (out_dir/'latest.md').write_text('\n'.join(lines),encoding='utf8')
    with (out_dir/'orders.csv').open('w',encoding='utf8',newline='') as h:
        w=csv.DictWriter(h,fieldnames=ORDER_FIELDS,extrasaction='ignore')
        w.writeheader();w.writerows(orders)
    return recency
