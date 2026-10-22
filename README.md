# Yotoha | Full-Stack Engineer · Go Backend · AI Applications

**Business workflows → Reliable services → Tested delivery → Observable operations**

面向业务场景构建可运行、可验证、可交付的软件系统，重点关注订单与库存一致性、身份认证与权限控制、AI 辅助知识工作流，以及自动化测试和云原生交付。

I build software around practical business workflows: order and inventory consistency, authentication and authorization, AI-assisted knowledge workflows, automated testing, and cloud-native delivery.

**Target roles:** Full-Stack Engineer · Go Backend Engineer · AI Application Engineer

## Quick Navigation | 快速导航

- **[KnowTrace-Workflow](https://github.com/Yotoha0303/KnowTrace-Workflow)** — [Live Demo](https://knowtrace-workflow.vercel.app) · AI-assisted knowledge workflow with evidence traceability
- **[go-order-management-system](https://github.com/Yotoha0303/go-order-management-system)** — [Live Demo](https://go-order-management-system.vercel.app/) · order lifecycle, idempotency, inventory consistency
- **[go-user-system](https://github.com/Yotoha0303/go-user-system)** — authentication, session security, RBAC
- [Cloud-Native Validation Lab](https://github.com/Yotoha0303/go-order-management-system-cloudnative-lab) · [QA Automation](https://github.com/Yotoha0303/QA_Automation_Architecture) · [AI Development Quality Gates](https://github.com/Yotoha0303/ai-collaboration-framework)
- **Organization Collaboration | 组织协作项目:** [Koliance — NeutrinoGroup](https://github.com/NeutrinoGroup/koliance) · real-time trading UI, Go event processing, RPC resilience, CI quality gates

**Core stack:** Go · Gin · MySQL/PostgreSQL · Redis · TypeScript · React/Next.js · Docker · Kubernetes · GitHub Actions · Prometheus/Grafana

---

## Project Map | 项目索引

| Business Scenario | Engineering Evidence |
| --- | --- |
| Concurrent order and inventory updates | [go-order-management-system](https://github.com/Yotoha0303/go-order-management-system) — idempotency, conditional stock updates, state transitions, timeout cancellation |
| Secure account access and session management | [go-user-system](https://github.com/Yotoha0303/go-user-system) — token lifecycle, session revocation, RBAC, rate limiting |
| AI-assisted knowledge review and publication | [KnowTrace-Workflow](https://github.com/Yotoha0303/KnowTrace-Workflow) — versioned records, evidence traceability, human review, workspace isolation |
| Repeatable deployment and failure recovery | [go-order-management-system-cloudnative-lab](https://github.com/Yotoha0303/go-order-management-system-cloudnative-lab) — reliable messaging, Kubernetes delivery, fault drills, backup/restore verification |
| Quality control for AI-assisted development | [ai-collaboration-framework](https://github.com/Yotoha0303/ai-collaboration-framework) — automated quality gates, negative tests, least-privilege separation |
| Regression testing across APIs, UI, and performance | [QA_Automation_Architecture](https://github.com/Yotoha0303/QA_Automation_Architecture) — reproducible test runtime, API/UI automation, load tests, reports and CI |
| Autonomous-agent trust infrastructure and real-time trading workflows | [Koliance — NeutrinoGroup](https://github.com/NeutrinoGroup/koliance) — trading dashboard integration, RPC failover, Go event-listener recovery, unified errors and CI quality gates |

## Featured Projects | 核心项目

The first three projects are the main portfolio entry points: an AI workflow product, a transactional business service, and an authentication service. Each repository contains implementation details and verification boundaries.

### [KnowTrace-Workflow](https://github.com/Yotoha0303/KnowTrace-Workflow)
**AI-Assisted Knowledge Workflow with Traceable Evidence | 可追溯的 AI 知识工作流**

A full-stack application that connects capture, AI-assisted organization, evidence review, and controlled publication. AI outputs are stored as reviewable candidates tied to input versions; they do not silently overwrite source records or make final decisions. The repository includes a deployed demo, cloud and self-hosted deployment paths, CI checks, health probes, backup/restore workflows, and observability.

将记录采集、AI 辅助整理、证据审查与受控发布串成完整流程。AI 输出作为可复核候选项与输入版本关联，不直接覆盖原始记录，也不代替人工作出最终判断。仓库包含在线演示、云端与自托管部署路径、CI 检查、健康探测、备份恢复流程和可观测性配置。

**场景与收益：** 适用于需要整理大量资料、追踪 AI 生成内容依据并由人员审核后发布的知识工作流；通过版本关联和人工审核降低来源丢失与未经确认内容直接发布的风险。

**Stack:** Next.js, React, TypeScript, Tailwind CSS, Drizzle ORM, PostgreSQL, Zod, Go, Gin, GORM, Redis, Docker Compose, GitHub Actions, Prometheus, Grafana, Loki.

### [go-order-management-system](https://github.com/Yotoha0303/go-order-management-system)
**Order and Inventory Consistency | 订单与库存一致性**

A Go business service focused on the order lifecycle rather than a broad but shallow storefront. It demonstrates transaction boundaries, conditional stock updates, idempotent requests, legal state transitions, timeout cancellation, inventory reconciliation, and operational signals.

聚焦订单主链路，而非堆叠商城外围功能。通过事务边界、条件库存扣减、请求幂等、状态流转、超时取消、库存对账和运行指标，验证并发业务场景下的数据一致性处理。

**场景与收益：** 面向促销抢购、库存预扣和网络重试等订单场景，重点降低重复请求引发的重复扣减、超卖与订单状态不一致风险；具体并发能力和账实差异应以仓库中可复现的压测数据为准。

**Stack:** Go, Gin, GORM, MySQL, Redis, Lua, RabbitMQ, Transactional Outbox, React, TypeScript, Docker Compose, GitHub Actions, Prometheus.

### [go-user-system](https://github.com/Yotoha0303/go-user-system)
**Authentication and Access Control | 身份认证与访问控制**

A self-hostable authentication service with browser-facing administration UI. The project covers token lifecycle, refresh rotation, session revocation and replay detection, RBAC, account/IP rate limiting, migration, security checks, and container/Kubernetes deployment manifests. The repository marks the current release as a release candidate and provides a production-readiness checklist.

可自托管的身份认证服务及管理界面，覆盖 Token 生命周期、Refresh Rotation、会话吊销与重放检测、RBAC、账号/IP 限流、数据库迁移、安全检查，以及容器和 Kubernetes 部署配置。仓库将当前版本标注为候选版本，并提供生产使用前检查清单。

**场景与收益：** 适用于需要独立身份服务的中小型应用或内部系统；将会话生命周期、权限边界和安全检查集中管理，减少业务服务重复实现认证逻辑的成本。

**Stack:** Go, Gin, GORM, MySQL, Redis, React, TypeScript, Vite, Docker, Kubernetes, GitHub Actions, CodeQL, Prometheus.

### [go-order-management-system-cloudnative-lab](https://github.com/Yotoha0303/go-order-management-system-cloudnative-lab)
**Cloud-Native Delivery and Failure Verification | 云原生交付与故障验证**

An executable engineering lab for service boundaries, order Saga compensation, reliable messaging, worker recovery, Kubernetes deployment, immutable image releases, backup/restore, bounded load tests, and repeatable fault drills. It is explicitly a validation lab, not a claim of production-scale operation.

用于验证服务边界、订单 Saga 补偿、可靠消息、Worker 故障恢复、Kubernetes 部署、不可变镜像发布、备份恢复、有界压测和可重复故障演练的工程实验系统。项目明确定位为可执行验证实验，不将测试结果等同于生产规模承载能力。

**场景与收益：** 用于在上线前演练消息延迟、Worker 中断和恢复流程，提前发现部署、补偿与恢复机制中的缺口；它是验证环境，不代表已具备生产规模的 SLA。

**Stack:** Go, MySQL, RabbitMQ, Docker Compose, Kubernetes, Kustomize, GitHub Actions, GHCR, Prometheus, Grafana, OpenTelemetry, Tempo.

### [ai-collaboration-framework](https://github.com/Yotoha0303/ai-collaboration-framework)
**Quality Gates for AI-Assisted Development | AI 辅助研发质量门禁**

An experimental framework that turns development rules into executable checks. It focuses on stage-transition gates, negative testing, evidence-backed verification, least-privilege role separation, and consistency between repository state and progress documentation.

将 AI 辅助开发中的工程要求转化为可执行检查，覆盖端到端质量门禁（Quality Gates）、负向测试、证据化验证和最小权限隔离。该项目当前标注为 Experimental。

**场景与收益：** 用于降低 AI 辅助改动遗漏检查、缺少验证证据或越权操作的风险；目前属于实验性研发工具，不宣称已量化提升团队交付效率。

**Stack:** PowerShell, Bash, Git hooks, CI workflows, automated verification.

### [QA_Automation_Architecture](https://github.com/Yotoha0303/QA_Automation_Architecture)
**Containerized QA Automation Toolchain | 容器化自动化测试工具链**

A reproducible test environment that separates host tools from test runtimes and supporting services. It brings together browser/API automation, performance tests, mock services, test reports, mobile automation bridges, and CI execution.

通过容器化测试运行时隔离宿主工具与测试依赖，整合浏览器/API 自动化、性能测试、Mock 服务、测试报告、移动端自动化桥接和 CI 执行。

**场景与收益：** 面向需要在不同机器或 CI 环境中重复执行回归测试的项目，减少环境漂移，并让 API、UI 和性能检查能够纳入统一的交付流水线。

**Stack:** Python, Pytest, Playwright, k6, Appium, PostgreSQL, Prism, Allure, Docker, Dev Containers, GitHub Actions, Jira API.

---

## Organization Collaboration | 组织协作项目

### [Koliance — NeutrinoGroup](https://github.com/NeutrinoGroup/koliance)
**Autonomous AI Trust Infrastructure & Real-Time Trading Workflows | 自主 AI 信任基础设施与实时交易工作流**

A collaborative full-stack project combining a real-time trading dashboard with infrastructure for autonomous-agent identity, bounded delegation, and on-chain economic activity. My contribution focus spans frontend integration, backend event-processing reliability, and delivery quality—not a claim of sole ownership of the organization repository.

参与面向自主 AI Agent 的信任与经济基础设施项目，结合实时交易仪表盘、身份与授权边界及链上交互。贡献重点包括前端集成、后端事件处理可靠性和交付质量；该仓库属于组织协作项目，不代表个人独立拥有整个项目。

**Contribution areas | 贡献方向:**
- **Frontend integration | 前端集成:** Authentication-state synchronization and responsive UI fixes across trading workflows.
- **RPC resilience | RPC 韧性:** Multi-endpoint failover to improve behavior when an RPC endpoint becomes unavailable or unreliable.
- **Go event processing | Go 事件处理:** Listener watermark and reconnect handling to support recovery after interruptions.
- **Consistent error contracts | 统一错误契约:** ADR-006 unified error handling across relevant application boundaries.
- **CI quality gates | CI 质量门禁:** Test and coverage checks to make regressions visible before integration.

**Stack:** Next.js, React, TypeScript, Go, PostgreSQL/Supabase, Solidity, Monad Testnet, GitHub Actions.

---

## Live Engineering Signals | 动态工程数据

GitHub statistics cards use the standard GitHub Readme Stats theme for a cleaner, lighter presentation. These cards are rendered by the public GitHub Readme Stats service; availability and rate limits depend on that service.

GitHub 统计卡片改用主流的 GitHub Readme Stats 默认主题，采用更简洁、明亮的原生样式。卡片由其公开服务渲染，显示情况受该服务可用性与限流策略影响。

<p align="center">
  <img width="49%" src="https://github-readme-stats.vercel.app/api?username=Yotoha0303&show_icons=true&theme=default&hide_border=true&include_all_commits=true" alt="GitHub stats" />
  <img width="49%" src="https://github-readme-stats.vercel.app/api/top-langs/?username=Yotoha0303&layout=compact&theme=default&hide_border=true" alt="Top languages" />
</p>

### WakaTime Coding Activity | WakaTime 编码统计

<!--START_SECTION:waka-->
_WakaTime data will appear here after the WakaTime API key is configured and the scheduled workflow runs._
<!--END_SECTION:waka-->

---

## Representative Architecture | 代表性架构项目

| Architecture Focus | Representative Project | Evidence to Explore |
| --- | --- | --- |
| AI-assisted workflow with human review | [KnowTrace-Workflow](https://github.com/Yotoha0303/KnowTrace-Workflow) | Versioned inputs, evidence traceability, review and controlled publication |
| Transactional business workflow | [Go Order Management](https://github.com/Yotoha0303/go-order-management-system) | Idempotency, transaction boundaries, inventory consistency, asynchronous compensation |
| Identity and access control | [Go User System](https://github.com/Yotoha0303/go-user-system) | Token lifecycle, session revocation, RBAC and rate limiting |
| Cloud-native recovery and delivery | [Cloud-Native Validation Lab](https://github.com/Yotoha0303/go-order-management-system-cloudnative-lab) | Kubernetes delivery, reliable messaging, backup/restore and fault drills |
| Autonomous-agent trust and trading workflows | [Koliance — NeutrinoGroup](https://github.com/NeutrinoGroup/koliance) | RPC resilience, Go event processing, unified errors and CI quality gates |

## Project Planning & Automation | 项目管理与自动化

Use [GitHub Projects (v2)](https://github.com/Yotoha0303?tab=projects) as the portfolio-level planning surface for prioritization, status tracking, and cross-repository work. Recommended workflow: **Backlog → Ready → In Progress → In Review → Done**, with issue/PR-linked items, owner and priority fields, and automations for status changes when pull requests are opened, reviewed, merged, or issues are closed. Keep implementation evidence in repository Issues and PRs; use the Project board for coordination rather than duplicating technical details.

使用 [GitHub Projects (v2)](https://github.com/Yotoha0303?tab=projects) 作为跨仓库的项目规划看板，统一管理优先级、进度和交付状态。建议流程：**待规划 → 就绪 → 进行中 → 审查中 → 已完成**，关联 Issue/PR，并设置负责人、优先级等字段；在 PR 创建、审查、合并或 Issue 关闭时，通过自动化同步状态。技术细节和验证证据仍保留在各仓库的 Issue 与 PR 中，看板负责协调，不重复维护实现细节。

---

## Technology Stack | 技术栈

| Area | Technologies | Practical Use |
| --- | --- | --- |
| Backend | Go, Gin, GORM | REST APIs, business services, authentication, state transitions |
| Frontend | TypeScript, React, Next.js, Vite, Tailwind CSS | Admin workspaces, full-stack workflows, responsive UI |
| Data | PostgreSQL, MySQL, Redis, Drizzle ORM | Relational data, migrations, caching, session and rate-limit state |
| Messaging | RabbitMQ, Transactional Outbox, TTL/DLX | Asynchronous workflows, retries, delayed cancellation, idempotent consumers |
| Testing | Go testing, Vitest, Playwright, Pytest, k6, Appium | Unit/integration tests, E2E checks, API/UI automation, bounded load tests |
| Delivery | Docker, Docker Compose, Kubernetes, Kustomize, GitHub Actions, GHCR | Reproducible environments, CI/CD, release artifacts, deployment verification |
| Operations | Linux, Nginx, Caddy, Prometheus, Grafana, Loki, OpenTelemetry, Tempo | Health checks, metrics/logs/traces, alerting, diagnosis and recovery |
| AI application | OpenAI-compatible APIs, DeepSeek, structured workflow and review controls | AI-assisted synthesis with traceability and human review |

## Engineering Practices | 工程实践

- **Business flow first | 业务闭环优先:** Validate the complete path from request to persisted state and observable outcome.
- **Explicit consistency | 一致性显式化:** Define idempotency, transaction boundaries, retry behavior, and compensation for state changes.
- **Access control by design | 权限边界明确:** Enforce authentication, authorization, session lifecycle, and least privilege at service boundaries.
- **Repeatable delivery | 交付可重复:** Keep environments, migrations, build steps, and deployment checks versioned and executable.
- **Quality Gates & Regression Baselines | 质量门禁与回归基线:** Use automated tests, CI results, runtime checks, and repeatable failure drills to detect regressions before release.
- **Operational readiness | 面向运行维护:** Treat logs, metrics, backup/restore, runbooks, and rollback as part of delivery—not post-release extras.
- **State project maturity honestly | 如实标注成熟度:** Distinguish implemented features, locally verified behavior, deployed demos, bounded test results, and production-scale claims.

## Current Focus | 当前重点

Build and validate small, end-to-end software products; improve Go backend and full-stack delivery; use AI where it reduces implementation or operating cost; and make reliability, security, and maintainability visible through repeatable evidence.

围绕真实业务问题构建小而完整的软件产品，持续提升 Go 后端与全栈交付能力；仅在能够降低开发或运行成本、改善产品价值时引入 AI，并通过可重复验证的证据呈现可靠性、安全性与可维护性。

---

**Note | 说明:** The repositories above are public engineering projects and labs. Benchmarks and deployment setups apply only to their documented environments and test boundaries; they should not be interpreted as production SLA or commercial customer results unless explicitly stated in the relevant repository.

以上为公开工程项目与实验系统。性能数据和部署能力仅适用于各仓库明确记录的环境与测试边界；除非对应仓库另有明确说明，不应将其理解为生产 SLA 或商业客户成果。
