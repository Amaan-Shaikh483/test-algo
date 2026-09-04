import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { toast } from 'sonner'
import {
  type Alert,
  type AlertCreateParams,
  type AlertLog,
  type AlertSourceType,
  alertsApi,
} from '@/api/alerts'
import { pineApi } from '@/api/pine'
import { useSocketContext } from '@/components/socket/SocketProvider'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'

const OPERATOR_LABELS: Record<string, string> = {
  crossing: 'Crossing',
  crossing_up: 'Crossing Up',
  crossing_down: 'Crossing Down',
  greater_than: 'Greater Than',
  less_than: 'Less Than',
  greater_than_equal: 'Greater Than or Equal',
  less_than_equal: 'Less Than or Equal',
}

const STATUS_STYLES: Record<string, string> = {
  ACTIVE: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400',
  TRIGGERED: 'bg-blue-500/15 text-blue-600 dark:text-blue-400',
  EXPIRED: 'bg-zinc-500/15 text-zinc-600 dark:text-zinc-400',
}

interface AlertPanelProps {
  symbol: { symbol: string; exchange: string } | null
  interval: string
}

function formatTime(value: string | null): string {
  if (!value) {
    return '-'
  }
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) {
    return value
  }
  return d.toLocaleString()
}

/* --------------------------------------------------------------------- */
/* Create Alert dialog                                                    */
/* --------------------------------------------------------------------- */

