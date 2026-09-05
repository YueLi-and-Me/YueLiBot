# 生图管线

从一张参考图跑出一整套角色素材：1 张定稿底图 + 16 张表情 + 4 张闭眼 + 3 张嘴型 + 一份 manifest，
最终供桌宠窗口渲染。你只需做两件事：**定稿底图、看图挑好坏**，不接触提示词工程。

```bash
npm run sprite:test                                        # 冒烟测试：先验证 key、网络、模型可用
npx tsx scripts/sprite/base.ts --ref <参考图> --desc "<角色描述>"
npx tsx scripts/sprite/base.ts --pick 2                    # 定稿底图
npm run sprite:gen                                         # 16 表情 + 4 闭眼 + 3 嘴型
npm run sprite:preview                                     # 逐张 diff 挑图
npm run sprite:process                                     # 抠图 + 对齐 + 烘焙 + manifest
```

> Windows 下 `npm run xxx -- --flag` 会被 npm 吞掉参数，带参数一律用 `npx tsx` 直调。

## 跑之前需要什么

- 在 `.env` 里备好一家图像厂商（模板见 `.env.example`）：
  - `SPRITE_PROVIDER=gemini`（默认）：`GEMINI_API_KEY` 必填，可用 `GEMINI_IMAGE_MODEL`、`GEMINI_BASE_URL` 覆盖。
    注意 Gemini 图像模型的免费层额度为 0，需要开通付费层。
  - `SPRITE_PROVIDER=seedream`（别名 `ark`，火山引擎方舟）：`ARK_API_KEY` 必填，
    可用 `ARK_IMAGE_MODEL`、`ARK_BASE_URL`、`ARK_IMAGE_SIZE`（默认竖幅 1440x2160）覆盖。
  - 走代理用 `HTTPS_PROXY`；单次请求超时 `SPRITE_TIMEOUT_MS`（默认 300000 毫秒）。
- 后处理需要 rembg 命令行：`pip install "rembg[cli]" onnxruntime`，首次运行要下载约 170 MB 模型。
  装不上也不是死路，`process` 支持 `--naive` 降级为近白阈值抠图。
- 角色名用 `--name` 指定，所有脚本默认 `yueli`。

## 各步在做什么

**`sprite:test` 冒烟**：先文生图一张测试图，再以它为底图做一次指令编辑。
验证密钥、网络、模型、响应解析四件事，并检查编辑是否改动画布尺寸（改了会摧毁后续对齐前提）。
产物在 `scratch/`，与正式素材无关。失败时按输出提示排查；`SPRITE_PROVIDER=gemini` 时可再跑
`npm run sprite:diagnose` 分步定位代理、密钥、模型列表。

**`base` 底图候选**：`--ref` 给参考图（可重复传多张）、`--desc` 必填角色描述、`--count` 候选张数（1~12，默认 4）。
候选图写到工作目录。注意本步**不幂等**：每次生成都重新请求并按编号覆盖候选，重复跑会重复计费。
不给 `--ref` 只警告不拦截。

**定稿（人工介入点）**：从候选里挑一张，`npx tsx scripts/sprite/base.ts --pick 2` 把它复制为定稿 `base.png`。
纯本地复制，可反复换。

**`gen` 批量差分**：以定稿为底跑 23 个编辑任务——16 张表情、4 张闭眼（normal/happy/smile/shy 对应的眨眼帧）、
3 张嘴型（闭/半开/开）。并发默认 2（上限 6）；遇到限流或网络错误按指数退避重试。
单张失败不阻断同阶段其它条目，失败清单在结尾汇总。

**`preview` 挑图（人工介入点）**：起本机预览页（默认端口 5178），逐张与原图比对：
原图、差异、闪烁、并排四种查看模式；差异模式先补偿位移再逐像素比，身体或腿部改动超过阈值会判为「真问题」。
按 `X` 标记重跑并选预设原因，`Enter` 保存到 `review.json`。纯本地，随意重开。

**`process` 后处理**：纯本地转换，整体可反复重跑。三段：rembg 抠透明背景；
按 alpha 包围盒做对齐（等比缩放归一 + 头顶中心锚点对齐到统一画布，并输出一致性报告）；
检测眼/嘴/脸区域做「脸部烘焙」并写出 manifest。自动检测脸部区域失败时，用
`--face/--eyes/--mouth x,y,w,h` 手填坐标重跑。

**`sprite:fixture` 自检**：生成合成素材（其中一张故意加宽身体），配合 `sprite:process:fixture`、
`sprite:preview:fixture` 走通后处理与预览，不烧 API 配额。验证管线改动时用。

## 产物落在哪

- 中间产物（白底原图、候选、进度）：`.sprite-work/<name>/`，已被 git 忽略，刻意放在 assets 之外避免打进构建。
  其中 `state.json` 记录已完成条目（断点续跑依据），`review.json` 记录你标记的重跑清单。
- 最终产物（透明背景、已对齐）：`assets/character/<name>/`——`base.png`、`face/`（16 张）、
  `eyes/`（4 张）、`mouth/`（3 张）、`manifest.json`。
- manifest 记录画布尺寸、对齐锚点、每张素材的文件位置与脸部区域矩形；桌宠渲染层按它做局部合成（表情整图加眼/嘴矩形覆盖，运行时自动眨眼）。

## 失败了怎么重跑

- `gen` 断点续跑：每完成一项立即写 `state.json`，重跑时跳过已完成且文件存在的条目，不重复计费。
  直接 `npm run sprite:gen` 补缺；`--only face/shy,face/cry` 只跑指定条目；`--force` 全量重跑；
  你在 preview 里标记的条目会自动强制重跑（带强化指令）。
- 内容审核拦截（blocked）不会自愈：换底图重跑 base，并在 `--desc` 里写清服装。
- `base` 的 `--pick`、`preview`、`process` 都是纯本地操作，随意重跑；`base` 生成本身会重新计费，想清楚再跑。

更多参数与实现细节见 [`scripts/sprite/README.md`](../../../scripts/sprite/README.md) 与[开发手册·scripts](../../dev/modules/scripts.md)。
