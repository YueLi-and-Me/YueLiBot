/**
 * 模型编辑表单里单价、缓存命中价与分时段价格的纯函数：空时段模板、可留空单价的
 * 解析与提交前校验。
 *
 * 时段规则（时刻格式、开始等于结束、重叠）与 Python 加载器、Electron 读取器共用
 * `electron/shared/price-periods.ts`，表单在保存前就给出与启动期相同的报错。
 */
import type { PricePeriodConfig } from '../../../../electron/shared/ipc.ts'
import { pricePeriodProblem } from '../../../../electron/shared/price-periods.ts'

/** 新增时段的初始值：夜间 00:00～08:00，单价待填。 */
export function emptyPricePeriod(): PricePeriodConfig {
  return { start: '00:00', end: '08:00', price_in: 0, price_out: 0, cache_price_in: null }
}

/**
 * 解析可留空的单价输入。
 *
 * @param text 输入框文本。
 * @returns 空白时为 null（与输入单价相同），否则为数字；非数字为 NaN，由校验报错。
 */
export function parseOptionalPrice(text: string): number | null {
  return text.trim() === '' ? null : Number(text)
}

/** 参与校验的单价字段。 */
export interface PriceFields {
  name: string
  price_in: number
  price_out: number
  cache_price_in: number | null
  price_periods: PricePeriodConfig[]
}

/**
 * 判断一个单价是否为合法的非负有限数。
 *
 * @param value 单价。
 * @returns 合法时为 true。
 */
function validPrice(value: number): boolean {
  return Number.isFinite(value) && value >= 0
}

/**
 * 找出表单单价部分的第一处错误。
 *
 * @param fields 模型的单价字段。
 * @returns 报错文本；没有错误时为 null。
 */
export function priceFormProblem(fields: PriceFields): string | null {
  const name = fields.name.trim() || '（未命名）'
  if (!validPrice(fields.price_in) || !validPrice(fields.price_out)) {
    return '输入价格与输出价格必须是不小于 0 的数字'
  }
  if (fields.cache_price_in !== null && !validPrice(fields.cache_price_in)) {
    return '缓存命中价必须是不小于 0 的数字；留空表示与输入价格相同'
  }
  for (const [index, period] of fields.price_periods.entries()) {
    const label = `第 ${index + 1} 个时段`
    if (!validPrice(period.price_in) || !validPrice(period.price_out)) {
      return `${label}的输入价格与输出价格必须是不小于 0 的数字`
    }
    if (period.cache_price_in !== null && !validPrice(period.cache_price_in)) {
      return `${label}的缓存命中价必须是不小于 0 的数字；留空表示与该时段输入价格相同`
    }
  }
  return pricePeriodProblem(name, fields.price_periods)
}
