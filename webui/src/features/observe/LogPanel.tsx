/**
 * 实时日志分面板：恒定深色终端渲染后端日志流。
 *
 * 日志行经 ANSI 解析为带颜色/粗体的片段；新行到达时自动滚动到底部（与旧版
 * 行为一致）；终端底色不随主题变化，保证日志可读性稳定。数据通道由
 * use-logs hook 提供，被会话观察页引用。
 *
 * 性能：每行日志是 memo 化的 LogRow，key 为自增 id；新行到达时历史行跳过重
 * 渲染，只有新增的一行进入 React 提交阶段，避免日志刷屏时 500 行全量重绘。
 */
import { Terminal } from 'lucide-react'
import { memo, useEffect, useRef } from 'react'

import { Card, CardBody, SectionHeading } from '@/components/ui'
import { useLogs } from '@/hooks/use-logs'
import type { AnsiSegment } from '@/lib/ansi'

/**
 * 渲染单行日志（memo：segments 引用不变时跳过重渲染）。
 *
 * @param props.segments 该行的 ANSI 片段数组。
 * @returns 日志行元素；偶数行带斑马纹。
 */
const LogRow = memo(function LogRow({ segments }: { segments: AnsiSegment[] }) {
  return (
    <div className="rounded px-2 py-0.5 whitespace-pre-wrap [overflow-wrap:anywhere] even:bg-white/[0.035] hover:bg-white/[0.07]">
      {segments.map((segment, segmentIndex) => (
        <span
          key={segmentIndex}
          style={segment.color ? { color: segment.color } : undefined}
          className={segment.bold ? 'font-bold' : undefined}
        >
          {segment.text}
        </span>
      ))}
    </div>
  )
})

/**
 * 渲染实时日志分面板。
 *
 * @param props.enabled 是否建立日志通道（会话观察页挂载期间为 `true`）。
 * @returns 日志卡片；头部显示连接状态，主体为深色终端。
 */
export function LogPanel({ enabled }: { enabled: boolean }) {
  const { lines, status } = useLogs(enabled)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  // 新日志到达时保持滚动到底部，与旧版逐行 append 后的滚动行为一致。
  useEffect(() => {
    const container = scrollRef.current
    if (container) container.scrollTop = container.scrollHeight
  }, [lines])

  return (
    <Card id="logs" aria-label="实时日志" className="animate-rise scroll-mt-6">
      <SectionHeading
        title="实时日志"
        subtitle={status}
        icon={<Terminal />}
        tint="olive"
        actions={
          status === '已连接' ? (
            <div className="flex items-center gap-3 text-xs text-muted-foreground">
              <span className="font-mono tabular-nums">当前 {lines.length} 条</span>
              <span className="flex items-center gap-1.5">
                <span className="live-dot" aria-hidden="true" />
                已连接
              </span>
            </div>
          ) : undefined
        }
      />
      <CardBody>
        <div
          ref={scrollRef}
          aria-live="polite"
          className="flex h-96 flex-col gap-1 overflow-y-auto rounded-lg border border-terminal-border bg-terminal p-3 font-mono text-xs leading-6 text-terminal-foreground"
        >
          {lines.map((row) => (
            <LogRow key={row.id} segments={row.segments} />
          ))}
        </div>
      </CardBody>
    </Card>
  )
}
