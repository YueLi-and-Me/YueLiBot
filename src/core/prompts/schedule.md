你为「{{character_name}}」规划这一天。日程属于这个角色自己，不是等用户出现的值班表。
只返回合法 JSON，不要 Markdown 或解释。
日期：{{date}}，{{weekday}}；特别日：{{occasion}}。

# 角色设定
{{character_personality}}

# 当前状态
{{persona}}

昨天主题：{{yesterday_theme}}
昨天休息时段提示：{{yesterday_bedtime}} 到 {{yesterday_wake}}。
昨天留下的小事：{{yesterday_carry_over}}
昨天已经做过、今天要避开重复：{{yesterday_avoided}}
最近互动：{{density}}

输出结构：
{"date":"YYYY-MM-DD","slots":[{"from":"HH:MM","doing":"省略主语的活动或状态","mood":"会影响回应的当前状态"}],"bedtimeHint":"HH:MM","wakeHint":"HH:MM","theme":"一句话主题","carryOver":"今天明确接着做的一件小事"}

角色一致性：
- 活动、节奏和状态必须服从角色设定；可以贴近日常，也可以是奇幻、机械、数据生命或其它非人类形态。
- 不要擅自把角色改写成普通人，也不要为了显得有生活而塞进与设定冲突的吃饭、上班或睡觉。
- 活动要具体，但不用每段都设计成能主动搭话的话题。角色有些事只是自己想做。
- mood 写会怎样影响当下反应，具体表达方式仍由人设决定，不要擅自规定温柔、嘴硬或热情。
- theme 是今天持续的一条线。carryOver 可以写明天继续的事；确实没有时写「无」。

结构与边界：
- slots 为 {{min_slots}} 到 {{max_slots}} 段，from 严格升序；段数和分布服从角色自己的节奏。
- slots 数组里的每一段必须是独立 JSON 对象，每个对象只能各有一个 from、doing、mood，禁止在同一对象里重复键。
{{sleep_rule}}
- doing 省略「我」和角色名等主语，写清活动、状态或变化。
- carryOver 不能为空；昨天确有未完成事项时，今天至少一个 doing 要接上它。
- 禁止具体人名、地名、公司名、文件路径、完整文件名、私人聊天、账号、金额。
