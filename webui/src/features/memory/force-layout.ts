/**
 * 联想网络的力导向布局：把「哪些记忆连在一起」算成一组二维坐标。
 *
 * 与 React 完全解耦——本模块只做数值迭代，不认识组件也不碰 DOM，由
 * `AssociationGraph` 负责驱动与渲染。分开是因为布局要在一帧里跑几十次
 * 迭代，混进组件里就只能靠 setState 推进，每次迭代都要过一遍协调器。
 *
 * 三种力：节点间的库仑斥力（把图摊开）、边上的弹簧（把相关的拉近，边越强
 * 静止长度越短）、以及指向原点的向心力（防止孤立子图飘走）。
 */

/** 斥力系数。数值决定图整体的疏密，调大则节点互相推得更开。 */
const CHARGE = 5200
/** 两点距离的下限（像素）。完全重合时距离为 0，斥力会算出 Infinity 并让坐标变成 NaN。 */
const MIN_DISTANCE = 12
/** 弹簧的基准静止长度（像素），实际长度按边强度在此基础上缩短。 */
const LINK_DISTANCE = 118
/** 边强度对静止长度的压缩幅度（像素）：强度 1 的边比强度 0 的短这么多。 */
const LINK_PULL = 46
/** 弹簧劲度，与边强度相乘后生效。 */
const LINK_STIFFNESS = 0.16
/** 向心力系数，随节点到原点的距离线性增长。 */
const CENTER_PULL = 0.014
/** 速度阻尼，每步保留的动量比例。低于 0.8 收敛快但容易卡在局部纠缠。 */
const DAMPING = 0.84
/** 每步的温度衰减；温度决定这一步允许的位移比例。 */
const ALPHA_DECAY = 0.985
/** 单步最大位移（像素）。没有这个上限时初始重叠的两点会被一脚踹出画面。 */
const MAX_STEP = 34

/** 初始布局的收敛迭代次数。同步跑完再渲染，避免用户看到一团乱麻慢慢散开。 */
export const SETTLE_TICKS = 420
/** 拖拽时每帧补跑的迭代次数：让被拖节点的邻居跟着动，而不是整张图僵住。 */
export const DRAG_TICKS = 3
/** 拖拽结束后的回稳迭代次数，把拖出来的形变收回一个稳定状态。 */
export const RELAX_TICKS = 90

/** 参与迭代的一个节点。 */
export interface Particle {
  /** 节点 ID，与后端 memory_nodes 主键一致。 */
  id: number
  x: number
  y: number
  vx: number
  vy: number
  /** 为真时坐标由外部（拖拽）指定，本模块不再修改它的位置与速度。 */
  fixed: boolean
}

/** 参与迭代的一条边。 */
export interface Spring {
  source: number
  target: number
  /** 边强度，取值 0~1；越强静止长度越短、劲度越大。 */
  strength: number
}

/**
 * 按节点顺序生成确定性的初始坐标。
 *
 * 用黄金角螺旋而**不是** `Math.random`：随机初值会让同一张图每次刷新都收敛成
 * 不同形状，用户刚认出来的那团结构下一次就找不到了。螺旋按数组下标铺开，而
 * 调用方传入的节点按 ID 升序，因此新记忆只会追加在螺旋外圈，已有节点的起点
 * 保持不变。
 *
 * @param ids 节点 ID，需按稳定顺序（ID 升序）传入。
 * @returns 初始化好的粒子数组，顺序与入参一致。
 */
export function seedParticles(ids: readonly number[]): Particle[] {
  // 黄金角 ≈ 137.5°，使相邻下标的点在角度上尽量分散，避免初始就挤成几条射线。
  const goldenAngle = Math.PI * (3 - Math.sqrt(5))
  return ids.map((id, index) => {
    const radius = 26 * Math.sqrt(index + 1)
    const angle = index * goldenAngle
    return {
      id,
      x: radius * Math.cos(angle),
      y: radius * Math.sin(angle),
      vx: 0,
      vy: 0,
      fixed: false,
    }
  })
}

/**
 * 推进一步迭代。
 *
 * @param particles 粒子数组，**原地修改**坐标与速度。
 * @param springs 边列表；端点不在粒子数组中的边会被跳过。
 * @param alpha 当前温度，控制本步允许的位移比例。
 * @returns 衰减后的温度，供下一步传入。
 * @remarks 斥力是 O(n²) 的全对遍历。页面按后端的默认上限取 200 个节点，
 * 收敛一次约 840 万次配对计算，在主线程上是一次约百毫秒的停顿；这个量级下
 * 全对遍历比引入四叉树近似更划算，也少一份可能算错的代码。节点上限若要提到
 * 后端允许的 1000，必须先把收敛挪出主线程。
 */
