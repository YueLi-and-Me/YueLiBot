/**
 * 提示词工作台分面板：模板选择、元数据、版本历史与在线编辑。
 *
 * 左侧为模板元数据与版本归档（点击可载入编辑器但不保存），右侧为生效内容
 * 编辑器（恒定深色终端样式）、保存/恢复操作与内置版本对照。本机可编辑模板
 * 允许写操作，固定模板保持只读。状态机由 use-prompts hook 提供。
 */
import { FileText, RefreshCw } from 'lucide-react'

import { Button, Card, CardBody, Field, SectionHeading, Select, Textarea, cn } from '@/components/ui'
import { usePrompts } from '@/hooks/use-prompts'
import { dateTime } from '@/lib/format'

/**
 * 渲染提示词工作台。
 *
 * @param props.enabled 是否启用数据通道（会话观察页挂载期间为 `true`）。
 * @returns 提示词工作台卡片。
 */
export function PromptWorkbench({ enabled }: { enabled: boolean }) {
  const {
    summaries,
    selectedId,
    select,
    detail,
    history,
    editorValue,
    setEditorValue,
    status,
    busy,
    reload,
    save,
    reset,
    loadVersion,
  } = usePrompts(enabled)

  const readOnly = detail?.fixed ?? false
  const saveDisabled = busy || readOnly || !detail
  const resetDisabled = busy || readOnly || !detail || detail.source === 'builtin'

  return (
    <Card id="prompts" aria-label="提示词工作台" className="scroll-mt-6">
      <SectionHeading
        title="提示词工作台"
        subtitle="本机可编辑；远程连接保持只读"
        icon={<FileText />}
        tint="violet"
        actions={
          <>
            <Field label="模板" htmlFor="prompt-select" className="w-64">
              <Select
                id="prompt-select"
                value={selectedId}
                onChange={(event) => select(event.target.value)}
              >
                {summaries.map((summary) => (
                  <option key={summary.id} value={summary.id}>
                    {summary.id} · {summary.source === 'builtin' ? '内置' : '覆盖'}
                  </option>
                ))}
              </Select>
            </Field>
            <Button variant="secondary" className="self-end" onClick={reload} disabled={busy}>
              <RefreshCw className="size-3.5" aria-hidden="true" />
              重新读取
            </Button>
          </>
        }
      />
      <CardBody>
        <div className="grid gap-6 lg:grid-cols-[17rem_minmax(0,1fr)]">
          {/* 元数据与版本历史 */}
          <aside className="flex flex-col gap-5">
            <dl className="flex flex-col divide-y divide-border/60">
              {([
                ['来源', detail ? (detail.source === 'builtin' ? '内置' : '用户覆盖') : '—', false],
                ['哈希', detail?.promptHash ?? '—', true],
                ['占位符', detail ? (detail.placeholders.length ? detail.placeholders.join(', ') : '无') : '—', true],
                ['权限', detail ? (detail.fixed ? '固定模板，只读' : '本机可编辑') : '—', false],
              ] as Array<[string, string, boolean]>).map(([term, value, mono]) => (
                <div key={term} className="flex items-baseline justify-between gap-3 py-1.5">
                  <dt className="flex-none text-xs text-muted-foreground">{term}</dt>
                  <dd className={cn('min-w-0 text-right text-[13px] break-all', mono && 'font-mono text-xs')}>
                    {value}
                  </dd>
                </div>
              ))}
            </dl>
            <div className="flex flex-col gap-2">
              <h3 className="text-[13px] font-semibold text-muted-foreground">版本历史</h3>
              {history.length ? (
                <div className="flex max-h-72 flex-col gap-1 overflow-y-auto pr-1">
                  {history.map((version) => (
                    <button
                      key={`${version.name}-${version.updatedAt}`}
                      type="button"
                      onClick={() => loadVersion(version)}
                      className="cursor-pointer rounded-md px-2.5 py-1.5 text-left text-xs transition-colors hover:bg-accent hover:text-accent-foreground"
                    >
                      <span className="font-medium">{version.name}</span>
                      <span className="ml-1.5 font-mono text-muted-foreground tabular-nums">
                        {dateTime(version.updatedAt)}
                      </span>
                    </button>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">尚无归档版本。</p>
              )}
            </div>
          </aside>

          {/* 编辑器与操作区 */}
          <div className="flex min-w-0 flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <label htmlFor="prompt-content" className="text-xs font-medium text-muted-foreground">
                生效内容
              </label>
              <Textarea
                id="prompt-content"
                spellCheck={false}
                readOnly={readOnly}
                value={editorValue}
                onChange={(event) => setEditorValue(event.target.value)}
                className="h-96 resize-y border-transparent bg-terminal font-mono text-xs leading-relaxed text-terminal-foreground read-only:opacity-80"
              />
            </div>
            <div className="flex flex-wrap items-center gap-2.5">
              <Button onClick={save} disabled={saveDisabled}>校验并保存</Button>
              <Button variant="danger-outline" onClick={reset} disabled={resetDisabled}>
                删除覆盖并恢复内置
              </Button>
              <span aria-live="polite" className="text-xs text-muted-foreground">{status}</span>
            </div>
            {detail ? (
              <details>
                <summary className="cursor-pointer text-xs font-medium text-primary select-none">
                  查看内置版本
                </summary>
                <pre className="mt-1.5 max-h-72 overflow-auto rounded-lg bg-terminal p-3 font-mono text-xs whitespace-pre-wrap text-terminal-foreground">
                  {detail.builtinContent}
                </pre>
              </details>
            ) : null}
          </div>
        </div>
      </CardBody>
    </Card>
  )
}
