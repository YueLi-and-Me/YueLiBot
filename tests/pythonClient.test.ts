/**
 * PythonClient 连接协议测试。
 *
 * 本模块属于 Electron 主进程后端连接层的 Vitest 测试，覆盖 WebSocket 子协议、
 * 反向截图通知与关闭诊断。测试使用 undici WebSocket 替身，避免依赖真实网络连接。
 * 后端连接的接管与重连行为在 `backendLink.test.ts` 中单独验证。
 */
import { describe, expect, it, vi } from 'vitest'

const webSocketConstructor = vi.hoisted(() => vi.fn())

vi.mock('undici', () => ({ WebSocket: webSocketConstructor }))

import { PythonClient } from '../electron/main/python/client.ts'

describe('PythonClient', () => {
  it('桌宠连接明确声明 desktop client', () => {
    webSocketConstructor.mockReset()
    webSocketConstructor.mockImplementation(function FakeWebSocket() {
      return { addEventListener: vi.fn() }
    })
    const client = new PythonClient(
      51092,
      'test-token',
      { send: () => undefined, isAlive: () => false },
      () => undefined,
    )

    ;(client as unknown as { _connect(): void })._connect()

    expect(webSocketConstructor).toHaveBeenCalledWith(
      'ws://127.0.0.1:51092/ws?client=desktop',
      ['yueli-test-token'],
    )
  })

  it('桌宠窗口隐藏时仍把反向截图请求交给主进程', () => {
    const requests: string[] = []
    const client = new PythonClient(
      1,
      'test-token',
      { send: () => undefined, isAlive: () => false },
      (reason) => requests.push(reason),
    )

    ;(client as unknown as { _handleMessage(raw: string): void })._handleMessage(JSON.stringify({
      channel: 'vision.capture_request',
      payload: { reason: 'scene' },
    }))

    expect(requests).toEqual(['scene'])
  })

  it('静默回合通知会转发给渲染进程以结束等待状态', () => {
    const send = vi.fn()
    const client = new PythonClient(
      1,
      'test-token',
      { send, isAlive: () => true },
      () => undefined,
    )

    ;(client as unknown as { _handleMessage(raw: string): void })._handleMessage(JSON.stringify({
      channel: 'chat.silent',
      payload: { turnId: 7, kind: 'silent', reason: '当前无需回复' },
    }))

    expect(send).toHaveBeenCalledWith('chat:event', {
      turnId: 7,
      kind: 'silent',
      reason: '当前无需回复',
    })
  })

  it('轮次开始通知会转发给渲染进程更新当前轮次', () => {
    const send = vi.fn()
    const client = new PythonClient(
      1,
      'test-token',
      { send, isAlive: () => true },
      () => undefined,
    )

    ;(client as unknown as { _handleMessage(raw: string): void })._handleMessage(JSON.stringify({
      channel: 'chat.start',
      payload: { turnId: 8, kind: 'start' },
    }))

    expect(send).toHaveBeenCalledWith('chat:event', { turnId: 8, kind: 'start' })
  })

  it('WS 失败诊断包含事件类型、关闭码和关闭原因', () => {
    webSocketConstructor.mockReset()
    const listeners: Record<string, (event: Event) => void> = {}
    webSocketConstructor.mockImplementation(function FakeWebSocket() {
      return {
        addEventListener: (name: string, listener: (event: Event) => void) => {
          listeners[name] = listener
        },
        close: vi.fn(),
      }
    })
    const debugSpy = vi.spyOn(console, 'debug').mockImplementation(() => undefined)
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const dispatch = (name: string, event: Event): void => {
      const listener = listeners[name]
      if (!listener) throw new Error(`缺少 WebSocket ${name} 监听器`)
      listener(event)
    }
    const client = new PythonClient(
      51092,
      'test-token',
      { send: () => undefined, isAlive: () => false },
    )

    try {
      ;(client as unknown as { _connect(): void })._connect()
      dispatch('error', { type: 'error', message: '' } as unknown as Event)
      dispatch('close', { code: 1006, reason: 'connection refused' } as unknown as Event)

      // error 事件未提供可用原因时，不应额外占用日志记录。
      expect(debugSpy).not.toHaveBeenCalled()
      expect(warnSpy).toHaveBeenCalledWith(
        '[python-client] WS 首次连接关闭：',
        'code=1006 reason=connection refused',
      )

      dispatch('open', { type: 'open' } as Event)
      dispatch('close', { code: 1001, reason: 'server restart' } as unknown as Event)
      expect(warnSpy).toHaveBeenCalledWith(
        '[python-client] WS 重连关闭：',
        'code=1001 reason=server restart',
      )
    } finally {
      client.stop()
      debugSpy.mockRestore()
      warnSpy.mockRestore()
    }
  })

  it('主动关停不打任何告警', () => {
    webSocketConstructor.mockReset()
    const listeners: Record<string, (event: Event) => void> = {}
    webSocketConstructor.mockImplementation(function FakeWebSocket() {
      return {
        addEventListener: (name: string, listener: (event: Event) => void) => {
          listeners[name] = listener
        },
        close: vi.fn(),
      }
    })
    const client = new PythonClient(
      51092,
      'test-token',
      { send: () => undefined, isAlive: () => false },
    )
    ;(client as unknown as { _connect(): void })._connect()
    listeners.open?.({ type: 'open' } as Event)

    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    try {
      client.stop()
      // 退出桌宠时服务端也会把连接关掉，close 仍然会到达
      listeners.close?.({ code: 1005 } as unknown as Event)

      expect(warnSpy).not.toHaveBeenCalled()
      expect(errorSpy).not.toHaveBeenCalled()
    } finally {
      warnSpy.mockRestore()
      errorSpy.mockRestore()
    }
  })
})
