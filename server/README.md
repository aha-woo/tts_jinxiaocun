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
export DATABASE_URL='postgresql+psycopg://user:pwd@host:5432/vil?sslmode=require'  # 方案 A′（生产口径）
alembic upgrade head                                               # 建表 + RLS 三件套 + 种子
alembic upgrade head --sql > schema.sql                            # 离线渲染（无需连库）
```

> 生产环境请注入**完整 `DATABASE_URL`**：云开发的 `PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD`
> 这一套本项目当前**不读**（`create_engine` 只吃 URL），只注入 PG* 会静默连到 SQLite 内存库。

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

### 选型：方案 A′（云开发 PG 模式环境）为主、方案 B 可切

> **口径变更（架构 v1.1.1）**：数据层从「自己开一台外挂托管 PostgreSQL」改为
> 「**云开发 PG 模式环境自带的 PostgreSQL**」。T02 的 RLS 代码**一行都没降级**，
> 变的只是「连到哪台 PG」——不再需要外挂实例。下文所有 RLS/`FORCE`/引导策略照旧成立。

| | 方案 A′（生产，默认） | 方案 B / 本地测试 |
|---|---|---|
| 存储 | **云开发 PG 模式环境自带的 PostgreSQL**（同环境同 VPC，标准 PG 协议直连） | SQLite / 云开发数据库（传统模式） |
| DB 级隔离 | **RLS**（`ENABLE` + **`FORCE`** + `CREATE POLICY`） | **无** |
| 代码路径 | 完全一致（`TenantRepository` + ORM 全局 scope） | 完全一致 |
| `capabilities()["isolation_mode"]` | `db_rls` | `app_scope_only` |
| `capabilities()["rls_enabled"]` | `True` | **`False`** |

**如实声明**：非 PG 方言下**没有 RLS**，隔离强度下降一层，只剩
`TenantRepository` 强制 scope + ORM 全局 scope + 唯一索引 `tenant_id` 打头 + CI 静态扫描
+ 双租户越权单测。任何代码都不得在 SQLite 上声称 RLS 生效（`/readyz` 会写明
`strength: reduced_one_layer_less`）。

### ⚠️ 云开发 PG 模式：三条**建环境时定死、事后改不了**的硬约束

顺序错了**只能重建环境**（存量传统模式环境**不支持升级成 PG 模式**）。下单前逐条核对：

| # | 约束 | 错了会怎样 |
|---|---|---|
| 1 | 建云开发环境**必须在下单页选 PostgreSQL** | PG 模式**仅新建环境支持**，存量传统模式环境不支持升级 → 只能重建环境、数据全部重灌 |
| 2 | 地域**必须选上海** | 微信云托管资源在上海，跨地域**不能内网互通** → 只能走公网（延迟 + 出网费 + 白名单运维），或重建 |
| 3 | 开通云托管**必须选「系统默认配置」** | 只有它才会自动新建 VPC 并**绑定该云开发环境**；**开通后不支持更换网络设置** → 内网不通就只能重建 |

### Q25 / Q26：哪些已核实、哪些**尚未实测**（不要当成已实测写进任何文档）

> **本机现状（可复核）**：`PG_TEST_URL` 未设置、无 Docker / 无 psql / 无本机 PG 实例，
> 也**没有任何云开发环境凭证**。因此 Q25 的四项**全部未实测**，`tests/test_rls_cross_tenant.py`
> 的 5 条用例在跑全量时如实 `skip`（skip reason 里写了替代证据）。
> 下面把「已可验证的代码事实」与「必须在环境里实测的项」分开写，**不混着说**。

**① 已可用代码验证、且会缩小风险的结论（不依赖云环境）**

| 事项 | 结论 | 依据 |
|---|---|---|
| `pg_trgm` 是否必须开 | **不必须**。全仓 0 处使用（无 `CREATE EXTENSION`、无 `%>` / `similarity()` / trigram 索引） | `grep -rn "pg_trgm\|CREATE EXTENSION" server/app server/migrations` → 0 命中 |
| `pgcrypto` 是否必须开 | **不必须**。主键是**客户端生成**的 `uuid4`（`default=uuid.uuid4`），DDL 里没有 `gen_random_uuid()`；`created_at` 用内置 `now()` | `server/app/db/base.py::uuid_pk`（`mapped_column(Uuid, default=...)`，不是 `server_default`） |
| PG 版本下限（按代码用到的特性推） | **≥ 9.5**（`FORCE ROW LEVEL SECURITY` 是 9.5 才有；`jsonb` 9.4） | `app/tenant/rls.py` 的 `FORCE` + `models/base.py` 的 `JSONB` |
| 连接数预算 | 每容器实例**峰值 15 条**（`pool_size=5` + `max_overflow=10`）。若云托管最大实例数 10 → 需 **≥150** 可用连接 | `app/db/session.py::create_db_engine` |
| 生产必须注入 `DATABASE_URL` | **是**。`Settings.database_url` 只读 `DATABASE_URL`，缺省是 SQLite。**云托管的 `PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD` 这套 libpq 变量本项目当前不读**（`create_engine(url)` 只吃 URL），只注入 PG* 会静默连到 SQLite 内存库 | `app/config.py:85`、`app/db/session.py:49` |
| 加密口径 | 后端是 **psycopg3（Python）**，不是 Node 的 `pg`。Node 的 `ssl:{rejectUnauthorized:false}` 在 psycopg 里对应的是 **`?sslmode=require`**（加密但**不校验证书**）；要校验证书是 `sslmode=verify-full` + `sslrootcert` | `pyproject.toml` 依赖 `psycopg[binary]` |

> 关于「不校验证书」要说明白：`sslmode=require`（≈ `rejectUnauthorized:false`）只保证**链路加密**，
> 不防同 VPC 内的中间人。**内网直连可以接受**（可用 `require`）；一旦退成「公网 + 白名单」，
> 必须换成 `sslmode=verify-full` + `sslrootcert`，否则等于裸奔。

**② 必须在云开发环境里实测的四项（Q25，逐条给命令，别只看控制台）**

在**云托管容器内**（不是本机）跑，任一失败就要按「退路」处理：

```bash
# Q25-1 内网连通（是否真的同 VPC / 端口是否放通）
getent hosts "$PGHOST"                       # 期望解析到内网地址（10.x/172.16-31.x），不是公网 IP
python - <<'PY'
import os, psycopg
dsn = ("host=%s port=%s dbname=%s user=%s password=%s sslmode=require connect_timeout=5"
       % (os.environ["PGHOST"], os.environ.get("PGPORT", "5432"), os.environ["PGDATABASE"],
          os.environ["PGUSER"], os.environ["PGPASSWORD"]))