export function stepLayout(particles: Particle[], springs: readonly Spring[], alpha: number): number {
  const index = new Map<number, Particle>()
  for (const particle of particles) index.set(particle.id, particle)

  // 下标一定在界内，非空断言用来抵消 noUncheckedIndexedAccess 的一刀切；
  // 这里改成运行时判空只会在最内层循环里加一次永远为真的比较。
  for (let i = 0; i < particles.length; i += 1) {
    const a = particles[i]!
    for (let j = i + 1; j < particles.length; j += 1) {
      const b = particles[j]!
      let dx = b.x - a.x
      let dy = b.y - a.y
      let distance = Math.hypot(dx, dy)
      if (distance < MIN_DISTANCE) {
        // 完全重合时方向没有意义，按下标给一个确定的偏向推开，保持可复现。
        dx = distance === 0 ? (i % 2 === 0 ? MIN_DISTANCE : -MIN_DISTANCE) : dx
        dy = distance === 0 ? MIN_DISTANCE : dy
        distance = MIN_DISTANCE
      }
      const force = CHARGE / (distance * distance)
      const fx = (dx / distance) * force
      const fy = (dy / distance) * force
      a.vx -= fx
      a.vy -= fy
      b.vx += fx
      b.vy += fy
    }
  }

  for (const spring of springs) {
    const a = index.get(spring.source)
    const b = index.get(spring.target)
    if (!a || !b) continue
    const dx = b.x - a.x
    const dy = b.y - a.y
    const distance = Math.max(MIN_DISTANCE, Math.hypot(dx, dy))
    const rest = LINK_DISTANCE - LINK_PULL * spring.strength
    const force = (distance - rest) * LINK_STIFFNESS * Math.max(0.15, spring.strength)
    const fx = (dx / distance) * force
    const fy = (dy / distance) * force
    a.vx += fx
    a.vy += fy
    b.vx -= fx
    b.vy -= fy
  }

  for (const particle of particles) {
    if (particle.fixed) {
      // 被拖住的节点仍然对别人施力，但自己不动；残留速度必须清掉，
      // 否则松手瞬间会带着累积的动量弹开。
      particle.vx = 0
      particle.vy = 0
      continue
    }
    particle.vx -= particle.x * CENTER_PULL
    particle.vy -= particle.y * CENTER_PULL
    particle.vx *= DAMPING
    particle.vy *= DAMPING
    const dx = clampStep(particle.vx * alpha)
    const dy = clampStep(particle.vy * alpha)
    particle.x += dx
    particle.y += dy
  }
  return alpha * ALPHA_DECAY
}

/**
 * 把单步位移限制在 :data:`MAX_STEP` 之内。
 *
 * @param value 原始位移。
 * @returns 截断后的位移。
 */
function clampStep(value: number): number {
  if (value > MAX_STEP) return MAX_STEP
  if (value < -MAX_STEP) return -MAX_STEP
  return value
}

/** 一个矩形视口，字段与 SVG viewBox 的四个分量一一对应。 */
export interface ViewBox {
  x: number
  y: number
  width: number
  height: number
}

/**
 * 计算刚好框住全部节点的视口。
 *
 * @param particles 已收敛的粒子数组。
 * @param padding 四周留白（像素），需容纳节点半径与标签。
 * @returns 视口矩形；粒子为空时返回一个居中的默认矩形。
 */
export function fitViewBox(particles: readonly Particle[], padding: number): ViewBox {
  if (particles.length === 0) return { x: -200, y: -150, width: 400, height: 300 }
  let minX = Infinity
  let minY = Infinity
  let maxX = -Infinity
  let maxY = -Infinity
  for (const particle of particles) {
    if (particle.x < minX) minX = particle.x
    if (particle.y < minY) minY = particle.y
    if (particle.x > maxX) maxX = particle.x
    if (particle.y > maxY) maxY = particle.y
  }
  // 单节点或一条直线时宽高会退化为 0，viewBox 宽高为 0 的 SVG 什么都不显示。
  const width = Math.max(maxX - minX, 1) + padding * 2
  const height = Math.max(maxY - minY, 1) + padding * 2
  return { x: minX - padding, y: minY - padding, width, height }
}

/**
 * 收敛一组粒子直到温度耗尽。
 *
 * @param particles 粒子数组，原地修改。
 * @param springs 边列表。
 * @param ticks 迭代次数。
 * @param alpha 起始温度，默认 1。
 * @returns 结束时的温度。
 */
export function settleLayout(
  particles: Particle[],
  springs: readonly Spring[],
  ticks: number,
  alpha = 1,
): number {
  let current = alpha
  for (let i = 0; i < ticks; i += 1) current = stepLayout(particles, springs, current)
  return current
}
