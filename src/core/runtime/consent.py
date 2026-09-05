"""首次启动的用户协议同意闸门。

本模块在任何配置生成、数据库创建之前拦一道：没同意过就不启动。同意记录落在
数据目录的 ``consent.json``，只记版本与时间戳，不记任何身份信息。

为什么必须拦在最前面：协议要讲的是「这个程序会替你存别人的什么、会把内容发给
谁、以及接入 QQ 的账号风险」。等配置生成完、数据库建好再问，用户已经在毫不知情
的情况下让程序动了盘。

无头场景是本模块最容易出错的地方，见 :func:`require_consent` 的说明。

被 ``src.main`` 在解析完命令行之后、创建配置目录之前调用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import json
import sys

from src.core.logging.console_layout import print_box
from src.core.runtime.clock import now as current_time

# 同意记录的文件名，落在数据目录下。
CONSENT_FILENAME = 'consent.json'
# 协议版本。协议正文有实质改动时加一，已同意过旧版本的用户会被重新询问；
# 错别字与排版调整不要动它，否则每次改文案都在打扰所有人。
AGREEMENT_VERSION = 1
# 协议正文相对仓库根的位置。
AGREEMENT_FILENAME = 'AGREEMENT.md'
# 必须逐字输入的同意词。不接受 y / yes / 回车：那些是随手一按就会发生的动作，
# 而这份协议要确认的是「你知道自己在替别人存数据」。
CONSENT_WORD = '同意'
# 拒绝时的退出码，与配置未填完保持一致，便于 systemd 之类按码区分。
_REFUSED_EXIT_CODE = 1


def consent_path(data_dir: Path) -> Path:
    """给出同意记录文件的路径。

    :param data_dir: 运行时数据目录。
    :return: ``<data_dir>/consent.json`` 的路径；文件是否存在不在此校验。
    """
    return data_dir / CONSENT_FILENAME


def read_consent(data_dir: Path) -> Dict[str, Any] | None:
    """读取已有的同意记录。

    :param data_dir: 运行时数据目录。
    :return: 记录字典；文件不存在或内容不是合法 JSON 对象时返回 ``None``。
        损坏的记录按「没同意过」处理而不是报错——重新问一次的代价远小于
        让用户对着一个自己看不懂的解析错误卡住。
    """
    path = consent_path(data_dir)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return None
    return document if isinstance(document, dict) else None


def has_consented(data_dir: Path) -> bool:
    """判断当前协议版本是否已被同意。

    :param data_dir: 运行时数据目录。
    :return: 记录存在且版本不低于当前协议版本时为真。
    """
    document = read_consent(data_dir)
    if document is None:
        return False
    version = document.get('version')
    return isinstance(version, int) and version >= AGREEMENT_VERSION


def record_consent(data_dir: Path, *, channel: str) -> Path:
    """写入同意记录。

    只记版本、时间戳与同意渠道，不记任何身份信息——这份文件的用途仅仅是
    「别再问第二遍」，不是凭证。

    :param data_dir: 运行时数据目录；不存在时递归创建。
    :param channel: 同意渠道，``console`` 或 ``flag``，仅供排查用。
    :return: 写入的文件路径。
    :raises OSError: 目录或文件无法写入。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    path = consent_path(data_dir)
    path.write_text(
        json.dumps(
            {'version': AGREEMENT_VERSION, 'acceptedAt': current_time(), 'channel': channel},
            ensure_ascii=False,
            indent=2,
        ) + '\n',
        encoding='utf-8',
    )
    return path


def _agreement_location(project_root: Path) -> str:
    """给出协议正文的可读位置，文件缺失时退回仓库内的相对路径。"""
    path = project_root / AGREEMENT_FILENAME
    return str(path) if path.is_file() else AGREEMENT_FILENAME