with psycopg.connect(dsn) as conn:
    print("connected", conn.execute("select version()").fetchone()[0])
PY

# Q25-2 主版本号（决定 FORCE RLS / jsonb / psycopg3 是否需要降级写法）
psql "$DATABASE_URL" -Atc "show server_version"

# Q25-3 连接数上限（对上表 15×实例数 的预算）
psql "$DATABASE_URL" -Atc "show max_connections"
psql "$DATABASE_URL" -Atc \
  "select count(*) from pg_stat_activity" # 与 usename 分组一起看，确认应用账号配额

# Q25-4 扩展可用性（代码不依赖，但先问清楚免得以后想加索引时才发现没权限）
psql "$DATABASE_URL" -Atc "select name from pg_available_extensions where name in ('pg_trgm','pgcrypto')"
psql "$DATABASE_URL" -Atc "select extname from pg_extension"

# Q25-5 应用账号不是超级用户、也不能 BYPASSRLS（这是 FORCE 之外的第二重保证）
psql "$DATABASE_URL" -Atc \
  "select current_user, usesuper, usebypassrls from pg_user where usename = current_user"
```

- **内网不通** → 先按架构 Q25 的退路办：改「公网 + 白名单」（此时**必须** `sslmode=verify-full`），
  或退方案 B；**不要**先动手改迁移。
- 实测通过后，把 `PG_TEST_URL` 注入 CI，`tests/test_rls_cross_tenant.py` 的 5 条会从 `skip` 变成真跑。

**③ Q26：业务表放哪个 schema —— 我的结论与理由（等架构确认后我改迁移）**

- **结论：放独立 schema，但名字不叫 `app`，改叫 `vil`**。
- **为什么不放 `public`**：PG 模式下 `anon` / `authenticated` 这类角色通常对 `public` 有默认
  privileges，云开发的数据 API 默认也只暴露 `public`。业务表放 `public` 等于把它们放进
  数据 API 的可达 schema 里，只能靠 RLS 兜（RLS 仍会挡住，但「看不见」比「看得见但查不出」强一档）。
  独立 schema 让数据 API **根本枚举不到这 34 张表**。
- **为什么不叫 `app`（与架构建议的唯一分歧）**：本项目**会话 GUC 的命名空间已经占用了 `app.`**
  ——`app.tenant_id`、`app.auth_bootstrap`、`app.deliver_bootstrap`。若 schema 也叫 `app`，
  策略里会出现 `ON app.doc_header USING (tenant_id = current_setting('app.tenant_id')::uuid)`：
  同一个 `app.` 既指 schema 又指 GUC 前缀，读代码的人和 `grep` 都会错，日后改策略极易改错。
  **保持不变式：`app.*` 只用于会话配置，`vil` 只用于业务表。**
- **落地时只有 4 个触点**（models 一行都不改，所以 563 条测试不受影响）：

  | 触点 | 改法 |
  |---|---|
  | `migrations/env.py` | PG 在线模式：`context.configure` **之前**先 `exec_driver_sql("CREATE SCHEMA IF NOT EXISTS vil")` + `SET search_path TO vil, public`，再配 `version_table_schema="vil"` |
  | `migrations/versions/0001_initial.py` | 不需要改（`create_all` 走同一个 search_path，非 schema 前缀写法保持原样） |
  | `app/db/session.py::create_db_engine` | PG 分支加 `connect_args={"options": "-csearch_path=vil,public"}`，让每条池连接都带 search_path |
  | `tests/test_migration_rls_ddl.py` | 加断言：离线渲染里有 `CREATE SCHEMA`/`version_table_schema` 装配痕迹；SQLite 分支**不得**出现任何 schema 前缀 |

- ⚠️ **顺序陷阱（必须写在这里，否则第一次部署就会踩）**：Alembic 的 `alembic_version`
  是**在跑第一个 revision 之前**建的。所以 `vil` schema 必须在 `context.run_migrations()`
  **之前**存在（`CREATE SCHEMA IF NOT EXISTS` 要放在 env.py 的 online 分支里，而不是放在
  `0001_initial.py` 里）。放错位置的后果不是报错，而是**建出第二套表**或「以为升级过了」。
- ⚠️ **两个前置未知**：① 云开发 PG 模式下应用账号**有没有 `CREATE SCHEMA` 权限**（与 Q25-5 同批测）；
  ② 若没有，得由控制台/DBA 预建 `vil` 并 `GRANT USAGE`。这两条**未实测**，所以：**Q26 的迁移改动
  等 Q25 实测回来再落地**，现在不动 `migrations/`。

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
  domain/intent_registry.py  # 38 个 intent 的行为规格（**只从契约派生**，零复制）
  voice/*.py           # 六层漏斗 22 个模块（gate/filter/template/llm/clarify/pipeline）
  services/auth.py     # JWT 签发校验 + 首次登录开通租户
  services/wx_provider.py # FakeWxProvider（仅本地/测试）/ RealWxProvider（jscode2session）
  services/quota.py    # 租户令牌桶 30/min + 1000/day（Redis Lua，内存降级）
  services/usage_meter.py # usage_ledger 唯一写入口（只写 CARD_CREATED，同事务）
  services/degrade.py  # L0~L4 逐级降级（可查可导出）
  services/cost_monitor.py # 命中率/调用率/缓存命中率/平均 tokens/单次成本
  services/upload_ticket.py # 音频票据（只收 audio_object_ref，共用 100KiB 常量）
  pay/deliver_callback.py # 虚拟支付发货（单路径 + 验签 + OutTradeNo 幂等）
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
  voice_demo.py               # T03 共享夹具（离线商品词表 + 离线流水线）
  test_template_hit.py        # **G3 命中率实测** + 模板意图 0 次 LLM
  test_conflict_snapshot_vs_increment.py # 12 类冲突逐类回归（C1 双向断言）
  test_quota_idempotency.py   # 令牌桶 + usage_ledger 幂等/同事务
  test_clarify_rounds.py      # 澄清 ≤2 轮 / 1 槽 / 2~3 选项 / 不重调 AI
  test_audio_100k.py          # 100KiB 同一常量 + 类型层禁 base64 + voice_log 只存文本
  test_deliver_idempotency.py # 验签失败不发 / 重复不重发 / 不写 usage_ledger
  test_auth_bootstrap_guard.py# ast 护栏：set_auth_bootstrap 只允许 auth.py 调用
  test_cost_guardrails.py     # cost_monitor/degrade/prompt 预算/provider 误用
```

