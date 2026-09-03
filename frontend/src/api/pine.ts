import { webClient } from './client'

/* Response shapes of the /pine endpoints (see blueprints/pine.py). */

export interface PineCompileError {
  type: string
  line: number
  column: number
  message: string
}

export interface PineCompileResult {
  status: 'success' | 'error'
  ok?: boolean
  title?: string
  kind?: 'indicator' | 'strategy'
  overlay?: boolean
  inputs?: PineInputSpec[]
  error?: PineCompileError
}

export interface PineInputSpec {
  name: string
  title?: string
  type: string
  default?: unknown
  min?: number
  max?: number
  step?: number
  options?: unknown[]
}

export interface PinePlotPoint {
  time: number
  value: number | null
}

export interface PinePlot {
  id: string
  title: string
  color: string | null
  data: PinePlotPoint[]
}

export interface PineShape {
  time: number
  title: string
  style: string
  location: string
  color: string | null
  text?: string
}

export interface PineHLine {
  price: number
  title: string
  color: string | null
}

export interface PineEvalSignal {
  signal: string
  kind: string
  order_id: string
  qty: number
  time: number
  price: number
}

export interface PineEvalTrade {
  entry_id: string
  direction: string
  qty: number
  entry_time: number
  entry_price: number
  exit_time: number | null
  exit_price: number | null
  pnl: number
  exit_reason: string
}

export interface PineEvalAlert {
  kind: string
  title: string
  message: string
  time: number
}

export interface PineEvaluateResult {
  status: 'success' | 'error'
  meta?: { title: string; kind: string; overlay: boolean }
  inputs?: PineInputSpec[]
  plots?: PinePlot[]
  shapes?: PineShape[]
  hlines?: PineHLine[]
  signals?: PineEvalSignal[]
  trades?: PineEvalTrade[]
  alerts?: PineEvalAlert[]
  error?: PineCompileError
}

export interface PineBacktestMetrics {
  total_trades: number
  winning_trades: number
  losing_trades: number
  win_rate: number
  gross_profit: number
  gross_loss: number
  net_profit: number
  max_drawdown: number
  profit_factor: number | null
  initial_capital: number
  final_equity: number
  return_pct: number
  equity_curve: { time: number; equity: number }[]
  trade_list: PineEvalTrade[]
}

export interface PineBacktestResult extends PineEvaluateResult {
  metrics?: PineBacktestMetrics
}

export interface PineScriptSummary {
  id: number
  name: string
  kind: string
  updated_at: string | null
}

export interface PineScriptDetail {
  id: number
  name: string
  code: string
  kind: string
}

export interface PineStrategy {
  id: string
  script_id: number
  name: string
  symbol: string
  exchange: string
  timeframe: string
  status: string
  execution_mode: 'PAPER' | 'LIVE'
  quantity: number
  product: string
  inputs: Record<string, unknown>
  last_bar_time: number | null
  last_signal_time: string | null
  last_error: string | null
  created_at: string | null
  live_confirmed: boolean
}

export interface PineSignalRow {
  signal_id: string
  signal: string
  kind: string
  order_ref: string
  symbol: string
  exchange: string
  timeframe: string
  price: number
  quantity: number
  bar_time: number
  source: string
  executed: boolean
  order_id: string | null
  order_status: string | null
  created_at: string | null
}

export interface PineAlertRow {
  id: number
  kind: string
  title: string
  message: string
  bar_time: number
  created_at: string | null
}

export interface PineOrderRow {
  id: number
  signal_id: string
  signal: string
  order_id: string | null
  order_status: string | null
  message: string | null
  created_at: string | null
}

interface ApiEnvelope {
  status: string
  message?: string
  error?: unknown
}

/**
 * All /pine calls go through the session-authenticated webClient, matching
 * every other /api-adjacent page in the app. Errors are normalised into
 * thrown Errors so the editor always shows a readable message. HTTP error
 * responses (400/500) reject at the axios layer before `res.data` is read,
 * and the shared interceptor only surfaces `message`, never our
 * `{error: {line, column, message}}` body, so those are unpacked here too.
 */
async function request<T>(
  method: 'get' | 'post' | 'put' | 'delete',
  url: string,
  body?: unknown
): Promise<T> {
  let payload: unknown
  try {
    const res = await webClient.request<T & ApiEnvelope>({
      method,
      url,
      data: body,
    })
    payload = res.data
  } catch (e) {
    const bodyData = (e as { response?: { data?: unknown } }).response?.data
    throw normalizeError(bodyData, (e as Error).message)
  }
  const data = payload as ApiEnvelope
  if (data && data.status === 'error') {
    throw normalizeError(data, 'Request failed')
  }
  return payload as T
}

