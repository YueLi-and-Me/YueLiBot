/**
 * 组合 Tailwind 类名的工具函数。
 *
 * 过滤掉条件类名中的假值并以空格拼接，供全部 UI 组件合并默认样式与调用方
 * 追加样式使用；被 components/ui 下各组件与业务分区组件依赖。
 *
 * @param parts 待组合的类名片段，允许 `false`、`null`、`undefined` 占位。
 * @returns 拼接后的类名字符串；空输入返回空字符串。
 */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}
