# 匿名安装统计服务端

月璃的匿名统计后端：一个 Cloudflare Worker 加一个 D1 数据库，负责下发安装
标识、接收心跳、向开发者提供聚合数据。客户端在 [`src/core/runtime/telemetry.py`](../src/core/runtime/telemetry.py)。

这一套是可选的。仓库里 `TELEMETRY_ENDPOINT` 为空，整条链路惰性，不部署也
不影响任何功能——部署只在你想统计自己那份分发的装机情况时才需要。

## 数据边界

`installs` 表里没有 IP 列，也没有任何可回溯到人的列。这不是承诺而是结构
约束：没有那一列，就无处可存。Worker 代码全文不读取请求方 IP，连注册限流
都只用机房标识做内存计数，不落库、不写日志。

一条记录的全部内容是：一个服务端生成的随机 UUID、首次与最近出现的时间戳、
应用版本号、操作系统类型、Python 版本号。

## 目录

| 文件 | 内容 |
| :--- | :--- |
| `src/index.js` | Worker 全部逻辑：三个路由与一个定时任务 |
| `schema.sql` | 两张表与一个索引 |
| `wrangler.toml` | 名称、D1 绑定、Cron 表达式 |

用 JavaScript 而非 TypeScript 是有意的：仓库根 `tsconfig.json` 的 `include`
不覆盖这个目录，写成 `.ts` 会是「看着有类型、其实没进 typecheck」的状态。

## 部署

需要一个 Cloudflare 账号，免费额度足够。以下命令都在本目录下执行。

### 1. 登录

```bash
npx wrangler login
```

### 2. 创建 D1 数据库

```bash
npx wrangler d1 create yueli-installs
```

把输出里的 `database_id` 填进 `wrangler.toml` 的 `[[d1_databases]]`。
仓库中已填了一个 id，换成你自己的。

### 3. 建表

```bash
npx wrangler d1 execute yueli-installs --remote --file=./schema.sql
```

### 4. 设置 `/stats` 的访问令牌

```bash
npx wrangler secret put STATS_TOKEN
```

按提示粘贴一串足够长的随机字符串。这是 `/inst` 命令读取聚合数据的凭据，
不要写进 `wrangler.toml`——那个文件会进版本控制。

未设置该 secret 时 `/stats` 一律返回 401，不会裸奔。

### 5. 部署

```bash
npx wrangler deploy
```

输出里会给出 `https://yueli-telemetry.<你的子域>.workers.dev`。

### 6. 回填端点

把上一步的地址填进 `src/core/runtime/telemetry.py` 的 `TELEMETRY_ENDPOINT`
常量。填之前它是空串，客户端不发任何请求。

## 端点

| 方法 | 路径 | 鉴权 | 说明 |
| :--- | :--- | :--- | :--- |
| POST | `/register` | 无 | 下发 UUID。不读请求体。同一机房每小时 10 次，超出返回 429 |
| POST | `/heartbeat` | `Client-UUID` 头 | 刷新 `last_seen` 与三个字段。成功 204，未知 UUID 403 |
| GET | `/stats?days=30` | `Authorization: Bearer <STATS_TOKEN>` | 聚合读取，失败一律 401 |

心跳是单向上报：响应体不回写任何指令或配置，服务端没有控制客户端的通道。

`/heartbeat` 收到 403 时客户端会删除本地身份文件并在下一轮重新注册，因此
清库或重建数据库不会把老客户端永久踢出统计。

### `GET /stats` 的返回

```json
{
  "installs": 1024,
  "online": 312,
  "versions": [{ "version": "1.0.0", "count": 280 }],
  "daily": [
    { "day": "2026-09-05", "installs": 1000, "online": 305,
      "versions": { "1.0.0": 275 } }
  ]
}
```

`versions` 与 `daily[].versions` 都只统计存活实例（最近 24 小时内有心跳）。
按注册总数统计的话，旧版本计数只增不减，图上会是一堆永不下降的线，而这张
图要回答的恰恰是「谁还在用」。版本条目超过 10 条时，长尾合并为「其它」。

`days` 缺省 30，取值夹在 1 到 365 之间。

## 日快照

`installs` 表只有「此刻」，画不出趋势。Cron 每日 UTC 00:05 跑一次，把当天的
三项聚合写成 `daily_stats` 的一行，折线图的历史维度全靠它。按 `day` 主键
覆盖，重复触发或手动补跑都不会产生重复行。

手动触发一次（本地开发时）：

```bash
npx wrangler dev --test-scheduled
```

然后访问 `http://localhost:8787/__scheduled`。

## 运维

查看当前数据：

```bash
npx wrangler d1 execute yueli-installs --remote --command "SELECT COUNT(*) FROM installs"
```

查看最近的快照：

```bash
npx wrangler d1 execute yueli-installs --remote --command "SELECT * FROM daily_stats ORDER BY day DESC LIMIT 7"
```

实时日志：

```bash
npx wrangler tail
```
