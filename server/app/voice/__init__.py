"""语音解析流水线（六层漏斗）—— 架构 §4.2 的 G0~G6。

| 层 | 模块 | 成本 |
|---|---|---|
| G0 误触门禁 | :mod:`app.voice.gate` | 0（端侧） |
| G1 时长/VAD 门禁 | :mod:`app.voice.gate` | 0（端侧） |
| G2 非命令过滤 | :mod:`app.voice.non_command_filter` | 0（规则） |
| G3 规则/模板层 | :mod:`app.voice.template_rule` + :mod:`app.voice.template_engine` | 0（最大省钱点） |
| G4 LLM 兜底 | :mod:`app.voice.llm_fallback` | **唯一 LLM 计费点** |
| G5 置信度/澄清 | :mod:`app.voice.confidence`、:mod:`app.voice.clarify_fsm` | 0（话术静态） |
| G6 卡片/预演 | T04 ``card_builder`` | 0（DB 只读） |

模块地图（22 个文件，按职责分四组）
----------------------------------
**门禁与过滤（G0~G2）**

- :mod:`app.voice.gate`：误触（连点）、0.8s 下限、15s 软上限 / 20s 硬上限、VAD tail 300ms
- :mod:`app.voice.asr_provider`：ASR 抽象（**只吃 ``audio_object_ref``，不吃字节**）
- :mod:`app.voice.asr_calibration`：同音纠正 + 置信度打折 + 三档划界
- :mod:`app.voice.non_command_filter`：非命令判定链（无汉字 → 闲聊 → 静态意图 → 60s 去重 → 业务动词 → 放行）

**规则层（G3，0 次 LLM）**

- :mod:`app.voice.template_rule`：槽位片段抽取器 + 规则表 + 匹配器（优先级链）
- :mod:`app.voice.template_engine`：原话 → ``ParseResult`` 的完整编排
- :mod:`app.voice.slot_filler`：契约缺省规则（D/PS/W/C/S/U/UP/TA）+ 必填校验 + 金额自洽
- :mod:`app.voice.alias_resolver`：商品/个人词表最长匹配（命中多个不猜）
- :mod:`app.voice.number_normalizer`：中文数字归一（幺/拐/勾、三五歧义、一百二省尾）
- :mod:`app.voice.unit_converter`：单位换算（**缺系数一律 pending，不臆造**）
- :mod:`app.voice.amount_calculator`：单价/总价判定与反算（整数分 + ``ROUND_HALF_UP``）
- :mod:`app.voice.conflict_detector`：PRD §2.6 的 **12 类冲突**（阻断级冲突不得出卡）

**LLM 层（G4，唯一计费点）**

- :mod:`app.voice.llm_provider`：``LlmProvider`` 协议 + 计数/假/真三种实现（误用立刻炸）
- :mod:`app.voice.prompt_builder`：注入预算（Top20 商品 + 最近 3 条上下文）+ 静态/动态分离
- :mod:`app.voice.prompt_cache`：前缀缓存 + 结果缓存（TTL + LRU）
- :mod:`app.voice.llm_fallback`：Schema 强校验 + 最多 1 次修正重试 + 超时降级（不扣次）

**输出与澄清（G5/G6）**

- :mod:`app.voice.output_schema`：``ParseResult`` 强校验（规则层与 LLM 层**共用**）
- :mod:`app.voice.confidence`：置信度合成（0.85 / 0.6 分档）
- :mod:`app.voice.clarifier_templates`：静态追问话术（2~3 个可点选项，不调 AI）
- :mod:`app.voice.clarify_fsm`：澄清状态机（**≤2 轮**、一次 1 槽、点选后只做本地更新）

编排入口是 :class:`app.voice.pipeline.VoicePipeline`；本包只做「文本 -> ParseResult」，
不碰数据库写入，也不构建确认卡片（那是 T04）。
"""

from __future__ import annotations

__all__: list[str] = []
