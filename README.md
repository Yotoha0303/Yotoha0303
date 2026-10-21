# Yotoha | Full-Stack Engineering & Software Delivery

**Go Backend · TypeScript Frontend · AI Applications · Cloud-Native Delivery**

我围绕可交付的软件业务闭环开展工程实践：从用户与权限、核心业务流程、数据一致性，到自动化测试、部署、监控和恢复。关注的不是技术组件数量，而是系统能否正确处理真实业务场景、能否重复交付，以及出现问题后能否定位和恢复。

I build end-to-end software workflows across backend services, frontend applications, data consistency, automated testing, deployment, observability, and recovery. My focus is not the number of technologies used, but whether a system handles business cases correctly, can be delivered repeatably, and can be diagnosed and recovered when failures occur.

- **Target roles | 求职方向:** Full-Stack Engineer / Go Backend Engineer / AI Application Engineer
- **Engineering focus | 工程重点:** Business workflow integrity, identity and access control, concurrency, traceable AI workflows, CI/CD, operational readiness
- **Profile | 主页:** [github.com/Yotoha0303](https://github.com/Yotoha0303)

---

## What I Build | 工程方向

| Business / Engineering Need | Project Evidence |
| --- | --- |
| Order processing under concurrent requests | [go-order-management-system](https://github.com/Yotoha0303/go-order-management-system) — order state transitions, inventory consistency, idempotency, asynchronous timeout cancellation |
| Authentication and permission boundaries | [go-user-system](https://github.com/Yotoha0303/go-user-system) — Access/Refresh tokens, session revocation, RBAC, rate limiting, deployment checks |
| AI-assisted knowledge workflows | [KnowTrace-Workflow](https://github.com/Yotoha0303/KnowTrace-Workflow) — versioned records, evidence traceability, human review, workspace isolation |
| Repeatable cloud-native delivery | [go-order-management-system-cloudnative-lab](https://github.com/Yotoha0303/go-order-management-system-cloudnative-lab) — service boundaries, message reliability, Kubernetes delivery, fault drills, backup and restore verification |
| Guardrails for AI-assisted development | [ai-collaboration-framework](https://github.com/Yotoha0303/ai-collaboration-framework) — executable checks, stage gates, negative testing, least-privilege separation |
| Reusable QA automation environment | [QA_Automation_Architecture](https://github.com/Yotoha0303/QA_Automation_Architecture) — containerized test runtime, API/UI automation, performance testing, reports and CI integration |

## Featured Projects | 核心项目

### [KnowTrace-Workflow](https://github.com/Yotoha0303/KnowTrace-Workflow)
**AI-Assisted Knowledge Workflow with Traceable Evidence | 可追溯的 AI 知识工作流**

A full-stack application that connects capture, AI-assisted organization, evidence review, and controlled publication. AI outputs are stored as reviewable candidates tied to input versions; they do not silently overwrite source records or make final decisions. The repository includes a deployed demo, cloud and self-hosted deployment paths, CI checks, health probes, backup/restore workflows, and observability.

将记录采集、AI 辅助整理、证据审查与受控发布串成完整流程。AI 输出作为可复核候选项与输入版本关联，不直接覆盖原始记录，也不代替人工作出最终判断。仓库包含在线演示、云端与自托管部署路径、CI 检查、健康探测、备份恢复流程和可观测性配置。

**Stack:** Next.js, React, TypeScript, Tailwind CSS, Drizzle ORM, PostgreSQL, Zod, Go, Gin, GORM, Redis, Docker Compose, GitHub Actions, Prometheus, Grafana, Loki.

### [go-order-management-system](https://github.com/Yotoha0303/go-order-management-system)
**Order and Inventory Consistency | 订单与库存一致性**

A Go business service focused on the order lifecycle rather than a broad but shallow storefront. It demonstrates transaction boundaries, conditional stock updates, idempotent requests, legal state transitions, timeout cancellation, inventory reconciliation, and operational signals.

聚焦订单主链路，而非堆叠商城外围功能。通过事务边界、条件库存扣减、请求幂等、状态流转、超时取消、库存对账和运行指标，验证并发业务场景下的数据一致性处理。

**Stack:** Go, Gin, GORM, MySQL, Redis, Lua, RabbitMQ, Transactional Outbox, React, TypeScript, Docker Compose, GitHub Actions, Prometheus.

### [go-user-system](https://github.com/Yotoha0303/go-user-system)
**Authentication and Access Control | 身份认证与访问控制**

A self-hostable authentication service with browser-facing administration UI. The project covers token lifecycle, refresh rotation, session revocation and replay detection, RBAC, account/IP rate limiting, migration, security checks, and container/Kubernetes deployment manifests. The repository marks the current release as a release candidate and provides a production-readiness checklist.

可自托管的身份认证服务及管理界面，覆盖 Token 生命周期、Refresh Rotation、会话吊销与重放检测、RBAC、账号/IP 限流、数据库迁移、安全检查，以及容器和 Kubernetes 部署配置。仓库将当前版本标注为候选版本，并提供生产使用前检查清单。

**Stack:** Go, Gin, GORM, MySQL, Redis, React, TypeScript, Vite, Docker, Kubernetes, GitHub Actions, CodeQL, Prometheus.

### [go-order-management-system-cloudnative-lab](https://github.com/Yotoha0303/go-order-management-system-cloudnative-lab)
**Cloud-Native Delivery and Failure Verification | 云原生交付与故障验证**

An executable engineering lab for service boundaries, order Saga compensation, reliable messaging, worker recovery, Kubernetes deployment, immutable image releases, backup/restore, bounded load tests, and repeatable fault drills. It is explicitly a validation lab, not a claim of production-scale operation.

用于验证服务边界、订单 Saga 补偿、可靠消息、Worker 故障恢复、Kubernetes 部署、不可变镜像发布、备份恢复、有界压测和可重复故障演练的工程实验系统。项目明确定位为可执行验证实验，不将测试结果等同于生产规模承载能力。

**Stack:** Go, MySQL, RabbitMQ, Docker Compose, Kubernetes, Kustomize, GitHub Actions, GHCR, Prometheus, Grafana, OpenTelemetry, Tempo.

### [ai-collaboration-framework](https://github.com/Yotoha0303/ai-collaboration-framework)
**Quality Gates for AI-Assisted Development | AI 辅助研发质量门禁**

An experimental framework that turns development rules into executable checks. It focuses on stage-transition gates, negative testing, evidence-backed verification, least-privilege role separation, and consistency between repository state and progress documentation.

将 AI 辅助开发中的流程约束转化为可执行检查，重点覆盖阶段门禁、负向测试、基于证据的验证、最小权限角色隔离，以及仓库状态与进度文档的一致性。该项目当前标注为 Experimental。

**Stack:** PowerShell, Bash, Git hooks, CI workflows, automated verification.

### [QA_Automation_Architecture](https://github.com/Yotoha0303/QA_Automation_Architecture)
**Containerized QA Automation Toolchain | 容器化自动化测试工具链**

A reproducible test environment that separates host tools from test runtimes and supporting services. It brings together browser/API automation, performance tests, mock services, test reports, mobile automation bridges, and CI execution.

通过容器化测试运行时隔离宿主工具与测试依赖，整合浏览器/API 自动化、性能测试、Mock 服务、测试报告、移动端自动化桥接和 CI 执行。

**Stack:** Python, Pytest, Playwright, k6, Appium, PostgreSQL, Prism, Allure, Docker, Dev Containers, GitHub Actions, Jira API.

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
- **Evidence-backed quality | 质量结论有证据:** Use tests, CI results, runtime checks, and failure drills rather than relying on code presence or green-looking dashboards alone.
- **Operational readiness | 面向运行维护:** Treat logs, metrics, backup/restore, runbooks, and rollback as part of delivery—not post-release extras.
- **State project maturity honestly | 如实标注成熟度:** Distinguish implemented features, locally verified behavior, deployed demos, bounded test results, and production-scale claims.

## Current Focus | 当前重点

Build and validate small, end-to-end software products; improve Go backend and full-stack delivery; use AI where it reduces implementation or operating cost; and make reliability, security, and maintainability visible through repeatable evidence.

围绕真实业务问题构建小而完整的软件产品，持续提升 Go 后端与全栈交付能力；仅在能够降低开发或运行成本、改善产品价值时引入 AI，并通过可重复验证的证据呈现可靠性、安全性与可维护性。

---

**Note | 说明:** The repositories above are public engineering projects and labs. Benchmarks and deployment setups apply only to their documented environments and test boundaries; they should not be interpreted as production SLA or commercial customer results unless explicitly stated in the relevant repository.

以上为公开工程项目与实验系统。性能数据和部署能力仅适用于各仓库明确记录的环境与测试边界；除非对应仓库另有明确说明，不应将其理解为生产 SLA 或商业客户成果。
