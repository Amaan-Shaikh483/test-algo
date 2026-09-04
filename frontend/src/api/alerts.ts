import { webClient } from './client'

/* Response shapes of the /alerts endpoints (see blueprints/alerts.py). */

export type AlertSourceType = 'price' | 'strategy'

export type AlertOperator =
  | 'crossing'
  | 'crossing_up'
  | 'crossing_down'
  | 'greater_than'
  | 'less_than'
  | 'greater_than_equal'
  | 'less_than_equal'

export type AlertStatus = 'ACTIVE' | 'TRIGGERED' | 'EXPIRED'

export interface Alert {
  id: string
  name: string
  symbol: string
  exchange: string
  timeframe: string
  source_type: AlertSourceType
  strategy_id: string | null
  signal: string | null
  operator: string | null
  value: number | null
  trigger_mode: string
  expiration: string | null
  message: string | null
  webhook_url: string
  status: AlertStatus
  enabled: boolean
  created_at: string | null
  last_triggered_at: string | null
}

export interface AlertDelivery {
  status: string
  attempt: number
  http_status: number | null
  error: string | null
  created_at: string | null
  completed_at: string | null
}

export interface AlertLog {
  id: string
  event_type: string
  signal: string | null
  symbol: string
  price: number | null
  bar_time: number | null
  idempotency_key: string
  created_at: string | null
  deliveries: AlertDelivery[]
}

export interface AlertCreateParams {
  symbol: string
  exchange: string
  timeframe: string
  source_type: AlertSourceType
  strategy_id?: string
  signal?: string
  operator?: string
  value?: number
  expiration?: string | null
  message?: string
  webhook_url: string
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  try {
    const { data } = await webClient.request<T>({
      method,
      url,
      data: body,
    })
    return data
  } catch (err) {
    const payload = (err as { response?: { data?: unknown } })?.response?.data
    if (payload && typeof payload === 'object' && payload !== null) {
      const msg = (payload as { message?: unknown }).message
      if (typeof msg === 'string' && msg) {
        throw new Error(msg)
      }
    }
    throw err
  }
}

export const alertsApi = {
  list(): Promise<{ status: string; alerts: Alert[] }> {
    return request('get', '/alerts')
  },

  create(params: AlertCreateParams): Promise<{ status: string; alert: Alert }> {
    return request('post', '/alerts', params)
  },

  get(id: string): Promise<{ status: string; alert: Alert }> {
    return request('get', `/alerts/${id}`)
  },

  update(id: string, body: Partial<AlertCreateParams>): Promise<{ status: string; alert: Alert }> {
    return request('put', `/alerts/${id}`, body)
  },

  remove(id: string): Promise<{ status: string }> {
    return request('delete', `/alerts/${id}`)
  },

  enable(id: string): Promise<{ status: string; alert: Alert }> {
    return request('post', `/alerts/${id}/enable`)
  },

  disable(id: string): Promise<{ status: string; alert: Alert }> {
    return request('post', `/alerts/${id}/disable`)
  },

  logs(id: string): Promise<{ status: string; logs: AlertLog[] }> {
    return request('get', `/alerts/${id}/logs`)
  },

  testWebhook(url: string): Promise<{
    status: string
    message: string
    http_status: number | null
  }> {
    return request('post', '/alerts/test', { webhook_url: url })
  },
}
