# Atlas Top 10 — test retrospettivo di avvio al 5 novembre 2026

**NON È IL FORWARD UFFICIALE**. Usa lo snapshot scaricato dopo le sedute e ricrea
ipotetici acquisti al CLOSE 4 novembre, senza affermare che tali esecuzioni
sarebbero state realizzabili esattamente a quel prezzo. Il ledger ufficiale resta immutato.

L'universo è stato congelato il 3 novembre: la simulazione metodologicamente
ammissibile con il paniere v0.7 attuale riguarda il 4–5 novembre, non il 2025.

| Strategia | Stato | NAV finale € | Benchmark finale € | P/L % | Ordini azionari | Commissioni € |
|---|---|---:|---:|---:|---:|---:|
| MA10_Official | COMPLETE_DIAGNOSTIC | 9,426.76 | 9,752.30 | -5.732% | 10 | 10.00 |
| MA6_MA12_Challenger | COMPLETE_DIAGNOSTIC | 9,426.76 | 9,752.30 | -5.732% | 10 | 10.00 |

Fonte: `market_data/latest_snapshot.json`; hash sha256 dei dati: `b88bc68e0dd1e79290e4e60e55615b902726c062466fe53b38109d2faa8a1fdc`.

IUSQ.DE del 5 novembre proviene dal singolo snapshot Yahoo: contrasto con altre fonti
non risolto. Esecuzioni storiche dei soli prezzi di CLOSE, non prove di eseguibilità.

Un backtest pluriennale di v0.8 fino al 2025 è separato; non è stato esteso
al 2026 con falsi universi retroattivi o dati futuri.
