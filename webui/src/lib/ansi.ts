/**
 * 实时日志的 ANSI 转义解析。
 *
 * 后端日志行带 SGR 颜色/粗体序列，本模块把单行日志解析为带样式的文本片段
 * 数组，由日志面板渲染为彩色 DOM。支持基础色、粗体、24 位 RGB 与 256 色；
 * 解析逻辑移植自旧版渲染入口，颜色行为保持一致。
 */

/** 单个日志文本片段的样式描述。 */
export interface AnsiSegment {
  /** 片段文本内容。 */
  text: string
  /** CSS 颜色文本；无色（默认终端色）时为 `undefined`。 */
  color?: string
  /** 是否粗体。 */
  bold: boolean
}

/** ANSI 基础色索引到终端配色的映射，与恒定深色终端底色配套。 */
const BASIC_COLORS: Record<number, string> = {
  31: '#ff6b6b',
  33: '#ffd166',
  35: '#d787ff',
}

/**
 * 将 ANSI 256 色索引转换为 CSS 颜色文本。
 *
 * @param index ANSI 颜色索引，通常范围为 0~255。
 * @returns 对应的十六进制或 `rgb()` 颜色文本；超出范围时按算法结果生成颜色。
 */
function ansi256(index: number): string {
  if (index < 16) {
    const colors = ['#000000', '#800000', '#008000', '#808000', '#000080', '#800080', '#008080', '#c0c0c0', '#808080', '#ff0000', '#00ff00', '#ffff00', '#0000ff', '#ff00ff', '#00ffff', '#ffffff']
    return colors[index] ?? '#d7e4f5'
  }
  if (index >= 232) {
    const value = 8 + (index - 232) * 10
    return `rgb(${value} ${value} ${value})`
  }
  const offset = index - 16
  const levels = [0, 95, 135, 175, 215, 255]
  return `rgb(${levels[Math.floor(offset / 36)]} ${levels[Math.floor(offset / 6) % 6]} ${levels[offset % 6]})`
}

/**
 * 解析一行含 ANSI SGR 转义序列的日志文本。
 *
 * @param line 原始日志行。
 * @returns 带颜色与粗体标记的片段数组；无转义时返回单个默认样式片段。
 * @remarks 按 SGR 序列切分文本，每个片段绑定解析到该位置时的颜色与粗体状态；
 * 序列 `0` 重置样式，`1` 置粗体，`38;2;r;g;b` 为 24 位色，`38;5;n` 为 256 色。
 */
export function parseAnsiLine(line: string): AnsiSegment[] {
  const segments: AnsiSegment[] = []
  let color = ''
  let bold = false
  let cursor = 0
  const pattern = /\x1b\[([0-9;]*)m/g
  for (const match of line.matchAll(pattern)) {
    const index = match.index ?? 0
    if (index > cursor) {
      segments.push({
        text: line.slice(cursor, index),
        color: color || undefined,
        bold,
      })
    }
    const codes = (match[1] ?? '').split(';').filter(Boolean).map(Number)
    if (!codes.length || codes.includes(0)) {
      color = ''
      bold = false
    }
    if (codes.includes(1)) bold = true
    for (const code of codes) if (BASIC_COLORS[code]) color = BASIC_COLORS[code]
    // 24 位颜色和 256 色都使用前缀 38；分别读取后续模式和值。
    const trueColorAt = codes.indexOf(38)
    if (trueColorAt >= 0 && codes[trueColorAt + 1] === 2) {
      color = `rgb(${codes[trueColorAt + 2]} ${codes[trueColorAt + 3]} ${codes[trueColorAt + 4]})`
    } else if (trueColorAt >= 0 && codes[trueColorAt + 1] === 5) {
      color = ansi256(codes[trueColorAt + 2] ?? 15)
    }
    cursor = index + match[0].length
  }
  if (cursor < line.length) {
    segments.push({ text: line.slice(cursor), color: color || undefined, bold })
  }
  return segments
}
