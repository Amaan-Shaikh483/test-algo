# Pine Script Strategy Engine

A TradingView-style Pine Script (v5 subset) engine built into OpenAlgo's
existing `/trading` page. Scripts are compiled and executed **server-side**
by the same runtime for chart studies, backtests and live/paper strategies —
no second market-data, order or WebSocket system was added; everything rides
the existing ZMQ feed, order pipeline, Socket.IO events and database.

Quick orientation:

| Need | Entry point |
|---|---|
| Write/compile a script, put it on the chart | `/trading` → `Pine` button (bottom dock) |
| Run a strategy server-side | Same dock → Strategy tab → Create (starts in PAPER) |
| REST endpoints | [API reference below](#rest-api) |
| Language internals | `pine/` (lexer → parser → validator → runtime) |
| Runner service | `services/pine_strategy_service.py` |
| Persistence | `database/pine_db.py`, migration `upgrade/migrate_pine.py` |

## 1. Overview and architecture

```
Browser (/trading Pine dock)
  │  POST /pine/compile | /pine/evaluate | /pine/backtest
  │  POST /pine/strategies[...]/start|pause|resume|stop|live|paper
  ▼
blueprints/pine.py  (session auth, rate limited)
  ▼
pine/compiler.py    (lexer → parser → validator → AST → PineRuntime plan)
  ▼
services/pine_strategy_service.py
  ├─ one-shot: evaluate_script / backtest_script  (chart + backtest tabs)
  └─ PineStrategyManager → StrategyRunner per instance
        ▲  ticks
        │
websocket_proxy/server.py (existing ZMQ → WS market data)
        ▲  broker feed (existing, untouched)
```

Realtime path reuses the existing feed: `PineFeedDispatcher` subscribes on
the **existing** WebSocket proxy / market-data pipeline (it never binds or
creates publishers; `ZMQ_PORT` is fixed). Ticks are aggregated per instance
by `CandleAggregator`; a bar is only evaluated once per timeframe and only
after it closes (confirmed-candle model), which kills intrabar duplicates.

## 2. Supported language subset

- `//@version=5`, `indicator(...)`, `strategy(...)` declarations
- `input()`, `input.int`, `input.float`, `input.bool`, `input.string`
- Series: `open high low close volume time bar_index`
- `ta.`: `sma ema wma rsi atr highest lowest crossover crossunder`
- `math.`: `abs max min round floor ceil sqrt pow`
- Operators: arithmetic, comparison, logical `and/or/not`, ternary `?:`
- Variables, reassignment, `var` initialisers, `if` / `else` blocks
- `plot()`, `plotshape()`, `hline()`
- `strategy.entry()`, `strategy.close()`, `strategy.exit()` (stop/limit)
- `alertcondition()`, `alert()`

Anything outside the subset (e.g. `request.security()`, `array.*`,
`matrix.*`, methods, libraries) fails at compile time with
`type / line / column / message` — never a silent approximation.

## 3. What is not supported

`request.security`, `request.quandl`, multi-symbol anything, `array`/`matrix`
/`map`, `varip`, `for`…`to`…`by` loops beyond the simple form, user methods,
`import` of libraries, `strategy.order` raw orders, `table`/`label`/`box`
drawings, `timeframe.*` helpers, `security`-style repainting controls. Each
produces an explicit compile error with position info.

## 4. Editor usage

Open `/trading`, press the `Pine` button in pane zero's toolbar. The dock
gives you a CodeMirror editor (line numbers, Pine syntax highlighting),
Compile (errors show line:column), Add/Remove from chart (plots, hlines,
plotshape and BUY/SELL markers on the focused pane), Save/Load/Delete named
scripts, an Inputs panel built from the compiled `input.*` defaults, a
Backtest tab, and a Strategy tab for the server-side instance lifecycle.

## 5. Strategy lifecycle

```
create (PAPER) → start → RUNNING ⇄ PAUSED (pause/resume) → stop → STOPPED
                                  ↘ ERROR (compile/runtime failure)
```

- Instances persist in `pine_strategy_instances` and keep running after the
  browser closes; `restore_pine_strategies()` re-subscribes them at boot.
- Execution mode defaults to **PAPER** (sandbox orders). LIVE requires an
  explicit `POST /pine/strategies/<id>/live {"confirm": true}` — the UI asks
  through a confirmation dialog; paper can always be re-enabled.
- Restart recovery skips bars at or before `last_bar_time` and replays
  nothing that the idempotency store already holds.

## 6. Backtesting

`POST /pine/backtest` runs the **same** `PineRuntime` over historical bars
fetched through the existing history REST path. Inputs: initial capital,
commission %, slippage ticks, tick size, long/short toggles, plus the
script's own inputs. Output: trades, win rate, gross/net P&L, max drawdown,
profit factor, equity curve and trade list.

## 7. Signal flow and idempotency

```
confirmed bar → PineRuntime → signal (intent JSON, not an order)
  → idempotency check (uuid5 of instance|symbol|timeframe|bar_time|signal|
    order_ref|sequence; unique index uq_pine_signals_idem)
  → pine_db.record_signal → event bus (PineSignalEvent / PineAlertEvent)
  → risk validation → order builder → sandbox (paper) or broker adapter (live)
  → PineOrderEvent → existing Socket.IO events (strategy_signal /
    strategy_alert / strategy_status / strategy_order) → UI
```

Duplicate ticks, reconnects, refreshes and restarts cannot double-order: the
signal id is deterministic per (bar, signal, sequence) and the DB rejects the
second insert before any order is built.

## 8. REST API

All routes are session-authenticated (`check_session_validity`) and rate
limited. Timeframes: `1s…30m, 1h…4h, D, W, M`. Exchanges: `NSE BSE NFO BFO
CDS BCD MCX NCDEX NSE_INDEX BSE_INDEX MCX_INDEX GLOBAL_INDEX`.

| Method + path | Purpose |
|---|---|
| `POST /pine/compile` | Compile source, return inputs + meta or positioned error |
| `POST /pine/evaluate` | Run over history, return plots/markers/hlines for the chart |
| `POST /pine/backtest` | Run backtest with the same runtime, return metrics |
| `GET/POST /pine/scripts` | List / save scripts |
| `GET/PUT/DELETE /pine/scripts/<id>` | Load / update / delete a script |
| `GET/POST /pine/strategies` | List / create instances |
| `GET/DELETE /pine/strategies/<id>` | Fetch / delete an instance |
| `POST /pine/strategies/<id>/start|pause|resume|stop` | Lifecycle |
| `POST /pine/strategies/<id>/live` | LIVE mode; body `{"confirm": true}` required |
| `POST /pine/strategies/<id>/paper` | Switch back to paper (always allowed) |
| `GET /pine/strategies/<id>/signals|alerts|orders` | History tables |

## 9. Database entities and migrations

New tables: `pine_scripts`, `pine_script_versions`, `pine_strategy_instances`,
`pine_signals`, `pine_alerts`, `pine_orders`, `pine_backtest_runs` (see
`database/pine_db.py`). Migration `upgrade/migrate_pine.py` is registered in
`upgrade/migrate_all.py`'s `MIGRATIONS` list: idempotent (safe to rerun,
reports "already up to date"), creates indexes including the unique
`uq_pine_signals_idem`, and never touches existing OpenAlgo tables.

