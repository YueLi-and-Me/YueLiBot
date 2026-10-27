/**
 * 插件市场工作台：左侧目录筛选，右侧版本选择与安装管理。
 * 复用布局组件与市场 hook，完整 README 只链接到独立商店；
 * 商店的一键安装链接 `/plugins?install=<id>&version=<版本>` 在这里转成确认框。
 */
import { ExternalLink, RefreshCw, RotateCcw, Search, Store } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { PageFrame } from '@/components/layout/PageFrame'
import { Pane, PaneEmpty, PaneFooter, PaneHeader, PaneTitle, Panes, ScrollArea } from '@/components/layout/panes'
import { Button, Chip, ConfirmDialog, ErrorText, Input, Loading, SectionHeading, cn } from '@/components/ui'
import { usePluginMarket } from '@/hooks/use-plugin-market'
import { useRestart } from '@/hooks/use-restart'
import { MarketActions } from './MarketActions'
import type { MarketAction } from './types'
import { filterPlugins, installable, readInstallIntent, stripInstallParams } from './view'

/**
 * 复用工具页的面板边缘与标题高度，目录状态由后端统一决定。
 * @returns 包含搜索、版本表和操作确认的市场工作台。
 * @remarks 首次挂载读取目录，只有显式操作才修改插件目录；一键安装链接只选中插件并弹确认框。
 */
