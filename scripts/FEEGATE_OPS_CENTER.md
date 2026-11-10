# FeeGate 离线运营总控

Python 3 标准库实现，将既有 media harvester、ops pipeline 和 billing adapter 串联为
采集、规范化、商品目录、x402 模拟结算、总账审计的完整闭环。所有入口均离线。

## 使用

在项目根目录执行：

```sh
python3 scripts/feegate_ops_center.py --run-e2e --mock-run
python3 scripts/feegate_ops_center.py --audit-catalog --mock-run
python3 scripts/feegate_ops_center.py --mock
python3 scripts/feegate_ops_center.py --run-e2e
python3 scripts/feegate_ops_center.py --audit-catalog
```

`--mock-run`（别名 `--mock`）完全在内存运行，不读取或写入商品、账本、凭据文件。
单独提供该参数也会运行全链路。`--audit-catalog --mock-run` 重新生成并审计一份内存模拟账。
时间固定 `2026-11-10 08:00`，时区按 UTC+8 契约处理；报告全部时间使用 `YYYY-MM-DD HH:MM`。

`--run-e2e` 使用相同离线流程，并将演练快照原子写入
`data/ops_center/mock/settlement_ledger.json`；`--root` 可将输出放在独立测试仓库。
仅此入口写入文件，路径必须位于 root/data 内且不包含符号链接或父目录穿越。
重复演练输出相同快照，不累计成真实收入。成功退出 0；输入、审计、结算或 I/O 错误退出 2，
异常详情和凭据不写入错误输出。

## 数据与支付契约

收割阶段调用 `feegate_media_harvester.harvest(mock=True)`，覆盖四个平台原始模拟计数。
缺失播放量保持 null；字符串数量转为整数；记录保留 mock 标记和内容哈希。
商品阶段调用原收割器的 make_product/verify_catalog 和
`feegate_ops_pipeline.normalize_product`，复用清洗、脱敏、时间、定价及 schema 校验。
内存发布采用原流水线的 canonical JSON 加换行、SHA-256、内容寻址文件名及目录条目格式。
目录记录存于快照的 catalog，产物字节以 UTF-8 文本保存在 artifacts 映射中。
真实商品目录保持独立；mock 商品不进入正式商品或生产结算目录。

结算调用原 `Billing.settle`，保留 API key、金额、有效期、nonce 唯一性、幂等、
不确定结算阻断、HMAC 收据及商品哈希绑定。适配器增加 SQLite `:memory:` 支持；
mock x402 入口必须显式启用，且仅允许 mock 模式、内存数据库和 offline_only 结算方。
既有 mock 模式仍默认拒绝 x402，不改变原生产结算语义。

OfflineFacilitator 仅接受固定测试签名，不执行椭圆曲线恢复、不联网、不操作真实资金。
其收据明确标记 mode=mock、method=x402。公开 fixture HMAC key 只用于可重复沙盒校验；
内容哈希和 fixture 签名不能认证恶意修改者，也不能证明链上支付。
生产路径仍须使用真实 facilitator，不应使用本总控输出作为付款凭证。

程序 API `run_e2e(orders=[...])` 接受订单数组，每条含 order_id、client_id、product_id、
units，以及可选 currency、pricing。sandbox 禁止调用方注入 headers；币种仅支持 x402 USD，
库存为商品记录数。订单上限 10000；默认采购全部四条记录，使用 tiered 定价。
基础单价直接来自 billing.quote(1)，100 次和 1000 次调用阶梯继续复用原适配器。
金额按六位小数向上舍入，因此默认四条记录总额为 USD 0.000020。
总账保留执行顺序，以便跨阶梯价格可复核。

## 审计

`--audit-catalog` 读取上述演练快照，核验外层哈希、产物精确字节哈希、目录元数据、
记录 schema 及哈希、mock 标记、收据哈希和 fixture HMAC、商品关联、价格、时间、
唯一订单/交易标识及总额。目录或收据缺失时失败，不返回空账成功。
如存在 `data/products/feegate_catalog.json`，同时使用原 load_catalog 验证正式目录产物；
如存在 `data/products/media_metrics.json`，同时使用原 verify_catalog 验证媒体目录。
该命令审计的是演练快照中的结算账，不读取生产私钥，也不修改生产账本。

## 离线验收

```sh
python3 -m unittest discover -s scripts -p 'test_feegate_ops_center.py'
PYTHONPATH=scripts python3 -m unittest scripts.test_feegate_media_harvester scripts.test_feegate_ops_pipeline scripts.test_feegate_billing_adapter scripts.test_feegate_ops_center
python3 -m trace --count --summary --missing --coverdir /tmp/feegate-center-coverage --module unittest discover -s scripts -p 'test_feegate_ops_center.py'
```

初次交付新增 97 项测试全绿，核心脚本行覆盖率 99.5%（标准库 trace）。
测试禁止网络 socket；包含内存零文件访问、组件集成、篡改、跨账本关联、重复 nonce、
支付验证失败、结算不确定状态、阶梯价格、CLI、持久化快照及正式商品目录损坏检测。
测试夹具仅证明模拟与本地协议校验，不替代生产平台响应或真实链上测试。

额外的旧网关 HTTPIntegrationTests 有 6 项需要本机监听 socket；受限执行沙盒拒绝
创建 socket 时无法运行这些集成项。这与本总控及三个直接依赖组件的离线回归分开记录，
未修改或跳过原测试来隐藏此环境限制。