## 10. Security model

The runtime is a hand-written interpreter over the AST — no `eval`, no
`exec`, no regex rewriting, no arbitrary code execution. Pine code cannot
reach the filesystem, environment, network, broker credentials, the database
or OS primitives: the interpreter only exposes whitelisted builtins and
arithmetic. The one-shot endpoints are rate limited; strategy ownership is
checked on every route (cross-user access 404s, never leaks existence).

## 11. Realtime events

| Socket.IO event | Fired when |
|---|---|
| `strategy_signal` | A confirmed-bar BUY/SELL intent was accepted |
| `strategy_alert` | `alertcondition()` / `alert()` fired |
| `strategy_status` | Lifecycle transition (RUNNING/PAUSED/STOPPED/ERROR) |
| `strategy_order` | Signal routed through the order pipeline (paper or live) |

## 12. Example scripts

EMA cross (plots + entries):

```pine
//@version=5
strategy("EMA Cross", overlay=true)
fast = input.int(9, "Fast EMA")
slow = input.int(21, "Slow EMA")
bull = ta.crossover(ta.ema(close, fast), ta.ema(close, slow))
bear = ta.crossunder(ta.ema(close, fast), ta.ema(close, slow))
plot(ta.ema(close, fast), "Fast")
plot(ta.ema(close, slow), "Slow")
strategy.entry("L", "long", when=bull)
strategy.close("L", when=bear)
```

RSI mean-reversion (crossover(rsi,30) / crossunder(rsi,70)) and an
`alertcondition()` demo ship as the editor's default templates.

## 13. Testing and known limitations

`uv run pytest test/pine/ -v` covers lexer → parser → validator →
indicators → strategy functions → realtime E2E with a fake feed and
monkeypatched order placement (paper sandbox and live path) → API blueprint
→ migration idempotency. Limitations: no multi-timeframe/multi-symbol
(`request.security`), no loops over series, no drawings beyond plot/shape/
hline, backtests use the broker's available history window only.
