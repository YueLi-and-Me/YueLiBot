/**
 * 市场页和工具页共享的操作确认与依赖说明。
 * 写入交给 usePluginMarket，重启交给 useRestart，后端失败原因原样展示。
 */
import { Copy, Download, RotateCcw, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { Button, ConfirmDialog, toast } from '@/components/ui'
import type { usePluginMarket } from '@/hooks/use-plugin-market'
import { useRestart } from '@/hooks/use-restart'
import type { MarketAction, MarketPlugin, MarketVersion } from './types'
import { installable, manualCommand } from './view'

/** 安装与接管时逐字展示的同进程权限提示。 */
const INSTALL_WARNING = '第三方插件与月璃运行在同一进程，能读取她的全部配置（包括模型密钥）与数据库。收录经过审核，但审核不能保证没有问题。确定安装吗？'

/**
 * 展示依赖原文和可复制命令，复制失败明确反馈。
 * @param note 后端生成的依赖说明，保留换行与命令转义。
 * @returns 说明区及可选的剪贴板操作按钮。
 */
export function DependencyNote({ note }: { note: string }) {
  const command = manualCommand(note)
  return <div className="space-y-2"><p className="whitespace-pre-wrap break-words text-[13px] leading-relaxed text-warning">{note}</p>
    {command ? <Button size="sm" variant="secondary" onClick={() => void navigator.clipboard.writeText(command).then(() => toast.success('已复制手动安装命令'), () => toast.error('复制失败，请手动选择命令'))}><Copy className="size-3.5" />复制手动命令</Button> : null}</div>
}

/**
 * 根据安装所有权和目标版本提供操作；成功后由用户点击重启并再次确认。
 * @param plugin 插件身份与安装状态；索引不可用时允许仅凭收据卸载。
 * @param version 目标版本，缺省时禁止下载类操作。
 * @param state 所属页面的市场请求状态。
 * @param unsaved 工具页是否有未保存草稿，默认 false；用于重启提示。
 * @returns 操作按钮、确认框和仅属于当前插件的结果。
 */
export function MarketActions({ plugin, version, state, unsaved = false }: {
  plugin: Pick<MarketPlugin, 'id' | 'installed'> & Partial<Pick<MarketPlugin, 'source'>>
  version?: MarketVersion
  state: ReturnType<typeof usePluginMarket>
  unsaved?: boolean
}) {
  const [confirm, setConfirm] = useState<MarketAction | null>(null)
  const [restartOpen, setRestartOpen] = useState(false)
  const { restarting, restartBackend } = useRestart()
  const kind = plugin.installed.kind
  const available = version !== undefined && installable(version)
  const label = confirm === 'uninstall' ? '卸载插件' : confirm === 'takeover' ? '交给市场管理' : confirm === 'upgrade' ? '升级插件' : '安装插件'
  return <div className="space-y-3">
    <div className="flex flex-wrap gap-2">
      {kind === '未装' ? <Button disabled={state.busy || !available || !plugin.source} onClick={() => setConfirm('install')}><Download className="size-4" />安装所选版本</Button> : null}
      {kind === '市场安装' ? <><Button disabled={state.busy || !available || version?.version === plugin.installed.version} onClick={() => setConfirm('upgrade')}>升级到 {version?.version ?? '所选版本'}</Button>
        <Button variant="secondary" disabled={state.busy} onClick={() => setConfirm('uninstall')}><Trash2 className="size-4" />卸载</Button></> : null}
      {kind === '手动安装' && plugin.installed.market_id_available ? <Button disabled={state.busy || !available} onClick={() => setConfirm('takeover')}>交给市场管理</Button> : null}
    </div>
    {state.result && state.resultId === plugin.id ? <div role="status" className="space-y-3 border-l-2 border-primary pl-4">
      <p className="text-[13px]">{state.result.detail}</p>
      {state.result.dependency_note ? <DependencyNote note={state.result.dependency_note} /> : null}
      {state.result.restart_required ? <Button size="sm" variant="secondary" disabled={restarting} onClick={() => setRestartOpen(true)}><RotateCcw className="size-3.5" />重启月璃</Button> : null}
    </div> : null}
    <ConfirmDialog open={confirm !== null} title={label} confirmText={label}
      description={confirm === 'uninstall' ? `插件目录（含 config.toml）将移到 ${state.snapshot?.backup_root}/<id>-<旧版本>-<YYYYmmdd-HHMMSS>/，不会直接删除；每个插件保留最近 3 份备份。插件写在月璃数据目录里的数据不受影响。` : INSTALL_WARNING}
      onCancel={() => setConfirm(null)} onConfirm={() => {
        if (confirm === null) return
        const action = confirm; setConfirm(null)
        void state.act(action, { id: plugin.id, ...(action === 'uninstall' ? {} : { version: version?.version }), ...(action === 'install' ? { source: plugin.source?.name } : {}) })
      }} />
    <ConfirmDialog open={restartOpen} title="重启月璃" description={unsaved ? '重启期间她会暂时无法回复。当前有未保存的修改，重启会丢弃这些修改。' : '重启期间她会暂时无法回复，插件变更将在重启后生效。'} confirmText="重启"
      onCancel={() => setRestartOpen(false)} onConfirm={() => { setRestartOpen(false); void restartBackend() }} />
  </div>
}
