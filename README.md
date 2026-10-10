# Yotoha | AI 应用工程师方向 · AI Application Engineering

以 **AI 应用工程（AI Application Engineering）** 为目标方向，重点探索如何将大模型能力接入真实软件工作流，并通过后端工程、数据管理、测试与部署，让 AI 功能可验证、可维护。

Focused on **AI application engineering**: integrating LLM capabilities into real software workflows and making AI features verifiable and maintainable through backend engineering, data management, testing, and deployment.

---

## 当前方向 | Current Focus

- **主线 / Primary:** LLM application integration, structured outputs, tool calling, retrieval and evidence-grounded workflows, workflow state, evaluation, and failure handling.
- **工程基础 / Engineering Foundation:** Go, APIs, SQL, Redis, authentication/authorization, Linux, Docker, CI/CD, logs, and deployment.
- **暂缓扩展 / Deprioritized for now:** model training and research, collecting agent frameworks, complex multi-agent orchestration without a validated use case, and expanding cloud-native tooling unrelated to the current application.

目标不是堆叠 AI 名词或框架，而是构建一个 **有明确用户问题、可验证输出、可追踪执行过程、能够部署和排错** 的 AI 应用。

The goal is not to collect AI terminology or frameworks, but to build applications that solve a defined user problem, produce verifiable outputs, provide traceable execution, and can be deployed and troubleshot.

---

## 核心项目 | Featured Projects

### 1. [KnowTrace-Workflow](https://github.com/Yotoha0303/KnowTrace-Workflow)

**AI-Assisted Knowledge Workflow | AI 辅助知识工作流**

以“记录优先、证据可追溯、人工核验”为核心的知识工作流项目，探索如何让 AI 辅助整理过程保留来源与验证路径，并结合 Web 应用、数据库、Go 服务和自托管交付。

A record-first knowledge workflow focused on traceable evidence and human verification. It explores AI-assisted organization with source traceability, a web application, database, Go service, and self-hosted delivery.

### 2. [ai-collaboration-framework](https://github.com/Yotoha0303/ai-collaboration-framework)

**Human–AI Engineering Governance | 人机协同工程治理**

探索如何通过角色契约、阶段门禁、证据要求、负向测试和单一事实源约束 AI 参与软件工程的过程。当前实现重点在 Claude Code 环境与部分自动化守卫，跨工具协同仍在演进。

Explores how role contracts, stage gates, evidence requirements, negative tests, and a single source of truth can govern AI-assisted software work. The current implementation focuses on Claude Code and partial automated guards; cross-tool orchestration remains a work in progress.

### 3. [go-order-management-system](https://github.com/Yotoha0303/go-order-management-system)

**Backend Engineering Foundation | 后端工程基础**

Go 订单与库存系统，用于展示业务建模、事务、并发、幂等和消息可靠性方面的工程基础。这些能力也可用于构建有状态、可审计的 AI 应用后端。

A Go order and inventory system demonstrating business modeling, transactions, concurrency, idempotency, and reliable messaging—foundational skills for stateful and auditable AI application backends.

### 4. [go-user-system](https://github.com/Yotoha0303/go-user-system)

**Identity & Access Control | 身份与访问控制**

Go 用户认证与授权系统，覆盖 JWT、Access/Refresh Token、权限控制、Redis、限流和交付流程。它作为 AI 应用中身份、权限与 API 保护的工程基础。

A Go authentication and authorization system covering JWT access/refresh tokens, access control, Redis, rate limiting, and delivery practices—relevant foundations for identity and API protection in AI applications.

### 5. [go-order-management-system-cloudnative-lab](https://github.com/Yotoha0303/go-order-management-system-cloudnative-lab)

**Deployment & Reliability Lab | 部署与可靠性实验**

用于练习容器化交付、CI/CD、可观测性、备份恢复和故障演练的实验环境。它是工程验证项目，不代表生产级平台经验。

A lab for practicing container delivery, CI/CD, observability, backup/recovery, and fault drills. It is an engineering validation project, not a claim of production-scale platform experience.

---

## 学习路线 | Roadmap

```
Define a real user problem
          ↓
LLM API + structured outputs
          ↓
Tool calling / retrieval / evidence grounding
          ↓
Workflow state + permissions + error handling
          ↓
Evaluation + tracing + cost / latency checks
          ↓
Deploy, monitor, troubleshoot, iterate
```

当前优先事项：

1. 选定一个明确的 AI 应用场景，完成端到端可运行闭环。
2. 验证输出质量、失败路径、来源追溯和人工复核机制。
3. 记录模型调用、工具执行、错误、延迟与成本，建立可复现的评估样例。
4. 用现有 Go、数据库和部署能力支撑应用，不为使用新框架而重写系统。
5. 根据目标岗位要求和项目缺口决定下一步，不同时展开模型训练、复杂多智能体和全套云原生学习。

Current priorities: complete one end-to-end AI use case, validate output quality and failure paths, preserve evidence and human review, measure model/tool calls and cost/latency, and reuse existing backend and delivery skills instead of rewriting systems for new frameworks.

---

## 工程原则 | Engineering Principles

- 先定义用户问题，再选择模型与框架。 / Define the user problem before choosing models or frameworks.
- AI 输出需要评估与验证，不能只凭演示效果判断。 / Evaluate and verify AI outputs rather than relying on demos.
- 工具调用必须有明确权限、边界与失败处理。 / Tool calls need explicit permissions, boundaries, and failure handling.
- 保留来源、执行记录和人工复核路径。 / Preserve sources, execution traces, and human review paths.
- 区分已实现能力、实验结果与未来计划。 / Distinguish implemented capabilities, experimental results, and future plans.
