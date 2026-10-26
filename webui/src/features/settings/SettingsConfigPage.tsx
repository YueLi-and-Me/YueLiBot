/**
 * 月璃设置页：按后端返回的 settings_schema.json 动态渲染五份配置。
 *
 * schema 是字段、中文说明、类型与约束的唯一映射来源，因此本页不写死具体配置项；
 * 支持 object、map（model_tasks/generation）和 table_list（厂商/模型目录）三类
 * 结构。保存时整包提交，后端完成启动级校验后原子写回五个 TOML 文件。
 *
 * 套用「设置表单」模板：左栏列出配置文件及当前文件的分区（点击滚动到对应分区），
 * 中栏为表单，字段一行一项、说明在左控件在右；底部保存条常驻，有未保存的修改时
 * 以琥珀色提示，并提供放弃修改。
 */
import { FileCog, Plus, RefreshCw, Save, Trash2 } from 'lucide-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'

import { PageFrame } from '@/components/layout/PageFrame'
import { Pane, PaneFooter, PaneHeader, PaneTitle, Panes, ScrollArea } from '@/components/layout/panes'
import {
  Button,
  Card,
  CardBody,
  Checkbox,
  ConfirmDialog,
  ErrorText,
  FlatCards,
  Input,
  Loading,
  SectionHeading,
  Select,
  Textarea,
  Toggle,
  cn,
  toast,
} from '@/components/ui'
import {
  useSettingsConfig,
  type SettingsFieldSchema,
  type SettingsFileSchema,
  type SettingsMapSection,
  type SettingsObjectSection,
  type SettingsSnapshot,
  type SettingsTableListSection,
} from '@/hooks/use-settings-config'
import { hasDraftChanges, useRestart } from '@/hooks/use-restart'

type SettingsValues = SettingsSnapshot['values']
type SettingsRecord = Record<string, unknown>

/** 深拷贝后端快照，避免表单直接修改服务端返回对象。 */
function cloneSnapshot(snapshot: SettingsSnapshot): SettingsSnapshot {
  return JSON.parse(JSON.stringify(snapshot)) as SettingsSnapshot
}

/** 把未知值安全收缩为记录。 */
function asRecord(value: unknown): SettingsRecord {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as SettingsRecord
    : {}
}

/** 把未知值安全收缩为字符串数组。 */
function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)) : []
}

/** 把未知值安全收缩为记录数组。 */
function asRecordArray(value: unknown): SettingsRecord[] {
  return Array.isArray(value)
    ? value.filter((item) => item && typeof item === 'object').map((item) => item as SettingsRecord)
    : []
}

/** 根据字段类型生成新表行/新 map 条目的默认值。 */
function defaultValueForField(field: SettingsFieldSchema): unknown {
  switch (field.type) {
    case 'boolean': return false
    case 'integer': return 0
    case 'number': return 0
    case 'enum': return field.options?.[0]?.value ?? ''
    case 'multi_enum': return []
    case 'string_list': return []
    case 'string_map': return {}
    case 'json': return {}
    case 'table_list': return []
    case 'date': return ''
    case 'time': return ''
    case 'password':
    case 'string':
    case 'textarea':
    default: return ''
  }
}

/** 判断 map section 的字段是否出现在当前 entry 下。 */
function fieldAppliesToEntry(field: SettingsFieldSchema, entryKey: string): boolean {
  return !field.only_for_entries || field.only_for_entries.includes(entryKey)
}

/** JSON 对象字段：显示格式化 JSON，解析成功才提交新值。 */
function JsonField({ value, onChange }: { value: unknown; onChange: (value: unknown) => void }) {
  const [text, setText] = useState(() => JSON.stringify(value ?? {}, null, 2))
  const [invalid, setInvalid] = useState(false)
  const apply = (next: string) => {
    setText(next)
    if (!next.trim()) {
      setInvalid(false)
      onChange({})
      return
    }
    try {
      const parsed = JSON.parse(next) as unknown
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        setInvalid(true)
        return
      }
      setInvalid(false)
      onChange(parsed)
    } catch {
      setInvalid(true)
    }
  }
  return (
    <div>
      <Textarea rows={4} value={text} onChange={(event) => apply(event.target.value)} className="font-mono text-xs" />
      {invalid ? <p role="alert" className="mt-1 text-xs text-destructive">JSON 无效：请输入对象格式，例如 {'{"key":"value"}'}</p> : null}
    </div>
  )
}

