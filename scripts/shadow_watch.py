"""持续采集 Conversation Agent shadow 观察事件并生成滚动统计报告。

shadow 阶段的行动决策落在 ``data/memory.db`` 的 ``pipeline_events`` 中
（``kind=action_decision`` 且 ``version.modelTask=chat.conversation.shadow``）。
脚本周期查询新增事件，在终端打印紧凑事件行，并把滚动统计写入 Markdown
报告，便于无人值守时事后回看。

用法示例：

    python scripts/shadow_watch.py                  # 从当前最新序号开始持续观察
    python scripts/shadow_watch.py --once           # 只统计现有事件并退出
    python scripts/shadow_watch.py --full           # 从 seq=0 重放全部历史事件
    python scripts/shadow_watch.py --interval 5     # 每 5 秒检查一次
    python scripts/shadow_watch.py --hash f18ffc34 # 只统计指定提示词版本

指标口径与 shadow 验收文档一致：解析失败率、非法动作率、目标合法率、
reply/silent 分布、危险沉默、natural_reply_window 过度触发、旧管线分歧
与 P50/P95 延迟。
"""

from __future__ import annotations

from argparse import ArgumentParser, Namespace
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import json
import sqlite3
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / 'data' / 'memory.db'
DEFAULT_REPORT = PROJECT_ROOT / 'data' / 'logs' / 'shadow-watch.md'
DEFAULT_CURSOR = PROJECT_ROOT / 'data' / 'logs' / 'shadow-watch.cursor'

DEFAULT_MODEL_TASK = 'chat.conversation.shadow'
FAILURE_STATUSES = ('parse_error', 'illegal_action', 'provider_error', 'timeout')
DANGEROUS_SILENT_SIGNALS = frozenset({
    'name_mention', 'direct_mention', 'clear_question', 'reply_to_bot',
})
OVER_TRIGGER_SILENT_CODES = frozenset({
    'others_conversation', 'not_addressed', 'low_relevance',
})


@dataclass
class ShadowSample:
    """一条 shadow action_decision 事件的解析视图。"""

    seq: int
    at: int
    stream_id: int
    turn_id: int | None
    payload: Dict[str, Any]
    text: str = ''
    legacy_action: str = ''

    @property
    def status(self) -> str:
        return str(self.payload.get('eventStatus', ''))

    @property
    def prompt_hash(self) -> str:
        return str((self.payload.get('version') or {}).get('promptHash', ''))

    @property
    def latency_ms(self) -> int:
        return int((self.payload.get('version') or {}).get('latencyMs', 0) or 0)

    @property
    def decision(self) -> Dict[str, Any]:
        return self.payload.get('decision') or {}

    @property
    def action(self) -> str:
        return str(self.decision.get('action', ''))

    @property
    def reasons(self) -> Tuple[str, ...]:
        values = self.decision.get('reasonCodes') or []
        return tuple(str(value) for value in values)

    @property
    def targets(self) -> Tuple[int, ...]:
        values = self.decision.get('targetMessageIds') or []
        return tuple(int(value) for value in values)

    @property
    def gate(self) -> Dict[str, Any]:
        return self.payload.get('gate') or {}

    @property
    def gate_reasons(self) -> Tuple[str, ...]:
        values = self.gate.get('reasonCodes') or []
        return tuple(str(value) for value in values)

    @property
    def inputs(self) -> Dict[str, Any]:
        return self.payload.get('inputs') or {}

    @property
    def selectable_ids(self) -> Tuple[int, ...]:
        values = self.inputs.get('selectableMessageIds') or []
        return tuple(int(value) for value in values)

    @property
    def watermark(self) -> int:
        return int(self.payload.get('messageWatermark', 0) or 0)

    @property
    def sender_label(self) -> str:
        return str(self.payload.get('senderLabel', ''))

    @property
    def target_legal(self) -> bool:
        return all(target in frozenset(self.selectable_ids) for target in self.targets)

    @property
    def is_dangerous_silent(self) -> bool:
        if self.status != 'silent_by_choice':
            return False
        if self.inputs.get('nameMentioned') or self.inputs.get('mentionedMe'):
            return True
        return bool(DANGEROUS_SILENT_SIGNALS.intersection(self.gate_reasons))

    @property
    def is_natural_window_only(self) -> bool:
        return (
            self.gate.get('disposition') == 'deliberate'
            and self.gate_reasons == ('natural_reply_window',)
        )


