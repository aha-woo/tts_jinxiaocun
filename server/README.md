# server · 微信云托管容器后端（FastAPI）

> T01：**基础设施 + 共享契约**（`/healthz`、统一包络、幂等键中间件、双端同语义工具）。
> T02：**数据层 + 多租户基座 + 登录**（34 张表、PG RLS、`TenantRepository`、`/readyz`、
> `POST /api/v1/auth/login`）。
> 语音解析流水线在 T03，写账 / 支付 / 权益在 T04。**本项目没有网页前端工程**。

## 本地启动

```bash
cd server
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

uvicorn app.main:app --port 8000          # http://127.0.0.1:8000/healthz
pytest -q                                 # 契约 + 工具同语义 + 健康检查 + 数据层/RLS + 登录
```

启动即 fail-fast：`packages/shared` 契约缺失或数量不符（非 38 intent / 18 slot / 5 source /
错误码 < 20）会直接抛 `ContractError`，服务起不来。

本地默认 `DATABASE_URL=sqlite+pysqlite:///:memory:`（无 RLS，`/readyz` 会如实标
`app_scope_only`）；要连真库就注入 `DATABASE_URL` 并先跑迁移：

```bash
export DATABASE_URL='postgresql+psycopg://user:pwd@host:5432/vil'   # 方案 A（生产口径）
alembic upgrade head                                               # 建表 + RLS 三件套 + 种子
alembic upgrade head --sql > schema.sql                            # 离线渲染（无需连库）
```

## 接口

| 路径 | 方法 | 说明 |
|---|---|---|
| `/healthz`（别名 `/api/v1/healthz`） | GET | 存活探针：状态 + 版本 + 契约计数 + 幂等后端 + 能力边界（**结构 T01 已冻结**） |
| `/readyz`（别名 `/api/v1/readyz`） | GET | 就绪探针：DB 连通 + 实际表数 34 + 隔离能力自描述 + JWT 密钥是否仍是默认值 |
| `/api/v1/auth/login` | POST | `{code}` -> `{jwt, tenant_id, store_id, role, tenant_version, ...}` |
| `/` | GET | 能力说明（明确「无独立网页端」） |

响应包络固定为 `{code:int, data:any, message:str}`，`code=0` 成功；失败时 `code` 为
`packages/shared/contracts/error-codes.json` 里的 `numeric`，并在 `data.error_code` 带字符串码。

```json
{
  "code": 0,
  "data": {
    "status": "ok",
    "version": "0.1.0-T01",
    "contract": { "intents": 38, "slots": 18, "sources": 5, "error_codes": 25, "domains": 12 }
  },
  "message": "ok"
}
```

## 关键实现说明

- **契约单一真相源**：服务端**只读** `packages/shared/contracts/*`，不复制第二份；契约变更先改
  `docs/` 再改契约，`tests/test_contract_sync.py` 会二次校验。
- **槽位 18 个的口径**：PRD §2.2 / 架构 §8.4 的表格实际列了 19 个代号，与「18 个」的说法冲突。
  收口方式：`CTX`（上下文继承）属 `internal_markers`（`is_slot=false`），槽位因此为 18，信息不丢。
- **幂等键**：`Idempotency-Key` 头必填于写接口；Redis 可用则 `SETNX EX 60`，不可用自动降级内存
  LRU；重复请求返回首次结果（`X-Idempotent-Replay: 1`），处理中重复返回 `E_DUPLICATE_REQUEST`。
  DB 唯一约束是第二道保险（T02/T04）。
- **金额**：一律整数分（`*_fen`），元→分用 `Decimal` + `ROUND_HALF_UP`，禁止 `float * 100`。
- **时间**：存储 ISO 8601 UTC，展示按租户时区；相对时间必须解析成具体日期并提示跨月确认。
- **单位**：只归一到 `base_unit`，换算系数只来自 `unit_conversion`；无系数返回
  `needs_teaching=True`，**绝不臆造**。
- **100K 守卫**：请求体超过 100K 直接 `E_AUDIO_TOO_LARGE`（`callContainer` 限制；音频只传
  `audio_object_ref` 引用，D11）。

## 数据层与多租户（T02）

### 选型：方案 A 为主、方案 B 可切