/** 每行一条的字符串列表字段。 */
function StringListField({ value, onChange }: { value: unknown; onChange: (value: string[]) => void }) {
  const list = asStringArray(value)
  return (
    <Textarea
      rows={Math.max(2, Math.min(8, list.length + 1))}
      value={list.join('\n')}
      onChange={(event) => onChange(event.target.value.split('\n').map((item) => item.trim()).filter(Boolean))}
      placeholder="每行一条"
    />
  )
}

/** key=value 形式的字符串映射字段。 */
function StringMapField({ value, onChange }: { value: unknown; onChange: (value: Record<string, string>) => void }) {
  const record = asRecord(value)
  const text = Object.entries(record)
    .map(([key, item]) => `${key}=${String(item ?? '')}`)
    .join('\n')
  const apply = (next: string) => {
    const result: Record<string, string> = {}
    for (const line of next.split('\n')) {
      const trimmed = line.trim()
      if (!trimmed) continue
      const index = trimmed.indexOf('=')
      if (index <= 0) continue
      result[trimmed.slice(0, index).trim()] = trimmed.slice(index + 1).trim()
    }
    onChange(result)
  }
  return <Textarea rows={3} value={text} onChange={(event) => apply(event.target.value)} placeholder="每行一条 key=value" />
}

/** 多选枚举字段。 */
function MultiEnumField({ field, value, onChange }: { field: SettingsFieldSchema; value: unknown; onChange: (value: string[]) => void }) {
  const selected = new Set(asStringArray(value))
  const options = field.options ?? []
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-2 pt-1.5">
      {options.map((option) => {
        const checked = selected.has(option.value)
        return (
          <Checkbox
            key={option.value}
            checked={checked}
            label={option.label}
            onChange={() => {
              const next = new Set(selected)
              if (checked) next.delete(option.value)
              else next.add(option.value)
              onChange(options.filter((item) => next.has(item.value)).map((item) => item.value))
            }}
          />
        )
      })}
    </div>
  )
}

/** 字段行的列模板：宽屏时说明在左（固定 15rem）、控件在右；窄屏上下排列。 */
const FIELD_ROW = 'grid min-w-0 gap-x-8 gap-y-1.5 py-3.5 md:grid-cols-[minmax(0,15rem)_minmax(0,1fr)]'

/** 渲染带可见中文说明的单个配置字段：一行一项，说明在左、控件在右。 */
function SettingsField({ field, value, onChange }: {
  field: SettingsFieldSchema
  value: unknown
  onChange: (value: unknown) => void
}) {
  return (
    <label className={FIELD_ROW}>
      <span className="min-w-0">
        <span className="block text-[13px] font-medium text-foreground">
          {field.label}
          {field.required ? <span className="ml-0.5 text-destructive">*</span> : null}
        </span>
        {field.help ? (
          <span className="mt-0.5 block text-xs leading-relaxed text-muted-foreground">{field.help}</span>
        ) : null}
      </span>
      <span className="min-w-0 max-w-xl self-start">
        <FieldControl field={field} value={value} onChange={onChange} />
      </span>
    </label>
  )
}

/** 字段行列表：行与行之间以细线分隔。 */
function FieldRows({ children }: { children: ReactNode }) {
  return <div className="flex flex-col divide-y divide-border-subtle">{children}</div>
}

/**
 * 计算分区在页面里的锚点 id，供左栏的分区导航滚动定位。
 *
 * @param sectionKey schema 中的分区键。
 * @returns DOM id。
 */
function sectionAnchor(sectionKey: string): string {
  return `settings-section-${sectionKey}`
}

