# 匿名安装统计服务端

月璃的匿名统计后端：一个 Cloudflare Worker 加一个 D1 数据库，负责下发安装
标识、接收心跳、向开发者提供聚合数据。客户端在 [`src/core/runtime/telemetry.py`](../src/core/runtime/telemetry.py)。

这一套是可选的。仓库里 `TELEMETRY_ENDPOINT` 为空，整条链路惰性，不部署也
不影响任何功能——部署只在你想统计自己那份分发的装机情况时才需要。

## 为什么要自建域名

`*.workers.dev` 在中国大陆被 DNS 污染：解析直接失败，请求到不了边缘节点，而
服务端侧连一条访问记录都不会留下。结果是装机量只统计到挂了代理的用户，且完全
静默——这正是「有新装机但统计不动」的成因。

因此正式入口是 zone 自己的域名（`wrangler.toml` 里的 `routes`），
`workers_dev` 保留只为兼容 0.1.0–0.1.2 的存量装机：那些版本把 workers.dev 地址
写死在代码里。两者指向同一个 Worker、同一份 D1。

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

### 4. 设置访问令牌

```bash
npx wrangler secret put STATS_TOKEN
npx wrangler secret put UPDATE_TOKEN
```

按提示粘贴两串足够长的随机字符串。`STATS_TOKEN` 是 `/inst` 命令读取聚合数据的凭据，
`UPDATE_TOKEN` 是发布流程写版本号的凭据；两者分开是为了不把读权限与写权限绑在同一个
秘密上。都不要写进 `wrangler.toml`——那个文件会进版本控制。

未设置对应 secret 时 `/stats` 与 `/update/publish` 一律返回 401，不会裸奔。
`UPDATE_TOKEN` 还要填进 GitHub 仓库的 Actions secret `UPDATE_RELAY_TOKEN`，
端点地址填进 `UPDATE_RELAY_URL`（形如 `https://telemetry.yuelibot.org`）。

### 5. 部署

部署前先把 `wrangler.toml` 的 `routes` 改成你自己的域名。`custom_domain = true`
会让 wrangler 自动建 DNS 记录与边缘证书，前提是 zone 在同一账号下；不用这个
字段的话，每次部署都只得到 workers.dev 地址，国内用户依旧统计不到。

```bash
npx wrangler deploy
```

输出里会列出两个入口：`https://yueli-telemetry.<你的子域>.workers.dev` 与
`telemetry.<你的域名> (custom domain)`。免费额度就够，但自定义域走的是境外
节点，绕开的是 DNS 污染，不保证国内直连一定稳定。

### 6. 回填端点

把自定义域地址填进 `src/core/runtime/telemetry.py` 的 `TELEMETRY_ENDPOINT`
常量，workers.dev 地址留给 `TELEMETRY_ENDPOINT_LEGACY`，两者都在
`TELEMETRY_ENDPOINTS` 候选里按顺序尝试。填之前候选为空串时客户端不发任何请求。

不要删掉 `TELEMETRY_ENDPOINT_LEGACY`：已发布版本的端点常量写死在代码里，删掉
等于那些装机的心跳全部落空。

## 端点

| 方法 | 路径 | 鉴权 | 说明 |
| :--- | :--- | :--- | :--- |
| POST | `/register` | 无 | 下发 UUID。不读请求体。同一机房每小时 10 次，超出返回 429 |
| POST | `/heartbeat` | `Client-UUID` 头 | 刷新 `last_seen` 与三个字段。成功 204，未知 UUID 403 |
| GET | `/stats?days=30` | `Authorization: Bearer <STATS_TOKEN>` | 聚合读取，失败一律 401 |
| GET | `/update/latest` | 无 | 已发布的最新版本号，公开信息（tag 本就挂在公开仓库上） |
| POST | `/update/publish` | `Authorization: Bearer <UPDATE_TOKEN>` | 记录最新版本号，由发布流程调用 |

两个入口（自定义域与 workers.dev）指向同一个 Worker、同一份 D1，客户端打哪个
都算同一次上报：身份是服务端下发的 UUID，按 UUID 更新 `last_seen`，不会重复计数。

心跳是单向上报：响应体不回写任何指令或配置，服务端没有控制客户端的通道。

`/heartbeat` 收到 403 时客户端会删除本地身份文件并在下一轮重新注册，因此
清库或重建数据库不会把老客户端永久踢出统计。这一删除动作只在**所有**端点都
认不出该 UUID 之后才执行，避免两个入口指向不同库时反复重注册、把装机量刷高。

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

## 版本广播与发布公告

`GET /update/latest` 与 `POST /update/publish` 只做一件事：把「现在最新是哪个版本」
存进 `release_state` 表的一行。发布流程在 Release 建成后写入，bot 侧读取它来决定
要不要往群里公告新版本（实现见 `src/core/services/update_announce.py`）。

为什么绕这一圈而不是让发布流程直接通知 bot：

- 现象：本机后端 HTTP 与协议端都只监听回环地址，公网无处可推。
- 原因：要让云上的发布流程"推送"进来，必须把其中一个开到公网。
- 后果：于是改成"发布流程写、本机读"。多等几分钟换掉一个对公网开放的可控接口，
  这个交换是有意的——不要为了省那几分钟把端口开出去。

整段功能默认关闭且群号默认为空，并且**不进入配置模板**（见
`bootstrap._feature_document`）：别人的 bot 不改配置就没有群号，也就没有任何东西能被
触发。开启方式是在自己那份 `config/features.toml` 里手写：

```toml
[update_announce]
enabled = true
group = "你的群号"
```

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

## 排查「新装机没进统计」

按下面的顺序看，每一步都能把问题范围砍掉一半：

1. 查 `installs` 最新注册时刻：

   ```bash
   npx wrangler d1 execute yueli-installs --remote --command \
     "SELECT COUNT(*), datetime(MAX(first_seen)/1000,'unixepoch') FROM installs"
   ```

2. 时刻对不上那台机器的安装时间，说明请求没到服务端。服务端不留痕是**预期
   行为**：DNS 解析失败根本产生不了访问日志，所以「这里什么都没有」不等于
   「服务端拒绝了」。

3. 去那台机器的数据目录看两点：有没有 `telemetry.json`（有则身份已下发），
   `logs/app_*.jsonl` 里有没有 `telemetry_failed`。带 `kind` 字段的那条日志
   就是结论——`ConnectError` 是网络到不了，`HTTPStatusError` 才是服务端回的错。

4. 排除客户端后，回到域名：`telemetry.<你的域名>` 与 workers.dev 各解析一次，
   前者必须解析到 zone 的真实 IP。若对方网络连自定义域也不通，那就不是配置
   问题，得换国内可达的中转。
