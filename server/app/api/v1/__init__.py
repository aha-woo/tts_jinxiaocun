"""v1 接口集合。

T01 仅 ``health``；T03 增 ``voice``/``cards``/``clarify``，T04 增 ``confirm``/``reversal``/
``query``/``usage``/``dict``/``exports``/``tasks``/``account``/``entitlement``/``upload``，
T02 增 ``auth``。新增接口时请同步 docs/02-架构设计.md §5.1 的 API 清单。
"""

from app.api.v1 import health

__all__ = ["health"]