| | 方案 A（生产，默认） | 方案 B / 本地测试 |
|---|---|---|
| 存储 | 外挂托管 PostgreSQL | SQLite / 云开发数据库 |
| DB 级隔离 | **RLS**（`ENABLE` + **`FORCE`** + `CREATE POLICY`） | **无** |
| 代码路径 | 完全一致（`TenantRepository` + ORM 全局 scope） | 完全一致 |
| `capabilities()["isolation_mode"]` | `db_rls` | `app_scope_only` |
| `capabilities()["rls_enabled"]` | `True` | **`False`** |

**如实声明**：非 PG 方言下**没有 RLS**，隔离强度下降一层，只剩
`TenantRepository` 强制 scope + ORM 全局 scope + 唯一索引 `tenant_id` 打头 + CI 静态扫描
+ 双租户越权单测。任何代码都不得在 SQLite 上声称 RLS 生效（`/readyz` 会写明
`strength: reduced_one_layer_less`）。

### 34 张表（架构 §4.3.2 全量）

| 归属层级 | 表 | 数量 | RLS |
|---|---|---|---|
| 根表 | `tenant` | 1 | ✅（连根表也强制隔离，不给特权） |
| 平台级（**无 tenant_id**） | `goods_item`、`clarifier_templates` | 2 | ⛔ 豁免 |
| tenant_id 可空（平台层 + 租户层并存） | `template_rule`、`template_candidate`、`num_normalization_rules` | 3 | ✅ 用 `tenant_id IS NULL OR ...` |
| 租户级（其余） | store / member / role / product / product_alias / unit_conversion / partner / partner_alias / inventory / doc_header / doc_line / ar_ap / payment / voice_session / voice_log / confirm_card / usage_quota / usage_ledger / personal_dict / correction_event / reversal / pending_task / audit_log / deletion_request / export_job / entitlement / virtual_order / deliver_event | 28 | ✅ |

### RLS 落地细节（容易漏的三点）

1. **必须 `FORCE`**：只有 `ENABLE` 时**表 owner 会绕过 RLS**，是最常见的「假隔离」。
2. **策略用 `current_setting('app.tenant_id', true)`**（第二个参数 `missing_ok`）：
   不带 `true` 时，未设置 GUC 的连接（迁移、运维脚本、登录引导阶段）执行任何查询都会
   `unrecognized configuration parameter` 把库打挂；带 `true` 时返回 NULL，比较恒为 NULL，
   语义变成「没有租户上下文 = 一行都看不见」的安全拒绝。
3. **登录鸡生蛋**：`openid -> tenant_id` 反查发生在租户上下文建立之前。解法是
   `app.auth_bootstrap` 会话标志 + `member` 上一条只授 `SELECT` 的 `member_login_lookup` 策略，
   **必须同时满足「无租户上下文」+「标志为 on」**；开通租户时先绑新 `tenant_id` 的 GUC
   再写行，否则 `tenant` 表自己的 `WITH CHECK` 会把 INSERT 拒掉。

### 缓存与导出

- 缓存 key 只允许由 `tenant_cache_key()` 拼：`t:{tenant_id}:{store_id}:{biz}:{id}`，
  缺上下文直接抛错，**不做无前缀兜底**；
- `TenantRepository.export_rows()` 行集强制带租户条件（T04 导出 Job 复用）。

### 迁移与验收

```bash
alembic upgrade head                       # 正向建表 + RLS + 种子
alembic upgrade head --sql > schema.sql    # 离线渲染 PG DDL（本机无 PG 时的主证据）
alembic downgrade base                     # 回滚
```

`tests/test_migration_rls_ddl.py` 直接断言渲染文本：逐表 `CREATE TABLE` 含 `tenant_id`、
`ENABLE` + **`FORCE`** + `CREATE POLICY ... current_setting('app.tenant_id'`、唯一约束
以 `tenant_id` 打头、平台级表豁免、SQLite 方言下不渲染任何 RLS。
`tests/test_rls_cross_tenant.py` 只在设置了 `PG_TEST_URL` 时跑（真实 RLS 行为），
未设置时 `skip` 并把替代证据写进 skip reason。

### 种子数据

`migrations/versions/0002_seed.py`：道具**从 `contracts/goods.json` 读取**（服务端不复制价格/额度
常量）；追问话术来自 `intents.json` 的 `clarifier_templates`；平台层 `template_rule` 来自
`sample_utterances`；`num_normalization_rules` 只灌 global 层中文数字词法表。主键用 `uuid5`
从业务键派生，跨环境可复现。