@dataclass
class ShadowReport:
    """滚动聚合 shadow 样本与验收指标。"""

    samples: List[ShadowSample] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.samples)

    def status_counts(self) -> Counter[str]:
        return Counter(sample.status for sample in self.samples)

    def rate(self, status: str) -> float:
        if not self.samples:
            return 0.0
        return sum(sample.status == status for sample in self.samples) / len(self.samples)

    def reply_count(self) -> int:
        return sum(
            sample.status == 'committed' and sample.action == 'reply'
            for sample in self.samples
        )

    def silent_count(self) -> int:
        return sum(
            sample.status == 'silent_by_choice' and sample.action == 'silent'
            for sample in self.samples
        )

    def reason_distribution(self) -> Counter[str]:
        counter: Counter[str] = Counter()
        for sample in self.samples:
            if sample.status in ('committed', 'silent_by_choice'):
                counter.update(sample.reasons)
        return counter

    def gate_reason_distribution(self) -> Counter[str]:
        counter: Counter[str] = Counter()
        for sample in self.samples:
            counter.update(sample.gate_reasons)
        return counter

    def latency_values(self) -> List[int]:
        return [sample.latency_ms for sample in self.samples if sample.latency_ms > 0]

    def latency_percentile(self, percent: float) -> int | None:
        values = sorted(self.latency_values())
        if not values:
            return None
        index = min(len(values) - 1, int(len(values) * percent))
        return values[index]

    def dangerous_silents(self) -> List[ShadowSample]:
        return [sample for sample in self.samples if sample.is_dangerous_silent]

    def natural_window_samples(self) -> List[ShadowSample]:
        return [
            sample for sample in self.samples
            if 'natural_reply_window' in sample.gate_reasons
        ]

    def over_triggered_natural_window(self) -> List[ShadowSample]:
        return [
            sample for sample in self.natural_window_samples()
            if sample.is_natural_window_only
            and sample.status == 'silent_by_choice'
            and bool(OVER_TRIGGER_SILENT_CODES.intersection(sample.reasons))
        ]

    def legacy_mismatches(self) -> Dict[str, List[ShadowSample]]:
        wanted_reply = [
            sample for sample in self.samples
            if sample.action == 'reply' and sample.legacy_action == 'silent'
        ]
        wanted_silent = [
            sample for sample in self.samples
            if sample.action == 'silent' and sample.legacy_action == 'reply'
        ]
        return {'wanted_reply': wanted_reply, 'wanted_silent': wanted_silent}


def _connect(db_path: Path) -> sqlite3.Connection:
    """打开只读 SQLite 连接；数据库被写入时依赖 SQLite 自身等待。"""
    path = db_path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f'数据库不存在：{path}')
    return sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True, timeout=5)


def _query_legacy_action(conn: sqlite3.Connection, stream_id: int, turn_id: int | None) -> str:
    """读取同一 stream/turn 的旧管线 turn_action 决策。"""
    if turn_id is None:
        return ''
    row = conn.execute(
        '''SELECT json_extract(payload, '$.action') FROM pipeline_events
           WHERE kind = 'turn_action' AND stream_id = ? AND turn_id = ?
           ORDER BY seq DESC LIMIT 1''',
        (stream_id, turn_id),
    ).fetchone()
    return str(row[0]) if row and row[0] is not None else ''


def _query_message_text(conn: sqlite3.Connection, stream_id: int, message_id: int) -> str:
    """按 watermark 读取消息正文，便于报告识别具体样本。"""
    if not message_id:
        return ''
    row = conn.execute(
        'SELECT content FROM messages WHERE stream_id = ? AND id = ?',
        (stream_id, message_id),
    ).fetchone()
    return str(row[0]) if row and row[0] is not None else ''