function CreateAlertDialog({
  open,
  onClose,
  onCreated,
  symbol,
  interval,
}: {
  open: boolean
  onClose: () => void
  onCreated: () => void
  symbol: { symbol: string; exchange: string } | null
  interval: string
}) {
  const queryClient = useQueryClient()
  const [sourceType, setSourceType] = useState<AlertSourceType>('price')
  const [operator, setOperator] = useState('crossing_up')
  const [value, setValue] = useState('')
  const [strategyId, setStrategyId] = useState('')
  const [signal, setSignal] = useState('ANY')
  const [expiration, setExpiration] = useState('')
  const [message, setMessage] = useState('')
  const [webhookUrl, setWebhookUrl] = useState('')
  const [notificationsOpen, setNotificationsOpen] = useState(false)

  const { data: strategiesData } = useQuery({
    queryKey: ['pine', 'strategies'],
    queryFn: () => pineApi.listStrategies(),
    enabled: open,
  })
  const strategies = strategiesData?.strategies ?? []

  useEffect(() => {
    if (open && sourceType === 'strategy' && !strategyId && strategies.length > 0) {
      setStrategyId(strategies[0].id)
    }
  }, [open, sourceType, strategies, strategyId])

  useEffect(() => {
    if (!symbol) {
      return
    }
    setMessage(
      sourceType === 'price'
        ? `${symbol.symbol} ${OPERATOR_LABELS[operator]?.toLowerCase() ?? ''} ${value}`.trim()
        : `${symbol.symbol} ${signal === 'ANY' ? 'BUY or SELL' : signal} signal`
    )
  }, [symbol, sourceType, operator, value, signal])

  const createMutation = useMutation({
    mutationFn: (params: AlertCreateParams) => alertsApi.create(params),
    onSuccess: () => {
      toast.success('Alert created')
      queryClient.invalidateQueries({ queryKey: ['alerts'] })
      onCreated()
      onClose()
      setValue('')
      setExpiration('')
      setWebhookUrl('')
    },
    onError: (err: Error) => {
      toast.error(err.message || 'Failed to create alert')
    },
  })

  const testMutation = useMutation({
    mutationFn: () => alertsApi.testWebhook(webhookUrl),
    onSuccess: (result) => {
      if (result.status === 'success') {
        toast.success(`Test webhook delivered (${result.message})`)
      } else {
        toast.error(`Test webhook failed: ${result.message}`)
      }
    },
    onError: (err: Error) => {
      toast.error(err.message || 'Test webhook failed')
    },
  })

  const submit = () => {
    if (!symbol) {
      toast.error('No active symbol on the chart')
      return
    }
    if (sourceType === 'price' && value === '') {
      toast.error('Enter a target price')
      return
    }
    if (sourceType === 'strategy' && !strategyId) {
      toast.error('Create and start a Pine strategy first, then select it')
      return
    }
    createMutation.mutate({
      symbol: symbol.symbol,
      exchange: symbol.exchange,
      timeframe: interval,
      source_type: sourceType,
      operator: sourceType === 'price' ? operator : undefined,
      value: sourceType === 'price' ? Number.parseFloat(value) : undefined,
      strategy_id: sourceType === 'strategy' ? strategyId : undefined,
      signal: sourceType === 'strategy' ? signal : undefined,
      expiration: expiration ? new Date(expiration).toISOString() : null,
      message: message || undefined,
      webhook_url: webhookUrl,
    })
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Create alert on {symbol?.symbol ?? 'symbol'}</DialogTitle>
          <DialogDescription>
            Alerts run on the server and keep working after this page is closed.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>Condition</Label>
              <Select value={sourceType} onValueChange={(v) => setSourceType(v as AlertSourceType)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="price">Price</SelectItem>
                  <SelectItem value="strategy">Strategy</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {sourceType === 'price' ? (
              <>
                <div className="space-y-1.5">
                  <Label>Operator</Label>
                  <Select value={operator} onValueChange={setOperator}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {Object.entries(OPERATOR_LABELS).map(([key, label]) => (
                        <SelectItem key={key} value={key}>
                          {label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label>Value</Label>
                  <Input
                    type="number"
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                    placeholder="22000"
                  />
                </div>
              </>
            ) : (
              <>
                <div className="space-y-1.5">
                  <Label>Strategy</Label>
                  <Select value={strategyId} onValueChange={setStrategyId}>
                    <SelectTrigger>
                      <SelectValue placeholder="Select strategy" />
                    </SelectTrigger>
                    <SelectContent>
                      {strategies.length === 0 ? (
                        <SelectItem value="none" disabled>
                          No strategies yet
                        </SelectItem>
                      ) : (
                        strategies.map((s) => (
                          <SelectItem key={s.id} value={s.id}>
                            {s.name}
                          </SelectItem>
                        ))
                      )}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label>Signal</Label>
                  <Select value={signal} onValueChange={setSignal}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="BUY">BUY</SelectItem>
                      <SelectItem value="SELL">SELL</SelectItem>
                      <SelectItem value="ANY">BUY or SELL</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </>
            )}
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>Trigger</Label>
              <Select value="once_only" onValueChange={() => undefined}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="once_only">Once only</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>Expiration</Label>
              <Input
                type="datetime-local"
                value={expiration}
                onChange={(e) => setExpiration(e.target.value)}
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <Label>Message</Label>
            <Input value={message} onChange={(e) => setMessage(e.target.value)} />
          </div>

          <div className="rounded border">
            <button
              type="button"
              className="flex w-full items-center justify-between px-3 py-2 text-sm font-medium"
              onClick={() => setNotificationsOpen((v) => !v)}
            >
              Notifications
              <span className="text-xs text-muted-foreground">
                {notificationsOpen ? 'Hide' : 'Show'}
              </span>
            </button>
            {notificationsOpen && (
              <div className="space-y-2 border-t px-3 py-3">
                <Label>Webhook URL</Label>
                <Input
                  value={webhookUrl}
                  onChange={(e) => setWebhookUrl(e.target.value)}
                  placeholder="https://your-server.com/webhook"
                />
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={!webhookUrl || testMutation.isPending}
                  onClick={() => testMutation.mutate()}
                >
                  {testMutation.isPending ? 'Sending…' : 'Test webhook'}
                </Button>
                <p className="text-xs text-muted-foreground">
                  Sends a POST with event, signal, symbol, exchange, timeframe, price, strategy,
                  alert id, event id, bar time and your message. No order is ever created by a
                  webhook or a test.
                </p>
              </div>
            )}
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            onClick={submit}
            disabled={
              createMutation.isPending || !webhookUrl || (sourceType === 'price' && value === '')
            }
          >
            {createMutation.isPending ? 'Creating…' : 'Create'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/* --------------------------------------------------------------------- */
/* Alert logs dialog                                                      */
/* --------------------------------------------------------------------- */

function LogsDialog({ alert, onClose }: { alert: Alert | null; onClose: () => void }) {
  const { data } = useQuery({
    queryKey: ['alerts', alert?.id, 'logs'],
    queryFn: () => alertsApi.logs(alert!.id),
    enabled: alert !== null,
  })
  const logs: AlertLog[] = data?.logs ?? []
  const lastDelivery = (log: AlertLog) => log.deliveries[0]

  return (
    <Dialog open={alert !== null} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Alert log — {alert?.name ?? ''}</DialogTitle>
          <DialogDescription>
            Time, alert, signal, webhook status and HTTP status.
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-[50vh] overflow-auto">
          {logs.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              No events yet. The log fills in when the alert triggers.
            </p>
          ) : (
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b text-left text-muted-foreground">
                  <th className="py-2 pr-2">Time</th>
                  <th className="py-2 pr-2">Signal</th>
                  <th className="py-2 pr-2">Price</th>
                  <th className="py-2 pr-2">Webhook</th>
                  <th className="py-2 pr-2">HTTP</th>
                  <th className="py-2">Attempts</th>
                </tr>
              </thead>
              <tbody>
                {logs.map((log) => {
                  const delivery = lastDelivery(log)
                  return (
                    <tr key={log.id} className="border-b">
                      <td className="py-2 pr-2">{formatTime(log.created_at)}</td>
                      <td className="py-2 pr-2">{log.signal ?? '-'}</td>
                      <td className="py-2 pr-2">{log.price ?? '-'}</td>
                      <td className="py-2 pr-2">{delivery?.status ?? 'PENDING'}</td>
                      <td className="py-2 pr-2">{delivery?.http_status ?? '-'}</td>
                      <td className="py-2">
                        {log.deliveries.length > 0 ? `${delivery?.attempt ?? 0}` : '0'}
                        {delivery?.error ? ` (${delivery.error.slice(0, 40)})` : ''}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}

/* --------------------------------------------------------------------- */
/* Panel                                                                  */
/* --------------------------------------------------------------------- */

export function AlertPanel({ symbol, interval }: AlertPanelProps) {
  const queryClient = useQueryClient()
  const [createOpen, setCreateOpen] = useState(false)
  const [logsAlert, setLogsAlert] = useState<Alert | null>(null)
  const { socket } = useSocketContext()

  const { data } = useQuery({
    queryKey: ['alerts'],
    queryFn: () => alertsApi.list(),
  })
  const alerts = useMemo(() => data?.alerts ?? [], [data])

  useEffect(() => {
    if (!socket) {
      return
    }
    const onTriggered = () => {
      queryClient.invalidateQueries({ queryKey: ['alerts'] })
      toast.success('Alert triggered')
    }
    const onDelivery = (payload: { status?: string; http_status?: number | null }) => {
      queryClient.invalidateQueries({ queryKey: ['alerts'] })
      if (payload?.status === 'FAILED') {
        toast.error(`Webhook delivery failed (${payload.http_status ?? 'network error'})`)
      }
    }
    socket.on('alert_triggered', onTriggered)
    socket.on('alert_delivery', onDelivery)
    return () => {
      socket.off('alert_triggered', onTriggered)
      socket.off('alert_delivery', onDelivery)
    }
  }, [socket, queryClient])

  const removeMutation = useMutation({
    mutationFn: (id: string) => alertsApi.remove(id),
    onSuccess: () => {
      toast.success('Alert deleted')
      queryClient.invalidateQueries({ queryKey: ['alerts'] })
    },
    onError: (err: Error) => toast.error(err.message || 'Failed to delete alert'),
  })

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      enabled ? alertsApi.enable(id) : alertsApi.disable(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['alerts'] })
    },
    onError: (err: Error) => toast.error(err.message || 'Failed to update alert'),
  })

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background text-sm">
      <div className="flex shrink-0 items-center justify-between border-b px-3 py-2">
        <span className="font-medium">Alerts</span>
        <Button size="sm" variant="outline" onClick={() => setCreateOpen(true)}>
          Create alert
        </Button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {alerts.length === 0 ? (
          <p className="py-8 text-center text-muted-foreground">
            No alerts yet. Create one on {symbol?.symbol ?? 'any symbol'} and receive a webhook when
            the condition is met.
          </p>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="border-b text-left text-xs text-muted-foreground">
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2">Condition</th>
                <th className="px-3 py-2">Message</th>
                <th className="px-3 py-2">Expiration</th>
                <th className="px-3 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {alerts.map((alert) => (
                <tr key={alert.id} className="border-b align-top">
                  <td className="px-3 py-2">
                    <Badge variant="secondary" className={STATUS_STYLES[alert.status] ?? ''}>
                      {alert.status}
                    </Badge>
                  </td>
                  <td className="px-3 py-2">
                    <div className="font-medium">
                      {alert.source_type === 'price'
                        ? `${alert.symbol} ${OPERATOR_LABELS[alert.operator ?? ''] ?? ''} ${alert.value ?? ''}`
                        : `${alert.symbol} ${alert.signal === 'ANY' ? 'BUY or SELL' : alert.signal} signal`}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {alert.source_type === 'strategy' ? 'Strategy alert — ' : 'Price alert — '}
                      {alert.exchange} · {alert.timeframe}
                    </div>
                  </td>
                  <td className="max-w-[220px] truncate px-3 py-2 text-xs">
                    {alert.message ?? '-'}
                  </td>
                  <td className="px-3 py-2 text-xs">
                    {alert.expiration ? formatTime(alert.expiration) : 'Open-ended'}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex justify-end gap-1">
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={alert.status === 'TRIGGERED'}
                        onClick={() =>
                          toggleMutation.mutate({ id: alert.id, enabled: !alert.enabled })
                        }
                      >
                        {alert.enabled ? 'Disable' : 'Enable'}
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => setLogsAlert(alert)}>
                        Logs
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => removeMutation.mutate(alert.id)}
                      >
                        Delete
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <CreateAlertDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={() => undefined}
        symbol={symbol}
        interval={interval}
      />
      <LogsDialog alert={logsAlert} onClose={() => setLogsAlert(null)} />
    </div>
  )
}
