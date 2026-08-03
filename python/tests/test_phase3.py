"""Phase 3 tests — awareness: classify, budget, look, monitor."""

from __future__ import annotations

import pytest
from yueli.awareness.classify import ForegroundInfo, classify, describe_activity, vision_context_for
from yueli.awareness.budget import (
    COOLDOWN_MS, DAILY_BUDGET, IGNORE_LIMIT,
    after_speak, after_user_spoke, cooldown_for, decide, decide_scene,
    describe_budget, initial_state, InterruptContext,
)
from yueli.awareness.look import FRAME_CHANGE_THRESHOLD, IDLE_GLANCE_AFTER_MS, IDLE_GLANCE_INTERVAL_MS, LOOK_COOLDOWN_MS, LookInput, should_look, within_look_cooldown
from yueli.awareness.monitor import ForegroundInfo as FI, ForegroundProcessMonitor

T0 = int(__import__('datetime').datetime(2026, 7, 15, 14, 0).timestamp() * 1000)  # local 14:00, +8h stays same day
MIN = 60_000


# ─── Classify ────────────────────────────────────────────────────────────────

class TestClassify:
    def test_recognizes_editors(self):
        assert classify(ForegroundInfo('Code.exe')).activity == 'coding'
        assert classify(ForegroundInfo('idea64.exe')).activity == 'coding'
        assert classify(ForegroundInfo('Cursor.exe')).activity == 'coding'

    def test_case_and_extension_insensitive(self):
        assert classify(ForegroundInfo('CLOUDMUSIC.EXE')).activity == 'music'
        assert classify(ForegroundInfo('cloudmusic')).activity == 'music'

    def test_gaming_video_chat(self):
        assert classify(ForegroundInfo('steam.exe')).activity == 'gaming'
        assert classify(ForegroundInfo('PotPlayerMini64.exe')).activity == 'video'
        assert classify(ForegroundInfo('WeChat.exe')).activity == 'chat'

    def test_explorer_is_files(self):
        assert classify(ForegroundInfo('explorer.exe')).activity == 'files'

    def test_unknown_is_other(self):
        assert classify(ForegroundInfo('SomeRandomApp.exe')).activity == 'other'

    def test_no_info_is_idle(self):
        assert classify(None).activity == 'idle'
        assert classify(ForegroundInfo('')).activity == 'idle'

    def test_browser_title_refinement(self):
        assert classify(ForegroundInfo('chrome.exe', title='【原神】新版本PV - 哔哩哔哩')).activity == 'video'
        assert classify(ForegroundInfo('chrome.exe', title='MDN Web Docs')).activity == 'coding'
        assert classify(ForegroundInfo('chrome.exe', title='某个普通网页')).activity == 'browsing'

    def test_title_not_in_output(self):
        secret = '工资表_2026Q3_最终版.xlsx - Excel'
        r = classify(ForegroundInfo('EXCEL.EXE', title=secret))
        import json
        s = json.dumps(r.__dict__)
        assert '工资' not in s and '2026Q3' not in s
        assert r.activity == 'work'

    def test_meeting_is_silent(self):
        assert classify(ForegroundInfo('Zoom.exe')).silent is True
        assert classify(ForegroundInfo('TencentMeeting.exe')).silent is True

    def test_fullscreen_is_silent(self):
        assert classify(ForegroundInfo('Code.exe', fullscreen=True)).silent is True
        assert classify(ForegroundInfo('Code.exe', fullscreen=False)).silent is False

    def test_vision_context(self):
        steam = ForegroundInfo('steam.exe', title='Steam')
        game = ForegroundInfo('GenshinImpact.exe', title='原神')
        folder = ForegroundInfo('explorer.exe', title='D:\\Games')
        assert vision_context_for(steam, classify(steam)) == 'steam-library'
        assert vision_context_for(game, classify(game)) == 'gameplay'
        assert vision_context_for(folder, classify(folder)) == 'game-folder'

    def test_describe_activity_no_digits_for_long_session(self):
        coding = classify(ForegroundInfo('Code.exe'))
        text = describe_activity(coding, 137)
        assert not any(c.isdigit() for c in text)
        assert '两个多小时' in text

    def test_describe_activity_short_session_no_span(self):
        coding = classify(ForegroundInfo('Code.exe'))
        assert describe_activity(coding, 5) == '他在写代码。'


# ─── Budget ──────────────────────────────────────────────────────────────────

def _ctx(**kwargs) -> InterruptContext:
    defaults = dict(now=T0, silent=False, asleep=False, visible=True, responded_since_last=True)
    defaults.update(kwargs)
    return InterruptContext(**defaults)


def _speak_times(state, n, **ctx_kw):
    s = state
    for i in range(n):
        c = _ctx(now=T0 + i * 2 * COOLDOWN_MS, **ctx_kw)
        if decide(s, c).allow:
            s = after_speak(s, c)
    return s


