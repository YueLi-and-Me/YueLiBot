/** 配置声明、内联值错误展示及保存后的重启提示；真实交互另在浏览器验收。 */
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { schemaFields, parseInlineToml, inlineToml, SAVED_RESTART_HINT } from '../webui/src/features/tools/form'
import { SchemaForm, TomlInput } from '../webui/src/features/tools/SchemaForm'
import { saveToolsSnapshot } from '../webui/src/features/tools/save'
import type { ToolsSnapshot } from '../webui/src/hooks/use-tools-config'

describe('工具配置表单', () => {
  it('按 schema 生成开关、数值约束、枚举、密钥与容器控件', () => {
    const schema = { type: 'object', properties: {
      enabled: { type: 'boolean', description: '启用说明' },
      limit: { type: 'integer', minimum: 1, maximum: 3 },
      mode: { type: 'string', enum: ['http', 'mcp'] },
      secret: { type: 'string', secret: true },
      headers: { type: 'object' }, args: { type: 'array' },
    } }
    expect(schemaFields(schema).map(field => field.type)).toEqual(['boolean', 'integer', 'string', 'string', 'object', 'array'])
    const html = renderToStaticMarkup(createElement(SchemaForm, {
      schema, values: { enabled: true, limit: 2, mode: 'http', secret: '', headers: {}, args: [] }, secretSet: { secret: true }, onChange: vi.fn(), onError: vi.fn(),
    }))
    expect(html).toContain('role="switch"')
    expect(html).toContain('min="1" max="3"')
    expect(html).toContain('<select')
    expect(html).toContain('type="password"')
    expect(html).toContain('已设置；留空保留原值')
    expect(html).toContain('启用说明')
  })
  it('object 非法 TOML 产生红色错误且 aria-invalid 为真', () => {
    let error = ''
    try { parseInlineToml('{ query = ', 'object') } catch (cause) { error = String(cause) }
    expect(error).not.toBe('')
    const html = renderToStaticMarkup(createElement(TomlInput, { id: 'headers', text: '{ query = ', error, onChange: vi.fn() }))
    expect(html).toContain('aria-invalid="true"')
    expect(html).toContain('border-destructive')
    expect(html).toContain('text-destructive')
    const value = { text: '引号"与换行\n', args: ['a', 'b'] }
    expect(parseInlineToml(inlineToml(value), 'object')).toEqual(value)
    expect(() => parseInlineToml('[]', 'object')).toThrow('内联表')
    expect(() => parseInlineToml('{}\nother=1', 'object')).toThrow('一个')
  })
  it('成功保存变更后返回重启提示，失败不会返回成功提示', async () => {
    const original: ToolsSnapshot = { plugins: { plugins: [] }, mcp: { enabled: true, servers: [], providers: [] } }
    const draft = structuredClone(original)
    draft.mcp.enabled = false
    const write = vi.fn(async () => ({ ok: true }))
    expect(await saveToolsSnapshot(original, draft, write)).toBe(SAVED_RESTART_HINT)
    expect(write).toHaveBeenCalledWith('/tools/mcp/config', draft.mcp)
    await expect(saveToolsSnapshot(original, draft, async () => { throw new Error('保存失败') })).rejects.toThrow('保存失败')
  })
})
