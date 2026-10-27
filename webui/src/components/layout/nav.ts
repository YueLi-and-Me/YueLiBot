/**
 * 管理面板的导航结构与路由归属判定。
 *
 * 侧栏、窄屏抽屉与页面顶栏的位置路径共用同一份分组表：新增页面时只在这里登记
 * 一次，三处展示自动同步。路由表本身仍在 app/App.tsx，本文件只描述「页面属于
 * 哪个分组、在导航里叫什么」。
 */
import {
  BookMarked,
  Coins,
  Cpu,
  ListChecks,
  MessageSquare,
  Network,
  PackagePlus,
  Plug,
  Quote,
  Settings,
  SlidersHorizontal,
  SquareTerminal,
  Sticker,
  Store,
  Users,
  type LucideIcon,
} from 'lucide-react'

/** 单个导航入口。 */
export interface NavItem {
  /** 路由路径；同时作为前缀匹配子页面（如 `/persons` 覆盖 `/persons/:id`）。 */
  to: string
  /** 导航与分组路径中展示的名称，应与页面标题一致。 */
  label: string
  /** lucide 图标组件。 */
  icon: LucideIcon
}

/** 一组语义相关的导航入口。 */
export interface NavGroup {
  /** 分组名，展示为侧栏小标题与页头分组路径的首段。 */
  label: string
  items: Array<NavItem>
}

/** 首页入口路径；未登记的路径回落到该页，与 App.tsx 的 `*` 路由一致。 */
const HOME_PATH = '/'

/** 侧栏分组表，自上而下即展示顺序。 */
export const NAV_GROUPS: Array<NavGroup> = [
  {
    label: '观察',
    items: [
      { to: HOME_PATH, label: '会话观察', icon: MessageSquare },
      { to: '/usage', label: '用量统计', icon: Coins },
    ],
  },
  {
    label: '人与表达',
    items: [
      { to: '/persons', label: '人物与关系', icon: Users },
      { to: '/jargon', label: '黑话词表', icon: BookMarked },
      { to: '/expressions', label: '表达方式', icon: Quote },
      { to: '/emojis', label: '表情包库', icon: Sticker },
    ],
  },
  {
    label: '记忆',
    items: [
      { to: '/memory', label: '记忆联想网络', icon: Network },
      { to: '/memory/manage', label: '记忆管理', icon: ListChecks },
      { to: '/memory/tuning', label: '检索调优', icon: SlidersHorizontal },
      { to: '/memory/import', label: '导入中心', icon: PackagePlus },
    ],
  },
  {
    label: '配置',
    items: [
      { to: '/models', label: '模型与厂商', icon: Cpu },
      { to: '/tools', label: '工具与 MCP', icon: Plug },
      { to: '/plugins', label: '插件市场', icon: Store },
      { to: '/settings', label: '月璃设置', icon: Settings },
      { to: '/developer/commands', label: '开发者命令', icon: SquareTerminal },
    ],
  },
]


/** 路由归属判定结果。 */
export interface ActiveNav {
  group: NavGroup
  item: NavItem
}

/**
 * 判断路径是否落在某个入口之下（按路径段匹配，`/memory` 不匹配 `/memoryx`）。
 *
 * @param pathname 当前路径。
 * @param to 入口路径。
 * @returns 路径等于入口或位于其子路径时为 true。
 */
function covers(pathname: string, to: string): boolean {
  return pathname === to || pathname.startsWith(`${to}/`)
}

/**
 * 找出当前路径所属的分组与入口。
 *
 * @param pathname 当前路由路径。
 * @returns 最长前缀命中的入口；无命中时回落到首页入口。
 * @remarks 取最长前缀是因为记忆分组的入口互为前缀（`/memory` 与
 * `/memory/manage`），先命中短前缀会把子页面错标为联想网络。
 */
export function findActiveNav(pathname: string): ActiveNav {
  let best: ActiveNav | null = null
  for (const group of NAV_GROUPS) {
    for (const item of group.items) {
      if (item.to === HOME_PATH || !covers(pathname, item.to)) continue
      if (!best || item.to.length > best.item.to.length) best = { group, item }
    }
  }
  if (best) return best
  const homeGroup = NAV_GROUPS[0]
  const homeItem = homeGroup?.items[0]
  if (!homeGroup || !homeItem) throw new Error('导航分组表缺少首页入口')
  return { group: homeGroup, item: homeItem }
}