/** 按 schema 类型选择控件。 */
function FieldControl({ field, value, onChange }: {
  field: SettingsFieldSchema
  value: unknown
  onChange: (value: unknown) => void
}) {
  if (field.type === 'boolean') {
    return <Toggle checked={Boolean(value)} onChange={(next) => onChange(next)} label={String(value === true ? '已开启' : '已关闭')} />
  }
  if (field.type === 'enum') {
    return (
      <Select value={String(value ?? '')} onChange={(event) => onChange(event.target.value)}>
        {(field.options ?? []).map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </Select>
    )
  }
  if (field.type === 'multi_enum') {
    return <MultiEnumField field={field} value={value} onChange={(next) => onChange(next)} />
  }
  if (field.type === 'string_list') {
    return <StringListField value={value} onChange={(next) => onChange(next)} />
  }
  if (field.type === 'string_map') {
    return <StringMapField value={value} onChange={(next) => onChange(next)} />
  }
  if (field.type === 'json') {
    return <JsonField value={value} onChange={(next) => onChange(next)} />
  }
  if (field.type === 'textarea') {
    return <Textarea rows={5} value={String(value ?? '')} onChange={(event) => onChange(event.target.value)} />
  }
  if (field.type === 'date') {
    return <Input type="date" value={String(value ?? '')} onChange={(event) => onChange(event.target.value)} />
  }
  if (field.type === 'time') {
    return <Input type="time" value={String(value ?? '')} onChange={(event) => onChange(event.target.value)} />
  }
  if (field.type === 'password') {
    return (
      <Input
        type="password"
        value={String(value ?? '')}
        onChange={(event) => onChange(event.target.value)}
        placeholder="新条目填写密钥；已保存的条目留空保持不变"
        autoComplete="new-password"
      />
    )
  }
  if (field.type === 'integer' || field.type === 'number') {
    const isNullable = field.nullable === true
    return (
      <Input
        type="number"
        min={field.min}
        max={field.max}
        step={field.step ?? (field.type === 'integer' ? 1 : 0.05)}
        value={value === null || value === undefined ? '' : String(value)}
        onChange={(event) => {
          if (event.target.value === '') {
            onChange(isNullable ? null : 0)
            return
          }
          const parsed = Number(event.target.value)
          onChange(Number.isFinite(parsed) ? (field.type === 'integer' ? Math.trunc(parsed) : parsed) : 0)
        }}
      />
    )
  }
  return (
    <Input
      type="text"
      value={String(value ?? '')}
      maxLength={field.maxLength}
      onChange={(event) => onChange(event.target.value)}
    />
  )
}

/** 渲染普通 object 配置段。 */
function ObjectSection({ section, values, onChange }: {
  section: SettingsObjectSection
  values: SettingsRecord
  onChange: (fieldKey: string, value: unknown) => void
}) {
  return (
    <Card id={sectionAnchor(section.key)} className="scroll-mt-2">
      <SectionHeading title={section.label} subtitle={section.description} />
      <CardBody className="pt-0">
        <FieldRows>
          {section.fields.map((field) => (
            <SettingsField
              key={field.key}
              field={field}
              value={values[field.key]}
              onChange={(value) => onChange(field.key, value)}
            />
          ))}
        </FieldRows>
      </CardBody>
    </Card>
  )
}

/** 渲染 map 配置段（model_tasks / generation）。 */
function MapSection({ section, values, onChange }: {
  section: SettingsMapSection
  values: SettingsRecord
  onChange: (entryKey: string, fieldKey: string, value: unknown) => void
}) {
  return (
    <Card id={sectionAnchor(section.key)} className="scroll-mt-2">
      <SectionHeading title={section.label} subtitle={section.description} />
      <CardBody className="flex flex-col gap-5 pt-0">
        {section.entries.map((entry) => (
          <div key={entry.key} className="rounded-lg border border-border px-4">
            <div className="flex flex-wrap items-baseline gap-x-2 border-b border-border-subtle py-2.5">
              <h3 className="text-[13px] font-semibold">{entry.label}</h3>
              {entry.description ? <p className="text-xs text-subtle-foreground">{entry.description}</p> : null}
            </div>
            <FieldRows>
              {section.fields
                .filter((field) => fieldAppliesToEntry(field, entry.key))
                .map((field) => (
                  <SettingsField
                    key={field.key}
                    field={field}
                    value={asRecord(values[entry.key])[field.key]}
                    onChange={(value) => onChange(entry.key, field.key, value)}
                  />
                ))}
            </FieldRows>
          </div>
        ))}
      </CardBody>
    </Card>
  )
}

/** 渲染 table_list 配置段（api_providers / models）。 */
function TableListSection({ section, values, onChange, onAdd, onRemove }: {
  section: SettingsTableListSection
  values: SettingsRecord
  onChange: (rowIndex: number, fieldKey: string, value: unknown) => void
  onAdd: () => void
  onRemove: (rowIndex: number) => void
}) {
  const rows = asRecordArray(values[section.key])
  // 待确认删除的行索引；null 表示确认弹窗处于关闭状态。
  const [pendingRemove, setPendingRemove] = useState<number | null>(null)
  const rowTitle = (row: SettingsRecord, index: number) => {
    const name = String(row.name ?? row.model_identifier ?? '')
    return name ? `第 ${index + 1} 条 · ${name}` : `第 ${index + 1} 条（未命名）`
  }
  return (
    <Card id={sectionAnchor(section.key)} className="scroll-mt-2">
      <SectionHeading
        title={section.label}
        subtitle={section.description}
        actions={
          <Button size="sm" variant="secondary" onClick={onAdd}>
            <Plus className="size-3.5" aria-hidden="true" />
            添加条目
          </Button>
        }
      />
      <CardBody className="flex flex-col gap-5 pt-0">
      {rows.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-[13px] text-muted-foreground">
          暂无条目，点击「添加条目」创建。
        </p>
      ) : null}
      {rows.map((row, rowIndex) => (
        <div key={`${section.key}-${rowIndex}-${String(row.name ?? '')}`} className="rounded-lg border border-border px-4">
          <div className="flex items-center justify-between gap-3 border-b border-border-subtle py-2">
            <h3 className="truncate text-[13px] font-semibold">{rowTitle(row, rowIndex)}</h3>
            <Button
              size="sm"
              variant="ghost"
              className="text-destructive hover:bg-destructive-soft hover:text-destructive"
              onClick={() => setPendingRemove(rowIndex)}
              title="删除条目"
            >
              <Trash2 className="size-3.5" aria-hidden="true" />
              删除
            </Button>
          </div>
          <FieldRows>
            {/* 隐藏字段不展示也不改动，行内原值随保存原样写回。 */}
            {section.fields.filter((field) => !field.hidden).map((field) => (
              <SettingsField
                key={field.key}
                field={field}
                value={row[field.key]}
                onChange={(value) => onChange(rowIndex, field.key, value)}
              />
            ))}
          </FieldRows>
        </div>
      ))}
      </CardBody>
      <ConfirmDialog
        open={pendingRemove !== null}
        title="删除条目"
        description={
          pendingRemove !== null
            ? `删除${rowTitle(rows[pendingRemove] ?? {}, pendingRemove)}？改动会在保存配置后写回文件。`
            : undefined
        }
        confirmText="删除"
        onConfirm={() => {
          if (pendingRemove !== null) onRemove(pendingRemove)
          setPendingRemove(null)
        }}
        onCancel={() => setPendingRemove(null)}
      />
    </Card>
  )
}

