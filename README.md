# 语音录入版轻量化进销存

> 小微商户「想到就说、说完就记」的轻量进销存。**微信小程序（手机端 + 电脑版微信大屏，同一 AppID）**
> + 微信云托管容器后端（Python 3.11 / FastAPI / SQLAlchemy 2 / Pydantic v2）+ 小程序虚拟支付 +
> 云开发云函数接发货推送。

当前进度：**T01~T05 已完成**，并通过三轮独立复验。未闭环项（R3 待办审批后的执行入口、
换算系数审批）逐条记在 `docs/03-遗留问题.md`，不是「已全部完成」，请照那份清单看。

---

## 0. 五分钟在本地看到效果（最快路径）

不用小程序、不用云托管、不用真实 ASR/大模型。本机没有语音识别和对象存储，
所以「说话」这一步用命令行代替，**后面全是真链路**（真路由、真建卡、真扣次、真写库、真库存）。

**① 建库（只需一次）**

```bash
cd server
export DATABASE_URL="sqlite+pysqlite:///./dev.db"
export JWT_SECRET="local-dev-only-0123456789abcdef"
python -m alembic upgrade head          # 34 张表 + 平台种子数据
```

**② 起后端（这个窗口别关）**

```bash
cd server
export DATABASE_URL="sqlite+pysqlite:///./dev.db"
export JWT_SECRET="local-dev-only-0123456789abcdef"
python -m uvicorn app.main:app --port 8000
```

浏览器打开 <http://127.0.0.1:8000/healthz> 和 <http://127.0.0.1:8000/docs>（本地才有的接口文档）。

**③ 另开一个窗口：登录一次 + 准备演示商品**

```bash
cd server
export DATABASE_URL="sqlite+pysqlite:///./dev.db"
curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login \
     -H "Content-Type: application/json" -d '{"code":"demo"}' > /dev/null
python scripts/demo_seed.py
```

`demo_seed.py` 会建「可乐 500ml（瓶，1 箱 = 24 瓶，已审核）」和「矿泉水」。
**为什么必须跑它**：语音建的商品基本单位默认是「个」，说「两箱」会缺换算系数被拦下，
而目前换算没有审批入口（`docs/03-遗留问题.md` P1-3b），所以演示前先把换算备好。

**④ 说一句话，看它出卡、确认、落账**

```bash
python scripts/demo_say.py "进了两箱可乐，一件三十五" --yes   # 自动确认
python scripts/demo_say.py                                  # 交互模式，一句一句说
```

能跑通的说法（规则层直接命中，不依赖大模型）：

| 你说 | 会发生什么 |
|---|---|
| `进了两箱可乐，一件三十五` | 出确认卡（明细 2 箱 × ¥35 = ¥70，库存 0 → 48），确认后单号 `CG…` |
| `卖了五瓶可乐，一瓶两块五` | 出确认卡（库存 48 → 43），确认后单号 `XS…` |
| `今天卖了多少` / `今天赚了多少` | 直接回答采购/销售额，**不出卡、不扣次** |
| `可乐别名叫'快乐水'` | 出卡，确认后写别名；**紧接着** `进了两箱快乐水，一件三十五` 就能认出来 |
| `新建商品雪碧` | 出卡，确认后商品档案里真的有「雪碧」 |

本地**跑不通**的说法（会返回 `E_LLM_SCHEMA_INVALID`）：`撤销上一单`、`把上一单更正一下`。
原因是这两句在契约里 `template_hit=false`，必须靠真实大模型理解，而本机 `LLM_PROVIDER=fake`
没有模型。**这不是代码坏了**，接上真模型即可；撤销也可以直接调接口验证：
`POST /api/v1/docs/{doc_id}/reversal`。

> `python` 若不在 PATH，用托管版本：
> `C:/Users/YHC/.workbuddy/binaries/python/envs/default/Scripts/python.exe`

---

## 1. 一眼看懂形态（不要误解）

