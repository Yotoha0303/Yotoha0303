# RFC-001: 面向自主 Agent 消费的全球模型算力套利与动态路由数据产品规范
# (AI Model Cost & Routing Arbitrage Index — Machine Contract Specification)

- **标准代号**：RFC-001-AGENT-ARBITRAGE-INDEX
- **版本号**：`v1.0.0`
- **发布组织**：Autonomous Agent Architecture Working Group
- **目标消费实体**：自主 AI 智能体 (Autonomous Agents)、多模型路由网关 (Model Routers / Gateways)、企业 AI 采购决策引擎
- **核心价值定位**：**止痛药单品**。为 Agent 与开发者提供毫秒级确定性的多渠道模型真实成本、可用性评分、封号风控等级与最优下一跳路由建议。帮助 Agent 在规划复杂任务时，**直接降低 30%~70% 的推理开销，规避单点宕机**。

---

## 1. 商业化产品形态与交付分级

| 产品分级 | 交付介质 | 刷新频率 | 适用场景 | 商业定价模型 |
| :--- | :--- | :---: | :--- | :--- |
| **Freemium 样本库** | 静态 JSON (`sample_commercial_payload.json`) | 每日一次 | 开源试用、Linux.do / GitHub 获客分流 | **免费开源 (零门槛引流)** |
| **Pro 全量索引源** | 结构化全量快照 (`agent_model_arbitrage_index_latest.json`) | 每 10 分钟 | 企业私有网关集成、批量回测 | **$19/月 或 0.005 ETH** |
| **Agent 即时路由引擎** | 极简路由判定流 (`agent_routing_recommendations.json`) | 实时动态 | Agent 决策前单次 Tool Call / HTTP 402 微支付 | **$0.001 / 次调用 或 包月** |

---

## 2. 核心数据契约标准 (Schema Definition)

每个推荐路由条目必须满足以下不可变量与物理字段约束：

```json
{
  "$schema": "https://schemas.modelarbitrage.io/rfc-001-agent-arbitrage-index.json",
  "version": "1.0.0",
  "generated_at": "2026-11-08T11:50:00Z",
  "catalog_id": "ai-model-cost-arbitrage-index",
  "records_count": 24,
  "opportunities_count": 6,
  "currency_base": "USD",
  "routing_matrix": [
    {
      "task_archetype": "coding_dense",
      "model_id": "deepseek-v3",
      "target_provider": "deepseek-direct",
      "baseline_reference_provider": "azure-openai",
      "input_cost_per_m_usd": 0.14,
      "output_cost_per_m_usd": 0.28,
      "savings_pct_vs_baseline": 88.5,
      "p95_latency_ms": 320,
      "context_window_tokens": 65536,
      "sla_reliability_score": 98.5,
      "ban_risk_level": "LOW",
      "is_actionable": true,
      "verification_timestamp": "2026-11-08T11:45:00Z",
      "fallback_route": {
        "model_id": "claude-3-5-sonnet",
        "provider": "inceptionlabs"
      }
    }
  ]
}
```

### 核心字段物理约束
- `task_archetype`: 场景特化枚举 (`coding_dense`, `fast_reasoning`, `deep_research`, `micro_extraction`, `creative_synthesis`)。
- `savings_pct_vs_baseline`: 相对于官方基准价的实测净节省百分比（必须大于 0.0%）。
- `ban_risk_level`: 风控等级 (`NONE`, `LOW`, `MEDIUM`, `HIGH`)。对于共享池或灰色卡槽必须显式标记 `HIGH`，避免 Agent 资产连带被封。
- `is_actionable`: 机器可用布尔旗标。只有具备生产可用性、非模拟假数据、无高危拦截的节点方可置为 `true`。

---

## 3. 防溯源与数字主权防护
- 严禁在对外发布的 Payload 中包含内部机器 IP、内网域名、统帅私人标识或未经清洗的调试堆栈。
- 遵循零暴露审计规范，所有对外公开的样本均经由本地空气隙脱敏生成。
