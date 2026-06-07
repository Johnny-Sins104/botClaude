# Scalping Bot - Paper-Only Binance Dashboard

Bot FastAPI per paper trading su dati pubblici Binance, con dashboard locale.

Questa build e' paper-only: non puo' inviare ordini reali. Se `PAPER_MODE=false`, il processo si blocca all'avvio con:

```text
LIVE TRADING DISABLED: this build is paper-only
```

## Installazione

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env
```

Compila `.env` solo in locale. Non condividere mai `.env`, screenshot del file, API key o secret. Lo ZIP pulito deve contenere solo `.env.example` con placeholder.

## Avvio

```powershell
python bot.py
```

Oppure:

```powershell
python -m uvicorn bot:app --host 127.0.0.1 --port 8000
```

Dashboard:

```text
http://127.0.0.1:8000
```

Il server usa `127.0.0.1` come host di default. CORS e' limitato a:

```text
http://127.0.0.1:8000
http://localhost:8000
```

## Configurazione Minima

`.env.example` contiene solo placeholder per le chiavi e i parametri operativi paper:

```dotenv
BINANCE_API_KEY=your_key_here
BINANCE_API_SECRET=your_secret_here
PAPER_MODE=true
SYMBOL=BTCUSDT
CAPITAL=1000.0
STRATEGY=ema
RISK_PCT=1.0
TP_RATIO=2.0
```

Il server resta comunque locale di default: `HOST=127.0.0.1` e `PORT=8000` sono default nel codice.

Non impostare `PAPER_MODE=false`: questa build lo rifiuta sempre.

## Strategie

| Codice | Strategia | Sintesi |
|---|---|---|
| `ema` | EMA + RSI + VWAP | Confluenza trend, momentum e prezzo medio volume |
| `bb` | Bollinger Bands | Mean reversion su chiusura fuori banda |
| `macd` | MACD | Cross MACD/signal calcolato con `signal_prev` e `signal_cur` |
| `ichi` | Ichimoku | Close vs Kumo, Tenkan/Kijun e struttura cloud |

Tutte le strategie calcolano nuovi segnali solo su candele chiuse. Il prezzo live non genera ingressi intrabar.

Parametri strategia letti da `.env`:

```dotenv
EMA_FAST=9
EMA_SLOW=21

BB_PERIOD=20
BB_DEV=2.0

MACD_FAST=12
MACD_SLOW=26
MACD_SIG=9

