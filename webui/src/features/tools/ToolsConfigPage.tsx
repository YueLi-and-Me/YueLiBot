/** 工具插件与 MCP 服务共用模型页的列表、详情和底部保存条组件。 */
import { Plus, RefreshCw, RotateCcw, Save } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router'

import { PageFrame } from '@/components/layout/PageFrame'
import { Pane, PaneEmpty, PaneFooter, PaneHeader, PaneTitle, Panes, ScrollArea } from '@/components/layout/panes'
import { Button, Chip, ConfirmDialog, ErrorText, Loading, SectionHeading, Toggle, cn, toast } from '@/components/ui'
import { hasDraftChanges, useRestart } from '@/hooks/use-restart'
import { useToolsConfig, type ToolsSnapshot } from '@/hooks/use-tools-config'
import { usePluginMarket } from '@/hooks/use-plugin-market'
import { DependencyNote, MarketActions } from '@/features/plugin-market/MarketActions'
import { installable } from '@/features/plugin-market/view'

import { McpDetail } from './McpDetail'
import { SchemaForm } from './SchemaForm'
import type { McpServerSettings, McpTestResponse } from './types'

function newServer(name: string): McpServerSettings {
  return { name, original_name: '', enabled: true, transport: 'http', url: '', api_key: '', api_key_provider: '',
    apiKeySet: false, clear_api_key: false, headers: {}, headerSet: {}, use_proxy: false, command: '', args: [], env: {}, envSet: {},
    cwd: '', call_timeout_seconds: 15, tools: {} }
}

