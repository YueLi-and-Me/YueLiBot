# 出站图片字节传输验收执行说明

本说明供部署与真机验收人员执行。代码基线为 `9df80f6`，交付分支为
`codex/outbound-image-bytes`。本地机检不能替代本说明中的 QQ 真机验收。

## 变更与判据

表情包和普通图片在出站组装时读取本地源文件，编码为 OneBot
`data.file=base64://…`。表情包保留 `sub_type`，普通图片不带 `sub_type`。
库内 `emoji.send_ref` 仍为 `file://` 定位引用，配置和数据库版本均不变。

单张源文件上限为 `5 * 1024 * 1024 = 5242880` 字节，恰好等于上限允许发送。
当前现场表情最大约 4825 KB，图表最大 18 KB，该上限覆盖现有素材。
超限、缺失、无权限、非常规文件或空文件会中止本条全部发送批次，包括此前排列的文字。
后续独立消息仍继续处理。此保证针对发送前的文件准备；协议端在实际发送某一批次时
拒绝请求，仍按既有逐批发送行为处理。

核心验收判据：**去掉协议端容器中表情目录和图表目录的两个 bind mount，重建容器后，
群聊与私聊均能收到表情包和 `/inst` 图表，图片相关 `retcode=100` 计数为 0。**
还须确认没有其他挂载覆盖这些目录，不能仅凭启动参数少了两行即判定通过。

## 执行步骤

1. 保持现有两个只读挂载，记录当前部署提交和容器名称。按下表发送一轮基线消息，
   记录每次触发时间、会话、素材、是否实际收到图片和失败次数。
2. 按既有部署流程部署交付分支的提交并重启主体服务，记录实际部署的提交号。
   依赖未变化，无需新增配置字段或执行本包数据库迁移。
3. 在协议端容器的持久启动定义中删除下面两个挂载，使用原有镜像、网络、账号、
   协议配置和登录数据重建该容器。若使用 Compose，修改对应服务的 `volumes` 后执行
   `docker compose up -d --force-recreate <协议端服务名>`；若使用 `docker run`，
   按原部署命令重建，只删除这两个 `-v` 参数。不要删除登录数据卷。

   ```text
   /opt/yueli/data/emojis:/opt/yueli/data/emojis:ro
   /opt/yueli/data/charts:/opt/yueli/data/charts:ro
   ```

4. 使用下面的只读检查确认挂载确实不存在。输出中也不能有覆盖
   `/opt/yueli`、`/opt/yueli/data` 的父目录挂载。主体或实际运行 Python 适配器的
   进程仍须可读源文件；本次去掉的是协议端容器的素材挂载。

   ```bash
   protocol_container='<填写协议端容器名>'
   docker inspect --format '{{range .Mounts}}{{println .Destination}}{{end}}' "$protocol_container"
   ```

5. 等待主体与协议端重新连接，按与基线相同的会话、素材和触发方式重复下表。
   测试人员必须查看接收方 QQ 客户端，确认图片可打开、动图可正常呈现、图表不是贴纸。
   模型没有选出表情、`/inst` 没有生成图表或消息未进入出站流程，均记为“未完成”，
   不得计入成功样本。
6. 保存两个阶段各自的日志时间窗和结果表，按后文命令计数并回贴。不得以
   `verify_integrity()` 全绿或纯文字送达替代图片验收。

## 必发消息

| 会话 | 触发消息与素材 | 成功判据 |
| --- | --- | --- |
| 已准入测试群 | 让 Bot 实际发送一张库内表情，至少覆盖事故中的 GIF；追加现有最大表情样本 | QQ 收到对应表情，GIF 正常呈现；出站图片段保留原 `sub_type` |
| 已准入私聊 | 同样触发库内表情发送 | QQ 收到表情，发送 action 为 `send_private_msg` |
| 已准入测试群 | 使用具备开发者命令权限的账号发送 `/inst` | 收到命令正文及实际生成的普通图表，包括 `inst-n.png`；图表可正常打开 |
| 已准入私聊 | 同一账号发送 `/inst` | 同样收到正文及实际生成的图表；普通图片不携带 `sub_type` |

`/inst` 可能按可用统计生成多张图，逐张记录实际应发送数和收到数；若现场仅输出文字，
先核对图表依赖和统计数据是否满足现有命令条件，再重做该项。基线与变更后使用同一素材，
尤其要覆盖此前失败的表情和图表路径。最大样本以现场真实文件大小为准，不按文件名猜测。

## 日志与成功响应

