# 云函数 `xpay_deliver_notify`（小程序虚拟支付发货推送 · 主路径）

## 为什么是云函数
决策卡 A10 / 架构 D13：小程序虚拟支付「发货推送」官方支持**云开发云函数接收，无需自建服务器与域名**。
这样才能保持「免域名 / 免 ICP 域名备案 / 免 HTTPS 证书」，与云托管 `callContainer` 私有链路一致。

## 铁律
| 规则 | 说明 |
|---|---|
| 发货只以推送为准 | 小程序 `wx.requestVirtualPayment` 的 `success` **不作发货依据** |
| 幂等键 `outTradeNo` | **只有服务端**的 `deliver_event UNIQUE(tenant_id, out_trade_no)` 是权威；重复推送 → `E_DELIVER_DUPLICATE`(1402)，不重复发额度 |
| 验签失败不发货 | 返回 `E_PAY_SIGN_INVALID`，不落任何权益 |
| 发放内容 | 只加 `usage_quota` 额度 / 延长 `entitlement.valid_until`；**不写 `usage_ledger`** |
| 兜底 | 推送丢失走 `query_order` 定时查单补发货（仍走验签 + 幂等） |
| 时段 | 平台规则 2026-11-15 检索，会变；上线前逐条复核 |

## 交付边界
- [x] 统一包络返回、错误码与 `error-codes.json` 对齐
- [x] 验签入口 + 缺密钥即拒绝
- [x] **真发请求**到云托管 `POST /api/v1/entitlement/deliver-callback`（`X-Wx-Channel: cloudfunc` + `X-Service-Token` + `X-Tenant-Id`，8s 超时）
- [x] **不吞异常**：未配置 / 令牌错 / 网络断 / 服务端 5xx / 服务端说「没发货」→ 一律返回非 0 业务码 + `delivered:false`，让微信重推
- [x] 日志必打 `OutTradeNo`（`console.log('[deliver] …', { outTradeNo, … })`）
- [x] **删除本地「抢占 deliver_event」**：两层幂等互相打架会把「失败后重推」判成重复，导致付了钱永远不发货
- [ ] TODO(T03)：真实验签算法（字段排序、时间戳容差）+ 单测（签名错 / 过期 / 篡改）

## 环境变量（云开发控制台配置，禁止入库）
| 变量 | 用途 |
|---|---|
| `XPAY_APP_KEY` | 现网 AppKey（**沙箱 Key 不得用于生产**） |
| `DELIVER_CALLBACK_URL` | 云托管 `/api/v1/entitlement/deliver-callback` 内网地址 |
| `SERVICE_TOKEN` | 服务间令牌，**必须与云托管同名环境变量完全一致**（服务端用 `hmac.compare_digest` 比对） |

> 运行时要能跑：需要 Node 18+（全局 `fetch`）。低于 18 时函数会显式报 `E_DELIVER_NOT_WIRED` 并在日志里说明，**不会**伪装成发货成功。

## 部署
1. 微信开发者工具 → 云开发 → 云函数 → 上传并部署（含 `node_modules`）；
2. 在「虚拟支付 → 发货推送」里把地址指向本云函数；
3. 在云托管服务里配置同名 `SERVICE_TOKEN`（未配置时服务端返回 **503**，不会放行）；
4. 用沙箱订单跑一次重复推送，确认第二次返回 `1402` 且额度只加一次。
