/**
 * 开发者命令通道只读页：展示开关、统一鉴权边界与已注册命令。
 *
 * 套用表格页模板：工具栏给出通道状态与鉴权边界，下方是满宽的命令表。
 */
import { RefreshCw } from 'lucide-react'

import { PageFrame } from '@/components/layout/PageFrame'
import { ScrollArea, Toolbar } from '@/components/layout/panes'
import { Button, Chip, Empty, ErrorText, Loading } from '@/components/ui'
import { useDeveloperCommands } from '@/hooks/use-developer-commands'

/** 渲染开发者命令通道的只读目录。 */
export function DeveloperCommandsPage() {
  const { snapshot, error, loading, reload } = useDeveloperCommands()

  return (
    <PageFrame
      hint="只读查看聊天内命令通道及注册目录；本页不能开启通道、注册命令或执行命令。"
      actions={
        <Button variant="secondary" onClick={() => void reload()} disabled={loading}>
          <RefreshCw className="size-4" aria-hidden="true" />
          刷新
        </Button>
      }
    >
      {snapshot ? (
        <Toolbar className="gap-y-1.5">
          <Chip label="状态" value={snapshot.enabled ? '已开启' : '已关闭'} />
          <Chip label="鉴权" value={snapshot.ownerRequired ? '仅 owner' : '未限制'} />
          <Chip label="已注册" value={`${snapshot.commands.length} 条`} />
          <p className="basis-full text-xs leading-relaxed text-muted-foreground">
            仅 owner 发出的消息会尝试匹配命令，私聊与群聊都可触发；非 owner 与关闭态
            一律按普通聊天处理，不会收到权限提示，也不会看到隐藏命令的存在。
            注意群聊里的回复整群可见，/stat 会带出安装 ID 与库规模。
          </p>
        </Toolbar>
      ) : null}

      <ScrollArea>
        {error ? <div className="px-6 pt-3"><ErrorText>{error}</ErrorText></div> : null}
        {loading && !snapshot ? <Loading>正在读取开发者命令目录…</Loading> : null}
        {snapshot && snapshot.commands.length === 0 ? <Empty>当前没有已注册命令。</Empty> : null}
        {snapshot && snapshot.commands.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] border-collapse text-left text-[13px]">
              <thead className="sticky top-0 bg-muted text-xs text-subtle-foreground">
                <tr>
                  <th className="h-9 px-6 font-medium">命令</th>
                  <th className="h-9 px-3 font-medium">完整匹配正则</th>
                  <th className="h-9 px-3 font-medium">说明</th>
                  <th className="h-9 px-6 font-medium">鉴权</th>
                </tr>
              </thead>
              <tbody>
                {snapshot.commands.map((command) => (
                  <tr key={command.name} className="border-b border-border-subtle">
                    <td className="px-6 py-3 font-mono font-semibold text-primary-strong">
                      <span className="whitespace-nowrap select-all">{command.name}</span>
                    </td>
                    <td className="px-3 py-3 font-mono text-xs text-muted-foreground">
                      <span className="whitespace-nowrap select-all">{command.pattern}</span>
                    </td>
                    <td className="px-3 py-3">{command.description}</td>
                    <td className="px-6 py-3 whitespace-nowrap">{command.ownerRequired ? '仅 owner' : '未限制'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </ScrollArea>
    </PageFrame>
  )
}