**当前运行器与传输层没有“每张图发送成功”的日志事件，本包也没有新增该事件。**
因此不能承诺在 `journalctl -u yueli` 中看到 `send_group_msg retcode=0` 之类的成功行。
该日志用于检查连接状态、准备失败和协议端发送失败，正向成功以 QQ 实际收图为准。
如果现场已有协议端 action 响应采集，可同时保留下列成功响应证据：

| 类型 | 请求与成功响应示意（非本包新增日志） |
| --- | --- |
| 群聊表情 | `action=send_group_msg`；图片段 `file=base64://…`、`sub_type=<原值>`；对应 echo 的响应为 `status=ok, retcode=0, data.message_id=<实际编号>` |
| 私聊表情 | `action=send_private_msg`；其余同上 |
| 群聊 `/inst` 图片 | `action=send_group_msg`；图片段 `file=base64://…`、无 `sub_type`；对应响应为 `status=ok, retcode=0, data.message_id=<实际编号>` |
| 私聊 `/inst` 图片 | `action=send_private_msg`；其余同上 |

以上是采集时应核对的协议字段，不能当成已经出现过的日志。取证只需保留 action、echo、
段类型、来源前缀、子类型、响应状态和消息编号，勿复制完整 base64 图片或认证内容。

本包复用以下既有事件，**新增日志事件数为 0**，也没有新增 logger 模块：

| 事件 | 等级 | 字段 | 本次取证用途 |
| --- | --- | --- | --- |
| `QQ 消息发送失败` | ERROR | `streamId`、`streamKind`、`targetId`、`error` | 图片准备失败或协议端拒绝时产生；本地准备错误的 `error` 含路径、原因，超限还含实际源字节数与上限 |
| `QQ 投递失败回报未送达主体` | WARNING | `streamId`、`action`、`error` | 适配器向主体回报失败又未送达时产生；正常验收不应出现 |

超限时 `error` 的示意如下，路径与字节数以实际值为准：

```text
action prepare_image 失败：status='local_error' retcode=None，原因：出站图片准备失败：路径=<源文件路径>；实际字节数=5242881；上限=5242880
```

`prepare_image` 表示本地准备阶段，不是已发给协议端的 action；`local_error` 是本地错误
上下文，不是协议端返回状态。此时没有协议端返回码，显示为 `retcode=None`，不会假冒
现场的 `retcode=100`。底层读取错误保留原始异常链，`error` 中还会出现
`FileNotFoundError` 或 `PermissionError` 及具体原因。

## 计数与失败判定

每轮开始和结束时分别记录时间，填写下面两个变量。日志文件写到系统临时目录。
以下命令在部署主机执行；如服务日志由独立适配器 unit 接管，还须对该 unit 采集同一时间窗。

```bash
start_time='<本轮开始时间，例如 YYYY-MM-DD HH:MM:SS>'
end_time='<本轮结束时间，例如 YYYY-MM-DD HH:MM:SS>'
evidence_dir=$(mktemp -d)
journalctl -u yueli --since "$start_time" --until "$end_time" --no-pager -o cat > "$evidence_dir/yueli.log"
python3 - "$evidence_dir/yueli.log" <<'PY'
from pathlib import Path
import re
import sys

lines = Path(sys.argv[1]).read_text(encoding='utf-8').splitlines()
pattern = re.compile(r'''retcode\s*["']?\s*[=:]\s*["']?100\b''')
print('retcode_100_lines=', sum(bool(pattern.search(line)) for line in lines))
print('local_image_failure_lines=', sum('出站图片准备失败' in line for line in lines))
print('send_failure_lines=', sum('QQ 消息发送失败' in line for line in lines))
PY
```

计数单位是日志行，不能直接当作失败请求数；多行日志须结合时间、会话和上下文归因。
专用测试窗口中三项均应为 0。如有其他会话噪声，必须附逐条归因记录，保留原始计数。
若协议端另有日志，也应保留同一时间窗内的 `send_group_msg`、`send_private_msg`
响应证据，防止错误仅出现在容器侧。

以下任一项即为失败：两个挂载或覆盖它们的父目录挂载仍存在；应发图片未收到；
只收到正文；普通图表变成表情；图片相关 `retcode=100`、`ENOENT stat`、准备失败、
超时或其他发送错误出现。即使 `retcode=100` 为 0，只要缺少应发图片也不能通过。

回贴结果至少包含：基线与部署提交号、去挂载后的 Mounts 目的路径清单、两轮时间窗、
每类消息的应发图数/实收图数、三个日志计数、异常明细，以及接收方确认。
