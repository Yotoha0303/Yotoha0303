# FeeGate billing adapter

TASK-20261110-COMM-05。仅依赖 Python 标准库和同目录既有采集器、x402 网关。

## 技术契约

输入为 `data/products/media_metrics.json`、`data/products/feegate_catalog.json` 和
`data/billing/pending_orders.json` 的订单数组。媒体产品必须通过采集器的完整哈希与行级校验；
目录成品复用 ops 的内容哈希、行数与元数据校验，并保留产品哈希、条数和目录价格作为账本证据。
生产拒绝媒体沙盒记录。计费政策独立于采集器的零价格占位字段。
订单字段为 `order_id/client_id/product_id/units/currency/pricing/headers`。
`units` 是实际请求交付的数据条数，不能超过产品条数；`pricing` 为 fixed 或 tiered。
固定按比例计价，每万条 USD 0.05 / CNY 0.35，向上取整到六位小数。
阶梯按当日本地日历日的已预留和已结算调用数计数：第 1–100 次原价，101–1000 次九折，随后八折。

`X-API-Key` 与环境变量 `FEEGATE_BILLING_API_KEY` 常量时间比较；密钥不得出现在订单文件或日志中，
CLI 只在未提供该请求头时从环境注入，已提供但错误的密钥必须失败。
私有账户凭单 `Payment-Voucher` 为 base64url JSON + HMAC-SHA256，
由可信后台使用 `FEEGATE_BILLING_SECRET`（至少 32 字节）签发。
凭单绑定订单、客户、产品、条数、币种、价格政策、报价、Nonce、分钟时间及模式。
这一路记录内部应收账户结算，不表示银行扣款或链上支付成功。

`PAYMENT-SIGNATURE` 使用既有网关的标准 x402 v2 Base USDC 验证器及真实 facilitator
`/verify`、`/settle`。仅 USD 支持该通道。生产模式必须显式配置
`FEEGATE_FACILITATOR_URL`，可选 `FEEGATE_FACILITATOR_TOKEN`。
协议在线上传输的 EIP-3009 时间使用协议整数；本适配器所有持久化与显示时间均为 `YYYY-MM-DD HH:MM`，
按本地时区解释，凭单前后有效窗口各五分钟。数据库不保存原始请求头、密钥、凭单或协议时间。

状态机：reserved -> settling -> settled；验证失败进入 rejected，外部结算不确定进入 reconciliation。
SQLite 唯一约束与事务在网络调用前持久化 Nonce，跨进程和重启防重放；不确定交易不会自动重试扣款。
相同订单及请求摘要重试可返回原收据。`Receipt-Hash` 只允许对同一已结算订单进行查询，不能支付新订单。
本地收据为 HMAC 防伪凭证，包含原始交易标识；不替代链上独立确认。

## CLI

```bash
python3 scripts/feegate_billing_adapter.py --settle --mock
python3 scripts/feegate_billing_adapter.py --mock-voucher
python3 scripts/feegate_billing_adapter.py --verify-receipt HASH --mock
python3 scripts/feegate_billing_adapter.py --settle --export-ledger data/billing/settlement_ledger.json
python3 scripts/feegate_billing_adapter.py --export-ledger data/billing/reconciliation.json
```

`--mock-voucher [TOKEN]` 可生成或验证模拟凭单，并隐含 `--mock`。沙盒完全不联网，自动使用内存产品，
输出到 `data/billing/mock/`，必须显式 `--mock` 才接受模拟凭单。生产缺数据、缺密钥立即失败。
`--orders`、`--database`、`--root` 可用于独立运行；输入及输出均限制在项目 data 内并拒绝符号链接。
导出采用临时文件和原子替换，数据库与临时账本权限为 0600；运行临时验证结果放 scratch。
导出路径不能覆盖数据库、密钥或输入文件；历史收据核验和单独导出不依赖当前采集文件。
`data/billing/` 已加入忽略规则，运行数据不会进入普通 Git 提交。

订单示例（由可信后台填入实际签名凭单；API Key 从环境注入）：

```json
[{"order_id":"order-001","client_id":"customer-001","product_id":"media-metrics","units":1,"currency":"USD","pricing":"fixed","headers":{"Payment-Voucher":"SIGNED_TOKEN"}}]
```

Python 后台可使用 `Billing.voucher(order)` 签发凭单，`Billing.priced(order)` 取得当前报价，
`Billing.settle(order)` 处理带请求头的订单。实际服务端应先完成授权与交付范围确认，再调用结算接口。

离线验收：`python3 -m unittest discover -s scripts -p test_feegate_billing_adapter.py`。
覆盖率可用标准库 `python3 -m trace --ignore-dir /usr --count --missing --summary --coverdir scratch/billing-coverage --module unittest discover -s scripts -p test_feegate_billing_adapter.py`；
以 feegate_billing_adapter 模块的可执行行覆盖率为门禁，要求至少 90%。

## 验收记录

新增 89 项离线测试全部通过，标准库 trace 测得核心脚本行覆盖率 99.4%。
FeeGate 关联回归通过 368 项；既有 6 项真实 HTTP 集成测试因执行沙盒禁止 socket 创建而无法运行。
原始覆盖率和回归日志位于 `scratch/sprint7-coverage/validation.log` 与 `scratch/sprint7-validation/regression.txt`。
沙盒账本样例位于 `scratch/sprint7-preview/data/billing/mock/settlement_ledger.json`。
