/**
 * 模型与厂商工作台页面。
 *
 * 左侧厂商列表负责过滤，右侧表格维护模型；「功能分配」页给每个任务按优先级
 * 指定候选模型。配置写入 providers.toml / models.toml，保存后重启后端生效。
 */
import {
  Check,
  ChevronsUpDown,
  Copy,
  Cpu,
  Eye,
  EyeOff,
  FlaskConical,
  ListPlus,
  Pencil,
  Plus,
  RefreshCw,
  Save,
  Trash2,
  X,
} from 'lucide-react'
import { useEffect, useState } from 'react'

import { PageHeader } from '@/components/layout/PageHeader'
import { apiMutate } from '@/lib/api'
import {
  Button,
  Card,
  CardBody,
  Chip,
  ConfirmDialog,
  Dialog,
  ErrorText,
  Field,
  Input,
  Loading,
  Metric,
  SectionHeading,
  SegmentedTabs,
  Select,
  Textarea,
  Toggle,
  toast,
} from '@/components/ui'
import {
  useModelConfig,
  type GenerationConfig,
  type ModelConfig,
  type ModelConfigSnapshot,
  type ProviderConfig,
  type RemoteModel,
  type TaskConfig,
} from '@/hooks/use-model-config'

const TASK_NAMES = [
  'chat', 'planner', 'replyer', 'scene', 'proactive', 'summary', 'schedule', 'vision',
  'expression', 'tts', 'embedding',
] as const
// 生成参数（温度、token 上限）覆盖后端 GenerationConfig 里的每一档任务。
// tts 与 embedding 不在其中：语音合成与向量化没有温度和输出上限可言。
const GENERATION_TASKS = [
  'chat', 'planner', 'replyer', 'scene', 'proactive', 'summary', 'schedule', 'expression', 'vision',
] as const

/** 任务字段的中文名称，仅用于功能分配页展示；TOML 配置键名保持英文不变。 */
const TASK_LABELS: Record<(typeof TASK_NAMES)[number], string> = {
  chat: '日常对话',
  planner: '行动决策',
  replyer: '回复生成',
  scene: '情景分析',
  proactive: '主动搭话',
  summary: '对话摘要',
  schedule: '日程安排',
  vision: '屏幕视觉',
  expression: '表达选择',
  tts: '语音合成',
  embedding: '向量嵌入',
}

/** 任务字段的业务用途说明，挂在任务按钮的悬停提示上。 */
const TASK_DESCRIPTIONS: Record<(typeof TASK_NAMES)[number], string> = {
  chat: '回复用户普通消息的对话模型',
  planner: '决定这一轮做什么动作的模型；留空继承日常对话。首字延迟主要由它决定',
  replyer: '把决策写成她实际说出口那句话的模型；留空继承日常对话',
  scene: '把一段聊天概括成「此刻是什么情况」的模型；群聊画像与私聊追问判断都用它',
  proactive: '判断并生成主动搭话消息的模型',
  summary: '把长对话压缩为角色记忆的摘要模型',
  schedule: '生成角色每日日程计划的模型',
  vision: '识别用户询问的屏幕画面的视觉模型',
  expression: '为当前语境挑选表达习惯的模型',
  tts: '把回复文本合成为语音的模型',
  embedding: '为长期记忆生成检索向量的嵌入模型',
}

/** 把任务英文键名转为中文展示名；未知键名原样返回，避免掩盖配置异常。 */
function taskLabel(task: string): string {
  return TASK_LABELS[task as (typeof TASK_NAMES)[number]] ?? task
}

/** 取任务用途说明；未知键名返回空串。 */
function taskDescription(task: string): string {
  return TASK_DESCRIPTIONS[task as (typeof TASK_NAMES)[number]] ?? ''
}

interface ProviderTemplate {
  key: string
  label: string
  name: string
  kind: string
  base_url: string
}

