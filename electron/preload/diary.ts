/**
 * 为日记窗口暴露最小化的只读 IPC bridge。
 *
 * 该桥接层只转发日记读取请求和窗口关闭动作，不把主进程对象或业务写入接口
 * 暴露给渲染层；调用方为 electron/renderer/diary.ts。
 */
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