## 语音解析与成本护栏（T03）

六层漏斗 G0~G6 全在 `app/voice/`，**规则层（G3）是唯一省钱点，LLM（G4）是唯一计费点**。
本层只做「文本 -> `ParseResult`」，不写库、不建卡片（那是 T04）。

### 规则层命中率（实测，非估算）

用 `intents.json` 的全部 `sample_utterances`（84 条）跑完整流水线：

| 口径 | 数字 |
|---|---|
| 回归集规模 | 84 条 |
| 规则层命中（停在 G2/G3） | **42 条 = 50.0%** |
| 其中 `template_hit=true` 样本 | 42 条，命中 **42/42 = 100%** |
| 非模板意图被误猜 | **0 条** |
| 18 个模板意图的模型调用 | **0 次**（`CountingLlmProvider.calls == 0` 逐意图断言） |

PRD 假设目标 ≥45%，实测 50.0%。**规则表按 intent 语义人工编写，匹配时不读取
`sample_utterances`**，样本只用于事后度量（否则「用样本命中样本」会让数字失效）。

### 三条防线的落地位置

- **不猜**：`conflict_detector` 12 类冲突（PRD §2.6 逐条），阻断级冲突不出写卡；
  单位缺系数一律 `pending` + 待教学，代码里没有「常见 24 瓶」这类兜底。
