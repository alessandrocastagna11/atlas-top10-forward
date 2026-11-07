# Atlas ETF — PAC riattivato a novembre 2026

## Progetto originale recuperato

Archivio originale recuperato: ATLAS_ETF_GLOBAL_V2_3_COMPLETE_2026-10-01.zip.
L'archivio è stato estratto e verificato in locale il 7 novembre 2026:
self-test motore, validazione dataset integrato (4.853 sedute 2007–2026)
e smoke-test del motore live hanno tutti superato i controlli.

La V2.3 originale CONGELATA aveva:
- 1.000 EUR iniziali + 250 EUR mensili; 50% core / 50% satellite;
- uscita satellite per drawdown di 7,5% / 5% o Fast10 <= -10%;
- primo rebound satellite a +3 punti percentuali;
- reset di regime bull 5/10 sedute e override bear di -5pp;
- al secondo exit del satellite, score di stress su credito (Moody's
  Baa-Aaa), breadth Fama/French e tassi, valutati sul precedente mese completo;
  core OUT con score 2/3, WARNING fino a 10 sedute con score 1/3;
- capitale difensivo in Treasury/governativi quando il momentum 126 giorni è
  positivo, altrimenti cash; contributi PAC riservati se un comparto è OUT;
- backtest proxy 1926–2025: 9,3604% CAGR netto, -36,5368% MaxDD,
  XIRR 9,177%; non è un vero fondo MSCI World del 1926 né una
  certificazione out-of-sample.

L'adattatore live storico usava principalmente EUNL.DE (azionario globale)
e CSBGE3.MI (obbligazionario difensivo). NON era ancora un ledger completo
a quattro ETF 65/15/10/10.

## Riavvio operativo indipendente

Modello: ATLAS_ETF_PAC_4SLEEVE_RESTART_V1, PAPER ONLY.
Non è il frozen Stress Filter V2.3 esatto, e non gli attribuiamo
il 9,36% CAGR del backtest originale.

Target PAC approvato: 65% World, 15% Nasdaq, 10% Emerging, 10% Gold.
Prezzi quotati in EUR su Xetra:
- WORLD EUNL.DE — iShares Core MSCI World UCITS Acc;
- NASDAQ SXRV.DE — iShares NASDAQ 100 UCITS Acc;
- EM IS3N.DE — iShares Core MSCI EM IMI UCITS Acc;
- GOLD PPFB.DE — iShares Physical Gold ETC (ETC, non ETF UCITS azionario).

1.000 EUR virtuali iniziali dal 7 novembre e zero ordini retrodatati.
Prima decisione utile non prima del 9 novembre 2026. PAC 250 EUR/mese
virtuale da dicembre 2026, accreditato una volta al mese alla prima seduta
valida. Nessun invio di ordini al broker.

La ricostruzione usa per World MA10 mensile ±1% da mesi COMPLETATI,
Fast10 -10% per emergenza, stato difensivo con 50% del World core,
oro permanente al target 10%. Le esecuzioni sono simulazioni al prossimo
OPEN effettivamente osservato, con segnali registrati prima dell'apertura.
MAX tre ordini complessivi mensili, prima vendite difensive, poi acquisti
dei comparti sottopeso; vendita solo dopo almeno 35 giorni dal buy.

Assunzioni: PAC senza fee fissa, ordini strategici 1 EUR, slippage 0,05%
per lato, 26% imposta indicativa sulle plusvalenze realizzate,
bollo maturato stimato 0,20% annuo, liquidità 0%.
Nessuna leva, crypto o azioni singole. L'oro è un ETC con regime fiscale
e rischio emittente da verificare, diverso da un ETF azionario UCITS.

## Monitoraggio automatico

Workflow dal ramo main: .github/workflows/atlas-etf-pac-dispatcher.yml
20:50 e 01:20 CET (ora solare, UTC 19:50 e 00:20).
Aggiorna solo il ramo atlas-etf-pac-paper; un workflow del ramo controlla
inoltre le modifiche al codice. Task ChatGPT separato giornaliero al mattino.

File aggiornati:
- atlas_etf_pac/state.json — posizioni, flussi, ordini, segnali;
- atlas_etf_pac/reports/latest_status.json — NAV, TWR, drawdown, ordini;
- atlas_etf_pac/reports/latest_quote_audit.json — data e hash feed.
Il semplice successo dei test non prova un acquisto virtuale.

## Lavoro ancora necessario per il FULL V2.3

L'implementazione di questo ramo è una ripartenza trasparente dei quattro
comparti, non una migrazione fedele del motore storico. Servono porting
della macchina a stati originale, feed macro credito/breadth/tassi completi,
gestione difensivo bond, compensazione delle minusvalenze, validazione
di replay identico e controllo di correttezza PIT prima di dichiarare
la parità V2.3. Il monitor che gira oggi non ha tali certificazioni.