/** 渲染月璃设置页。 */
export function SettingsConfigPage() {
  const state = useSettingsConfig()
  const [draft, setDraft] = useState<SettingsValues | null>(null)
  const [activeFile, setActiveFile] = useState('bot.toml')
  const [restartConfirmOpen, setRestartConfirmOpen] = useState(false)
  // 表单滚动区顶端的定位点：切换配置文件时回到表单开头。
  const formTopRef = useRef<HTMLDivElement>(null)
  // 与模型页共用同一段重启逻辑；确认弹窗在草稿有未保存改动时加丢失提醒。
  const { restarting, restartBackend } = useRestart()

  useEffect(() => {
    const snapshot = state.snapshot
    if (!snapshot) return
    const next = cloneSnapshot(snapshot)
    setDraft(next.values)
    setActiveFile((current) => (
      next.schema.files.some((file) => file.file === current) ? current : next.schema.files[0]?.file ?? current
    ))
  }, [state.snapshot])

  // 保存结果由 hook 以 status 文本回报（失败文本带「保存失败」前缀）；这里转成全局
  // Toast 后立即清除，页面不再渲染内联状态条。进行中的「正在校验并保存…」只通过
  // 按钮禁用态体现，不弹通知。
  const { status, clearStatus } = state
  useEffect(() => {
    if (!status || status === '正在校验并保存…') return
    if (status.startsWith('保存失败')) toast.error(status)
    else toast.success(status)
    clearStatus()
  }, [status, clearStatus])

  if (state.loading && !draft) {
    return (
      <PageFrame>
        <Loading>正在读取月璃设置…</Loading>
      </PageFrame>
    )
  }
  if (!draft || !state.snapshot) {
    return (
      <PageFrame>
        <div className="px-6 pt-4">
          <ErrorText>{state.error || '月璃设置不可用'}</ErrorText>
        </div>
      </PageFrame>
    )
  }

  const files = state.snapshot.schema.files
  const selectedFile = files.find((item) => item.file === activeFile) ?? files[0]

  const updateObjectField = (file: string, sectionKey: string, fieldKey: string, value: unknown) => {
    setDraft((current) => {
      if (!current) return current
      const fileValues = asRecord(current[file])
      const sectionValues = asRecord(fileValues[sectionKey])
      return {
        ...current,
        [file]: { ...fileValues, [sectionKey]: { ...sectionValues, [fieldKey]: value } },
      }
    })
  }

  const updateMapEntryField = (file: string, sectionKey: string, entryKey: string, fieldKey: string, value: unknown) => {
    setDraft((current) => {
      if (!current) return current
      const fileValues = asRecord(current[file])
      const sectionValues = asRecord(fileValues[sectionKey])
      const entryValues = asRecord(sectionValues[entryKey])
      return {
        ...current,
        [file]: {
          ...fileValues,
          [sectionKey]: { ...sectionValues, [entryKey]: { ...entryValues, [fieldKey]: value } },
        },
      }
    })
  }

  const updateTableRow = (file: string, sectionKey: string, rowIndex: number, fieldKey: string, value: unknown) => {
    setDraft((current) => {
      if (!current) return current
      const fileValues = asRecord(current[file])
      const rows = asRecordArray(fileValues[sectionKey]).map((row, index) => (
        index === rowIndex ? { ...row, [fieldKey]: value } : row
      ))
      return { ...current, [file]: { ...fileValues, [sectionKey]: rows } }
    })
  }

  const addTableRow = (section: SettingsTableListSection) => {
    const row: SettingsRecord = {}
    for (const field of section.fields) row[field.key] = defaultValueForField(field)
    setDraft((current) => {
      if (!current) return current
      const fileValues = asRecord(current[activeFile])
      const rows = asRecordArray(fileValues[section.key])
      return { ...current, [activeFile]: { ...fileValues, [section.key]: [...rows, row] } }
    })
  }

  const removeTableRow = (sectionKey: string, rowIndex: number) => {
    setDraft((current) => {
      if (!current) return current
      const fileValues = asRecord(current[activeFile])
      const rows = asRecordArray(fileValues[sectionKey]).filter((_, index) => index !== rowIndex)
      return { ...current, [activeFile]: { ...fileValues, [sectionKey]: rows } }
    })
  }
  const saveDraft = () => {
    state.clearStatus()
    void state.save(draft)
  }
  const discardDraft = () => {
    if (state.snapshot) setDraft(cloneSnapshot(state.snapshot).values)
  }

  const dirty = hasDraftChanges(draft, state.snapshot.values)
  const visibleSections = selectedFile ? selectedFile.sections.filter((section) => !section.hidden) : []

  return (
    <PageFrame
      hint="直接编辑五份配置文件；字段说明来自 settings_schema.json，保存后重启后端生效。"
      actions={
        <>
          <Button variant="secondary" onClick={state.reload} disabled={state.busy}>
            <RefreshCw className="size-4" aria-hidden="true" />
            重新读取
          </Button>
          <Button
            variant="secondary"
            onClick={() => setRestartConfirmOpen(true)}
            disabled={state.busy || restarting}
          >
            <RefreshCw className="size-4" aria-hidden="true" />
            重启后端
          </Button>
        </>
      }
    >
      <Panes>
        <Pane width="nav" aria-label="配置分区">
          <PaneHeader>
            <PaneTitle>配置文件</PaneTitle>
          </PaneHeader>
          <ScrollArea className="px-2 py-2">
            <nav aria-label="配置文件与分区" className="flex flex-col gap-px">
              {files.map((file) => {
                const active = file.file === selectedFile?.file
                return (
                  <div key={file.file}>
                    <button
                      type="button"
                      onClick={() => {
                        setActiveFile(file.file)
                        formTopRef.current?.scrollIntoView({ block: 'start' })
                      }}
                      aria-current={active ? 'true' : undefined}
                      className={cn(
                        'flex h-8 w-full cursor-pointer items-center gap-2 rounded-md px-3 text-left text-[13px] transition-colors',
                        active ? 'bg-nav-active font-medium text-foreground' : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                      )}
                    >
                      <FileCog className={cn('size-4 flex-none', active && 'text-primary')} aria-hidden="true" />
                      <span className="min-w-0 flex-1 truncate">{file.label}</span>
                      <span className="flex-none font-mono text-[11px] text-subtle-foreground">{file.file}</span>
                    </button>
                    {active && visibleSections.length > 1 ? (
                      <div className="my-1 ml-[22px] flex flex-col gap-px border-l border-border pl-2">
                        {visibleSections.map((section) => (
                          <button
                            key={section.key}
                            type="button"
                            onClick={() =>
                              document.getElementById(sectionAnchor(section.key))?.scrollIntoView({ behavior: 'smooth', block: 'start' })
                            }
                            className="flex h-7 cursor-pointer items-center truncate rounded-md px-2.5 text-left text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                          >
                            {section.label}
                          </button>
                        ))}
                      </div>
                    ) : null}
                  </div>
                )
              })}
            </nav>
          </ScrollArea>
        </Pane>

        <Pane aria-label="配置表单">
          <PaneHeader>
            <PaneTitle>{selectedFile?.label ?? '配置'}</PaneTitle>
            {selectedFile ? <span className="font-mono text-xs text-subtle-foreground">{selectedFile.file}</span> : null}
          </PaneHeader>
          <ScrollArea>
            <div ref={formTopRef} />
            {state.error ? <div className="px-6 pt-3"><ErrorText>{state.error}</ErrorText></div> : null}
            {selectedFile ? (
              <div className="max-w-5xl">
                {selectedFile.description ? (
                  <p className="px-6 pt-4 text-xs leading-relaxed text-muted-foreground">{selectedFile.description}</p>
                ) : null}
                <FlatCards>
                  {visibleSections.map((section) => {
                    const fileValues = asRecord(draft[selectedFile.file])
                    if (section.kind === 'object') {
                      return (
                        <ObjectSection
                          key={section.key}
                          section={section}
                          values={asRecord(fileValues[section.key])}
                          onChange={(fieldKey, value) => updateObjectField(selectedFile.file, section.key, fieldKey, value)}
                        />
                      )
                    }
                    if (section.kind === 'map') {
                      return (
                        <MapSection
                          key={section.key}
                          section={section}
                          values={asRecord(fileValues[section.key])}
                          onChange={(entryKey, fieldKey, value) => updateMapEntryField(selectedFile.file, section.key, entryKey, fieldKey, value)}
                        />
                      )
                    }
                    return (
                      <TableListSection
                        key={section.key}
                        section={section}
                        values={fileValues}
                        onChange={(rowIndex, fieldKey, value) => updateTableRow(selectedFile.file, section.key, rowIndex, fieldKey, value)}
                        onAdd={() => addTableRow(section)}
                        onRemove={(rowIndex) => removeTableRow(section.key, rowIndex)}
                      />
                    )
                  })}
                </FlatCards>
              </div>
            ) : null}
          </ScrollArea>
          <PaneFooter className={cn(dirty && 'bg-warning-soft')}>
            <span className={cn('flex items-center gap-2 text-[13px]', dirty ? 'text-warning' : 'text-subtle-foreground')}>
              {dirty ? <span className="size-1.5 rounded-full bg-tint-amber" aria-hidden="true" /> : null}
              {dirty ? '有未保存的修改' : '与已保存的配置一致'}
            </span>
            <div className="ml-auto flex items-center gap-2">
              {dirty ? (
                <Button variant="ghost" onClick={discardDraft} disabled={state.busy}>
                  放弃修改
                </Button>
              ) : null}
              <Button onClick={saveDraft} disabled={state.busy}>
                <Save className="size-4" aria-hidden="true" />
                保存配置
              </Button>
            </div>
          </PaneFooter>
        </Pane>
      </Panes>

      <ConfirmDialog
        open={restartConfirmOpen}
        title="重启月璃"
        description={hasDraftChanges(draft, state.snapshot?.values ?? null)
          ? '重启期间她会暂时无法回复。当前有未保存的修改，重启后将丢失。'
          : '重启期间她会暂时无法回复。'}
        confirmText="重启"
        danger
        onConfirm={() => {
          setRestartConfirmOpen(false)
          void restartBackend()
        }}
        onCancel={() => setRestartConfirmOpen(false)}
      />
    </PageFrame>
  )
}
