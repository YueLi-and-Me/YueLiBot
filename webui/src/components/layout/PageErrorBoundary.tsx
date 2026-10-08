/**
 * 页面级错误边界：业务页面渲染抛错时，在工作区里显示错误原文，侧栏照常可用。
 *
 * 没有这一层时，任何页面里的一次渲染异常都会让 React 卸载整棵树，表现为整页白屏，
 * 看不到是哪一页、哪个字段出的错。这里不吞错也不替换数据：错误原文和组件栈原样
 * 展示并打到控制台，换一个页面（路径变化）即重置。被 AppShell 引用。
 */
import { Component, type ErrorInfo, type ReactNode } from 'react'

import { Button } from '@/components/ui'

interface PageErrorBoundaryProps {
  /** 当前路径；变化时清除上一页的错误状态。 */
  resetKey: string
  children: ReactNode
}

interface PageErrorBoundaryState {
  error: Error | null
  componentStack: string
}

/** 捕获业务页面渲染异常并展示错误详情。 */
export class PageErrorBoundary extends Component<PageErrorBoundaryProps, PageErrorBoundaryState> {
  override state: PageErrorBoundaryState = { error: null, componentStack: '' }

  /**
   * 记录渲染异常，使下一次渲染进入错误态。
   *
   * @param error 子树抛出的异常。
   * @returns 新的错误状态。
   */
  static getDerivedStateFromError(error: Error): Partial<PageErrorBoundaryState> {
    return { error }
  }

  /**
   * 保存组件栈并输出到控制台，便于定位到具体组件。
   *
   * @param error 子树抛出的异常。
   * @param info React 提供的组件栈。
   */
  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('页面渲染出错', error, info.componentStack)
    this.setState({ componentStack: info.componentStack ?? '' })
  }

  /**
   * 路径变化时清除错误状态，让新页面正常渲染。
   *
   * @param previous 上一次的属性。
   */
  override componentDidUpdate(previous: PageErrorBoundaryProps): void {
    if (previous.resetKey !== this.props.resetKey && this.state.error) {
      this.setState({ error: null, componentStack: '' })
    }
  }

  override render(): ReactNode {
    const { error, componentStack } = this.state
    if (!error) return this.props.children
    return (
      <div role="alert" className="flex min-h-0 flex-1 flex-col overflow-y-auto px-6 py-6">
        <h1 className="text-sm font-semibold text-destructive">这个页面渲染出错了</h1>
        <p className="mt-1 text-[13px] text-muted-foreground">
          其他页面不受影响，可以从左侧切走；把下面的错误原文发给开发者即可定位。
        </p>
        <pre className="mt-4 overflow-x-auto rounded-lg border border-terminal-border bg-terminal p-4 font-mono text-xs leading-5 whitespace-pre-wrap text-terminal-foreground">
          {`${error.name}: ${error.message}${componentStack}`}
        </pre>
        <div className="mt-4">
          <Button variant="secondary" onClick={() => window.location.reload()}>
            重新加载
          </Button>
        </div>
      </div>
    )
  }
}