def _load_samples(
    conn: sqlite3.Connection,
    min_seq: int = 0,
    prompt_hash: str = '',
    model_task: str = DEFAULT_MODEL_TASK,
) -> List[ShadowSample]:
    """读取序号大于 ``min_seq`` 的 Conversation Agent 行动决策事件。

    :param prompt_hash: 非空时只读取该 promptHash；旧版本样本会干扰当前
        闸门通过率的判断，观察期建议固定传入当前版本指纹。
    :param model_task: 事件版本层模型任务；shadow 观察用
        ``chat.conversation.shadow``，selected_streams / enabled 用
        ``chat.conversation``。
    """
    conn.row_factory = sqlite3.Row
    sql = '''SELECT seq, at, stream_id, turn_id, payload FROM pipeline_events
           WHERE kind = 'action_decision'
             AND json_extract(payload, '$.version.modelTask') = ?
             AND seq > ?'''
    params: List[Any] = [model_task, min_seq]
    if prompt_hash:
        sql += " AND json_extract(payload, '$.version.promptHash') = ?"
        params.append(prompt_hash)
    sql += ' ORDER BY seq'
    rows = conn.execute(sql, params).fetchall()
    samples: List[ShadowSample] = []
    for row in rows:
        try:
            payload = json.loads(row['payload'])
        except (TypeError, json.JSONDecodeError):
            continue
        sample = ShadowSample(
            seq=int(row['seq']),
            at=int(row['at'] or 0),
            stream_id=int(row['stream_id'] or 0),
            turn_id=int(row['turn_id']) if row['turn_id'] is not None else None,
            payload=payload,
        )
        sample.text = _query_message_text(conn, sample.stream_id, sample.watermark)
        sample.legacy_action = _query_legacy_action(conn, sample.stream_id, sample.turn_id)
        samples.append(sample)
    return samples


def _current_max_seq(
    conn: sqlite3.Connection,
    prompt_hash: str = '',
    model_task: str = DEFAULT_MODEL_TASK,
) -> int:
    sql = '''SELECT COALESCE(MAX(seq), 0) FROM pipeline_events
           WHERE kind = 'action_decision'
             AND json_extract(payload, '$.version.modelTask') = ?'''
    params: List[Any] = [model_task]
    if prompt_hash:
        sql += " AND json_extract(payload, '$.version.promptHash') = ?"
        params.append(prompt_hash)
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0)


def _read_cursor(path: Path) -> int:
    try:
        return int(path.read_text(encoding='utf-8').strip())
    except (FileNotFoundError, ValueError):
        return -1


def _write_cursor(path: Path, seq: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(seq), encoding='utf-8')


def _format_time(at_ms: int) -> str:
    return datetime.fromtimestamp(at_ms / 1000).strftime('%m-%d %H:%M:%S')


def _compact_text(text: str, limit: int = 60) -> str:
    compact = ' '.join(text.split())
    if len(compact) > limit:
        compact = compact[:limit] + '…'
    return compact


def _event_line(sample: ShadowSample) -> str:
    """构造终端可见的单条 shadow 事件摘要。"""
    timestamp = _format_time(sample.at)
    sender = sample.sender_label or '未知发送者'
    text = _compact_text(sample.text)
    prefix = f'{timestamp} [shadow] turn#{sample.turn_id}'
    if sample.is_dangerous_silent:
        prefix += ' [危险沉默]'
    if sample.legacy_action and sample.action == 'reply' and sample.legacy_action == 'silent':
        prefix += ' [旧规则拒]'
    if sample.legacy_action and sample.action == 'silent' and sample.legacy_action == 'reply':
        prefix += ' [旧规则回]'
    if sample.status in ('committed', 'silent_by_choice'):
        decision = f'{sample.action} reasons={",".join(sample.reasons)}'
        if sample.targets:
            decision += f' target={",".join(map(str, sample.targets))}'
    else:
        detail = str(sample.payload.get('detail', '')).strip()
        decision = f'{sample.status} {detail}'.strip()
    body = f'{sender}: {text}' if text else sender
    return (
        f'{prefix} {sample.status} · {body} → {decision} '
        f'latency={sample.latency_ms}ms hash={sample.prompt_hash}'
    )