## 目录

```
app/
  main.py              # create_app / lifespan / 中间件（幂等、100K 守卫）/ Database 初始化
  config.py            # Settings + 契约装载与强校验（唯一入口）+ EXPECTED_TABLES=34
  deps.py              # 依赖注入：Settings/契约/幂等/DB 会话/TenantContext(JWT)
  api/v1/health.py     # /healthz（存活）+ /readyz（就绪，DB & RLS 状态只在这里）
  api/v1/auth.py       # POST /auth/login
  common/envelope.py   # {code,data,message}
  common/errors.py     # AppError -> 包络（含 404/422）
  common/idempotency.py# Redis SETNX + 内存 LRU 降级
  db/base.py           # 命名约定 / 跨方言类型 / TenantMixin / PLATFORM_TABLES 白名单
  db/session.py        # Engine+Session 工厂 / ORM 全局 scope / Database.capabilities()
  models/*.py          # 34 张表，按域拆文件（tenant→org→catalog→…→pay）
  tenant/context.py    # contextvar 租户上下文（缺上下文即抛错）
  tenant/rls.py        # set_config / RLS 三件套 DDL / 能力自描述（非 PG 如实标 no-RLS）
  tenant/repository.py # PlatformRepository / TenantRepository（唯一数据访问入口）
  tenant/cache.py      # tenant_cache_key：t:{tenant_id}:{store_id}:{biz}:{id}
  tenant/guards.py     # 角色 / 归属 / 租户状态守卫
  schemas/auth.py      # LoginRequest / LoginResponse
  services/auth.py     # JWT 签发校验 + 首次登录开通租户
  services/wx_provider.py # FakeWxProvider（仅本地/测试）/ RealWxProvider（jscode2session）
  utils/{money,date_utils,unit}.py   # 与 packages/shared/utils/*.ts 同语义
migrations/
  env.py               # 在线/离线双模式；URL 优先级 attributes > DATABASE_URL > alembic.ini
  script.py.mako
  versions/0001_initial.py   # 34 张表 + PG RLS 三件套 + member_login_lookup
  versions/0002_seed.py      # 平台层种子（道具从 goods.json 读）
alembic.ini            # 纯 ASCII（alembic 用 locale 编码读 ini，中文注释会炸）
tests/
  test_contract_sync.py       # 38/18/5/N + 风险/确认组合 + schema 对齐
  test_utils_parity.py        # 读 packages/shared/utils/parity-cases.json
  test_health.py              # TestClient：包络、幂等复用、100K、fail-fast
  test_migration_rls_ddl.py   # **主证据**：Alembic 离线 DDL 逐表断言 RLS/FORCE/UNIQUE
  test_tenant_scope_no_rls.py # SQLite 双租户交叉 + contextvar 并发 + 缓存前缀 + CI 扫描
  test_rls_cross_tenant.py    # 真实 PG 行为（PG_TEST_URL 缺失时 skip 并写明替代证据）
  test_auth.py                # 登录/开通/token 失效/错误码冻结/provider 注入
  test_readyz.py              # 就绪探针 + /healthz 结构冻结
```

## 容器

```bash
# 构建上下文必须是仓库根（镜像需要 packages/shared）
docker build -f server/Dockerfile -t voice-inventory-lite:local .
docker run --rm -p 8000:80 voice-inventory-lite:local
```

生产由**微信云托管**托管（`callContainer` 私有协议，免域名/免 ICP 域名备案/免 HTTPS 证书）；
云托管内不部署有状态服务，PG/Redis 必须外挂托管（D7/§4.5）。默认公网域名仅供接口测试，
**不能用于生产**。小程序备案（AppID 备案）与域名备案无关，是上线与开支付的前置。

## 待办（T03+）

- T03：`app/voice/*`（六层漏斗）、`app/domain/intent_registry.py`、`app/services/{quota,upload_ticket}.py`
- T04：`app/pay/*`、`app/services/{ledger,reversal,entitlement}.py`、API 全集
- 运维：把 PG 的**应用账号**建成非 `BYPASSRLS`、非表 owner（`FORCE` 之外的第二重保证），
  并用 `tests/test_rls_cross_tenant.py` 在真库上复核一次
