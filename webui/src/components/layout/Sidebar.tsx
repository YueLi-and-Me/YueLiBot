/**
 * 左侧导航栏与窄屏导航抽屉。
 *
 * 导航栏铺在银白框架色上，自上而下为品牌区（月璃头像与名称）、按 nav.ts 分组的
 * 页面入口、底部操作区（明暗主题、登出、收起）。品牌区高 56px，与工作区顶栏
 * 同高，两者的底边连成一条横线。可收起为只显示图标的窄栏，收起状态持久化到
 * localStorage。窄屏下导航栏隐藏，由页面顶栏的菜单按钮唤出同一份内容的抽屉。
 */
import { LogOut, Moon, PanelLeftClose, PanelLeftOpen, Sun, X } from 'lucide-react'
import { LayoutGroup, motion } from 'motion/react'
import { useEffect, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Link, useLocation } from 'react-router'

import { cn } from '@/components/ui'
import { useAuth } from '@/hooks/use-auth'
import { useTheme } from '@/hooks/use-theme'

import { BrandMark } from './BrandMark'
import { NAV_GROUPS, findActiveNav, type NavItem } from './nav'

/** 激活底色的弹簧参数。 */
const PILL_SPRING = { type: 'spring', stiffness: 520, damping: 40, mass: 0.6 } as const

/** 侧栏收起状态的本地存储键名；值为 `'1'` 表示收起。 */
const COLLAPSED_STORAGE_KEY = 'yueli-webui-sidebar-collapsed'

/**
 * 读取并持久化侧栏收起状态。
 *
 * @returns 当前是否收起与切换函数；存储不可用（如隐私模式）时只在本次会话内生效。
 */
function useCollapsed(): [boolean, () => void] {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(COLLAPSED_STORAGE_KEY) === '1'
    } catch {
      return false
    }
  })
  const toggle = () => {
    setCollapsed((current) => {
      const next = !current
      try {
        localStorage.setItem(COLLAPSED_STORAGE_KEY, next ? '1' : '0')
      } catch {
        // 写入失败只影响持久化，收起状态在本次会话内已生效。
      }
      return next
    })
  }
  return [collapsed, toggle]
}

interface IconButtonProps {
  /** 无障碍名称，同时作为悬停提示。 */
  label: string
  onClick: () => void
  children: ReactNode
}

/**
 * 渲染导航栏里的方形图标按钮。
 *
 * @param props.label 无障碍名称与悬停提示。
 * @param props.onClick 点击回调。
 * @returns 28px 图标按钮。
 */
function IconButton({ label, onClick, children }: IconButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className="inline-flex size-7 flex-none cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-nav-active hover:text-foreground [&>svg]:size-4"
    >
      {children}
    </button>
  )
}

/**
 * 渲染明暗主题的两段切换。
 *
 * @returns radiogroup 元素；收起态改用单个图标按钮，见 Sidebar。
 */
function ThemeSegment() {
  const { dark, toggle } = useTheme()
  const option = (isDark: boolean, label: string, icon: ReactNode) => {
    const checked = dark === isDark
    return (
      <button
        type="button"
        role="radio"
        aria-checked={checked}
        aria-label={label}
        title={label}
        onClick={() => {
          if (!checked) toggle()
        }}
        className={cn(
          'inline-flex h-6 w-7 cursor-pointer items-center justify-center rounded transition-colors [&>svg]:size-3.5',
          checked ? 'bg-card text-foreground shadow-xs' : 'text-muted-foreground hover:text-foreground',
        )}
      >
        {icon}
      </button>
    )
  }
  return (
    <div role="radiogroup" aria-label="界面主题" className="mr-auto flex gap-0.5 rounded-md bg-nav-active p-0.5">
      {option(false, '亮色主题', <Sun />)}
      {option(true, '暗色主题', <Moon />)}
    </div>
  )
}

/**
 * 渲染品牌区：月璃头像与面板名称。
 *
 * @param props.collapsed 收起态只显示头像。
 * @param props.trailing 品牌区右侧的附加按钮（抽屉里的关闭按钮），可选。
 * @returns 品牌区元素，高 56px，带底部分隔线。
 */
function Brand({ collapsed, trailing }: { collapsed: boolean; trailing?: ReactNode }) {
  return (
    <div
      className={cn(
        'flex h-14 flex-none items-center gap-2.5 border-b border-border',
        collapsed ? 'justify-center' : 'pr-3 pl-5',
      )}
    >
      <BrandMark className="size-7" />
      {collapsed ? null : (
        <div className="min-w-0 flex-1 leading-tight">
          <strong className="block truncate text-sm font-semibold">月璃</strong>
          <span className="block truncate text-[11px] text-subtle-foreground">管理控制台</span>
        </div>
      )}
      {trailing}
    </div>
  )
}

interface NavEntryProps {
  item: NavItem
  active: boolean
  collapsed: boolean
  /** 激活底色的 layoutId 命名空间；侧栏与抽屉各用一个，互不串扰。 */
  pillId: string
}

/**
 * 渲染单个页面入口。
 *
 * @param props.item 入口定义。
 * @param props.active 激活态：灰蓝底色（layoutId 滑动）+ 主色图标。
 * @param props.collapsed 收起态只显示图标，名称移入悬停提示。
 * @returns 路由链接元素。
 */