def _percent_text(value: int | None) -> str:
    return '—' if value is None else f'{value} ms'


def _render_report(report: ShadowReport) -> str:
    """渲染完整 Markdown 滚动报告。"""
    status_lines = '\n'.join(
        f'- `{status}`：{count}'
        for status, count in report.status_counts().items()
    ) or '- 暂无'
    reason_lines = '\n'.join(
        f'- `{code}`：{count}'
        for code, count in report.reason_distribution().most_common()
    ) or '- 暂无'
    gate_lines = '\n'.join(
        f'- `{code}`：{count}'
        for code, count in report.gate_reason_distribution().most_common()
    ) or '- 暂无'
    hash_lines = '\n'.join(
        f'- `{hash_value}`：{count}'
        for hash_value, count in Counter(sample.prompt_hash for sample in report.samples).most_common()
    ) or '- 暂无'
    recent_rows: List[str] = []
    for sample in reversed(report.samples[-20:]):
        decision = (
            f'{sample.action}（{",".join(sample.reasons)}）'
            if sample.action else sample.status
        )
        recent_rows.append(
            f'| {_format_time(sample.at)} | #{sample.turn_id} | {sample.sender_label} | '
            f'{_compact_text(sample.text, 40)} | {decision} | '
            f'{",".join(sample.gate_reasons)} | {sample.status} | '
            f'{sample.latency_ms}ms |'
        )
    recent_table = '\n'.join(recent_rows) or '| 暂无样本 |'
    mismatches = report.legacy_mismatches()
    dangerous = report.dangerous_silents()
    natural_window = report.natural_window_samples()
    over_triggered = report.over_triggered_natural_window()
    target_events = [sample for sample in report.samples if sample.targets]
    target_legal_rate = (
        sum(sample.target_legal for sample in target_events) / len(target_events)
        if target_events else 1.0
    )

    return f"""# Shadow 观察滚动报告

生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
样本数：{report.total}

## 推进闸门指标

| 指标 | 当前值 | 目标 |
|---|---|---|
| 解析失败率 | {report.rate('parse_error'):.1%} | 接近 0 |
| 非法动作率 | {report.rate('illegal_action'):.1%} | 接近 0 |
| 目标合法率 | {target_legal_rate:.1%} | 100% |
| reply / silent | {report.reply_count()} / {report.silent_count()} | 看场景是否合理 |
| 想回而旧规则拒 | {len(mismatches['wanted_reply'])} | 重点检查合理性 |
| 想沉默而旧规则回 | {len(mismatches['wanted_silent'])} | 重点检查合理性 |
| 危险沉默 | {len(dangerous)} | 0 |
| natural_reply_window 疑似过触发 | {len(over_triggered)} / {len(natural_window)} | 样本足够后评估 |
| P50 / P95 延迟 | {_percent_text(report.latency_percentile(0.5))} / {_percent_text(report.latency_percentile(0.95))} | 记录基线 |

## 事件状态

{status_lines}

## 理由码分布

{reason_lines}

## 门控原因分布

{gate_lines}

## promptHash 分布

{hash_lines}

## 最近 20 条样本

| 时间 | 回合 | 发送者 | 文本 | Agent 决策 | 门控 | 状态 | 延迟 |
|---|---|---|---|---|---|---|---|
{recent_table}
"""


def _print_summary(report: ShadowReport) -> None:
    """在终端打印一段紧凑滚动摘要。"""
    print(
        f'[shadow] 累计 {report.total} · '
        f'解析失败 {report.rate("parse_error"):.1%} · '
        f'非法动作 {report.rate("illegal_action"):.1%} · '
        f'reply {report.reply_count()} / silent {report.silent_count()} · '
        f'危险沉默 {len(report.dangerous_silents())}'
    )