ICHI_T=9
ICHI_K=26
ICHI_S=52
```

Se migliori queste quattro strategie, mantieni questi nomi nel `.env`; `/api/state` espone anche `strategy_params` con i valori effettivamente caricati.

## Filtro HTF Trend

Filtro direzionale che usa il trend sul timeframe alto (5m) per bloccare gli ingressi controtrend. Calcola due EMA sul 5m (`HTF_TREND_FAST` e `HTF_TREND_SLOW`) e ne misura il gap percentuale:

- Blocca **BUY** se il trend 5m **non è bullish** (`htf_trend_not_aligned_buy`).
- Blocca **SELL** se il trend 5m **non è bearish** (`htf_trend_not_aligned_sell`).
- Blocca entrambi se il 5m è piatto, cioè gap `< HTF_TREND_MIN_GAP_PCT` (`htf_trend_flat`).

Serve a ridurre gli ingressi controtrend, che nei test sono la principale fonte di perdite (in particolare i LONG durante fasi ribassiste).

```dotenv
HTF_TREND_FILTER=true
HTF_TREND_FAST=50
HTF_TREND_SLOW=100
HTF_TREND_MIN_GAP_PCT=0.02
```

Lo stato corrente è esposto in `/api/state` sotto `filters`: `htf_trend` (bull/bear/neutral), `htf_gap_pct`, `htf_fast`, `htf_slow`.

### Stato validazione

I risultati positivi riportati in precedenti versioni del README provenivano da una finestra
di soli 7 giorni e non sono stati confermati su dati out-of-sample a 90 giorni.

Il motore di backtest aveva inoltre un difetto: `_refresh_risk_guard()` ri-abilitava il
`daily_loss_limit` ogni candela, interrompendo il backtest a circa -3% e producendo
statistiche distorte. Il difetto è stato corretto nella versione corrente (vedi PATCH 1).

### Risultati validazione BTCUSDT 90 giorni (2026-03-08 → 2026-06-06)

Eseguita con `VALIDATION_CONFIG` immutabile (`allow_short=false`, `fee_pct=0.001`, `slippage_pct=0.0002`,
`ema_slow=21`, `bb_dev=2.0`, `htf_trend_fast=50`, `htf_trend_slow=100`). Modalità: **LONG-ONLY**.
OOS con warm-up 600 candele 1m (no lookahead). Dettaglio completo: `python validate_backtest.py`.

**90 giorni completi**

| Configurazione | Trade | Win% | PF | P&L netto | MaxDD |
|---|---:|---:|---:|---:|---:|
| Auto HTF-ON (con costi) | 490 | 32.2% | 0.417 | -332.62 USDT | 33.26% |
| Auto HTF-ON (senza costi) | 488 | 34.2% | 1.025 | +10.75 USDT | 3.51% |
| Auto HTF-OFF (con costi) | 906 | 31.3% | 0.416 | -538.41 USDT | 53.88% |
| EMA HTF-ON | 30 | 36.7% | 0.497 | -20.54 USDT | 2.20% |
| EMA HTF-OFF | 44 | 38.6% | 0.539 | -26.85 USDT | 2.91% |
| BB HTF-ON | 394 | 33.5% | 0.439 | -264.41 USDT | 26.44% |
| BB HTF-OFF | 778 | 32.1% | 0.425 | -473.64 USDT | 47.49% |
| MACD HTF-ON | 286 | 27.6% | 0.343 | -244.73 USDT | 24.47% |
| MACD HTF-OFF | 548 | 30.1% | 0.379 | -388.97 USDT | 39.10% |
| ICHI HTF-ON | 437 | 32.0% | 0.426 | -300.80 USDT | 30.08% |
| ICHI HTF-OFF | 843 | 30.7% | 0.405 | -522.77 USDT | 52.32% |

**Split 60d train / 30d OOS**

| Configurazione | Trade | Win% | PF | P&L netto | MaxDD |
|---|---:|---:|---:|---:|---:|
| Train 60d Auto HTF-ON | 402 | 34.1% | 0.448 | -261.38 USDT | 26.14% |
| OOS 30d Auto HTF-ON (con costi) | 88 | 23.9% | 0.267 | -96.46 USDT | 9.65% |
| OOS 30d Auto HTF-ON (senza costi) | 86 | 24.4% | 0.633 | -30.25 USDT | 3.02% |
| OOS 30d Auto HTF-OFF | 253 | 24.5% | 0.308 | -246.29 USDT | 24.69% |
| OOS 30d BB HTF-ON | 84 | 22.6% | 0.250 | -95.72 USDT | 9.63% |
| OOS 30d BB HTF-OFF | 243 | 24.7% | 0.299 | -237.10 USDT | 23.88% |
| OOS 30d MACD HTF-ON | 58 | 19.0% | 0.202 | -73.62 USDT | 7.36% |
| OOS 30d ICHI HTF-ON | 74 | 20.3% | 0.219 | -90.25 USDT | 9.03% |

**Verdetto**: nessuna configurazione supera i criteri minimi out-of-sample (≥50 trade, PF>1,
P&L netto positivo dopo costi, nessuna settimana con concentrazione perdite >50%).
Tutti i run OOS mostrano PF < 1 con i parametri di validazione standard.

Le commissioni assorbono l'intero edge grezzo: Auto HTF-ON 90d vale +10.75 USDT senza costi
e -332.62 USDT con costi. Il bot non dimostra edge statistico nel periodo testato.

Finché non viene completata una validazione su simboli e periodi diversi,
nessun valore di `TP_RATIO`, `SL_ATR_MULT` o `MIN_ENTRY_SCORE` deve essere considerato
una configurazione profittevole.

### Nota su ALLOW_SHORT

- `ALLOW_SHORT=false` è il default e l'unica modalità testata nella validazione ufficiale.
- I risultati sopra sono in modalità LONG-ONLY. Il criterio di dominanza lato (precedentemente
  bocciava il 100% LONG) è stato rimosso per la modalità long-only: con short disabilitati
  è atteso e corretto che tutti i trade siano LONG.

## Prezzo Live e Dashboard

- Live BTC price: aggiornato tick-by-tick dal WebSocket Binance.
- PnL, stop loss, take profit e trailing stop: aggiornati usando il prezzo live.
- Signal data: i segnali usano solo candele 1m chiuse, ma la strategia e i parametri arrivano da `.env`.
- La dashboard legge `/api/state` ogni 500 ms e aggiorna solo campi dinamici.

## Sicurezza e Robustezza

- Reconnect WebSocket automatico con backoff configurabile.
- Daily loss limit paper: quando il P&L giornaliero totale raggiunge il limite, il bot blocca nuove entry fino al giorno successivo.
- Kill switch manuale via dashboard o API: blocca nuove entry immediatamente; puo' anche chiudere la posizione paper aperta al prezzo live.
- SL, TP e trailing stop restano gestiti sul prezzo live anche quando nuove entry sono bloccate.

Parametri configurabili via `/api/config`:

| Parametro | Default | Descrizione |
|---|---:|---|
| `daily_loss_limit_pct` | `3.0` | Perdita giornaliera massima in percentuale della baseline del giorno |
| `daily_loss_limit_enabled` | `true` | Abilita/disabilita il blocco giornaliero |
| `allow_short` | `false` | Abilita gli ingressi SHORT simulati in paper |
| `min_entry_score` | `75` | Score minimo per aprire una nuova posizione |
| `reconnect_initial_delay_sec` | `2` | Primo ritardo prima del reconnect |
| `reconnect_max_delay_sec` | `60` | Backoff massimo tra reconnect |

## Paper Trading Realistico

Il paper trading applica fee e slippage configurabili nel backend:

- `fee_pct` default `0.001`
- `slippage_pct` default `0.0002`
- `max_notional_pct` default `0.95`
- `min_entry_score` default `75`
- `allow_short` default `false`

Il capitale viene aggiornato con `pnl_net`, cioe' PnL lordo meno fee di ingresso e uscita. Ogni trade salva prezzi signal/fill, fee, slippage, sizing e PnL lordo/netto.

Gli SHORT sono simulati solo in paper. Binance Spot non apre short reali. Per evitare aperture short involontarie, gli ingressi short sono disabilitati di default; si abilitano solo con `allow_short=true` via `/api/config`.

## API Locale

| Endpoint | Metodo | Descrizione |
|---|---|---|
| `/` | GET | Dashboard |
| `/api/state` | GET | Stato completo paper-only |
| `/api/config` | POST | Aggiorna configurazione validata |
| `/api/kill-switch` | POST | Attiva/disattiva kill switch paper |
| `/api/trades` | GET | Storico trade JSON |
| `/api/trades/download` | GET | Download `logs/trades.json` se esiste |

Esempio:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/config -ContentType 'application/json' -Body '{"strategy":"macd","risk_pct":1.0}'
```

