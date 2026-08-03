"""
对话编排服务。直接移植自 src/main/chat.ts。

持有所有后端资源：LLM provider、MemoryStore、Persona、DayPlanService。
通过 WebSocket push 推事件给 Electron 主进程。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

import asyncio
import random

from .trace import trace
from .trace_console import mark_turn_start, render_turn, render_turn_error
from .vector import VectorService
from yueli.agent.character import CHARACTER_NAME, pick_tone
from yueli.agent.expression import render_expression_habits, select_expression_habits
from yueli.agent.history import close_dangling_say, fit_char_budget, normalize_history
from yueli.agent.parser import (
    MemoryEvent, MoodEvent, ParseEvent, ResponseParser, SayEndEvent, SayEvent, TextEvent,
)
from yueli.agent.prompt import build_proactive_prompt, build_system_prompt
from yueli.agent.summarize import summarize
from yueli.awareness.sleep import SleepState
from yueli.common.clock import now as current_time
from yueli.common.logger import get_logger
from yueli.memory.store import EpisodeInput, FactInput, MemoryStore
from yueli.persona.state import MoodDelta, Persona, describe_acquaintance, describe_persona
from yueli.schedule.plan import DayPlanService, ScheduleSleepState

logger = get_logger(__name__)

# 工作记忆窗口：进 context 的最近消息条数
WINDOW = 40
# 待压缩消息超过这个数就触发一次 L2 摘要
SUMMARIZE_AT = 48
# 每次摘要吃掉多少条最老的消息
SUMMARIZE_BATCH = 16
# 真正的天花板是 min(WINDOW, pending_count)——working_memory() 只取
# episode_id IS NULL 的消息。所以 SUMMARIZE_AT 必须跟着 WINDOW 一起调，
# 否则摘要一跑 pending 就掉到 SUMMARIZE_AT - SUMMARIZE_BATCH，
# 单调 WINDOW 完全无效。当前下限 48-16=32 条 ≈ 16 轮。

# 两次对话间隔超过这个时长，视为新一段对话：重新抽语气、重新播种表达样本。
SESSION_GAP_MS = 30 * 60_000

_HINTS: dict[str, str] = {
    'auth': 'API Key 无效，检查 .env',
    'model': '模型 ID 不对，检查 .env 里的 LLM_MODEL',
    'quota': '限流或余额不足，稍等一下',
    'network': '连不上模型接口，检查网络或代理',
    'blocked': '这句被内容审核拦了，换个说法',
}


class ChatService:
    """
    对话编排。

    push_event: 把事件推送到 WebSocket 的回调，由 api/ws.py 注入。
    speak_audio: 把文本送去合成的回调，由 tts service 注入（可 None）。
    """

    def __init__(
        self,
        db: Any,
        provider: Any | None,
        push_event: Callable[[str, Any], Any],
        speak_audio: Callable[[str, int], Any] | None = None,
        vector: VectorService | None = None,
        cfg: Any | None = None,
    ) -> None:
        self._db = db
        self._provider = provider
        self._push_event = push_event
        self._speak_audio = speak_audio
        self._vector = vector or VectorService(None, None)
        self._cfg = cfg
        self.memory = MemoryStore(db)
        self.persona = Persona(db)
        self.persona.snapshot_daily()
        self._turn_id = 0
        self._inflight: asyncio.Task | None = None
        self._summarizing = False
        self._activity: Callable[[], str] | None = None
        self._sleep_state: Callable[[], SleepState] | None = None
        self._schedule: DayPlanService | None = None
        # 会话级人设状态。逐轮重掷会让她的语气一轮一个样，读起来就像每轮
        # 换了个人——这正是「像单次对话」的一部分。
        self._session_started_at: int | None = None
        self._session_tone: str | None = None
        self._session_seed: int = 0

    @property
    def ready(self) -> bool:
        return self._provider is not None

    def set_schedule(self, svc: DayPlanService) -> None:
        self._schedule = svc

    def set_activity_provider(self, fn: Callable[[], str]) -> None:
        self._activity = fn

    def set_sleep_state_provider(self, fn: Callable[[], SleepState]) -> None:
        self._sleep_state = fn

    def current_sleep(self) -> ScheduleSleepState:
        s = self._sleep_state() if self._sleep_state else None
        if s is None:
            return ScheduleSleepState(asleep=False, drowsy=False)
        return ScheduleSleepState(asleep=s.asleep, drowsy=s.drowsy, just_woke=s.just_woke)

    async def ensure_schedule(self, now: int | None = None) -> None:
        if self._schedule:
            await self._schedule.ensure(now or current_time())

    def settle_elapsed(self, now: int | None = None, earlier_asleep: bool = False) -> None:
        now = now or current_time()
        before = self.persona.get()
        if self._schedule:
            asleep_hours = self._schedule.sleep_hours_between(before.updated_at, now, earlier_asleep)
        else:
            asleep_hours = 0.0
        self.persona.apply_elapsed(now, asleep_hours)
        self.persona.snapshot_daily(now)

    async def send(self, text: str) -> int:
        trimmed = text.strip()
        if not trimmed:
            return self._turn_id

        self.interrupt()
        turn = self._next_turn()
        mark_turn_start(turn)
        trace.emit('user_input', turnId=turn, text=trimmed)

        if not self._provider:
            await self._emit('chat.error', {'turnId': turn, 'kind': 'error',
                                             'message': '对话未初始化', 'hint': '检查 .env 里的 LLM_* 配置'})
            return turn

        now = current_time()
        asleep = (self._sleep_state().asleep if self._sleep_state else False)
        self.settle_elapsed(now, asleep)
        self.memory.sweep(now)
        if self._schedule:
            await self._schedule.ensure(now)

        # ★ 必须在 append 之前判定：append 之后 last_message_at() 就是 now，
        #   间隔恒为 0，会话永远不会翻页。
        self._refresh_session(now)

        user_msg_id = self.memory.append_message('user', trimmed, now)
        cancel_event = asyncio.Event()

        async def _run():
            parser = ResponseParser()
            assistant_raw = ''
            side_effects: list[dict] = []
            interrupted = False
            from yueli.llm.openai import LlmError
            try:
                messages = await self._build_messages_with_vector(trimmed, now)
                trace.emit('llm_request', turnId=turn, messages=messages, temperature=0.85)
                async for chunk in self._provider.stream(messages=messages, temperature=0.85):
                    if cancel_event.is_set():
                        interrupted = True
                        break
                    trace.emit('llm_chunk', turnId=turn, text=chunk.get('text'), reasoning=chunk.get('reasoning'))
                    if not chunk.get('text'):
                        continue
                    assistant_raw += chunk['text']
                    for event in parser.push(chunk['text']):
                        if cancel_event.is_set():
                            interrupted = True
                            break
                        self._handle_side_effects(event, now, turn, side_effects)
                        self._track_speech(event, turn)
                        await self._emit_parse_event(turn, event)
                    if interrupted:
                        break

                if not interrupted:
                    for event in parser.flush():
                        if cancel_event.is_set():
                            interrupted = True
                            break
                        self._handle_side_effects(event, now, turn, side_effects)
                        self._track_speech(event, turn)
                        await self._emit_parse_event(turn, event)

                # ★ 中断也要落库。这段话已经显示（甚至念）给用户了，历史里
                #   不能当它没发生过——否则下一轮就是连着两条 user 消息，
                #   模型看不到自己上一句说了什么。副作用（<memory>/<mood>）
                #   在流式过程中已经写库了，话本身更不该丢。
                self._persist_reply(assistant_raw)
                if interrupted:
                    return

                trace.emit('llm_final', turnId=turn, text=assistant_raw)
                render_turn(turn, trimmed, messages, assistant_raw, side_effects)
                try:
                    self.persona.apply_turn(current_time())
                except Exception as exc:
                    # 人格推进失败不该把历史一起拖下水——下面的 except 会删用户消息。
                    logger.warning('persona_apply_turn_failed', turnId=turn, error=str(exc))
                await self._emit('chat.done', {'turnId': turn, 'kind': 'done'})
                asyncio.create_task(self._maybe_summarize())

            except LlmError as exc:
                if exc.kind == 'aborted':
                    self._persist_reply(assistant_raw)
                    return
                self._rollback_or_keep(user_msg_id, assistant_raw)
                hint = _HINTS.get(exc.kind, '')
                trace.emit('llm_error', turnId=turn, errorKind=exc.kind, message=str(exc))
                render_turn_error(turn, trimmed, exc.kind, str(exc))
                await self._emit('chat.error', {'turnId': turn, 'kind': 'error',
                                                 'message': str(exc), 'hint': hint})
            except Exception as exc:
                self._rollback_or_keep(user_msg_id, assistant_raw)
                trace.emit('llm_error', turnId=turn, errorKind='unknown', message=str(exc))
                render_turn_error(turn, trimmed, 'unknown', str(exc))
                await self._emit('chat.error', {'turnId': turn, 'kind': 'error', 'message': str(exc)})

        task = asyncio.create_task(_run())
        task._cancel_event = cancel_event  # type: ignore[attr-defined]
        self._inflight = task
        return turn

    def _refresh_session(self, now: int) -> None:
        """跨过静默间隔就开一段新会话，重抽语气和表达样本的随机种子。"""
        last = self.memory.last_message_at()
        if (self._session_started_at is None
                or last is None
                or now - last > SESSION_GAP_MS):
            self._session_started_at = now
            self._session_tone = pick_tone()
            self._session_seed = random.randrange(1 << 30)

    def _session_rng(self) -> random.Random:
        """同一会话内给出同一批表达样本；情境识别仍然逐轮进行。"""
        return random.Random(self._session_seed)

    def _persist_reply(self, assistant_raw: str) -> None:
        """把已经吐出去的回复落进历史，补齐流式中断留下的悬空 <say>。"""
        text = close_dangling_say(assistant_raw)
        if text:
            self.memory.append_message('assistant', text, current_time())

    def _rollback_or_keep(self, user_msg_id: int, assistant_raw: str) -> None:
        """出错时决定这一轮留不留。

        一个字都没吐出来 → 整轮回滚，用户那句话也删掉（保持原有意图：
        这轮相当于没发生）。已经有内容显示给用户了 → 两条都留下，
        删掉用户消息反而会让历史里出现「凭空的回复」。
        """
        if close_dangling_say(assistant_raw):
            self._persist_reply(assistant_raw)
        else:
            self.memory.delete_message(user_msg_id)

    def interrupt(self) -> None:
        if self._inflight and not self._inflight.done():
            cancel_ev = getattr(self._inflight, '_cancel_event', None)
            if cancel_ev:
                cancel_ev.set()
        self._inflight = None

    def speak(self, lines: list[dict]) -> int:
        if not lines:
            return self._turn_id
        self.interrupt()
        turn = self._next_turn()
        texts: list[str] = []
        for line in lines:
            asyncio.create_task(self._emit_parse_event(turn, SayEvent(emotion=line.get('emotion'))))
            asyncio.create_task(self._emit_parse_event(turn, TextEvent(value=line['text'])))
            asyncio.create_task(self._emit_parse_event(turn, SayEndEvent()))
            if self._speak_audio:
                asyncio.create_task(self._speak_audio(line['text'], turn))
            texts.append(f'<say>{line["text"]}</say>')
        asyncio.create_task(self._emit('chat.done', {'turnId': turn, 'kind': 'done'}))
        self.memory.append_message('assistant', ''.join(texts))
        return turn

    async def compose_proactive(self, situation: str) -> list[dict] | None:
        if not self._provider:
            return None
        now = current_time()
        self._refresh_session(now)
        if self._schedule:
            await self._schedule.ensure(now)
        persona_desc = describe_persona(self.persona.get())
        acquaintance = describe_acquaintance(self.memory.first_seen_at, now)
        schedule_desc = (self._schedule.describe(now, self.current_sleep())
                         if self._schedule else '')
        base_prompt = build_system_prompt(
            name=CHARACTER_NAME,
            now=datetime.fromtimestamp(now / 1000),
            persona=persona_desc,
            acquaintance=acquaintance,
            facts=[fact.content for fact in self.memory.top_facts(5, now)],
            episodes=[episode.summary for episode in self.memory.recent_episodes(2)],
            schedule=schedule_desc,
            expression_habits=render_expression_habits(
                select_expression_habits(situation, proactive=True, limit=3, rng=self._session_rng())
            ),
            tone=self._session_tone,
            **self._relationship_kwargs(),
        )
        system = build_proactive_prompt(base_prompt, situation)
        raw = ''
        try:
            async for chunk in self._provider.stream(
                messages=[{'role': 'system', 'content': system}],
                temperature=0.9, max_tokens=200,
            ):
                if chunk.get('text'):
                    raw += chunk['text']
        except Exception:
            return None
        return _extract_lines(raw)

    def diary_payload(self, now: int | None = None) -> dict:
        now = now or current_time()
        memories = [{'content': f.content, 'frozen': f.frozen} for f in self.memory.all_facts(now)]
        today = self._schedule.get(now) if self._schedule else None
        return {
            'entries': self.memory.all_episodes(),
            'today': _plan_to_dict(today) if today else None,
            'memories': memories,
            'now': now,
        }

    def observability_snapshot(self, now: int | None = None) -> dict:
        now = now or current_time()
        s = self.persona.get()
        fc = self.memory.fact_count()
        return {
            'now': now,
            'persona': {'state': s.__dict__, 'description': describe_persona(s)},
            'schedule': _plan_to_dict(self._schedule.get(now)) if self._schedule else None,
            'memory': {
                'semantic': [f.__dict__ for f in self.memory.all_facts(now)],
                'episodes': len(self.memory.all_episodes()),
                'workingMessages': self.memory.pending_count(),
            },
        }

    def _next_turn(self) -> int:
        self._turn_id += 1
        return self._turn_id

    def _relationship_kwargs(self) -> dict:
        """从 cfg.bot 取用户昵称/关系称呼，喂给 build_system_prompt。cfg 可以是 None
        （比如测试直接构造 ChatService 不传 cfg），这时两项都不注入，行为和之前一样。"""
        if self._cfg is None:
            return {}
        bot_cfg = self._cfg.bot
        return {
            'user_nickname': bot_cfg.user_nickname,
            'relationship': bot_cfg.relationship,
        }

    async def _build_messages_with_vector(self, query: str, now: int) -> list[dict]:
        """向量召回版本的消息构建。_build_messages 的异步替代。"""
        query_embedding = await self._vector.embed_query(query)
        facts = self.memory.recall_facts(query, 6, now, query_embedding=query_embedding)
        recalled = self.memory.recall_episodes(query, 2)
        recent = self.memory.recent_episodes(2)
        seen_ids: set[int] = set()
        episodes = []
        for e in [*recalled, *recent]:
            if e.id not in seen_ids:
                seen_ids.add(e.id)
                episodes.append(e)
        episodes = episodes[:3]
        persona_desc = describe_persona(self.persona.get())
        acquaintance = describe_acquaintance(self.memory.first_seen_at, now)
        schedule_desc = (self._schedule.describe(now, self.current_sleep()) if self._schedule else None)
        system = build_system_prompt(
            name=CHARACTER_NAME,
            now=datetime.fromtimestamp(now / 1000),
            persona=persona_desc,
            acquaintance=acquaintance,
            facts=[f.content for f in facts],
            episodes=[e.summary for e in episodes],
            activity=(self._activity() if self._activity else None),
            schedule=schedule_desc,
            expression_habits=render_expression_habits(
                select_expression_habits(query, rng=self._session_rng())
            ),
            tone=self._session_tone,
            **self._relationship_kwargs(),
        )
        # ★ 读时修复：不假设历史是干净的。库里已经存在的坏历史（每一次打断
        #   都损坏过一轮）只能在这里救回来，写入端的修复管不到已经写坏的部分。
        #   幂等，对干净历史没有副作用。
        wm = self.memory.working_memory(WINDOW)
        history = normalize_history({'role': m.role, 'content': m.content} for m in wm)
        return [{'role': 'system', 'content': system}, *fit_char_budget(history)]

    def _handle_side_effects(
        self, event: ParseEvent, now: int, turn: int, sink: list[dict] | None = None,
    ) -> None:
        if isinstance(event, MemoryEvent) and event.content:
            memory_kind = event.memory_type or '未分类'
            self.memory.add_fact(FactInput(content=event.content, kind=memory_kind), now)
            trace.emit('memory_fact', turnId=turn, content=event.content, memoryKind=memory_kind)
            if sink is not None:
                sink.append({'kind': 'memory_fact', 'content': event.content, 'memoryKind': memory_kind})
        elif isinstance(event, MoodEvent):
            self.persona.apply_mood(MoodDelta(favor=event.favor, energy=event.energy), now)
            trace.emit('mood_delta', turnId=turn, favor=event.favor, energy=event.energy)
            if sink is not None:
                sink.append({'kind': 'mood_delta', 'favor': event.favor, 'energy': event.energy})

    def _track_speech(self, event: ParseEvent, turn: int) -> None:
        if not self._speak_audio:
            return
        # speech tracking handled externally (TTS service injects speak_audio)

    async def _emit(self, channel: str, payload: Any) -> None:
        try:
            await self._push_event(channel, payload)
        except Exception as exc:
            logger.warning('emit_failed', channel=channel, error=str(exc))

    async def _emit_parse_event(self, turn: int, event: ParseEvent) -> None:
        if isinstance(event, SayEvent):
            ev = {'turnId': turn, 'kind': 'parse',
                  'event': {'type': 'say', **({'emotion': event.emotion} if event.emotion else {}),
                             **({'gesture': event.gesture} if event.gesture else {})}}
        elif isinstance(event, TextEvent):
            ev = {'turnId': turn, 'kind': 'parse', 'event': {'type': 'text', 'value': event.value}}
        elif isinstance(event, SayEndEvent):
            ev = {'turnId': turn, 'kind': 'parse', 'event': {'type': 'sayEnd'}}
        elif isinstance(event, MemoryEvent):
            ev = {'turnId': turn, 'kind': 'parse',
                  'event': {'type': 'memory', 'content': event.content,
                             **({'memoryType': event.memory_type} if event.memory_type else {})}}
        elif isinstance(event, MoodEvent):
            ev = {'turnId': turn, 'kind': 'parse',
                  'event': {'type': 'mood',
                             **({'favor': event.favor} if event.favor is not None else {}),
                             **({'energy': event.energy} if event.energy is not None else {})}}
        else:
            return
        await self._emit('chat.event', ev)

    async def _maybe_summarize(self) -> None:
        if self._summarizing or not self._provider:
            return
        if self.memory.pending_count() < SUMMARIZE_AT:
            return
        self._summarizing = True
        try:
            batch = self.memory.oldest_pending(SUMMARIZE_BATCH)
            if len(batch) < 4:
                return
            msgs = [{'role': m['role'], 'content': m['content']} for m in batch]
            episode = await summarize(self._provider, msgs)
            if not episode:
                return
            self.memory.add_episode(EpisodeInput(
                summary=episode.summary, cues=episode.recall_cues,
                started_at=batch[0]['created_at'], ended_at=batch[-1]['created_at'],
                message_ids=[m['id'] for m in batch],
            ))
        except Exception:
            pass
        finally:
            self._summarizing = False


def _extract_lines(raw: str) -> list[dict] | None:
    parser = ResponseParser()
    lines: list[dict] = []
    cur: dict | None = None
    for e in [*parser.push(raw), *parser.flush()]:
        if isinstance(e, SayEvent):
            cur = {'text': '', **({'emotion': e.emotion} if e.emotion else {})}
        elif isinstance(e, TextEvent) and cur is not None:
            cur['text'] += e.value
        elif isinstance(e, SayEndEvent) and cur is not None:
            if cur['text'].strip():
                lines.append({**cur, 'text': cur['text'].strip()})
            cur = None
    return lines if lines else None


def _plan_to_dict(plan: Any) -> dict | None:
    if plan is None:
        return None
    return {
        'date': plan.date,
        'slots': [{'from': s.from_time, 'doing': s.doing, 'mood': s.mood} for s in plan.slots],
        'bedtimeHint': plan.bedtime_hint,
        'wakeHint': plan.wake_hint,
        'theme': plan.theme,
        'carryOver': plan.carry_over,
    }
