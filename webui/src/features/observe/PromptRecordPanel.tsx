/**
 * 分阶段调用记录面板：按模型任务列出每次调用，并可展开单份完整请求与产出。
 *
 * 一次回合在多级 Agent 下会产生多份记录（决策一份、生成一份、认知检索各一份），
 * 控制台只看得到最后一条可见产物；本面板按任务筛选后逐份回看「这一级收到什么、
 * 答了什么」。数据通道由 use-prompt-records hook 提供，被会话观察页引用。
 */
import { Layers } from 'lucide-react'

import { Button, Card, CardBody, Chip, Empty, ErrorText, Field, SectionHeading, Select, cn } from '@/components/ui'
import { usePromptRecords } from '@/hooks/use-prompt-records'
import type { PromptRecordSummary } from '@/hooks/use-prompt-records'

/**
 * 把毫秒耗时渲染成秒；缺失时回退为占位符。
 *
 * @param value 毫秒数，可能为 null。
 * @returns 形如 `8.98 s` 的文本，或 `—`。
 */
function seconds(value: number | null | undefined): string {
  if (typeof value !== 'number') return '—'
  return `${(value / 1000).toFixed(2)} s`
}

/** 单条摘要行的标题：任务 + 时间 + 会话与回合。 */
function summaryTitle(summary: PromptRecordSummary): string {
  const parts = [summary.task]
  if (summary.turnId !== null) parts.push(`回合 ${summary.turnId}`)
  if (summary.streamId !== null) parts.push(`会话 ${summary.streamId}`)
  return parts.join(' · ')
}

/**
 * 渲染分阶段调用记录面板。
 *
 * @param props.enabled 是否启用数据通道（会话观察页挂载期间为 `true`）。
 * @returns 调用记录卡片。
 */
export function PromptRecordPanel({ enabled }: { enabled: boolean }) {
  const { enabled: recordsEnabled, tasks, task, setTask, records, openKey, toggle, detail, status, reload } =
    usePromptRecords(enabled)

  return (
    <Card id="prompt-records" aria-label="分阶段调用记录" className="animate-rise scroll-mt-6">
      <SectionHeading
        title="分阶段调用记录"
        subtitle="每次模型调用一份，按任务分目录"
        icon={<Layers />}
        actions={
          <>
            <Field label="模型任务" htmlFor="record-task" className="w-56">
              <Select id="record-task" value={task} onChange={(event) => setTask(event.target.value)}>
                <option value="">全部任务</option>
                {tasks.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </Select>
            </Field>
            <Button variant="secondary" onClick={reload}>
              刷新
            </Button>
          </>
        }
      />
      <CardBody className="flex flex-col gap-3">
        {!recordsEnabled ? (
          <Empty>未启用调用记录。在设置里打开「保存分阶段调用记录」后重启生效。</Empty>
        ) : null}
        {status ? <ErrorText>{status}</ErrorText> : null}
        {recordsEnabled && records.length === 0 && !status ? (
          <Empty>还没有记录。发生一次模型调用后这里就会出现。</Empty>
        ) : null}

        {records.map((summary) => {
          const key = `${summary.task}/${summary.name}`
          const open = key === openKey
          return (
            <div key={key} className="rounded-lg border border-border bg-card">
              <button
                type="button"
                onClick={() => toggle(summary)}
                aria-expanded={open}
                className={cn(
                  'flex w-full flex-wrap items-center justify-between gap-2 px-3 py-2 text-left',
                  open && 'border-b border-border',
                )}
              >
                <span className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-xs text-muted-foreground tabular-nums">
                    {summary.at ?? '—'}
                  </span>
                  <span className="text-sm">{summaryTitle(summary)}</span>
                  {summary.errorType ? (
                    <span className="rounded-full bg-destructive-soft px-2 py-0.5 text-xs font-medium text-destructive">
                      {summary.errorType}
                    </span>
                  ) : null}
                </span>
                <span className="flex flex-wrap items-center gap-2 font-mono text-xs text-muted-foreground tabular-nums">
                  <span>{summary.model ?? '—'}</span>
                  <span>首字 {seconds(summary.firstTokenMs)}</span>
                  <span>共 {seconds(summary.totalMs)}</span>
                  <span>{summary.textLength} 字</span>
                </span>
              </button>

              {open ? (
                <div className="flex flex-col gap-3 px-3 py-3">
                  {detail === null ? (
                    <p className="text-sm text-muted-foreground">读取中…</p>
                  ) : (
                    <>
                      <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
                        <Chip label="厂商" value={detail.model?.provider ?? '—'} />
                        <Chip label="阶段" value={detail.stage ?? '—'} />
                        <Chip label="温度" value={String(detail.request?.temperature ?? '—')} />
                        <Chip label="上限" value={String(detail.request?.maxTokens ?? '—')} />
                        <Chip label="分片" value={String(detail.response?.chunks ?? 0)} />
                      </div>

                      <div className="flex flex-col gap-2">
                        <p className="text-xs font-medium text-muted-foreground">请求消息</p>
                        {(detail.request?.messages ?? []).map((message, index) => (
                          <div
                            key={index}
                            className="overflow-hidden rounded-lg border border-terminal-border bg-terminal"
                          >
                            <p className="border-b border-terminal-border px-3 py-1.5 font-mono text-xs text-terminal-foreground/60">
                              {message.role}
                            </p>
                            <pre className="max-h-72 overflow-auto px-3 py-2 font-mono text-xs whitespace-pre-wrap break-words text-terminal-foreground">
                              {message.content}
                            </pre>
                          </div>
                        ))}
                      </div>

                      {detail.response?.reasoning ? (
                        <div className="flex flex-col gap-1">
                          <p className="text-xs font-medium text-muted-foreground">推理</p>
                          <pre className="max-h-72 overflow-auto rounded-lg border border-terminal-border bg-terminal p-3 font-mono text-xs whitespace-pre-wrap break-words text-terminal-foreground">
                            {detail.response.reasoning}
                          </pre>
                        </div>
                      ) : null}

                      <div className="flex flex-col gap-1">
                        <p className="text-xs font-medium text-muted-foreground">模型产出</p>
                        <pre className="max-h-72 overflow-auto rounded-lg border border-terminal-border bg-terminal p-3 font-mono text-xs whitespace-pre-wrap break-words text-terminal-foreground">
                          {detail.response?.text || '（空）'}
                        </pre>
                      </div>

                      {detail.error ? (
                        <ErrorText>
                          {detail.error.type}：{detail.error.message}
                        </ErrorText>
                      ) : null}
                    </>
                  )}
                </div>
              ) : null}
            </div>
          )
        })}
      </CardBody>
    </Card>
  )
}
