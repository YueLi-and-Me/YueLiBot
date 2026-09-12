/**
 * 月璃文档站的 Worker 入口。
 *
 * 所属模块：文档站发布链路，与仓库根的 wrangler.toml、zensical.toml 同属一组配置。
 *
 * 核心职责：
 * 1. 把 APEX 根域 yuelibot.org 的请求 301 跳转到 docs.yuelibot.org，并保留原路径与查询串；
 * 2. 其余请求（含 docs 子域与 workers.dev 备用入口）原样交给静态资源绑定处理。
 *
 * 依赖关系：
 * - 静态资源由 wrangler.toml 的 [assets] 声明，目录为构建产物 site/；
 * - 本文件由 wrangler.toml 顶层的 main 指向，且 [assets].run_worker_first 为 ["/*"] 时生效，
 *   即每个请求都会先进到这里，再由下面的 ASSETS 绑定取静态文件。
 *
 * 为什么需要这个文件：docs 子域的 custom domain 只签发了 docs.yuelibot.org 一张证书，
 * APEX 没有证书也没有落地页，访问根域会在 TLS 握手阶段直接失败（curl 报 SSL 错误）。
 * 搜索引擎抓到根域的证书错误属于负面信号，同时用户手输域名会看到浏览器报错。
 *
 * 定态说明：本 Worker 的常态是「docs 子域承载文档站、apex 只做入口跳转」，apex 不承载内容。
 * 如果将来要做独立官网，官网就是唯一能与文档站争用 yuelibot.org 的角色，届时需要同时处理：
 * 1. 本文件去掉或收窄跳转，把根域交给官网，并决定官网与文档站的路径分工；
 * 2. wrangler.toml 的 routes 放开 yuelibot.org，让官网那份配置去声明这个 custom domain，
 *    同一个主机名由两份配置各自当作自己的 custom domain 部署会失败。
 *
 * 类型说明：本文件不参与 tsc 与 vitest，仓库也未引入 Workers 的类型包，因此用 JSDoc
 * 就地声明最小结构，避免为一个入口文件牵入新的类型依赖。
 */

/**
 * 跳转目标，域名与 zensical.toml 的 site_url 保持一致，两处必须同改。
 *
 * 规范地址取子域而不是根域：文档站是用户实际访问的主入口，但它的规范位置定在 docs 子域，
 * 根域只做入口跳转。这样 canonical 与 sitemap 都指向真正提供内容的那个主机名，
 * 根域也不会被搜索引擎当成文档站的第二个副本。
 */
const CANONICAL_ORIGIN = "https://docs.yuelibot.org";

/** 需要跳转到文档子域的裸域名，带 www 的写法一并收归，避免两份入口各漏一半。 */
const REDIRECT_HOSTS = new Set(["yuelibot.org", "www.yuelibot.org"]);

/**
 * 判断请求是否需要跳转到文档子域。
 *
 * @param {string} hostname 请求的 host，大小写不敏感，这里统一转小写后比较。
 * @returns {boolean} 裸域返回 true，docs 子域与 workers.dev 入口返回 false。
 */
function shouldRedirect(hostname) {
  return REDIRECT_HOSTS.has(hostname.toLowerCase());
}

export default {
  /**
   * Worker 的请求入口。
   *
   * @param {Request} request 边缘收到的原始请求。
   * @param {{ ASSETS: { fetch: (req: Request) => Promise<Response> } }} env
   *        运行时绑定，ASSETS 来自 wrangler.toml 的 [assets].binding。
   * @returns {Promise<Response>} 裸域返回 301，其余返回静态资源的响应。
   */
  async fetch(request, env) {
    const url = new URL(request.url);

    if (shouldRedirect(url.hostname)) {
      // 保留 pathname 与 search 再跳转。
      //
      // 原因：apex 是文档站的短入口，用户会按 https://yuelibot.org/manual/ 这样的完整路径
      // 分享它。若一律跳到子域首页，这些链接的锚点会全部失效，
      // 搜索引擎也会把旧路径记成 301 到不相关页面。
      const target = new URL(CANONICAL_ORIGIN);
      target.pathname = url.pathname;
      target.search = url.search;
      return Response.redirect(target.toString(), 301);
    }

    // 交给静态资源绑定按 html_handling 与 not_found_handling 处理。
    //
    // 不能写成 fetch(url) 或 fetch(request.url)：那样会走一次真实的对外 HTTP 请求，
    // 请求打回同一个 Worker 就会自递归。ASSETS 是服务绑定，只按路径取资源，不经过网络。
    return env.ASSETS.fetch(request);
  },
};