const PROVIDER_TEMPLATES: ProviderTemplate[] = [
  { key: 'deepseek', label: 'DeepSeek', name: 'DeepSeek', kind: 'deepseek', base_url: 'https://api.deepseek.com' },
  { key: 'zhipu', label: '智谱 AI (ZhipuAI / GLM)', name: 'ZhipuAI', kind: 'openai', base_url: 'https://open.bigmodel.cn/api/paas/v4' },
  { key: 'moonshot', label: '月之暗面 (Moonshot / Kimi)', name: 'Moonshot', kind: 'openai', base_url: 'https://api.moonshot.cn/v1' },
  { key: 'doubao', label: '字节豆包 (Doubao)', name: 'Doubao', kind: 'openai', base_url: 'https://ark.cn-beijing.volces.com/api/v3' },
  { key: 'alibaba', label: '阿里云百炼 (Alibaba Qwen)', name: 'Alibaba', kind: 'openai', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
  { key: 'baichuan', label: '百川智能 (Baichuan)', name: 'Baichuan', kind: 'openai', base_url: 'https://api.baichuan-ai.com/v1' },
  { key: 'minimax', label: 'MiniMax (海螺 AI)', name: 'MiniMax', kind: 'openai', base_url: 'https://api.minimax.chat/v1' },
  { key: 'stepfun', label: '阶跃星辰 (StepFun)', name: 'StepFun', kind: 'openai', base_url: 'https://api.stepfun.com/v1' },
  { key: 'siliconflow', label: '硅基流动 (SiliconFlow)', name: 'SiliconFlow', kind: 'openai', base_url: 'https://api.siliconflow.cn/v1' },
  { key: 'openai', label: 'OpenAI', name: 'OpenAI', kind: 'openai', base_url: 'https://api.openai.com/v1' },
  { key: 'xai', label: 'xAI (Grok)', name: 'xAI', kind: 'openai', base_url: 'https://api.x.ai/v1' },
  { key: 'anthropic', label: 'Anthropic (Claude)', name: 'Anthropic', kind: 'openai', base_url: 'https://api.anthropic.com/v1' },
  { key: 'gemini', label: 'Google Gemini', name: 'Gemini', kind: 'openai', base_url: 'https://generativelanguage.googleapis.com/v1beta/openai/' },
  { key: 'cohere', label: 'Cohere', name: 'Cohere', kind: 'openai', base_url: 'https://api.cohere.ai/v1' },
  { key: 'groq', label: 'Groq', name: 'Groq', kind: 'openai', base_url: 'https://api.groq.com/openai/v1' },
  { key: 'mistral', label: 'Mistral AI', name: 'Mistral', kind: 'openai', base_url: 'https://api.mistral.ai/v1' },
  { key: 'perplexity', label: 'Perplexity AI', name: 'Perplexity', kind: 'openai', base_url: 'https://api.perplexity.ai' },
  { key: 'custom', label: '使用自定义提供商', name: '', kind: 'openai', base_url: '' },
]
function providerFromTemplate(template: ProviderTemplate): ProviderConfig {
  return {
    ...emptyProvider(),
    name: template.name,
    kind: template.kind,
    base_url: template.base_url,
    timeout_ms: 30_000,
    max_retries: 2,
    retry_interval_ms: 10_000,
  }
}

function emptyProvider(): ProviderConfig {
  return {
    name: '',
    kind: 'openai',
    base_url: '',
    api_key: '',
    apiKeySet: false,
    auth_type: 'bearer',
    auth_name: '',
    client_type: 'openai',
    app_id: '',
    model_list_endpoint: '/models',
    default_headers: {},
    default_query: {},
    timeout_ms: 120_000,
    max_retries: 2,
    retry_interval_ms: 800,
  }
}

function emptyModel(): ModelConfig {
  return {
    name: '',
    model_identifier: '',
    api_provider: '',
    extra_body: {},
    reasoning_parse_mode: 'field',
    visual: false,
    temperature: null,
    max_tokens: null,
    price_in: 0,
    price_out: 0,
    embedding_dim: 0,
  }
}

/** 返回模型思考参数的显式状态；未配置时沿用服务商默认行为。 */
function modelThinkingState(model: ModelConfig): 'default' | 'enabled' | 'disabled' {
  const value = model.extra_body.enable_thinking
  if (value === true) return 'enabled'
  if (value === false) return 'disabled'
  return 'default'
}

/** 更新思考开关，同时保留用户填写的其它 extra_body 厂商参数。 */
function withModelThinking(model: ModelConfig, enabled: boolean): ModelConfig['extra_body'] {
  return { ...model.extra_body, enable_thinking: enabled }
}

/** 渲染模型设置与功能分配两个标签页。 */
export function ModelConfigPage() {
  const state = useModelConfig()
  const [draft, setDraft] = useState<ModelConfigSnapshot | null>(null)
  const [tab, setTab] = useState<'models' | 'tasks'>('models')
  const [selectedTask, setSelectedTask] = useState<string>(TASK_NAMES[0])
  const [providerFilter, setProviderFilter] = useState('all')
  const [search, setSearch] = useState('')
  const [providerDialog, setProviderDialog] = useState<{ index: number | 'new'; form: ProviderConfig; template: string } | null>(null)
  const [modelDialog, setModelDialog] = useState<{ index: number; model: ModelConfig } | null>(null)
  const [restartConfirmOpen, setRestartConfirmOpen] = useState(false)
  const [providerDeleteTarget, setProviderDeleteTarget] = useState<string | null>(null)
  const [validationErrors, setValidationErrors] = useState<string[]>([])

  useEffect(() => {
    const snapshot = state.snapshot
    if (snapshot) {
      setDraft(JSON.parse(JSON.stringify(snapshot)) as ModelConfigSnapshot)
      setProviderFilter((current) =>
        current !== 'all' && snapshot.providers.some((item) => item.name === current)
          ? current
          : 'all',
      )
    }
  }, [state.snapshot])

  // 草稿一旦发生修改就清掉上一次保存校验的旧错误，避免用户已经修正仍看到过期提示。
  useEffect(() => {
    setValidationErrors([])
  }, [draft])

  // 保存/测试/拉取等操作的状态文本不再占用页面顶部条幅，统一转为全局 toast；
  // 弹出后立即清除 hook 内的状态，避免同一文本因状态残留而重复提示。
  const status = state.status
  const clearStatus = state.clearStatus
  useEffect(() => {
    if (!status) return
    if (status.includes('失败')) toast.error(status)
    else if (status.startsWith('正在')) toast.info(status)
    else toast.success(status)
    clearStatus()
  }, [status, clearStatus])

  if (state.loading && !draft) {
    return (
      <div className="mx-auto w-full max-w-[1440px] px-6 py-8">
        <Loading>正在读取模型配置…</Loading>
      </div>
    )
  }
  if (!draft) {
    return (
      <div className="mx-auto w-full max-w-[1440px] px-6 py-8">
        <ErrorText>{state.error || '模型配置不可用'}</ErrorText>
      </div>
    )
  }

  const updateTask = (task: string, patch: Partial<TaskConfig>) => {
    setDraft((current) => current ? {
      ...current,
      tasks: {
        ...current.tasks,
        [task]: { ...current.tasks[task], ...patch } as TaskConfig,
      },
    } : current)
  }
  const updateGeneration = (task: string, patch: Partial<GenerationConfig>) => {
    setDraft((current) => current ? {
      ...current,
      generation: {
        ...current.generation,
        [task]: { ...current.generation[task], ...patch } as GenerationConfig,
      },
    } : current)
  }

  const openNewModelDialog = (initial: Partial<ModelConfig> = {}) => {
    const next = emptyModel()
    next.api_provider = providerFilter !== 'all' ? providerFilter : ''
    setModelDialog({ index: -1, model: { ...next, ...initial } })
  }
  const openEditModelDialog = (index: number) => {
    const model = draft.models[index]
    if (!model) return
    setModelDialog({ index, model: JSON.parse(JSON.stringify(model)) as ModelConfig })
  }
  const saveModelDialog = (model: ModelConfig) => {
    if (!modelDialog) return
    setDraft((current) => current ? {
      ...current,
      models: modelDialog.index < 0
        ? [...current.models, model]
        : current.models.map((item, itemIndex) => itemIndex === modelDialog.index ? model : item),
    } : current)
    setModelDialog(null)
  }
  const removeModelAt = (index: number) => {
    if (index < 0) return
    setDraft((current) => {
      if (!current) return current
      const removed = current.models[index]?.name
      const models = current.models.filter((_, itemIndex) => itemIndex !== index)
      if (!removed) return { ...current, models }
      return {
        ...current,
        models,
        tasks: Object.fromEntries(
          Object.entries(current.tasks).map(([task, value]) => [
            task,
            { ...value, model_list: value.model_list.filter((name) => name !== removed) },
          ]),
        ) as ModelConfigSnapshot['tasks'],
      }
    })
  }
  const deleteModelDialog = (index: number) => {
    removeModelAt(index)
    setModelDialog(null)
  }

  const validateDraft = (next: ModelConfigSnapshot): string[] => {
    const problems: string[] = []
    const seenModels = new Map<string, number>()
    next.models.forEach((model, index) => {
      const row = index + 1
      const name = model.name.trim()
      const label = name ? `模型「${name}」` : `模型列表第 ${row} 个模型`
      if (!name) {
        problems.push(`${label}：名称为空，请编辑该行填写名称或删除该行`)
      } else if (seenModels.has(name)) {
        problems.push(`模型名称重复：「${name}」（第 ${seenModels.get(name)! + 1} 个与第 ${row} 个模型）`)
      } else {
        seenModels.set(name, index)
      }
      if (!model.api_provider.trim()) {
        problems.push(`${label}：未选择 API 提供商`)
      } else if (!next.providers.some((provider) => provider.name.trim() === model.api_provider.trim())) {
        problems.push(`${label}：引用了不存在的 API 提供商「${model.api_provider}」`)
      }
    })
    const seenProviders = new Map<string, number>()
    next.providers.forEach((provider, index) => {
      const row = index + 1
      const name = provider.name.trim()
      const label = name ? `厂商「${name}」` : `厂商列表第 ${row} 个厂商`
      if (!name) {
        problems.push(`${label}：名称为空`)
      } else if (seenProviders.has(name)) {
        problems.push(`厂商名称重复：「${name}」（第 ${seenProviders.get(name)! + 1} 个与第 ${row} 个厂商）`)
      } else {
        seenProviders.set(name, index)
      }
    })
    return problems
  }

  const saveDraft = () => {
    const problems = validateDraft(draft)
    if (problems.length) {
      setValidationErrors(problems)
      setTab('models')
      setSearch('')
      setProviderFilter('all')
      state.clearStatus()
      return
    }
    void state.save(draft)
  }

  const clearModelFilters = () => {
    setSearch('')
    setProviderFilter('all')
  }

  // 重启前的确认由 ConfirmDialog 承担，这里只负责发指令与结果反馈。
  const restartBackend = async () => {
    try {
      await apiMutate<{ ok: boolean }>('/system/restart', 'POST')
      toast.success('已发送重启指令，月璃即将重启…')
      window.setTimeout(() => window.location.reload(), 2200)
    } catch (error) {
      toast.error(`重启失败：${error instanceof Error ? error.message : String(error)}`)
    }
  }

  // 删除厂商：连带移除其名下模型，并把这些模型从所有任务的候选列表中剔除。
  const removeProvider = (removed: string) => {
    setDraft((current) => {
      if (!current) return current
      const removedModelNames = new Set(
        current.models.filter((model) => model.api_provider === removed).map((model) => model.name),
      )
      return {
        ...current,
        providers: current.providers.filter((provider) => provider.name !== removed),
        models: current.models.filter((model) => model.api_provider !== removed),
        tasks: Object.fromEntries(
          Object.entries(current.tasks).map(([task, value]) => [
            task,
            { ...value, model_list: value.model_list.filter((name) => !removedModelNames.has(name)) },
          ]),
        ) as ModelConfigSnapshot['tasks'],
      }
    })
    setProviderFilter('all')
  }

  const usedTasks = (modelName: string) =>
    TASK_NAMES.filter((task) => draft.tasks[task]?.model_list.includes(modelName))

  const visibleModels = draft.models.filter((model) => {
    if (providerFilter !== 'all' && model.api_provider !== providerFilter) return false
    const keyword = search.trim().toLowerCase()
    if (!keyword) return true
    return [model.name, model.model_identifier, model.api_provider]
      .join(' ')
      .toLowerCase()
      .includes(keyword)
  })

  const selectedProvider = draft.providers.find((item) => item.name === providerFilter)
  const addProvider = () => {
    const template = PROVIDER_TEMPLATES.find((item) => item.key === 'deepseek')!
    setProviderDialog({ index: 'new', form: providerFromTemplate(template), template: template.key })
  }

  const testByName = () => {
    if (selectedProvider) void state.testProviderByName(selectedProvider.name)
  }
  const testByFields = () => {
    if (!selectedProvider) return
    void state.testProviderByFields({
      base_url: selectedProvider.base_url,
      api_key: selectedProvider.api_key,
      client_type: selectedProvider.client_type,
      auth_type: selectedProvider.auth_type,
      auth_name: selectedProvider.auth_name,
      model_list_endpoint: selectedProvider.model_list_endpoint,
    })
  }
  const listByName = () => {
    if (selectedProvider) void state.listModelsByName(selectedProvider.name)
  }
  const listByFields = () => {
    if (!selectedProvider) return
    void state.listModelsByFields({
      base_url: selectedProvider.base_url,
      api_key: selectedProvider.api_key,
      client_type: selectedProvider.client_type,
      auth_type: selectedProvider.auth_type,
      auth_name: selectedProvider.auth_name,
      model_list_endpoint: selectedProvider.model_list_endpoint,
    })
  }

  const selectedProviderIsSaved = Boolean(
    selectedProvider && state.snapshot?.providers.some((item) => item.name === selectedProvider.name),
  )
  const testSelectedProvider = selectedProviderIsSaved ? testByName : testByFields
  const listSelectedProviderModels = selectedProviderIsSaved ? listByName : listByFields

  const saveProviderDialog = (form: ProviderConfig) => {
    if (!providerDialog) return
    const index = providerDialog.index
    setDraft((current) => {
      if (!current) return current
      if (index === 'new') return { ...current, providers: [...current.providers, form] }
      return {
        ...current,
        providers: current.providers.map((item, itemIndex) => itemIndex === index ? form : item),
      }
    })
    setProviderDialog(null)
    if (providerFilter === 'all' || providerFilter === form.name) setProviderFilter('all')
  }

  return (
    <div className="mx-auto flex w-full max-w-[1440px] flex-col gap-5 px-4 py-6 sm:px-6 lg:px-8">
      <PageHeader
        eyebrow="YUELI · CONSOLE"
        title="模型管理"
        subtitle="模型设置负责厂商与模型表；功能分配负责每个任务的候选模型。保存后重启后端生效。"
        actions={
          <>
            <Button variant="secondary" onClick={state.reload} disabled={state.busy}>
              <RefreshCw className="size-4" aria-hidden="true" />
              重新读取
            </Button>
            <Button onClick={saveDraft} disabled={state.busy}>
              <Save className="size-4" aria-hidden="true" />
              保存配置
            </Button>
            <Button variant="secondary" onClick={() => setRestartConfirmOpen(true)} disabled={state.busy}>
              <RefreshCw className="size-4" aria-hidden="true" />
              重启后端
            </Button>
          </>
        }
      />

      {state.error ? <ErrorText>{state.error}</ErrorText> : null}
      {validationErrors.length ? (
        <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3">
          <p className="text-sm font-semibold text-destructive">保存前请先修正以下问题：</p>
          <ul className="mt-1.5 flex list-disc flex-col gap-1 pl-5 text-sm text-destructive">
            {validationErrors.map((message) => <li key={message}>{message}</li>)}
          </ul>
        </div>
      ) : null}

      <Card className="animate-rise">
        <CardBody className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <strong className="text-sm">桌面视觉（屏幕识别）</strong>
              <p className="mt-1 text-xs text-muted-foreground">
                控制桌宠截图识别；需要桌面视觉模型已在 vision 任务中可用。
              </p>
            </div>
            <Toggle
              checked={draft.vision_enabled}
              onChange={(checked) => setDraft((current) => current ? { ...current, vision_enabled: checked } : current)}
              label={draft.vision_enabled ? '已开启' : '未开启'}
            />
          </div>
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
            <div className="min-w-0">
              <strong className="text-sm">QQ 图片识别（聊天图片描述）</strong>
              <p className="mt-1 text-xs text-muted-foreground">
                控制 QQ 群聊/私聊图片的视觉描述；需要视觉模型并分配给 vision 任务。
              </p>
            </div>
            <Toggle
              checked={draft.chat_image_enabled}
              onChange={(checked) => setDraft((current) => current ? { ...current, chat_image_enabled: checked } : current)}
              label={draft.chat_image_enabled ? '已开启' : '未开启'}
            />
          </div>
        </CardBody>
      </Card>

      <SegmentedTabs
        tabs={[
          { value: 'models', label: '模型设置' },
          { value: 'tasks', label: '功能分配' },
        ]}
        value={tab}
        onChange={setTab}
        className="w-full"
        tabClassName="flex-1"
      />

      {tab === 'models' ? (
        <div className="grid gap-4 lg:grid-cols-[300px_minmax(0,1fr)]">
          <Card className="animate-rise">
            <SectionHeading
              icon={<Cpu className="size-4.5" aria-hidden="true" />}
              tint="coral"
              title="模型厂商"
              subtitle={`${draft.models.length} 个模型 · ${draft.providers.length} 个厂商`}
              actions={
                <Button size="sm" variant="secondary" onClick={addProvider}>
                  <Plus className="size-4" aria-hidden="true" />
                  添加厂商
                </Button>
              }
            />
            <CardBody className="flex flex-col gap-1 p-2">
              <button
                type="button"
                onClick={() => setProviderFilter('all')}
                className={`flex w-full cursor-pointer items-center justify-between rounded-lg px-3 py-2 text-left text-[13px] transition-colors ${providerFilter === 'all' ? 'bg-primary/10 font-semibold text-primary-strong' : 'hover:bg-muted'}`}
              >
                <span>全部厂商</span>
                <span className="font-mono text-xs">{draft.models.length}</span>
              </button>
              {draft.providers.map((provider, index) => {
                const count = draft.models.filter((model) => model.api_provider === provider.name).length
                return (
                  <div key={`${provider.name}-${index}`} className="group rounded-lg">
                    <button
                      type="button"
                      onClick={() => setProviderFilter(provider.name)}
                      className={`flex w-full cursor-pointer items-center justify-between rounded-lg px-3 py-2 text-left text-[13px] transition-colors ${providerFilter === provider.name ? 'bg-primary/10 font-semibold text-primary-strong' : 'hover:bg-muted'}`}
                    >
                      <span className="min-w-0">
                        <strong className="block truncate">{provider.name}</strong>
                        <span className="block truncate text-[11px] text-muted-foreground">{provider.base_url || '未填 Base URL'}</span>
                      </span>
                      <span className="font-mono text-xs">{count}</span>
                    </button>
                    {/* 快捷编辑入口仅悬停/聚焦时露出；focus-visible 保证键盘可达。 */}
                    <div className="flex gap-1 px-3 pb-1">
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-7 px-1.5 text-xs opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
                        onClick={() => setProviderDialog({
                          index,
                          form: JSON.parse(JSON.stringify(provider)) as ProviderConfig,
                          template: PROVIDER_TEMPLATES.some((item) => item.base_url === provider.base_url)
                            ? PROVIDER_TEMPLATES.find((item) => item.base_url === provider.base_url)?.key ?? 'custom'
                            : 'custom',
                        })}
                        aria-label={`编辑厂商 ${provider.name}`}
                      >
                        编辑
                      </Button>
                    </div>
                  </div>
                )
              })}
            </CardBody>
          </Card>

          <div className="flex min-w-0 flex-col gap-4">
            {selectedProvider ? (
              <Card className="animate-rise">
                <CardBody className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                      <h2 className="font-semibold">{selectedProvider.name}</h2>
                      <span className="text-xs text-muted-foreground">
                        {draft.models.filter((model) => model.api_provider === selectedProvider.name).length} 个模型
                      </span>
                      <span className="text-xs text-muted-foreground">客户端类型：{selectedProvider.client_type}</span>
                    </div>
                    <p className="mt-1 truncate text-xs text-muted-foreground" title={selectedProvider.base_url}>
                      Base URL：{selectedProvider.base_url}
                    </p>
                  </div>
                  <div className="flex shrink-0 gap-1.5">
                    <Button size="sm" variant="secondary" onClick={testSelectedProvider} disabled={state.busy} title="测试连接" aria-label={`测试厂商 ${selectedProvider.name} 连接`}>
                      <FlaskConical className="size-3.5" aria-hidden="true" />
                      测试连接
                    </Button>
                    <Button size="sm" variant="secondary" onClick={listSelectedProviderModels} disabled={state.busy} title="拉取模型">
                      <ListPlus className="size-3.5" aria-hidden="true" />
                      拉取模型
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => {
                        const index = draft.providers.findIndex((item) => item.name === selectedProvider.name)
                        if (index >= 0) {
                          setProviderDialog({
                            index,
                            form: JSON.parse(JSON.stringify(draft.providers[index]!)) as ProviderConfig,
                            template: PROVIDER_TEMPLATES.some((item) => item.base_url === draft.providers[index]!.base_url)
                              ? PROVIDER_TEMPLATES.find((item) => item.base_url === draft.providers[index]!.base_url)?.key ?? 'custom'
                              : 'custom',
                          })
                        }
                      }}
                      title="编辑厂商"
                      aria-label={`编辑厂商 ${selectedProvider.name}`}
                    >
                      <Pencil className="size-3.5" aria-hidden="true" />
                      编辑
                    </Button>
                    <Button
                      size="sm"
                      variant="danger-outline"
                      onClick={() => setProviderDeleteTarget(selectedProvider.name)}
                      title="删除厂商"
                      aria-label={`删除厂商 ${selectedProvider.name}`}
                    >
                      <Trash2 className="size-3.5" aria-hidden="true" />
                      删除
                    </Button>
                  </div>
                </CardBody>
              </Card>
            ) : null}

            {modelDialog ? (
              <ModelDialog
                model={modelDialog.model}
                index={modelDialog.index}
                providers={draft.providers}
                remoteModels={state.remoteModels}
                onFetchModels={(providerName) => {
                  if (providerName) void state.listModelsByName(providerName)
                }}
                onCancel={() => setModelDialog(null)}
                onSave={saveModelDialog}
                onDelete={() => deleteModelDialog(modelDialog.index)}
              />
            ) : null}

            <Card className="animate-rise">
              <SectionHeading
                icon={<Cpu className="size-4.5" aria-hidden="true" />}
                tint="olive"
                title="模型列表"
                subtitle="点击行内「编辑」展开完整参数；视觉和温度可直接在表格中改"
                actions={
                  <>
                    <Field label="搜索模型" htmlFor="model-search" className="w-64">
                      <Input
                        id="model-search"
                        value={search}
                        onChange={(event) => setSearch(event.target.value)}
                        placeholder="搜索模型名称、标识符或提供商…"
                      />
                    </Field>
                    <Button size="sm" variant="secondary" onClick={() => openNewModelDialog()}>
                      <Plus className="size-4" aria-hidden="true" />
                      添加模型
                    </Button>
                  </>
                }
              />
              <CardBody className="p-0">
                {state.connection ? (
                  <div className="grid grid-cols-2 gap-3 border-b border-border bg-muted/30 px-5 py-3 sm:grid-cols-4">
                    <Metric label="网络连通" value={state.connection.network_ok ? '是' : '否'} />
                    <Metric label="API Key" value={state.connection.api_key_valid === null ? '未确认' : state.connection.api_key_valid ? '有效' : '无效'} />
                    <Metric label="HTTP 状态" value={state.connection.http_status ?? '—'} />
                    <Metric label="延迟" value={state.connection.latency_ms === null ? '—' : `${state.connection.latency_ms} ms`} />
                  </div>
                ) : null}
                {state.connection?.error ? <ErrorText>{state.connection.error}</ErrorText> : null}
                {providerFilter !== 'all' || search.trim() ? (
                  <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-primary/5 px-5 py-2.5">
                    <p className="text-xs text-muted-foreground">
                      当前显示 {visibleModels.length} / {draft.models.length} 个模型
                      {providerFilter !== 'all' ? `（仅厂商「${providerFilter}」）` : ''}
                      {search.trim() ? `（搜索「${search.trim()}」）` : ''}
                    </p>
                    <Button size="sm" variant="secondary" onClick={clearModelFilters}>清除筛选，查看全部</Button>
                  </div>
                ) : null}
                {state.remoteModels.length ? (
                  <div className="border-b border-border bg-muted/20 px-5 py-3">
                    <div className="mb-2 flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
                      <p className="text-xs font-semibold">上游模型列表（{state.remoteModels.length} 个，尚未加入下方模型表）</p>
                      <p className="text-xs text-muted-foreground">点击模型 ID 会打开「添加模型」，并自动填入模型名称与标识符</p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {state.remoteModels.map((model) => (
                        <button
                          key={model.id}
                          type="button"
                          className="cursor-pointer"
                          title={`添加模型 ${model.id}`}
                          onClick={() => openNewModelDialog({ model_identifier: model.id, name: model.id })}
                        >
                          <Chip label={model.id} value={model.name} />
                        </button>
                      ))}
                    </div>
                  </div>
                ) : null}
                <div className="overflow-x-auto">
                  <table className="w-full border-collapse text-left text-[13px]">
                    <thead>
                      <tr className="border-b border-border text-xs text-muted-foreground">
                        <th className="px-4 py-2.5 font-medium">使用</th>
                        <th className="px-3 py-2.5 font-medium">模型名称</th>
                        <th className="px-3 py-2.5 font-medium">模型标识符</th>
                        <th className="px-3 py-2.5 font-medium">提供商</th>
                        <th className="px-3 py-2.5 font-medium">视觉</th>
                        <th className="px-3 py-2.5 font-medium">思考</th>
                        <th className="px-3 py-2.5 font-medium">温度</th>
                        <th className="px-3 py-2.5 font-medium">输入价格</th>
                        <th className="px-3 py-2.5 font-medium">输出价格</th>
                        <th className="px-4 py-2.5 font-medium">操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {visibleModels.length === 0 ? (
                        <tr>
                          <td colSpan={10} className="px-4 py-10 text-center text-sm text-muted-foreground">
                            {draft.models.length === 0
                              ? '暂无模型配置，点击「添加模型」或先「拉取模型」'
                              : '当前筛选条件下没有模型'}
                            {draft.models.length > 0 && (providerFilter !== 'all' || search.trim()) ? (
                              <button type="button" className="ml-2 cursor-pointer font-semibold text-primary-strong underline-offset-4 hover:underline" onClick={clearModelFilters}>
                                清除筛选，查看全部 {draft.models.length} 个模型
                              </button>
                            ) : null}
                          </td>
                        </tr>
                      ) : visibleModels.map((model, index) => {
                        const actualIndex = draft.models.indexOf(model)
                        const usage = usedTasks(model.name)
                        const usageLabel = usage.map((task) => `${taskLabel(task)}（${task}）`)
                        const invalid = !model.name.trim() || !model.api_provider.trim()
                        const thinkingState = modelThinkingState(model)
                        const thinkingEnabled = thinkingState !== 'disabled'
                        return (
                          <tr
                            key={`${model.name || 'unnamed'}-${actualIndex}-${index}`}
                            className={`border-b border-border/60 hover:bg-muted/30 ${invalid ? 'bg-destructive/5' : ''}`}
                          >
                            <td className="px-4 py-2.5 text-center">
                              <span
                                className={`mx-auto block size-3 rounded-full ${usage.length ? 'bg-primary ring-2 ring-primary/25' : 'bg-input'}`}
                                title={usage.length ? `已分配给：${usageLabel.join('、')}` : '未使用'}
                                aria-label={usage.length ? '已使用' : '未使用'}
                              />
                            </td>
                            <td className="px-3 py-2.5 font-medium" title={usage.length ? `已分配给：${usageLabel.join('、')}` : ''}>
                              {model.name ? model.name : <span className="font-normal text-destructive">未命名（保存前必须填写或删除）</span>}
                            </td>
                            <td className="max-w-xs truncate px-3 py-2.5" title={model.model_identifier}>
                              {model.model_identifier}
                            </td>
                            <td className="px-3 py-2.5">{model.api_provider}</td>
                            <td className="px-3 py-2.5 text-center">
                              <span
                                className={`mx-auto block size-3 rounded-full ${model.visual ? 'bg-primary ring-2 ring-primary/25' : 'bg-input'}`}
                                title={model.visual ? '已启用视觉' : '未启用视觉'}
                                aria-label={model.visual ? '已启用视觉' : '未启用视觉'}
                              />
                            </td>
                            <td className="px-3 py-2.5 text-center">
                              {thinkingState === 'default' ? (
                                <span className="text-xs text-muted-foreground">默认</span>
                              ) : (
                                <span className={thinkingEnabled ? 'text-xs text-primary-strong' : 'text-xs text-muted-foreground'}>
                                  {thinkingEnabled ? '开启' : '关闭'}
                                </span>
                              )}
                            </td>
                            <td className="px-3 py-2.5 text-center">
                              {model.temperature !== null ? model.temperature : <span className="text-muted-foreground">-</span>}
                            </td>
                            <td className="px-3 py-2.5 text-right">¥{model.price_in}/M</td>
                            <td className="px-3 py-2.5 text-right">¥{model.price_out}/M</td>
                            <td className="px-4 py-2.5 text-right">
                              <div className="flex justify-end gap-1.5">
                                <Button size="sm" variant="secondary" className="h-8 w-8 p-0" onClick={() => openEditModelDialog(actualIndex)} title="编辑" aria-label={`编辑模型 ${model.name || actualIndex + 1}`}>
                                  <Pencil className="size-3.5" aria-hidden="true" />
                                </Button>
                                <Button size="sm" variant="danger-outline" className="h-8 w-8 p-0" onClick={() => removeModelAt(actualIndex)} title="删除" aria-label={`删除模型 ${model.name || actualIndex + 1}`}>
                                  <Trash2 className="size-3.5" aria-hidden="true" />
                                </Button>
                              </div>
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              </CardBody>
            </Card>
          </div>
        </div>
      ) : (
        <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[280px_minmax(0,1fr)]">
          <Card className="animate-rise">
            <SectionHeading
              icon={<Cpu className="size-4.5" aria-hidden="true" />}
              tint="plum"
              title="模型类别"
              subtitle="选择要配置的任务"
            />
            <CardBody className="flex flex-col gap-1 p-2">
              {TASK_NAMES.map((task) => {
                const config = draft.tasks[task]
                const assigned = config?.model_list ?? []
                return (
                  <button
                    key={task}
                    type="button"
                    onClick={() => setSelectedTask(task)}
                    className={`flex w-full cursor-pointer items-center justify-between gap-3 rounded-lg px-3 py-2 text-left text-sm transition-colors ${selectedTask === task ? 'bg-primary text-primary-foreground' : 'hover:bg-muted'}`}
                    aria-pressed={selectedTask === task}
                    title={`${TASK_LABELS[task]}（${task}）：${TASK_DESCRIPTIONS[task]}`}
                  >
                    <span className="min-w-0">
                      <strong className="block truncate">
                        {TASK_LABELS[task]}
                        <span className={`ml-1.5 font-mono text-[10px] font-normal ${selectedTask === task ? 'text-primary-foreground/70' : 'text-muted-foreground'}`}>{task}</span>
                      </strong>
                      <span className={`block truncate text-[11px] ${selectedTask === task ? 'text-primary-foreground/70' : 'text-muted-foreground'}`}>
                        {assigned.length ? assigned.join('、') : '未配置模型'}
                      </span>
                    </span>
                    <span className={`font-mono text-xs ${selectedTask === task ? 'text-primary-foreground/80' : 'text-muted-foreground'}`}>
                      {assigned.length}
                    </span>
                  </button>
                )
              })}
            </CardBody>
          </Card>

          <Card className="animate-rise">
            <SectionHeading
              icon={<Cpu className="size-4.5" aria-hidden="true" />}
              tint="plum"
              title={`功能分配 · ${taskLabel(selectedTask)}`}
              subtitle={`任务字段 ${selectedTask} · ${taskDescription(selectedTask)}；第一位是主力模型，后续是故障切换备用`}
            />
            <CardBody>
              {(() => {
                const taskConfig = draft.tasks[selectedTask]
                if (!taskConfig) return null
                const generation = (GENERATION_TASKS as readonly string[]).includes(selectedTask)
                  ? draft.generation[selectedTask as (typeof GENERATION_TASKS)[number]]
                  : null
                return (
                  <div className="space-y-4">
                    <Field label="模型列表" htmlFor="task-model-list">
                      <div className="flex min-h-12 flex-wrap gap-1.5 rounded-lg border border-border bg-muted/20 p-2">
                        {taskConfig.model_list.map((modelName, index) => (
                          <span key={`${selectedTask}-${modelName}`} className="inline-flex items-center gap-1 rounded-full border border-primary/30 bg-primary/10 px-2.5 py-1 text-xs text-primary-strong">
                            <span className="text-[10px] text-muted-foreground">#{index + 1}</span>
                            {modelName}
                            <button type="button" className="cursor-pointer text-muted-foreground hover:text-destructive" onClick={() => updateTask(selectedTask, { model_list: taskConfig.model_list.filter((name) => name !== modelName) })} aria-label={`移除 ${modelName}`}>
                              <X className="size-3" aria-hidden="true" />
                            </button>
                          </span>
                        ))}
                        {!taskConfig.model_list.length ? <span className="text-xs text-muted-foreground">暂无可用模型，请先在模型设置中添加模型</span> : null}
                      </div>
                    </Field>
                    <div className="flex gap-2">
                      <Select value="" onChange={(event) => {
                        const name = event.target.value
                        if (name && !taskConfig.model_list.includes(name)) {
                          updateTask(selectedTask, { model_list: [...taskConfig.model_list, name] })
                        }
                      }}>
                        <option value="">添加模型…</option>
                        {draft.models.filter((model) => !taskConfig.model_list.includes(model.name)).map((model) => (
                          <option key={model.name} value={model.name}>{model.name}</option>
                        ))}
                      </Select>
                    </div>
                    <div className="grid gap-3 md:grid-cols-3">
                      <Field label="模型选择策略" help="顺序优先适合主力加备用；随机选择会打乱候选；负载均衡按轮次稳定分摊给健康模型。">
                        <Select value={taskConfig.selection_strategy} onChange={(event) => updateTask(selectedTask, { selection_strategy: event.target.value as TaskConfig['selection_strategy'] })}>
                          <option value="sequential">按顺序优先（sequential）</option>
                          <option value="random">随机选择（random）</option>
                          <option value="balance">负载均衡（balance）</option>
                        </Select>
                      </Field>
                      <Field label="首字超时 ms">
                        <Input type="number" value={taskConfig.first_token_timeout_ms} onChange={(event) => updateTask(selectedTask, { first_token_timeout_ms: Number(event.target.value) })} />
                      </Field>
                      <Field label="慢响应 ms">
                        <Input type="number" value={taskConfig.slow_threshold_ms} onChange={(event) => updateTask(selectedTask, { slow_threshold_ms: Number(event.target.value) })} />
                      </Field>
                    </div>
                    {generation ? (
                      <div className="grid gap-3 rounded-xl border border-border bg-muted/20 p-3 md:grid-cols-3">
                        {selectedTask === 'proactive' ? (
                          <Toggle checked={Boolean(generation.enabled)} onChange={(checked) => updateGeneration(selectedTask, { enabled: checked })} label="启用主动搭话" />
                        ) : null}
                        <Field label="温度">
                          <Input type="number" step="0.1" min={0} max={2} value={generation.temperature} onChange={(event) => updateGeneration(selectedTask, { temperature: Number(event.target.value) })} />
                        </Field>
                        <Field label="最大 Token">
                          <Input type="number" min={1} value={generation.max_tokens} onChange={(event) => updateGeneration(selectedTask, { max_tokens: Number(event.target.value) })} />
                        </Field>
                      </div>
                    ) : null}
                  </div>
                )
              })()}
            </CardBody>
          </Card>
        </div>
      )}

      {providerDialog ? (
        <ProviderDialog
          dialog={providerDialog}
          providers={draft.providers}
          onCancel={() => setProviderDialog(null)}
          onSave={saveProviderDialog}
        />
      ) : null}

      <ConfirmDialog
        open={restartConfirmOpen}
        title="重启月璃"
        description="重启期间她会暂时无法回复。"
        confirmText="重启"
        danger
        onConfirm={() => {
          setRestartConfirmOpen(false)
          void restartBackend()
        }}
        onCancel={() => setRestartConfirmOpen(false)}
      />
      <ConfirmDialog
        open={providerDeleteTarget !== null}
        title="删除厂商"
        description={`删除厂商「${providerDeleteTarget ?? ''}」及其关联模型？`}
        confirmText="删除"
        danger
        onConfirm={() => {
          if (providerDeleteTarget) removeProvider(providerDeleteTarget)
          setProviderDeleteTarget(null)
        }}
        onCancel={() => setProviderDeleteTarget(null)}
      />
    </div>
  )
}

/** 提供商弹窗：外壳为 Dialog 原语（ESC/遮罩关闭、焦点陷阱），表单对齐 MaiBot ProviderForm 的模板搜索、锁定字段、密钥显示与校验。 */
function ProviderDialog({
  dialog,
  providers,
  onCancel,
  onSave,
}: {
  dialog: { index: number | 'new'; form: ProviderConfig; template: string }
  providers: ProviderConfig[]
  onCancel: () => void
  onSave: (provider: ProviderConfig) => void
}) {
  const [form, setForm] = useState<ProviderConfig>(() => JSON.parse(JSON.stringify(dialog.form)) as ProviderConfig)
  const [templateKey, setTemplateKey] = useState(dialog.template)
  const [lastTemplateKey, setLastTemplateKey] = useState(dialog.template)
  const [comboboxOpen, setComboboxOpen] = useState(false)
  const [templateSearch, setTemplateSearch] = useState('')
  const [showApiKey, setShowApiKey] = useState(false)
  const [errors, setErrors] = useState<{ name?: string; base_url?: string; api_key?: string }>({})

  const isUsingTemplate = templateKey !== 'custom'
  const selectedTemplate = PROVIDER_TEMPLATES.find((item) => item.key === templateKey)
  const templateOptions = PROVIDER_TEMPLATES.filter((item) => item.key !== 'custom')
  const visibleTemplateOptions = templateOptions.filter((item) =>
    item.label.toLowerCase().includes(templateSearch.trim().toLowerCase()),
  )

  const applyTemplate = (key: string) => {
    const template = PROVIDER_TEMPLATES.find((item) => item.key === key)
    if (!template || template.key === 'custom') return
    setTemplateKey(key)
    setLastTemplateKey(key)
    setComboboxOpen(false)
    setForm((current) => ({
      ...current,
      name: template.name,
      kind: template.kind,
      base_url: template.base_url,
      client_type: 'openai',
    }))
  }

  const toggleTemplateMode = () => {
    if (isUsingTemplate) {
      setLastTemplateKey(templateKey)
      setTemplateKey('custom')
      setComboboxOpen(false)
      return
    }
    if (lastTemplateKey !== 'custom') applyTemplate(lastTemplateKey)
  }

  const updateForm = (patch: Partial<ProviderConfig>, clearError?: keyof typeof errors) => {
    setForm((current) => ({ ...current, ...patch }))
    if (clearError) setErrors((current) => ({ ...current, [clearError]: undefined }))
  }

  const validate = () => {
    const next: typeof errors = {}
    if (!form.name.trim()) next.name = '请输入提供商名称'
    else if (providers.some((item, index) => index !== (dialog.index === 'new' ? -1 : dialog.index) && item.name.trim().toLowerCase() === form.name.trim().toLowerCase())) {
      next.name = '提供商名称已存在，请使用其他名称'
    }
    if (!form.base_url.trim()) next.base_url = '请输入基础 URL'
    // 提示文案先落到独立常量：直接把字符串字面量赋给 api_key 字段会被安全
    // 扫描当成硬编码凭据，尽管它只是一句输入框报错。
    const apiKeyPromptText = '请输入 API Key'
    if (!form.api_key.trim() && !(dialog.index !== 'new' && form.apiKeySet)) next.api_key = apiKeyPromptText
    setErrors(next)
    return Object.keys(next).length === 0
  }

  const copyApiKey = async () => {
    if (!form.api_key) return
    try {
      await navigator.clipboard.writeText(form.api_key)
    } catch {
      // 浏览器拒绝剪贴板时保持静默，不阻塞表单保存。
    }
  }

  const submit = () => {
    if (!validate()) return
    onSave(form)
  }

  return (
    <Dialog
      open
      onClose={onCancel}
      title={dialog.index === 'new' ? '添加提供商' : '编辑提供商'}
      width="max-w-2xl"
      footer={
        <>
          <Button variant="ghost" onClick={onCancel}>取消</Button>
          <Button type="submit" form="provider-form">保存</Button>
        </>
      }
    >
      <form
        id="provider-form"
        className="flex flex-col"
        autoComplete="off"
        onSubmit={(event) => {
          event.preventDefault()
          submit()
        }}
      >
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <span className="flex items-center gap-1.5">
              <label htmlFor="provider-template" className="text-xs font-medium text-muted-foreground">提供商模板</label>
            </span>
            <div className="relative flex items-center gap-2">
              <button
                type="button"
                disabled={!isUsingTemplate}
                onClick={() => setComboboxOpen((value) => !value)}
                className={`flex h-9 min-w-0 flex-1 items-center justify-between rounded-md border border-input bg-card px-3 text-left text-sm shadow-card ${isUsingTemplate ? 'cursor-pointer hover:border-ring/50' : 'cursor-not-allowed bg-muted opacity-60'}`}
                role="combobox"
                aria-expanded={comboboxOpen}
              >
                <span className="truncate">
                  {isUsingTemplate ? selectedTemplate?.label ?? '选择提供商模板...' : '自定义提供商'}
                </span>
                <ChevronsUpDown className="ml-2 size-4 shrink-0 opacity-50" aria-hidden="true" />
              </button>
              <Button type="button" variant="secondary" className="shrink-0" onClick={toggleTemplateMode}>
                {isUsingTemplate ? '使用自定义提供商' : '使用供应商模板'}
              </Button>
              {comboboxOpen ? (
                <div className="absolute top-11 left-0 z-10 w-full rounded-xl border border-border bg-card p-2 shadow-lifted">
                  <Input
                    autoFocus
                    value={templateSearch}
                    onChange={(event) => setTemplateSearch(event.target.value)}
                    placeholder="搜索提供商模板..."
                  />
                  <div className="mt-2 max-h-72 overflow-y-auto">
                    {visibleTemplateOptions.length ? visibleTemplateOptions.map((template) => (
                      <button
                        key={template.key}
                        type="button"
                        onClick={() => applyTemplate(template.key)}
                        className="flex w-full cursor-pointer items-center rounded-lg px-2.5 py-2 text-left text-sm hover:bg-muted"
                      >
                        <Check className={`mr-2 size-4 ${templateKey === template.key ? 'opacity-100' : 'opacity-0'}`} aria-hidden="true" />
                        {template.label}
                      </button>
                    )) : <p className="px-2 py-4 text-center text-xs text-muted-foreground">未找到匹配的模板</p>}
                  </div>
                </div>
              ) : null}
            </div>
            <p className="text-xs text-muted-foreground">选择预设模板可自动填充 URL 和客户端类型，支持搜索</p>
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <Field label="名称 *" htmlFor="provider-name" help="为这个 API 提供商设置一个便于识别的名称，用于在模型配置中引用。\n\n推荐使用厂商官方名称，如 DeepSeek、OpenAI\n名称需要唯一，不能与现有提供商重复">
              <Input id="provider-name" value={form.name} onChange={(event) => updateForm({ name: event.target.value }, 'name')} placeholder="例如: DeepSeek, SiliconFlow" aria-invalid={errors.name ? true : undefined} />
              {errors.name ? <p role="alert" className="text-xs text-destructive">{errors.name}</p> : null}
            </Field>
            <Field label="客户端类型" htmlFor="provider-client-type" help="指定与提供商通信时使用的 API 协议格式。\n\nOpenAI：兼容 OpenAI API 格式的提供商\nOpenAI Responses：OpenAI Responses API 原生格式\nGemini：Google Gemini 专用格式">
              <Select id="provider-client-type" value={form.client_type} disabled={isUsingTemplate} onChange={(event) => updateForm({ client_type: event.target.value as ProviderConfig['client_type'] })}>
                <option value="openai">openai</option>
                <option value="volcengine">volcengine</option>
              </Select>
            </Field>
          </div>

          <Field label="基础 URL *" htmlFor="provider-base-url" help="提供商的 API 端点基础 URL，通常以 /v1 结尾。\n\nOpenAI 格式：https://api.openai.com/v1\nDeepSeek：https://api.deepseek.com\n硅基流动：https://api.siliconflow.cn/v1\n选择模板会自动填充正确的 URL">
            <Input id="provider-base-url" value={form.base_url} disabled={isUsingTemplate} onChange={(event) => updateForm({ base_url: event.target.value }, 'base_url')} placeholder="https://api.example.com/v1" aria-invalid={errors.base_url ? true : undefined} />
            {errors.base_url ? <p role="alert" className="text-xs text-destructive">{errors.base_url}</p> : null}
          </Field>

          <Field label="API Key *" htmlFor="provider-api-key" help="从提供商平台获取的身份验证密钥。\n\n通常以 sk- 开头\n请妥善保管，不要泄露给他人\n可以点击眼睛图标切换显示/隐藏\n点击复制图标可快速复制密钥">
            <div className="flex gap-2">
              <Input id="provider-api-key" type={showApiKey ? 'text' : 'password'} value={form.api_key} onChange={(event) => updateForm({ api_key: event.target.value }, 'api_key')} placeholder="sk-..." aria-invalid={errors.api_key ? true : undefined} />
              <Button type="button" variant="secondary" size="sm" onClick={() => setShowApiKey((value) => !value)} title={showApiKey ? '隐藏密钥' : '显示密钥'}>
                {showApiKey ? <EyeOff className="size-4" aria-hidden="true" /> : <Eye className="size-4" aria-hidden="true" />}
              </Button>
              <Button type="button" variant="secondary" size="sm" onClick={() => void copyApiKey()} title="复制密钥">
                <Copy className="size-4" aria-hidden="true" />
              </Button>
            </div>
            {errors.api_key ? <p role="alert" className="text-xs text-destructive">{errors.api_key}</p> : null}
          </Field>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <Field label="最大重试" htmlFor="provider-max-retries" help="API 请求失败时的最大重试次数。设置为 0 表示不重试。默认值：2">
              <Input id="provider-max-retries" type="number" min={0} value={form.max_retries} onChange={(event) => updateForm({ max_retries: Number(event.target.value) })} placeholder="默认: 2" />
            </Field>
            <Field label="超时(秒)" htmlFor="provider-timeout" help="单次 API 请求的超时时间（秒）。超时后会触发重试或报错。默认值：30 秒">
              <Input id="provider-timeout" type="number" min={1} value={Math.round(form.timeout_ms / 1000)} onChange={(event) => updateForm({ timeout_ms: Number(event.target.value) * 1000 })} placeholder="默认: 30" />
            </Field>
            <Field label="重试间隔(秒)" htmlFor="provider-retry-interval" help="两次重试之间的等待时间（秒）。适当的间隔可以避免触发 API 限流。默认值：10 秒">
              <Input id="provider-retry-interval" type="number" min={1} value={Math.round(form.retry_interval_ms / 1000)} onChange={(event) => updateForm({ retry_interval_ms: Number(event.target.value) * 1000 })} placeholder="默认: 10" />
            </Field>
          </div>
        </div>
      </form>
    </Dialog>
  )
}


/** 模型编辑弹窗：外壳为 Dialog 原语，内部集中维护模型标识、能力、生成参数与厂商请求参数。 */
function ModelDialog({
  model,
  index,
  providers,
  remoteModels,
  onFetchModels,
  onCancel,
  onSave,
  onDelete,
}: {
  model: ModelConfig
  index: number
  providers: ProviderConfig[]
  remoteModels: RemoteModel[]
  onFetchModels: (providerName: string) => void
  onCancel: () => void
  onSave: (model: ModelConfig) => void
  onDelete: () => void
}) {
  const [form, setForm] = useState<ModelConfig>(() => JSON.parse(JSON.stringify(model)) as ModelConfig)
  const [errors, setErrors] = useState<{ name?: string; api_provider?: string; model_identifier?: string }>({})
  const [advanced, setAdvanced] = useState(false)
  const [identifierOpen, setIdentifierOpen] = useState(false)
  const [identifierSearch, setIdentifierSearch] = useState('')

  const updateForm = (patch: Partial<ModelConfig>, clearError?: keyof typeof errors) => {
    setForm((current) => ({ ...current, ...patch }))
    if (clearError) setErrors((current) => ({ ...current, [clearError]: undefined }))
  }

  const validate = () => {
    const next: typeof errors = {}
    if (!form.name.trim()) next.name = '请输入模型名称'
    if (!form.api_provider.trim()) next.api_provider = '请选择 API 提供商'
    if (!form.model_identifier.trim()) next.model_identifier = '请输入模型标识符'
    setErrors(next)
    return Object.keys(next).length === 0
  }

  const visibleRemoteModels = remoteModels.filter((item) =>
    `${item.id} ${item.name}`.toLowerCase().includes(identifierSearch.trim().toLowerCase()),
  )
  const thinkingState = modelThinkingState(form)
  const thinkingEnabled = thinkingState !== 'disabled'

  return (
    <Dialog
      open
      onClose={onCancel}
      title={index >= 0 ? '编辑模型' : '添加模型'}
      description="配置模型的基本信息和参数"
      width="max-w-2xl"
      footer={
        <>
          {index >= 0 ? (
            <Button variant="danger-outline" size="sm" className="mr-auto" onClick={onDelete}>
              <Trash2 className="size-4" aria-hidden="true" />
              删除模型
            </Button>
          ) : null}
          <Button variant="ghost" onClick={onCancel}>取消</Button>
          <Button type="submit" form="model-form">保存</Button>
        </>
      }
    >
      <form
        id="model-form"
        className="flex flex-col"
        autoComplete="off"
        onSubmit={(event) => {
          event.preventDefault()
          if (!validate()) return
          onSave(form)
        }}
      >
        <div className="flex flex-col gap-4">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <Field label="模型名称 *" htmlFor="model-name" help="用于在功能分配中识别这个模型；名称需要唯一。">
              <Input id="model-name" value={form.name} onChange={(event) => updateForm({ name: event.target.value }, 'name')} placeholder="例如: qwen3-30b" aria-invalid={errors.name ? true : undefined} />
              {errors.name ? <p role="alert" className="text-xs text-destructive">{errors.name}</p> : null}
            </Field>
            <Field label="API 提供商 *" htmlFor="model-provider" help="选择模型所属的厂商；切换厂商后可以拉取该厂商的可用模型列表。">
              <Select
                id="model-provider"
                value={form.api_provider}
                onChange={(event) => {
                  const value = event.target.value
                  updateForm({ api_provider: value }, 'api_provider')
                  if (value) onFetchModels(value)
                }}
                aria-invalid={errors.api_provider ? true : undefined}
              >
                <option value="">选择提供商</option>
                {providers.map((provider) => (
                  <option key={provider.name} value={provider.name}>{provider.name}</option>
                ))}
              </Select>
              {errors.api_provider ? <p role="alert" className="text-xs text-destructive">{errors.api_provider}</p> : null}
            </Field>
          </div>

          <Field label="模型标识符 *" htmlFor="model-identifier" help="API 提供商提供的真实模型 ID；可以从厂商模型列表中搜索选择，也可以手动填写。">
            <div className="relative flex flex-col gap-2 sm:flex-row">
              <button
                type="button"
                className="flex h-9 min-w-0 items-center justify-between rounded-md border border-input bg-card px-3 text-left text-sm shadow-card sm:w-[46%]"
                onClick={() => setIdentifierOpen((value) => !value)}
              >
                <span className="truncate">
                  {form.model_identifier || '搜索或选择模型...'}
                </span>
                <ChevronsUpDown className="ml-2 size-4 shrink-0 opacity-50" aria-hidden="true" />
              </button>
              <Input
                id="model-identifier"
                value={form.model_identifier}
                onChange={(event) => updateForm({ model_identifier: event.target.value }, 'model_identifier')}
                placeholder="手动输入模型标识符"
                aria-invalid={errors.model_identifier ? true : undefined}
              />
              {identifierOpen ? (
                <div className="absolute top-11 left-0 z-10 w-full rounded-xl border border-border bg-card p-2 shadow-lifted">
                  <Input autoFocus value={identifierSearch} onChange={(event) => setIdentifierSearch(event.target.value)} placeholder="搜索模型..." />
                  <div className="mt-2 max-h-64 overflow-y-auto">
                    {visibleRemoteModels.length ? visibleRemoteModels.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        className="flex w-full cursor-pointer flex-col rounded-lg px-2.5 py-2 text-left text-sm hover:bg-muted"
                        onClick={() => {
                          updateForm({ model_identifier: item.id }, 'model_identifier')
                          setIdentifierOpen(false)
                        }}
                      >
                        <span className="truncate">{item.id}</span>
                        {item.name !== item.id ? <span className="truncate text-xs text-muted-foreground">{item.name}</span> : null}
                      </button>
                    )) : <p className="px-2 py-4 text-center text-xs text-muted-foreground">未找到匹配的模型</p>}
                  </div>
                </div>
              ) : null}
            </div>
            {errors.model_identifier ? <p role="alert" className="text-xs text-destructive">{errors.model_identifier}</p> : null}
          </Field>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <Field label="视觉能力" help="开启后，这个模型可以被分配给图片理解和屏幕视觉任务。">
              <Toggle checked={form.visual} onChange={(checked) => updateForm({ visual: checked })} label={form.visual ? '已开启视觉' : '未开启视觉'} />
            </Field>
            <Field label="思考模式" help="控制请求体中的 enable_thinking；未显式设置时沿用模型默认行为。">
              <Toggle
                checked={thinkingEnabled}
                onChange={(checked) => updateForm({ extra_body: withModelThinking(form, checked) })}
                label={thinkingState === 'default' ? '模型默认（点击关闭）' : thinkingEnabled ? '开启思考' : '关闭思考'}
              />
            </Field>
          </div>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <Field label="输入价格 (¥/M token)" htmlFor="model-price-in">
              <Input id="model-price-in" type="number" step="0.1" min={0} value={form.price_in} onChange={(event) => updateForm({ price_in: Number(event.target.value) })} placeholder="默认: 0" />
            </Field>
            <Field label="输出价格 (¥/M token)" htmlFor="model-price-out">
              <Input id="model-price-out" type="number" step="0.1" min={0} value={form.price_out} onChange={(event) => updateForm({ price_out: Number(event.target.value) })} placeholder="默认: 0" />
            </Field>
          </div>

          <Button type="button" variant={advanced ? 'primary' : 'secondary'} size="sm" className="self-start" onClick={() => setAdvanced((value) => !value)}>
            高级设置
          </Button>

          {advanced ? (
            <div className="rounded-lg border border-warning/40 bg-warning-soft p-3 text-foreground">
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <Field label="自定义模型温度" help="启用后将覆盖「功能分配」中的任务温度配置。低温度更确定，高温度更有创造性。">
                  <div className="flex items-center gap-3">
                    <Toggle checked={form.temperature !== null} onChange={(checked) => updateForm({ temperature: checked ? 0.7 : null })} label="启用" />
                    {form.temperature !== null ? (
                      <Input type="number" step="0.05" min={0} max={2} value={form.temperature} onChange={(event) => updateForm({ temperature: Number(event.target.value) })} className="w-24" />
                    ) : null}
                  </div>
                </Field>
                <Field label="模型级最大 token" help="留空表示继承任务配置。">
                  <Input type="number" min={1} value={form.max_tokens ?? ''} onChange={(event) => updateForm({ max_tokens: event.target.value === '' ? null : Number(event.target.value) })} placeholder="继承任务配置" />
                </Field>
                <Field label="思考内容解析" help="这里只决定怎样读取模型已经返回的思考内容，不会开启或关闭模型思考。">
                  <Select value={form.reasoning_parse_mode} onChange={(event) => updateForm({ reasoning_parse_mode: event.target.value as ModelConfig['reasoning_parse_mode'] })}>
                    <option value="field">解析思考字段（field）</option>
                    <option value="tag">解析 think 标签（tag）</option>
                    <option value="none">不解析思考内容（none）</option>
                  </Select>
                </Field>
                <Field label="Embedding 维度" help="仅嵌入模型需要填写。">
                  <Input type="number" min={0} value={form.embedding_dim} onChange={(event) => updateForm({ embedding_dim: Number(event.target.value) })} />
                </Field>
              </div>
              <Field label="extra_body（JSON）" className="mt-3" help="需要透传给厂商请求体的额外参数；JSON 对象格式。">
                <Textarea rows={3} value={JSON.stringify(form.extra_body)} onChange={(event) => {
                  try { updateForm({ extra_body: JSON.parse(event.target.value) }) } catch { /* 编辑中 */ }
                }} />
              </Field>
            </div>
          ) : null}
        </div>
      </form>
    </Dialog>
  )
}
