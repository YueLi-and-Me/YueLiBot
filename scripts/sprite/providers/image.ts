import sharp from 'sharp'

/**
 * 把 provider 返回的图统一成 PNG。
 *
 * 方舟返回的是 JPEG，Gemini 视模型而定。下游全链路都假定 PNG：
 *   · 抠图产物需要 alpha 通道，JPEG 没有
 *   · 预览页的逐像素 diff 会把 JPEG 压缩噪点当成「改动」，干扰判断
 *   · sharp 的 extract/composite 在混合格式下行为不一致
 *
 * 转换不能挽回已经损失的画质，但能保证后续每一步的输入形态一致。
 */
export async function toPng(buf: Buffer): Promise<Buffer> {
  const meta = await sharp(buf).metadata()
  if (meta.format === 'png') return buf
  return sharp(buf).png({ compressionLevel: 6 }).toBuffer()
}