class TestBudget:
    def test_default_allow(self):
        assert decide(initial_state(T0), _ctx()).allow is True

    def test_silent_blocks(self):
        d = decide(initial_state(T0), _ctx(silent=True))
        assert d.allow is False and d.reason == 'silent'

    def test_asleep_blocks(self):
        assert decide(initial_state(T0), _ctx(asleep=True)).reason == 'asleep'

    def test_hidden_blocks(self):
        assert decide(initial_state(T0), _ctx(visible=False)).reason == 'hidden'

    def test_cooldown(self):
        s = after_speak(initial_state(T0), _ctx())
        assert decide(s, _ctx(now=T0 + 10 * MIN)).reason == 'cooldown'
        assert decide(s, _ctx(now=T0 + 31 * MIN)).allow is True

    def test_budget_exhausted(self):
        s = _speak_times(initial_state(T0), DAILY_BUDGET)
        assert s.used == DAILY_BUDGET
        assert decide(s, _ctx(now=T0 + 8 * 3_600_000)).reason == 'budget'

    def test_daily_reset(self):
        s = _speak_times(initial_state(T0), DAILY_BUDGET)
        tomorrow = T0 + 24 * 3_600_000 + 1
        assert decide(s, _ctx(now=tomorrow)).allow is True

    def test_high_priority_skips_budget_and_cooldown(self):
        s = _speak_times(initial_state(T0), DAILY_BUDGET)
        high = _ctx(now=T0 + MIN, priority='high')
        assert decide(s, high).allow is True
        after = after_speak(s, high)
        assert after.used == DAILY_BUDGET
        assert after.last_at == high.now

    def test_ignore_accumulates(self):
        s = initial_state(T0)
        for i in range(3):
            s = after_speak(s, _ctx(now=T0 + i * 5 * COOLDOWN_MS, responded_since_last=False))
        assert s.ignored == 3

    def test_cooldown_doubles_per_ignore(self):
        assert cooldown_for(0) == COOLDOWN_MS
        assert cooldown_for(IGNORE_LIMIT) == COOLDOWN_MS * 2
        assert cooldown_for(IGNORE_LIMIT + 1) == COOLDOWN_MS * 4

    def test_cooldown_has_cap(self):
        assert cooldown_for(99) <= 4 * 3_600_000

    def test_user_spoke_clears_ignore(self):
        s = after_speak(initial_state(T0), _ctx(responded_since_last=False))
        assert after_user_spoke(s).ignored == 0

    def test_scene_reserve(self):
        from yueli.awareness.budget import SCENE_RESERVED_EVENT_SLOTS
        roomy = initial_state(T0)
        roomy.used = 2
        tight = initial_state(T0)
        tight.used = DAILY_BUDGET - SCENE_RESERVED_EVENT_SLOTS
        assert decide_scene(roomy, _ctx()).allow is True
        d = decide_scene(tight, _ctx())
        assert d.allow is False and d.reason == 'scene-reserve'
        assert decide(tight, _ctx()).allow is True


# ─── Look ─────────────────────────────────────────────────────────────────────

NOW = 1_000_000


def _look_input(**kw) -> LookInput:
    defaults = dict(context='steam-library', now=NOW, last_call_at=0,
                    context_since=NOW - 30_000, delta=0.0, window_changed=False)
    defaults.update(kw)
    return LookInput(**defaults)


class TestLook:
    def test_idle_glance_after_3min(self):
        d = should_look(_look_input(context_since=NOW - IDLE_GLANCE_AFTER_MS - 60_000))
        assert d.look is True and d.reason == 'idle-glance'

    def test_no_immediate_glance_on_enter(self):
        d = should_look(_look_input(context_since=NOW - 30_000))
        assert d.look is False

    def test_glance_has_interval(self):
        d = should_look(_look_input(context_since=NOW - 30 * 60_000,
                                    last_call_at=NOW - IDLE_GLANCE_INTERVAL_MS + 1))
        assert d.look is False

    def test_cooldown_boundary(self):
        assert within_look_cooldown(NOW, NOW - LOOK_COOLDOWN_MS) is False
        assert within_look_cooldown(NOW, NOW - LOOK_COOLDOWN_MS + 1) is True

    def test_frame_change_triggers(self):
        d = should_look(_look_input(delta=FRAME_CHANGE_THRESHOLD + 0.1))
        assert d.look is True and d.reason == 'frame-change'

    def test_game_folder_only_on_window_change(self):
        assert should_look(_look_input(context='game-folder',
                                       context_since=NOW - 60 * 60_000)).look is False
        assert should_look(_look_input(context='game-folder', window_changed=True)).reason == 'folder-switch'


# ─── Monitor ─────────────────────────────────────────────────────────────────

class TestMonitor:
    def test_first_call_marks_changed(self):
        m = ForegroundProcessMonitor()
        obs = m.observe(FI('Code.exe'))
        assert obs.process_changed is True and obs.window_changed is True

    def test_same_window_no_change(self):
        m = ForegroundProcessMonitor()
        m.observe(FI('Code.exe', title='hello'))
        obs = m.observe(FI('Code.exe', title='hello'))
        assert obs.process_changed is False and obs.window_changed is False

    def test_title_change_detects_window_change(self):
        m = ForegroundProcessMonitor()
        m.observe(FI('Code.exe', title='file1.ts'))
        obs = m.observe(FI('Code.exe', title='file2.ts'))
        assert obs.process_changed is False and obs.window_changed is True

    def test_title_not_in_observation(self):
        """标题不持久保存在 monitor 内部状态里，只在本轮 observation 中透传。"""
        m = ForegroundProcessMonitor()
        obs = m.observe(FI('Code.exe', title='工资表.xlsx'))
        # 监控器的内部状态只存了指纹和进程名，不存原始标题
        assert '工资' not in str(m._last_process)
        assert '工资' not in str(m._last_window_fingerprint)
