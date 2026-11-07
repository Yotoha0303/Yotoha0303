<div align="center">

<h1>Yotoha Engineering</h1>

<p>Data ingestion · API reliability · Agent orchestration</p>

<p>Fixed scope · Source + tests + runbook · CNY pricing</p>

</div>

## Engineering packages

| Package | Fixed fee | Delivery |
| :--- | ---: | :--- |
| PKG-02 · Zero-Support Gateway | ¥3,500 CNY | 48h, subject to agreed start conditions |

Source and test files are available for review. Acceptance criteria describe the agreed handoff; no test pass rate is inferred.

### PKG-02 · Zero-Support Gateway · ¥3,500 CNY

API 密钥鉴权、RPM/TPM 限流、SQLite 幂等计费与账单补偿。

- **固定范围**：1 个上游 API、1 个 SQLite 账本、最多 3 档 RPM/TPM/预付配额配置。
- **交付物**：完整源代码、配置样例与 README 启动/恢复手册。 自动化验收测试、样例结果与含文件 SHA-256 的交付清单。
- **验收标准**：无效密钥拒绝；重复请求只扣费一次；并发请求不透支。 重启后账本可复核；支付样例幂等入账与超期补偿测试通过。
- **范围外**：不含真实支付渠道开户、税务发票、跨区高可用与上游 API 费用。
- **工程证据**：[源码 1](https://github.com/Yotoha0303/Yotoha0303/blob/main/scripts/b2b_api_gateway_metering.py) · [源码 2](https://github.com/Yotoha0303/Yotoha0303/blob/main/scripts/zero_support_reconciliation_engine.py)
- **条款 SHA-256**：`9f53786767e02040fb3062b9a1b70322ebb1ccc77bf152660392127e3fd31f2d`

## 交付与合作约定

- 双方确认固定范围、验收样例与档期，资料/访问权限齐备且托管方确认全额入金后，48 个自然小时开始计时。
- 客户资料或权限缺失、客户依赖服务中断时，记录原因与双方确认的暂停/恢复时间；变更范围另行报价。
- 交付后 24 个自然小时内按约定样例验收或列明缺陷；托管款仅在明确验收后释放，逾期不自动放款。
- 交付后 7 个自然日内，范围内缺陷 4 个工作小时响应、9 个工作小时修复或提供可用替代方案；含一轮范围内修订。
- 周一至周五 09:00–18:00，Asia/Shanghai，按此工作时段计时。
- 供应方延迟交付每开始 24 小时抵扣服务费 10%，上限 30%；修复时限违约按同一比例抵扣，合计不超过 30%。
- 由托管方确认全额入金，客户明确验收后放款；争议依托管平台流程处理。
- 此哈希仅校验范围、价格、SLA 与证据是否改动，不证明托管款已到账。

[完整双语规格与原始条款](../portfolio.json)

[Discuss a scoped project](https://github.com/Yotoha0303/Yotoha0303/issues/new)
