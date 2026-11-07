<div align="center">

<h1>Yotoha Engineering</h1>

<p>Data ingestion · API reliability · Agent orchestration</p>

<p>Fixed scope · Source + tests + runbook · CNY pricing</p>

</div>

## Engineering packages

| Package | Fixed fee | Delivery |
| :--- | ---: | :--- |
| PKG-01 · Web Harvester | ¥1,500 CNY | 48h, subject to agreed start conditions |

Source and test files are available for review. Acceptance criteria describe the agreed handoff; no test pass rate is inferred.

### PKG-01 · Web Harvester · ¥1,500 CNY

静态 HTML/JSON/CSV 采集、限次重试、去重与来源追踪。

- **固定范围**：1 个授权站点、最多 3 个静态页面模板、10 个字段；JSON/CSV 导出。
- **交付物**：完整源代码、配置样例与 README 启动/恢复手册。 自动化验收测试、样例结果与含文件 SHA-256 的交付清单。
- **验收标准**：约定样例输出字段、去重结果和来源信息与预期一致。 429/超时重试次数与等待上限测试通过；附可重复运行命令。
- **范围外**：不含 JS 渲染、验证码处理、付费数据采购与长期托管。
- **工程证据**：[源码 1](https://github.com/Yotoha0303/Yotoha0303/blob/main/scripts/rapid_lead_scraper_resilience.py)
- **条款 SHA-256**：`ff41aa834efcf9108e48fcc17907d3d5b9b0b7ea6f70ef49a43cd4f188fee525`

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
