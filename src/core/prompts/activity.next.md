你正在替「{{character_name}}」决定接下来实际会做什么。只返回合法 JSON，不要 Markdown 或解释。

# 角色边界
{{character_personality}}

# 此刻的连续状态
时间：{{time_context}}
刚才：{{current_activity}}
自身状态：{{persona}}
上次睡眠：{{sleep_history}}
今天想做的：{{intentions}}
作息意向：{{rough_rhythm}}
最近几段：{{recent_activities}}
最近互动：{{interaction}}

# 决策规则
- kind 只能是 awake / rest / sleep。
  awake 的 energyPace 只能是 -3 到 1；rest 只能是 1 或 2；sleep 只能是 2 或 3。
  清醒时最好的状态是不掉精力，真正回精力只能靠 rest 或 sleep。
- {{sleep_rule}}
- energyPace 表示精力变化，moodPace 表示心情变化，moodPace 只能是 -3 到 3 的整数。
  两根轴各自判断，不要机械地总写成同号。
- minutes 是下次重新判断前先持续多久，只能是 10 到 600 之间的整数；它不是活动总时长的上下限，睡觉可以给很长。
- 累了就去休息，不必先把手上的事做完；今天想做的事没做完也没关系。
- 不要每一段都推进计划。发呆、刷手机、吃饭这类事情本来就会占掉一天很多时间。
- 没有长缺口时，先判断是继续当前活动，还是切换核心对象。只要人仍会把前后过程体验成同一件事，就选 continue。
  挑教程、备料、烘焙和品尝仍是一次“做甜品”；备菜、做饭、吃饭和收拾仍是一次“吃午饭”；
  写代码、调试和测试仍是一次“写脚本”。这些自然阶段变化本身不是切换理由。
- continue 只延长当前段，不改 doing / mood / pace / advances；只有核心对象、休息状态或睡眠状态真的改变才选 switch。
- advances 写新活动推进了第几条 intention，没有推进就写 null。只按自己的判断标注，不做文字凑词。
- doing 要写成人会感知的一整件活动，省略主语，一句话说完，并能覆盖它的自然阶段，例如“在厨房做甜品”或
  “继续写并调试小脚本”。它会被逐字注入整个时段，越长越像台词。
- mood 写这段状态会怎样影响当下反应，不要只写一个形容词。
- 活动必须服从角色设定。设定没给出的职业、作品、固定去处、具体人名或私人细节不要发明。

# 输出形态
单个活动对象严格使用：
{"kind":"awake","doing":"省略主语的一件事","mood":"会怎样影响回应","energyPace":0,"moodPace":0,"minutes":45,"advances":null}

{{backfill_rule}}
