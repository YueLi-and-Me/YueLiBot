/** 服务连接、发现结果与显式白名单编辑，测试结果不会自动开放工具。 */
import { useState } from 'react'

import { Button, Checkbox, Chip, ErrorText, Field, Input, SectionHeading, Select, Textarea, Toggle } from '@/components/ui'

import { InlineField } from './SchemaForm'
import type { McpServerSettings, McpServerStatus, McpTestResponse, McpToolSettings } from './types'

export const DEFAULT_TOOL: McpToolSettings = { expose: true, readonly: false, alias: '', description: '', daily_limit: 50 }

export function McpDetail({ server, runtime, providers, tested, busy, onChange, onTest, onRemove, onError }: {
  server: McpServerSettings; runtime?: McpServerStatus; providers: string[]; tested?: McpTestResponse
  busy: boolean; onChange: (value: McpServerSettings) => void; onTest: () => void
  onRemove: () => void; onError: (name: string, invalid: boolean) => void
}) {
  const [toolName, setToolName] = useState('')
  const update = <K extends keyof McpServerSettings>(key: K, value: McpServerSettings[K]) => onChange({ ...server, [key]: value })
  const discovered = tested?.ok ? tested.tools : runtime?.tools ?? []
  const names = [...new Set([...discovered.map(tool => tool.name), ...Object.keys(server.tools)])]
  const setTool = (name: string, setting: McpToolSettings | null) => {
    const tools = { ...server.tools }
    if (setting) tools[name] = setting
    else delete tools[name]
    update('tools', tools)
  }
  return <div className="space-y-7 p-6">
    <section className="space-y-4">
      <SectionHeading tint="coral" title="连接设置" subtitle="保存后重启后端生效" />
      <div className="grid gap-5 sm:grid-cols-2">
        <Field label="服务名称" htmlFor="mcp-name"><Input id="mcp-name" value={server.name} maxLength={20}
          onChange={e => update('name', e.target.value)} /><p className="text-xs text-subtle-foreground">1～20 个小写字母、数字或下划线</p></Field>
        <Field label="接法" htmlFor="mcp-transport"><Select id="mcp-transport" value={server.transport}
          onChange={e => update('transport', e.target.value as 'http' | 'stdio')}><option value="http">HTTP</option><option value="stdio">本机程序</option></Select></Field>
        <Toggle checked={server.enabled} label="启用此服务" onChange={value => update('enabled', value)} />
        <Field label="调用超时（秒）" htmlFor="mcp-timeout"><Input id="mcp-timeout" type="number" min={1} max={120}
          value={server.call_timeout_seconds} onChange={e => update('call_timeout_seconds', Number(e.target.value))} /></Field>
      </div>
      {server.transport === 'http' ? <div className="grid gap-5 sm:grid-cols-2">
        <Field label="服务地址" htmlFor="mcp-url" className="sm:col-span-2"><Input id="mcp-url" value={server.url} onChange={e => update('url', e.target.value)} placeholder="https://服务地址/mcp" /></Field>
        <Field label="密钥" htmlFor="mcp-api-key"><Input id="mcp-api-key" type="password" autoComplete="off" value={server.api_key}
          placeholder={server.apiKeySet && !server.clear_api_key ? '已设置；留空保留原值' : '直接填写密钥'}
          onChange={e => onChange({ ...server, api_key: e.target.value, clear_api_key: false })} /></Field>
        <Field label="引用厂商密钥" htmlFor="mcp-provider"><Select id="mcp-provider" value={server.api_key_provider}
          onChange={e => onChange({ ...server, api_key_provider: e.target.value, api_key: '', clear_api_key: !!e.target.value })}>
          <option value="">不引用厂商</option>{providers.map(name => <option key={name}>{name}</option>)}
        </Select><p className="text-xs text-subtle-foreground">直接密钥与厂商引用只能选一种</p></Field>
        <InlineField id="mcp-headers" label="请求头（TOML 内联表）" type="object" value={server.headers}
          onChange={value => update('headers', value as Record<string, string>)} onError={bad => onError('headers', bad)}
          hint={`可用 {api_key} 引用密钥。已设置的非模板请求头：${Object.keys(server.headerSet).filter(key => server.headerSet[key]).join('、') || '无'}；留空保留。`} />
        <Toggle checked={server.use_proxy} label="使用全局代理" onChange={value => update('use_proxy', value)} />
      </div> : <div className="grid gap-5 sm:grid-cols-2">
        <Field label="命令" htmlFor="mcp-command"><Input id="mcp-command" value={server.command} onChange={e => update('command', e.target.value)} /></Field>
        <Field label="工作目录" htmlFor="mcp-cwd"><Input id="mcp-cwd" value={server.cwd} onChange={e => update('cwd', e.target.value)} /></Field>
        <InlineField id="mcp-args" label="参数（TOML 数组）" type="array" value={server.args}
          onChange={value => update('args', value as string[])} onError={bad => onError('args', bad)} />
        <InlineField id="mcp-env" label="环境变量（TOML 内联表）" type="object" value={server.env}
          onChange={value => update('env', value as Record<string, string>)} onError={bad => onError('env', bad)}
          hint={`已设置：${Object.keys(server.envSet).filter(key => server.envSet[key]).join('、') || '无'}；值留空保留，删除键即移除。`} />
      </div>}
      <div className="flex gap-2"><Button variant="secondary" size="sm" disabled={busy} onClick={onTest}>测试连接</Button>
        <Button variant="ghost" size="sm" onClick={onRemove}>移除服务</Button></div>
    </section>
    <section className="space-y-3 border-t border-border pt-5">
      <SectionHeading tint="plum" title="运行状态" />
      <div className="flex flex-wrap gap-x-6 gap-y-2 text-[13px]">
        <span>状态：{runtime?.state ?? '未启动'}</span><span>协议：{runtime?.protocol_version || '尚未协商'}</span>
        <span>服务器：{runtime?.server_name || '—'} {runtime?.server_version}</span>
      </div>
      {runtime?.last_error ? <ErrorText>{runtime.last_error}</ErrorText> : null}
      {runtime?.changes.map((change, index) => <p key={index} className="text-[13px] text-warning">{change.tool}：{change.kind}
        {change.kind === '说明变化' ? `；原说明「${change.old_head}」，新说明「${change.new_head}」` : ''}</p>)}
      {tested ? <div role="status" className="rounded-md border border-border bg-muted/40 p-3 text-[13px]">
        {tested.ok ? `测试连接成功；协议 ${tested.protocol_version}，服务器 ${tested.server_name} ${tested.server_version}，发现 ${tested.tools.length} 个工具。未登记、未保存。`
          : <ErrorText>{tested.error}</ErrorText>}
      </div> : null}
    </section>
    <section className="space-y-4 border-t border-border pt-5">
      <SectionHeading tint="olive" title="工具清单" />
      <p className="text-xs leading-relaxed text-subtle-foreground">发现的工具不会自动开放。列入配置后，仍需服务器标注只读或由你确认只读。</p>
      {names.length === 0 ? <p className="text-[13px] text-muted-foreground">尚未发现工具，可先测试连接。</p> : null}
      {names.map(name => {
        const discoveredTool = discovered.find(tool => tool.name === name)
        const actual = runtime?.tools.find(tool => tool.name === name)
        const settings = server.tools[name]
        const set = (patch: Partial<McpToolSettings>) => { if (settings) setTool(name, { ...settings, ...patch }) }
        return <div key={name} className="space-y-3 border-b border-border pb-5 last:border-0">
          <div className="flex flex-wrap items-center gap-3"><h4 className="font-mono text-[13px] font-semibold">{name}</h4>
            <Chip label="标注" value={discoveredTool?.read_only === true ? '已标注只读' : discoveredTool?.read_only === false ? '非只读标注' : '未标注只读'} />
            <span className="ml-auto text-xs text-subtle-foreground">今天已用 {actual?.used_today ?? 0} 次</span></div>
          <details className="text-[13px] text-muted-foreground"><summary className="cursor-pointer">服务器原始说明</summary>
            <p className="mt-2 whitespace-pre-wrap leading-relaxed">{discoveredTool?.description || discoveredTool?.title || '尚未发现原始说明'}</p></details>
          <Checkbox checked={!!settings} onChange={checked => setTool(name, checked ? { ...DEFAULT_TOOL } : null)} label="列入配置" />
          {settings ? <>
            <div className="flex flex-wrap items-center gap-x-6 gap-y-2"><Toggle checked={settings.expose} label="开放给她" onChange={checked => set({ expose: checked })} />
              <Checkbox checked={settings.readonly} onChange={checked => set({ readonly: checked })} label="我确认只读" /></div>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="别名" htmlFor={`alias-${name}`}><Input id={`alias-${name}`} value={settings.alias} onChange={e => set({ alias: e.target.value })} /></Field>
              <Field label="每日上限" htmlFor={`limit-${name}`}><Input id={`limit-${name}`} type="number" min={1} max={1000} value={settings.daily_limit} onChange={e => set({ daily_limit: Number(e.target.value) })} /></Field>
              <Field className="sm:col-span-2" label="覆盖说明" htmlFor={`description-${name}`}><Textarea id={`description-${name}`} rows={2} value={settings.description} onChange={e => set({ description: e.target.value })} /></Field>
            </div>
          </> : null}
          <p className="text-xs text-subtle-foreground">当前开放判定：{actual?.reason ?? '未在运行清单中；保存并重启后重新判定'}</p>
        </div>
      })}
      <div className="flex items-center gap-2"><Input aria-label="手动添加工具名称" placeholder="原始工具名称" value={toolName} onChange={e => setToolName(e.target.value)} />
        <Button variant="secondary" size="sm" disabled={!toolName.trim() || !!server.tools[toolName.trim()]} onClick={() => { setTool(toolName.trim(), { ...DEFAULT_TOOL }); setToolName('') }}>添加工具</Button></div>
    </section>
  </div>
}
