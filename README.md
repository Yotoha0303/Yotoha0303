# Yotoha | AI 应用工程 · 软件交付与风险控制

我关注将 AI 能力接入真实软件流程，并处理落地过程中常见的工程问题：输出难验证、操作过程不透明、错误难追踪、权限边界不清，以及功能从本地开发到部署运行之间的断层。通过可追溯记录、验证机制、后端工程与部署实践，降低这些问题带来的交付风险。

I build toward practical AI application engineering, focusing on common delivery risks: hard-to-verify outputs, opaque execution, difficult troubleshooting, unclear permission boundaries, and gaps between local development and deployment. My projects explore traceability, verification, backend engineering, and operational practices to reduce these risks.

---

## 关注的问题 | Problems I Work On

- **AI 输出难以核验：** 保留来源、证据与人工复核路径，降低错误内容被直接当作事实使用的风险。
- **AI 辅助开发过程难追踪：** 明确任务边界、验收条件和验证证据，减少未经验证的改动进入后续阶段的风险。
- **业务状态与重复请求：** 使用事务、幂等和可靠消息处理，降低重复操作、状态不一致及数据丢失的风险。
- **身份与 API 安全：** 实践认证、授权、令牌管理和限流，降低越权访问及接口滥用风险。
- **部署后难以排错与恢复：** 练习日志、可观测性、备份恢复和故障演练，缩短定位问题与恢复服务所需的路径。

- **AI outputs are difficult to verify:** Preserve sources, evidence, and human review paths to reduce the risk of treating incorrect output as fact.
- **AI-assisted changes are hard to audit:** Define task boundaries, acceptance criteria, and verification evidence to reduce the risk of unverified changes progressing.
- **Duplicate requests and inconsistent business state:** Apply transactions, idempotency, and reliable messaging to reduce duplicate operations, inconsistent state, and message-loss risks.
- **Identity and API security:** Practice authentication, authorization, token management, and rate limiting to reduce unauthorized access and API abuse.
- **Production issues are difficult to diagnose or recover from:** Practice logging, observability, backup/recovery, and fault drills to improve troubleshooting and recovery readiness.

---

## 核心项目 | Featured Projects

### 1. [KnowTrace-Workflow](https://github.com/Yotoha0303/KnowTrace-Workflow)

**Evidence-Traceable Knowledge Workflow | 可追溯的知识工作流**

针对 AI 辅助整理中来源丢失、结论难以复核的问题，探索保留原始记录、来源线索和人工核验路径。项目结合 Web 应用、数据库、Go 服务与自托管交付；AI 工作流能力按实际实现逐步验证。

Addresses source loss and difficult-to-review conclusions in AI-assisted knowledge work by exploring record retention, source traceability, and human verification. The project combines a web application, database, Go service, and self-hosted delivery; AI workflow capabilities are validated incrementally against actual implementation.

### 2. [ai-collaboration-framework](https://github.com/Yotoha0303/ai-collaboration-framework)

**Verification Controls for AI-Assisted Development | AI 辅助开发的验证机制**

针对 AI 辅助开发中任务边界模糊、改动缺少验证、文档与实际状态不一致等风险，实践任务约束、阶段检查、证据要求、负向测试和单一事实源。当前实现重点在 Claude Code 环境与部分自动化守卫；跨工具协同仍在演进。

Explores controls for common AI-assisted development risks: unclear task boundaries, unverified changes, and drift between documentation and actual state. The repository practices task constraints, stage checks, evidence requirements, negative tests, and a single source of truth. The current implementation focuses on Claude Code and partial automated guards; cross-tool collaboration remains in progress.

### 3. [go-order-management-system](https://github.com/Yotoha0303/go-order-management-system)

**Order Integrity Under Concurrent Requests | 并发场景下的订单与库存一致性**

使用 Go 构建订单与库存系统，实践业务建模、数据库事务、并发控制、幂等和可靠消息处理，研究如何降低重复请求、库存竞争和业务状态不一致的风险。

A Go order and inventory system for practicing business modeling, database transactions, concurrency control, idempotency, and reliable messaging—focused on reducing risks from duplicate requests, inventory contention, and inconsistent business state.

### 4. [go-user-system](https://github.com/Yotoha0303/go-user-system)

**Authentication & API Access Protection | 身份认证与 API 访问保护**

实践 JWT Access/Refresh Token、权限控制、Redis 与限流机制，探索如何降低会话滥用、越权访问和接口过度请求的风险。

Practices JWT access/refresh tokens, access control, Redis, and rate limiting to explore ways to reduce session abuse, unauthorized access, and excessive API requests.

### 5. [go-order-management-system-cloudnative-lab](https://github.com/Yotoha0303/go-order-management-system-cloudnative-lab)

**Deployment, Observability & Recovery Lab | 部署、可观测性与恢复实验**

用于验证容器化交付、CI/CD、可观测性、备份恢复和故障演练流程，重点关注部署失败、问题难定位和恢复过程不可验证等风险。这是工程实验环境，不代表生产规模的运维经验。

A lab for validating container delivery, CI/CD, observability, backup/recovery, and fault-drill workflows. It focuses on risks such as failed deployments, difficult troubleshooting, and unverified recovery procedures. This is an engineering lab, not a claim of production-scale operations experience.

---

## 当前路线 | Current Roadmap

```
Choose a real user or delivery problem
                  ↓
Define expected behavior and acceptance criteria
                  ↓
Implement the smallest useful workflow
                  ↓
Add evidence, tests, permissions, and failure handling
                  ↓
Measure quality, latency, cost, and operational signals
                  ↓
Deploy, observe, troubleshoot, and iterate
```

当前优先事项：

1. 选定一个真实且范围明确的 AI 应用场景，完成端到端闭环。
2. 验证输出质量、来源追溯、人工复核和失败路径。
3. 记录模型调用、工具执行、错误、延迟与成本，保留可复现的测试样例。
4. 复用现有 Go、数据库和部署能力，不为追逐框架而重写系统。
5. 根据用户反馈、项目证据和目标岗位要求决定下一步。

Current priorities:

1. Complete one end-to-end AI use case for a real, well-scoped problem.
2. Validate output quality, source traceability, human review, and failure paths.
3. Record model/tool calls, errors, latency, and cost, with reproducible test cases.
4. Reuse existing Go, database, and deployment skills instead of rewriting systems to follow frameworks.
5. Choose the next step based on user feedback, project evidence, and target-role requirements.

---

## 工程原则 | Engineering Principles

- **先定义风险和验收标准，再选技术。** / Define risks and acceptance criteria before choosing technology.
- **关键输出必须能核验。** / Critical outputs must be verifiable.
- **工具与接口必须有明确的权限边界和失败处理。** / Tools and APIs need explicit permission boundaries and failure handling.
- **保留来源、执行记录与验证结果，方便追踪问题。** / Preserve sources, execution records, and verification results for troubleshooting.
- **明确区分已实现、实验中与计划中的能力。** / Distinguish implemented capabilities from experiments and plans.