def _parse_args() -> Namespace:
    parser = ArgumentParser(description='持续采集 shadow 行动决策并生成滚动报告')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB, help='memory.db 路径')
    parser.add_argument('--report', type=Path, default=DEFAULT_REPORT, help='Markdown 报告输出路径')
    parser.add_argument('--cursor', type=Path, default=DEFAULT_CURSOR, help='游标文件路径')
    parser.add_argument('--interval', type=float, default=15.0, help='轮询间隔秒数')
    parser.add_argument('--hash', default='', help='只统计该 promptHash，留空表示全部')
    parser.add_argument(
        '--model-task',
        default=DEFAULT_MODEL_TASK,
        help='观察的 version.modelTask；selected_streams/enabled 填 chat.conversation',
    )
    parser.add_argument('--once', action='store_true', help='只统计一次并退出')
    parser.add_argument('--full', action='store_true', help='从 seq=0 重放全部历史事件')
    return parser.parse_args()


def _initial_cursor(args: Namespace, conn: sqlite3.Connection) -> int:
    if args.full:
        return 0
    saved = _read_cursor(args.cursor)
    if saved >= 0:
        return saved
    return _current_max_seq(conn)


def _run_once(args: Namespace) -> int:
    try:
        with _connect(args.db) as conn:
            samples = _load_samples(conn, min_seq=0, prompt_hash=args.hash, model_task=args.model_task)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f'[shadow] 读取失败：{exc}', file=sys.stderr)
        return 1
    report = ShadowReport(samples)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(_render_report(report), encoding='utf-8')
    print(_render_report(report))
    print(f'报告已写入：{args.report}')
    return 0


def _run_watch(args: Namespace) -> int:
    try:
        with _connect(args.db) as conn:
            samples = _load_samples(conn, min_seq=0, prompt_hash=args.hash, model_task=args.model_task)
            cursor = _initial_cursor(args, conn)
            max_seq = _current_max_seq(conn, args.hash, args.model_task)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f'[shadow] 启动失败：{exc}', file=sys.stderr)
        return 1

    report = ShadowReport(samples)
    known_seqs = {sample.seq for sample in samples}
    if args.full:
        for sample in samples:
            print(_event_line(sample))
        cursor = max_seq
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(_render_report(report), encoding='utf-8')
    _write_cursor(args.cursor, cursor)
    _print_summary(report)
    print(f'[shadow] 从 seq>{cursor} 开始观察，每 {args.interval:g}s 检查一次；')
    print(f'[shadow] 报告滚动写入：{args.report}，Ctrl+C 退出。')

    try:
        while True:
            time.sleep(args.interval)
            try:
                with _connect(args.db) as conn:
                    incoming = _load_samples(conn, min_seq=cursor, prompt_hash=args.hash, model_task=args.model_task)
                if not incoming:
                    continue
                for sample in incoming:
                    cursor = max(cursor, sample.seq)
                    if sample.seq in known_seqs:
                        continue
                    known_seqs.add(sample.seq)
                    print(_event_line(sample))
                    report.samples.append(sample)
                args.report.write_text(_render_report(report), encoding='utf-8')
                _write_cursor(args.cursor, cursor)
                _print_summary(report)
            except (FileNotFoundError, sqlite3.Error) as exc:
                print(f'[shadow] 读取失败，稍后重试：{exc}', file=sys.stderr)
    except KeyboardInterrupt:
        print('\n[shadow] 已停止观察。')
    return 0


def main() -> int:
    # Windows 控制台默认编码可能是 GBK；仅把无法编码的字符替换为占位符，
    # 保证 Markdown 报告仍按 UTF-8 保存、终端打印不因个别 QQ 昵称中断。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError):
            pass
    args = _parse_args()
    return _run_once(args) if args.once else _run_watch(args)


if __name__ == '__main__':
    raise SystemExit(main())
