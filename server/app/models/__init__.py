"""ORM 模型集合（34 张表，按域拆分）。

导入本包 = 完成全部模型注册（`Base.metadata` 可用于建表 / Alembic DDL 渲染）。

| 文件 | 表 |
|---|---|
| tenant.py | tenant |
| org.py | store / member / role |
| catalog.py | product / product_alias / unit_conversion |
| partner.py | partner / partner_alias |
| inventory.py | inventory |
| doc.py | doc_header / doc_line / ar_ap / payment |
| voice.py | voice_session / voice_log / confirm_card |
| quota.py | usage_quota / usage_ledger |
| dict.py | personal_dict / correction_event / reversal |
| rule.py | template_rule / template_candidate / clarifier_templates / num_normalization_rules |
| ops.py | pending_task / audit_log / deletion_request / export_job |
| pay.py | entitlement / goods_item / virtual_order / deliver_event |

计数：1+3+3+2+1+4+3+2+3+4+4+4 = **34**（架构 §4.3.2 的 33 行清单中
`template_rule` 与 `template_candidate` 同处一行，故实际表数为 34）。
"""

from app.models.base import Base
from app.models.catalog import Product, ProductAlias, UnitConversion
from app.models.dict import CorrectionEvent, PersonalDict, Reversal
from app.models.doc import ArAp, DocHeader, DocLine, Payment
from app.models.inventory import (  # noqa: F401
    CostAdjustment,
    CountLine,
    CountSession,
    Inventory,
    InventoryMovement,
    InventoryPolicy,
    StockBatch,
)
from app.models.ops import Attachment, AuditLog, DeletionRequest, ExportJob, Gap, PendingTask
from app.models.org import Member, Role, Store
from app.models.partner import Partner, PartnerAlias
from app.models.pay import DeliverEvent, Entitlement, GoodsItem, VirtualOrder
from app.models.quota import UsageLedger, UsageQuota
from app.models.rule import (
    ClarifierTemplate,
    NumNormalizationRule,
    TemplateCandidate,
    TemplateRule,
)
from app.models.tenant import Tenant
from app.models.voice import ConfirmCard, VoiceLog, VoiceSession

#: 业务表（带 tenant_id，须启用 RLS）；平台级表另行处理
TENANT_SCOPED_MODELS: tuple[type[Base], ...] = (
    Store,
    Member,
    Role,
    Product,
    ProductAlias,
    UnitConversion,
    Partner,
    PartnerAlias,
    Inventory,
    InventoryMovement,
    StockBatch,
    InventoryPolicy,
    CountSession,
    CountLine,
    CostAdjustment,
    DocHeader,
    DocLine,
    ArAp,
    Payment,
    VoiceSession,
    VoiceLog,
    ConfirmCard,
    UsageQuota,
    UsageLedger,
    PersonalDict,
    CorrectionEvent,
    TemplateRule,
    TemplateCandidate,
    NumNormalizationRule,
    Reversal,
    PendingTask,
    Gap,
    Attachment,
    AuditLog,
    DeletionRequest,
    ExportJob,
    Entitlement,
    VirtualOrder,
    DeliverEvent,
)

#: 平台级表（无 tenant_id，不参与 RLS）
PLATFORM_MODELS: tuple[type[Base], ...] = (GoodsItem, ClarifierTemplate)

#: 全部模型（含根表 tenant）
ALL_MODELS: tuple[type[Base], ...] = (Tenant, *TENANT_SCOPED_MODELS, *PLATFORM_MODELS)

__all__ = [
    "Base",
    "Tenant",
    "Store",
    "Member",
    "Role",
    "Product",
    "ProductAlias",
    "UnitConversion",
    "Partner",
    "PartnerAlias",
    "Inventory",
    "InventoryMovement",
    "StockBatch",
    "InventoryPolicy",
    "CountSession",
    "CountLine",
    "CostAdjustment",
    "DocHeader",
    "DocLine",
    "ArAp",
    "Payment",
    "VoiceSession",
    "VoiceLog",
    "ConfirmCard",
    "UsageQuota",
    "UsageLedger",
    "PersonalDict",
    "CorrectionEvent",
    "Reversal",
    "TemplateRule",
    "TemplateCandidate",
    "ClarifierTemplate",
    "NumNormalizationRule",
    "PendingTask",
    "Gap",
    "Attachment",
    "AuditLog",
    "DeletionRequest",
    "ExportJob",
    "Entitlement",
    "GoodsItem",
    "VirtualOrder",
    "DeliverEvent",
    "TENANT_SCOPED_MODELS",
    "PLATFORM_MODELS",
    "ALL_MODELS",
]
