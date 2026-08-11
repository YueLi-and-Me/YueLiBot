/**
 * 图像格式归一化模块。
 *
 * 本模块接收图像供应商返回的二进制数据，使用 `sharp` 验证并在必要时重新编码为
 * PNG，向立绘抠图、对齐和逐像素比较流程提供统一的输入容器。模块只返回内存中的
 * `Buffer`，不负责文件写入、网络请求或供应商错误分类；各 Provider 文件负责解析
 * 响应并决定错误处理策略。
 */
import sharp from 'sharp'

/**
 * 将供应商返回的图像数据统一为 PNG。
 *
 * @param {Buffer} buf 完整图像二进制数据；格式必须是 `sharp` 支持的图像格式，
 *   本函数不接受文件路径，也不预先校验空值或文件大小。
 * @returns {Promise<Buffer>} 已经是 PNG 时返回原始 Buffer；其他受支持格式会被
 *   重新编码为 PNG 后返回。
 * @throws {Error} 输入不是受支持的图像数据，或解码、编码过程中发生错误时，传播
 *   `sharp` 返回的错误。
 * @remarks 已经是 PNG 的输入不会重新编码；其他格式会完整解码并按压缩级别 6
 *   重新编码，处理时间和临时内存占用取决于图像像素数。该转换只统一容器格式，
 *   不保证新增透明通道，也不恢复源图像已经丢失的画质。
 */
export async function toPng(buf: Buffer): Promise<Buffer> {
  const meta = await sharp(buf).metadata()
  if (meta.format === 'png') return buf
  return sharp(buf).png({ compressionLevel: 6 }).toBuffer()
}
