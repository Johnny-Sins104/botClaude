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

Eseguita con `FEE_PCT=0.001`, `SLIPPAGE_PCT=0.0002`. Dettaglio completo: `python validate_backtest.py`.

| Configurazione | Trade | Win% | PF | P&L netto | MaxDD |
|---|---:|---:|---:|---:|---:|
| 90d Auto+HTF (con costi) | 147 | 21.1% | 0.763 | -86.37 USDT | 13.91% |
| 90d Auto+HTF (senza costi) | 147 | 21.1% | 1.016 | +4.80 USDT | 7.44% |
| 90d EMA solo, HTF off | 89 | 18.0% | 0.612 | -93.49 USDT | 11.78% |
| 90d BB solo, HTF off | 136 | 19.1% | 0.668 | -115.24 USDT | 14.55% |
| 90d MACD solo, HTF off | 144 | 20.8% | 0.724 | -101.60 USDT | 10.94% |
| 90d ICHI solo, HTF off | 172 | 17.4% | 0.584 | -182.20 USDT | 18.44% |
| Train 60d Auto+HTF | 118 | 18.6% | 0.617 | -117.28 USDT | 13.56% |
| **OOS 30d Auto+HTF** | **38** | **26.3%** | **1.104** | **+9.36 USDT** | 4.89% |
| OOS 30d BB solo, HTF off | 34 | 32.4% | 1.350 | +27.88 USDT | 2.02% |

**Verdetto**: nessuna configurazione supera i criteri minimi (≥50 trade, PF>1 out-of-sample,
P&L positivo dopo costi, non dipendente da un solo lato). Il risultato OOS 30d è positivo
(PF 1.104) ma con soli 38 trade, insufficienti per validità statistica.

Le commissioni totali assorbono l'intero edge grezzo (90d Auto+HTF: -86 USDT con costi vs
+4.80 senza). Prima di ottimizzare i parametri è necessario ridurre il numero di trade
o aumentare il payoff ratio grezzo.

Finché non viene completata una validazione su simboli e periodi diversi,
nessun valore di `TP_RATIO`, `SL_ATR_MULT` o `MIN_ENTRY_SCORE` deve essere considerato
una configurazione profittevole.

### Nota su ALLOW_SHORT

- `ALLOW_SHORT=false` è la scelta più prudente: nessuno short simulato, rischio operativo minore.
- Precedenti backtest su 7 giorni mostravano P&L positivo quasi interamente da SHORT, ma
  questo risultato non è stato confermato out-of-sample. Default impostato a `false`.

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
