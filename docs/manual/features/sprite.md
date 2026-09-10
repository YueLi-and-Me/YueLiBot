# 生图管线

生图管线从一张参考图生成整套角色素材：1 张定稿底图 + 16 张表情 + 4 张闭眼 + 3 张嘴型 + 一份 manifest，
最终供桌宠窗口渲染。使用者只需完成两处人工操作：定稿底图、审图标记，不涉及提示词工程。

```bash
npm run sprite:test                                        # 冒烟测试：验证密钥、网络、模型可用
npx tsx scripts/sprite/base.ts --ref <参考图> --desc "<角色描述>"
npx tsx scripts/sprite/base.ts --pick 2                    # 定稿底图
npm run sprite:gen                                         # 16 表情 + 4 闭眼 + 3 嘴型
npm run sprite:preview                                     # 逐张 diff 审图
npm run sprite:process                                     # 抠图 + 对齐 + 烘焙 + manifest
```

> Windows 下 `npm run xxx -- --flag` 的参数会被 npm 吞掉，带参数的命令一律使用 `npx tsx` 直调。

## 运行前提

- 在 `.env` 中配置一家图像厂商（模板见 `.env.example`）：
  - `SPRITE_PROVIDER=gemini`（默认）：`GEMINI_API_KEY` 必填，可用 `GEMINI_IMAGE_MODEL`、`GEMINI_BASE_URL` 覆盖。
    注意 Gemini 图像模型的免费层额度为 0，需开通付费层。
  - `SPRITE_PROVIDER=seedream`（别名 `ark`，火山引擎方舟）：`ARK_API_KEY` 必填，
    可用 `ARK_IMAGE_MODEL`、`ARK_BASE_URL`、`ARK_IMAGE_SIZE`（默认竖幅 1440x2160）覆盖。
  - 代理使用 `HTTPS_PROXY`；单次请求超时 `SPRITE_TIMEOUT_MS`（默认 300000 毫秒）。
- 后处理依赖 rembg 命令行：`pip install "rembg[cli]" onnxruntime`，首次运行需下载约 170 MB 模型。
  无法安装时可使用 `process --naive` 降级为近白阈值抠图。
- 角色名通过 `--name` 指定，所有脚本默认为 `yueli`。

## 各步骤的职责

**`sprite:test` 冒烟**：先文生图一张测试图，再以它为底图执行一次指令编辑。
验证密钥、网络、模型、响应解析四项，并检查编辑是否改动画布尺寸（尺寸变化会破坏后续对齐前提）。
产物位于 `scratch/`，与正式素材无关。失败时按输出提示排查；`SPRITE_PROVIDER=gemini` 时可再执行
`npm run sprite:diagnose` 分步定位代理、密钥、模型列表问题。

**`base` 底图候选**：`--ref` 传入参考图（可重复传入多张）、`--desc` 必填角色描述、
`--count` 指定候选张数（1~12，默认 4），候选写入工作目录。本步骤不幂等：
每次执行都重新请求并按编号覆盖候选，重复执行将重复计费。不传 `--ref` 仅警告、不拦截。

**定稿（人工操作）**：从候选中选定一张，执行 `npx tsx scripts/sprite/base.ts --pick 2`
将其复制为定稿 `base.png`。纯本地复制，可反复更换。

**`gen` 批量差分**：以定稿为底执行 23 个编辑任务——16 张表情、4 张闭眼（normal/happy/smile/shy
对应的眨眼帧）、3 张嘴型（闭合、半开、张开）。并发默认 2（上限 6）；限流或网络错误按指数退避重试。
单张失败不阻断同阶段其它条目，失败清单在结尾汇总。

**`preview` 审图（人工操作）**：启动本机预览页（默认端口 5178），逐张与原图比对，
提供原图、差异、闪烁、并排四种查看模式；差异模式先补偿位移再逐像素比较，
身体或腿部改动超过阈值判定为「真问题」。按 `X` 标记重跑并选择预设原因，`Enter` 保存至 `review.json`。
纯本地操作，可反复打开。

**`process` 后处理**：纯本地转换，可整体反复执行。三段处理：rembg 抠取透明背景；
按 alpha 包围盒对齐（等比缩放归一，头顶中心锚点对齐到统一画布，并输出一致性报告）；
检测眼、嘴、脸区域执行「脸部烘焙」并写出 manifest。脸部区域自动检测失败时，
以 `--face/--eyes/--mouth x,y,w,h` 手动指定坐标后重跑。

**`sprite:fixture` 自检**：生成合成素材（其中一张故意加宽身体），配合 `sprite:process:fixture`、
`sprite:preview:fixture` 走通后处理与预览流程，不消耗 API 配额。验证管线改动时使用。

## 产物位置

- 中间产物（白底原图、候选、进度）：`.sprite-work/<name>/`，已被 git 忽略，
  刻意置于 assets 之外以避免打入构建产物。其中 `state.json` 记录已完成条目（断点续跑依据），
  `review.json` 记录审图标记的重跑清单。
- 最终产物（透明背景、已对齐）：`assets/character/<name>/`，含 `base.png`、`face/`（16 张）、
  `eyes/`（4 张）、`mouth/`（3 张）、`manifest.json`。
- manifest 记录画布尺寸、对齐锚点、各素材的文件位置与脸部区域矩形；
  桌宠渲染层据此执行局部合成（表情整图叠加眼、嘴矩形覆盖，运行时自动眨眼）。

## 失败后的重跑

- `gen` 断点续跑：每完成一项立即写入 `state.json`；重跑时跳过已完成且文件存在的条目，不重复计费。
  直接 `npm run sprite:gen` 补齐缺失；`--only face/shy,face/cry` 仅执行指定条目；`--force` 全量重跑；
  在 preview 中标记的条目会自动强制重跑（带强化指令）。
- 内容审核拦截（blocked）不会自行恢复：更换底图后重跑 base，并在 `--desc` 中写明服装。
- `base --pick`、`preview`、`process` 均为纯本地操作，可随意重跑；
  `base` 的生成本身按次计费，执行前确认参数。

更多参数与实现细节见 [`scripts/sprite/README.md`](https://github.com/YueLi-and-Me/YueLiBot/blob/main/scripts/sprite/README.md) 与[开发手册·scripts](../../dev/modules/scripts.md)。
