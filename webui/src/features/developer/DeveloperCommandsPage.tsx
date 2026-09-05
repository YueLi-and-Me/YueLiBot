/**
 * 开发者命令通道只读页：展示开关、统一鉴权边界与已注册命令。
 */
import { RefreshCw, ShieldCheck, SquareTerminal } from 'lucide-react'

import { PageHeader } from '@/components/layout/PageHeader'
import {
  Button,
  Card,
  CardBody,
  Chip,
  Empty,
  ErrorText,
  Loading,
  SectionHeading,
} from '@/components/ui'
import { useDeveloperCommands } from '@/hooks/use-developer-commands'

/** 渲染开发者命令通道的只读目录。 */
export function DeveloperCommandsPage() {
  const { snapshot, error, loading, reload } = useDeveloperCommands()

  return (
    <div className="mx-auto flex w-full max-w-[1200px] flex-col gap-5 px-4 py-6 sm:px-6 lg:px-8">
      <PageHeader
        eyebrow="YUELI · DEVELOPER"
        title="开发者命令"
        subtitle="只读查看聊天内命令通道及注册目录；本页不能开启通道、注册命令或执行命令。"
        actions={
          <Button variant="secondary" onClick={() => void reload()} disabled={loading}>
            <RefreshCw className="size-4" aria-hidden="true" />
            刷新
          </Button>
        }
      />

      {error ? <ErrorText>{error}</ErrorText> : null}
      {loading && !snapshot ? <Loading>正在读取开发者命令目录…</Loading> : null}

      {snapshot ? (
        <>
          <Card className="animate-rise">
            <CardBody className="space-y-3">
              <SectionHeading
                title="通道边界"
                icon={<ShieldCheck />}
                tint="amber"
                actions={
                  <div className="flex flex-wrap gap-2">
                    <Chip label="状态" value={snapshot.enabled ? '已开启' : '已关闭'} />
                    <Chip label="鉴权" value={snapshot.ownerRequired ? '仅 owner' : '未限制'} />
                  </div>
                }
              />
              <p className="text-sm leading-relaxed text-muted-foreground">
                仅 owner 发出的消息会尝试匹配命令，私聊与群聊都可触发；非 owner 与关闭态
                一律按普通聊天处理，不会收到权限提示，也不会看到隐藏命令的存在。
                注意群聊里的回复整群可见，/stat 会带出安装 ID 与库规模。
              </p>
            </CardBody>
          </Card>

          <Card className="animate-rise">
            <CardBody className="space-y-3">
              <SectionHeading
                title={`已注册命令（${snapshot.commands.length}）`}
                icon={<SquareTerminal />}
                tint="coral"
              />
              {snapshot.commands.length === 0 ? (
                <Empty>当前没有已注册命令。</Empty>
              ) : (
                <div className="overflow-x-auto rounded-lg border border-border">
                  <table className="w-full min-w-[720px] border-collapse text-left text-sm">
                    <thead className="bg-muted/40 text-xs text-muted-foreground">
                      <tr>
                        <th className="px-4 py-3 font-medium">命令</th>
                        <th className="px-4 py-3 font-medium">完整匹配正则</th>
                        <th className="px-4 py-3 font-medium">说明</th>
                        <th className="px-4 py-3 font-medium">鉴权</th>
                      </tr>
                    </thead>
                    <tbody>
                      {snapshot.commands.map((command) => (
                        <tr key={command.name} className="border-t border-border">
                          <td className="px-4 py-3 font-mono font-semibold text-primary-strong">
                            <span className="whitespace-nowrap select-all">{command.name}</span>
                          </td>
                          <td className="px-4 py-3 font-mono text-xs text-muted-foreground">
                            <span className="whitespace-nowrap select-all">{command.pattern}</span>
                          </td>
                          <td className="px-4 py-3">{command.description}</td>
                          <td className="px-4 py-3">
                            {command.ownerRequired ? '仅 owner' : '未限制'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </CardBody>
          </Card>
        </>
      ) : null}
    </div>
  )
}
