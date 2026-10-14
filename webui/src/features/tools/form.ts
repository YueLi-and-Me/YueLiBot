/** 插件 schema 控件分类与内联 TOML 转换，不维护另一份插件字段声明。 */
import { parse } from 'smol-toml'

export interface SchemaField {
  name: string
  label: string
  description: string
  type: string
  secret: boolean
  enum?: unknown[]
  minimum?: number
  maximum?: number
}

export function schemaFields(schema: Record<string, unknown>): SchemaField[] {
  const properties = schema.properties
  if (!properties || typeof properties !== 'object' || Array.isArray(properties)) return []
  return Object.entries(properties).map(([name, raw]) => {
    const entry = raw as Record<string, unknown>
    return {
      name, label: name === 'enabled' ? '启用插件' : typeof entry.title === 'string' ? entry.title : name,
      description: typeof entry.description === 'string' ? entry.description : '',
      type: typeof entry.type === 'string' ? entry.type : 'unsupported', secret: entry.secret === true,
      ...(Array.isArray(entry.enum) ? { enum: entry.enum } : {}),
      ...(typeof entry.minimum === 'number' ? { minimum: entry.minimum } : {}),
      ...(typeof entry.maximum === 'number' ? { maximum: entry.maximum } : {}),
    }
  })
}

export function inlineToml(value: unknown): string {
  if (typeof value === 'string') return JSON.stringify(value)
  if (typeof value === 'boolean' || typeof value === 'number') return String(value)
  if (Array.isArray(value)) return `[${value.map(inlineToml).join(', ')}]`
  if (value && typeof value === 'object') {
    return `{ ${Object.entries(value).map(([key, item]) => `${JSON.stringify(key)} = ${inlineToml(item)}`).join(', ')} }`
  }
  throw new Error('该值不能表示为 TOML 内联值')
}

export function parseInlineToml(text: string, type: 'object' | 'array'): unknown {
  const parsed = parse(`value = ${text}`)
  if (Object.keys(parsed).length !== 1) throw new Error('只能填写一个 TOML 内联值')
  const value = parsed.value
  if (type === 'array' ? !Array.isArray(value) : !value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(type === 'array' ? '请输入 TOML 数组，例如 ["参数"]' : '请输入 TOML 内联表，例如 { query = "内容" }')
  }
  return value
}

export const SAVED_RESTART_HINT = '已保存配置，重启后端后生效'