- **不超支**：`services/quota.py` 租户级令牌桶 30/min + 1000/day（Redis Lua 原子，
  无 Redis 降级内存）；`services/usage_meter.py` 是 `usage_ledger` 的**唯一写入口**，
  只写 `CARD_CREATED`（DB `CHECK` 约束兜底），与卡片插入**同一事务**。
- **不静默**：`services/degrade.py` L0→L4 逐级关能力（L3 仍能手录、L4 仍能查账），
  历史可查可导出；`services/cost_monitor.py` 给出命中率/调用率/缓存命中率/平均 tokens/单次成本。

### 音频链路（100KiB）

> **现行口径（2026-11-17 起）**：语音在**手机端**由微信「同声传译」插件转成文字，
> 只把 `asr_text` 发给 `/voice/sessions/{id}/parse`——**音频一个字节都不出手机**，
> 也不再有「上传票据 → 直传 → 只传引用」这一跳（端侧见 `miniprogram/services/speech.ts`）。
> 下面的票据链路**保留但降级为 P1 兜底**（真 ASR 接线时用），不是当前主路径。

`callContainer` 请求体有 **文本 100KiB 硬限制**，所以音频**不进请求体**：
`POST /upload/ticket` 发票据 → 端侧 `wx.cloud.uploadFile` 直传 → `/voice/*` 只收
`audio_object_ref`。两个入口共用 `upload_ticket.MAX_AUDIO_BYTES` **同一个常量**；
入参 Schema（`UploadTicketRequest`）**不定义任何音频字段**（`extra="forbid"`），
从类型层禁掉 base64；`voice_log` 只存 `asr_text`，对象引用在 `voice_session` 上，
短 TTL + 限期删除（**合规口径需核实**）。端侧转文字后，「录音不留存」从「靠 TTL 删」
变成**结构上不可能留存**——这条是插件路线的附带收益。

