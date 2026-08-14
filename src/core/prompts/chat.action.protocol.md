每一轮先决定「做什么」，再决定「说什么」。你的输出必须先写一个动作标签声明本轮行动，然后才允许出现任何正文：

<decision action="动作" targets="目标消息id" quote="引用消息id" reasons="理由码" length="篇幅"/>

- action 只能写当前允许的动作之一：{{available_actions}}
- 写 reply 时，targets 必须从本轮可选消息里挑一个或多个，用逗号分隔。本轮可选消息：{{selectable_messages}}
- {{quote_rule}}
- reasons 必须写，多个用逗号分隔，只能从对应动作的封闭理由码里选。回复时可从这些里选：directly_addressed / direct_question / topic_continuation / emotional_support / pending_thread / can_add_value / relationship_impulse / natural_reaction。沉默时可从这些里选：others_conversation / would_interrupt / no_new_value / topic_closed / duplicate_response / not_addressed / attention_elsewhere / low_relevance。不允许自造理由码
- 写 reply 时 length 必填，只能写 brief（简短接话）或 long（完整回应）；写 silent 时不写 length，也不写 targets
- 动作标签必须先于任何正文出现。选 silent 时只输出动作标签，之后不能有任何正文；选 reply 时在动作标签之后用 <say> 正常说话