export function PluginMarketPage() {
  const state = usePluginMarket()
  const [search, setSearch] = useState('')
  const [tag, setTag] = useState('')
  const [selected, setSelected] = useState('')
  const [version, setVersion] = useState('')
  const [restartOpen, setRestartOpen] = useState(false)
  const [requested, setRequested] = useState<{ id: string; action: MarketAction } | null>(null)
  const [linkError, setLinkError] = useState('')
  const { restarting, restartBackend } = useRestart()
  const location = useLocation()
  const navigate = useNavigate()
  const snapshot = state.snapshot
  const clearRequested = useCallback(() => setRequested(null), [])
  useEffect(() => {
    if (!snapshot) return
    const intent = readInstallIntent(location.search, snapshot.plugins)
    if (intent.kind === 'none') return
    // 参数一经读取就从地址栏去掉，刷新页面不会再次弹框；能否处理都一样。
    navigate({ pathname: location.pathname, search: stripInstallParams(location.search) }, { replace: true })
    if (intent.kind === 'error') { setLinkError(intent.reason); return }
    // 清空筛选，保证目标插件在列表里，确认框作用于它而不是筛选后的第一项。
    setLinkError(''); setSearch(''); setTag(''); setSelected(intent.pluginId); setVersion(intent.version)
    setRequested({ id: intent.pluginId, action: intent.action })
  }, [snapshot, location.search, location.pathname, navigate])
  const plugins = filterPlugins(snapshot?.plugins ?? [], search, tag)
  const plugin = plugins.find(item => item.id === selected) ?? plugins[0]
  const selectedVersion = plugin?.versions.find(item => item.version === version) ?? plugin?.versions.find(installable) ?? plugin?.versions[0]
  const tags = [...new Set(snapshot?.plugins.flatMap(item => item.tags) ?? [])]
  return <PageFrame hint="浏览插件目录并选择兼容版本，安装、升级与卸载后重启生效。" actions={<>
    {snapshot ? <div className="max-w-72 text-[11px] leading-relaxed text-subtle-foreground">
      <p className="truncate" title={snapshot.sources.map(source => source.name).join('、')}>索引源：{snapshot.sources.map(source => source.name).join('、') || '未配置'} · 自动安装依赖：{snapshot.auto_install ? '开' : '关'}</p>
      <p>在 config/plugin_market.toml 修改</p>
    </div> : null}
    <Button size="sm" variant="ghost" disabled={state.busy} aria-label="刷新插件索引" onClick={() => void state.load(true)}><RefreshCw className="size-4" /></Button>
    <Button size="sm" variant="secondary" disabled={restarting} onClick={() => setRestartOpen(true)}><RotateCcw className="size-4" />重启月璃</Button>
  </>}>
    {state.error ? <ErrorText>{state.error}</ErrorText> : null}
    {linkError ? <p role="alert" className="text-[13px] text-warning">商店的安装链接没有执行：{linkError}</p> : null}
    {!snapshot ? <Loading>正在读取插件索引…</Loading> : <>
      <Panes>
        <Pane width="list" aria-label="插件市场列表">
          <PaneHeader><PaneTitle count={plugins.length}>发现插件</PaneTitle></PaneHeader>
          <ScrollArea>
            <div className="space-y-4 border-b border-border px-6 py-5">
              <div className="relative"><Search className="pointer-events-none absolute top-2.5 left-3 size-4 text-muted-foreground" /><Input aria-label="搜索插件" placeholder="搜索名称、说明或 ID" className="pl-9" value={search} onChange={event => setSearch(event.target.value)} /></div>
              <div className="flex flex-wrap gap-1.5">{['', ...tags].map(value => <button type="button" key={value} onClick={() => setTag(value)} aria-pressed={tag === value}
                className={cn('cursor-pointer rounded px-2.5 py-1 text-xs transition-colors', tag === value ? 'bg-primary/10 font-medium text-primary' : 'bg-muted text-muted-foreground hover:text-foreground')}>{value || '全部'}</button>)}</div>
            </div>
            {plugins.map(item => {
              const latest = item.versions.find(installable)
              return <button type="button" key={item.id} onClick={() => { setSelected(item.id); setVersion('') }}
                className={cn('w-full cursor-pointer space-y-2 border-b border-border px-6 py-4 text-left hover:bg-muted/50', item.id === plugin?.id && 'bg-primary/8')}>
                <span className="flex items-center justify-between gap-2 text-[13px] font-semibold"><span>{item.name}</span>{item.installed.kind !== '未装' ? <span className="shrink-0 text-[10px] font-normal text-primary">已安装</span> : null}</span>
                <span className="line-clamp-2 block text-xs leading-relaxed text-muted-foreground">{item.description}</span>
                <span className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-subtle-foreground"><span>{item.author}</span><span>{latest ? `v${latest.version}` : '暂无兼容版本'}</span>{item.downloads_count !== null ? <span>{item.downloads_count.toLocaleString()} 次下载</span> : null}</span>
              </button>
            })}
            {plugins.length === 0 ? <PaneEmpty icon={<Store />}>没有符合条件的插件</PaneEmpty> : null}
            <div className="space-y-3 px-6 py-5 text-xs text-muted-foreground">
              {snapshot.errors.map(item => <p key={item.index_url} className="break-words text-warning">{item.source}：{item.reason}</p>)}
              {snapshot.conflicts.map(item => <p key={`${item.id}:${item.ignored}`} className="break-words text-warning">{item.id} 存在来源冲突，采用「{item.kept}」，忽略「{item.ignored}」。</p>)}
            </div>
          </ScrollArea>
        </Pane>
        <Pane aria-label="插件市场详情">
          <PaneHeader><PaneTitle>{plugin?.name ?? '选择一个插件'}</PaneTitle></PaneHeader>
          <ScrollArea>
            {plugin ? <div className="space-y-7 p-6">
              <section className="space-y-4"><SectionHeading tint="coral" title="插件信息" />
                <p className="text-[13px] leading-relaxed text-muted-foreground">{plugin.description}</p>
                <div className="grid gap-x-8 gap-y-2 text-[13px] sm:grid-cols-2">
                  <span>ID：<span className="break-all font-mono">{plugin.id}</span></span><span>作者：{plugin.author}</span>
                  <span>许可证：{plugin.license}</span><span>来源：{plugin.source.name}</span>
                </div>
                <div className="flex flex-wrap gap-4 text-[13px] text-primary">
                  {/^https?:\/\//.test(plugin.repository) ? <a href={plugin.repository} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 hover:underline">作者仓库<ExternalLink className="size-3.5" /></a> : <span>仓库：{plugin.repository}</span>}
                  {plugin.store_url ? <a href={plugin.store_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 hover:underline">在商店页查看完整说明<ExternalLink className="size-3.5" /></a> : null}
                </div>
                <div className="flex flex-wrap gap-2"><Chip label="安装状态" value={plugin.installed.kind} />{plugin.installed.version ? <Chip label="当前版本" value={plugin.installed.version} /> : null}</div>
                {plugin.installed.upgrade_to ? <p className="text-[13px] text-primary">可升级到 {plugin.installed.upgrade_to}</p> : null}
                {plugin.versions.some(item => item.version === plugin.installed.version && item.yanked) ? <p className="text-[13px] text-warning">当前已安装版本已撤回，请检查作者说明并选择其他版本。</p> : null}
              </section>
              <section className="space-y-4 border-t border-border pt-5"><SectionHeading tint="olive" title="选择版本" subtitle="支持区间与当前月璃版本共同决定能否安装" />
                <div className="overflow-x-auto"><table className="w-full text-left text-[13px]"><thead className="text-xs text-subtle-foreground"><tr className="border-b border-border"><th className="py-2 pr-4 font-medium">版本</th><th className="py-2 pr-4 font-medium">主程序版本</th><th className="py-2 pr-4 font-medium">兼容判定</th><th className="py-2 font-medium">发布日期</th></tr></thead>
                  <tbody>{plugin.versions.map(item => <tr key={item.version} className={cn('border-b border-border', selectedVersion?.version === item.version && 'bg-primary/5')}>
                    <td className="whitespace-nowrap py-3 pr-4"><label className="flex cursor-pointer items-center gap-2"><input type="radio" name="plugin-version" checked={selectedVersion?.version === item.version} onChange={() => setVersion(item.version)} />{item.version}</label></td>
                    <td className="whitespace-nowrap py-3 pr-4 font-mono text-xs">{item.host_application.min_version} ～ {item.host_application.max_version}</td>
                    <td className="whitespace-nowrap py-3 pr-4 text-xs">{item.yanked ? '已撤回' : item.decision}</td><td className="whitespace-nowrap py-3 text-xs text-muted-foreground">{item.published_at.slice(0, 10)}</td>
                  </tr>)}</tbody></table></div>
                {selectedVersion?.reason ? <p className="text-[13px] text-warning">{selectedVersion.reason}</p> : null}
              </section>
              <section className="space-y-3 border-t border-border pt-5"><SectionHeading tint="plum" title="Python 依赖" />
                {selectedVersion?.python_dependencies.length ? <ul className="space-y-1 text-[13px]">{selectedVersion.python_dependencies.map(value => <li key={value} className="break-all font-mono">{value}</li>)}</ul> : <p className="text-[13px] text-muted-foreground">这个版本没有声明额外的 Python 库。</p>}
                <p className="text-xs leading-relaxed text-subtle-foreground">{snapshot.auto_install ? '安装时会检查并补齐缺失的库，已安装库的版本保持不变。' : '自动安装已关闭。缺少依赖时会提供手动命令，补齐并重启后插件才能加载。'}</p>
              </section>
              <section className="border-t border-border pt-5"><MarketActions key={plugin.id} plugin={plugin} version={selectedVersion} state={state}
                requested={requested?.id === plugin.id ? requested.action : null} onRequestHandled={clearRequested} /></section>
            </div> : <PaneEmpty>从左侧选择插件，查看说明与可用版本。</PaneEmpty>}
          </ScrollArea>
        </Pane>
      </Panes>
      <PaneFooter><span className="text-[13px] text-subtle-foreground">安装、升级与卸载后重启生效；不会自动重启或升级。</span></PaneFooter>
    </>}
    <ConfirmDialog open={restartOpen} title="重启月璃" description="重启期间她会暂时无法回复，插件变更将在重启后生效。" confirmText="重启"
      onCancel={() => setRestartOpen(false)} onConfirm={() => { setRestartOpen(false); void restartBackend() }} />
  </PageFrame>
}