| 项 | 结论 |
|---|---|
| 端形态 | **只有微信小程序**，同一 AppID 同一入口；电脑版微信大屏靠 `app.json` 的 `resizable: true` + 双栏布局适配 |
| 网页端 | **没有**。不做 H5、不做独立网页管理端、**无 Vite/React 工程** |
| 账号 | **没有账号密码**：`wx.login` 的 code → 服务端换 JWT（T02） |
| 后端 | **微信云托管容器**，小程序经 `callContainer` 私有协议调用：免域名 / 免 ICP 域名备案 / 免 HTTPS 证书 |
| 音频 | 端侧直传对象存储，`callContainer` **只传 `audio_object_ref` 引用**（请求体 ≤100K），音频默认不留存 |
| 收款 | 小程序**虚拟支付**一次性道具（30 天权益 / 语音加油包），**无自动续费**，不引导外部付款 |
| 发货 | **只以发货推送为准**（云开发云函数 `xpay_deliver_notify`），前端 `success` 不算；`outTradeNo` 幂等 |
| R3 高危动作 | 语音只生成待办，**只能在电脑版微信大屏管理页**（同 JWT + `role=boss` + 二次确认）执行 |
| 备案 | 云托管免的是**域名/ICP 备案**；**小程序备案（AppID 备案）必须做**，是上线与开支付前置 |

---

## 2. 目录结构

```
语音录入版进销存/
├─ docs/                        # 5 份设计基线（PRD v1.1 / 架构 v1.1 / 速览 / 类图 / 时序图）
├─ miniprogram/                 # 微信小程序（手机端 + 电脑版微信大屏，同一份代码）
│  ├─ app.ts / app.json（resizable:true）/ app.wxss / project.config.json（libVersion 3.0.0）
│  ├─ pages/index/              # T01 自检页：后端健康状态 + 契约计数 + 云托管初始化引导
│  ├─ services/api.ts           # 唯一出网入口：callContainer + 包络解包 + Idempotency-Key
│  ├─ services/idempotency.ts   # 60s 内同作用域复用幂等键（防连点）
│  └─ utils/format.ts           # 展示层格式化（分/日期/单位双显）
├─ packages/shared/             # ★ 端/服共享契约（单一真相源，两端只读）
│  ├─ contracts/intents.json    # 38 个 intent：risk R0~R3 / confirm C0~C3 / 槽位 / 样例句 / 副作用 / 反做
│  ├─ contracts/slots.json      # 18 个槽位（+ CTX 作为 internal_marker）
│  ├─ contracts/source-enum.json# 5 值：asr / default / computed / alias / pending
│  ├─ contracts/error-codes.json# 25 个错误码（含 v1.1 新增 5 个）
│  ├─ contracts/domains.json    # 12 个业务域
│  ├─ contracts/goods.json      # 虚拟支付道具（auto_renew 恒 false）
│  ├─ contracts/api-envelope.ts # 包络 + 解包 + ApiError
│  ├─ schemas/*.json            # parse-result / confirm-card / voice-session / entitlement
│  └─ utils/{money,date,unit}.ts + parity-cases.json   # 双端同语义 + 语言中立用例表
├─ server/                      # 微信云托管容器后端
│  ├─ app/main.py / config.py / deps.py
│  ├─ app/api/v1/{health,auth}.py   # GET /healthz、GET /readyz、POST /api/v1/auth/login
│  ├─ app/common/{envelope,errors,idempotency}.py
│  ├─ app/db/{base,session}.py      # 命名约定 / 跨方言类型 / ORM 全局 scope / 能力自描述
│  ├─ app/models/*.py               # ★ 34 张表（按域拆文件）
│  ├─ app/tenant/{context,rls,repository,cache,guards}.py  # ★ 九道防线里的 5 道
│  ├─ app/schemas/auth.py / app/services/{auth,wx_provider}.py
│  ├─ app/utils/{money,date_utils,unit}.py
│  ├─ migrations/{env.py,script.py.mako,versions/{0001_initial,0002_seed}.py} + alembic.ini
│  ├─ tests/{test_contract_sync,test_utils_parity,test_health,
│  │        test_migration_rls_ddl,test_tenant_scope_no_rls,
│  │        test_rls_cross_tenant,test_auth,test_readyz}.py
│  ├─ pyproject.toml / Dockerfile / .dockerignore / README.md
├─ cloudfunctions/xpay_deliver_notify/   # 发货推送云函数（T01 骨架：验签 + 幂等占位 + TODO）
└─ infra/
   ├─ docker-compose.yml / .env.example  # 仅本地联调（生产由云托管托管）
   └─ ci/{lint.yml,test.yml,tenant-guard-scan.py}
```

---

## 3. 怎么跑

### 3.1 后端（本地）

```bash
cd server
pip install -e ".[dev]"          # 或用隔离 venv
uvicorn app.main:app --port 8000
curl -s http://127.0.0.1:8000/healthz
pytest -q
```

启动即校验契约，不符就 fail-fast（不允许带病上线）。

### 3.2 小程序

