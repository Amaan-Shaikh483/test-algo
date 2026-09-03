import type { Extension } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { tags as t } from '@lezer/highlight'
import { createTheme } from '@uiw/codemirror-themes'
import CodeMirror from '@uiw/react-codemirror'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import {
  type PineBacktestMetrics,
  type PineCompileResult,
  type PineEvaluateResult,
  type PineInputSpec,
  type PineScriptSummary,
  type PineStrategy,
  pineApi,
} from '@/api/pine'
import { useSocketContext } from '@/components/socket/SocketProvider'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { pineLanguage } from '@/lib/pine/pineMode'
import type { PineStudyMarker, PineStudyPayload, TradingTerminal } from '@/lib/trading/terminal'
import { useThemeStore } from '@/stores/themeStore'

const DEFAULT_SCRIPT = `//@version=5
indicator("EMA Cross", overlay=true)

fast = input.int(9, "Fast EMA")
slow = input.int(21, "Slow EMA")

emaFast = ta.ema(close, fast)
emaSlow = ta.ema(close, slow)

bull = ta.crossover(emaFast, emaSlow)
bear = ta.crossunder(emaFast, emaSlow)

plot(emaFast, "Fast", color=color.new("#2962ff", 0))
plot(emaSlow, "Slow", color=color.new("#089981", 0))

plotshape(bull, style=shape.triangleup, location=location.belowbar, color=color.new("#089981", 0))
plotshape(bear, style=shape.triangledown, location=location.abovebar, color=color.new("#f23645", 0))
`

interface PineEditorProps {
  /** Focused pane's terminal; the study is rendered on its chart. */
  terminal: TradingTerminal | null
  /** Focused pane's instrument + interval, for evaluate/backtest/strategy. */
  symbol: { symbol: string; exchange: string } | null
  interval: string
}

interface ConsoleLine {
  kind: 'info' | 'error' | 'signal' | 'alert' | 'order' | 'status'
  text: string
  time: number
}

/** Map the runtime shapes onto what terminal.addPineStudy expects. */
function toStudyPayload(result: PineEvaluateResult): PineStudyPayload {
  const shapes = result.shapes ?? []
  const signals = result.signals ?? []
  const markers: PineStudyMarker[] = []

  for (const s of shapes) {
    const up = s.style.includes('up') || s.location.includes('below')
    markers.push({
      time: s.time,
      position: s.location.includes('below') ? 'belowBar' : 'aboveBar',
      shape: up ? 'triangleUp' : 'triangleDown',
      color: s.color ?? (up ? '#089981' : '#f23645'),
      text: s.text || s.title || undefined,
      size: 'small',
    })
  }
  for (const sig of signals) {
    markers.push({
      time: sig.time,
      position: sig.signal === 'BUY' ? 'belowBar' : 'aboveBar',
      shape: sig.signal === 'BUY' ? 'triangleUp' : 'triangleDown',
      color: sig.signal === 'BUY' ? '#089981' : '#f23645',
      text: `${sig.signal}${sig.kind ? ` ${sig.kind}` : ''}`,
      size: 'big',
    })
  }
  markers.sort((a, b) => a.time - b.time)

  return {
    id: `pine-${Date.now()}`,
    title: result.meta?.title ?? 'Pine study',
    overlay: result.meta?.overlay !== false,
    plots: (result.plots ?? []).map((p) => ({
      id: p.id,
      title: p.title,
      color: p.color,
      data: p.data,
    })),
    markers,
    hlines: (result.hlines ?? []).map((h) => ({ price: h.price, title: h.title, color: h.color })),
  }
}