`/api/config` rifiuta valori fuori range e parametri sconosciuti con HTTP 400. In caso di errore non modifica parzialmente la configurazione.

Kill switch:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/kill-switch -ContentType 'application/json' -Body '{"enabled":true,"reason":"manual","close_position":false}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/kill-switch -ContentType 'application/json' -Body '{"enabled":false}'
```

Per chiudere anche la posizione paper aperta:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/kill-switch -ContentType 'application/json' -Body '{"enabled":true,"reason":"manual","close_position":true}'
```

## Persistenza Paper

Il bot salva lo stato paper in:

```text
logs/state.json
```

Al riavvio ripristina eventuale posizione aperta. Se il file e' corrotto, il bot avvisa e riparte flat.

`logs/` non deve essere incluso nello ZIP finale.

## Test Minimi

```powershell
python -m py_compile bot.py
$env:PAPER_MODE='true'; python -c "import bot; print('paper import ok')"
$env:PAPER_MODE='false'; python -c "import bot"
```

Test API config senza avviare il WebSocket:

```powershell
$env:PAPER_MODE='true'; python -c "from fastapi.testclient import TestClient; import bot; c=TestClient(bot.app); print(c.post('/api/config', json={'risk_pct':999}).status_code); print(c.post('/api/config', json={'strategy':'bb','risk_pct':1.0}).status_code)"
$env:PAPER_MODE='true'; python -c "from fastapi.testclient import TestClient; import bot; c=TestClient(bot.app); print(c.post('/api/kill-switch', json={'enabled':True}).status_code); print(c.post('/api/kill-switch', json={'enabled':False}).status_code)"
```

## ZIP Pulito

Comando PowerShell consigliato:

```powershell
Compress-Archive -LiteralPath bot.py,index.html,requirements.txt,README.md,.env.example -DestinationPath ..\botClaude-paper-only.zip -Force
```

Questo include solo i file sorgente necessari ed esclude `.env`, `logs/`, `__pycache__/`, `*.pyc`, `.git/` e cache temporanee.
