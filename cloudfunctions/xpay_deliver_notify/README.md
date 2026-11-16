# 云函数 `xpay_deliver_notify`（小程序虚拟支付发货推送 · 主路径）

## 为什么是云函数
决策卡 A10 / 架构 D13：小程序虚拟支付「发货推送」官方支持**云开发云函数接收，无需自建服务器与域名**。
这样才能保持「免域名 / 免 ICP 域名备案 / 免 HTTPS 证书」，与云托管 `callContainer` 私有链路一致。

## 铁律
| 规则 | 说明 |
|---|---|
| 发货只以推送为准 | 小程序 `wx.requestVirtualPayment` 的 `success` **不作发货依据** |
| 幂等键 `outTradeNo` | `deliver_event UNIQUE(tenant_id, out_trade_no)`；重复推送 → `E_DELIVER_DUPLICATE`，不重复发额度 |
| 验签失败不发货 | 返回 `E_PAY_SIGN_INVALID`，不落任何权益 |
| 发放内容 | 只加 `usage_quota` 额度 / 延长 `entitlement.valid_until`；**不写 `usage_ledger`** |
| 兜底 | 推送丢失走 `query_order` 定时查单补发货（仍走验签 + 幂等） |
| 时段 | 平台规则 2026-11-15 检索，会变；上线前逐条复核 |

## T01 交付边界（骨架）
- [x] 统一包络返回、错误码与 `error-codes.json` 对齐
- [x] `outTradeNo` 幂等占位（云开发数据库查重 + 落事件）
- [x] 验签入口 + 缺密钥即拒绝
- [ ] TODO(T03)：真实验签算法（字段排序、时间戳容差）+ 单测（签名错 / 过期 / 篡改）
- [ ] TODO(T03)：幂等改为数据库唯一索引强约束（防并发双发）
- [ ] TODO(T04)：调用云托管 `/entitlement/deliver-callback` 完成额度发放与 `delivered=true`

## 环境变量（云开发控制台配置，禁止入库）
| 变量 | 用途 |
|---|---|
| `XPAY_APP_KEY` | 现网 AppKey（**沙箱 Key 不得用于生产**） |
| `DELIVER_CALLBACK_URL` | 云托管 `/entitlement/deliver-callback` 内网地址 |
| `DELIVER_CALLBACK_TOKEN` | 服务间调用令牌 |

## 部署
1. 微信开发者工具 → 云开发 → 云函数 → 上传并部署（含 `node_modules`）；
2. 在「虚拟支付 → 发货推送」里把地址指向本云函数；
3. 用沙箱订单跑一次重复推送，确认返回 `E_DELIVER_DUPLICATE` 且额度只加一次。