/** Editor palette for Pine, cut down from the python-editor theme. */
function createPineTheme(isDark: boolean): Extension {
  return createTheme({
    theme: isDark ? 'dark' : 'light',
    settings: {
      background: 'transparent',
      foreground: isDark ? '#e5e5e5' : '#171717',
      caret: isDark ? '#38bdf8' : '#0284c7',
      selection: isDark ? 'rgba(56, 189, 248, 0.2)' : 'rgba(2, 132, 199, 0.2)',
      lineHighlight: isDark ? 'rgba(255, 255, 255, 0.03)' : 'rgba(0, 0, 0, 0.02)',
      gutterBackground: 'transparent',
      gutterForeground: isDark ? 'rgba(255, 255, 255, 0.4)' : 'rgba(0, 0, 0, 0.4)',
      gutterBorder: 'transparent',
    },
    styles: [
      { tag: t.keyword, color: '#c084fc' },
      { tag: t.bool, color: '#c084fc' },
      { tag: t.string, color: '#34d399' },
      { tag: t.number, color: '#fb923c' },
      { tag: t.comment, color: isDark ? '#6b7280' : '#9ca3af', fontStyle: 'italic' },
      { tag: t.operator, color: isDark ? '#a3a3a3' : '#525252' },
      { tag: t.function(t.variableName), color: '#38bdf8' },
      { tag: t.className, color: '#facc15' },
      { tag: t.variableName, color: isDark ? '#e5e5e5' : '#171717' },
      { tag: t.meta, color: '#f472b6' },
    ],
  })
}

function createPineBaseTheme(isDark: boolean): Extension {
  const borderColor = isDark ? 'rgba(255, 255, 255, 0.1)' : 'rgba(0, 0, 0, 0.1)'
  return EditorView.theme({
    '&': {
      fontSize: '13px',
      fontFamily: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
      height: '100%',
      backgroundColor: 'transparent',
    },
    '.cm-scroller': {
      fontFamily: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
      lineHeight: '1.5',
      overflow: 'auto',
    },
    '.cm-gutters': {
      backgroundColor: 'transparent',
      borderRight: `1px solid ${borderColor}`,
      color: isDark ? 'rgba(255, 255, 255, 0.35)' : 'rgba(0, 0, 0, 0.35)',
    },
    '.cm-activeLine': {
      backgroundColor: isDark ? 'rgba(255, 255, 255, 0.04)' : 'rgba(0, 0, 0, 0.03)',
    },
    '.cm-activeLineGutter': {
      backgroundColor: isDark ? 'rgba(255, 255, 255, 0.06)' : 'rgba(0, 0, 0, 0.05)',
    },
    '.cm-content': { padding: '8px 0' },
  })
}

