# Alert + Webhook System

TradingView-style alerts on the `/trading` page: a price condition or a Pine
strategy BUY/SELL signal, evaluated **server-side**, delivered to the user's
own webhook URL. Alerts keep running after the browser or editor is closed.

Quick orientation:

| Need | Entry point |
|---|---|
| Create an alert | `/trading` → `Alert` button (chart toolbar, beside Pine) |
| REST endpoints | [API reference below](#rest-api) |
| Matching engine | `services/alert_engine.py` |
| Persistence | `database/alert_db.py`, migration `upgrade/migrate_alerts.py` |
| Frontend | `frontend/src/components/trading/AlertPanel.tsx` |

## 1. Architecture

```
Pine strategy (server-side runner)
  │  emits PineSignalEvent on the existing event bus
  ▼
subscribers/__init__.py ── bus.subscribe("pine.signal", alert_engine.handle_pine_signal)
  ▼
services/alert_engine.py
  │  match alerts (strategy_id + signal, or price operator)
  │  dedup via deterministic idempotency key (uuid5)
  │  persist AlertEvent + AlertDelivery rows
  ▼
ThreadPoolExecutor (bounded, requests-based)
  │  POST webhook (timeout, up to 3 attempts)
  │  headers: X-OpenAlgo-Event-ID, X-OpenAlgo-Idempotency-Key
  ▼
User's webhook receiver

Price alerts: AlertFeed reuses the existing WebSocket proxy (one
WebSocketClient per user, same as the Pine feed dispatcher) — ticks are
evaluated against active price alerts. No new broker feed.
```

The Pine runtime never calls a webhook; it only publishes events. The engine
never places orders; webhooks are notifications only.

## 2. Alert lifecycle

- `ACTIVE` — being evaluated every tick/signal
- `TRIGGERED` — condition met once (Once-only trigger mode); the alert is
  disabled immediately so retries of the webhook never duplicate events
- `EXPIRED` — past its expiration timestamp; can never trigger again

Trigger modes: `once_only` today; the model is extensible for
`every_time` / `once_per_bar` / `once_per_bar_close`.

## 3. Webhook payload

```json
{
  "event": "strategy_signal",       // or "price_cross"
  "signal": "BUY",                  // or the price operator
  "symbol": "NIFTY",
  "exchange": "NSE",
  "timeframe": "5m",
  "price": 22050.0,
  "strategy": "EMA Cross",
  "strategy_id": "<instance id>",
  "alert_id": "<alert id>",
  "event_id": "<event id>",
  "idempotency_key": "<deterministic uuid5>",
  "bar_time": "2026-09-04T10:30:00+05:30",
  "timestamp": "2026-09-04T10:30:01+05:30",
  "message": "<user message>"
}
```

Headers: `X-OpenAlgo-Event-ID` (unique per logical event),
`X-OpenAlgo-Idempotency-Key` (same on every retry attempt, so receivers can
deduplicate). A `POST /alerts/test` sends
`{"event": "test", "source": "openalgo", ...}` and never creates orders.

## 4. Delivery semantics

- Timeout: 8s (env `ALERT_WEBHOOK_TIMEOUT`, clamped 1-30)
- Retries: up to 3 attempts (env `ALERT_WEBHOOK_RETRY_DELAYS`, seconds
  between attempts, default `0,10,30`)
- Status machine per delivery row: `PENDING → SENDING → SUCCESS |
  RETRYING → … | FAILED`
- One failed webhook never stops other alerts or market data (bounded worker
  pool, per-alert isolation)

## 5. Security

- Webhook URLs are validated for SSRF: only http/https, no localhost, no
  private/loopback/link-local (cloud metadata) targets — DNS names are
  resolved at validation time. Self-hosted users may opt in via
  `ALLOW_PRIVATE_WEBHOOKS=true`.
- Webhook payloads never include broker credentials or secrets.
- Every endpoint is session-authenticated and rate limited; users can only
  see/modify their own alerts.

## 6. Environment flags

| Flag | Default | Purpose |
|---|---|---|
| `ALERT_WEBHOOK_TIMEOUT` | `8` | Webhook POST timeout (s) |
| `ALERT_WEBHOOK_WORKERS` | `4` | Delivery pool size |
| `ALERT_WEBHOOK_RETRY_DELAYS` | `0,10,30` | Seconds between attempts |
| `ALLOW_PRIVATE_WEBHOOKS` | `false` | Allow private webhook targets (self-hosted) |

## 7. REST API

All routes session-authenticated (browser same-origin), CSRF-protected.

| Method | Route | Purpose |
|---|---|---|
| GET | `/alerts` | List own alerts |
| POST | `/alerts` | Create (price or strategy condition) |
| GET | `/alerts/<id>` | Alert detail |
| PUT | `/alerts/<id>` | Update fields |
| DELETE | `/alerts/<id>` | Delete |
| POST | `/alerts/<id>/enable` | Re-enable (not after TRIGGERED) |
| POST | `/alerts/<id>/disable` | Pause evaluation |
| GET | `/alerts/<id>/logs` | Events + delivery history |
| POST | `/alerts/test` | Send a test webhook (no order) |

Price operators: `crossing`, `crossing_up`, `crossing_down`,
`greater_than`, `less_than`, `greater_than_equal`, `less_than_equal`.
Strategy signals: `BUY`, `SELL`, `ANY`.

## 8. Database

`alerts`, `alert_events`, `alert_deliveries` — created by
`upgrade/migrate_alerts.py` (registered in `upgrade/migrate_all.py`,
idempotent). `alert_events.idempotency_key` has a UNIQUE index: the same
alert + source + bar timestamp + signal can never produce two logical
events, even across restarts.
