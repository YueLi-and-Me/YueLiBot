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

import { PageHeader } from '@/components/layout/PageHeader'
import {
  Button,
  Card,
  CardBody,
  Chip,
  ConfirmDialog,
  Empty,
  ErrorText,
  Loading,
  Metric,
  SectionHeading,
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
    <div className="space-y-4">
      <PageHeader
        eyebrow="YUELI · MEMORY"
        title="导入中心"
        subtitle="把资料成批写进知识层，按来源批次整批撤销；去重与向量化复用运行期链路"
        actions={
          <Button variant="ghost" size="sm" onClick={() => void reload()}>
            <RotateCcw className="h-4 w-4" /> 刷新
          </Button>
        }
      />

      {error && <ErrorText>{error}</ErrorText>}
      {message && (
        <Card>
          <CardBody className="py-2 text-sm">{message}</CardBody>
        </Card>
      )}
      {loading && batches.length === 0 && <Loading>正在读取批次…</Loading>}

      <Card>
        <CardBody className="space-y-3">
          <SectionHeading title="导入" />
          <textarea
            className="min-h-40 w-full rounded border bg-transparent p-2 text-sm"
            placeholder="粘贴要导入的资料；空行分段，一段一条知识条目"
            value={text}
            disabled={busy || inProgress}
            onChange={(event) => setText(event.target.value)}
          />
          {limits && (
            <p className="text-xs text-muted-foreground">
              上限：单次粘贴 {limits.pasteChars.toLocaleString()} 字符、文件{' '}
              {(limits.fileBytes / 1000).toFixed(0)} KB、单批 {limits.batchItems} 条
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="secondary"
              disabled={busy || inProgress || !text.trim()}
              onClick={handlePaste}
            >
              <ClipboardPaste className="h-4 w-4" /> 导入粘贴内容
            </Button>
            <Button
              size="sm"
              variant="secondary"
              disabled={busy || inProgress}
              onClick={() => fileInput.current?.click()}
            >
              <FileUp className="h-4 w-4" /> 选择文本文件
            </Button>
            {originName && <span className="text-xs text-muted-foreground">{originName}</span>}
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
          {(busy || inProgress) && (
            <Loading>导入进行中：分块、去重与向量化，完成后自动刷新批次列表…</Loading>
          )}
          {result && (
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <Metric label="提交条目" value={result.submitted} />
              <Metric label="实际新增" value={result.added} />
              <Metric label="去重丢弃" value={result.duplicated} />
              <Metric label="向量化成功" value={result.embedded} />
            </div>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardBody className="space-y-3">
          <SectionHeading
            title="来源批次"
            actions={
              <span className="text-xs text-muted-foreground">
                实时条数为零的批次已被撤掉；无批次的存量不受任何删除影响
              </span>
            }
          />
          {batches.length === 0 ? (
            <Empty>还没有导入过批次</Empty>
          ) : (
            <div className="space-y-2">
              {batches.map((batch) => (
                <div
                  key={batch.id}
                  className="flex flex-wrap items-center gap-2 rounded border px-3 py-2 text-sm"
                >
                  <span className="font-mono text-xs">#{batch.id}</span>
                  <Chip label="来源" value={batch.originName} />
                  <Chip label="状态" value={STATUS_LABEL[batch.status] ?? batch.status} />
                  <span className="text-muted-foreground">
                    {batch.submitted} 提交 / {batch.added} 新增 / 现存 {batch.liveCount}
                    {' · '}
                    {dateTime(batch.createdAt)}
                  </span>
                  {batch.summary && (
                    <span className="max-w-72 truncate text-xs text-muted-foreground">
                      {batch.summary}
                    </span>
                  )}
                  <span className="ml-auto">
                    <Button
                      size="sm"
                      variant="danger-outline"
                      disabled={busy || batch.liveCount === 0}
                      onClick={() => void askDelete(batch.id)}
                    >
                      <Trash2 className="h-4 w-4" /> 删除该批
                    </Button>
                  </span>
                </div>
              ))}
            </div>
          )}
        </CardBody>
      </Card>

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
    </div>
  )
}
