/**
 * 应用侧栏：深藏青撞色的品牌与导航容器。
 *
 * 自上而下为品牌区（蓝色渐变字母块 + 面板名称）、主导航（会话观察/人物画像）、
 * 会话分区锚点组（仅会话观察页可见）、底部操作区（主题开关 + 登出）。
 * 侧栏在明暗两主题下保持藏青不变；窄屏时整体隐藏，由 AppShell 的顶部条替代。
 */
import {
  Activity,
  Cpu,
  FileText,
  LayoutGrid,
  List,
  LogOut,
  MessageSquare,
  Settings,
  Terminal,
  Users,
} from 'lucide-react'
import type { ReactNode } from 'react'
import { Link, useLocation } from 'react-router'

import { ThemeSwitch, cn } from '@/components/ui'
import { useAuth } from '@/hooks/use-auth'
import { useTheme } from '@/hooks/use-theme'

/** 会话观察页内的锚点分区导航项。 */
const ANCHOR_ITEMS = [
  { hash: '#overview', label: '运行总览', icon: LayoutGrid },
  { hash: '#events', label: '事件账本', icon: List },
  { hash: '#prompts', label: '提示词工作台', icon: FileText },
  { hash: '#logs', label: '实时日志', icon: Terminal },
] as const

/**
 * 渲染品牌区：蓝色渐变字母块与面板名称。
 *
 * @returns 品牌区元素。
 */
function Brand() {
  return (
    <div className="flex items-center gap-3 px-5 pt-6 pb-5">
      <span
        className="grid size-9 flex-none place-items-center rounded-xl bg-gradient-to-br from-sky-400 to-blue-600 text-[17px] font-bold text-white shadow-[0_2px_10px_rgb(30_100_220/0.45)]"
        aria-hidden="true"
      >
        Y
      </span>
      <div className="min-w-0 leading-tight">
        <strong className="block truncate text-[15px] font-semibold text-white">Bot 观察面板</strong>
        <span className="text-[10px] font-medium tracking-[0.18em] text-sidebar-muted">YUELI / WEBUI</span>
      </div>
    </div>
  )
}

interface NavLinkProps {
  /** 链接目标（路由路径或页内锚点）。 */
  to: string
  /** 是否当前激活。 */
  active?: boolean
  /** lucide 图标节点。 */
  icon: ReactNode
  /** 导航文本。 */
  label: string
}

/**
 * 渲染单个侧栏导航项。
 *
 * @param props.to 链接目标。
 * @param props.active 激活态：品牌蓝实心底 + 白字。
 * @returns 导航链接元素；锚点项使用原生 a，路由项使用 Link。
 */
function NavLink({ to, active = false, icon, label }: NavLinkProps) {
  const classes = cn(
    'flex items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] font-medium transition-colors duration-150',
    active
      ? 'bg-sidebar-active text-sidebar-active-foreground shadow-[0_2px_8px_rgb(30_100_220/0.4)]'
      : 'text-sidebar-muted hover:bg-white/6 hover:text-sidebar-foreground',
  )
  const content = (
    <>
      <span className="[&>svg]:size-4" aria-hidden="true">{icon}</span>
      {label}
    </>
  )
  if (to.startsWith('#')) {
    return (
      <a href={to} className={classes}>
        {content}
      </a>
    )
  }
  return (
    <Link to={to} className={classes}>
      {content}
    </Link>
  )
}

/**
 * 渲染分区小标题。
 *
 * @param props.children 标题文本。
 * @returns 小号大写字距的分区标题。
 */
function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <p className="px-3 pt-4 pb-1.5 text-[10px] font-semibold tracking-[0.16em] text-sidebar-muted/80">
      {children}
    </p>
  )
}

/**
 * 渲染深藏青侧栏。
 *
 * @returns aside 侧栏元素；宽 244px，桌面端常驻。
 * @remarks 「会话分区」锚点组仅在会话观察页展示，人物画像页整组隐藏（与旧版
 * 行为一致，避免锚点指向不存在的分区）。
 */
export function Sidebar() {
  const { logout } = useAuth()
  const { dark, toggle } = useTheme()
  const location = useLocation()
  const onPersons = location.pathname.startsWith('/persons')
  const onModels = location.pathname.startsWith('/models')
  const onSettings = location.pathname.startsWith('/settings')
  const onHome = !onPersons && !onModels && !onSettings

  return (
    <aside className="hidden w-(--sidebar-width) flex-none flex-col border-r border-sidebar-border bg-sidebar lg:flex">
      <Brand />
      <nav className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-3 pb-3">
        <SectionTitle>概览</SectionTitle>
        <NavLink to="/" active={onHome} icon={<MessageSquare />} label="会话观察" />
        <NavLink to="/models" active={location.pathname.startsWith('/models')} icon={<Cpu />} label="模型与厂商" />
        <NavLink to="/persons" active={onPersons} icon={<Users />} label="人物画像" />
        <NavLink to="/settings" active={onSettings} icon={<Settings />} label="月璃设置" />
        {onHome ? (
          <div className="mt-1 border-t border-sidebar-border/60 pt-1">
            <SectionTitle>会话分区</SectionTitle>
            {ANCHOR_ITEMS.map((item) => (
              <NavLink key={item.hash} to={item.hash} icon={<item.icon />} label={item.label} />
            ))}
          </div>
        ) : null}
      </nav>
      <div className="flex items-center justify-between gap-2 border-t border-sidebar-border px-4 py-3.5">
        <ThemeSwitch dark={dark} onToggle={toggle} />
        <button
          type="button"
          onClick={() => void logout()}
          className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[13px] font-medium text-sidebar-muted transition-colors hover:bg-white/6 hover:text-sidebar-foreground"
        >
          <LogOut className="size-4" aria-hidden="true" />
          登出
        </button>
      </div>
    </aside>
  )
}

/**
 * 渲染窄屏顶部导航条（侧栏的移动退化形态）。
 *
 * @returns 顶部条元素；仅在小屏显示，横向滚动容纳导航项。
 */
export function MobileTopbar() {
  const { logout } = useAuth()
  const { dark, toggle } = useTheme()
  const location = useLocation()
  const onPersons = location.pathname.startsWith('/persons')
  const onModels = location.pathname.startsWith('/models')
  const onSettings = location.pathname.startsWith('/settings')

  return (
    <div className="flex flex-none items-center gap-2 border-b border-sidebar-border bg-sidebar px-3 py-2 lg:hidden">
      <span
        className="grid size-7 flex-none place-items-center rounded-lg bg-gradient-to-br from-sky-400 to-blue-600 text-[13px] font-bold text-white"
        aria-hidden="true"
      >
        Y
      </span>
      <nav className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
        <NavLink to="/" active={!onPersons && !onModels && !onSettings} icon={<Activity />} label="会话观察" />
        <NavLink to="/models" active={onModels} icon={<Cpu />} label="模型与厂商" />
        <NavLink to="/persons" active={onPersons} icon={<Users />} label="人物画像" />
        <NavLink to="/settings" active={onSettings} icon={<Settings />} label="月璃设置" />
      </nav>
      <ThemeSwitch dark={dark} onToggle={toggle} className="scale-[0.85]" />
      <button
        type="button"
        onClick={() => void logout()}
        aria-label="登出"
        className="cursor-pointer rounded-lg p-1.5 text-sidebar-muted transition-colors hover:bg-white/6 hover:text-sidebar-foreground"
      >
        <LogOut className="size-4" aria-hidden="true" />
      </button>
    </div>
  )
}
