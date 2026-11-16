"""0002 seed：平台层种子数据（**全部从 packages/shared 的单一真相源派生**）。

灌入内容
--------
1. ``goods_item``：**从 ``contracts/goods.json`` 读取**（服务端绝不复制价格/额度常量）；
2. ``clarifier_templates``：从 ``contracts/intents.json`` 每个 intent 的
   ``clarifier_templates`` 逐槽位生成（**平台级，无 tenant_id**）；
3. ``template_rule``：从每个 intent 的 ``sample_utterances`` 生成平台层模板
   （``tenant_id`` 为 NULL；``pattern_hash`` 用归一化文本的 sha256，保证可回归去重）；
4. ``num_normalization_rules``：global 层基础中文数字词表（T03 归一器共用，
   只做「词 -> 数字」映射，不含任何业务规则）。

幂等性与主键
------------
所有主键用 ``uuid.uuid5`` 从业务键派生，**同一份契约重复执行得到同一批 id**，
便于跨环境对比与重放；本迁移按 Alembic 语义只执行一次。

Revision ID: 0002_seed
Revises: 0001_initial
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any, Sequence

from alembic import op

from app.config import load_contracts
from app.models.pay import GoodsItem
from app.models.rule import ClarifierTemplate, NumNormalizationRule, TemplateRule

revision: str = "0002_seed"
down_revision: str | None = "0001_initial"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

#: 模板文本归一化（去空白/标点、转小写）——只用于计算 pattern_hash
_NOISE_PATTERN = re.compile(r"[\s，,。.、！!？?：:；;“”\"'（）()【】\[\]…—~·]+")

#: uuid5 命名空间（固定值，保证跨环境可复现）
SEED_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

#: global 层基础中文数字词表（仅词法映射，不含业务规则）
NUMERIC_WORDS: tuple[tuple[str, str], ...] = (
    ("零", "0"),
    ("一", "1"),
    ("二", "2"),
    ("两", "2"),
    ("三", "3"),
    ("四", "4"),
    ("五", "5"),
    ("六", "6"),
    ("七", "7"),
    ("八", "8"),
    ("九", "9"),
    ("十", "10"),
    ("半", "0.5"),
)


def normalize_pattern(text: str) -> str:
    """模板归一化：去标点空白 + 小写（与 T03 解析链路的归一化口径一致）。"""
    return _NOISE_PATTERN.sub("", text).strip().lower()


def pattern_hash(text: str) -> str:
    """归一化文本的 sha256（去重键的一半，与 version/intent_id 共同唯一）。"""
    return hashlib.sha256(normalize_pattern(text).encode("utf-8")).hexdigest()


def stable_uuid(*parts: str) -> uuid.UUID:
    """由业务键派生稳定 UUID（重复执行结果一致）。"""
    return uuid.uuid5(SEED_NAMESPACE, "|".join(parts))


def goods_rows(contracts: Any) -> list[dict[str, Any]]:
    """道具行：严格来自 ``goods.json`` 的 ``goods[]``（不硬编码价格/额度）。"""
    rows: list[dict[str, Any]] = []
    for item in contracts.goods.get("goods", []):
        rows.append(
            {
                "goods_id": str(item["goods_id"]),
                "goods_name": str(item.get("name_zh") or item["goods_id"]),
                "goods_type": str(item["goods_type"]),
                "price_fen": int(item["price_fen"]),
                "quota_amount": int(item.get("quota_amount", 0)),
                "validity_days": int(item.get("validity_days", 0)),
                "status": str(item.get("status", "on_shelf")),
            }
        )
    return rows


def clarifier_rows(contracts: Any) -> list[dict[str, Any]]:
    """追问话术行：来自 intents.json 的 ``clarifier_templates``（平台级，无 tenant_id）。"""
    rows: list[dict[str, Any]] = []
    for intent in contracts.intents:
        intent_id = str(intent["intent_id"])
        for slot_code, question in (intent.get("clarifier_templates") or {}).items():
            text = str(question).strip()
            if not text:
                continue
            rows.append(
                {
                    "clarifier_id": stable_uuid("clarifier_templates", intent_id, str(slot_code), "default"),
                    "intent_id": intent_id,
                    "slot_code": str(slot_code),
                    "band": "default",
                    "question": text,
                    "options": None,
                }
            )
    return rows


def template_rows(contracts: Any) -> list[dict[str, Any]]:
    """平台层规则模板：每个 intent 的样例话术各一条（tenant_id 保持 NULL）。"""
    rows: list[dict[str, Any]] = []
    for intent in contracts.intents:
        if not bool(intent.get("template_hit", True)):
            continue
        intent_id = str(intent["intent_id"])
        for utterance in intent.get("sample_utterances", []):
            pattern = str(utterance).strip()
            if not pattern:
                continue
            digest = pattern_hash(pattern)
            rows.append(
                {
                    "rule_id": stable_uuid("template_rule", intent_id, digest),
                    "tenant_id": None,  # 平台层行（RLS 策略用 tenant_id IS NULL 放行）
                    "intent_id": intent_id,
                    "pattern": pattern,
                    "pattern_hash": digest,
                    "version": 1,
                    "priority": 100,
                    "enabled": True,
                }
            )
    return rows


def numeric_rows() -> list[dict[str, Any]]:
    """global 层中文数字词表（平台级：tenant_id / member_id 均为 NULL）。"""
    rows: list[dict[str, Any]] = []
    for index, (word, value) in enumerate(NUMERIC_WORDS):
        rows.append(
            {
                "rule_id": stable_uuid("num_normalization_rules", "global", word),
                "tenant_id": None,
                "member_id": None,
                "layer": "global",
                "pattern": word,
                "normalized": value,
                # 单字词优先级低于多字词（数值越小越优先），保持确定性排序
                "priority": 100 + index,
            }
        )
    return rows


def upgrade() -> None:
    """灌入平台层种子（道具 / 追问话术 / 规则模板 / 数字词表）。"""
    contracts = load_contracts()

    goods = goods_rows(contracts)
    if not goods:
        raise RuntimeError("goods.json 未提供任何道具，拒绝空种子（虚拟支付需要道具定义）")
    op.bulk_insert(GoodsItem.__table__, goods, multiinsert=False)

    op.bulk_insert(ClarifierTemplate.__table__, clarifier_rows(contracts), multiinsert=False)
    op.bulk_insert(TemplateRule.__table__, template_rows(contracts), multiinsert=False)
    op.bulk_insert(NumNormalizationRule.__table__, numeric_rows(), multiinsert=False)


def downgrade() -> None:
    """清空 0002 灌入的平台层种子（按表整体删除；这些表只存种子数据）。"""
    for table in (
        NumNormalizationRule.__table__,
        TemplateRule.__table__,
        ClarifierTemplate.__table__,
        GoodsItem.__table__,
    ):
        op.execute(table.delete())