1. 微信开发者工具 → 导入 `miniprogram/` 目录；
2. 把 `project.config.json` 的 `appid` 换成真实 AppID；
3. 把 `miniprogram/services/api.ts` 的 `CLOUD_ENV` 换成真实云开发环境 id，
   `CLOUD_SERVICE` 与云托管服务名一致；
4. 基础库需 ≥ 2.23.0（`callContainer` 要求；工程默认 `libVersion: 3.0.0`）；
5. 编译后首页点「初始化云托管」→ 应显示 `38 / 18 / 5 / 12` 与错误码个数，即端服契约同版本。

本地无云托管时：把 `services/dev-channel.ts` 的 `DEV_MODE` 改成 `true`
（它默认 `false`，且 `server/tests/test_miniprogram_prod_channel.py` 会断言默认值——
**看完界面记得改回 `false`，否则测试会红**），并在开发者工具里勾选
「详情 → 本地设置 → 不校验合法域名」，即可直连本机 uvicorn——**仅开发态**。

手机端首页的录音链是「录音 → 直传对象存储 → 只传引用」，本机没有云开发对象存储，
所以**录音那一键在本地点不通**，能看的是页面结构与各页 UI；要说一句话看效果，用
上面 §0 的 `scripts/demo_say.py`。

### 3.3 容器（仅本地联调）

```bash
cd infra
cp .env.example .env
docker compose up --build            # http://127.0.0.1:8000/healthz
```

### 3.4 CI 骨架

```bash
python infra/ci/tenant-guard-scan.py     # 裸 SQL 是否带 tenant_id（当前 0 命中）
```

`infra/ci/lint.yml` / `test.yml` 为 GitHub Actions 定义：ruff、契约 JSON 校验、多租户守卫、
小程序 tsc、pytest、`/healthz` 冒烟。

### 3.5 小程序模板编译检查（**仅 Windows 本地**）

`tsc --noEmit` 只证明 TS 类型没问题，**证明不了 WXML/WXSS 能编译**。开发者工具自带官方
编译器，命令行可直接调用，**不需要开 IDE 服务端口**（`cli.bat open` 会报
`IDE service port disabled`，那条路走不通）：

```text
<微信开发者工具>/resources/app.asar.unpacked/node_modules/wcc-exec/wcc.exe    # WXML
<微信开发者工具>/resources/app.asar.unpacked/node_modules/wcc-exec/wcsc.exe   # WXSS
```

```bash
# 全量检查：遍历 *.wxml / *.wxss（跳过 node_modules），逐个编译
python miniprogram/scripts/check-templates.py

# 编译器不在默认位置时（或用环境变量 WCC / WCSC）
python miniprogram/scripts/check-templates.py --wcc "D:/wechatSDK/resources/app.asar.unpacked/node_modules/wcc-exec/wcc.exe" \
                                             --wcsc "D:/wechatSDK/resources/app.asar.unpacked/node_modules/wcc-exec/wcsc.exe"
```

两条必须知道的坑：

1. **`wcc.exe` / `wcsc.exe` 的退出码恒为 0**（实测：故意写坏语法时同样是 `EXIT=0`）。
   所以脚本的判定口径是「**输出非空即失败**」，不看 returncode；
2. **GitHub Actions 的 Linux runner 上没有 `wcc.exe` / `wcsc.exe`**（PE 可执行文件，
   也没有对应的 Linux 版）。找不到编译器时脚本打印 `[skip]` 并 `exit 0`：
   那**不代表「模板没问题」，只代表「模板层这一关没验」**。
   **CI 全绿 ≠ 模板层验过**，发版前必须在 Windows 本地跑一次。

当前状态：`WXML 12 + WXSS 13 = 25 个文件，零错误`。

---

## 4. T01 边界（做了什么 / 没做什么）

**已完成**
- 38 intent / 18 槽位 / 5 source / 12 域 / 25 错误码 / 4 份 JSON Schema / 道具契约，端服单一真相源；
- 后端 `GET /healthz`（含契约计数）、统一包络、`AppError` 异常处理器、404/422 也走包络；
- 幂等键中间件（Redis `SETNX EX 60`，不可用降级内存 LRU）、100K 请求体守卫；
- 双端同语义工具（金额分 / ISO 时间 / 单位归一）+ 语言中立用例表；
- 小程序入口、首页自检、`callContainer` 封装、幂等键生成、展示格式化；
- 发货推送云函数骨架（验签入口 + `outTradeNo` 幂等占位 + 明确 TODO）；
- pytest 三条测试全绿；CI 骨架与租户守卫脚本可跑。

