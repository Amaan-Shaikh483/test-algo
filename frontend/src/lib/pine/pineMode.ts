import { StreamLanguage, type StreamParser } from '@codemirror/language'
import type { Extension } from '@codemirror/state'

/**
 * Pine Script (v5 subset) highlighting for CodeMirror, via StreamLanguage.
 * The Pine engine on the server does the real parsing; this mode only
 * colours the editor so it reads like the TradingView editor.
 */

const KEYWORDS = new Set([
  'if',
  'else',
  'for',
  'while',
  'var',
  'varip',
  'true',
  'false',
  'na',
  'and',
  'or',
  'not',
  'to',
  'by',
  'import',
  'export',
  'method',
  'type',
  'switch',
  'break',
  'continue',
  'return',
])

const BUILTIN_NAMESPACES = new Set([
  'ta',
  'math',
  'strategy',
  'input',
  'plot',
  'color',
  'timeframe',
  'syminfo',
  'alert',
  'request',
  'array',
  'str',
])

function isAlpha(ch: string): boolean {
  return /[A-Za-z_]/.test(ch)
}

function isDigit(ch: string): boolean {
  return /[0-9]/.test(ch)
}

interface PineState {
  /** Inside a triple-quoted (multi-line) string. */
  inBlockString: boolean
}

const pineMode: StreamParser<PineState> = {
  name: 'pine',
  startState: () => ({ inBlockString: false }),
  token(stream, state: PineState) {
    // Multi-line strings run until the closing triple quote.
    if (state.inBlockString) {
      const end = stream.string.indexOf('"""', stream.pos)
      if (end === -1) {
        stream.skipToEnd()
        return 'string'
      }
      stream.pos = end + 3
      state.inBlockString = false
      return 'string'
    }

    if (stream.eatSpace()) return null

    // Comments run to end of line.
    if (stream.match('//')) {
      stream.skipToEnd()
      return 'comment'
    }

    // Triple-quoted strings open here and may span lines.
    if (stream.match('"""')) {
      const end = stream.string.indexOf('"""', stream.pos)
      if (end === -1) {
        stream.skipToEnd()
        state.inBlockString = true
        return 'string'
      }
      stream.pos = end + 3
      return 'string'
    }

    // Single- and double-quoted single-line strings, with escapes.
    if (stream.match(/^"(\\"|[^"\\\n])*"?/)) return 'string'
    if (stream.match(/^'(\\'|[^'\\\n])*'?/)) return 'string'

    // Numbers: 123, 1.5, 1e-3
    if (isDigit(stream.peek() ?? '')) {
      stream.match(/^\d+(\.\d+)?([eE][+-]?\d+)?/)
      return 'number'
    }

    // Identifiers / keywords, including dotted namespaces (ta.sma).
    if (isAlpha(stream.peek() ?? '')) {
      stream.match(/^[A-Za-z_][A-Za-z0-9_]*/)
      const name = stream.current()
      if (KEYWORDS.has(name)) {
        return name === 'true' || name === 'false' ? 'bool' : 'keyword'
      }
      if (BUILTIN_NAMESPACES.has(name) && stream.peek() === '.') {
        return 'className'
      }
      // A call position (next non-space char is '(') reads as a function.
      const rest = stream.string.slice(stream.pos)
      if (/^\s*\(/.test(rest)) return 'def'
      return 'variableName'
    }

    // Operators and punctuation.
    if (stream.match(/^[+\-*/%<>=!:?&|#]+/)) return 'operator'
    if (stream.match(/^@/)) return 'meta'
    stream.next()
    return 'punctuation'
  },
  languageData: {
    commentTokens: { line: '//', block: { open: '/*', close: '*/' } },
  },
}

export const pineLanguage = StreamLanguage.define(pineMode)

/** Keyword completion for the editor, optional extension. */
export function pineModeExtension(): Extension[] {
  return [pineLanguage]
}