function fmtTime(ms: number): string {
  const d = new Date(ms < 1e12 ? ms * 1000 : ms)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export function PineEditor({ terminal, symbol, interval }: PineEditorProps) {
  const { mode, appMode } = useThemeStore()
  const isDark = mode === 'dark' || appMode === 'analyzer'
  const socket = useSocketContext().socket ?? null

  const [code, setCode] = useState(DEFAULT_SCRIPT)
  const [scriptName, setScriptName] = useState('EMA Cross')
  const [savedId, setSavedId] = useState<number | null>(null)
  const [scripts, setScripts] = useState<PineScriptSummary[]>([])
  const [compiled, setCompiled] = useState<PineCompileResult | null>(null)
  const [inputs, setInputs] = useState<Record<string, unknown>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [onChart, setOnChart] = useState(false)
  const [console_, setConsole] = useState<ConsoleLine[]>([])
  const [strategy, setStrategy] = useState<PineStrategy | null>(null)
  const [metrics, setMetrics] = useState<PineBacktestMetrics | null>(null)
  const [quantity, setQuantity] = useState(1)
  const [product, setProduct] = useState('MIS')
  const [liveConfirmOpen, setLiveConfirmOpen] = useState(false)
  const strategyRef = useRef<PineStrategy | null>(null)
  strategyRef.current = strategy

  const log = useCallback((kind: ConsoleLine['kind'], text: string) => {
    setConsole((prev) => [...prev.slice(-400), { kind, text, time: Date.now() }])
  }, [])

  const refreshScripts = useCallback(async () => {
    try {
      const res = await pineApi.listScripts()
      setScripts(res.scripts ?? [])
    } catch {
      /* listing is best-effort */
    }
  }, [])

  const refreshStrategy = useCallback(async () => {
    try {
      const res = await pineApi.listStrategies()
      // Show the first strategy for the currently viewed instrument, else the
      // most recent one - the panel manages one active instance at a time.
      const list = res.strategies ?? []
      const match =
        list.find((s) => symbol && s.symbol === symbol.symbol && s.exchange === symbol.exchange) ??
        list[0] ??
        null
      setStrategy(match)
    } catch {
      /* not authenticated yet, or no strategies */
    }
  }, [symbol])

  useEffect(() => {
    void refreshScripts()
    void refreshStrategy()
  }, [refreshScripts, refreshStrategy])

  /* Realtime strategy events from the shared Socket.IO connection. */
  useEffect(() => {
    if (!socket) return
    const mine = (id: unknown) => strategyRef.current && id === strategyRef.current.id

    const onSignal = (d: {
      strategy_id: string
      signal: string
      kind: string
      symbol: string
      price: number
    }) => {
      if (!mine(d.strategy_id)) return
      log('signal', `SIGNAL ${d.signal} ${d.kind ?? ''} ${d.symbol} @ ${d.price}`)
    }
    const onAlert = (d: { strategy_id: string; title: string; message: string }) => {
      if (!mine(d.strategy_id)) return
      log('alert', `ALERT ${d.title ?? ''} ${d.message ?? ''}`.trim())
    }
    const onStatus = (d: { strategy_id: string; status: string; detail: string }) => {
      if (!mine(d.strategy_id)) return
      log('status', `STATUS ${d.status}${d.detail ? ` - ${d.detail}` : ''}`)
      if (strategyRef.current) setStrategy({ ...strategyRef.current, status: d.status })
    }
    const onOrder = (d: {
      strategy_id: string
      signal: string
      order_id: string
      order_status: string
      message: string
    }) => {
      if (!mine(d.strategy_id)) return
      log('order', `ORDER ${d.signal} id=${d.order_id ?? '-'} ${d.order_status ?? d.message ?? ''}`)
    }

    socket.on('strategy_signal', onSignal)
    socket.on('strategy_alert', onAlert)
    socket.on('strategy_status', onStatus)
    socket.on('strategy_order', onOrder)
    return () => {
      socket.off('strategy_signal', onSignal)
      socket.off('strategy_alert', onAlert)
      socket.off('strategy_status', onStatus)
      socket.off('strategy_order', onOrder)
    }
  }, [socket, log])

  const extensions = useMemo(
    () => [
      pineLanguage,
      createPineTheme(isDark),
      createPineBaseTheme(isDark),
      EditorView.lineWrapping,
    ],
    [isDark]
  )

  /* Actions */

  const doCompile = async () => {
    setBusy('compile')
    try {
      const res = await pineApi.compile(code)
      setCompiled(res)
      if (res.status === 'success') {
        setInputs({})
        log('info', `Compiled: ${res.title} (${res.kind}, overlay=${String(res.overlay)})`)
        toast.success(`Compiled: ${res.title}`)
      } else {
        const e = res.error
        log('error', `Compile error ${e ? `${e.line}:${e.column} ${e.message}` : 'unknown'}`)
        toast.error(e ? `Line ${e.line}:${e.column} - ${e.message}` : 'Compile failed')
      }
    } catch (e) {
      log('error', `Compile failed: ${(e as Error).message}`)
      toast.error((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const doEvaluate = async () => {
    if (!symbol) {
      toast.error('No symbol loaded on the focused chart')
      return
    }
    setBusy('chart')
    try {
      const res = await pineApi.evaluate({
        code,
        symbol: symbol.symbol,
        exchange: symbol.exchange,
        timeframe: interval,
        inputs,
      })
      if (res.status !== 'success' || !res.meta) {
        const err = res.error
        throw new Error(
          err ? `Line ${err.line}:${err.column} - ${err.message}` : 'Evaluation failed'
        )
      }
      terminal?.addPineStudy(toStudyPayload(res))
      setOnChart(true)
      log(
        'info',
        `Added to chart: ${res.meta.title} (${res.plots?.length ?? 0} plots, ${res.signals?.length ?? 0} signals)`
      )
      toast.success(`${res.meta.title} added to chart`)
    } catch (e) {
      log('error', `Evaluate failed: ${(e as Error).message}`)
      toast.error((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const removeFromChart = () => {
    terminal?.removePineStudy()
    setOnChart(false)
    log('info', 'Removed study from chart')
  }

  const doSave = async () => {
    if (!scriptName.trim()) {
      toast.error('Script needs a name')
      return
    }
    setBusy('save')
    try {
      if (savedId != null) {
        await pineApi.updateScript(savedId, { name: scriptName, code })
        toast.success('Script updated')
      } else {
        const res = await pineApi.saveScript(scriptName, code)
        setSavedId(res.id)
        toast.success('Script saved')
      }
      await refreshScripts()
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const doLoad = async (id: number) => {
    setBusy('load')
    try {
      const res = await pineApi.getScript(id)
      setCode(res.script.code)
      setScriptName(res.script.name)
      setSavedId(res.script.id)
      setCompiled(null)
      setMetrics(null)
      log('info', `Loaded script: ${res.script.name}`)
      toast.success(`Loaded: ${res.script.name}`)
    } catch (e) {
      toast.error((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const doDeleteScript = async (id: number) => {
    try {
      await pineApi.deleteScript(id)
      if (savedId === id) setSavedId(null)
      await refreshScripts()
      toast.success('Script deleted')
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  const doBacktest = async () => {
    if (!symbol) {
      toast.error('No symbol loaded on the focused chart')
      return
    }
    setBusy('backtest')
    try {
      const res = await pineApi.backtest({
        code,
        symbol: symbol.symbol,
        exchange: symbol.exchange,
        timeframe: interval,
        inputs,
      })
      if (res.status !== 'success' || !res.metrics) {
        const err = res.error
        throw new Error(err ? `Line ${err.line}:${err.column} - ${err.message}` : 'Backtest failed')
      }
      setMetrics(res.metrics)
      log(
        'info',
        `Backtest: ${res.metrics.total_trades} trades, net ${res.metrics.net_profit}, DD ${res.metrics.max_drawdown}`
      )
      toast.success('Backtest complete')
    } catch (e) {
      log('error', `Backtest failed: ${(e as Error).message}`)
      toast.error((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const strategyAction = async (action: () => Promise<unknown>, label: string) => {
    const s = strategyRef.current
    if (!s) return
    setBusy(label)
    try {
      await action()
      log('status', `${label} requested for ${s.name}`)
      await refreshStrategy()
    } catch (e) {
      log('error', `${label} failed: ${(e as Error).message}`)
      toast.error((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const doCreateStrategy = async () => {
    if (!symbol) {
      toast.error('No symbol loaded on the focused chart')
      return
    }
    if (!scriptName.trim()) {
      toast.error('Script needs a name')
      return
    }
    setBusy('create')
    try {
      let id = savedId
      if (id == null) {
        const saved = await pineApi.saveScript(scriptName, code)
        id = saved.id
        setSavedId(id)
        await refreshScripts()
      }
      const res = await pineApi.createStrategy({
        script_id: id,
        name: `${scriptName} ${symbol.symbol}`,
        symbol: symbol.symbol,
        exchange: symbol.exchange,
        timeframe: interval,
        quantity,
        product,
        inputs,
      })
      setStrategy(res.strategy)
      log('info', `Strategy created: ${res.strategy.name} (${res.strategy.execution_mode})`)
      toast.success('Strategy created in PAPER mode')
    } catch (e) {
      log('error', `Create strategy failed: ${(e as Error).message}`)
      toast.error((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const doEnableLive = async () => {
    const s = strategyRef.current
    if (!s) return
    setLiveConfirmOpen(false)
    setBusy('live')
    try {
      const res = await pineApi.enableLive(s.id)
      setStrategy(res.strategy)
      log('status', `LIVE mode enabled for ${s.name}`)
      toast.warning('Live trading enabled')
    } catch (e) {
      log('error', `Enable live failed: ${(e as Error).message}`)
      toast.error((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  const statusColor = (status: string) => {
    switch (status) {
      case 'RUNNING':
        return 'default'
      case 'PAUSED':
        return 'secondary'
      case 'STOPPED':
        return 'outline'
      case 'ERROR':
        return 'destructive'
      default:
        return 'outline'
    }
  }

  const modeColor = (m: string) => (m === 'LIVE' ? 'destructive' : 'secondary')

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-1.5 border-b px-2 py-1.5">
        <Input
          value={scriptName}
          onChange={(e) => setScriptName(e.target.value)}
          className="h-7 w-40 text-xs"
          placeholder="Script name"
          aria-label="Script name"
        />
        <Button
          size="sm"
          variant="outline"
          className="h-7 text-xs"
          onClick={doCompile}
          disabled={busy !== null}
        >
          {busy === 'compile' ? 'Compiling...' : 'Compile'}
        </Button>
        <Button
          size="sm"
          variant="outline"
          className="h-7 text-xs"
          onClick={doEvaluate}
          disabled={busy !== null}
        >
          {busy === 'chart' ? 'Adding...' : onChart ? 'Refresh on Chart' : 'Add to Chart'}
        </Button>
        {onChart && (
          <Button size="sm" variant="outline" className="h-7 text-xs" onClick={removeFromChart}>
            Remove
          </Button>
        )}
        <Button
          size="sm"
          variant="outline"
          className="h-7 text-xs"
          onClick={doSave}
          disabled={busy !== null}
        >
          Save
        </Button>
        <Select
          value={savedId != null ? String(savedId) : ''}
          onValueChange={(v) => v && void doLoad(Number(v))}
        >
          <SelectTrigger className="h-7 w-36 text-xs" aria-label="Load saved script">
            <SelectValue placeholder="Load script" />
          </SelectTrigger>
          <SelectContent>
            {scripts.length === 0 && (
              <div className="px-2 py-1.5 text-xs text-muted-foreground">No saved scripts</div>
            )}
            {scripts.map((s) => (
              <div key={s.id} className="flex items-center">
                <button
                  type="button"
                  className="flex-1 px-2 py-1.5 text-left text-xs hover:bg-accent"
                  onClick={() => void doLoad(s.id)}
                >
                  {s.name}
                </button>
                <button
                  type="button"
                  className="px-2 py-1.5 text-xs text-muted-foreground hover:text-destructive"
                  title={`Delete ${s.name}`}
                  aria-label={`Delete ${s.name}`}
                  onClick={() => void doDeleteScript(s.id)}
                >
                  x
                </button>
              </div>
            ))}
          </SelectContent>
        </Select>
        <Button
          size="sm"
          variant="outline"
          className="h-7 text-xs"
          onClick={doBacktest}
          disabled={busy !== null}
        >
          {busy === 'backtest' ? 'Testing...' : 'Backtest'}
        </Button>
        <div className="ml-auto flex items-center gap-1.5">
          {symbol ? (
            <span className="text-xs text-muted-foreground">
              {symbol.exchange}:{symbol.symbol} {interval}
            </span>
          ) : (
            <span className="text-xs text-muted-foreground/60">no chart symbol</span>
          )}
        </div>
      </div>

      {/* Body: editor + side panel */}
      <div className="grid min-h-0 flex-1 grid-cols-[1fr_320px]">
        <div className="min-h-0 overflow-hidden border-r">
          <CodeMirror
            value={code}
            onChange={setCode}
            extensions={extensions}
            theme={isDark ? 'dark' : 'light'}
            height="100%"
            basicSetup={{
              lineNumbers: true,
              highlightActiveLineGutter: true,
              highlightActiveLine: true,
              foldGutter: true,
              bracketMatching: true,
              closeBrackets: true,
              autocompletion: false,
              tabSize: 4,
            }}
          />
        </div>

        <div className="flex min-h-0 flex-col">
          <Tabs defaultValue="console" className="flex min-h-0 flex-1 flex-col">
            <TabsList className="mx-1 mt-1 grid w-auto grid-cols-4">
              <TabsTrigger value="console" className="text-xs">
                Console
              </TabsTrigger>
              <TabsTrigger value="inputs" className="text-xs">
                Inputs
              </TabsTrigger>
              <TabsTrigger value="status" className="text-xs">
                Strategy
              </TabsTrigger>
              <TabsTrigger value="backtest" className="text-xs">
                Backtest
              </TabsTrigger>
            </TabsList>

            <TabsContent value="console" className="min-h-0 flex-1 overflow-y-auto p-1">
              {console_.length === 0 && (
                <p className="px-1 py-2 text-xs text-muted-foreground">
                  Compile to check for errors. Signals, alerts and orders appear here in realtime.
                </p>
              )}
              {console_.map((l, i) => (
                <div
                  key={i}
                  className={
                    'font-mono text-[11px] leading-5 ' +
                    (l.kind === 'error'
                      ? 'text-red-500'
                      : l.kind === 'signal'
                        ? 'text-emerald-500'
                        : l.kind === 'order'
                          ? 'text-sky-500'
                          : l.kind === 'alert'
                            ? 'text-amber-500'
                            : 'text-muted-foreground')
                  }
                >
                  [{fmtTime(l.time)}] {l.text}
                </div>
              ))}
            </TabsContent>

            <TabsContent value="inputs" className="min-h-0 flex-1 overflow-y-auto p-2">
              {compiled?.inputs?.length ? (
                compiled.inputs.map((spec: PineInputSpec) => (
                  <div key={spec.name} className="mb-2">
                    <Label className="text-xs" htmlFor={`pine-input-${spec.name}`}>
                      {spec.title || spec.name}
                    </Label>
                    {spec.type === 'bool' ? (
                      <Select
                        value={String(inputs[spec.name] ?? spec.default ?? false)}
                        onValueChange={(v) =>
                          setInputs((p) => ({ ...p, [spec.name]: v === 'true' }))
                        }
                      >
                        <SelectTrigger id={`pine-input-${spec.name}`} className="h-7 text-xs">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="true">true</SelectItem>
                          <SelectItem value="false">false</SelectItem>
                        </SelectContent>
                      </Select>
                    ) : (
                      <Input
                        id={`pine-input-${spec.name}`}
                        type={spec.type === 'int' || spec.type === 'float' ? 'number' : 'text'}
                        className="h-7 text-xs"
                        value={String(inputs[spec.name] ?? spec.default ?? '')}
                        onChange={(e) =>
                          setInputs((p) => ({
                            ...p,
                            [spec.name]:
                              spec.type === 'int' || spec.type === 'float'
                                ? Number(e.target.value)
                                : e.target.value,
                          }))
                        }
                      />
                    )}
                  </div>
                ))
              ) : (
                <p className="text-xs text-muted-foreground">
                  Compile the script to see its inputs.
                </p>
              )}
            </TabsContent>

            <TabsContent value="status" className="min-h-0 flex-1 overflow-y-auto p-2">
              {strategy ? (
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-xs font-medium">{strategy.name}</span>
                    <Badge variant={statusColor(strategy.status) as never} className="text-[10px]">
                      {strategy.status}
                    </Badge>
                    <Badge
                      variant={modeColor(strategy.execution_mode) as never}
                      className="text-[10px]"
                    >
                      {strategy.execution_mode}
                    </Badge>
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {strategy.exchange}:{strategy.symbol} {strategy.timeframe} - qty{' '}
                    {strategy.quantity} {strategy.product}
                  </div>
                  {strategy.last_error && (
                    <div className="rounded border border-red-500/40 bg-red-500/10 p-1.5 text-[11px] text-red-500">
                      {strategy.last_error}
                    </div>
                  )}
                  <div className="flex flex-wrap gap-1">
                    {strategy.status !== 'RUNNING' && (
                      <Button
                        size="sm"
                        className="h-7 text-xs"
                        disabled={busy !== null}
                        onClick={() =>
                          void strategyAction(() => pineApi.startStrategy(strategy.id), 'start')
                        }
                      >
                        {busy === 'start' ? 'Starting...' : 'Start'}
                      </Button>
                    )}
                    {strategy.status === 'RUNNING' && (
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 text-xs"
                        disabled={busy !== null}
                        onClick={() =>
                          void strategyAction(() => pineApi.pauseStrategy(strategy.id), 'pause')
                        }
                      >
                        Pause
                      </Button>
                    )}
                    {strategy.status === 'PAUSED' && (
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 text-xs"
                        disabled={busy !== null}
                        onClick={() =>
                          void strategyAction(() => pineApi.resumeStrategy(strategy.id), 'resume')
                        }
                      >
                        Resume
                      </Button>
                    )}
                    {(strategy.status === 'RUNNING' || strategy.status === 'PAUSED') && (
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 text-xs"
                        disabled={busy !== null}
                        onClick={() =>
                          void strategyAction(() => pineApi.stopStrategy(strategy.id), 'stop')
                        }
                      >
                        Stop
                      </Button>
                    )}
                    {strategy.execution_mode === 'PAPER' ? (
                      <Button
                        size="sm"
                        variant="destructive"
                        className="h-7 text-xs"
                        disabled={busy !== null}
                        onClick={() => setLiveConfirmOpen(true)}
                      >
                        Enable Live
                      </Button>
                    ) : (
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 text-xs"
                        disabled={busy !== null}
                        onClick={() =>
                          void strategyAction(() => pineApi.enablePaper(strategy.id), 'paper')
                        }
                      >
                        Back to Paper
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 text-xs"
                      disabled={busy !== null}
                      onClick={() => {
                        setStrategy(null)
                        void pineApi.deleteStrategy(strategy.id)
                      }}
                    >
                      Delete
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="space-y-2">
                  <p className="text-xs text-muted-foreground">
                    Create a server-side strategy instance for the focused symbol. It keeps running
                    after this tab closes. New strategies always start in PAPER mode.
                  </p>
                  <div className="flex items-center gap-2">
                    <Label className="text-xs" htmlFor="pine-qty">
                      Qty
                    </Label>
                    <Input
                      id="pine-qty"
                      type="number"
                      min={1}
                      className="h-7 w-20 text-xs"
                      value={quantity}
                      onChange={(e) => setQuantity(Math.max(1, Number(e.target.value) || 1))}
                    />
                    <Label className="text-xs" htmlFor="pine-product">
                      Product
                    </Label>
                    <Select value={product} onValueChange={setProduct}>
                      <SelectTrigger id="pine-product" className="h-7 w-24 text-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="MIS">MIS</SelectItem>
                        <SelectItem value="CNC">CNC</SelectItem>
                        <SelectItem value="NRML">NRML</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <Button
                    size="sm"
                    className="h-7 text-xs"
                    onClick={doCreateStrategy}
                    disabled={busy !== null}
                  >
                    {busy === 'create' ? 'Creating...' : 'Create Strategy'}
                  </Button>
                </div>
              )}
            </TabsContent>

            <TabsContent value="backtest" className="min-h-0 flex-1 overflow-y-auto p-2">
              {metrics ? (
                <div className="space-y-1.5 text-xs">
                  <div className="grid grid-cols-2 gap-x-3 gap-y-1">
                    <span className="text-muted-foreground">Trades</span>
                    <span>{metrics.total_trades}</span>
                    <span className="text-muted-foreground">Win rate</span>
                    <span>{metrics.win_rate}%</span>
                    <span className="text-muted-foreground">Gross P&L</span>
                    <span>{metrics.gross_profit - metrics.gross_loss}</span>
                    <span className="text-muted-foreground">Net P&L</span>
                    <span className={metrics.net_profit >= 0 ? 'text-emerald-500' : 'text-red-500'}>
                      {metrics.net_profit}
                    </span>
                    <span className="text-muted-foreground">Max drawdown</span>
                    <span className="text-red-500">{metrics.max_drawdown}</span>
                    <span className="text-muted-foreground">Profit factor</span>
                    <span>{metrics.profit_factor ?? 'inf'}</span>
                    <span className="text-muted-foreground">Return</span>
                    <span>{metrics.return_pct}%</span>
                    <span className="text-muted-foreground">Final equity</span>
                    <span>{metrics.final_equity}</span>
                  </div>
                  {metrics.trade_list.length > 0 && (
                    <details className="mt-1">
                      <summary className="cursor-pointer text-muted-foreground">
                        Trade list ({metrics.trade_list.length})
                      </summary>
                      <div className="mt-1 max-h-48 overflow-y-auto">
                        {metrics.trade_list.map((tr, i) => (
                          <div key={i} className="font-mono text-[10px] leading-4">
                            {tr.direction === 'long' ? 'L' : 'S'} {tr.qty} @ {tr.entry_price} -&gt;{' '}
                            {tr.exit_price ?? '-'} = {tr.pnl}
                          </div>
                        ))}
                      </div>
                    </details>
                  )}
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">Run a backtest to see metrics.</p>
              )}
            </TabsContent>
          </Tabs>
        </div>
      </div>

      {/* Live confirmation dialog */}
      {liveConfirmOpen && strategy && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
          role="dialog"
          aria-modal="true"
        >
          <div className="max-w-md rounded-lg border bg-background p-4 shadow-lg">
            <h3 className="text-sm font-semibold text-red-500">Enable Live Trading</h3>
            <p className="mt-2 text-xs text-muted-foreground">
              {strategy.name} will place <strong>real orders</strong> with {strategy.quantity} qty (
              {strategy.product}) on {strategy.exchange}:{strategy.symbol} {strategy.timeframe}{' '}
              through your default broker. Paper mode can be re-enabled at any time.
            </p>
            <p className="mt-2 text-xs text-muted-foreground">
              Are you sure you want to enable live trading?
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <Button
                size="sm"
                variant="outline"
                className="h-7 text-xs"
                onClick={() => setLiveConfirmOpen(false)}
              >
                Cancel
              </Button>
              <Button
                size="sm"
                variant="destructive"
                className="h-7 text-xs"
                onClick={doEnableLive}
                disabled={busy !== null}
              >
                {busy === 'live' ? 'Enabling...' : 'Yes, enable live trading'}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