export function ToolsConfigPage() {
  const state = useToolsConfig()
  const market = usePluginMarket()
  const [draft, setDraft] = useState<ToolsSnapshot | null>(null)
  const [selected, setSelected] = useState<{ kind: 'plugin' | 'mcp'; key: string } | null>(null)
  const [errors, setErrors] = useState<Record<string, boolean>>({})
  const [tested, setTested] = useState<Record<string, McpTestResponse>>({})
  const [restartOpen, setRestartOpen] = useState(false)
  const [removeOpen, setRemoveOpen] = useState(false)
  const [saved, setSaved] = useState(false)
  const { restarting, restartBackend } = useRestart()
  useEffect(() => {
    if (state.snapshot) {
      setDraft(structuredClone(state.snapshot))
      setSelected(current => current ?? (state.snapshot?.plugins.plugins[0]
        ? { kind: 'plugin', key: state.snapshot.plugins.plugins[0].directory }
        : state.snapshot?.mcp.servers[0] ? { kind: 'mcp', key: '0' } : null))
    }
  }, [state.snapshot])
  const changed = hasDraftChanges(draft, state.snapshot)
  const invalid = Object.values(errors).some(Boolean)
  const choose = (next: NonNullable<typeof selected>) => {
    if (invalid) { toast.error('请先修正当前表单中的内联 TOML 错误'); return }
    setSelected(next)
  }
  const markError = (name: string, bad: boolean) => setErrors(current => current[name] === bad ? current : { ...current, [name]: bad })
  const plugin = selected?.kind === 'plugin' ? draft?.plugins.plugins.find(item => item.directory === selected.key) : undefined
  const serverIndex = selected?.kind === 'mcp' ? Number(selected.key) : -1
  const server = draft?.mcp.servers[serverIndex]
  const updateServer = (value: McpServerSettings) => {
    setDraft(current => current ? { ...current, mcp: { ...current.mcp, servers: current.mcp.servers.map((item, index) => index === serverIndex ? value : item) } } : current)
    // 选择按草稿位置保持稳定，改名不能让表单重挂载而丢失输入焦点。
  }
  const detailTitle = plugin?.name ?? server?.name ?? '请选择插件或 MCP 服务'
  const marketPlugin = market.snapshot?.plugins.find(item => item.id === plugin?.id)
  const updates = market.snapshot?.plugins.filter(item => item.installed.upgrade_to).length ?? 0
  return <PageFrame hint="查看工具插件与 MCP 服务，配置保存后重启后端生效。" actions={<>
    {updates ? <Link to="/plugins" className="text-xs text-primary hover:underline">有 {updates} 个插件可升级</Link> : null}
    {draft ? <Toggle checked={draft.mcp.enabled} label="启用 MCP" onChange={checked => setDraft({ ...draft, mcp: { ...draft.mcp, enabled: checked } })} /> : null}
    <Button size="sm" variant="ghost" disabled={state.busy || changed || invalid} onClick={() => void state.load()} aria-label="刷新工具状态"><RefreshCw className="size-4" /></Button>
    <Button size="sm" variant="secondary" disabled={restarting} onClick={() => setRestartOpen(true)}><RotateCcw className="size-4" />重启月璃</Button>
  </>}>
    {state.error ? <ErrorText>{state.error}</ErrorText> : null}
    {!draft ? <Loading>正在读取工具配置…</Loading> : <>
      <Panes>
        <Pane width="list" aria-label="插件与 MCP 服务列表">
          <PaneHeader><PaneTitle count={draft.plugins.plugins.length + draft.mcp.servers.length}>插件与服务</PaneTitle></PaneHeader>
          <ScrollArea>
            <p className="px-6 pt-5 pb-2 text-[11px] font-semibold text-subtle-foreground">插件</p>
            {draft.plugins.plugins.map(item => <button key={item.directory} type="button" onClick={() => choose({ kind: 'plugin', key: item.directory })}
              className={cn('flex w-full cursor-pointer items-start gap-2 border-b border-border px-6 py-3 text-left hover:bg-muted/50', plugin?.directory === item.directory && 'bg-primary/8')}>
              <span className={cn('mt-1.5 size-1.5 shrink-0 rounded-full', item.decision === '正常' ? 'bg-tint-olive' : item.decision.includes('兼容') ? 'bg-tint-amber' : 'bg-muted-foreground')} />
              <span className="min-w-0"><span className="block truncate text-[13px] font-medium">{item.name} <span className="text-xs text-subtle-foreground">{item.version}</span></span>
                <span className="text-xs text-muted-foreground">{item.decision}</span></span>
            </button>)}
            {draft.plugins.plugins.length === 0 ? <p className="px-6 py-3 text-xs text-muted-foreground">尚未发现工具插件</p> : null}
            <p className="px-6 pt-5 pb-2 text-[11px] font-semibold text-subtle-foreground">MCP 服务</p>
            {draft.mcp.servers.map((item, index) => {
              const status = state.status.servers.find(runtime => runtime.name === (item.original_name || item.name))?.state ?? '未启动'
              return <button key={item.original_name || index} type="button" onClick={() => choose({ kind: 'mcp', key: String(index) })}
                className={cn('flex w-full cursor-pointer items-start gap-2 border-b border-border px-6 py-3 text-left hover:bg-muted/50', serverIndex === index && 'bg-primary/8')}>
                <span className={cn('mt-1.5 size-1.5 shrink-0 rounded-full', status === '已连接' ? 'bg-tint-olive' : status === '失败' ? 'bg-destructive' : 'bg-muted-foreground')} />
                <span><span className="block text-[13px] font-medium">{item.name || '未命名服务'}</span><span className="text-xs text-muted-foreground">{item.transport === 'http' ? 'HTTP' : '本机程序'} · {status}</span></span>
              </button>
            })}
            <div className="px-6 py-3"><Button variant="ghost" size="sm" disabled={invalid} onClick={() => {
              let index = 1
              while (draft.mcp.servers.some(item => item.name === `server_${index}`)) index++
              const next = newServer(`server_${index}`)
              setDraft({ ...draft, mcp: { ...draft.mcp, servers: [...draft.mcp.servers, next] } }); setSelected({ kind: 'mcp', key: String(draft.mcp.servers.length) })
            }}><Plus className="size-3.5" />新增服务</Button></div>
          </ScrollArea>
        </Pane>
        <Pane aria-label="工具配置详情">
          <PaneHeader><PaneTitle>{detailTitle}</PaneTitle></PaneHeader>
          <ScrollArea>
            {plugin ? <div className="space-y-7 p-6">
              <section className="space-y-3"><SectionHeading tint="coral" title="插件信息" />
                <p className="text-[13px] text-muted-foreground">{plugin.description}</p>
                <div className="grid gap-x-8 gap-y-2 text-[13px] sm:grid-cols-2">
                  <span>ID：<span className="font-mono">{plugin.id || '清单未解析'}</span></span><span>版本：{plugin.version || '—'}</span>
                  <span>作者：{plugin.author || '未声明'}</span><span>来源：{plugin.source}</span>
                  <span className="sm:col-span-2">支持主程序：{plugin.min_version || '未声明'} ～ {plugin.max_version || '未声明'}</span>
                </div><div className="flex flex-wrap gap-2"><Chip label="判定" value={plugin.decision} />{plugin.source === '第三方' ? <Chip label="安装方式" value={plugin.managed ? '市场安装' : '手动安装'} /> : null}</div>
                {plugin.reason ? <DependencyNote note={plugin.reason} /> : null}
                {market.error ? <ErrorText>{market.error}</ErrorText> : null}
                {plugin.source === '第三方' && (marketPlugin || plugin.managed) ? <MarketActions key={plugin.id} plugin={marketPlugin ?? {
                  id: plugin.id, installed: { kind: '市场安装', version: plugin.installed_version, upgrade_to: null, market_id_available: false },
                }} version={marketPlugin?.versions.find(installable)} state={market} unsaved={changed} /> : null}
              </section>
              <section className="space-y-4 border-t border-border pt-5"><SectionHeading tint="olive" title="提供的工具" />
                {plugin.tools.map(tool => <div key={tool.name} className="space-y-1 text-[13px]">
                  <h4 className="font-mono font-semibold">{tool.name}</h4><p className="text-muted-foreground">{tool.description}</p>
                  <p className="text-xs text-subtle-foreground">所需能力：{tool.capabilities.join('、') || '无'}</p>
                  <p className="whitespace-pre-wrap text-xs leading-relaxed text-subtle-foreground">本轮可见判据（插件声明）：{tool.visibility || '未声明'}</p>
                </div>)}
                {plugin.tools.length === 0 ? <p className="text-[13px] text-muted-foreground">当前没有可读取的工具声明</p> : null}
              </section>
              <section className="space-y-4 border-t border-border pt-5"><SectionHeading tint="plum" title="插件配置" subtitle="密钥留空沿用原值" />
                {!plugin.configEditable ? <p className="text-[13px] text-warning">入口尚未导入，目前只可编辑启用开关；重启并通过版本校验后读取完整配置声明。</p> : null}
                {plugin.id ? <SchemaForm key={plugin.directory} schema={plugin.config_schema} values={plugin.values} secretSet={plugin.secretSet}
                  onError={(name, bad) => markError(`${plugin.directory}:${name}`, bad)} onChange={(name, value) => setDraft(current => current ? {
                    ...current, plugins: { plugins: current.plugins.plugins.map(item => item.directory === plugin.directory ? { ...item, values: { ...item.values, [name]: value } } : item) },
                  } : current)} /> : null}
              </section>
            </div> : server ? <McpDetail key={selected?.key} server={server} providers={draft.mcp.providers} busy={state.busy}
              runtime={state.status.servers.find(item => item.name === (server.original_name || server.name))} tested={tested[selected?.key ?? '']}
              onError={(name, bad) => markError(`mcp:${selected?.key}:${name}`, bad)} onChange={updateServer}
              onRemove={() => setRemoveOpen(true)} onTest={() => { if (invalid) return; const key = selected?.key ?? ''; void state.test(server).then(result => { if (result) setTested(current => ({ ...current, [key]: result })) }) }} />
              : <PaneEmpty>从左栏选择插件或 MCP 服务，或新增服务。</PaneEmpty>}
          </ScrollArea>
        </Pane>
      </Panes>
      <PaneFooter className={cn(changed && 'bg-warning-soft')}>
        <span role="status" className={cn('text-[13px]', changed ? 'text-warning' : 'text-subtle-foreground')}>
          {invalid ? '内联 TOML 有错误，请修正后保存' : changed ? '有未保存的修改，保存后重启后端生效' : saved ? '已保存配置，重启后端后生效' : '与已保存的配置一致'}
        </span><Button className="ml-auto" disabled={state.busy || invalid} onClick={() => void state.save(draft).then(ok => { if (ok) setSaved(true) })}><Save className="size-4" />保存配置</Button>
      </PaneFooter>
    </>}
    <ConfirmDialog open={restartOpen} title="重启月璃" description={changed ? '重启期间她会暂时无法回复。当前有未保存的修改，重启会丢弃这些修改。' : '重启期间她会暂时无法回复，已保存的配置将在重启后生效。'}
      confirmText="重启" onCancel={() => setRestartOpen(false)} onConfirm={() => { setRestartOpen(false); void restartBackend() }} />
    <ConfirmDialog open={removeOpen} title="移除 MCP 服务" description="保存并重启后该服务的工具将不可用。" confirmText="移除"
      onCancel={() => setRemoveOpen(false)} onConfirm={() => {
        setRemoveOpen(false); setErrors({}); setSelected(null)
        setDraft(current => current ? { ...current, mcp: { ...current.mcp, servers: current.mcp.servers.filter((_item, index) => index !== serverIndex) } } : current)
      }} />
  </PageFrame>
}
