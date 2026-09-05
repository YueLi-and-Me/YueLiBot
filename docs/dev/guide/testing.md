# 开发与验证

常用命令、四条状态门的口径，以及无头自检能证明什么。

| 命令 | 作用 |
| :--- | :--- |
| `uv run bot.py` | 正常启动（进程入口，按桌宠开关决定要不要拉外壳） |
| `npm run dev` | 只起桌宠外壳；连接已经在跑的后端，配合入口的 `--no-shell` 使用 |
| `npm run dev:renderer` | 只起渲染层（浏览器里调画面，比重启 Electron 快得多） |
| `npm run dev:webui` | 只起管理面板前端，需要后端已经在跑 |
| `npm run build` | 生产构建 |
| `npm run selftest` | **无头自检**，8 段断言 |
| `npm run typecheck` | 类型检查 |
| `npm test` | Electron 端单元测试 |
| `uv run pytest pytests/ -q` | Python 后端测试，业务逻辑主要在这边 |
| `npm run test:integration` | 真实拉起 Python 后端的集成测试。**手动验收项**，不在任何默认门里 |
| `npm run sprite:*` | 生图管线，见[生图管线](../../manual/features/sprite.md) |
| `uv run python pytests/boot_probe.py` | 端到端启动探针。拉真进程验证启动链路，**不在默认门里** |

## 四条状态门

改动的验收标准是四条门全绿：

| 门 | 命令 | 它能拦住什么 |
| :--- | :--- | :--- |
| Python 测试 | `uv run pytest -q` | 业务逻辑的绝大部分，1700 余条 |
| 类型检查 | `npm run typecheck` | TypeScript 侧的类型错误 |
| 前端测试 | `npm test` | Electron 主进程与渲染层的单元行为 |
| 生产构建 | `npm run build` | 「类型过了但打包塌了」——`tsc --noEmit` 拦不住这一类 |

前三条由 CI 在每次 push 与 PR 上自动执行（见 `.github/workflows/ci.yml`），
另加一条跨语言的配置对齐校验：用 Electron 写入器导出全默认配置，与 Python schema
逐字段比对，漂移时红。新增或退休配置字段时忘了同步两侧，只有这条门拦得住。

CI 跑在 `windows-latest` 上。桌宠与协议端都在 Windows 运行，路径分隔符、文件锁与
控制台编码的差异只在这个平台暴露；换 Linux 会让一批真实问题跑不出来。

> [!NOTE]
> 四条门都绿也证明不了「启动之后活着」。`pytest` 测不出「startup 钩子不返回」
> 这类死锁——那会让其后的服务全部起不来，而进程看上去一切正常。地基级改动
> （数据库迁移、记忆分区、后台服务的生命周期）之后，务必再跑一次
> `uv run python pytests/boot_probe.py`：它拉真进程、占真端口，确认日志走到
> `YUELI_READY=1`。这一项不在默认门里，因为一次要十几秒。

## 无头自检

「进程还活着」不等于「功能正常」——白窗口、preload 静默失败、素材 404 全都表现为进程正常运行。所以有一套无头自检直接问程序要证据：

```bash
npm run build && npm run selftest
```

```
SELFTEST           画布真的画出了东西
SELFTEST-WINDOW    可聚焦、可移动、置顶、拖动不撑大窗口
SELFTEST-HIT       输入栏隐藏时不挡点击、展开时点得到
SELFTEST-TRAY      显隐、搬回原位、图标加载
SELFTEST-CHAT      真实生成一轮，验记忆落库与人格推进
SELFTEST-REFLECT   做梦 → 排队 → 到点讲出来
SELFTEST-AWARE     感知可用，且原始窗口标题没有外泄
SELFTEST-DIARY     日记窗口渲染，兼验生产多入口
```

中文结果会写进 `YUELI_SELFTEST_OUT` 指定的 UTF-8 文件——Windows 控制台按本地代码页解码，中文在管道里就已经坏掉，事后无法还原。
