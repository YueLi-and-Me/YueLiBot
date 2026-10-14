/**
 * 导入中心页：把外部资料成批写进知识层，并按来源批次整批撤销。
 *
 * 第三方部署的库是空的——知识、事实全部为零，此前唯一的填充通道是只对
 * 作者历史库有效的一次性迁移。本页给任何部署者一个最小导入通道：
 *
 * 1. 粘贴文本或选择本地文本文件（页面侧解码后提交，服务端不做格式转换）；
 * 2. 分块、去重、向量化全部复用运行期链路，导入结果给出新增/去重/向量化计数；
 * 3. 批次列表按时间倒序展示每批的实时条数，删除前先预览影响面再确认。
 *
 * 数据来自 `@/hooks/use-import-center`；导入是同步请求，进行中后端设闸，
 * 并发导入会被 409 拒绝而不是排队。
 */
import { FileUp, ClipboardPaste, RotateCcw, Trash2 } from 'lucide-react'
import { useRef, useState } from 'react'

import { PageFrame } from '@/components/layout/PageFrame'
import { Pane, PaneHeader, PaneTitle, Panes, ScrollArea } from '@/components/layout/panes'
import {
  Button,
  ConfirmDialog,
  Empty,
  ErrorText,
  Loading,
  Metric,
  Textarea,
} from '@/components/ui'
import {
  useImportCenter,
  type ImportDeletePreview,
  type ImportResult,
} from '@/hooks/use-import-center'
import { dateTime } from '@/lib/format'

/** 批次状态的展示口径。 */
const STATUS_LABEL: Record<string, string> = {
  running: '进行中',
  done: '完成',
  failed: '失败',
}