function NavEntry({ item, active, collapsed, pillId }: NavEntryProps) {
  const Icon = item.icon
  return (
    <Link
      to={item.to}
      title={collapsed ? item.label : undefined}
      aria-label={collapsed ? item.label : undefined}
      aria-current={active ? 'page' : undefined}
      className={cn(
        'relative flex h-8 items-center gap-2.5 rounded-md text-[13px] whitespace-nowrap transition-colors duration-150',
        collapsed ? 'justify-center' : 'px-3',
        active ? 'font-medium text-foreground' : 'text-muted-foreground hover:bg-nav-active/60 hover:text-foreground',
      )}
    >
      {active ? (
        <motion.span
          layoutId={pillId}
          className="absolute inset-0 rounded-md bg-nav-active"
          transition={PILL_SPRING}
          aria-hidden="true"
        />
      ) : null}
      <Icon className={cn('relative z-10 size-4 flex-none', active ? 'text-primary' : null)} aria-hidden="true" />
      {collapsed ? null : <span className="relative z-10 truncate">{item.label}</span>}
    </Link>
  )
}

/**
 * 渲染分组导航列表。
 *
 * @param props.collapsed 收起态用细分隔线代替分组标题。
 * @param props.pillId 激活底色的 layoutId。
 * @returns 可纵向滚动的 nav 元素。
 */
function NavList({ collapsed, pillId }: { collapsed: boolean; pillId: string }) {
  const { pathname } = useLocation()
  const active = findActiveNav(pathname)
  return (
    <LayoutGroup id={pillId}>
      <nav aria-label="主导航" className="flex min-h-0 flex-1 flex-col overflow-x-hidden overflow-y-auto px-2 pt-1 pb-3">
        {NAV_GROUPS.map((group, index) => (
          <div key={group.label} className="flex flex-col gap-px">
            {collapsed ? (
              index > 0 ? <div className="mx-2 my-2 h-px bg-border" aria-hidden="true" /> : <div className="h-2" />
            ) : (
              <p className="px-3 pt-3.5 pb-1.5 text-[11px] font-semibold tracking-[0.04em] text-subtle-foreground">
                {group.label}
              </p>
            )}
            {group.items.map((item) => (
              <NavEntry
                key={item.to}
                item={item}
                active={active.item === item}
                collapsed={collapsed}
                pillId={`${pillId}-pill`}
              />
            ))}
          </div>
        ))}
      </nav>
    </LayoutGroup>
  )
}

/**
 * 渲染桌面端导航栏。
 *
 * @returns aside 元素；展开 240px、收起 56px，仅在 lg 及以上断点显示。
 */
export function Sidebar() {
  const { logout } = useAuth()
  const { dark, toggle: toggleTheme } = useTheme()
  const [collapsed, toggleCollapsed] = useCollapsed()

  return (
    <aside
      className={cn(
        'hidden flex-none flex-col border-r border-border bg-chrome transition-[width] duration-200 ease-emphasized lg:flex',
        collapsed ? 'w-(--sidebar-width-collapsed)' : 'w-(--sidebar-width)',
      )}
    >
      <Brand collapsed={collapsed} />
      <NavList collapsed={collapsed} pillId="sidebar-nav" />
      {collapsed ? (
        <div className="flex flex-none flex-col items-center gap-1 border-t border-border py-3">
          <IconButton label={dark ? '切换到亮色主题' : '切换到暗色主题'} onClick={toggleTheme}>
            {dark ? <Moon /> : <Sun />}
          </IconButton>
          <IconButton label="登出" onClick={() => void logout()}>
            <LogOut />
          </IconButton>
          <IconButton label="展开侧栏" onClick={toggleCollapsed}>
            <PanelLeftOpen />
          </IconButton>
        </div>
      ) : (
        <div className="flex h-14 flex-none items-center gap-1 border-t border-border pr-3 pl-5">
          <ThemeSegment />
          <IconButton label="登出" onClick={() => void logout()}>
            <LogOut />
          </IconButton>
          <IconButton label="收起侧栏" onClick={toggleCollapsed}>
            <PanelLeftClose />
          </IconButton>
        </div>
      )}
    </aside>
  )
}

/**
 * 渲染窄屏导航抽屉。
 *
 * @param props.open 是否打开。
 * @param props.onClose 关闭回调（遮罩点击、ESC、关闭按钮）。
 * @returns 打开时经 portal 挂到 body 的抽屉；关闭时不渲染。
 */
export function NavDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { logout } = useAuth()

  useEffect(() => {
    if (!open) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [open, onClose])

  if (!open) return null
  return createPortal(
    <div className="fixed inset-0 z-50 lg:hidden">
      <div className="absolute inset-0 animate-fade-in bg-black/40" onClick={onClose} aria-hidden="true" />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="导航"
        className="absolute inset-y-0 left-0 flex w-72 max-w-[85vw] animate-drawer-in flex-col bg-chrome shadow-dialog"
      >
        <Brand
          collapsed={false}
          trailing={
            <IconButton label="关闭导航" onClick={onClose}>
              <X />
            </IconButton>
          }
        />
        <NavList collapsed={false} pillId="drawer-nav" />
        <div className="flex h-14 flex-none items-center gap-1 border-t border-border pr-3 pl-5">
          <ThemeSegment />
          <IconButton label="登出" onClick={() => void logout()}>
            <LogOut />
          </IconButton>
        </div>
      </aside>
    </div>,
    document.body,
  )
}
