# 生图管线

从一张参考图跑出整套角色素材的流程。你只需定稿底图和挑图，不接触提示词工程。

从参考图跑出一整套角色素材。**你只需做两件事：定稿底图、看图挑好坏**，不接触提示词工程。

```bash
npm run sprite:test                                        # 冒烟测试
npx tsx scripts/sprite/base.ts --ref <参考图> --desc "<角色描述>"
npx tsx scripts/sprite/base.ts --pick 2                    # 定稿底图
npm run sprite:gen                                         # 16 表情 + 4 闭眼 + 3 嘴型
npm run sprite:preview                                     # 逐张 diff 挑图
npm run sprite:process                                     # 抠图 + 对齐 + 烘焙 + manifest
```

详见 [`scripts/sprite/README.md`](../../../scripts/sprite/README.md)。

> Windows 下 `npm run xxx -- --flag` 会被 npm 吞掉参数，带参数一律用 `npx tsx` 直调。