### 发货回调（虚拟支付）

**单路径**：只认云开发云函数消息推送（`xpay_*`）；云托管消息推送被显式拒绝（否则必然重复发货）。
四条次序：验签（失败 → `E_PAY_SIGN_INVALID`，不落 `deliver_event`、不发额度）→
`OutTradeNo` 幂等（重复 → `E_DELIVER_DUPLICATE`，不重复发）→ 发额度/权益 →
**绝不写 `usage_ledger`**（发货与扣次是两本账）。`query_order` 兜底走同一个 `deliver_once`。

#### 服务间鉴权：`ServiceDeliverDep`（不进用户态鉴权链）

发货推送是**微信侧的服务调用**，没有用户 JWT，所以**不能**挂在 `AuthContextDep` 后面
（上一版就是因此每次都 403 `E_TENANT_DENIED`，付了钱的用户拿不到额度）。
`app/deps.py::require_service_deliver` 是独立的一条链，三道判定：

| 判定 | 结果 |
|---|---|
| 服务端未配置 `SERVICE_TOKEN` | **503** + `ERROR` 日志（`reason=service_token_not_configured`）——**绝不默认放行** |
| `X-Wx-Channel` 不是 `cloudfunc` | **403**（`reason=channel_not_allowed`） |
| `X-Service-Token` 与配置不一致（`hmac.compare_digest`，bytes 比较） | **403**（`reason=service_token_mismatch`）；缺令牌与错令牌同码同文案 |

> 403 用的是契约里本就是 403 的 `E_TENANT_DENIED`（不新造错误码，改契约要走文档流程）；
> 503 用 `E_INTERNAL` + `http_status=503` 覆盖。**未配置是服务端问题**，
> 必须表现为「功能不可用，请重试/告警」，而不是「谁都能发额度」。

#### 租户从哪来：`xpay_deliver_lookup` 策略（**不用 `bypass_scope`**）

推送里只有 `out_trade_no`，而所有业务写入都受 RLS `tenant_id = current_setting('app.tenant_id')`
约束——这与「登录时 openid → tenant_id」是同一个鸡生蛋问题，解法同构：

```
开 app.deliver_bootstrap（事务级，is_local=true）
  → 按 out_trade_no 精确查 virtual_order（策略只给 SELECT，且要求 app.tenant_id 为空）
  → 立刻 set_tenant_guc(tenant_id) + set_deliver_bootstrap(False)
  → 业务写入全程走正常 RLS 过滤
```

- 策略总数：32 条租户隔离 + 2 条引导反查（`member_login_lookup` / `xpay_deliver_lookup`）
  = `config.EXPECTED_RLS_POLICIES`（34），迁移、DDL 断言、`/readyz.isolation.rls_policies_expected`
  **共用这一个常量**；
- 两个引导开关**互相独立**（`app.auth_bootstrap` ≠ `app.deliver_bootstrap`），
  共用一个开关会让「登录那一刻」也能读别人的订单；
- 单号不全球唯一（`UNIQUE(tenant_id, out_trade_no)`）：查不到 → 404，查到多租户 → 422
  要求云函数带 `X-Tenant-Id` 消歧，**绝不猜租户**。

