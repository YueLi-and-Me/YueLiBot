/**
 * 为桌宠主渲染层暴露聊天、角色、前台采集和窗口控制 IPC bridge。
 *
 * 所有通道名和 payload 类型来自 electron/shared/ipc.ts；本模块只做参数转发，
 * 不在隔离上下文中执行业务逻辑或保存状态。
 */
import { contextBridge, ipcRenderer } from 'electron'
import {
  IPC,
  type ChatStreamEvent,
  type PetBridge,
  type SleepStateEvent,
  type VisionWatchEvent,
  type VoiceEvent,
} from '../shared/ipc.ts'

/**
 * 只暴露具体动作，不透传 ipcRenderer。
 * 渲染层要显示模型生成的内容，给它通用 IPC 能力等于把整个主进程
 * 暴露在一个内容不可信的环境里。
 */
const bridge: PetBridge = {
  setInteractive: (interactive) => ipcRenderer.send(IPC.SetInteractive, interactive),
  quit: () => ipcRenderer.send(IPC.Quit),
  beginDrag: () => ipcRenderer.send(IPC.BeginDrag),
  endDrag: () => ipcRenderer.send(IPC.EndDrag),
  focusInput: (focus) => ipcRenderer.send(IPC.FocusInput, focus),
  userInteracted: () => ipcRenderer.send(IPC.UserInteracted),
  send: (text) => ipcRenderer.invoke(IPC.Send, text) as Promise<number>,
  interrupt: () => ipcRenderer.send(IPC.Interrupt),
  onEvent: (handler) => {
    const listener = (_e: unknown, payload: ChatStreamEvent) => handler(payload)
    ipcRenderer.on(IPC.Event, listener)
    // 返回退订函数：渲染层热重载时不清理会导致监听器越堆越多，
    // 表现是一条消息触发多次打字机
    return () => ipcRenderer.off(IPC.Event, listener)
  },
  onOpenComposer: (handler) => {
    const listener = (): void => handler()
    ipcRenderer.on(IPC.OpenComposer, listener)
    return () => ipcRenderer.off(IPC.OpenComposer, listener)
  },
  onVoice: (handler) => {
    const listener = (_e: unknown, payload: VoiceEvent): void => handler(payload)
    ipcRenderer.on(IPC.Voice, listener)
    return () => ipcRenderer.off(IPC.Voice, listener)
  },
  onVisionWatching: (handler) => {
    const listener = (_e: unknown, payload: VisionWatchEvent): void => handler(payload)
    ipcRenderer.on(IPC.Vision, listener)
    return () => ipcRenderer.off(IPC.Vision, listener)
  },
  onSleepState: (handler) => {
    const listener = (_e: unknown, payload: SleepStateEvent): void => handler(payload)
    ipcRenderer.on(IPC.Sleep, listener)
    return () => ipcRenderer.off(IPC.Sleep, listener)
  },
}

contextBridge.exposeInMainWorld('pet', bridge)