export function ImportCenterPage() {
  const {
    batches,
    limits,
    inProgress,
    error,
    loading,
    reload,
    runImport,
    fetchDeletePreview,
    deleteBatch,
  } = useImportCenter()

  const [text, setText] = useState('')
  const [originName, setOriginName] = useState('')
  const [message, setMessage] = useState<string | null>(null)
  const [result, setResult] = useState<ImportResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState<ImportDeletePreview | null>(null)

  const fileInput = useRef<HTMLInputElement>(null)

  const doImport = async (kind: 'paste' | 'upload', content: string, name: string) => {
    setBusy(true)
    setMessage(null)
    try {
      const got = await runImport(kind, content, name)
      setResult(got)
      setText('')
      setOriginName('')
      setMessage(
        `导入完成：提交 ${got.submitted} 条，新增 ${got.added} 条，` +
          `去重丢弃 ${got.duplicated} 条，向量化 ${got.embedded} 条`,
      )
    } catch (err) {
      setMessage(`导入失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setBusy(false)
    }
  }

  const handlePaste = () => {
    if (!text.trim()) {
      setMessage('粘贴内容为空')
      return
    }
    void doImport('paste', text, '粘贴导入')
  }

  const handleFile = async (file: File) => {
    if (limits && file.size > limits.fileBytes) {
      setMessage(`文件 ${(file.size / 1000).toFixed(0)} KB 超过上限 ${limits.fileBytes / 1000} KB`)
      return
    }
    const content = await file.text()
    void doImport('upload', content, file.name)
  }

  const askDelete = async (batchId: number) => {
    setPreview(await fetchDeletePreview(batchId))
  }

  const confirmDelete = async () => {
    if (preview === null) return
    setBusy(true)
    try {
      const done = await deleteBatch(preview.batch_id)
      setMessage(`批次 ${done.batch_id} 已删除 ${done.deleted} 条`)
    } catch (err) {
      setMessage(`删除失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setBusy(false)
      setPreview(null)
    }
  }

  return (
    <PageFrame
      hint="把资料成批写进知识层，按来源批次整批撤销；去重与向量化复用运行期链路"
      actions={
        <Button variant="secondary" onClick={() => void reload()}>
          <RotateCcw className="size-4" aria-hidden="true" />
          刷新
        </Button>
      }
    >
      <Panes>
        <Pane aria-label="导入">
          <PaneHeader>
            <PaneTitle>导入资料</PaneTitle>
          </PaneHeader>
          <ScrollArea>
            {error ? <div className="px-6 pt-3"><ErrorText>{error}</ErrorText></div> : null}
            {message ? (
              <div className="border-b border-border bg-primary-soft px-6 py-2.5 text-[13px]">{message}</div>
            ) : null}
            <div className="flex max-w-4xl flex-col gap-3 px-6 py-5">
              <Textarea
                aria-label="要导入的资料"
                className="min-h-64"
                placeholder="粘贴要导入的资料；空行分段，较短的相邻段落合并为一条，单独一行的小标题挂在其下各条开头"
                value={text}
                disabled={busy || inProgress}
                onChange={(event) => setText(event.target.value)}
              />
              {limits ? (
                <p className="text-xs text-muted-foreground">
                  上限：单次粘贴 {limits.pasteChars.toLocaleString()} 字符、文件{' '}
                  {(limits.fileBytes / 1000).toFixed(0)} KB、单批 {limits.batchItems} 条
                </p>
              ) : null}
              <div className="flex flex-wrap items-center gap-2">
                <Button disabled={busy || inProgress || !text.trim()} onClick={handlePaste}>
                  <ClipboardPaste className="size-4" aria-hidden="true" />
                  导入粘贴内容
                </Button>
                <Button variant="secondary" disabled={busy || inProgress} onClick={() => fileInput.current?.click()}>
                  <FileUp className="size-4" aria-hidden="true" />
                  选择文本文件
                </Button>
                {originName ? <span className="text-xs text-muted-foreground">{originName}</span> : null}
                <input
                  ref={fileInput}
                  type="file"
                  accept=".txt,.md,.text,text/plain"
                  className="hidden"
                  onChange={(event) => {
                    const file = event.target.files?.[0]
                    event.target.value = ''
                    if (file) void handleFile(file)
                  }}
                />
              </div>
              {busy || inProgress ? (
                <Loading>导入进行中：分块、去重与向量化，完成后自动刷新批次列表…</Loading>
              ) : null}
              {result ? (
                <div className="grid grid-cols-2 gap-x-8 border-y border-border py-2 md:grid-cols-4">
                  <Metric label="提交条目" value={result.submitted} />
                  <Metric label="实际新增" value={result.added} />
                  <Metric label="去重丢弃" value={result.duplicated} />
                  <Metric label="向量化成功" value={result.embedded} />
                </div>
              ) : null}
            </div>
          </ScrollArea>
        </Pane>

        <Pane width="inspector" aria-label="来源批次">
          <PaneHeader>
            <PaneTitle count={batches.length}>来源批次</PaneTitle>
          </PaneHeader>
          <ScrollArea>
            <p className="border-b border-border-subtle px-6 py-2.5 text-xs leading-relaxed text-subtle-foreground">
              实时条数为零的批次已被撤掉；无批次的存量不受任何删除影响。
            </p>
            {loading && batches.length === 0 ? <Loading>正在读取批次…</Loading> : null}
            {!loading && batches.length === 0 ? <Empty>还没有导入过批次</Empty> : null}
            {batches.map((batch) => (
              <div key={batch.id} className="flex flex-col gap-1.5 border-b border-border-subtle px-6 py-3">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-xs text-subtle-foreground">#{batch.id}</span>
                  <strong className="min-w-0 flex-1 truncate text-[13px] font-medium" title={batch.origin_name}>
                    {batch.origin_name}
                  </strong>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="text-destructive hover:bg-destructive-soft hover:text-destructive"
                    disabled={busy || batch.live_count === 0}
                    onClick={() => void askDelete(batch.id)}
                    aria-label={`删除批次 #${batch.id}`}
                  >
                    <Trash2 className="size-3.5" aria-hidden="true" />
                    删除
                  </Button>
                </div>
                <p className="text-xs text-muted-foreground">
                  {STATUS_LABEL[batch.status] ?? batch.status}
                  {' · '}
                  {batch.submitted} 提交 / {batch.added} 新增 / 现存 {batch.live_count}
                </p>
                <p className="text-[11px] text-subtle-foreground">{dateTime(batch.created_at)}</p>
                {batch.summary ? (
                  <p className="line-clamp-2 text-xs text-muted-foreground" title={batch.summary}>{batch.summary}</p>
                ) : null}
              </div>
            ))}
          </ScrollArea>
        </Pane>
      </Panes>

      <ConfirmDialog
        open={preview !== null}
        title={`删除批次 #${preview?.batch_id ?? ''}`}
        description={
          preview
            ? `将删除 ${preview.to_delete} 条知识条目，删除后无法恢复（可重新导入）。`
            : ''
        }
        confirmText="确认删除"
        cancelText="取消"
        onConfirm={() => void confirmDelete()}
        onCancel={() => setPreview(null)}
      />
    </PageFrame>
  )
}