### 静态护栏

`tests/test_auth_bootstrap_guard.py`：用 `ast` 扫全仓，断言 `set_auth_bootstrap`
的**调用点只在 `app/services/auth.py`**（定义处 `app/tenant/rls.py` 与测试文件豁免），
防止「随手关掉租户隔离」这类不报错的数据泄漏。
`infra/ci/tenant-guard-scan.py` 覆盖目录已扩到
`app/{models,voice,services,domain,api,pay}`（T04 起把 API 层与支付层也**点名**进覆盖率），
命中 0、扫描 114 个 Python 文件。

## 写账与确认卡片（T04）

三条铁律落在代码里的位置（破一条整套账就不可信）：

| 铁律 | 落地 | 证据测试 |
|---|---|---|
| **影响预演必须真只读** | `voice/impact_preview.py`：PG `SET TRANSACTION READ ONLY` / SQLite `PRAGMA query_only`；`simulate()` 是无副作用纯函数 | `test_impact_preview_readonly.py`（A/B 全表快照相等 + 同一句 INSERT 普通会话成功、只读窗口必失败） |
| **落账必须 CAS** | `domain/inventory_service.py`、`domain/ar_ap_service.py`：`UPDATE ... WHERE version = :expected`，rowcount 0 → `E_PREVIEW_VERSION_CONFLICT`(1202) | `test_preview_version_conflict.py`（预演 v1 → 别人改成 v2 → 落账返 1202 且库存保持别人的新值） |
| **任何写操作必经确认卡片** | 写账入口只有 `services/ledger.py`（确认落账）与 `services/reversal.py`（反做单）；`app/api` 里没有任何模块能直接构造单据/库存/应收 | `test_route_matrix.py`（38 个 intent 逐个断言路由 + 行为）、`test_api_surface.py`（写账路径只有两条） |

### 38 个 intent 的路由分布（实测）

| 路由 | 数量 | 写库 | 扣次 | 说明 |
|---|---|---|---|---|
| `static` | 2 | 否 | 否 | `sys.help` / `sys.example`，本地话术 |
| `answer` | 11 | 否 | 否 | R0 只读，查完就返回，**不建卡** |
| `pending_task` | 3 | 只写待办 | 否 | R3：`partner.merge` / `product.unit.set` / `report.export` |
| `card` | 22 | 是 | 是 | R1(10) + R2(12)，唯一写库入口 |

判定只写一处：`app/voice/routing.py::route_of_spec`（顺序 `static > pending_task > answer > card`）。
API 与测试**都调它**，`test_route_matrix.py` 再把「判定结论」与「真实行为」双向对齐
（判 card 就必须真能构造 7 区块卡片；判 answer/static/pending_task 就必须构造器抛错 + 落账报对应错误码）。

### 卡片与超时

- 卡片 7 区块键固定：`original / intent / slots / calc / impact / risk / actions`
  （`BLOCK_KEYS` + `ConfirmCardView.assert_complete()` 双重卡口，手机端与电脑版大屏**同一份**）；
- `source` 4 值一律带角标：`default`（缺省补全）/ `computed`（换算得到，**必须**附 `calc_trace`）/
  `alias`（按你的习惯）/ `pending`（**待确认，绝不静默填值**）；存在待确认项时第一个按钮是「先确认黄色项」；
- 主按钮文案动态带值：「确认入库 2箱 可乐 500ml ¥70.00」；
- **30s TTL**：超时 → `E_CARD_EXPIRED`(1201)，不落账、进待确认、**不重复扣次**；
  复活复用同一 `session_id`（`UNIQUE(tenant_id, session_id)` 兜底），并**重写卡片截止时刻**
  （只改状态会让复活后的卡片「一确认又过期」）；
- **禁止超时自动执行**：C2 必须二次语音**或**点按钮兜底，两者都没有就不执行。

