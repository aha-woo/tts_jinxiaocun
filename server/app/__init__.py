"""语音录入版轻量化进销存 · 云托管后端（微信云托管容器 + FastAPI）。

分层（T01 只落地基座，业务见 T02~T04）：
- ``app/api``        接口层（统一包络）
- ``app/common``     包络 / 错误码 / 幂等
- ``app/config``     配置与共享契约装载（单一真相源入口）
- ``app/utils``      双端同语义工具（金额 / 日期 / 单位）
- ``app/voice``      T03 语音解析流水线（待建）
- ``app/domain``     T02/T04 领域模型与写账服务（待建）
"""

__all__ = ["__version__"]

#: 与服务 /healthz 的 version 字段保持一致
__version__ = "0.1.0-T01"
