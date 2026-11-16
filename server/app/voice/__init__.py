"""语音解析流水线（六层漏斗）—— 架构 §4.2 的 G0~G6。

| 层 | 模块 | 成本 |
|---|---|---|
| G0 误触门禁 | :mod:`app.voice.gate` | 0（端侧） |
| G1 时长/VAD 门禁 | :mod:`app.voice.gate` | 0（端侧） |
| G2 非命令过滤 | :mod:`app.voice.non_command_filter` | 0（规则） |
| G3 规则/模板层 | :mod:`app.voice.template_engine` | 0（最大省钱点） |
| G4 LLM 兜底 | :mod:`app.voice.llm_fallback` | **唯一 LLM 计费点** |
| G5 置信度/澄清 | :mod:`app.voice.confidence`、:mod:`app.voice.clarify_fsm` | 0（话术静态） |
| G6 卡片/预演 | T04 ``card_builder`` | 0（DB 只读） |

编排入口是 :class:`app.voice.pipeline.VoicePipeline`；本包只做「文本 -> ParseResult」，
不碰数据库写入，也不构建确认卡片（那是 T04）。
"""

from __future__ import annotations

__all__: list[str] = []