### 撤销 = 反做单

`services/reversal.py`：原单保留、生成 `原单号-R` 的反向单据、`reversal` 表记关联、库存/应收反向再记一次。
超 10 分钟窗口 → `E_REVERSAL_WINDOW_CLOSED`(1203)（引导 `sys.correct`）；被引用
（应收应付已核销 / 已有反做单 / 有收付款流水）→ `E_DOC_REFERENCED`(1204) 并**展示引用链**。
盘点快照（`stock.take`）没有留档前值，**不能反做**（拒绝而不是猜一个数）。

反做单靠 `doc_header.reverses_doc_id`（非空 = 我撤的是那张单）自我标识，三处据此收敛：

| 位置 | 规则 |
|---|---|
| `reversal.reverse()` | 目标单 `reverses_doc_id` 非空 → `E_VALIDATION`（`cannot_reverse_a_reversal`）。**不做「撤销的撤销」**：反做单继承原意图、方向只按契约取反，再撤一次就是再冲销一次（实测 240→192→144） |
| `ledger._resolve_target_doc("上一单")` | 只挑 `status='confirmed' AND reverses_doc_id IS NULL`，不会盯上那辆 `-R` |
| `report_service._sum_docs()` | 同样排除 `reverses_doc_id IS NOT NULL`，否则「撤销后今日采购仍是 7000」 |

### 编辑不重调 AI

`voice/correction_edit.py::apply_slot_updates` 是纯函数：改数量/单价就机械重算金额，0 次模型调用。
`tests/test_correction_no_llm.py` 用 `CountingLlmProvider.calls` 钉住「编辑前后相等」，
并断言回流三件套：`correction_event` + `personal_dict` + `template_candidate(status=pending)`，
`dict_version` 递增以击穿词表缓存；候选模板**必须人工审核**才生效（一次误改不能污染所有人）。

### 虚拟支付：生产必须换微信支付平台证书

⚠️ **`PAY_VERIFIER` 在生产必须是 `platform_cert` 且注入真实平台证书**：

```bash
PAY_VERIFIER=platform_cert
WX_PLATFORM_CERT_PATH=/run/secrets/wechatpay_platform_cert.pem  # 微信支付平台证书（PEM）
WX_PLATFORM_CERT_SERIAL=<证书序列号>
```

- `app/pay/sign.py::build_verifier` 在 `env=prod` 下**缺证书直接抛 `VerifierConfigError`**，
  **不存在 HMAC 兜底路径**；`FakePlatformCertVerifier(is_production=True)` 构造即抛；
- `RealPlatformCertVerifier` 构造期校验「路径存在 + 是 PEM」，验签时 `Wechatpay-Serial`
  必须命中已知证书且未过期，缺 `cryptography` 或解析失败一律 **fail-closed**；
- 本地/测试默认 fake（显式、可发现），沙箱 HMAC（`HmacSignatureVerifier`）只用于 T03 的历史测试。

虚拟支付的另外三条：**发货只以推送为准**（前端 `success` 明确不发货，额度一点不动）、
`out_trade_no` 由幂等键 `sha256(tenant|key)` 派生（同一键必算出同一单号）、
`query_order` 兜底复用同一个 `deliver_once`（不存在第二条发货代码路径）。
价格一律读 `packages/shared/contracts/goods.json`，代码里没有 `price_fen = 数字`。

### 两套口径分开存

| 口径 | 落库 | 展示 | 不得互相推导 |
|---|---|---|---|
| 自然月 | `usage_quota.period = YYYY-MM` | `/usage`（`caliber="自然月 …"`） | 1 号买的 30 天权益会横跨两个自然月 |
| 30 天滚动 | `entitlement.valid_from / valid_until` | `/entitlement`（`caliber="30 天滚动窗口 …"`） | 同上 |

发货**只写额度/权益**，`usage_ledger` 行数必须保持 0（`tests/test_entitlement_separation.py` 直接断言）。

