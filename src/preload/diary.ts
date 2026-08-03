import { contextBridge, ipcRenderer } from 'electron'
import { IPC, type DiaryBridge, type DiaryPayload } from '../shared/ipc.ts'

/**
 * 日记不应继承桌宠的交互能力。
 *
 * 单独的 preload 只暴露读取方法，模型生成内容即使出现 XSS 也没有可调用的
 * set / update / write 通道。
 */
const bridge: DiaryBridge = {
  read: () => ipcRenderer.invoke(IPC.Diary) as Promise<DiaryPayload>,
}

contextBridge.exposeInMainWorld('diary', bridge)
