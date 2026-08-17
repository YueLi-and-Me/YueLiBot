/**
 * 月璃设置页：按后端返回的 settings_schema.json 动态渲染五份配置。
 *
 * schema 是字段、中文说明、类型与约束的唯一映射来源，因此本页不写死具体配置项；
 * 支持 object、map（model_tasks/generation）和 table_list（厂商/模型目录）三类
 * 结构。保存时整包提交，后端完成启动级校验后原子写回五个 TOML 文件。
 */
import { FileCog, Plus, RefreshCw, Save, Settings, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'

import { PageHeader } from '@/components/layout/PageHeader'
import { Button, Card, CardBody, ErrorText, Input, Loading, Select, Textarea, Toggle } from '@/components/ui'
import {
  useSettingsConfig,
  type SettingsFieldSchema,
  type SettingsFileSchema,
  type SettingsMapSection,
  type SettingsObjectSection,
  type SettingsSnapshot,
  type SettingsTableListSection,
} from '@/hooks/use-settings-config'

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
          <label key={option.value} className="inline-flex cursor-pointer items-center gap-1.5 text-sm">
            <input
              type="checkbox"
              checked={checked}
              onChange={() => {
                const next = new Set(selected)
                if (checked) next.delete(option.value)
                else next.add(option.value)
                onChange(options.filter((item) => next.has(item.value)).map((item) => item.value))
              }}
            />
            {option.label}
          </label>
        )
      })}
    </div>
  )
}

/** 渲染带可见中文说明的单个配置字段。 */
function SettingsField({ field, value, onChange }: {
  field: SettingsFieldSchema
  value: unknown
  onChange: (value: unknown) => void
}) {
  const label = field.label + (field.required ? ' *' : '')
  return (
    <label className="flex min-w-0 flex-col gap-1">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <span className="text-[11px] leading-relaxed text-muted-foreground/80">{field.help}</span>
      <FieldControl field={field} value={value} onChange={onChange} />
    </label>
  )
}

/** 按 schema 类型选择控件。 */
function FieldControl({ field, value, onChange }: {
  field: SettingsFieldSchema
  value: unknown
  onChange: (value: unknown) => void
}) {
  if (field.type === 'boolean') {
    return <Toggle checked={Boolean(value)} onChange={(next) => onChange(next)} label={String(value === true ? '已开启' : '已关闭')} className="pt-1" />
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
    <Card>
      <div className="border-b border-border px-5 py-4">
        <h2 className="text-[15px] font-semibold">{section.label}</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">{section.description}</p>
      </div>
      <CardBody>
        <div className="grid gap-x-5 gap-y-4 md:grid-cols-2 xl:grid-cols-3">
          {section.fields.map((field) => (
            <SettingsField
              key={field.key}
              field={field}
              value={values[field.key]}
              onChange={(value) => onChange(field.key, value)}
            />
          ))}
        </div>
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
    <div className="space-y-4">
      <div className="px-1">
        <h2 className="text-[15px] font-semibold">{section.label}</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">{section.description}</p>
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        {section.entries.map((entry) => (
          <Card key={entry.key}>
            <div className="border-b border-border px-5 py-3">
              <h3 className="text-sm font-semibold">{entry.label}</h3>
              <p className="mt-0.5 text-xs text-muted-foreground">{entry.description}</p>
            </div>
            <CardBody>
              <div className="grid gap-x-5 gap-y-4 sm:grid-cols-2">
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
              </div>
            </CardBody>
          </Card>
        ))}
      </div>
    </div>
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
  const rowTitle = (row: SettingsRecord, index: number) => {
    const name = String(row.name ?? row.model_identifier ?? '')
    return name ? `第 ${index + 1} 条 · ${name}` : `第 ${index + 1} 条（未命名）`
  }
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3 px-1">
        <div>
          <h2 className="text-[15px] font-semibold">{section.label}</h2>
          <p className="mt-0.5 text-xs text-muted-foreground">{section.description}</p>
        </div>
        <Button size="sm" variant="secondary" onClick={onAdd}>
          <Plus className="size-4" aria-hidden="true" />
          添加条目
        </Button>
      </div>
      {rows.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-muted-foreground">
          暂无条目，点击「添加条目」创建。
        </p>
      ) : null}
      {rows.map((row, rowIndex) => (
        <Card key={`${section.key}-${rowIndex}-${String(row.name ?? '')}`}>
          <div className="flex items-center justify-between gap-3 border-b border-border px-5 py-3">
            <h3 className="text-sm font-semibold">{rowTitle(row, rowIndex)}</h3>
            <Button
              size="sm"
              variant="danger-outline"
              onClick={() => {
                if (window.confirm(`删除${rowTitle(row, rowIndex)}？`)) onRemove(rowIndex)
              }}
              title="删除条目"
            >
              <Trash2 className="size-3.5" aria-hidden="true" />
              删除
            </Button>
          </div>
          <CardBody>
            <div className="grid gap-x-5 gap-y-4 md:grid-cols-2 xl:grid-cols-3">
              {section.fields.map((field) => (
                <SettingsField
                  key={field.key}
                  field={field}
                  value={row[field.key]}
                  onChange={(value) => onChange(rowIndex, field.key, value)}
                />
              ))}
            </div>
          </CardBody>
        </Card>
      ))}
    </div>
  )
}