### 导出（R3）

`export_job` 异步任务 + 强制 `tenant_id`（子查询/JOIN 同样要带）+ 对象存储路径 `t/{tenant_id}/`
+ 5 分钟短 TTL 签名 URL；**不做网页下载**——文件经微信转发/保存/预览交付。
导出内容不含音频、不含跨租户（`ExportService.sanitize` 直接拒），每次导出写 `audit_log`。

## 容器

```bash
# 构建上下文必须是仓库根（镜像需要 packages/shared）
docker build -f server/Dockerfile -t voice-inventory-lite:local .
docker run --rm -p 8000:80 voice-inventory-lite:local
```

生产由**微信云托管**托管（`callContainer` 私有协议，免域名/免 ICP 域名备案/免 HTTPS 证书）；
云托管内不部署有状态服务：**PostgreSQL 由「云开发 PG 模式环境」自带**（同环境同 VPC 直连，
不再需要外挂实例），**Redis 仍需外挂托管**（D7/§4.5）。建环境的顺序与硬约束见上文
「云开发 PG 模式：三条建环境时定死的硬约束」——**顺序错了只能重建环境**。
默认公网域名仅供接口测试，**不能用于生产**。小程序备案（AppID 备案）与域名备案无关，是上线与开支付的前置。

## 待办（T05+）

- T05：小程序双形态 UI（手机端 + 电脑版微信大屏 `resizable` 双栏）与端到端联调
- T03 遗留：接真实 LLM/ASR 后需复验「模板意图仍 0 调用」「Schema 修正重试次数」
- T04 遗留（必须接真实证书/真实环境才能复验）：
  - 微信支付平台证书轮换（`RotatingVerifier` 已就位，缺真实证书与 `Wechatpay-Serial` 推送）；
  - `query_order` 真实查单接口（当前接受注入的 `paid` 结论）；
  - 对象存储真实签名 URL（当前 `local-signed://` 占位，TTL 逻辑已就位）；
  - PG 上的只读预演（`SET TRANSACTION READ ONLY`）与真并发 CAS 冲突（需 `PG_TEST_URL`）。
- 运维：把 PG 的**应用账号**建成非 `BYPASSRLS`、非表 owner（`FORCE` 之外的第二重保证），
  并用 `tests/test_rls_cross_tenant.py` 在真库上复核一次


## 测试约定：写操作必须断言**业务终态**

不要写 `assert count(DocHeader) == 1` 这种「有没有写单据」的断言——它挡不住
「确认成功但档案里没这个商品」「说撤销了其实多写一张正向单」这类**语义反转的记错账**。

要写：

```python
assert session.scalar(select(Product).where(Product.name == "可乐")) is not None
assert inventory.qty_base == Decimal("240")
assert original_doc.status == "reversed"          # 撤销后原单仍在、状态是 reversed
assert _count(database, UsageQuota) == 1          # 发货后额度行真的多了一条
```

参考实现：`tests/test_write_end_state.py`（15 条）、`tests/test_deliver_callback_http.py`（9 条）、
`tests/test_main_chain_http.py`（9 条，**只说 PRD 原话**走真实 HTTP：
`login → /voice/sessions → parse → confirm`）。后者覆盖「可选槽位留白不阻塞确认／必填缺失仍拦／
纯实体意图能建出商品／不许撤销反做单／报表 200 且撤销后采购归零／35 次连打只放行 30」。
后者覆盖「正确令牌 → 权益真到账（贴着 `select(Entitlement)` 的实际值断言）／缺令牌·错令牌·错通道 → 403
且 `deliver_event` 0 行、额度没动／服务端未配令牌 → 503 + ERROR 告警／重复推送 → 1402 只加一次」，
并额外要求：**发货、回调这类端点必须走 TestClient 打真实 HTTP 路径**，
不要只测 service（上一轮的 403 就是「只测 service 绕过装配」漏掉的）。