def _summary_rows(location: str) -> list[str]:
    """协议要点。完整条款在正文里，这里只列「不看会出事」的几条。"""
    return [
        '启动之前请阅读并同意用户协议。完整条款：',
        f'  {location}',
        '',
        '要点：',
        '  1. 接入 QQ 需要第三方协议端，这违反 QQ 的服务条款，账号存在被封禁的',
        '     风险。请使用小号，风险由你自行承担。',
        '  2. 程序会在本机存储聊天记录，以及群成员的账号、昵称、群名片，还有模型',
        '     从对话中归纳出的、关于他们的事实。这些人并未同意，由你作为部署者',
        '     承担相应责任。',
        '  3. 对话内容会发送给你自己配置的模型服务商，受其条款与隐私政策约束。',
        '  4. 默认开启匿名统计，只上报应用版本、系统类型与 Python 版本三项，',
        '     不含聊天内容与任何身份信息。可在 features.toml 里关闭。',
        '  5. 本软件按现状提供，不作任何担保。',
    ]



def _exit_without_terminal() -> None:
    """在无人可询问的环境下打印自解指引并退出。

    :raises SystemExit: 总是抛出，退出码与拒绝一致。
    """
    print_box('用户协议 · 无法交互询问', [
        '当前环境无法在终端询问（无头部署、服务托管或输出被重定向时都会这样）。',
        '请先阅读上述正文，确认接受后用下面的方式启动一次：',
        '',
        '  uv run bot.py --accept-agreement',
        '',
        '接受一次即可，记录写入数据目录的 consent.json，之后照常启动。',
    ], publish=False)
    raise SystemExit(_REFUSED_EXIT_CODE)


def require_consent(
    data_dir: Path,
    project_root: Path,
    *,
    preaccepted: bool = False,
) -> None:
    """首次启动时要求用户同意协议，未同意则终止启动。

    无头场景是这里最容易出错的一处：

    - 现象：作为 systemd 服务启动时 stdin 不是终端，``input()`` 立刻 EOF，
      进程要么抛 ``EOFError`` 要么在重启循环里反复起停。
    - 原因：交互式询问隐含「有人坐在终端前」，而无头部署恰恰没有。
    - 后果：不检测 TTY 就直接问，等于让所有无头用户撞上一个无法自解的启动失败。
      因此非交互环境下不询问，而是打印如何用 ``--accept-agreement`` 接受，
      并以非零码退出。

    :param data_dir: 运行时数据目录，同意记录写在这里。
    :param project_root: 仓库根目录，用于定位协议正文。
    :param preaccepted: 命令行已用 ``--accept-agreement`` 表示接受时为真。
    :raises SystemExit: 用户拒绝、或在非交互环境下尚未接受时退出。
    副作用：可能向标准输出打印信息框，并写入同意记录文件。
    """
    if has_consented(data_dir):
        return

    location = _agreement_location(project_root)
    if preaccepted:
        record_consent(data_dir, channel='flag')
        print_box('用户协议', ['已通过 --accept-agreement 接受用户协议。', f'正文：{location}'],
                  publish=False)
        return

    print_box('用户协议', _summary_rows(location), publish=False)

    if not sys.stdin.isatty():
        _exit_without_terminal()

    print(f'阅读后请输入「{CONSENT_WORD}」继续，输入其它内容或按 Ctrl+C 退出。')
    try:
        answer = input('> ').strip()
    except EOFError:
        # isatty() 说是终端、input() 却立刻 EOF，在被 supervisor 或容器接管的
        # 场景里确实会发生（分配了伪终端但没有人在输入）。判据不能只靠 isatty，
        # 这里按无头处理，给出可自解的指引而不是笼统的「未同意」。
        print()
        _exit_without_terminal()
    except KeyboardInterrupt:
        print()
        print('未同意用户协议，已退出。')
        raise SystemExit(_REFUSED_EXIT_CODE)

    if answer != CONSENT_WORD:
        print(f'输入的不是「{CONSENT_WORD}」，未同意用户协议，已退出。')
        raise SystemExit(_REFUSED_EXIT_CODE)

    path = record_consent(data_dir, channel='console')
    print_box('用户协议', [
        '已记录你的同意，下次启动不再询问。',
        f'记录位置：{path}',
    ], publish=False)


__all__ = [
    'AGREEMENT_FILENAME',
    'AGREEMENT_VERSION',
    'CONSENT_WORD',
    'consent_path',
    'has_consented',
    'read_consent',
    'record_consent',
    'require_consent',
]