function normalizeError(payload: unknown, fallback: string): Error {
  if (typeof payload === 'string' && payload) {
    // HTML from a redirect or proxy: keep it short rather than useful.
    return new Error(
      payload
        .replace(/<[^>]*>/g, '')
        .trim()
        .slice(0, 200) || fallback
    )
  }
  const data = payload as { message?: string; error?: unknown } | null | undefined
  if (!data) return new Error(fallback)
  const err = data.error
  if (err && typeof err === 'object') {
    const e = err as PineCompileError
    if (e.message) {
      return new Error(e.line ? `Line ${e.line}:${e.column} - ${e.message}` : e.message)
    }
  }
  if (typeof err === 'string' && err) return new Error(err)
  if (data.message) return new Error(data.message)
  return new Error(fallback)
}

export const pineApi = {
  compile(code: string): Promise<PineCompileResult> {
    return request<PineCompileResult>('post', '/pine/compile', { code })
  },

  evaluate(params: {
    code: string
    symbol: string
    exchange: string
    timeframe: string
    inputs?: Record<string, unknown>
  }): Promise<PineEvaluateResult> {
    return request<PineEvaluateResult>('post', '/pine/evaluate', params)
  },

  backtest(params: {
    code: string
    symbol: string
    exchange: string
    timeframe: string
    inputs?: Record<string, unknown>
    config?: {
      initial_capital?: number
      commission_pct?: number
      slippage_ticks?: number
      tick_size?: number
      long_enabled?: boolean
      short_enabled?: boolean
    }
  }): Promise<PineBacktestResult> {
    return request<PineBacktestResult>('post', '/pine/backtest', params)
  },

  listScripts(): Promise<{ scripts: PineScriptSummary[] }> {
    return request('get', '/pine/scripts')
  },

  getScript(id: number): Promise<{ script: PineScriptDetail }> {
    return request('get', `/pine/scripts/${id}`)
  },

  saveScript(name: string, code: string): Promise<{ id: number }> {
    return request('post', '/pine/scripts', { name, code })
  },

  updateScript(id: number, body: { name?: string; code?: string; kind?: string }): Promise<void> {
    return request('put', `/pine/scripts/${id}`, body)
  },

  deleteScript(id: number): Promise<void> {
    return request('delete', `/pine/scripts/${id}`)
  },

  listStrategies(): Promise<{ strategies: PineStrategy[] }> {
    return request('get', '/pine/strategies')
  },

  createStrategy(params: {
    script_id: number
    name: string
    symbol: string
    exchange: string
    timeframe: string
    quantity: number
    product: string
    inputs?: Record<string, unknown>
  }): Promise<{ strategy: PineStrategy }> {
    return request('post', '/pine/strategies', params)
  },

  deleteStrategy(id: string): Promise<void> {
    return request('delete', `/pine/strategies/${id}`)
  },

  startStrategy(id: string): Promise<void> {
    return request('post', `/pine/strategies/${id}/start`)
  },

  pauseStrategy(id: string): Promise<void> {
    return request('post', `/pine/strategies/${id}/pause`)
  },

  resumeStrategy(id: string): Promise<void> {
    return request('post', `/pine/strategies/${id}/resume`)
  },

  stopStrategy(id: string): Promise<void> {
    return request('post', `/pine/strategies/${id}/stop`)
  },

  enableLive(id: string): Promise<{ strategy: PineStrategy }> {
    // The backend demands {"confirm": true}; the dialog in the UI is the
    // confirmation, so this call is only ever reachable after the user
    // accepted the live-trading warning.
    return request('post', `/pine/strategies/${id}/live`, { confirm: true })
  },

  enablePaper(id: string): Promise<{ strategy: PineStrategy }> {
    return request('post', `/pine/strategies/${id}/paper`)
  },

  strategySignals(id: string): Promise<{ signals: PineSignalRow[] }> {
    return request('get', `/pine/strategies/${id}/signals`)
  },

  strategyAlerts(id: string): Promise<{ alerts: PineAlertRow[] }> {
    return request('get', `/pine/strategies/${id}/alerts`)
  },

  strategyOrders(id: string): Promise<{ orders: PineOrderRow[] }> {
    return request('get', `/pine/strategies/${id}/orders`)
  },
}
