"""定义四份 TOML 对应的 Pydantic 配置模型和运行时组合视图。

持久化层由 providers/models/bot/features 四份 TOML 组成；加载器会把它们
组合为本文件末尾的 Config 运行时视图，业务服务不需要知道磁盘布局。

启动时一次性校验，字段缺失/类型错在进入任何业务逻辑之前就报错。
每个字段在 TOML 中的写入模板由 Electron 主进程维护；本模块只负责类型、范围、
字段关系和废弃字段校验。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Literal
import re

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator


class InnerConfig(BaseModel):
    """表示一份配置文件的版本信息。

    :ivar version: 当前配置格式版本，固定为 `1.1.0`。
    """

    # 1.1.0 起 model_tasks 从「一个任务一个模型名」改成候选列表 + 轮询策略。
    # 旧配置由 Electron 侧在读取时就地升级，Python 只解析当前版本。
    version: Literal['1.1.0'] = '1.1.0'


class BotConfig(BaseModel):
    """保存 Bot 名称、别名以及与用户的称呼和关系。

    :ivar name: 非空主名称。
    :ivar aliases: 非空、不重复且不等于主名称的别名列表。
    :ivar user_nickname: 用户希望使用的称呼，可为空。
    :ivar relationship: 关系描述，可为空。
    :raises pydantic.ValidationError: 名称或别名不满足非空、唯一性约束。
    """

    # Bot 的显示名和提示词身份名
    name: str
    # 群聊和私聊中可用于匹配 Bot 的其他称呼。
    aliases: List[str] = Field(default_factory=list)
    # Bot 对用户的称呼偏好；留空表示不在提示词中要求使用特定称呼。
    user_nickname: str = ''
    # Bot 和用户的关系：哥哥/姐姐/朋友/自定义文本，留空则不设定这层关系
    relationship: str = ''

    @model_validator(mode='after')
    def _validate_names(self) -> 'BotConfig':
        """规范化 Bot 名称与别名并验证非空和唯一性。

        :return: 当前完成校验的模型实例。
        :raises ValueError: 主名称为空，或别名为空、重复、等于主名称。
        副作用：更新当前模型中的 `name` 和 `aliases` 为去空白后的值。
        """
        self.name = self.name.strip()
        if not self.name:
            raise ValueError('bot.name 不能为空')
        normalized = [alias.strip() for alias in self.aliases]
        if any(not alias for alias in normalized):
            raise ValueError('bot.aliases 不能包含空字符串')
        if self.name in normalized:
            raise ValueError('bot.aliases 不要重复 bot.name')
        if len(set(normalized)) != len(normalized):
            raise ValueError('bot.aliases 不能包含重复别名')
        self.aliases = normalized
        return self


class GroupChatConfig(BaseModel):
    """白名单群进入主体后的回复策略；群准入仍由 QQ 适配器负责。"""

    at_mention_must_reply: StrictBool = True
    name_mention_probability: float = Field(default=1.0, ge=0.0, le=1.0)
    presence_decay_strength: float = Field(
        default=3.0,
        ge=0.0,
        le=20.0,
        description='群里说话占比越高，被叫到名字时越倾向于让别人先说',
    )
    # 群里一句一答的消耗远小于面对面长聊；同一倍率作用于全部人格增量。
    persona_weight: float = Field(default=0.05, ge=0.0, le=1.0)
    reply_window_minutes: int = Field(default=10, ge=1)
    max_replies_in_window: int = Field(default=3, ge=0)
    # 是否允许她给别人的消息贴 QQ 表情回应（react 动作）。
    #
    # 默认开启：语义反应名到 QQ 表情编号的映射（napcat/segments.py 的
    # REACTION_EMOJI_IDS）左列逐字取自 QQ 自己的表情名，已按协议端表情编号表逐条
    # 核对，「名字对了编号错了」这类不报错的错已经排除。剩余未知只有「某个编号是否
    # 被 set_msg_emoji_like 接受」，而那条路失败是响亮的（协议端报错并落日志）。
    #
    # 不额外给表情回应设频率预算：她要能贴表情，先得拿到这一轮候选机会，而那已经
    # 受门控与 max_replies_in_window 约束；再加一个互相牵制的窗口常量只会让行为
    # 更难推理。
    reactions_enabled: bool = True
    # 是否允许她戳一戳群里的某个人（poke 动作）。
    #
    # 与表情回应分开且**默认关闭**：贴表情是安静的，戳一戳会给对方推送提醒，
    # 扰动量级完全不同。合成一个开关就没法只开安静的那个。
    pokes_enabled: bool = False
    # 是否允许她起一个**不接任何人**的话头（speak 动作）。
    #
    # 它与 reply 的区别只在有没有目标：reply 是接某条消息，speak 是她自己想说点
    # 什么。**不需要独立的触发机制**——扩展触发口径（frequency / reply_necessity）
    # 本来就会在「群里热闹但没人理她」时给出候选，speak 只是让那个候选里多一个
    # 选项，因此它天然受同一条频率闸门约束，不会另外增加她开口的次数。
    self_started_topics: bool = True
    # 群里每积累这么多条新消息，就在后台重算一次场景画像（在聊什么、什么气氛）。
    #
    # 观察跑在对话之外，不占她开口前的等待；这个数只决定画像有多新。给 0 关闭观察，
    # 系统提示词里就没有场景块，行为与引入观察 Agent 之前一致。
    #
    # 不再给它配第二个「最小间隔」常量：条数本身已经是节流器——消息来得慢就自然
    # 算得少，来得快才算得勤，这正是我们想要的。
    scene_refresh_messages: int = Field(default=15, ge=0)


class ConversationAgentConfig(BaseModel):
    """Conversation 行动核心的灰度开关、触发口径与测试流清单。

    mode 为四段灰度：off 保留旧管线；shadow 对 DELIBERATE 候选调用
    Conversation Agent 只记录决策、不改可见行为；selected_streams 只对
    清单内的 stream 启用真实决策；enabled 把全部真实候选交给 Agent。

    trigger_mode 决定无点名/无 @ 的普通群消息何时进入 DELIBERATE：
    ``signal`` 沿用原口径，无信号直接 DROP；``frequency`` 按发言频率预算
    攒够候选消息后给一次 DELIBERATE；``reply_necessity`` 按回复必要性
    评分是否达到阈值决定，内容信号为主、积压压力为辅。
    ``frequency_talk_value`` 同时为 reply_necessity 提供压力归一化的
    消息条数尺度。两种扩展模式都不会让纯沉默自动触发，也不会绕过休眠、
    频率硬上限等确定性边界。

    ``max_cognitive_rounds`` 控制 ReAct 回环：一个回合内她最多可以先做几次
    认知动作（recall / inspect）再给出终局动作。**每一次都是一次完整的模型
    往返，直接加在首字延迟上**，因此上界很小；置 0 即关闭回环、退回单轮，
    与引入 ReAct 之前的行为逐字相同，是零风险回退开关。
    """

    mode: Literal['off', 'shadow', 'selected_streams', 'enabled'] = 'off'
    selected_streams: List[str] = Field(default_factory=list)
    trigger_mode: Literal['signal', 'frequency', 'reply_necessity'] = 'signal'
    frequency_talk_value: float = Field(default=0.6, gt=0.0, le=1.0)
    reply_necessity_threshold: int = Field(default=80, ge=0, le=100)
    max_cognitive_rounds: int = Field(default=2, ge=0, le=4)


class TypingNudgeConfig(BaseModel):
    """久等之后看见对方打字时的催促发言开关与阈值。

    只在私聊生效：协议端只为私聊推送输入状态，群里盯着某个人的输入框也不合适。
    触发条件是「她说完话后对方长时间没回，直到现在才开始打字」，这一条件本身
    已经足够稀有，因此不再叠加额外的频率窗口。
    """

    # 是否允许据输入状态催促；关闭后输入状态通知只被记录后丢弃。
    enabled: bool = True
    # 她上次发言后对方至少静默这么久才考虑催，单位为分钟。取值需大于 0。
    peer_silence_minutes: float = Field(default=5.0, gt=0.0)
    # 同一段静默里最多催几次；催得再多就从「等急了」变成缠人。0 等同于关闭。
    max_per_silence: int = Field(default=2, ge=0)


class TypingConfig(BaseModel):
    """她把一段话打出来的节奏：分几条气泡、每条之间隔多久。

    模型按语义写 ``<say>``，一个 ``<say>`` 常常仍是完整的一长句；这里的参数决定
    它再被切成几条、以及每条发出前停顿多久，使多条消息呈现真人的打字节奏而不是
    脚本连发。切分与停顿共用同一套假设，因此收在同一段配置里。
    """

    # 单条气泡的目标字数：先按标点切成最小片段，再贪心合并到接近该长度为止。
    bubble_target_chars: int = Field(default=18, gt=0)
    # 一条台词最多切成几条气泡；话越长每条也越长，条数不随长度无限增长。
    max_bubbles_per_say: int = Field(default=3, ge=1)
    # 是否模拟打字停顿；关闭后多条气泡会连续发出，等同于所有延迟为 0。
    delay_enabled: bool = True
    # 中文按整字输入的秒数；拉丁字母与数字连打明显更快，因此分开计价。
    chinese_char_seconds: float = Field(default=0.28, ge=0.0)
    latin_char_seconds: float = Field(default=0.12, ge=0.0)
    # 打完到按下回车之间的固定停顿，单位为秒。
    send_gap_seconds: float = Field(default=0.4, ge=0.0)
    # 单条气泡的等待上限，单位为秒；长气泡按字数线性算会让对方干等。
    max_delay_seconds: float = Field(default=8.0, ge=0.0)
    # 挑一张表情包所需的时间，单位为秒；表情包不逐字打，不走字数公式。
    emoji_pick_seconds: float = Field(default=1.5, ge=0.0)
    nudge: TypingNudgeConfig = Field(default_factory=TypingNudgeConfig)


class ScheduleConfig(BaseModel):
    """日程形状与作息开关；这些是用户选择，不由解析器写死。"""

    min_slots: int = Field(default=8, ge=1, le=24)
    max_slots: int = Field(default=10, ge=1, le=24)
    sleep_enabled: bool = True
    fallback_bedtime: str = '23:00'
    fallback_wake: str = '07:00'
    bedtime_day_boundary: str = '02:00'
    fallback_activity: str = Field(
        default='按自己的节奏度过这段时间',
        min_length=1,
        max_length=72,
    )
    fallback_mood: str = Field(default='状态平稳', min_length=1, max_length=40)
    fallback_theme: str = Field(
        default='按自己的节奏度过今天。',
        min_length=1,
        max_length=72,
    )
    fallback_carry_over: str = Field(
        default='无',
        min_length=1,
        max_length=72,
    )
    generation_retry_interval_minutes: int = Field(default=10, ge=1, le=1440)

    @field_validator(
        'fallback_activity',
        'fallback_mood',
        'fallback_theme',
        'fallback_carry_over',
    )
    @classmethod
    def _strip_fallback_text(cls, value: str) -> str:
        """去除日程备用文本空白并拒绝空内容。

        :param value: 日程备用活动、心情、主题或承接文本。
        :return: 去除首尾空白后的文本。
        :raises ValueError: 文本为空或只包含空白。
        副作用：不修改原字符串。
        """
        normalized = value.strip()
        if not normalized:
            raise ValueError('日程备用文本不能为空')
        return normalized

    @model_validator(mode='after')
    def _validate_schedule(self) -> 'ScheduleConfig':
        """验证日程槽位范围和三个时间字段的 `HH:MM` 格式。

        :return: 当前完成校验的日程配置。
        :raises ValueError: 最小槽位数大于最大槽位数，或时间不在合法范围内。
        副作用：不修改配置字段。
        """
        if self.min_slots > self.max_slots:
            raise ValueError('schedule.min_slots 不能大于 schedule.max_slots')
        for field_name, value in (
            ('fallback_bedtime', self.fallback_bedtime),
            ('fallback_wake', self.fallback_wake),
            ('bedtime_day_boundary', self.bedtime_day_boundary),
        ):
            match = re.fullmatch(r'(\d{2}):(\d{2})', value)
            if match is None or int(match.group(1)) > 23 or int(match.group(2)) > 59:
                raise ValueError(f'schedule.{field_name} 必须是合法 HH:MM 时间')
        return self


class PersonalityConfig(BaseModel):
    """用户可以在 bot.toml 完整改写的人设与说话风格。"""

    birthday: str
    personality: str
    reply_style: str
    tone_probability: float = Field(ge=0.0, le=1.0)
    tone_variants: List[str]
    expression_habits: List[str]
    proactive_expression_habits: List[str]

    @model_validator(mode='before')
    @classmethod
    def _reject_retired_fields(cls, value: Any) -> Any:
        """拒绝已删除的人格字段，避免旧配置被静默解释。

        :param value: Pydantic before 校验阶段的原始人格映射。
        :return: 不含废弃字段的原始值。
        :raises ValueError: 发现 `identity`、`behavior`、`attention` 或 `boundaries`。
        副作用：不修改输入映射。
        """
        if not isinstance(value, dict):
            return value
        migration_errors = (
            ('identity', 'personality.identity 已改名为 personality.personality，请把内容挪过去。'),
            ('behavior', 'personality.behavior 已取消：接话方式并进 reply_style。'),
            ('attention', 'personality.attention 已取消：接话方式并进 reply_style。'),
            ('boundaries', 'personality.boundaries 已取消：边界与事实纪律现在由固定提示词资源维护，不再可配。'),
        )
        for field_name, message in migration_errors:
            if field_name in value:
                raise ValueError(message)
        return value

    @field_validator('birthday')
    @classmethod
    def _validate_birthday(cls, value: str) -> str:
        """校验生日为空或合法的过去日期。

        :param value: `YYYY-MM-DD` 格式的生日文本，可以为空字符串。
        :return: 原始生日文本；空值保持为空。
        :raises ValueError: 格式错误、日期不存在或日期晚于当前日期。
        副作用：读取本地当前日期，不修改模型外状态。
        """
        if not value:
            return value
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) is None:
            raise ValueError('personality.birthday 必须使用 YYYY-MM-DD 格式')
        try:
            birthday = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError('personality.birthday 必须是合法日期') from exc
        if birthday > date.today():
            raise ValueError('personality.birthday 不能晚于今天')
        return value

    @field_validator('tone_variants', 'expression_habits', 'proactive_expression_habits')
    @classmethod
    def _validate_text_lists(cls, value: List[str]) -> List[str]:
        """规范化人格文本列表并拒绝空条目。

        :param value: 临时语调、表达习惯或主动表达习惯列表。
        :return: 每项去除首尾空白后的新列表。
        :raises ValueError: 任一条目为空或只包含空白。
        副作用：不修改输入列表。
        """
        normalized = [variant.strip() for variant in value]
        if any(not variant for variant in normalized):
            raise ValueError('personality 的文本列表不能包含空字符串')
        return normalized


class ConversationConfig(BaseModel):
    """对话窗口、摘要节奏和记忆召回数量。"""

    working_memory_messages: int = Field(default=40, ge=8, le=200)
    summarize_trigger_messages: int = Field(default=48, ge=12, le=500)
    summarize_batch_messages: int = Field(default=16, ge=4, le=200)
    session_gap_minutes: int = Field(default=30, ge=1, le=1440)
    fact_recall_limit: int = Field(default=6, ge=0, le=50)
    recalled_episode_limit: int = Field(default=2, ge=0, le=20)
    recent_episode_limit: int = Field(default=2, ge=0, le=20)
    episode_context_limit: int = Field(default=3, ge=0, le=30)

    @model_validator(mode='after')
    def _validate_summary_window(self) -> 'ConversationConfig':
        """确保摘要批次、触发阈值和工作记忆窗口之间存在可用余量。

        :return: 当前完成校验的对话配置。
        :raises ValueError: 批次不小于触发阈值，或触发后剩余窗口超出工作记忆容量。
        副作用：不修改配置字段。
        """
        if self.summarize_batch_messages >= self.summarize_trigger_messages:
            raise ValueError('summarize_batch_messages 必须小于 summarize_trigger_messages')
        remaining = self.summarize_trigger_messages - self.summarize_batch_messages
        if remaining > self.working_memory_messages:
            raise ValueError(
                'summarize_trigger_messages - summarize_batch_messages '
                '不能大于 working_memory_messages'
            )
        return self


class GenerationTaskConfig(BaseModel):
    """单类模型调用的采样参数；max_tokens=0 表示交给厂商决定。"""

    temperature: float = Field(default=0.85, ge=0.0, le=2.0)
    max_tokens: int = Field(default=0, ge=0, le=1_000_000)

    @model_validator(mode='before')
    @classmethod
    def _reject_thinking(cls, value: Any) -> Any:
        """拒绝已从任务级配置移除的 `thinking` 字段。

        :param value: 原始 generation 配置映射。
        :return: 未含废弃字段的原始值。
        :raises ValueError: 显式提供 `thinking` 字段。
        副作用：不修改输入映射。
        """
        if isinstance(value, dict) and 'thinking' in value:
            raise ValueError(
                'generation.<任务>.thinking 已经取消。思考模式现在是模型属性：'
                '请在 models.toml 里为该任务注册一个 extra_body 关掉思考的模型条目，'
                '再把 model_tasks.<任务>.model_list 指向它。'
            )
        return value

    @property
    def token_limit(self) -> int | None:
        """把零值最大 token 配置转换为 provider 使用的可选值。

        :return: `max_tokens` 大于零时返回原值，否则返回 `None`。
        副作用：不修改配置。
        """
        return self.max_tokens or None


class ProactiveGenerationTaskConfig(GenerationTaskConfig):
    """主动系统的采样参数和总开关。关闭时 Electron 不安装全局键鼠钩子。"""

    enabled: bool = True


class GenerationConfig(BaseModel):
    """按任务拆分参数，避免改视觉模型时意外改变普通聊天。"""

    chat: GenerationTaskConfig = Field(default_factory=GenerationTaskConfig)
    proactive: ProactiveGenerationTaskConfig = Field(
        default_factory=lambda: ProactiveGenerationTaskConfig(temperature=0.9, max_tokens=200)
    )
    summary: GenerationTaskConfig = Field(
        default_factory=lambda: GenerationTaskConfig(temperature=0.3, max_tokens=0)
    )
    schedule: GenerationTaskConfig = Field(
        default_factory=lambda: GenerationTaskConfig(
            temperature=0.95,
            max_tokens=4096,
        )
    )
    expression: GenerationTaskConfig = Field(
        default_factory=lambda: GenerationTaskConfig(
            temperature=0.1,
            max_tokens=4096,
        )
    )
    vision: GenerationTaskConfig = Field(
        default_factory=lambda: GenerationTaskConfig(temperature=0.3, max_tokens=120)
    )


class TtsConfig(BaseModel):
    """语音合成的功能参数。地址、密钥、协议都在它的候选模型里，不在这里。"""

    # 不开就是纯文字，其余功能不受影响
    enabled: bool = False
    voice: str = ''
    format: Literal['mp3', 'wav', 'opus'] = 'mp3'
    # 陪伴场景略慢一点更自然，太快像播报
    speed: float = Field(default=0.95, ge=0.25, le=4.0)
    # 仅 volcengine 协议使用：豆包语音的集群名
    cluster: str = 'volcano_tts'


class VisionConfig(BaseModel):
    """定义视觉功能开关和截图范围。

    :ivar enabled: 是否允许桌面屏幕视觉调用。
    :ivar chat_image_enabled: 是否允许 QQ 聊天图片视觉描述。
    :ivar fullscreen_silent: 疑似全屏时是否静默，默认值为 `True`。
    :ivar capture_mode: 截取前台窗口或整个主屏，默认值为 `window`。
    """

    # 视觉请求可能包含屏幕中的敏感信息；服务不落盘并将图片缩放到 768px 宽，
    # 但远程 base_url 仍会接收图像内容。使用本地推理地址时，图像不会离开本机。
    enabled: bool = False
    # 聊天图片是否调用视觉模型描述；与桌面屏幕视觉独立开关。
    chat_image_enabled: bool = False
    # 疑似全屏时保持静默，避免直播或录屏场景输出桌宠声音。
    fullscreen_silent: bool = True
    # 截图范围：window 仅捕获前台窗口（默认）；screen 捕获整个主屏，包含当时可见的
    # 桌面、任务栏和其他窗口。整屏模式会扩大上传范围，使用远程模型时应明确评估隐私边界。
    capture_mode: Literal['window', 'screen'] = 'window'

    @property
    def ready(self) -> bool:
        """返回视觉功能是否已启用。

        :return: `enabled` 的布尔值。
        副作用：不读取截图或模型连接状态。
        """
        return self.enabled


class PerceptionConfig(BaseModel):
    """屏幕情境获准出现的出口；群聊在配置形状上永久排除。"""

    surfaces: List[Literal['desktop', 'direct']] = Field(
        default_factory=lambda: ['desktop']
    )

    @field_validator('surfaces', mode='before')
    @classmethod
    def _validate_surfaces(cls, value: object) -> object:
        """校验屏幕情境允许出现的会话表面。

        :param value: 原始 surfaces 配置，必须为列表。
        :return: 原始合法列表，供 Pydantic 继续转换。
        :raises ValueError: 值不是列表、含未知表面或尝试启用群聊。
        副作用：不修改输入列表。
        """
        if not isinstance(value, list):
            raise ValueError('perception.surfaces 必须是列表，可填 desktop 或 direct')
        invalid = [item for item in value if item not in ('desktop', 'direct')]
        if 'group' in invalid:
            raise ValueError(
                '群聊不能启用屏幕情境：群消息会被多人看见，屏幕内容一旦发出无法撤回'
            )
        if invalid:
            raise ValueError(
                f'perception.surfaces 只能填写 desktop 或 direct，收到：{invalid}'
            )
        return value


class VectorConfig(BaseModel):
    """定义向量混合召回功能的开关。

    :ivar enabled: 是否启用向量召回；具体 embedding 模型由任务路由配置。
    """

    # 向量混合召回，默认关；还需 pip install yueli[vector]
    # 用哪个 embedding 模型由 model_tasks.embedding 决定，不在这里重复。
    enabled: bool = False


class LogConfig(BaseModel):
    """日志等级、落盘与控制台样式。"""

    # 全局兜底等级
    level: str = 'INFO'
    # 终端和文件各自的等级，留空跟随 level
    console_level: str = ''
    file_level: str = ''
    # 控制台等级列：lite 只体现在时间戳颜色上，compact 显示单字母，full 显示全称
    level_style: Literal['lite', 'compact', 'full'] = 'lite'
    # 着色范围：none 不着色，title 只染时间戳与模块名，full 连正文一起染
    color_scope: Literal['none', 'title', 'full'] = 'full'
    date_format: str = '%m-%d %H:%M:%S'
    # 关掉则只剩控制台和 WebUI 推流
    to_file: bool = True
    # 单个日志文件上限，超过就换新文件
    file_max_bytes: int = Field(default=5 * 1024 * 1024, ge=64 * 1024)
    # 最多保留几个文件，超出的从最旧的删起
    max_files: int = Field(default=30, ge=1, le=1000)
    # 超过这些天的文件直接清掉，0 表示只按数量
    cleanup_days: int = Field(default=14, ge=0, le=3650)
    # 按库名压噪音，没列出的库跟随 level
    library_levels: Dict[str, str] = Field(
        default_factory=lambda: {'httpx': 'WARNING', 'httpcore': 'WARNING', 'PIL': 'WARNING'}
    )
    # 完全不要的库，一行都不输出
    suppress_libraries: List[str] = Field(default_factory=lambda: ['urllib3'])
    # 模型调用失败时，把实际发出的请求体存进 logs/llm_request/（密钥已隐去）
    request_snapshots: bool = True
    max_snapshot_files: int = Field(default=50, ge=1, le=1000)
    # 每次模型调用（成功也算）按任务分目录存进 logs/prompt/<任务>/，密钥已隐去。
    # 多级 Agent 下同一回合会有多次调用，只看控制台无法还原是哪一级的问题。
    prompt_records: bool = True
    # 每个任务子目录保留的记录份数；按任务分别计数，高频任务不挤掉低频任务
    max_prompt_records_per_task: int = Field(default=200, ge=1, le=5000)
    # 事件账本保留上限
    event_retention_count: int = Field(default=20_000, ge=1)
    event_retention_hours: int = Field(default=72, ge=0)


class AdvancedConfig(BaseModel):
    """保存全局高级运行参数。

    :ivar https_proxy: 可选的 HTTP(S) 代理地址，默认值为空字符串。
    """

    # 全局 HTTP(S) 代理，例如 http://127.0.0.1:7890
    https_proxy: str = ''


class ApiProviderConfig(BaseModel):
    """providers.toml 中的一条可复用连接定义。"""

    name: str
    kind: str
    base_url: str = ''
    api_key: str = ''
    auth_type: Literal['bearer', 'header', 'query', 'none'] = 'bearer'
    auth_name: str = ''
    # openai = OpenAI 兼容协议，支持对话、视觉、向量和 TTS 任务。
    # volcengine = 豆包语音私有协议，仅允许绑定 TTS 任务。
    client_type: Literal['openai', 'volcengine'] = 'openai'
    # 豆包语音要 App ID + Access Token 两个凭证，api_key 放 Access Token
    app_id: str = ''
    # 模型列表端点，用于 WebUI 连通性测试与模型拉取；OpenAI 兼容默认 /models
    model_list_endpoint: str = '/models'
    # 中转头等需要额外 HTTP 头的厂商在这里写键值；认证头仍由 auth_* 负责
    default_headers: Dict[str, str] = Field(default_factory=dict)
    # 中转头等需要固定查询参数的厂商在这里写键值
    default_query: Dict[str, str] = Field(default_factory=dict)
    # 单次 HTTP 连接与流式读取超时；首字阶段的内部重试仍受任务级首字超时整体截断。
    timeout_ms: int = Field(default=120_000, ge=1_000, le=3_600_000)
    # 同一连接内的重试次数；需要让重试跑满时，应调大任务级首字超时。
    max_retries: int = Field(default=2, ge=0, le=10)
    retry_interval_ms: int = Field(default=800, ge=0, le=60_000)

    @model_validator(mode='after')
    def _validate_auth(self) -> 'ApiProviderConfig':
        """根据客户端协议和鉴权类型验证厂商连接凭据。

        :return: 当前完成校验的厂商配置。
        :raises ValueError: 鉴权名称、API key 与鉴权类型组合不合法。
        副作用：规范化 `auth_name`，不发起网络请求。
        """
        if self.client_type != 'openai':
            return self
        self.auth_name = self.auth_name.strip()
        has_key = bool(self.api_key.strip())
        if self.auth_type in ('header', 'query') and not self.auth_name:
            raise ValueError(f'auth_type={self.auth_type} 时 auth_name 不能为空')
        if self.auth_type in ('bearer', 'none') and self.auth_name:
            raise ValueError(f'auth_type={self.auth_type} 时 auth_name 必须留空')
        if self.auth_type == 'none' and has_key:
            raise ValueError('auth_type=none 时 api_key 必须留空')
        if self.auth_type != 'none' and not has_key:
            raise ValueError(f'auth_type={self.auth_type} 时 api_key 不能为空')
        return self


class ProviderCatalog(BaseModel):
    """表示 providers.toml 的顶层结构。"""

    inner: InnerConfig
    api_providers: List[ApiProviderConfig]


class ModelDefinitionConfig(BaseModel):
    """models.toml 中的具体模型，借助 api_provider 引用厂商连接。"""

    name: str
    model_identifier: str = ''
    api_provider: str
    extra_body: Dict[str, Any] = Field(default_factory=dict)
    reasoning_parse_mode: Literal['field', 'tag', 'none'] = 'field'
    # WebUI 模型能力标记：视觉模型才应进入 vision / 图片描述任务
    visual: bool = False
    # 可选模型级温度与最大输出覆盖；留空时使用任务 generation 配置
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    # 可选计费参考价，单位元/百万 token；仅用于 WebUI 展示
    price_in: float = Field(default=0.0, ge=0.0)
    price_out: float = Field(default=0.0, ge=0.0)
    embedding_dim: int = 0

    @model_validator(mode='before')
    @classmethod
    def _reject_thinking(cls, value: Any) -> Any:
        """拒绝已从模型定义移除的顶层 `thinking` 字段。

        :param value: 原始模型定义映射。
        :return: 未含废弃字段的原始值。
        :raises ValueError: 发现 `thinking` 字段。
        副作用：不修改输入映射。
        """
        if isinstance(value, dict) and 'thinking' in value:
            raise ValueError(
                'models.*.thinking 已经取消，请把厂商参数原样写入 extra_body。'
            )
        return value


class TaskRoutingConfig(BaseModel):
    """一个任务的候选模型与轮询策略。

    model_list 排第一的是主力，其余是它挂掉之后依次顶上的备用。
    sequential = 永远优先第一条；random = 每次随机起点，把流量摊到多家。
    """

    model_config = ConfigDict(protected_namespaces=())

    model_list: List[str] = Field(default_factory=list)
    selection_strategy: Literal['sequential', 'random'] = 'sequential'
    # provider 管网络读，first_token 管切换，slow 只记账。
    first_token_timeout_ms: int = Field(default=30_000, ge=1_000)
    slow_threshold_ms: int = Field(default=8_000, ge=0)

    @field_validator('model_list')
    @classmethod
    def _reject_duplicates(cls, v: List[str]) -> List[str]:
        """拒绝任务候选列表中的重复模型名。

        :param v: 模型名称列表。
        :return: 原列表对象。
        :raises ValueError: 同一个模型名出现多次。
        副作用：不修改列表。
        """
        # 重复候选会改变轮询顺序并增加无效尝试，因此在配置加载期直接拒绝。
        if len(set(v)) != len(v):
            raise ValueError(f'model_list 存在重复模型：{v}')
        return v

    @model_validator(mode='after')
    def _validate_timing(self) -> 'TaskRoutingConfig':
        """验证慢响应阈值严格早于首 token 超时。

        :return: 当前完成校验的任务路由配置。
        :raises ValueError: 慢响应阈值非零且大于等于首 token 超时。
        副作用：不修改字段。
        """
        if self.slow_threshold_ms and self.slow_threshold_ms >= self.first_token_timeout_ms:
            raise ValueError('slow_threshold_ms 必须小于 first_token_timeout_ms，或设为 0')
        return self


class ModelTaskConfig(BaseModel):
    """保存八类任务各自的模型候选和选择策略。

    `extra='forbid'` 确保拼写错误的任务段在加载期直接失败，不会意外继承 chat 配置。
    """

    # 段名写错必须炸在加载期。留空继承 chat 是合法语义，段名打错不是——
    # 没有这一条，[model_tasks.summry] 会静默变成「跟 chat 一样」。
    model_config = ConfigDict(extra='forbid')

    chat: TaskRoutingConfig = Field(default_factory=TaskRoutingConfig)
    proactive: TaskRoutingConfig = Field(default_factory=TaskRoutingConfig)
    summary: TaskRoutingConfig = Field(default_factory=TaskRoutingConfig)
    schedule: TaskRoutingConfig = Field(default_factory=TaskRoutingConfig)
    vision: TaskRoutingConfig = Field(default_factory=TaskRoutingConfig)
    expression: TaskRoutingConfig = Field(default_factory=TaskRoutingConfig)
    tts: TaskRoutingConfig = Field(default_factory=TaskRoutingConfig)
    embedding: TaskRoutingConfig = Field(default_factory=TaskRoutingConfig)


class ModelCandidate(BaseModel):
    """把「模型定义 + 它引用的厂商连接」合成一条，是轮询的最小单位。

    运行时视图：由 loader 组装，不直接对应任何一段 TOML。
    """

    # 配置内部模型名，出现在日志里，方便对照 models.toml
    name: str
    # 引用的厂商名。熔断按厂商记——一个厂商挂了，它下面所有模型都别再撞。
    provider: str
    kind: str = ''
    base_url: str = ''
    api_key: str = ''
    auth_type: Literal['bearer', 'header', 'query', 'none'] = 'bearer'
    auth_name: str = ''
    # 发给厂商接口的真实模型 ID
    identifier: str = ''
    extra_body: Dict[str, Any] = Field(default_factory=dict)
    reasoning_parse_mode: Literal['field', 'tag', 'none'] = 'field'
    visual: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    default_headers: Dict[str, Any] = Field(default_factory=dict)
    default_query: Dict[str, Any] = Field(default_factory=dict)
    client_type: Literal['openai', 'volcengine'] = 'openai'
    app_id: str = ''
    embedding_dim: int = 0
    timeout_ms: int = Field(default=120_000, ge=1_000, le=3_600_000)
    max_retries: int = Field(default=0, ge=0, le=10)
    retry_interval_ms: int = Field(default=0, ge=0, le=60_000)


class TaskRouting(BaseModel):
    """一个任务解析完成后的候选序列。candidates 为空表示这个任务没有模型。"""

    task: str
    candidates: List[ModelCandidate] = Field(default_factory=list)
    strategy: Literal['sequential', 'random'] = 'sequential'
    first_token_timeout_ms: int = Field(default=30_000, ge=1_000)
    slow_threshold_ms: int = Field(default=8_000, ge=0)

    @model_validator(mode='after')
    def _validate_timing(self) -> 'TaskRouting':
        """验证运行时路由的慢响应阈值和首 token 超时关系。

        :return: 当前完成校验的运行时路由。
        :raises ValueError: 慢响应阈值非零且不小于首 token 超时。
        副作用：不修改路由字段。
        """
        if self.slow_threshold_ms and self.slow_threshold_ms >= self.first_token_timeout_ms:
            raise ValueError('slow_threshold_ms 必须小于 first_token_timeout_ms，或设为 0')
        return self

    @property
    def ready(self) -> bool:
        """判断该任务是否至少有一个可用模型候选。

        :return: `candidates` 非空时返回 `True`。
        副作用：不修改候选列表。
        """
        return bool(self.candidates)


class RoutingConfig(BaseModel):
    """八类任务各自的候选序列。业务侧只跟这里打交道，不再关心厂商怎么配。"""

    chat: TaskRouting = Field(default_factory=lambda: TaskRouting(task='chat'))
    proactive: TaskRouting = Field(default_factory=lambda: TaskRouting(task='proactive'))
    summary: TaskRouting = Field(default_factory=lambda: TaskRouting(task='summary'))
    schedule: TaskRouting = Field(default_factory=lambda: TaskRouting(task='schedule'))
    vision: TaskRouting = Field(default_factory=lambda: TaskRouting(task='vision'))
    expression: TaskRouting = Field(default_factory=lambda: TaskRouting(task='expression'))
    tts: TaskRouting = Field(default_factory=lambda: TaskRouting(task='tts'))
    embedding: TaskRouting = Field(default_factory=lambda: TaskRouting(task='embedding'))


class ModelCatalog(BaseModel):
    """表示 models.toml 的顶层结构。"""

    inner: InnerConfig
    model_tasks: ModelTaskConfig
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    models: List[ModelDefinitionConfig]


class BotDocument(BaseModel):
    """表示 bot.toml 的顶层结构及默认业务配置段。"""

    inner: InnerConfig
    bot: BotConfig
    group_chat: GroupChatConfig = Field(default_factory=GroupChatConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    personality: PersonalityConfig
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    conversation_agent: ConversationAgentConfig = Field(default_factory=ConversationAgentConfig)
    typing: TypingConfig = Field(default_factory=TypingConfig)

    @model_validator(mode='before')
    @classmethod
    def _require_at_mention_switch(cls, value: Any) -> Any:
        """要求 bot.toml 显式声明协议 @ 必回开关。

        :param value: Bot 配置文档的原始映射。
        :return: 包含必填开关的原始映射。
        :raises ValueError: 缺少 ``group_chat.at_mention_must_reply``。
        副作用：不修改输入映射。
        """
        if not isinstance(value, dict):
            return value
        group_chat = value.get('group_chat')
        if not isinstance(group_chat, dict) or 'at_mention_must_reply' not in group_chat:
            raise ValueError(
                'group_chat.at_mention_must_reply 必须在 bot.toml 中显式设置；'
                'true 开启 @ 必回，false 关闭'
            )
        return value


class FeatureDocument(BaseModel):
    """表示 features.toml 的顶层结构及各功能开关。"""

    inner: InnerConfig
    tts: TtsConfig
    vision: VisionConfig
    perception: PerceptionConfig = Field(default_factory=PerceptionConfig)
    vector: VectorConfig
    log: LogConfig = Field(default_factory=LogConfig)
    advanced: AdvancedConfig


class Config(BaseModel):
    """表示加载器组合后的完整运行时配置。

    生产环境由四份 TOML 显式提供所有关键字段；字段工厂只为不经过磁盘加载器的
    纯单元测试构造空配置，不提供运行时人格或模型兜底。
    """

    # 生产加载器始终显式传入 BotDocument 的各段。这里的空 Bot 只服务于
    # 不经过磁盘加载器、且与角色内容无关的纯单元测试，不提供角色信息。
    bot: BotConfig = Field(default_factory=lambda: BotConfig.model_construct(
        name='',
        aliases=[],
        user_nickname='',
        relationship='',
    ))
    group_chat: GroupChatConfig = Field(default_factory=GroupChatConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    # 生产加载器始终显式传入 BotDocument.personality。空值工厂只让不经过
    # 磁盘加载器的纯单元测试能构造其余配置段，不提供任何运行时人设兜底。
    personality: PersonalityConfig = Field(default_factory=lambda: PersonalityConfig(
        birthday='',
        personality='',
        reply_style='',
        tone_probability=0.0,
        tone_variants=[],
        expression_habits=[],
        proactive_expression_habits=[],
    ))
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    conversation_agent: ConversationAgentConfig = Field(default_factory=ConversationAgentConfig)
    typing: TypingConfig = Field(default_factory=TypingConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    # 八类任务的候选模型与轮询策略；连接细节都收在候选里
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    tts: TtsConfig = Field(default_factory=TtsConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    perception: PerceptionConfig = Field(default_factory=PerceptionConfig)
    vector: VectorConfig = Field(default_factory=VectorConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    advanced: AdvancedConfig = Field(default_factory=AdvancedConfig)
