/** 复用现有控件渲染配置声明；内联值错误保留在输入框并阻止保存。 */
import { useEffect, useState } from 'react'

import { ErrorText, Field, Input, Select, Toggle } from '@/components/ui'

import { inlineToml, parseInlineToml, schemaFields } from './form'

export function TomlInput({ id, text, error, onChange }: { id: string; text: string; error: string; onChange: (text: string) => void }) {
  return <>
    <Input id={id} value={text} aria-invalid={!!error} className={error ? 'border-destructive text-destructive' : ''}
      onChange={event => onChange(event.target.value)} />
    {error ? <ErrorText>{error}</ErrorText> : null}
  </>
}

export function InlineField({ label, value, type, onChange, onError, hint, id }: {
  label: string; value: unknown; type: 'object' | 'array'; onChange: (value: unknown) => void
  onError: (invalid: boolean) => void; hint?: string; id: string
}) {
  const [text, setText] = useState(() => inlineToml(value))
  const [error, setError] = useState('')
  useEffect(() => { setText(inlineToml(value)); setError(''); onError(false) }, [value])
  return <Field label={label} htmlFor={id}>
    <TomlInput id={id} text={text} error={error}
      onChange={next => {
        setText(next)
        try {
          const parsed = parseInlineToml(next, type)
          setError(''); onError(false); onChange(parsed)
        } catch (cause) {
          setError(`TOML 内联值无效：${cause instanceof Error ? cause.message : String(cause)}`); onError(true)
        }
      }} />
    {hint ? <p className="text-xs leading-relaxed text-subtle-foreground">{hint}</p> : null}
  </Field>
}

export function SchemaForm({ schema, values, secretSet, onChange, onError }: {
  schema: Record<string, unknown>; values: Record<string, unknown>; secretSet: Record<string, boolean>
  onChange: (name: string, value: unknown) => void; onError: (name: string, invalid: boolean) => void
}) {
  return <div className="grid gap-5 sm:grid-cols-2">{schemaFields(schema).map(field => {
    const id = `plugin-field-${field.name}`
    const value = values[field.name]
    if (field.type === 'object' || field.type === 'array') return <InlineField key={field.name} id={id}
      label={field.label} type={field.type} value={value} hint={field.description}
      onError={invalid => onError(field.name, invalid)} onChange={next => onChange(field.name, next)} />
    return <Field key={field.name} label={field.label} htmlFor={id}>
      {field.type === 'boolean' ? <Toggle checked={value === true} label={field.name === 'enabled' ? '启用插件' : field.label}
        onChange={checked => onChange(field.name, checked)} />
        : field.enum ? <Select id={id} value={String(value)} onChange={e => onChange(field.name, field.enum?.find(item => String(item) === e.target.value))}>
          {field.enum.map(item => <option key={String(item)} value={String(item)}>{String(item)}</option>)}
        </Select>
        : field.type === 'string' ? <Input id={id} type={field.secret ? 'password' : 'text'} autoComplete="off"
          placeholder={field.secret && secretSet[field.name] ? '已设置；留空保留原值' : ''}
          value={String(value ?? '')} onChange={e => onChange(field.name, e.target.value)} />
        : field.type === 'integer' || field.type === 'number' ? <Input id={id} type="number" min={field.minimum} max={field.maximum}
          step={field.type === 'integer' ? 1 : 'any'} value={typeof value === 'number' ? value : ''}
          onChange={e => onChange(field.name, e.target.value === '' ? null : Number(e.target.value))} />
        : <ErrorText>暂不支持该字段类型：{field.type}</ErrorText>}
      {field.description ? <p className="text-xs leading-relaxed text-subtle-foreground">{field.description}</p> : null}
    </Field>
  })}</div>
}