**未做（属 T03+）**
- 语音六层漏斗、ASR/LLM 适配、澄清状态机、代价护栏、音频临时上传凭证（T03）；
- 确认卡片 7 区块、只读影响预演 + CAS、写账/反做单、虚拟支付下单/发货/权益发放、API 全集（T04）；
- 双端完整 UI（语音大按钮、卡片、待确认、大屏管理页、导出转发微信）（T05）。

---

## 4.5 T02 边界（数据层 + 多租户基座 + 登录）

**已完成**
- **34 张表**按架构 §4.3.2 全量落地（`app/models/*.py`，按域拆文件），`EXPECTED_TABLES=34`；
- **迁移**：`server/migrations/versions/{0001_initial,0002_seed}.py`，`alembic upgrade head`
  正向可跑、`--sql` 可离线渲染、SQLite 建表可跑通；
- **PG RLS**：32 张表 `ENABLE` + **`FORCE`** + `CREATE POLICY`（含根表 `tenant`）；平台级
  `goods_item` / `clarifier_templates` 豁免；可空租户表用 `tenant_id IS NULL OR ...`；
  `member` 追加 `member_login_lookup` 只读策略解决 `openid -> tenant_id` 的鸡生蛋问题；
- **九道防线的应用层四道**：`TenantRepository` 强制 scope、ORM 全局 scope
  （`do_orm_execute` + `with_loader_criteria`）、`contextvar` 上下文、缓存 key
  `t:{tenant_id}:{store_id}:{biz}:{id}`；外加 CI 静态扫描（已补强到能扫 f-string 拼出的 SQL）；
- **登录**：`POST /api/v1/auth/login`（`{code}` -> JWT），首次登录自动开通
  租户 + 主仓 + boss 角色 + 免费权益 + 当月额度；`tenant_version` 变更后旧 token 立即失效；
  **不新增错误码**（仍 25 条，复用 `E_AUTH_INVALID` / `E_TENANT_DENIED` / `E_ROLE_DENIED`）；
- **`/readyz`**：DB 连通、实际表数、`isolation_mode`、JWT 密钥是否仍为默认值；`/healthz`
  结构保持 T01 冻结不变。

**如实标注的两件事**
1. **本机没有 PostgreSQL**（无 Docker / 无 psql / 无本机实例），所以 RLS 采用
   「Alembic 离线 DDL 逐表断言」作为主证据，外加 SQLite 应用层隔离测试；
   `tests/test_rls_cross_tenant.py` 只在设置 `PG_TEST_URL` 时运行，否则 `skip` 并把
   替代证据写进 skip reason。**上线前必须在真 PG 上复查一次**（`relforcerowsecurity`、
   `pg_policies`、应用账号非 `BYPASSRLS`）。
2. **非 PG 方言没有 RLS**：`Database.capabilities()` 会返回 `rls_enabled=False`、
   `isolation_mode="app_scope_only"`，`/readyz` 的 `strength` 标为
   `reduced_one_layer_less`（隔离强度下降一层）。代码里不存在「假装 SQLite 有 RLS」的路径。

---

## 5. 两个需要知道的「文档收口」

1. **槽位 18 vs 19**：PRD §2.2 与架构 §8.4 的表格实际列出 19 个代号（多一个 `CTX`），文中却写「18 个」。
   本仓库的收口：`CTX` 归为 `internal_markers`（`is_slot=false`，表示连续录单态的上下文继承），
   槽位 = **18**，`CTX` 定义保留、语义不丢。`tests/test_contract_sync.py` 对此有断言。
2. **文档里的成本 / 延迟 / 阈值 / 平台规则**（100K、2~3s、30/min、iOS 12%、备案时长等）全部是
   **假设，需核实**；代码注释也照此标注，禁止把「需核实」写成事实。平台规则 2026-11-15 检索，
   会变，上线前逐条复核。

---

## 6. 下一步

T03 语音解析流水线与成本护栏 → T04 卡片/写账/支付 → T05 双端 UI 与端到端联调。
每个任务的验收项见 `docs/02-架构设计.md` §8.3。

**T02 留下的必做复查项**（本机做不到）：接上 PostgreSQL 后

```bash
cd server
export DATABASE_URL='postgresql+psycopg://user:pwd@host:5432/vil'
alembic upgrade head
PG_TEST_URL="$DATABASE_URL" pytest -q tests/test_rls_cross_tenant.py
```