/** 渲染月璃设置页。 */
export function SettingsConfigPage() {
  const state = useSettingsConfig()
  const [draft, setDraft] = useState<SettingsValues | null>(null)
  const [activeFile, setActiveFile] = useState('bot.toml')

  useEffect(() => {
    const snapshot = state.snapshot
    if (!snapshot) return
    const next = cloneSnapshot(snapshot)
    setDraft(next.values)
    setActiveFile((current) => (
      next.schema.files.some((file) => file.file === current) ? current : next.schema.files[0]?.file ?? current
    ))
  }, [state.snapshot])

  if (state.loading && !draft) {
    return (
      <div className="mx-auto w-full max-w-[1440px] px-6 py-8">
        <Loading>正在读取月璃设置…</Loading>
      </div>
    )
  }
  if (!draft || !state.snapshot) {
    return (
      <div className="mx-auto w-full max-w-[1440px] px-6 py-8">
        <ErrorText>{state.error || '月璃设置不可用'}</ErrorText>
      </div>
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

  return (
    <div className="mx-auto flex w-full max-w-[1440px] flex-col gap-5 px-4 py-6 sm:px-6 lg:px-8">
      <PageHeader
        eyebrow="YUELI / WEBUI"
        title="月璃设置"
        subtitle="直接编辑五份配置文件；字段说明来自 settings_schema.json，保存后重启后端生效。"
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
          </>
        }
      />

      {state.error ? <ErrorText>{state.error}</ErrorText> : null}
      {state.status ? (
        <p className="rounded-lg border border-border bg-muted/40 px-3 py-2 text-sm text-muted-foreground" role="status">
          {state.status}
        </p>
      ) : null}

      <div className="flex flex-wrap gap-1.5 rounded-xl border border-border bg-card p-1.5 shadow-card">
        {files.map((file) => (
          <button
            key={file.file}
            type="button"
            onClick={() => setActiveFile(file.file)}
            className={`flex cursor-pointer items-center gap-1.5 rounded-lg px-3 py-2 text-[13px] font-medium transition-colors ${activeFile === file.file ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-muted hover:text-foreground'}`}
          >
            <Settings className="size-3.5" aria-hidden="true" />
            {file.label}
            <span className={`font-mono text-[10px] ${activeFile === file.file ? 'text-primary-foreground/70' : 'text-muted-foreground/70'}`}>
              {file.file}
            </span>
          </button>
        ))}
      </div>
      {selectedFile ? (
        <div className="space-y-4">
          <div className="flex items-start gap-2 rounded-xl border border-border bg-muted/20 px-4 py-3">
            <FileCog className="mt-0.5 size-4 flex-none text-primary" aria-hidden="true" />
            <p className="text-xs leading-relaxed text-muted-foreground">
              <strong className="text-foreground">{selectedFile.label}（{selectedFile.file}）</strong>
              {' '}— {selectedFile.description}
            </p>
          </div>
          {selectedFile.sections.map((section) => {
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
        </div>
      ) : null}
    </div>
  )
}
